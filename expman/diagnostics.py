"""Read-only local deployment checks; never starts a node or a container."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from .agent import Agent


_IMAGE = re.compile(r".+@sha256:[0-9a-f]{64}")
_CLOCK = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]-(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _run(argv, timeout=8):
    environment = dict(os.environ)
    if "--host" in argv:
        environment.pop("DOCKER_CONTEXT", None)
        environment.pop("DOCKER_HOST", None)
    return subprocess.run(argv, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace", shell=False, timeout=timeout)


def _command(argv):
    try:
        result = _run(argv)
        if result.returncode:
            return None, "命令返回错误，退出码 " + str(result.returncode)
        return result.stdout.strip()[:500], None
    except subprocess.TimeoutExpired:
        return None, "只读检查超时（8 秒），未改变配置"
    except OSError:
        return None, "无法执行命令；请检查安装位置和访问权限"


def _validate(config):
    """Reject shapes that would otherwise crash Agent.snapshot or scheduling."""
    if not isinstance(config, dict):
        return "节点配置必须是 JSON 对象"
    for key in ("node_id", "hub_url", "token", "root"):
        if not isinstance(config.get(key), str) or not config[key].strip() or "\x00" in config[key]:
            return "配置缺少有效字符串字段：" + key
    try:
        url = urlsplit(config["hub_url"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            return "hub_url 必须是没有账号密码的 HTTP(S) 地址"
        url.port
    except ValueError:
        return "hub_url 地址格式无效"
    for key in ("assets", "profiles", "gpu_policy", "policy"):
        if not isinstance(config.get(key, {}), dict):
            return key + " 必须是 JSON 对象"
    for key in ("allowed_repos", "tags"):
        values = config.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            return key + " 必须是非空字符串组成的列表"
    if not isinstance(config.get("allow_demo", False), bool):
        return "allow_demo 必须为 true 或 false"
    for key, value in config.get("assets", {}).items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value or "\x00" in value:
            return "assets 必须将资源 ID 映射到有效路径字符串"
    for item in config.get("profiles", {}).values():
        if not isinstance(item, dict) or not isinstance(item.get("image"), str):
            return "每个 profile 必须包含 image 字符串"
        patterns = item.get("gpu_name_patterns", [])
        if not isinstance(patterns, list) or any(not isinstance(x, str) or not x for x in patterns):
            return "gpu_name_patterns 必须是非空字符串组成的列表"
        if not isinstance(item.get("verified", False), bool):
            return "profile.verified 必须为 true 或 false"
    policy = config.get("policy", {})
    if not isinstance(policy.get("run_enabled", True), bool):
        return "policy.run_enabled 必须为 true 或 false"
    windows = policy.get("windows", [])
    if not isinstance(windows, list) or any(not isinstance(x, str) or not _CLOCK.fullmatch(x) for x in windows):
        return "policy.windows 应为 HH:MM-HH:MM 时间段列表"
    numbers = [(key, policy[key], key in ("max_running", "max_prefetch"))
               for key in ("max_running", "max_prefetch", "cpu_budget", "ram_budget_mb", "speed", "min_disk_free_mb") if key in policy]
    for item in config.get("gpu_policy", {}).values():
        if not isinstance(item, dict):
            return "每个 gpu_policy 必须是 JSON 对象"
        numbers.extend(("gpu_policy." + key, item[key], key == "max_jobs")
                       for key in ("reserve_mb", "max_jobs") if key in item)
    numbers.extend((key, config[key], False) for key in ("poll_seconds", "stop_grace_seconds") if key in config)
    for key, value, integer in numbers:
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 or (integer and int(value) != value)):
            return key + " 必须是有限的非负数" + ("整数" if integer else "")
    return None


def _redact(value, token):
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]") if token else value
    if isinstance(value, list):
        return [_redact(x, token) for x in value]
    if isinstance(value, dict):
        return {_redact(key, token): ("[REDACTED]" if key.lower() in ("token", "admin_token", "password") else _redact(item, token))
                for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def inspect_node(config_path):
    """Report local prerequisites, not scientific correctness or GPU runtime proof.

    Docker context metadata is read locally. Remote Docker endpoints are refused,
    even for info, so this command never contacts a remote engine or the Hub.
    """
    checks = []
    report = {"ready": False, "readiness_scope": "local_preflight",
              "gpu_runtime_validated": False, "checks": checks, "snapshot": None}

    def add(key, status, message, **details):
        checks.append({"id": key, "status": status, "message": message, **details})

    add("python", "pass" if sys.version_info >= (3, 10) else "fail",
        "Python " + sys.version.split()[0] + "；要求 3.10 或以上")
    config = None
    try:
        path = Path(config_path).expanduser().resolve()
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        problem = _validate(config)
        if problem:
            add("config", "fail", problem)
            return _redact(report, config.get("token", "") if isinstance(config, dict) and isinstance(config.get("token"), str) else "")
    except (OSError, ValueError, TypeError):
        add("config", "fail", "无法读取有效的节点 JSON 配置；请检查路径、权限和 JSON 语法")
        return report
    add("config", "pass", "配置结构有效；不会输出节点令牌")
    demo_only = config.get("allow_demo", False) and not any(config.get(key) for key in ("profiles", "gpu_policy", "allowed_repos"))
    severity = "warn" if demo_only else "fail"
    report["mode"] = "demo" if demo_only else "docker"
    add("platform", "pass" if sys.platform == "linux" else severity,
        "Linux/WSL 节点可使用 Docker 后端" if sys.platform == "linux" else
        "当前为原生 Windows/非 Linux；本代理仅支持 CPU 演示，GPU 节点应在 WSL2 Ubuntu 或 Ubuntu 中运行")
    try:
        root = Path(config["root"]).expanduser()
        if not root.is_absolute():
            root = path.parent / root
            add("root_path", "warn", "root 是相对路径，按节点配置所在目录解析")
        existing = root.resolve()
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        if not existing.is_dir():
            add("disk", "fail", "节点 root 或其已有父路径不是目录")
        else:
            free_mb = shutil.disk_usage(existing).free // (1024 * 1024)
            minimum = config.get("policy", {}).get("min_disk_free_mb", 2048)
            add("disk", "pass" if free_mb >= minimum else "fail",
                "只读检查已有父目录的剩余空间；未创建 root，也未验证写权限", free_mb=free_mb, required_mb=minimum)
    except (OSError, ValueError):
        add("disk", "fail", "无法读取节点 root 所在磁盘的信息")
    missing = []
    for name, value in config.get("assets", {}).items():
        try:
            asset = Path(value)
            if not asset.is_absolute() or not asset.exists():
                missing.append(name)
        except (OSError, ValueError):
            missing.append(name)
    add("assets", "fail" if missing else "pass",
        "资源路径必须为本节点已有的绝对路径；本检查不验证数据内容或版本",
        unavailable_ids=missing, registered_count=len(config.get("assets", {})))
    git = shutil.which("git")
    if git:
        version, error = _command([git, "--version"])
        add("git", severity if error else "pass", error or version)
    else:
        add("git", severity, "未找到 Git；Docker 实验准备代码需要 Git")
    docker = shutil.which("docker")
    endpoint = None
    if not docker:
        add("docker_cli", severity, "未找到 Docker CLI；CPU 演示不需要 Docker")
    else:
        version, error = _command([docker, "--version"])
        add("docker_cli", severity if error else "pass", error or version)
        if not error:
            # DOCKER_CONTEXT takes precedence over DOCKER_HOST in the CLI.
            context = os.environ.get("DOCKER_CONTEXT")
            if not context and os.environ.get("DOCKER_HOST"):
                endpoint = os.environ["DOCKER_HOST"]
            else:
                argv = [docker, "context", "inspect"] + ([context] if context else [])
                endpoint, error = _command(argv + ["--format", "{{.Endpoints.docker.Host}}"])
            if error:
                add("docker_endpoint", severity, "无法读取本地 Docker context 元数据：" + error)
            elif not endpoint or not (endpoint.startswith("unix:///") or endpoint.startswith("npipe:////./pipe/")):
                endpoint = None
                add("docker_endpoint", severity, "Docker 端点不是本地 socket/命名管道；已跳过，避免访问远程或网络端点")
            else:
                add("docker_endpoint", "pass", "已确认使用本地 Docker socket/命名管道")
                version, error = _command([docker, "--host", endpoint, "info", "--format", "{{.ServerVersion}}"])
                add("docker_daemon", severity if error else "pass", error or ("本地 Docker daemon 可读，版本 " + version))

    class LocalInspection(Agent):
        def _exec(self, argv, timeout=20, check=True):
            if argv[0] == "docker":
                if endpoint is None:
                    raise RuntimeError("Local Docker endpoint unavailable")
                argv = [docker, "--host", endpoint, *argv[1:]]
            elif argv[0] != "nvidia-smi":
                raise RuntimeError("Unexpected diagnostic command")
            result = _run(argv, timeout=min(timeout, 8))
            if check and result.returncode:
                raise RuntimeError("Read-only command failed")
            return result

    try:
        snapshot = LocalInspection.inspect_config(path)
        report["snapshot"] = snapshot
        add("snapshot", "pass", "已读取节点快照；未打开数据库、锁、同步连接或运行容器")
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        snapshot = {}
        add("snapshot", "fail", "无法读取节点快照；检查资源路径、Docker 状态和配置字段")
    profiles = config.get("profiles", {})
    invalid = [name for name, item in profiles.items() if not _IMAGE.fullmatch(item["image"])]
    verified = [name for name, item in profiles.items() if item.get("verified") is True and item.get("gpu_name_patterns") and name not in invalid]
    add("profiles", "fail" if invalid else ("pass" if verified else severity),
        "环境需要固定 sha256 镜像、GPU 名称匹配和 verified 登记；verified 只是已有验证声明",
        verified_names=verified, invalid_digest_names=invalid)
    add("gpu_runtime", "warn", "未拉取镜像、运行容器或执行 CUDA 运算；本报告不证明镜像可用、GPU 透传或算法兼容性")
    registered = config.get("gpu_policy", {})
    gpus = snapshot.get("gpus", [])
    usable = [gpu for gpu in gpus if gpu.get("max_jobs", 1) > 0 and gpu.get("profiles") and gpu.get("total_mb", 0) > gpu.get("reserve_mb", 2048)]
    add("gpus", "pass" if usable else severity,
        "读取到已登记且匹配环境的 GPU" if usable else "没有可用的已登记 GPU；检查 GPU UUID、显存查询、策略和环境匹配",
        registered_count=len(registered), visible_registered_count=len(gpus), usable_count=len(usable))
    if registered and len(gpus) < len(registered):
        add("gpu_missing", "warn", "部分已登记 GPU 当前没有有效遥测；不会据此派发 GPU 实验")
    add("repositories", "pass" if config.get("allowed_repos") else severity,
        "已登记代码仓库白名单；本次未连接仓库或检查提交存在性" if config.get("allowed_repos") else "未登记允许的代码仓库，无法准备 Docker 实验")
    policy = config.get("policy", {})
    if not policy.get("run_enabled", True) or policy.get("max_running", 2) == 0 or policy.get("max_prefetch", 4) == 0:
        add("scheduling", "warn", "策略已暂停接单/启动；自检不会改变策略")
    add("hub", "warn", "未连接管理中心；需要节点正常上线后确认认证与结果同步")
    report["ready"] = not any(check["status"] == "fail" for check in checks)
    return _redact(report, config["token"])
