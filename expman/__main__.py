"""Actual entry points; no installation, firewall changes, or service setup."""
import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time

from .common import atomic_json, read_json, api_request


def node_config(node_id, url, token, root, demo=False):
    return {"node_id": node_id, "hub_url": url, "token": token, "root": str(root),
            "allow_demo": demo, "poll_seconds": 0.5 if demo else 5, "stop_grace_seconds": 60,
            "allowed_repos": [], "assets": {}, "profiles": {}, "gpu_policy": {}, "tags": [],
            "policy": {"run_enabled": True, "max_running": 2, "max_prefetch": 4,
                       "cpu_budget": 4, "ram_budget_mb": 8192, "speed": 1,
                       "min_disk_free_mb": 2048, "windows": []}}


def _stop_demo(instance):
    """Stop only demo children whose process handles belong to this instance."""
    instance.mode = "drain"
    try:
        for record in instance.records():
            if record["state"] in ("starting", "running") and record["spec"]["backend"] == "demo":
                atomic_json(instance.root / "runs" / record["id"] / "STOP", {"reason": "demo closing"})
        for _ in range(10):
            for record in instance.records():
                instance._reconcile(record)
            if not instance.processes:
                return
            time.sleep(0.2)
    finally:
        # A workload sleeping for a long step may not observe STOP promptly.
        for process in list(instance.processes.values()):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for record in instance.records():
            instance._reconcile(record)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="实验台：本地管理中心与离线节点代理")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "hub", "add-node", "demo"):
        command = subs.add_parser(name)
        command.add_argument("--root", required=True, help="中心持久数据目录（含令牌，请勿提交Git）")
        if name in ("hub", "demo"):
            command.add_argument("--port", type=int, default=8765)
        if name == "hub":
            command.add_argument("--host", default="127.0.0.1", help="默认仅本机；私网访问时指定私网IP或0.0.0.0")
        if name == "add-node":
            command.add_argument("--id", required=True)
            command.add_argument("--url", required=True, help="节点能访问的管理中心地址")
            command.add_argument("--output", required=True, help="导出的节点JSON配置（含专属令牌）")
            command.add_argument("--node-root", default="/home/USER/experiment-node")
    agent = subs.add_parser("agent")
    agent.add_argument("--config", required=True)
    agent.add_argument("--once", action="store_true")
    doctor = subs.add_parser("doctor")
    doctor.add_argument("--config", required=True)
    smoke = subs.add_parser("sasrec-smoke", help="用微型数据真实验证SASRec停止恢复，不安装依赖")
    smoke.add_argument("--project", required=True)
    smoke.add_argument("--root", required=True)
    smoke.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    submit = subs.add_parser("submit")
    submit.add_argument("--root", required=True)
    submit.add_argument("--url", default="http://127.0.0.1:8765")
    submit.add_argument("--spec", required=True)
    submit.add_argument("--grid")
    submit.add_argument("--request-id", help="网络重试时使用相同ID；同ID不同内容会拒绝")
    args = parser.parse_args()
    release = read_json(Path(__file__).resolve().parents[1] / 'release-role.json')
    if release:
        worker_command = args.command in ('agent', 'doctor', 'sasrec-smoke')
        worker_edition = 'worker' in release['role']
        if worker_command != worker_edition:
            raise ValueError('Download the other edition to use this role; controller and worker are separate packages.')
    if args.command == "agent":
        from .agent import run
        run(args.config, once=args.once)
        return
    if args.command == "doctor":
        from .diagnostics import inspect_node
        report = inspect_node(args.config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ready"]:
            raise SystemExit(2)
        return
    if args.command == "sasrec-smoke":
        from .adapters.sasrec_smoke import smoke
        smoke(args.project, args.root, args.device)
        return
    if args.command == "submit":
        import uuid
        config = read_json(Path(args.root) / "hub.json")
        if not config:
            raise ValueError("Run init first with the same --root")
        payload = {"spec": read_json(args.spec), "grid": read_json(args.grid, {}) if args.grid else {},
                   "request_id": args.request_id or uuid.uuid4().hex}
        print("request_id:", payload["request_id"], flush=True)
        print(json.dumps(api_request(args.url.rstrip("/") + "/api/jobs", config["admin_token"], payload)))
        return
    from .hub import Hub, make_server, serve
    if args.command == "hub":
        serve(args.root, args.host, args.port)
        return
    hub = Hub(args.root)
    if args.command == "init":
        try:
            print("管理目录:", hub.root)
            print("管理令牌:", hub.config["admin_token"])
            print("下一步: python -m expman hub --root \"" + str(hub.root) + "\"")
        finally:
            hub.close()
        return
    if args.command == "add-node":
        try:
            output = Path(args.output)
            if output.exists():
                raise ValueError("Output already exists; choose another name to preserve node settings")
            token = hub.add_node(args.id)
            atomic_json(output, node_config(args.id, args.url, token, args.node_root))
            print("已导出节点配置:", output.resolve())
            print("配置默认没有允许的GPU/仓库/镜像。补充并验证后才接真实实验。")
        finally:
            hub.close()
        return
    # Demo is localhost only and uses our fixed CPU workload.
    from .agent import Agent
    config_path = hub.root / "demo-node.json"
    token = hub.add_node("demo-node")
    server = None
    thread = None
    instance = None
    try:
        server = make_server(hub, "127.0.0.1", args.port)
        port = server.server_address[1]
        config = node_config("demo-node", f"http://127.0.0.1:{port}", token, hub.root / "node", True)
        atomic_json(config_path, config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        instance = Agent(config_path)
        print(f"演示页面: http://127.0.0.1:{port}", flush=True)
        print("管理令牌:", hub.config["admin_token"], flush=True)
        print("本演示只运行自带CPU示例，不调用Docker或训练GPU。按Ctrl+C结束。", flush=True)
        while True:
            instance.tick()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("正在结束演示任务…", flush=True)
    finally:
        try:
            if instance is not None:
                try:
                    _stop_demo(instance)
                finally:
                    instance.close()
        finally:
            if server is not None:
                if thread is not None:
                    server.shutdown()
                    thread.join(timeout=5)
                server.server_close()
            hub.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print("错误:", error, file=sys.stderr)
        raise SystemExit(1)
