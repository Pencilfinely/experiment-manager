"""Generic release worker: pair, inspect, build, verify and start without JSON edits."""
import codecs
from collections import deque
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from . import __version__
from .common import atomic_json, read_json, validate_task
from .launcher import InstanceLock
from .pairing import validate_pairing

BASE_IMAGE = 'pytorch/pytorch@sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2'
REGISTRY_IMAGE = 'registry:3'
GPU_UUID = re.compile(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
COMMAND_OUTPUT_LIMIT = 1024 * 1024
COMMAND_HEARTBEAT_SECONDS = 30
DOCKERFILE = '''ARG BASE_IMAGE=''' + BASE_IMAGE + '''
FROM ${BASE_IMAGE}
ENV PYTHONPATH=/opt/experiment-manager PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache OMP_NUM_THREADS=2 MKL_NUM_THREADS=2
COPY expman /opt/experiment-manager/expman
WORKDIR /workspace/code
ENTRYPOINT []
CMD ["python", "-m", "expman.gpu_check"]
'''


class SetupCancelled(RuntimeError):
    """A cooperative exit canceled this setup's own command process."""


def _check_cancel(cancel_requested):
    if cancel_requested is not None and cancel_requested():
        raise SetupCancelled('Setup canceled for application exit / 已取消环境准备，正在退出')


def private_write(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Configuration path must not be a symbolic link')
    atomic_json(path, value)
    if os.name != 'nt':
        path.chmod(0o600)


def private_connection(pairing):
    """Do not send private controller traffic through a system web proxy."""
    hostname = urlsplit(pairing['hub_url']).hostname
    for key in ('no_proxy', 'NO_PROXY'):
        entries = [v for v in os.environ.get(key, '').split(',') if v]
        if hostname not in entries:
            os.environ[key] = ','.join(entries + [hostname])


def authenticate(pairing):
    request = urllib.request.Request(pairing['hub_url'] + '/api/node-info',
        headers={'Authorization': 'Bearer ' + pairing['token']})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
            result = json.load(response)
    except OSError as error:
        raise RuntimeError('Cannot pair with controller. Check its address, network/firewall and version (0.2+). '
                           '/ 无法配对，请检查主控地址、网络、防火墙及主控版本。') from error
    if result.get('node_id') != pairing['node_id'] or result.get('paired') is not True:
        raise ValueError('Pairing identity mismatch / 配对身份不符')


class WorkerSetup:
    def __init__(self, root, pairing, *, cancel_requested=None):
        self.cancel_requested = cancel_requested
        self.root = Path(root).expanduser().resolve()
        if ',' in str(self.root):
            raise ValueError('Worker data path cannot contain a comma')
        self.pairing = validate_pairing(pairing)
        self.state_path = self.root / 'setup-state.json'
        self.state = read_json(self.state_path, {})
        if self.state and self.state.get('node_id') != self.pairing['node_id']:
            raise ValueError('This data directory belongs to another worker')
        self.state['node_id'] = self.pairing['node_id']
        self.log_path = self.root / 'setup.log'
        self.endpoint = None

    def save(self):
        private_write(self.state_path, self.state)

    def command(self, argv, timeout=1800, check=True, cwd=None, *, progress=False):
        _check_cancel(self.cancel_requested)
        self.root.mkdir(parents=True, exist_ok=True)
        actual = list(argv)
        environment = dict(os.environ)
        if argv[0] == 'docker' and self.endpoint:
            actual = ['docker', '--host', self.endpoint, *argv[1:]]
            environment.pop('DOCKER_CONTEXT', None)
            environment.pop('DOCKER_HOST', None)
        token = self.pairing['token']
        captured, captured_size = deque(), 0
        pending = ''
        decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder('utf-8')(errors='replace'), translate=True)
        chunks = queue.Queue(maxsize=64)
        stopped = threading.Event()
        process = reader = None

        with self.log_path.open('a', encoding='utf-8') as log:
            def record(value, display=False):
                log.write(value)
                log.flush()
                if display:
                    print(value, end='', flush=True)

            def consume(data, final=False):
                nonlocal pending, captured_size
                value = (pending + decoder.decode(data, final=final)).replace(token, '[redacted]')
                pending = ''
                # A credential can straddle arbitrary pipe reads. Retain only a
                # possible credential prefix, so ordinary progress appears now.
                if not final:
                    for length in range(min(len(token) - 1, len(value)), 0, -1):
                        if token.startswith(value[-length:]):
                            pending, value = value[-length:], value[:-length]
                            break
                if value:
                    record(value, progress)
                    captured.append(value)
                    captured_size += len(value)
                    while captured and captured_size - len(captured[0]) >= COMMAND_OUTPUT_LIMIT:
                        captured_size -= len(captured.popleft())

            def read_output():
                try:
                    while not stopped.is_set():
                        data = process.stdout.read(4096)
                        while not stopped.is_set():
                            try:
                                chunks.put(data, timeout=0.1)
                                break
                            except queue.Full:
                                continue
                        if not data:
                            return
                except OSError as error:
                    if not stopped.is_set():
                        chunks.put(error)

            def terminate():
                if process is None:
                    return
                # Docker build may spawn buildx. Stop our entire CLI process
                # group, leaving unrelated containers and the daemon alone.
                if os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                elif process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    if os.name != 'nt':
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    elif process.poll() is None:
                        process.kill()
                    process.wait(timeout=3)

            started = last_output = last_heartbeat = time.monotonic()
            record('\n$ ' + repr(actual).replace(token, '[redacted]') + '\n', progress)
            try:
                _check_cancel(self.cancel_requested)
                process = subprocess.Popen(actual, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, bufsize=0, start_new_session=os.name != 'nt')
                reader = threading.Thread(target=read_output, daemon=True)
                reader.start()
                eof = False
                while True:
                    _check_cancel(self.cancel_requested)
                    now = time.monotonic()
                    elapsed, silent = now - started, now - last_output
                    if eof and process.poll() is not None:
                        break
                    if elapsed >= timeout:
                        reason = f'Command exceeded {timeout}s / 命令超过 {timeout} 秒'
                        advice = ('\nCheck Docker Desktop/daemon, registry network/proxy and free disk space; '
                                  'then retry setup. Completed image layers are reused. / '
                                  '请检查 Docker 服务、镜像仓库网络/代理及磁盘空间后重试，已完成的镜像层会复用。'
                                  if argv[0] == 'docker' else '\nCheck the command and retry / 请检查命令后重试。')
                        raise RuntimeError(reason + advice + '\nLog / 日志: ' + str(self.log_path))
                    if progress and now - last_heartbeat >= COMMAND_HEARTBEAT_SECONDS:
                        record(f'\n[setup] Elapsed / 已用 {elapsed:.0f}s; last output / 距上次输出 {silent:.0f}s. '
                               f'Log / 日志: {self.log_path}\n', True)
                        last_heartbeat = now
                    wait = min(0.2, max(0.001, timeout - elapsed))
                    try:
                        data = chunks.get(timeout=wait)
                    except queue.Empty:
                        continue
                    if isinstance(data, OSError):
                        raise data
                    if not data:
                        eof = True
                        continue
                    last_output = time.monotonic()
                    consume(data)
            except BaseException as error:
                terminate()
                # Retain any partial output already read when a timeout or
                # cancellation interrupts the command.
                while not chunks.empty():
                    data = chunks.get_nowait()
                    if isinstance(data, bytes) and data:
                        consume(data)
                record('\n[setup] ' + (str(error) or type(error).__name__).replace(token, '[redacted]') + '\n')
                raise
            finally:
                stopped.set()
                if reader is not None:
                    reader.join(timeout=1)
                if process is not None:
                    process.stdout.close()
                consume(b'', final=True)
            record(f'\n[setup] Exit {process.returncode}; elapsed {time.monotonic() - started:.1f}s\n')
        output = ''.join(captured)[-COMMAND_OUTPUT_LIMIT:]
        if check and process.returncode:
            raise RuntimeError(output[-2000:] + '\nLog / 日志: ' + str(self.log_path))
        return process.returncode, output

    def prerequisites(self):
        print('[1/5] Checking Docker, GPU, disk and controller / 检查环境和连接', flush=True)
        if sys.platform != 'linux' or sys.version_info < (3, 10):
            raise RuntimeError('Worker requires Linux/WSL2 and Python 3.10+; use the Windows worker launcher on Windows.')
        for program in ('git', 'docker', 'nvidia-smi'):
            if not shutil.which(program):
                raise RuntimeError(f'Missing {program}. Check prerequisites in README / 缺少系统依赖 {program}。')
        context = os.environ.get('DOCKER_CONTEXT')
        if os.environ.get('DOCKER_HOST') and not context:
            endpoint = os.environ['DOCKER_HOST']
        else:
            argv = ['docker', 'context', 'inspect'] + ([context] if context else [])
            _, endpoint = self.command(argv + ['--format', '{{.Endpoints.docker.Host}}'], timeout=10)
        endpoint = endpoint.strip()
        if not endpoint.startswith('unix:///') or any(c.isspace() for c in endpoint):
            raise ValueError('Use this machine\'s local Docker socket; remote Docker contexts are not supported.')
        self.endpoint = endpoint
        self.state['docker_endpoint'] = endpoint
        self.command(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=20)
        self.command(['git', '--version'], timeout=10)
        _, data = self.command(['nvidia-smi', '--query-gpu=uuid,name,memory.total,memory.free',
                                '--format=csv,noheader,nounits'], timeout=20)
        self.gpus = []
        for row in csv.reader(io.StringIO(data.strip())):
            if len(row) != 4:
                raise ValueError('GPU inventory is incomplete')
            identity, name, total, free = [v.strip() for v in row]
            if not GPU_UUID.fullmatch(identity) or not total.isdigit() or not free.isdigit() or not 0 <= int(free) <= int(total):
                raise ValueError('Invalid GPU inventory')
            self.gpus.append({'uuid': identity, 'name': name, 'total_mb': int(total), 'free_mb': int(free)})
        if not self.gpus:
            raise ValueError('No NVIDIA GPUs found')
        if shutil.disk_usage(self.root).free < 8 * 1024**3:
            raise ValueError('At least 8 GiB free disk space is needed to prepare the runtime')
        authenticate(self.pairing)

    def sources(self):
        print('[2/5] Preparing versioned application files / 准备程序版本', flush=True)
        package = Path(__file__).resolve().parent
        files = {path.relative_to(package.parent).as_posix(): path.read_bytes()
                 for path in package.rglob('*') if path.is_file()
                 and '__pycache__' not in path.parts and path.suffix in ('.py', '.js', '.html', '.css')}
        files['Dockerfile'] = DOCKERFILE.encode('utf-8')
        digest = hashlib.sha256()
        for name, data in sorted(files.items()):
            digest.update(name.encode() + b'\0' + hashlib.sha256(data).digest())
        self.source_id = digest.hexdigest()
        self.build_root = self.root / 'versions' / self.source_id
        self.build_root.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            path = self.build_root / name
            if path.is_symlink() or path.exists() and path.read_bytes() != data:
                raise ValueError('Installed version was modified; restore it or choose another data directory')
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
        self.repo = self.root / 'gpu-check-source'
        self.repo.mkdir(exist_ok=True)
        source = self.repo / 'README.txt'
        expected = b'Experiment Manager built-in CUDA check. No research dataset.\n'
        if source.exists() and source.read_bytes() != expected:
            raise ValueError('GPU check source was modified')
        if not source.exists():
            source.write_bytes(expected)
        if not (self.repo / '.git').exists():
            self.command(['git', 'init', str(self.repo)])
        if (self.repo / '.git').is_symlink() or not (self.repo / '.git').is_dir():
            raise ValueError('GPU check repository must have its own Git directory')
        code, _ = self.command(['git', '-C', str(self.repo), 'rev-parse', '--verify', 'HEAD'], check=False)
        if code:
            self.command(['git', '-C', str(self.repo), 'add', 'README.txt'])
            self.command(['git', '-C', str(self.repo), '-c', 'user.name=ExperimentManager',
                          '-c', 'user.email=setup@localhost', 'commit', '-m', 'Record built-in check'])
        _, status = self.command(['git', '-C', str(self.repo), 'status', '--porcelain'])
        if status.strip():
            raise ValueError('GPU check repository has uncommitted changes')
        _, commit = self.command(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'])
        self.commit = commit.strip()

    def image(self):
        print('[3/5] Preparing PyTorch runtime / 准备 PyTorch 运行环境；下方显示下载和构建进度', flush=True)
        print(f'  Progress log / 实时日志: {self.log_path}', flush=True)
        print('  Docker may be silent while downloading a layer; elapsed time is shown every 30s. / '
              '镜像层下载期间可能暂时无输出，每 30 秒显示等待时间。', flush=True)
        print('  [3.1] Preparing local image registry / 准备本地镜像仓库', flush=True)
        registry = 'expman-worker-registry'
        code, output = self.command(['docker', 'container', 'inspect', registry], check=False)
        if code:
            self.command(['docker', 'run', '-d', '--name', registry, '--restart', 'unless-stopped',
                          '--label', 'expman.component=worker-registry', '-p', '127.0.0.1:5001:5000',
                          '-v', 'expman-worker-registry-data:/var/lib/registry',
                          '-e', 'OTEL_TRACES_EXPORTER=none', REGISTRY_IMAGE],
                         progress=True)
        else:
            item = json.loads(output)[0]
            if (item['Config'].get('Labels', {}).get('expman.component') != 'worker-registry'
                    or item['HostConfig'].get('PortBindings', {}).get('5000/tcp') != [{'HostIp': '127.0.0.1', 'HostPort': '5001'}]):
                raise ValueError('An unrelated container uses the worker registry name; it was left unchanged')
            if not item['State']['Running']:
                self.command(['docker', 'start', registry])
        cache = self.state.get('image', {})
        if cache.get('source_id') == self.source_id and cache.get('base') == BASE_IMAGE:
            print('  Reusing prepared runtime / 复用已准备好的运行环境', flush=True)
            self.command(['docker', 'pull', cache['reference']], progress=True)
            return cache['reference']
        tag = 'localhost:5001/expman-runtime:' + self.source_id[:20]
        # Docker Desktop's pull path can use its configured proxy even when
        # BuildKit's direct metadata lookup cannot reach the registry.
        print('  [3.2] Downloading PyTorch base image / 下载 PyTorch 基础镜像', flush=True)
        self.command(['docker', 'pull', BASE_IMAGE], timeout=7200, progress=True)
        print('  [3.3] Building worker runtime / 构建算力端运行环境', flush=True)
        self.command(['docker', 'build', '--progress=plain', '--build-arg', 'BASE_IMAGE=' + BASE_IMAGE, '-t', tag, '.'],
                     cwd=self.build_root, timeout=7200, progress=True)
        print('  [3.4] Publishing to local registry / 将运行环境写入本机镜像仓库', flush=True)
        _, output = self.command(['docker', 'push', tag], timeout=7200, progress=True)
        digests = set(re.findall(r'\bdigest: (sha256:[0-9a-f]{64})\b', output))
        if len(digests) != 1:
            raise ValueError('Registry did not return a unique digest')
        reference = 'localhost:5001/expman-runtime@' + digests.pop()
        self.command(['docker', 'pull', reference], progress=True)
        self.state['image'] = {'source_id': self.source_id, 'base': BASE_IMAGE, 'reference': reference}
        self.save()
        return reference

    def verify(self, image, selected):
        print('[4/5] Testing selected GPUs inside the training container / 在真实容器中验卡', flush=True)
        receipts = []
        for gpu in selected:
            print('  ' + gpu['name'] + ' ' + gpu['uuid'], flush=True)
            _, result = self.command(['docker', 'run', '--rm', '--gpus', 'device=' + gpu['uuid'],
                '-e', 'CUDA_VISIBLE_DEVICES=' + gpu['uuid'], '--user', f'{os.getuid()}:{os.getgid()}',
                '--read-only', '--cap-drop=ALL', '--security-opt', 'no-new-privileges', '--network', 'none',
                '--cpus', '1', '--memory', '2g', '--tmpfs', '/tmp:rw,nosuid,size=512m',
                image, 'python', '-m', 'expman.gpu_check'], timeout=180)
            rows = [json.loads(line) for line in result.splitlines() if line.startswith('{')]
            if len(rows) != 1 or rows[0].get('status') != 'passed' or rows[0].get('gpu_uuid', '').lower().removeprefix('gpu-') != gpu['uuid'].lower().removeprefix('gpu-'):
                raise ValueError('GPU verification did not return the expected receipt')
            receipts.append(rows[0])
        private_write(self.root / 'gpu-verification.json', {'image': image, 'gpus': receipts})

    def configure(self, image, selected):
        from .agent import _free_ram_mb
        from .__main__ import node_config
        print('[5/5] Saving configuration and task template / 保存配置与验收任务', flush=True)
        config = node_config(self.pairing['node_id'], self.pairing['hub_url'], self.pairing['token'], self.root / 'runtime')
        profile = 'pytorch-' + self.pairing['node_id']
        available_ram = _free_ram_mb()
        if available_ram is None or available_ram < 2048:
            raise ValueError('At least 2 GiB available system RAM is required')
        config.update(allowed_repos=[str(self.repo)], tags=[self.pairing['node_id']],
            profiles={profile: {'image': image, 'verified': True, 'gpu_name_patterns': sorted({g['name'] for g in selected})}},
            gpu_policy={g['uuid']: {'max_jobs': 1 if g in selected else 0, 'reserve_mb': 1024} for g in self.gpus})
        config['policy'].update(max_running=1, max_prefetch=2, cpu_budget=min(4, os.cpu_count() or 1),
                                ram_budget_mb=min(8192, int(available_ram * 0.75)))
        task = validate_task({'name': 'GPU-check-' + self.pairing['node_id'], 'algorithm': 'CUDA-check',
            'group': 'installation-check', 'backend': 'docker', 'metric_protocol': 'cuda-check-v1',
            'source': {'repo': str(self.repo), 'commit': self.commit}, 'tags': config['tags'],
            'command': ['python', '-m', 'expman.gpu_check'], 'environments': [{'profile': profile, 'image': image}],
            'params': {}, 'resources': {'gpu_memory_mb': 1024, 'ram_mb': 1536, 'cpu': 1, 'exclusive': True}})
        config['task_templates'] = [task]
        private_write(self.root / 'gpu-check.task.json', task)
        # Preserve user-added projects/assets and their profiles during a recheck.
        previous = read_json(self.root / 'node.ready.json', {})
        if previous:
            if previous.get('node_id') != config['node_id'] or previous.get('token') != config['token']:
                raise ValueError('Existing configuration has a different enrollment')
            for key in ('assets', 'profiles'):
                config[key] = {**previous.get(key, {}), **config[key]}
            config['allowed_repos'] = list(dict.fromkeys(previous.get('allowed_repos', []) + config['allowed_repos']))
            config['task_templates'] += [t for t in previous.get('task_templates', []) if t.get('algorithm') != 'CUDA-check']
        private_write(self.root / 'node.ready.json', config)
        self.state['configured_version'] = __version__
        self.save()
        return self.root / 'node.ready.json'


def load_pairing(root, supplied):
    saved = read_json(root / 'pairing.json')
    if not supplied and saved:
        return validate_pairing(saved)
    if not supplied:
        candidates = list(Path.cwd().glob('*.pairing.json'))
        if len(candidates) == 1:
            supplied = str(candidates[0])
        else:
            print('Export a pairing file from Controller > Add worker. / 在主控网页点击“添加算力机”导出配对文件。')
            supplied = input('Pairing file path / 配对文件路径: ').strip().strip('"').strip("'")
    path = Path(supplied).expanduser()
    if path.stat().st_size > 16384:
        raise ValueError('Pairing file is too large')
    pairing = validate_pairing(read_json(path))
    if saved and validate_pairing(saved) != pairing:
        raise ValueError('This installation is already paired. Use a separate --root for another worker.')
    return pairing


def start(args, cancel_requested=None):
    _check_cancel(cancel_requested)
    if sys.platform != 'linux':
        raise RuntimeError('Use Start-Worker.cmd to run the worker in WSL2')
    root = Path(args.root or Path.home() / '.local/share/experiment-manager/worker').expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if os.geteuid() == 0:
        raise RuntimeError('Run as your normal Linux user, not root / 请使用普通 Linux 用户运行。')
    with InstanceLock(root / 'setup.lock'):
        pairing = load_pairing(root, args.pairing)
        private_connection(pairing)
        config_path = root / 'node.ready.json'
        current = read_json(config_path)
        if current and (current.get('node_id') != pairing['node_id'] or current.get('token') != pairing['token']):
            raise ValueError('Configuration and pairing do not match')
        # Updating the worker software does not invalidate its enrolled GPU
        # runtime or authorize replacing the user's GPU/resource preferences.
        # Project bundles carry their own harness; dependency preparation checks
        # the selected image when needed. Rebuild/reselect only on explicit setup.
        if not current or args.configure or args.gpu:
            # The agent lock prevents changes while an existing worker owns this runtime.
            with InstanceLock(root / 'runtime' / 'agent.lock'):
                setup = WorkerSetup(root, pairing, cancel_requested=cancel_requested)
                setup.prerequisites()
                _check_cancel(cancel_requested)
                selected = [g for g in setup.gpus if not args.gpu or g['uuid'] in args.gpu]
                if not selected or args.gpu and set(args.gpu) != {g['uuid'] for g in selected}:
                    raise ValueError('Requested GPU UUID was not found')
                setup.sources()
                _check_cancel(cancel_requested)
                image = setup.image()
                _check_cancel(cancel_requested)
                setup.verify(image, selected)
                _check_cancel(cancel_requested)
                config_path = setup.configure(image, selected)
                private_write(root / 'pairing.json', pairing)
        _check_cancel(cancel_requested)
        print('Worker ready / 算力端已就绪: ' + pairing['node_id'], flush=True)
        print('Data / 数据: ' + str(root), flush=True)
        print('Controller: choose this node\'s GPU check template, then submit once. / 主控网页中可填入本节点验收任务。', flush=True)
    if not args.prepare_only:
        _check_cancel(cancel_requested)
        # Pin the validated local Docker endpoint for runtime bind mounts too.
        endpoint = read_json(root / 'setup-state.json', {}).get('docker_endpoint')
        if endpoint:
            os.environ.pop('DOCKER_CONTEXT', None)
            os.environ['DOCKER_HOST'] = endpoint
        from .agent import run
        run(str(config_path))
