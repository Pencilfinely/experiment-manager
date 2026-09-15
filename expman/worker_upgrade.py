"""Run new worker software using a discovered existing node configuration.

This entry deliberately bypasses pairing and WorkerSetup. Configuration files,
GPU policies, runtime images and recorded experiment ownership are reused.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit

from . import __version__, common


def _argument(argv, flag):
    for index, value in enumerate(argv):
        if value == flag and index + 1 < len(argv):
            return argv[index + 1]
        if value.startswith(flag + "="):
            return value[len(flag) + 1:]
    return None


def _resolve(value, cwd):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(cwd) / path).resolve()


def process_config(argv, cwd, home):
    """Recognize supported entry forms without executing or shell-parsing argv."""
    if "-m" not in argv:
        return None
    index = argv.index("-m")
    if len(argv) < index + 3:
        return None
    module, action = argv[index + 1:index + 3]
    arguments = argv[index + 3:]
    if module == "expman" and action == "agent":
        path = _argument(arguments, "--config")
        return _resolve(path, cwd) if path else None
    if module == "expman.launcher" and action == "worker":
        root = _argument(arguments, "--root")
        return (_resolve(root, cwd) if root else Path(home) / ".local/share/experiment-manager/worker") / "node.ready.json"
    if module == "expman.worker_upgrade":
        path = _argument(argv[index + 2:], "--config")
        return _resolve(path, cwd) if path else None
    return None


def _candidate(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        return None
    config = common.read_json(path)
    if not isinstance(config, dict) or any(not isinstance(config.get(key), str) or not config[key]
            for key in ("node_id", "token", "hub_url", "root")):
        return None
    parsed = urlsplit(config["hub_url"])
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        return None
    return {"path": str(path), "node_id": config["node_id"],
        "root": str(_resolve(config["root"], path.parent)),
        "hub_url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), "pids": []}


def discover_configs(home=None, proc_root="/proc"):
    """Read only known config locations and supported live same-user commands."""
    home = Path(home or Path.home()).resolve()
    found = {}
    def add(path, pid=None):
        try:
            item = _candidate(path)
        except (OSError, ValueError, TypeError):
            return
        if item:
            current = found.setdefault(item["path"], item)
            if pid is not None and pid not in current["pids"]:
                current["pids"].append(pid)
    proc_root = Path(proc_root)
    if proc_root.is_dir():
        for folder in proc_root.iterdir():
            if not folder.name.isdecimal() or int(folder.name) == os.getpid():
                continue
            try:
                if hasattr(os, "getuid") and folder.stat().st_uid != os.getuid():
                    continue
                raw = (folder / "cmdline").read_bytes()
                if len(raw) > 128 * 1024:
                    continue
                argv = [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]
                path = process_config(argv, (folder / "cwd").resolve(strict=True), home)
                if path:
                    add(path, int(folder.name))
            except (OSError, ValueError):
                continue
    add(home / ".local/share/experiment-manager/worker/node.ready.json")
    legacy = home / "expman-first-run"
    for pattern in ("*.ready.json", "node.ready.json", "*/node.ready.json", "*/*.ready.json"):
        for path in legacy.glob(pattern):
            add(path)
    return sorted(found.values(), key=lambda item: (not bool(item["pids"]), item["node_id"], item["path"]))


def choose_config(candidates, supplied=None, input_fn=input, print_fn=print):
    if supplied:
        candidate = _candidate(supplied)
        if candidate is None:
            raise ValueError("This is not an existing worker configuration / 请选择已有算力代理的配置文件")
        return candidate
    active = [candidate for candidate in candidates if candidate["pids"]]
    if len(active) == 1:
        return active[0]
    if len(candidates) == 1:
        return candidates[0]
    print_fn("Choose the existing worker / 选择原来使用的算力配置：")
    for index, candidate in enumerate(candidates, 1):
        state = "running / 运行中" if candidate["pids"] else "saved / 已保存"
        print_fn(f"[{index}] {candidate['node_id']} · {state}\n    {candidate['path']}")
    print_fn("Enter a number above, or paste the full old --config path. / 输入编号，或粘贴旧启动命令中 --config 后的完整路径。")
    selected = input_fn("Existing configuration / 原配置: ").strip().strip('"').strip("'")
    if selected.isdecimal() and 1 <= int(selected) <= len(candidates):
        return candidates[int(selected) - 1]
    if not selected:
        raise ValueError("No worker configuration selected / 尚未选择原配置")
    return choose_config([], selected, input_fn, print_fn)


def agent_is_running(root):
    """Inspect an existing lock without creating directories, files or state."""
    path = Path(root) / "agent.lock"
    try:
        stream = path.open("r+b")
    except FileNotFoundError:
        return False
    with stream:
        if os.name == "nt":
            import msvcrt
            if not path.stat().st_size:
                return False
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    return False


def run_existing(candidate, input_fn=input, print_fn=print, runner=None):
    if sys.platform != "linux":
        raise RuntimeError("Use Update-Worker.cmd to run this entry in WSL Ubuntu")
    if os.geteuid() == 0:
        raise RuntimeError("Run as the original normal Linux user, not root / 请使用原来的普通 Linux 用户")
    missing = [name for name in ("docker", "git") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Existing worker prerequisites unavailable: " + ", ".join(missing))
    ready = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, text=True, timeout=15)
    if ready.returncode:
        raise RuntimeError("Start Docker Desktop / 启动 Docker Desktop 并开启当前 Ubuntu 的 WSL 集成后重试")
    print_fn(f"Worker software / 新代理版本: {__version__}")
    print_fn(f"Node / 原节点: {candidate['node_id']}\nConfig / 原配置: {candidate['path']}")
    print_fn("Existing identity, policies, images and experiments are reused. / 继续使用原身份、策略、镜像和实验记录。")
    if runner is None:
        from .agent import run
        runner = run
    while True:
        if agent_is_running(candidate["root"]):
            print_fn("The old worker is still running. In its Ubuntu terminal press Ctrl+C, then return here.\n"
                     "旧代理仍在运行：到原来的 Ubuntu 代理窗口按 Ctrl+C，再回到这里。\n"
                     "Docker training containers keep running; the new agent reconnects to their recorded state.\n"
                     "Docker 训练容器会继续运行，新代理会接管原来的任务记录。")
            input_fn("Press Enter after the old agent exits / 旧代理退出后按回车: ")
            continue
        # Reread without rewriting, so deliberate configuration changes made
        # while the old agent was running are preserved as well.
        selected = _candidate(candidate["path"])
        if selected is None or selected["node_id"] != candidate["node_id"] or selected["root"] != candidate["root"]:
            raise ValueError("Selected worker identity/path changed; select the existing worker again")
        config = common.read_json(selected["path"])
        from .worker_setup import private_connection
        private_connection(config)
        setup = common.read_json(Path(selected["path"]).parent / "setup-state.json", {})
        endpoint = setup.get("docker_endpoint")
        if isinstance(endpoint, str) and endpoint.startswith("unix://"):
            os.environ.pop("DOCKER_CONTEXT", None)
            os.environ["DOCKER_HOST"] = endpoint
        try:
            runner(selected["path"])
            return
        except RuntimeError as error:
            if not str(error).startswith("Another agent already owns "):
                raise
            print_fn("Another worker acquired the lock. / 原代理仍占用运行目录。")


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Original node config path; skips automatic selection")
    parser.add_argument("--list", action="store_true", help="Read-only configuration discovery")
    args = parser.parse_args()
    try:
        candidates = discover_configs()
        if args.list:
            print(json.dumps(candidates, ensure_ascii=False, indent=2))
            return
        selected = choose_config(candidates, args.config)
        run_existing(selected)
    except KeyboardInterrupt:
        print("Worker upgrade entry closed / 更新入口已关闭")
    except (ValueError, OSError, RuntimeError, EOFError, subprocess.TimeoutExpired) as error:
        raise SystemExit("Cannot start existing worker / 无法启动原节点: " + str(error))


if __name__ == "__main__":
    main()
