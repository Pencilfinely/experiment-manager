"""Bridge to SASRec_Original's schema-3 training protocol.

Run in a dedicated process. The original files stay read-only; only the output
root and the post-epoch log callback are bound for this process. No training,
sampling, validation, optimizer or RNG implementation is replaced.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
import zipfile

from expman.common import atomic_json, read_json, safe_child, sha256_file
from expman.sdk import Run

PROTOCOL = "external_sasrec_original_v1"
BUNDLE_SCHEMA = 1
_MEMBERS = {"config.json", "sasrec_last.pt", "sasrec_best.pt"}
_MODULES = ("experiment", "datasets", "models", "modules", "trainers", "utils")


class SavedStop(Exception):
    """Raised only after a complete epoch has a published checkpoint bundle."""


def load_algorithm(project):
    project = Path(project).resolve()
    source = project / "src"
    if not (source / "experiment.py").is_file():
        raise ValueError("SASRec project must contain src/experiment.py")
    # SASRec uses top-level imports, which could collide with unrelated packages.
    if any(name in sys.modules for name in _MODULES):
        raise RuntimeError("SASRec adapter requires a fresh dedicated Python process")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    module = importlib.import_module("experiment")
    for name in _MODULES:
        loaded = sys.modules.get(name)
        if loaded and not Path(loaded.__file__).resolve().is_relative_to(source):
            raise ValueError("Unexpected algorithm module origin: " + name)
    if (module.CHECKPOINT_SCHEMA_VERSION != 3 or module.TRAINING_PROTOCOL != PROTOCOL
            or not all(callable(getattr(module, name, None)) for name in
                       ("load_run_config", "run_all", "_append_log"))):
        raise ValueError("Unsupported SASRec contract; adapter needs review")
    return module


def publish_bundle(run, directory, epoch):
    """Publish immutable last/best/config together, then retire older bundles."""
    folder = safe_child(run.output, "checkpoints")
    folder.mkdir(exist_ok=True)
    target = folder / ("epoch-%06d-%s.zip" % (epoch, uuid.uuid4().hex))
    temporary = target.with_suffix(".pending")
    files = [safe_child(directory, name) for name in sorted(_MEMBERS)
             if (directory / name).is_file()]
    if not {"config.json", "sasrec_last.pt"}.issubset({p.name for p in files}):
        raise ValueError("SASRec epoch did not produce config and last checkpoint")
    metadata = {"schema": BUNDLE_SCHEMA, "epoch": epoch,
                "files": {p.name: sha256_file(p) for p in files}}
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_STORED, allowZip64=True) as archive:
            for path in files:
                archive.write(path, path.name)
            archive.writestr("bundle.json", json.dumps(metadata))
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        run.publish_checkpoint(target, epoch)
    finally:
        temporary.unlink(missing_ok=True)
    # Agent snapshots files only after the process exits. Never prune before the
    # new manifest is durable; an interruption preserves the previous bundle.
    for old in folder.iterdir():
        if old != target and re.fullmatch(r"epoch-\d+-[0-9a-f]{32}\.zip", old.name):
            try:
                old.unlink()
            except OSError:
                pass
    return target


def restore_bundle(run, directory, config):
    bundle = run.checkpoint()
    if bundle is None:
        raise ValueError("Resume requires a published bundle")
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if (len(names) != len(set(names)) or set(names) - (_MEMBERS | {"bundle.json"})
                or not {"config.json", "sasrec_last.pt", "bundle.json"}.issubset(names)):
            raise ValueError("Unexpected or missing checkpoint bundle members")
        meta_info = archive.getinfo("bundle.json")
        config_info = archive.getinfo("config.json")
        if meta_info.file_size > 65536 or config_info.file_size > 1024 * 1024:
            raise ValueError("Checkpoint metadata too large")
        metadata = json.loads(archive.read("bundle.json"))
        if (metadata.get("schema") != BUNDLE_SCHEMA or
                set(metadata.get("files", {})) != set(names) - {"bundle.json"}):
            raise ValueError("Invalid checkpoint bundle manifest")
        if json.loads(archive.read("config.json")) != config:
            raise ValueError("Resume config differs from the checkpoint")
        # Validate every file before mutating the working checkpoint pair.
        for name, expected in metadata["files"].items():
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("Checkpoint member checksum mismatch: " + name)
        directory.mkdir(parents=True, exist_ok=True)
        for name in sorted(metadata["files"]):
            destination = safe_child(directory, name)
            temporary = destination.with_name("." + name + "." + uuid.uuid4().hex + ".restore")
            try:
                with archive.open(name) as source, temporary.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        if "sasrec_best.pt" not in names:
            safe_child(directory, "sasrec_best.pt").unlink(missing_ok=True)
        # Evaluation could have been interrupted after it wrote these derived
        # outputs. run_all recomputes them; they are not resume authority.
        safe_child(directory, "metrics.json").unlink(missing_ok=True)
    return metadata["epoch"]


def _provenance(project, data_root, dataset, params, torch, numpy):
    source = Path(project).resolve() / "src"
    files = sorted(source.rglob("*.py"))
    if any(path.is_symlink() or not path.resolve().is_relative_to(source) for path in files):
        raise ValueError("Algorithm sources must be regular files inside src")
    data = {split: sha256_file(safe_child(data_root, dataset + "/" + dataset + "." + split + ".txt"))
            for split in ("train", "valid", "test")}
    return {"adapter": "sasrec-v1", "protocol": PROTOCOL,
            "adapter_sha256": sha256_file(__file__),
            "sdk_sha256": sha256_file(Path(__file__).parents[1] / "sdk.py"),
            "sources": {p.relative_to(source).as_posix(): sha256_file(p) for p in files},
            "data_sha256": data, "params": params,
            "python": list(sys.version_info[:3]), "torch": torch.__version__,
            "numpy": numpy.__version__, "cuda_runtime": torch.version.cuda}


def execute(project, run=None):
    run = run or Run()
    module = load_algorithm(project)
    import torch
    import numpy

    params = dict(run.params)
    allowed = set(module._DEFAULTS) | {"dataset", "data_asset", "torch_threads"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError("Unknown SASRec parameters: " + ", ".join(sorted(unknown)))
    dataset = params.get("dataset")
    if not isinstance(dataset, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", dataset):
        raise ValueError("dataset must be a simple dataset name")
    asset = params.get("data_asset")
    if not isinstance(asset, str) or asset not in run.assets:
        raise ValueError("data_asset must name a registered shared resource")
    data_root = run.assets[asset]
    if not data_root.is_absolute() or not data_root.is_dir():
        raise ValueError("Shared data root must be an existing absolute directory")
    threads = params.get("torch_threads", 2)
    if isinstance(threads, bool) or not isinstance(threads, int) or threads < 1:
        raise ValueError("torch_threads must be a positive integer")
    torch.set_num_threads(threads)
    device = params.get("device", "cuda")
    if device not in ("cpu", "cuda") or params.get("gpu_id", 0) != 0:
        raise ValueError("Choose explicit cpu/cuda; selected GPU is always logical gpu_id 0")
    if device == "cuda" and (not torch.cuda.is_available() or torch.cuda.device_count() != 1):
        raise ValueError("CUDA adapter expects exactly one GPU visible in its container")

    current = _provenance(project, data_root, dataset, params, torch, numpy)
    provenance_path = run.output / "adapter-provenance.json"
    if run.resuming:
        if read_json(provenance_path) != current:
            raise ValueError("Source, data, parameters or runtime changed; refusing resume")
    elif provenance_path.exists() or (run.output / "sasrec").exists():
        raise ValueError("Output already contains an algorithm run; use explicit resume or a new task")

    output_root = safe_child(run.output, "sasrec")
    module.OUTPUT_ROOT = output_root
    raw = {key: value for key, value in params.items() if key in module._DEFAULTS}
    raw.update(dataset=dataset, data_root=str(data_root), output_root=str(output_root), device=device, gpu_id=0)
    input_path = run.output / "sasrec-input.json"
    # Deterministic config; on resume compare before replacing anything.
    if run.resuming:
        if read_json(input_path) != raw:
            raise ValueError("Adapter input config changed")
    else:
        atomic_json(input_path, raw)
    config = module.load_run_config(input_path, "managed")
    directory = Path(config["output_dir"])
    if run.resuming:
        restore_bundle(run, directory, config)
        (run.output / "result.json").unlink(missing_ok=True)
    else:
        atomic_json(provenance_path, current)

    output_root.mkdir(exist_ok=True)
    original_log = module._append_log
    latest_epoch = [0]

    def on_log(path, payload):
        original_log(path, payload)
        if payload.get("stage") == "train" and "global_step" in payload:
            epoch = int(payload["epoch"])
            publish_bundle(run, directory, epoch)
            latest_epoch[0] = epoch
            values = {key: payload[key] for key in
                      ("global_step", "train_loss", "epoch_seconds", "early_stop_count")
                      if payload.get(key) is not None}
            values.update({"valid/" + key: value for key, value in (payload.get("valid_metrics") or {}).items()})
            if device == "cuda":
                values["gpu_peak_allocated_mb"] = torch.cuda.max_memory_allocated() / 1048576
                values["gpu_peak_reserved_mb"] = torch.cuda.max_memory_reserved() / 1048576
            run.metric(epoch, **values)
            print(json.dumps({"epoch": epoch, **values}, ensure_ascii=False), flush=True)
            if run.should_stop():
                raise SavedStop()
        elif payload.get("status") == "skipped_complete":
            latest_epoch[0] = int(payload["epoch"])

    module._append_log = on_log
    try:
        metrics = module.run_all(config, resume_mode="require" if run.resuming else "never")
        if not (run.output / "checkpoint.json").is_file():
            raise RuntimeError("SASRec did not call the supported epoch hook; no managed checkpoint")
        if metrics:
            run.metric(latest_epoch[0], **{"test/" + key: value for key, value in metrics.items()})
        result = {"algorithm": "SASRec", "protocol": PROTOCOL, "status": "succeeded",
                  "epochs_completed": latest_epoch[0], "attempt": run.attempt, "test_metrics": metrics}
        run.finish(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    except SavedStop:
        result = {"status": "paused", "epoch": latest_epoch[0], "attempt": run.attempt}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    finally:
        module._append_log = original_log


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="SASRec外部适配器：独立进程、共享数据、完整epoch断点")
    parser.add_argument("--project", required=True, help="包含src/experiment.py的SASRec项目目录")
    args = parser.parse_args()
    execute(args.project)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as error:
        print("SASRec adapter error: " + str(error), file=sys.stderr, flush=True)
        raise SystemExit(1)

