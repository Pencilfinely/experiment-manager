"""Prepare a source snapshot and a conservative first SASRec node deployment.

``configure`` records the caller's verification: before invoking it, run the
CUDA SASRec smoke test inside the exact pinned image on each supplied GPU UUID.
A passed JSON report is an acceptance record, not proof of the image/GPU used.
This helper never starts training, changes the original Git repository, or
prints the node token. Requires Python 3.10+; configure runs on the Linux node.
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import zipfile

SOFTWARE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOFTWARE_ROOT))
from expman.common import validate_task  # noqa: E402

PROFILE = "sasrec-2080-cu128"
ASSET = "sasrec-toy-v1"
IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
TOY_SPLITS = {
    "train": "1 1 2 3 4\n2 2 3 5\n",
    "valid": "1 1 2 3 4 5\n2 2 3 5 6\n",
    "test": "1 1 2 3 4 5 6\n2 2 3 5 6 7\n",
}


def _regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular file, without symlinks: " + str(path))
    return path


def _python_files(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Expected a source directory, without symlinks: " + str(root))
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        for name in directories:
            if (Path(folder) / name).is_symlink():
                raise ValueError("Source symlinks cannot be packed: " + str(Path(folder) / name))
        for name in sorted(files):
            if name.endswith(".py"):
                yield _regular(Path(folder) / name)


def pack(project, output):
    """Archive current source bytes; do not inspect/stage/commit original Git."""
    project = Path(project).expanduser()
    if project.is_symlink():
        raise ValueError("Project directory cannot be a symlink")
    project = project.resolve()
    _regular(project / "src" / "experiment.py")
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Archive output already exists; choose a new --output")
    entries = [(path, "sasrec-source/SASRec_Original/src/" + path.relative_to(project / "src").as_posix())
               for path in _python_files(project / "src")]
    entries += [(path, "experiment_manager/expman/" + path.relative_to(SOFTWARE_ROOT / "expman").as_posix())
                for path in _python_files(SOFTWARE_ROOT / "expman")]
    for path in sorted((SOFTWARE_ROOT / "deploy").glob("Dockerfile.sasrec*")):
        entries.append((_regular(path), "experiment_manager/deploy/" + path.name))
    for relative in ("scripts/sasrec_first_run.py", "examples/sasrec-task.template.json"):
        entries.append((_regular(SOFTWARE_ROOT / relative), "experiment_manager/" + relative))
    manifest = {"schema": 1, "source_kind": "current-working-directory-snapshot",
                "git_inspected_or_modified": False,
                "note": "Current file bytes, including uncommitted/untracked source; no original Git history, data, results or credentials.",
                "source_directories": {"algorithm": str(project), "software": str(SOFTWARE_ROOT)}, "files": {}}
    # Exclusive creation protects existing output even if another process creates it.
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in entries:
            data = path.read_bytes()
            archive.writestr(name, data)
            manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        archive.writestr("source-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return output


def _run(argv):
    result = subprocess.run(argv, check=False, text=True, encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, shell=False)
    if result.returncode:
        # Do not echo configuration or arbitrary command output containing secrets.
        raise ValueError(argv[0] + " check failed; verify the repository/GPU locally")
    return result.stdout.strip()


def _read_json(path):
    value = json.loads(_regular(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return value


def _private_write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(data)


def _write_json(path, value):
    _private_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def configure(config_path, repo, image, gpus, smoke_reports, output_dir):
    """Record caller-verified pinned-image/UUID smoke tests and emit new files."""
    if sys.platform != "linux":
        raise ValueError("Run configure in Ubuntu/Linux on the 2080 Ti node")
    if not IMAGE.fullmatch(image):
        raise ValueError("--image must be repository@sha256: followed by 64 lowercase hexadecimal characters")
    if not gpus or len(gpus) != len(smoke_reports) or len(set(gpus)) != len(gpus):
        raise ValueError("Supply unique --gpu values and one --smoke-report for each, in the same order")
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Output directory already exists; choose a new --output-dir")
    repo = Path(repo).expanduser()
    if not repo.is_absolute() or repo.is_symlink() or not repo.is_dir():
        raise ValueError("--repo must be the existing absolute Git repository directory")
    repo = repo.resolve()
    if output == repo or output.is_relative_to(repo):
        raise ValueError("--output-dir must be outside the source repository")
    _regular(repo / "SASRec_Original" / "src" / "experiment.py")
    if Path(_run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"])).resolve() != repo:
        raise ValueError("--repo must point to the Git repository root")
    commit = _run(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"])
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Repository HEAD must be a full 40-character commit")
    _run(["git", "-C", str(repo), "cat-file", "-e", "HEAD:SASRec_Original/src/experiment.py"])
    if _run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--", "SASRec_Original/src"]):
        raise ValueError("Exported SASRec source differs from HEAD; commit the exported snapshot before smoke/configure")
    config = _read_json(Path(config_path).expanduser().resolve())
    for key in ("node_id", "hub_url", "token"):
        if not isinstance(config.get(key), str) or not config[key].strip() or "\x00" in config[key]:
            raise ValueError("Node config is missing a valid " + key)
    url = urlsplit(config["hub_url"])
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise ValueError("Node hub_url must be an HTTP(S) address without embedded credentials")
    url.port  # Reject invalid ports before writing anything.
    records = []
    for gpu, report_path in zip(gpus, smoke_reports):
        path = Path(report_path).expanduser().resolve()
        report = _read_json(path)
        if report.get("status") != "passed" or report.get("device") != "cuda":
            raise ValueError("Each smoke report must have status=passed and device=cuda: " + str(path))
        records.append({"gpu_uuid": gpu, "smoke_report": str(path),
                        "smoke_report_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    detected = {}
    query = _run(["nvidia-smi", "--query-gpu=uuid,name,memory.total,memory.free", "--format=csv,noheader,nounits"])
    for row in csv.reader(io.StringIO(query)):
        if len(row) != 4:
            raise ValueError("nvidia-smi must return uuid,name,memory.total,memory.free for every GPU")
        gpu, name, total, free = [value.strip() for value in row]
        if not UUID.fullmatch(gpu) or gpu in detected or not name or not total.isdigit() or not free.isdigit():
            raise ValueError("nvidia-smi returned invalid UUID or nonnumeric memory (including N/A); resolve this first")
        total, free = int(total), int(free)
        if not 0 <= free <= total or total <= 0:
            raise ValueError("nvidia-smi returned invalid GPU memory values")
        detected[gpu] = {"name": name, "total_mb": total, "free_mb": free}
    for gpu in gpus:
        if gpu not in detected:
            raise ValueError("Selected --gpu UUID was not found by nvidia-smi: " + gpu)
        if "2080 ti" not in detected[gpu]["name"].casefold():
            raise ValueError("This first-run profile requires an RTX 2080 Ti: " + gpu)
        if detected[gpu]["total_mb"] < 4096:
            raise ValueError("Selected GPU needs at least 4096 MiB total for task + reserve")
    result = copy.deepcopy(config)
    result.update(root=str(Path.home() / "ExperimentNode"), allow_demo=False,
                  allowed_repos=[str(repo)], assets={ASSET: str(output / "assets")}, tags=["win2080"],
                  profiles={PROFILE: {"image": image, "gpu_name_patterns": ["2080 Ti"], "verified": True}},
                  gpu_policy={gpu: {"reserve_mb": 2048, "max_jobs": 1 if gpu in gpus else 0} for gpu in detected},
                  stop_grace_seconds=600,
                  policy={"run_enabled": True, "max_running": 1, "max_prefetch": 2, "cpu_budget": 4,
                          "ram_budget_mb": 8192, "windows": [], "speed": 1, "min_disk_free_mb": 2048})
    task = validate_task({
        "name": "SASRec-Toy-first-run", "algorithm": "SASRec", "group": "sasrec-toy-acceptance",
        "metric_protocol": "external_sasrec_original_v1__synthetic_toy_acceptance_only", "backend": "docker",
        "source": {"repo": str(repo), "commit": commit},
        "command": ["python", "-m", "expman.adapters.sasrec", "--project", "/workspace/code/SASRec_Original"],
        "environments": [{"profile": PROFILE, "image": image}], "assets": [ASSET], "tags": ["win2080"],
        "params": {"dataset": "Toy", "data_asset": ASSET, "device": "cuda", "gpu_id": 0,
                   "torch_threads": 1, "seed": 42, "epochs": 3, "star_test": -1, "batch_size": 2,
                   "max_seq_length": 5, "hidden_size": 8, "num_hidden_layers": 1, "num_attention_heads": 1,
                   "hidden_dropout_prob": 0.2, "attention_probs_dropout_prob": 0.2,
                   "candidate_chunk_size": 4, "patience": 10},
        "resources": {"gpu_memory_mb": 2048, "cpu": 2, "ram_mb": 4096, "exclusive": True},
    })
    record = {"schema": 1, "image": image, "source": task["source"], "gpus": records,
              "detected_gpus": detected, "synthetic_data": True,
              "verification_kind": "caller-confirmed-image-and-GPU-smoke",
              "note": "Reports do not prove which Docker image or physical GPU was used. Caller must verify the pinned image and each selected UUID before configure."}
    # Validate everything before creating the directory. Never overwrite inputs.
    output.mkdir(parents=True, mode=0o700)
    (output / "assets" / "Toy").mkdir(parents=True, mode=0o700)
    for split, data in TOY_SPLITS.items():
        _private_write(output / "assets" / "Toy" / ("Toy." + split + ".txt"), data)
    _write_json(output / "win2080.ready.json", result)
    _write_json(output / "sasrec-toy.task.json", task)
    _write_json(output / "deployment-record.json", record)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser("pack", help="Pack current Python sources without reading/changing original Git")
    archive.add_argument("--project", required=True, help="SASRec_Original directory containing src/experiment.py")
    archive.add_argument("--output", required=True, help="New ZIP file; parent directory must exist")
    node = commands.add_parser("configure", help="Record manually verified image/UUID smoke and create node/task files",
                               description="Run on the Linux node, after smoke has passed inside the exact --image on every --gpu UUID. Reports are records, not hardware proof.")
    node.add_argument("--config", required=True, help="JSON exported by expman add-node; kept unchanged")
    node.add_argument("--repo", required=True, help="Absolute path to the exported Git repository root on this node")
    node.add_argument("--image", required=True, help="Exact smoke-tested repository@sha256:... image")
    node.add_argument("--gpu", action="append", required=True, help="Smoke-tested physical GPU UUID (repeatable; first run: one)")
    node.add_argument("--smoke-report", action="append", required=True, help="Passed CUDA report, one per --gpu in the same order")
    node.add_argument("--output-dir", required=True, help="New directory outside the source repository")
    args = parser.parse_args(argv)
    if args.command == "pack":
        output = pack(args.project, args.output)
    else:
        output = configure(args.config, args.repo, args.image, args.gpu, args.smoke_report, args.output_dir)
    print("Created:", output)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        print("First-run setup error:", error, file=sys.stderr)
        raise SystemExit(1)
