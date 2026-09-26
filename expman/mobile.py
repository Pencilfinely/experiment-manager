"""Revocable, scoped mobile access to the existing controller."""
from __future__ import annotations

import hashlib
import secrets
import uuid

from .common import now


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS mobile_devices (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, permission TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL,
        expires REAL NOT NULL, revoked REAL)""")


def _pick(value, keys):
    return {key: value[key] for key in keys if key in value}


def _job(value):
    result = _pick(value, ("id", "state", "node_id", "detail", "created", "updated",
                          "attempt", "metrics", "timing", "command_id", "command_ack", "action"))
    result["spec"] = _pick(value["spec"], ("name", "algorithm", "group", "params", "resume_supported"))
    return result


class MobileHubMixin:
    def mobile_devices(self):
        with self.lock:
            return {"devices": [dict(row) for row in self.db.execute(
                "SELECT id,name,permission,created,expires,revoked FROM mobile_devices ORDER BY created DESC")],
                "time": now()}

    def mobile_enroll(self, payload):
        from .hub import APIError, _object
        _object(payload, "request")
        name, permission = payload.get("name"), payload.get("permission", "monitor")
        days = payload.get("days", 30)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise APIError(400, "请输入 1–80 字的设备名称")
        if permission not in ("monitor", "control"):
            raise APIError(400, "权限必须为 monitor 或 control")
        if type(days) is not int or not 1 <= days <= 90:
            raise APIError(400, "有效期必须为 1–90 天")
        token, identity, timestamp = secrets.token_urlsafe(32), uuid.uuid4().hex, now()
        with self.transaction():
            count = self.db.execute("SELECT COUNT(*) FROM mobile_devices WHERE revoked IS NULL AND expires>?", (timestamp,)).fetchone()[0]
            if count >= 100:
                raise APIError(409, "有效移动凭证已达 100 个，请先撤销不用的凭证")
            self.db.execute("INSERT INTO mobile_devices VALUES (?,?,?,?,?,?,NULL)",
                            (identity, name.strip(), permission, hashlib.sha256(token.encode()).hexdigest(),
                             timestamp, timestamp + days * 86400))
        return {"id": identity, "name": name.strip(), "permission": permission,
                "expires": timestamp + days * 86400, "token": token}

    def mobile_revoke(self, payload):
        from .hub import APIError, _object
        _object(payload, "request")
        identity = payload.get("id")
        if not isinstance(identity, str):
            raise APIError(400, "缺少设备 ID")
        with self.transaction():
            if self.db.execute("UPDATE mobile_devices SET revoked=COALESCE(revoked,?) WHERE id=?", (now(), identity)).rowcount != 1:
                raise APIError(404, "找不到移动设备")
        return {"id": identity, "revoked": True}

    def mobile_authenticate(self, header):
        from .hub import APIError
        if not isinstance(header, str) or not header.startswith("Bearer ") or not 1 <= len(header[7:]) <= 512:
            raise APIError(401, "请输入管理端签发的移动访问凭证")
        digest = hashlib.sha256(header[7:].encode()).hexdigest()
        with self.lock:
            row = self.db.execute("SELECT id,name,permission,expires FROM mobile_devices "
                                  "WHERE token_hash=? AND revoked IS NULL AND expires>?", (digest, now())).fetchone()
            if row is None:
                raise APIError(401, "移动凭证无效、已过期或已撤销，请重新连接")
            return dict(row)

    def mobile_state(self):
        state = self.state()
        nodes = []
        for value in state["nodes"]:
            node = _pick(value, ("id", "last_seen", "online", "mode"))
            snapshot = value["snapshot"]
            node["snapshot"] = _pick(snapshot, ("free_ram_mb", "disk_free_mb", "policy"))
            node["snapshot"]["gpus"] = [_pick(gpu, ("uuid", "name", "total_mb", "free_mb", "local_enabled"))
                                        for gpu in snapshot.get("gpus", [])]
            nodes.append(node)
        return {"jobs": [_job(value) for value in state["jobs"]], "nodes": nodes,
                "time": state["time"], "version": state["version"]}

    def mobile_job(self, identity):
        value = self.job(identity)
        return {**_job(value), "time": value["time"], "log_tail": value["log_tail"],
                "events": value["events"], "artifacts": value["artifacts"]}

    def mobile_request(self, header, method, path, payload=None, identity=None):
        """Keep revocation, authorization and execution under the same hub lock."""
        from .hub import APIError, _integer, _object
        with self.lock:
            session = self.mobile_authenticate(header)
            if method == "GET":
                if path == "/api/mobile/state":
                    return {**self.mobile_state(), "session": session}
                if path == "/api/mobile/job":
                    return self.mobile_job(identity)
                raise APIError(404, "找不到移动接口")
            if method != "POST":
                raise APIError(405, "不支持的请求方法")
            if path not in ("/api/mobile/action", "/api/mobile/node-mode"):
                raise APIError(404, "找不到移动接口")
            if session["permission"] != "control":
                raise APIError(403, "此设备仅有查看权限")
            _object(payload, "request")
            if path == "/api/mobile/action":
                expected = _integer(payload.get("expected_command_id"), "expected_command_id")
                return self.action({"job_id": payload.get("job_id"), "action": payload.get("action"),
                                    "expected_command_id": expected})
            if payload.get("expected_mode") not in ("run", "drain"):
                raise APIError(400, "请先刷新节点状态")
            return self.set_mode({"node_id": payload.get("node_id"), "mode": payload.get("mode"),
                                  "expected_mode": payload["expected_mode"]})
