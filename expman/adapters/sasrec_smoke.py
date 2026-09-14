"""Opt-in real SASRec smoke test; uses tiny generated data and fixed outputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from expman.common import atomic_json, read_json, sha256_file


def _equal(first, second, location="state"):
    import numpy as np
    import torch
    if isinstance(first, torch.Tensor):
        ok = isinstance(second, torch.Tensor) and torch.equal(first, second)
    elif isinstance(first, np.ndarray):
        ok = isinstance(second, np.ndarray) and np.array_equal(first, second)
    elif isinstance(first, dict):
        ok = isinstance(second, dict) and first.keys() == second.keys()
        if ok:
            for key in first:
                _equal(first[key], second[key], location + "." + str(key))
    elif isinstance(first, (list, tuple)):
        ok = type(first) is type(second) and len(first) == len(second)
        if ok:
            for index, (a, b) in enumerate(zip(first, second)):
                _equal(a, b, location + "." + str(index))
    else:
        ok = first == second
    if not ok:
        raise ValueError("Continuous/resumed states differ at " + location)


def smoke(project, root, device="cpu"):
    # Import dependencies before allocating a new output root.
    import torch
    import numpy
    project, root = Path(project).resolve(), Path(root).resolve()
    if root.exists():
        raise ValueError("Smoke output already exists; choose a new --root to preserve evidence")
    if not (project / "src" / "experiment.py").is_file():
        raise ValueError("SASRec project missing src/experiment.py")
    if root == project or root.is_relative_to(project):
        raise ValueError("Smoke output must be outside the original algorithm project")
    sources = {str(p): sha256_file(p) for p in (project / "src").rglob("*.py")}
    root.mkdir(parents=True)
    data = root / "assets" / "Toy"
    data.mkdir(parents=True)
    for split, text in {
        "train": "1 1 2 3 4\n2 2 3 5\n",
        "valid": "1 1 2 3 4 5\n2 2 3 5 6\n",
        "test": "1 1 2 3 4 5 6\n2 2 3 5 6 7\n",
    }.items():
        (data / ("Toy." + split + ".txt")).write_text(text, encoding="utf-8")
    params = {"dataset": "Toy", "data_asset": "toy-v1", "device": device,
              "torch_threads": 1, "seed": 42, "epochs": 3, "star_test": -1,
              "batch_size": 2, "max_seq_length": 5, "hidden_size": 8,
              "num_hidden_layers": 1, "num_attention_heads": 1,
              "hidden_dropout_prob": 0.2, "attention_probs_dropout_prob": 0.2,
              "candidate_chunk_size": 4, "patience": 10}
    software = Path(__file__).resolve().parents[2]
    def launch(name, attempt=1, stop=False):
        output = root / name
        output.mkdir(exist_ok=True)
        atomic_json(output / "params.json", params)
        if stop:
            (output / "STOP").write_text("smoke: stop after first complete epoch", encoding="utf-8")
        elif attempt > 1:
            (output / "STOP").unlink(missing_ok=True)
        env = dict(os.environ, EXPERIMENT_OUTPUT=str(output),
                   EXPERIMENT_PARAMS=str(output / "params.json"),
                   EXPERIMENT_ASSETS=json.dumps({"toy-v1": str(data.parent)}),
                   EXPERIMENT_RESUME="1" if attempt > 1 else "0",
                   EXPERIMENT_ATTEMPT=str(attempt), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=str(software), OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
        log = output / ("smoke-attempt-%s.log" % attempt)
        print("SASRec smoke:", name, "attempt", attempt, "device", device, flush=True)
        with log.open("wb") as stream:
            result = subprocess.run([sys.executable, "-B", "-m", "expman.adapters.sasrec",
                                     "--project", str(project)], cwd=software, env=env,
                                    stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=180, shell=False)
        if result.returncode:
            raise RuntimeError("SASRec smoke failed; inspect " + str(log))
        return output

    full = launch("continuous")
    resumed = launch("resumed", stop=True)
    paused = read_json(resumed / "checkpoint.json")
    if not paused or paused["step"] != 1 or (resumed / "result.json").exists():
        raise ValueError("Cooperative stop did not save exactly the first complete epoch")
    # A half-written working file must not override the immutable resume bundle.
    (resumed / "sasrec" / "Toy" / "managed" / "sasrec_last.pt").write_bytes(b"simulate interrupted working file")
    launch("resumed", attempt=2)
    states = [torch.load(path / "sasrec" / "Toy" / "managed" / "sasrec_last.pt",
                         map_location="cpu", weights_only=False) for path in (full, resumed)]
    fields = ["model", "optimizer", "rng_state", "early_stopping", "global_step", "epoch"]
    for field in fields:
        _equal(states[0][field], states[1][field], field)
    _equal(read_json(full / "result.json")["test_metrics"],
           read_json(resumed / "result.json")["test_metrics"], "test_metrics")
    after = {str(p): sha256_file(p) for p in (project / "src").rglob("*.py")}
    if after != sources:
        raise ValueError("Original sources changed during smoke test")
    report = {"status": "passed", "device": device, "synthetic_data": True,
              "epochs": 3, "pause_after_epoch": 1, "resume_attempt": 2,
              "compared_exactly": fields + ["test_metrics"],
              "original_sources_unchanged": True,
              "torch": torch.__version__, "numpy": numpy.__version__,
              "gpu_container_verified": False,
              "note": "Direct process test. GPU/Docker deployment requires running inside the target container."}
    atomic_json(root / "smoke-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description="用合成小数据真实训练SASRec，校验停止恢复；不安装环境")
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", required=True, help="新建且位于算法目录外的测试结果目录")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    args = parser.parse_args()
    smoke(args.project, args.root, args.device)


if __name__ == "__main__":
    main()

