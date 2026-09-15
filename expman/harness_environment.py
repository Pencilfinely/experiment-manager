"""Prepare additive algorithm dependency profiles on an already enrolled worker.

Only dependency declarations enter an image build. Algorithm source, datasets,
controller credentials, and original profiles are never added to or replaced by
the generated environment. This module does not save the worker configuration.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess


_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)")
_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_LOCAL_IMAGE = re.compile(r"((?:localhost|127\.0\.0\.1):[0-9]+)/[^\s@]+@sha256:[0-9a-f]{64}")
_PREFIX = "EXPMAN_ENVIRONMENT="

_CHECK = '''import importlib, importlib.metadata, json, os, sys
modules, requirements, gpu_uuid = json.loads(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3]
for name in modules:
    importlib.import_module(name)
versions = {}
for requirement in requirements:
    name, expected = requirement.split('==', 1)
    actual = importlib.metadata.version(name)
    assert actual == expected, 'Dependency version mismatch: ' + requirement + ' installed=' + actual
    versions[name] = actual
if gpu_uuid:
    import torch
    assert torch.cuda.is_available(), 'CUDA unavailable'
    assert torch.cuda.device_count() == 1, 'Expected exactly one visible CUDA GPU'
    props = torch.cuda.get_device_properties(0)
    actual_uuid = str(props.uuid).lower().removeprefix('gpu-')
    assert actual_uuid == gpu_uuid.lower().removeprefix('gpu-'), 'Allocated GPU UUID mismatch'
    x = torch.ones((32, 32), device='cuda')
    assert (x @ x).sum().item() == 32768.0, 'CUDA matrix multiplication failed'
print('EXPMAN_ENVIRONMENT=' + json.dumps({'status': 'passed', 'gpu_uuid': gpu_uuid, 'versions': versions, 'imports': modules}))
'''


def validate_runtime(runtime):
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be an object")
    value = copy.deepcopy(runtime)
    for name, pattern in (("imports", _MODULE), ("requirements", _REQUIREMENT)):
        items = value.setdefault(name, [])
        if not isinstance(items, list) or any(not isinstance(item, str) or not pattern.fullmatch(item) for item in items):
            raise ValueError("runtime.imports requires Python module names; runtime.requirements requires exact package==version pins")
        value[name] = sorted(set(items))
    packages = {}
    for requirement in value["requirements"]:
        name, version = requirement.split("==", 1)
        name = re.sub(r"[-_.]+", "-", name).lower()
        if name in packages and packages[name] != version:
            raise ValueError(f"Conflicting dependency versions: {name}")
        packages[name] = version
    if value.get("profile") is not None and (not isinstance(value["profile"], str) or not value["profile"]):
        raise ValueError("runtime.profile must name an existing verified worker profile")
    return value


class _Docker:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = self.root / "environment-setup.log"
        self.endpoint = None

    def command(self, argv, check=True, timeout=180, cwd=None):
        actual = list(argv)
        environment = dict(os.environ)
        if argv[0] == "docker" and self.endpoint:
            actual = ["docker", "--host", self.endpoint, *argv[1:]]
            environment.pop("DOCKER_HOST", None)
            environment.pop("DOCKER_CONTEXT", None)
        result = subprocess.run(actual, shell=False, cwd=cwd, env=environment, timeout=timeout,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
        output = result.stdout + "\n" + result.stderr
        with self.log.open("a", encoding="utf-8") as stream:
            stream.write("\n$ " + repr(argv) + "\n" + output)
        if check and result.returncode:
            raise RuntimeError(output[-2500:] + "\nEnvironment setup log: " + str(self.log))
        return result.returncode, output

    def local_endpoint(self):
        context = os.environ.get("DOCKER_CONTEXT")
        if os.environ.get("DOCKER_HOST") and not context:
            endpoint = os.environ["DOCKER_HOST"]
        else:
            _, endpoint = self.command(["docker", "context", "inspect", *([context] if context else []),
                                        "--format", "{{.Endpoints.docker.Host}}"], timeout=20)
        endpoint = endpoint.strip()
        if not endpoint.startswith("unix:///") or any(char.isspace() for char in endpoint):
            raise ValueError("Algorithm environments require this worker's local Linux/WSL Docker socket")
        self.endpoint = endpoint

    def inventory(self, config):
        _, output = self.command(["nvidia-smi", "--query-gpu=uuid,name", "--format=csv,noheader"], timeout=20)
        result = []
        for row in csv.reader(io.StringIO(output.strip())):
            if len(row) != 2:
                raise ValueError("GPU inventory is incomplete")
            identity, name = [item.strip() for item in row]
            if not _UUID.fullmatch(identity) or not name:
                raise ValueError("GPU inventory contains an invalid identity")
            policy = config.get("gpu_policy", {}).get(identity)
            if isinstance(policy, dict) and policy.get("max_jobs", 1) > 0:
                result.append({"uuid": identity, "name": name})
        return result

    def verify(self, image, runtime, gpu=None):
        argv = ["docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                "--network", "none", "--cpus", "1", "--memory", "2g", "--tmpfs", "/tmp:rw,nosuid,size=512m"]
        if hasattr(os, "getuid"):
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        if gpu:
            argv += ["--gpus", "device=" + gpu["uuid"], "-e", "CUDA_VISIBLE_DEVICES=" + gpu["uuid"]]
        argv += ["--entrypoint", "python", image, "-c", _CHECK,
                 json.dumps(runtime["imports"]), json.dumps(runtime["requirements"]), gpu["uuid"] if gpu else ""]
        code, output = self.command(argv, check=False)
        if code:
            raise RuntimeError(output[-2500:] or "Dependency or CUDA check failed")
        receipts = [json.loads(line[len(_PREFIX):]) for line in output.splitlines() if line.startswith(_PREFIX)]
        if len(receipts) != 1 or receipts[0].get("status") != "passed" or receipts[0].get("gpu_uuid") != (gpu["uuid"] if gpu else ""):
            raise ValueError("Dependency/GPU check did not return its expected receipt")
        return receipts[0]

    def registry(self, base, base_pulled):
        local = _LOCAL_IMAGE.fullmatch(base)
        if local and base_pulled:
            return local.group(1)
        name = "expman-harness-registry"
        code, output = self.command(["docker", "container", "inspect", name], check=False, timeout=20)
        if code:
            self.command(["docker", "run", "-d", "--name", name, "--restart", "unless-stopped",
                          "--label", "expman.component=harness-registry", "-p", "127.0.0.1:5002:5000",
                          "-v", "expman-harness-registry-data:/var/lib/registry", "-e", "OTEL_TRACES_EXPORTER=none", "registry:3"], timeout=600)
        else:
            item = json.loads(output)[0]
            if (item.get("Config", {}).get("Labels", {}).get("expman.component") != "harness-registry"
                    or item.get("HostConfig", {}).get("PortBindings", {}).get("5000/tcp") != [{"HostIp": "127.0.0.1", "HostPort": "5002"}]):
                raise ValueError("An unrelated container owns the harness registry name; it was left unchanged")
            if not item.get("State", {}).get("Running"):
                self.command(["docker", "start", name])
        return "localhost:5002"

    def build(self, base, runtime):
        descriptor = {"base": base, "requirements": runtime["requirements"]}
        key = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        build_root = self.root / key
        build_root.mkdir(parents=True, exist_ok=True)
        # No project source, data, installed private node configuration, or token
        # is copied into this two-file context.
        dockerfile = ("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
                      "COPY requirements.txt /tmp/expman-requirements.txt\n"
                      'RUN ["python", "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", "--no-input", "-r", "/tmp/expman-requirements.txt"]\n'
                      "ENTRYPOINT []\n")
        expected = {"Dockerfile": dockerfile, "requirements.txt": "\n".join(runtime["requirements"]) + "\n"}
        if any(path.name not in expected for path in build_root.iterdir()):
            raise ValueError("Dependency build context contains unexpected files; refusing to upload it to Docker")
        for name, text in expected.items():
            path = build_root / name
            if path.is_symlink() or path.exists() and path.read_text(encoding="utf-8") != text:
                raise ValueError("A dependency build file was modified; refusing to overwrite it")
            path.write_text(text, encoding="utf-8")
        code, _ = self.command(["docker", "pull", base], check=False, timeout=7200)
        if code:
            self.command(["docker", "image", "inspect", base], timeout=20)
        registry = self.registry(base, base_pulled=code == 0)
        tag = registry + "/expman-harness:" + key[:24]
        self.command(["docker", "build", "--build-arg", "BASE_IMAGE=" + base, "--tag", tag, "."], cwd=build_root, timeout=7200)
        _, output = self.command(["docker", "push", tag], timeout=7200)
        digests = set(re.findall(r"\bdigest: (sha256:[0-9a-f]{64})\b", output))
        if len(digests) != 1:
            raise ValueError("Dependency image push did not return a unique registry digest")
        reference = registry + "/expman-harness@" + digests.pop()
        self.command(["docker", "pull", reference], timeout=7200)
        return reference, key


def ensure_environment(runtime, node_config, storage_root):
    """Return compatible environment choices and an additive configuration copy.

    All candidate images must pass actual single-GPU CUDA and import checks.
    Caller owns its configuration lock and atomically saves the returned config.
    Docker build/push caches can remain after failure, but no node setting is
    changed by this function.
    """
    runtime = validate_runtime(runtime)
    config = copy.deepcopy(node_config)
    candidates = [(name, value) for name, value in config.get("profiles", {}).items()
                  if isinstance(value, dict) and value.get("verified") is True
                  and _IMAGE.fullmatch(value.get("image", "")) and value.get("gpu_name_patterns")
                  and (not runtime.get("profile") or name == runtime["profile"])]
    if not candidates:
        raise ValueError("No matching verified GPU runtime. Set runtime.profile to an existing verified worker profile, or finish worker GPU setup.")
    # Previously prepared dependencies are checked first so a repeated project
    # installation reuses them without rebuilding/pushing their base again.
    candidates.sort(key=lambda item: 0 if item[1].get("requirements") == runtime["requirements"] else 1)
    docker = _Docker(Path(storage_root) / "environments")
    docker.local_endpoint()
    gpus = docker.inventory(config)
    environments, failures, checked_images, covered_gpus = [], [], set(), set()
    for name, profile in candidates:
        image = profile["image"]
        selected = [gpu for gpu in gpus if any(pattern.casefold() in gpu["name"].casefold()
                    for pattern in profile["gpu_name_patterns"])]
        check_key = (image, tuple(sorted(gpu["uuid"] for gpu in selected)))
        if check_key in checked_images or selected and {gpu["uuid"] for gpu in selected} <= covered_gpus:
            continue
        if not selected:
            failures.append(name + ": no enabled enrolled GPU matches this profile")
            continue
        try:
            derived = False
            try:
                docker.verify(image, runtime)
            except (RuntimeError, ValueError):
                if not runtime["requirements"]:
                    raise RuntimeError("Required imports are missing or broken. Add exact package==version entries to runtime.requirements.")
                image, key = docker.build(image, runtime)
                derived = True
            receipts = [docker.verify(image, runtime, gpu) for gpu in selected]
            if derived:
                gpu_names = sorted({gpu["name"] for gpu in selected})
                gpu_key = hashlib.sha256(json.dumps(gpu_names).encode()).hexdigest()[:6]
                derived_name = "harness-" + key[:20] + "-" + gpu_key
                new_profile = {"image": image, "verified": True,
                               "gpu_name_patterns": gpu_names,
                               "base_profile": name, "requirements": runtime["requirements"], "imports": runtime["imports"]}
                previous = config["profiles"].get(derived_name)
                if previous:
                    if any(previous.get(field) != new_profile[field] for field in ("image", "verified", "gpu_name_patterns", "requirements")):
                        raise ValueError("Derived profile identity already has different settings; it was left unchanged")
                else:
                    config["profiles"][derived_name] = new_profile
                name = derived_name
            checked_images.add(check_key)
            receipt_path = docker.root / (re.sub(r"[^A-Za-z0-9_.-]", "_", name) + "-verified.json")
            receipt_path.write_text(json.dumps({"image": image, "receipts": receipts}, indent=2), encoding="utf-8")
            choice = {"profile": name, "image": image}
            if choice not in environments:
                environments.append(choice)
            covered_gpus.update(gpu["uuid"] for gpu in selected)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            failures.append(name + ": " + str(error))
    if not environments:
        raise RuntimeError("No runtime passed dependency and CUDA checks:\n" + "\n".join(failures) + "\nLog: " + str(docker.log))
    return {"config": config, "environments": environments, "warnings": failures}
