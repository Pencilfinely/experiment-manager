"""Built-in CPU-only example; metrics are synthetic, not research results."""
import json
import os
import random
import time

from .sdk import Run, _atomic


def parent_alive():
    parent = int(os.environ.get("EXPERIMENT_AGENT_PID", "0"))
    if not parent:
        return True
    if os.name != "nt":
        return os.getppid() == parent
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, parent)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def main():
    run = Run()
    steps = int(run.params.get("steps", 10))
    delay = float(run.params.get("delay", 0.2))
    rng = random.Random(int(run.params.get("seed", 42)))
    start = 0
    checkpoint = run.checkpoint() if run.resuming else None
    if run.resuming and checkpoint is None:
        raise RuntimeError("Resume was requested but there is no checkpoint")
    if checkpoint:
        with checkpoint.open(encoding="utf-8") as stream:
            saved = json.load(stream)
        start = saved["step"]
        # This demo consumes one RNG draw per completed step.
        for _ in range(start):
            rng.random()
    for step in range(start + 1, steps + 1):
        if not parent_alive():
            print("Agent stopped; ending demo without an orphan writer", flush=True)
            return 76
        if run.should_stop():
            print("Cooperative stop at completed step", step - 1, flush=True)
            return 75
        time.sleep(delay)
        loss = 1 / step + rng.random() * 0.01
        run.metric(step, loss=loss, score=1 - min(loss, 1))
        _atomic(run.output / "checkpoints" / "latest.json", {"step": step})
        run.publish_checkpoint("checkpoints/latest.json", step)
        print(f"demo step={step}/{steps} loss={loss:.6f}", flush=True)
    run.finish({"kind": "synthetic-demo", "steps": steps, "seed": run.params.get("seed", 42)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
