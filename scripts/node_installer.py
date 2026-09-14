"""Create a node-specific installer; install SASRec automatically on Linux/WSL.

Python, Git, working GPU Docker and a reachable Hub are prerequisites. The
Windows launcher checks WSL and guides missing system components. The install
pipeline is repeatable and validates each selected GPU before enabling it.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile
from urllib.parse import urlsplit

BASE_IMAGE = 'pytorch/pytorch@sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2'
UUID = re.compile(r'GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def exact_file(path, data):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('拒绝符号链接：' + str(path))
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ValueError('已有文件内容不同，保留原文件；请使用新的安装目录：' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)


def safe_path(root, name):
    parts = name.replace('\\', '/').split('/')
    if any(not part or part in ('.', '..') or ':' in part for part in parts):
        raise ValueError('安装包路径不合法')
    path = root.joinpath(*parts)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('安装目录含符号链接：' + str(parent))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('安装包路径越界')
    return path


def validate_source(data):
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        names = source.namelist()
        if len(names) != len(set(names)) or 'source-manifest.json' not in names:
            raise ValueError('源码包缺少清单或含重复路径')
        if (any(item.file_size > 32 * 1024 * 1024 for item in source.infolist()) or
                sum(item.file_size for item in source.infolist()) > 128 * 1024 * 1024):
            raise ValueError('源码包文件过大')
        manifest = json.loads(source.read('source-manifest.json'))
        if set(names) != set(manifest['files']) | {'source-manifest.json'}:
            raise ValueError('源码包清单不匹配')
        entries = {}
        for name, expected in manifest['files'].items():
            if not name.startswith(('experiment_manager/', 'sasrec-source/')):
                raise ValueError('源码包含非预期目录')
            safe_path(Path.cwd(), name)
            value = source.read(name)
            if expected != {'sha256': sha(value), 'bytes': len(value)}:
                raise ValueError('源码包摘要错误：' + name)
            entries[name] = value
    required = {'sasrec-source/SASRec_Original/src/experiment.py',
                'experiment_manager/expman/agent.py', 'experiment_manager/expman/__main__.py',
                'experiment_manager/deploy/Dockerfile.sasrec'}
    if not required.issubset(entries):
        raise ValueError('源码包不完整')
    return entries


def build(source_path, config_path, output):
    source = Path(source_path).read_bytes()
    validate_source(source)
    config = config_path if isinstance(config_path, dict) else json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', config.get('node_id', '')):
        raise ValueError('节点 ID 不合法')
    for key in ('hub_url', 'token'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError('缺少节点配置字段：' + key)
    url = urlsplit(config['hub_url'])
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('中心地址须为不含账号密码的 HTTP(S) 地址')
    url.port
    # Only enrollment information travels; deployment paths are discovered on site.
    config = {key: config[key] for key in ('node_id', 'hub_url', 'token')}
    node = io.BytesIO()
    with zipfile.ZipFile(node, 'w', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('__main__.py', Path(__file__).read_bytes())
        package.writestr('source.zip', source)
        package.writestr('node.json', json_bytes(config))
        package.writestr('installer.json', json_bytes({'schema': 1, 'source_sha256': sha(source),
                                                      'base_image': BASE_IMAGE}))
    root = Path(__file__).resolve().parents[1]
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as delivery:
        delivery.writestr('ExperimentNode.pyz', node.getvalue())
        for name in ('Start-Windows.cmd', 'Install-Windows.ps1', 'Start-Linux.sh'):
            delivery.writestr(name, (root / 'deploy' / name).read_bytes())
        delivery.writestr('README.txt', (
            '此包只用于节点 ' + config['node_id'] + '，包含其连接凭证。\n'
            'Windows：解压后双击 Start-Windows.cmd。\n'
            'Linux：在解压目录运行 bash Start-Linux.sh。\n'
            '程序会先检查系统前提，再准备源码、固定镜像、逐卡验收、生成配置并启动代理。\n'
            '已完成步骤会校验后复用。缺少 WSL/Docker/驱动时会给出操作入口。\n'
            '当前版本不安装系统服务，不自动重启系统，不提供多卡训练。\n'
        ).encode('utf-8'))
    return Path(output)


def build_from_center(source_path, center, node_id, hub_url, output):
    """One command on the center creates an enrolled, node-specific ZIP."""
    if not node_id or not hub_url:
        raise ValueError('--center 需要同时提供 --node-id 和 --hub-url')
    if Path(output).exists():
        raise ValueError('输出包已存在，请保留并使用新文件名')
    validate_source(Path(source_path).read_bytes())
    center = Path(center).absolute()
    if not (center / 'hub.json').is_file() or not (center / 'hub.sqlite3').is_file():
        raise ValueError('找不到已有中心；安装包生成器不会初始化新中心')
    url = urlsplit(hub_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('中心地址须为不含账号密码的 HTTP(S) 地址')
    url.port
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from expman.hub import Hub
    hub = Hub(center)
    try:
        token = hub.add_node(node_id)
        return build(source_path, {'node_id': node_id, 'hub_url': hub_url, 'token': token}, output)
    finally:
        hub.close()


class Installer:
    def __init__(self, package, root=None):
        with zipfile.ZipFile(package) as archive:
            if set(archive.namelist()) != {'__main__.py', 'source.zip', 'node.json', 'installer.json'}:
                raise ValueError('安装包内容不符')
            self.meta = json.loads(archive.read('installer.json'))
            self.enrollment = json.loads(archive.read('node.json'))
            source = archive.read('source.zip')
        if self.meta.get('schema') != 1 or sha(source) != self.meta['source_sha256']:
            raise ValueError('安装包校验失败')
        self.entries = validate_source(source)
        self.root = Path(root or Path.home() / 'ExperimentNodeSetup' / self.meta['source_sha256'][:16]).expanduser().absolute()
        if ',' in str(self.root):
            raise ValueError('安装目录不能含逗号，因为 Docker 挂载参数使用逗号分隔字段')
        safe_path(self.root, 'install-state.json')
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / 'install-state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        if self.state and (self.state.get('source_sha256') != self.meta['source_sha256'] or
                           self.state.get('node_id') != self.enrollment['node_id']):
            raise ValueError('该目录属于另一份源码或节点，请使用新的安装目录')
        self.state.update(source_sha256=self.meta['source_sha256'], node_id=self.enrollment['node_id'])
        self.log = self.root / 'install.log'
        safe_path(self.root, 'install.log')

    def save(self):
        temporary = self.state_path.with_suffix('.tmp')
        safe_path(self.root, temporary.name)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(json_bytes(self.state))
        os.replace(temporary, self.state_path)

    def command(self, argv, *, input=None, timeout=1800, check=True, cwd=None):
        result = subprocess.run(argv, input=input, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding='utf-8', errors='replace', timeout=timeout,
                                cwd=cwd, shell=False)
        output = result.stdout.replace(self.enrollment['token'], '[redacted]')
        descriptor = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8') as log:
            log.write('\n$ ' + shlex.join(argv) + '\n' + output)
        if check and result.returncode:
            print(output[-2500:], flush=True)
            raise ValueError('步骤失败，完整日志在 ' + str(self.log))
        return result.returncode, output

    def prerequisites(self):
        print('[1/6] 检查 Python、Git、Docker、显卡和中心连接', flush=True)
        if sys.platform != 'linux' or sys.version_info < (3, 10):
            raise ValueError('请在 Ubuntu/WSL2 或原生 Linux 中使用 Python 3.10 以上版本运行。')
        self.command(['git', '--version'], timeout=10)
        self.command(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=15)
        _, inventory = self.command(['nvidia-smi', '--query-gpu=uuid,name,memory.total,memory.free',
                                     '--format=csv,noheader,nounits'], timeout=15)
        import csv
        self.gpus = []
        for row in csv.reader(io.StringIO(inventory)):
            if len(row) != 4:
                raise ValueError('GPU 信息格式不符')
            gpu, name, total, free = [value.strip() for value in row]
            if not UUID.fullmatch(gpu) or not total.isdigit() or not free.isdigit():
                raise ValueError('显卡 UUID 或显存遥测无效')
            self.gpus.append({'uuid': gpu, 'name': name, 'total_mb': int(total), 'free_mb': int(free)})
        if not self.gpus:
            raise ValueError('未检测到可用 NVIDIA GPU')
        url = self.enrollment['hub_url'].rstrip('/')
        with urllib.request.urlopen(url, timeout=10) as response:
            if response.status != 200 or 'Experiment Manager' not in response.read().decode('utf-8'):
                raise ValueError('中心地址未返回实验台页面')

    def sources(self):
        print('[2/6] 校验源码并记录 Git 版本', flush=True)
        for name, data in self.entries.items():
            path = safe_path(self.root, name)
            if path.exists() and path.read_bytes() != data:
                raise ValueError('已有安装源码被修改，请保留并选择新目录：' + name)
        for name, data in self.entries.items():
            exact_file(safe_path(self.root, name), data)
        self.repo = self.root / 'sasrec-source'
        self.software = self.root / 'experiment_manager'
        git_path = safe_path(self.repo, '.git')
        if git_path.exists() and not git_path.is_dir():
            raise ValueError('安装目录已属于外部 Git worktree，请选择新目录')
        if not (self.repo / '.git').exists():
            self.command(['git', 'init', str(self.repo)])
        code, _ = self.command(['git', '-C', str(self.repo), 'rev-parse', '--verify', 'HEAD'], check=False)
        if code:
            self.command(['git', '-C', str(self.repo), 'add', 'SASRec_Original/src'])
            self.command(['git', '-C', str(self.repo), '-c', 'user.name=ExperimentInstaller',
                          '-c', 'user.email=installer@localhost', 'commit', '-m', 'Record deployment snapshot'])
        _, status = self.command(['git', '-C', str(self.repo), 'status', '--porcelain', '--', 'SASRec_Original/src'])
        if status.strip():
            raise ValueError('安装源码与记录的 Git 版本不同')
        _, commit = self.command(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'])
        self.commit = commit.strip()
        if not re.fullmatch(r'[0-9a-f]{40}', self.commit):
            raise ValueError('无法记录完整源码版本')

    def image(self):
        print('[3/6] 准备固定训练镜像；首次下载可能需要较长时间', flush=True)
        code, existing = self.command(['docker', 'container', 'inspect', 'expman-registry'], check=False)
        if code:
            self.command(['docker', 'run', '-d', '--name', 'expman-registry', '--restart', 'unless-stopped',
                          '-p', '127.0.0.1:5000:5000', '-e', 'OTEL_TRACES_EXPORTER=none',
                          '-v', 'expman-registry-data:/var/lib/registry', 'registry:3'])
        else:
            item = json.loads(existing)[0]
            bindings = item['HostConfig'].get('PortBindings', {}).get('5000/tcp', [])
            if bindings != [{'HostIp': '127.0.0.1', 'HostPort': '5000'}] or item['Config']['Image'] != 'registry:3':
                raise ValueError('已有 expman-registry 用途或端口不同，请先检查；安装器不会替换它')
            if not item['State']['Running']:
                self.command(['docker', 'start', 'expman-registry'])
        if self.state.get('image'):
            self.command(['docker', 'pull', self.state['image']])
            return self.state['image']
        tag = 'expman-sasrec:' + self.meta['source_sha256'][:16]
        self.command(['docker', 'build', '--build-arg', 'BASE_IMAGE=' + self.meta['base_image'],
                      '-f', 'deploy/Dockerfile.sasrec', '-t', tag, '.'], cwd=self.software, timeout=3600)
        repository = 'localhost:5000/expman-sasrec'
        remote_tag = repository + ':' + self.meta['source_sha256'][:16]
        self.command(['docker', 'tag', tag, remote_tag])
        _, pushed = self.command(['docker', 'push', remote_tag])
        digests = re.findall(r'\bdigest: (sha256:[0-9a-f]{64})\b', pushed)
        if len(set(digests)) != 1:
            raise ValueError('推送未返回唯一镜像摘要，请查看日志')
        image = repository + '@' + digests[0]
        self.command(['docker', 'pull', image])
        self.state['image'] = image
        self.save()
        return image

    def verify_gpus(self, image, selected):
        print('[4/6] 在实际训练容器里逐卡运行 CUDA 和 SASRec 恢复验收', flush=True)
        probes = self.root / 'probes'
        probes.mkdir(exist_ok=True)
        receipts = self.state.setdefault('gpu_checks', {})
        for gpu in selected:
            receipt = receipts.get(gpu['uuid'], {})
            report = safe_path(self.root, receipt.get('report', 'missing-report'))
            if (receipt.get('image') == image and receipt.get('commit') == self.commit and report.is_file()
                    and sha(report.read_bytes()) == receipt.get('sha256')):
                print('  已通过，复用记录：' + gpu['name'] + ' ' + gpu['uuid'], flush=True)
                continue
            run_id = uuid.uuid4().hex
            script = '''import os, sys, torch
expected = os.environ['CUDA_VISIBLE_DEVICES'].lower().removeprefix('gpu-')
available, count = torch.cuda.is_available(), torch.cuda.device_count()
print('CUDA', available, count, flush=True)
if not available: torch.cuda.init()
assert available and count == 1, 'Expected one accessible CUDA GPU'
p = torch.cuda.get_device_properties(0)
print(p.name, p.uuid, torch.cuda.get_device_capability(0), flush=True)
assert str(p.uuid).lower().removeprefix('gpu-') == expected, 'GPU UUID mismatch'
x = torch.ones((32,32),device='cuda')
assert (x@x).sum().item() == 32768.0
os.execv(sys.executable, [sys.executable,'-m','expman','sasrec-smoke','--project',
'/workspace/code/SASRec_Original','--root','/workspace/run/RUN_ID','--device','cuda'])
'''.replace('RUN_ID', run_id)
            print('  验收：' + gpu['name'] + ' ' + gpu['uuid'], flush=True)
            self.command(['docker', 'run', '--rm', '-i', '--gpus', 'device=' + gpu['uuid'],
                          '-e', 'CUDA_VISIBLE_DEVICES=' + gpu['uuid'], '--user', f'{os.getuid()}:{os.getgid()}',
                          '--cap-drop=ALL', '--security-opt', 'no-new-privileges', '--cpus', '2',
                          '--memory', '4g', '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=512m',
                          '--mount', f'type=bind,src={self.repo},dst=/workspace/code,readonly',
                          '--mount', f'type=bind,src={probes},dst=/workspace/run', image, 'python', '-'],
                         input=script, timeout=900)
            report = probes / run_id / 'smoke-report.json'
            result = json.loads(report.read_text())
            if result.get('status') != 'passed' or result.get('device') != 'cuda':
                raise ValueError('显卡验收未通过：' + gpu['uuid'])
            receipts[gpu['uuid']] = {'image': image, 'commit': self.commit,
                'report': str(report.relative_to(self.root)), 'sha256': sha(report.read_bytes())}
            self.save()

    def configure(self, image, selected):
        print('[5/6] 生成节点配置和第一条队列验收任务', flush=True)
        sys.path.insert(0, str(self.software))
        from expman.common import validate_task
        profile = 'sasrec-cu128-' + self.enrollment['node_id']
        config = dict(self.enrollment, root=str(Path.home() / 'ExperimentNodes' / self.enrollment['node_id']), allow_demo=False,
                      allowed_repos=[str(self.repo)], assets={'sasrec-toy-v1': str(self.root / 'assets')},
                      tags=[self.enrollment['node_id']], poll_seconds=5, stop_grace_seconds=600,
                      profiles={profile: {'image': image, 'gpu_name_patterns': [g['name'] for g in selected], 'verified': True}},
                      gpu_policy={g['uuid']: {'reserve_mb': 2048, 'max_jobs': 1 if g in selected else 0} for g in self.gpus},
                      policy={'run_enabled': True, 'max_running': 1, 'max_prefetch': 2, 'cpu_budget': 4,
                              'ram_budget_mb': 8192, 'min_disk_free_mb': 2048, 'windows': [], 'speed': 1})
        for split, text in {'train': '1 1 2 3 4\n2 2 3 5\n', 'valid': '1 1 2 3 4 5\n2 2 3 5 6\n',
                            'test': '1 1 2 3 4 5 6\n2 2 3 5 6 7\n'}.items():
            exact_file(self.root / 'assets' / 'Toy' / ('Toy.' + split + '.txt'), text.encode())
        task = validate_task({'name': 'SASRec-Toy-first-run', 'algorithm': 'SASRec', 'backend': 'docker',
            'group': 'sasrec-toy-acceptance', 'metric_protocol': 'external_sasrec_original_v1__synthetic_toy_acceptance_only',
            'source': {'repo': str(self.repo), 'commit': self.commit},
            'command': ['python', '-m', 'expman.adapters.sasrec', '--project', '/workspace/code/SASRec_Original'],
            'environments': [{'profile': profile, 'image': image}], 'assets': ['sasrec-toy-v1'],
            'tags': [self.enrollment['node_id']],
            'params': {'dataset': 'Toy', 'data_asset': 'sasrec-toy-v1', 'device': 'cuda', 'gpu_id': 0,
                       'torch_threads': 1, 'seed': 42, 'epochs': 3, 'star_test': -1, 'batch_size': 2,
                       'max_seq_length': 5, 'hidden_size': 8, 'num_hidden_layers': 1, 'num_attention_heads': 1,
                       'hidden_dropout_prob': 0.2, 'attention_probs_dropout_prob': 0.2,
                       'candidate_chunk_size': 4, 'patience': 10},
            'resources': {'gpu_memory_mb': 2048, 'cpu': 2, 'ram_mb': 4096, 'exclusive': True}})
        exact_file(self.root / 'node.ready.json', json_bytes(config))
        exact_file(self.root / 'sasrec-toy.task.json', json_bytes(task))
        return self.root / 'node.ready.json'

    def install(self, gpu_ids, start):
        self.prerequisites()
        selected = [gpu for gpu in self.gpus if not gpu_ids or gpu['uuid'] in gpu_ids]
        if not selected or gpu_ids and set(gpu_ids) != {g['uuid'] for g in selected}:
            raise ValueError('所选 GPU UUID 不存在')
        if any(g['total_mb'] < 4096 for g in selected):
            raise ValueError('显卡显存不足以执行此验收配置')
        self.sources()
        image = self.image()
        self.verify_gpus(image, selected)
        config = self.configure(image, selected)
        print('[6/6] 本地自检并启动接单员', flush=True)
        self.command([sys.executable, '-m', 'expman', 'doctor', '--config', str(config)], cwd=self.software, timeout=60)
        self.state['configured'] = True
        self.save()
        print('节点准备完成：' + str(config), flush=True)
        print('任务模板：' + str(self.root / 'sasrec-toy.task.json'), flush=True)
        if start:
            os.chdir(self.software)
            env = dict(os.environ, PYTHONPATH=str(self.software), PYTHONUNBUFFERED='1')
            os.execve(sys.executable, [sys.executable, '-m', 'expman', 'agent', '--config', str(config)], env)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    if Path(sys.argv[0]).suffix == '.pyz':
        parser.add_argument('--root')
        parser.add_argument('--gpu', action='append', help='Allowed GPU UUID; repeat for multiple cards; default verifies all cards')
        parser.add_argument('--prepare-only', action='store_true', help='Complete setup without starting the agent')
        args = parser.parse_args()
        Installer(sys.argv[0], args.root).install(args.gpu, not args.prepare_only)
    else:
        parser.add_argument('--source-bundle', required=True)
        enrollment = parser.add_mutually_exclusive_group(required=True)
        enrollment.add_argument('--config', help='Existing exported node configuration')
        enrollment.add_argument('--center', help='Existing center directory; enroll and package in one command')
        parser.add_argument('--node-id')
        parser.add_argument('--hub-url')
        parser.add_argument('--output', required=True)
        args = parser.parse_args()
        if args.center:
            output = build_from_center(args.source_bundle, args.center, args.node_id, args.hub_url, args.output)
        else:
            output = build(args.source_bundle, args.config, args.output)
        print('Created:', output)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        print('部署暂停：' + str(error), file=sys.stderr)
        print('已完成的记录会保留。处理上述问题后可重新运行安装器。', file=sys.stderr)
        raise SystemExit(1)
