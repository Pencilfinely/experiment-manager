"""Local, explicit GPU enrollment from the worker's desktop settings."""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from . import common
from .launcher import InstanceLock
from .worker_setup import GPU_UUID, private_write


CUDA_PROBE = '''
import json, os, torch
expected = os.environ['CUDA_VISIBLE_DEVICES'].lower().removeprefix('gpu-')
if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
    raise RuntimeError('Expected exactly one accessible CUDA GPU')
p = torch.cuda.get_device_properties(0)
if str(p.uuid).lower().removeprefix('gpu-') != expected:
    raise RuntimeError('CUDA selected a different GPU UUID')
x = torch.ones((32, 32), device='cuda')
if (x @ x).sum().item() != 32768.0:
    raise RuntimeError('CUDA calculation failed')
torch.cuda.synchronize()
print(json.dumps({'status': 'passed', 'gpu_uuid': str(p.uuid), 'gpu_name': p.name,
                  'torch': torch.__version__, 'cuda': torch.version.cuda}))
'''


def _configuration(root):
    from . import worker_service as service
    path = service._settings(root).get('config')
    candidate = service._candidate(path) if path else None
    if candidate is None:
        raise ValueError('请先启动算力端，完成已有配置的选择或首次环境准备。')
    path = Path(candidate['path'])
    return path, candidate, common.read_json(path)


def _environment(path):
    environment = os.environ.copy()
    endpoint = common.read_json(path.parent / 'setup-state.json', {}).get('docker_endpoint')
    if endpoint:
        if not isinstance(endpoint, str) or not endpoint.startswith('unix:///') or any(c.isspace() for c in endpoint):
            raise ValueError('显卡检查必须使用本机 Docker。请检查算力端的 Docker 设置。')
        environment.pop('DOCKER_CONTEXT', None)
        environment['DOCKER_HOST'] = endpoint
    return environment


def _profiles(config, name):
    return {key: item['image'] for key, item in config.get('profiles', {}).items()
            if isinstance(item, dict) and item.get('verified') is True
            and isinstance(item.get('image'), str)
            and any(isinstance(pattern, str) and pattern and pattern.casefold() in name.casefold()
                    for pattern in item.get('gpu_name_patterns', []))}


def _inventory(config):
    result = subprocess.run(['nvidia-smi', '--query-gpu=uuid,name,memory.total,memory.free',
                             '--format=csv,noheader,nounits'],
                            capture_output=True, text=True, timeout=10, check=True)
    gpus = []
    seen = set()
    for row in csv.reader(io.StringIO(result.stdout)):
        if len(row) != 4:
            raise ValueError('无法读取完整的显卡信息，请检查 NVIDIA 驱动。')
        identity, name, total, free = (value.strip() for value in row)
        if not GPU_UUID.fullmatch(identity) or identity in seen:
            raise ValueError('显卡返回了无效或重复的 UUID。')
        seen.add(identity)
        total, free = int(total), int(free)
        if not 0 <= free <= total or total <= 0:
            raise ValueError('无法读取显卡的显存信息。')
        policy = config.get('gpu_policy', {}).get(identity, {})
        jobs = policy.get('max_jobs', 1) if identity in config.get('gpu_policy', {}) else 0
        profiles = _profiles(config, name)
        enabled = isinstance(jobs, int) and not isinstance(jobs, bool) and jobs > 0 and bool(profiles)
        gpus.append(dict(uuid=identity, name=name, total_mb=total, free_mb=free,
                         local_enabled=enabled, can_enable=bool(profiles), max_jobs=jobs,
                         reason='已启用' if enabled else ('本地未启用' if profiles else '没有匹配的已验证运行环境')))
    return gpus


def gpu_status(service_root=None):
    from . import worker_service as service
    service._require_linux()
    root = service._root(service_root)
    _, _, config = _configuration(root)
    return {**service.status(root), 'gpus': _inventory(config)}


def _verify(image, identity, environment, deadline):
    if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('运行环境未固定镜像摘要，请重新准备算力环境。')
    name = 'expman-gpu-check-' + uuid.uuid4().hex
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError('显卡检查超时，原配置保持不变。请稍后重试。')
    try:
        result = subprocess.run(['docker', 'run', '--rm', '--pull=never', '--name', name,
            '--label', 'expman.component=gpu-settings-check', '--gpus', 'device=' + identity,
            '-e', 'CUDA_VISIBLE_DEVICES=' + identity, '--user', f'{os.getuid()}:{os.getgid()}',
            '--read-only', '--cap-drop=ALL', '--security-opt', 'no-new-privileges',
            '--network', 'none', '--cpus', '1', '--memory', '2g',
            '--tmpfs', '/tmp:rw,nosuid,size=512m', image, 'python', '-c', CUDA_PROBE],
            env=environment, capture_output=True, text=True, timeout=min(60, remaining))
        if result.returncode:
            raise RuntimeError('显卡 CUDA 验证失败，原配置保持不变：' + (result.stderr or result.stdout)[-1500:])
        receipts = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
        if (len(receipts) != 1 or receipts[0].get('status') != 'passed'
                or receipts[0].get('gpu_uuid', '').lower().removeprefix('gpu-') != identity.lower().removeprefix('gpu-')):
            raise ValueError('显卡检查没有返回匹配该 UUID 的成功记录，原配置保持不变。')
        return dict(receipts[0], image=image)
    finally:
        # A timed-out docker client may leave its container running. Only remove
        # this invocation's random, explicitly labelled verification container.
        try:
            inspected = subprocess.run(['docker', 'inspect', name, '--format',
                '{{index .Config.Labels "expman.component"}}'],
                env=environment, capture_output=True, text=True, timeout=5)
            if inspected.returncode == 0 and inspected.stdout.strip() == 'gpu-settings-check':
                cleaned = subprocess.run(['docker', 'rm', '-f', name], env=environment,
                                         capture_output=True, text=True, timeout=5)
                if cleaned.returncode:
                    raise RuntimeError('显卡检查容器未能清理，配置保持不变。请检查 Docker 后重试。')
        except subprocess.TimeoutExpired as error:
            raise RuntimeError('无法确认显卡检查容器已退出，配置保持不变。请检查 Docker 后重试。') from error


def _enable_stopped(root, identity, expected_path):
    from . import worker_service as service
    with InstanceLock(root / 'control.lock'), InstanceLock(root / 'supervisor.lock'):
        path, candidate, config = _configuration(root)
        if path != expected_path:
            raise ValueError('所选节点配置已改变，请重新打开显卡设置。')
        ready = service.update_status(root)
        if ready['running'] or not ready['ready_for_update']:
            raise RuntimeError(ready['detail'])
        with InstanceLock(Path(candidate['root']) / 'agent.lock'):
            original = path.read_bytes()
            if json.loads(original) != config:
                raise ValueError('节点配置已改变，请刷新后重试。')
            gpu = next((g for g in _inventory(config) if g['uuid'] == identity), None)
            if gpu is None:
                raise ValueError('该显卡已不在本机，请刷新显卡列表。')
            if gpu['local_enabled']:
                return dict(status='succeeded', gpu_uuid=identity, configuration_saved=False,
                            detail='这张显卡已在本地启用，可在实验台调整并发和显存预留。')
            profiles = _profiles(config, gpu['name'])
            if not profiles:
                raise ValueError('没有匹配的已验证运行环境，请先完成算力端的环境准备。')
            environment = _environment(path)
            deadline = time.monotonic() + 90
            receipts = [_verify(image, identity, environment, deadline) for image in sorted(set(profiles.values()))]
            if path.read_bytes() != original:
                raise ValueError('验证期间配置被其他程序修改，请刷新后重试。')
            backup = path.with_name(path.name + '.before-gpu-' + uuid.uuid4().hex + '.json')
            private_write(backup, json.loads(original))
            private_write(root / 'gpu-verification.json', dict(gpu_uuid=identity, checked_at=time.time(), receipts=receipts))
            settings = config.setdefault('gpu_policy', {}).setdefault(identity, {'reserve_mb': 2048})
            jobs = settings.get('max_jobs', 0)
            settings['max_jobs'] = max(1, jobs) if isinstance(jobs, int) and not isinstance(jobs, bool) else 1
            private_write(path, config)
            return dict(status='succeeded', gpu_uuid=identity, configuration_saved=True,
                        detail='显卡检查通过，已启用。每卡并发、节点并发和资源预算可在实验台调整。')


def gpu_enable(service_root=None, *, gpu):
    """Drain only an idle worker, verify, save, then restore its prior lifecycle."""
    from . import worker_service as service
    service._require_linux()
    if not isinstance(gpu, str) or not GPU_UUID.fullmatch(gpu):
        raise ValueError('请选择有效的显卡 UUID。')
    root = service._root(service_root)
    path, _, _ = _configuration(root)
    before = service.update_status(root)
    if not before['ready_for_update']:
        raise RuntimeError('当前无法检查显卡：' + before['detail'].replace('安装更新', '启用显卡'))
    was_running = before['running']
    result = None
    try:
        if was_running:
            stopped = service.stop_for_update(root)
            if not stopped['ready_for_update']:
                raise RuntimeError(stopped['detail'])
            deadline = time.monotonic() + 15
            while True:
                state = service.status(root)
                if not state['running'] and state['status'] == 'stopped':
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('代理仍在退出，请稍后重试显卡检查。')
                time.sleep(0.1)
        result = _enable_stopped(root, gpu, path)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        result = dict(status='failed', configuration_saved=False, detail=service._redact(root, str(error)))
    finally:
        if was_running:
            try:
                state = service.status(root)
                if not state['running'] and state['status'] not in ('starting', 'preparing', 'stopping', 'shutting_down'):
                    if _configuration(root)[0] != path:
                        raise RuntimeError('节点配置已改变，请手动启动所需节点。')
                    restarted = service.start(root)
                    if restarted.get('status') not in ('starting', 'preparing', 'online', 'offline') and not restarted.get('running'):
                        raise RuntimeError(restarted.get('detail', '代理未能启动'))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result = result or dict(status='failed', configuration_saved=False, detail='显卡设置未完成。')
                result['detail'] += ' 代理未能自动恢复，请点击“启动后台代理”：' + service._redact(root, str(error))
    state = service.status(root)
    if result['status'] == 'succeeded' and not was_running and not state['running']:
        result['detail'] += ' 代理当前停止，请点击“启动后台代理”使设置生效。'
    return {**state, **result}
