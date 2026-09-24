"""User-owned background worker, with optional systemd user-session integration.

The service directory stores only lifecycle settings and logs. The existing node
configuration and its experiment directory remain the source of truth.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from . import __version__, common
from .launcher import InstanceLock
from .pairing import validate_pairing
from .update_protocol import UPDATE_STOP_PROTOCOL, manual_stop_required, supports_update_stop
from .worker_setup import private_connection, private_write
from .worker_upgrade import _candidate, agent_is_running, discover_configs

SHUTDOWN_PROTOCOL = 1


def default_service_root():
    return Path.home() / '.local/share/experiment-manager/client'


def _root(value=None):
    result = Path(value or default_service_root()).expanduser().resolve()
    if '\n' in str(result) or '\r' in str(result):
        raise ValueError('Service path cannot contain a newline')
    return result


def _settings(root):
    return common.read_json(root / 'service.json', {})


def _release_role():
    return common.read_json(Path(__file__).resolve().parents[1] / 'release-role.json', {})


def _check_release_role():
    release = _release_role()
    if release and (not isinstance(release, dict) or release.get('role') not in
                    ('windows-worker-x64', 'ubuntu-worker-x64')):
        raise ValueError('This package contains the controller edition. Download the worker edition '
                         'to provide compute / 这是主控端安装包；提供算力请安装独立算力端。')


def _save_settings(root, settings):
    private_write(root / 'service.json', settings)


def _write_status(root, **values):
    current = common.read_json(root / 'status.json', {})
    current.update(values, updated_at=time.time())
    private_write(root / 'status.json', current)
    return current


def _public_candidate(item):
    return {key: item[key] for key in ('path', 'node_id', 'root', 'hub_url', 'pids') if key in item}


def select_configuration(root, config=None, pairing=None, worker_root=None):
    """Noninteractive selection. Never regenerate a previously paired config."""
    settings = _settings(root)
    enrolled = None
    if pairing:
        pairing_path = Path(pairing).expanduser().resolve()
        if pairing_path.stat().st_size > 16384:
            raise ValueError('Pairing file is too large')
        enrolled = validate_pairing(common.read_json(pairing_path))
    supplied = config or settings.get('config')
    if not supplied and worker_root:
        proposed = Path(worker_root).expanduser().resolve() / 'node.ready.json'
        if proposed.is_file():
            supplied = str(proposed)
    candidates = []
    selected = None
    if supplied:
        selected = _candidate(supplied)
        if selected is None:
            # A saved setup may not have finished creating this file yet.
            if config or not settings.get('pairing'):
                raise ValueError('Selected worker configuration does not exist or is invalid')
    else:
        candidates = discover_configs()
        if enrolled:
            candidates = [item for item in candidates if item['node_id'] == enrolled['node_id']]
        active = [item for item in candidates if item.get('pids')]
        if len(active) == 1:
            selected = active[0]
        elif len(candidates) == 1:
            selected = candidates[0]
        elif candidates:
            return {'status': 'selection_required', 'running': False,
                    'detail': 'Choose an existing worker configuration / 请选择原算力端配置',
                    'candidates': [_public_candidate(item) for item in candidates]}
    if selected:
        current = common.read_json(selected['path'])
        if enrolled and any(current.get(key) != enrolled[key] for key in ('node_id', 'token', 'hub_url')):
            raise ValueError('Pairing does not match the existing worker. Existing configuration was preserved.')
        settings.update(config=selected['path'], node_id=selected['node_id'],
                        worker_root=str(Path(selected['path']).parent))
    else:
        stored_pairing = root / 'pairing.json'
        if enrolled is None and stored_pairing.is_file():
            enrolled = validate_pairing(common.read_json(stored_pairing))
        if enrolled is None:
            return {'status': 'pairing_required', 'running': False,
                    'detail': 'Choose the credential exported by the controller / 请选择管理端提供的凭证',
                    'candidates': []}
        data_root = Path(worker_root or settings.get('worker_root') or
                         Path.home() / '.local/share/experiment-manager/worker').expanduser().resolve()
        existing = common.read_json(data_root / 'node.ready.json')
        if existing:
            raise ValueError('Worker directory already has an enrollment; select its existing configuration')
        settings.update(config=str(data_root / 'node.ready.json'), node_id=enrolled['node_id'],
                        worker_root=str(data_root), pairing=str(stored_pairing))
        private_write(stored_pairing, enrolled)
    settings.setdefault('backend', 'detached')
    _save_settings(root, settings)
    return settings


def _process_identity(pid, proc_root='/proc'):
    """Linux start ticks prevent signalling a recycled PID."""
    try:
        folder = Path(proc_root) / str(int(pid))
        if hasattr(os, 'getuid') and folder.stat().st_uid != os.getuid():
            return None
        value = (folder / 'stat').read_text(encoding='utf-8')
        fields = value[value.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        argv = (folder / 'cmdline').read_bytes().split(b'\0')
        if b'expman.worker_service' not in argv or b'_serve' not in argv:
            return None
        return fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _owned_process(state):
    pid = state.get('pid')
    identity = state.get('process_identity')
    return bool(pid and identity and _process_identity(pid) == identity)


def status(service_root=None):
    root = _root(service_root)
    settings = _settings(root)
    state = common.read_json(root / 'status.json', {})
    running = _owned_process(state)
    result = {key: state[key] for key in ('status', 'detail', 'pid', 'node_id', 'updated_at',
               'online', 'jobs', 'shutdown') if key in state}
    result.update(running=running, backend=settings.get('backend', 'detached'),
                  node_id=settings.get('node_id'), config=settings.get('config'),
                  installed_version=settings.get('version'), last_run_version=state.get('version'),
                  service_root=str(root), log_path=str(root / 'worker.log'),
                  session_note=settings.get('session_note',
                    'Background process; auto-start at login is not configured / 后台运行，未配置登录自启'))
    if running:
        # A deployment can change while stopped without running that version.
        # Only the verified live owner's own report describes a running backend;
        # installation metadata and previous-run history remain separate fields.
        if 'version' in state:
            result['version'] = state['version']
        # _write_status merges old fields, including after launching older code.
        # Only trust a capability published for this exact process lifetime.
        if state.get('update_stop_process_identity') == state.get('process_identity'):
            if 'update_stop_protocol' in state:
                result['update_stop_protocol'] = state['update_stop_protocol']
        if state.get('shutdown_process_identity') == state.get('process_identity'):
            if 'shutdown_protocol' in state:
                result['shutdown_protocol'] = state['shutdown_protocol']
        return result
    config_path = settings.get('config')
    candidate = _candidate(config_path) if config_path else None
    if candidate and agent_is_running(candidate['root']):
        result.update(status='external_running', running=True, pid=None,
            detail='An existing worker owns this configuration. Close its old launcher to hand over. '
                   '/ 原代理仍在运行；退出原启动窗口后再由客户端启动。')
    elif state.get('status') in ('starting', 'preparing') and time.time() - state.get('updated_at', 0) < 10:
        # The detached child has not recorded its process identity yet.
        result.update(status=state['status'], detail=state.get('detail', 'Starting worker'))
    elif state.get('status') == 'shutting_down':
        if state.get('pid') is None and time.time() - state.get('updated_at', 0) < 10:
            result.update(status='shutting_down', detail='正在启动保存退出检查，请稍候')
        else:
            result.update(status='exit_failed', detail='保存退出进程未完成，请重试；实验及待回传数据仍保留在本机')
    elif state.get('status') in ('failed', 'exit_failed'):
        result.update(status=state['status'])
    else:
        result.update(status='stopped', pid=None,
            detail='Agent stopped. Existing Docker experiments can continue. '
                   '/ 代理已停止，已启动的 Docker 实验可继续运行。')
    return result


def _require_linux():
    if sys.platform != 'linux' or sys.version_info < (3, 10):
        raise RuntimeError('Use Linux/WSL2 with Python 3.10 or newer')
    if os.geteuid() == 0:
        raise RuntimeError('Run as the existing normal Linux user; do not use sudo / 请用原普通用户运行')


def _unit_name(root):
    return 'experiment-manager-worker-' + hashlib.sha256(str(root).encode()).hexdigest()[:12] + '.service'


def _spawn_supervisor(root, settings, *, shutdown_only=False):
    # A stopped older installation can be drained by the current package without
    # starting its normal scheduler or changing the saved node configuration.
    package = str(Path(__file__).resolve().parents[1]) if shutdown_only else (
        settings.get('package_dir') or str(Path(__file__).resolve().parents[1]))
    executable = sys.executable if shutdown_only else settings.get('python', sys.executable)
    environment = dict(os.environ, PYTHONPATH=package, PYTHONUNBUFFERED='1')
    argv = [executable, '-u', '-m', 'expman.worker_service', '_serve', '--service-root', str(root)]
    if shutdown_only:
        argv.append('--shutdown-only')
    with (root / 'worker.log').open('a', encoding='utf-8') as output:
        subprocess.Popen(argv, cwd=package, env=environment, stdin=subprocess.DEVNULL,
                         stdout=output, stderr=output, start_new_session=True)


def start(service_root=None, *, config=None, pairing=None, worker_root=None):
    _require_linux()
    root = _root(service_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with InstanceLock(root / 'control.lock'):
        previous = status(root)
        if previous['running'] or previous['status'] in ('starting', 'preparing'):
            return previous
        settings = select_configuration(root, config, pairing, worker_root)
        if settings.get('status') in ('selection_required', 'pairing_required'):
            return {**previous, **settings}
        previous = status(root)
        if previous['running']:
            return previous
        _write_status(root, status='starting', detail='Starting worker / 正在启动算力端',
                      node_id=settings['node_id'], pid=None, process_identity=None, online=False,
                      stop_requested=False, shutdown=None)
        (root / 'shutdown-request.json').unlink(missing_ok=True)
        if settings.get('backend') == 'systemd':
            reply = subprocess.run(['systemctl', '--user', 'start', _unit_name(root)],
                                   capture_output=True, text=True, timeout=15)
            if reply.returncode:
                _write_status(root, status='failed', detail='Could not start user service: ' + reply.stderr[-1000:])
            return status(root)
        _spawn_supervisor(root, settings)
        return status(root)


def stop(service_root=None):
    _require_linux()
    root = _root(service_root)
    with InstanceLock(root / 'control.lock'):
        current = status(root)
        if current['status'] == 'external_running':
            return current
        state = common.read_json(root / 'status.json', {})
        if _owned_process(state):
            _write_status(root, status='stopping',
                detail='Stopping agent; Docker experiments remain running / 正在停止代理；Docker 实验继续运行')
            # Signal only our identity-checked supervisor, never a process group or Docker.
            os.kill(state['pid'], signal.SIGTERM)
        elif state.get('status') in ('starting', 'preparing'):
            _write_status(root, stop_requested=True, status='stopped',
                          detail='Startup canceled / 已取消启动')
        return status(root)


def _shutdown_requested(root):
    request = common.read_json(root / 'shutdown-request.json', {})
    owner = common.read_json(root / 'status.json', {})
    return bool(request.get('request_id') and request.get('pid') == owner.get('pid')
                and request.get('process_identity')
                and request['process_identity'] == owner.get('process_identity'))


def shutdown(service_root=None):
    """Request checkpoint-and-exit without killing the agent or its experiments."""
    _require_linux()
    root = _root(service_root)
    with InstanceLock(root / 'control.lock'):
        current = status(root)
        if current['status'] == 'starting' or (current['status'] == 'preparing' and not current['running']):
            return dict(current, status='exit_failed', detail='代理正在准备环境，当前步骤尚不能安全取消；'
                        '请等待准备完成后重试退出。本次未停止任何实验或其他容器。')
        if not current['running']:
            if current['status'] == 'shutting_down':
                return current
            settings = _settings(root)
            if settings.get('config') and Path(settings['config']).is_file():
                _write_status(root, status='shutting_down', pid=None, process_identity=None, online=False,
                              detail='正在启动保存退出检查；不会启动新的实验')
                _spawn_supervisor(root, settings, shutdown_only=True)
                return status(root)
            # Failed first-time preparation has no usable Agent configuration.
            # It can still exit, but claim the supervisor lock before publishing
            # success so a delayed child cannot race us into starting setup.
            from .desktop import _lock_is_held
            try:
                with InstanceLock(root / 'supervisor.lock'):
                    folders = set()
                    if settings.get('worker_root'):
                        folders.add(Path(settings['worker_root']).expanduser().resolve())
                    if settings.get('config'):
                        folders.add(Path(settings['config']).expanduser().resolve().parent)
                    if any(_lock_is_held(folder / 'setup.lock') or
                           _lock_is_held(folder / 'runtime' / 'agent.lock') for folder in folders):
                        return dict(current, status='exit_failed', running=True,
                                    detail='原环境准备或节点代理仍在运行，尚不能确认退出；请稍后重试')
                    if any((folder / 'runtime' / 'node.sqlite3').exists() for folder in folders):
                        return dict(current, status='exit_failed',
                                    detail='原节点配置缺失但实验记录仍在，请恢复配置后保存退出；本次保留了全部数据')
                    _write_status(root, status='stopped', pid=None, process_identity=None,
                                  online=False, stop_requested=True,
                                  detail='环境准备已结束，未运行代理或实验；软件可以退出')
            except RuntimeError:
                return dict(current, status='exit_failed', running=True,
                            detail='原代理或准备进程仍占用服务目录，请等待其退出后重试')
            return status(root)
        if type(current.get('shutdown_protocol')) is not int or current['shutdown_protocol'] != SHUTDOWN_PROTOCOL:
            return dict(current, status='exit_failed', manual_shutdown_required=True,
                detail='当前后台版本不支持保存实验后退出。请先在实验列表暂停所有运行实验并确认停止，'
                       '再停止旧代理并启动新版客户端；本次未停止代理，也未中断实验。')
        owner = common.read_json(root / 'status.json', {})
        if not _owned_process(owner):
            return dict(current, status='exit_failed', detail='代理所有权已变化，请重试退出')
        if not _shutdown_requested(root):
            private_write(root / 'shutdown-request.json', dict(request_id=uuid.uuid4().hex,
                pid=owner['pid'], process_identity=owner['process_identity']))
        _write_status(root, status='shutting_down',
            detail='正在请求实验保存并停止；不再启动新实验。无原生续训能力的实验将记为中断。')
        return status(root)


def _shutdown_step(root, agent):
    """Drain only local work; never sync, accept assignments or launch a task."""
    from .agent import ACTIVE, TERMINAL, _pid_alive, inspect_update_state
    errors = []
    lingering = 0
    deferred = set()
    for record in agent.records():
        try:
            if (record['state'] == 'starting' and record['spec']['backend'] == 'docker'
                    and record.get('container_name') and not record.get('stop_action')):
                container = agent._inspect(record)
                if container and container['State'].get('Status') == 'created':
                    # No execution happened. Leave its durable start intent for
                    # next launch instead of stranding an unresumable algorithm.
                    deferred.add(record['id'])
                    continue
            if (record['state'] in TERMINAL and record['spec']['backend'] == 'docker'
                    and record.get('container_name')):
                container = agent._inspect(record)
                state = container['State'] if container else {}
                if state.get('Running') or state.get('Restarting') or state.get('Paused'):
                    # A previous supervisor may have stopped before learning the
                    # real process outcome. Verify ownership through _inspect and
                    # request a checkpoint without rewriting its historical state.
                    common.atomic_json(agent._output(record) / 'STOP', {'reason': 'application exiting'})
                    if record.get('shutdown_stop_at') is None:
                        agent._save(record, shutdown_stop_at=common.now(), archive_scanned=False)
                    if common.now() - record['shutdown_stop_at'] >= float(agent.config.get('stop_grace_seconds', 60)):
                        agent._exec(['docker', 'stop', '--time', '10', record['container_name']], timeout=30)
                    lingering += 1
            if record['state'] in ACTIVE:
                if record.get('exit_requested_attempt') != record['attempt']:
                    # Local exit does not acknowledge an unseen Hub command.
                    if record.get('stop_at') is None:
                        agent._command(record, 'stop', record.get('command_ack', 0))
                    agent._save(record, exit_requested_attempt=record['attempt'])
                if record['state'] in ACTIVE:
                    agent._reconcile(record)
                agent._metrics(record)
            if (record.get('exit_requested_attempt') == record['attempt'] and record['state'] == 'paused'
                    and record.get('ever_started') and record['spec'].get('resume_supported') is False):
                agent._save(record, state='interrupted',
                    detail='Stopped for application exit; this algorithm has no configured native resume')
            # A demo interrupted by an earlier agent may still be flushing its
            # checkpoint. Only its STOP file is authority; never kill a saved PID.
            if (record['state'] in TERMINAL and record['spec']['backend'] == 'demo'
                    and _pid_alive(record.get('worker_pid'))):
                common.atomic_json(agent._output(record) / 'STOP', {'reason': 'application exiting'})
                errors.append('演示进程仍在保存并退出，请等待；不会按历史 PID 强制结束进程')
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
            errors.append(_redact(root, str(error))[:300])
    records = agent.records()
    active = sum(record['state'] in ACTIVE and record['id'] not in deferred for record in records) + lingering
    progress = inspect_update_state(agent.db)
    progress.update(active_jobs=active,
        queued_jobs=sum(record['state'] in ('assigned', 'preparing', 'ready') or record['id'] in deferred
                        for record in records),
        paused_jobs=sum(record['state'] == 'paused' for record in records),
        interrupted_jobs=sum(record['state'] == 'interrupted' for record in records))
    if not active and not errors and (not agent.config.get('allow_demo', False)
            or any(record['spec']['backend'] == 'docker' for record in records)):
        try:
            candidate = _candidate(_settings(root).get('config'))
            live = _update_containers(candidate, execution_only=True)
            if live:
                errors.append(f'{len(live)} 个受管容器仍未退出，请确认实验状态后重试')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
            errors.append(_redact(root, str(error))[:300])
    detail = (f'正在保存并停止实验：还剩 {active} 个；已暂停 {progress["paused_jobs"]} 个，'
              f'中断 {progress["interrupted_jobs"]} 个。待回传数据保留在本机，下次启动继续同步。')
    _write_status(root, status='exit_failed' if errors else 'shutting_down', shutdown=progress,
                  detail='退出尚未完成：' + '；'.join(errors) if errors else detail)
    return not active and not errors


def _update_containers(candidate, *, execution_only=False):
    """Use exactly the Docker endpoint selected by the service; never stop a container."""
    environment = dict(os.environ)
    endpoint = common.read_json(Path(candidate['path']).parent / 'setup-state.json', {}).get('docker_endpoint')
    if isinstance(endpoint, str) and endpoint.startswith('unix://'):
        environment.pop('DOCKER_CONTEXT', None)
        environment['DOCKER_HOST'] = endpoint
    reply = subprocess.run(['docker', 'ps', '--all', '--filter', 'label=expman.node=' + candidate['node_id'],
                            '--filter', 'label=expman.job', '--format', '{{json .}}'],
                           capture_output=True, text=True, timeout=10, env=environment)
    if reply.returncode:
        raise ValueError('无法核查 Docker 容器，请先恢复 Docker 连接')
    active = []
    for line in reply.stdout.splitlines():
        item = json.loads(line)
        if not item.get('ID') or not isinstance(item.get('State'), str):
            raise ValueError('Docker 返回了无法识别的容器状态')
        inactive = ('created', 'exited', 'dead') if execution_only else ('exited',)
        if item['State'] not in inactive:
            active.append(item['ID'])
    return active


def update_status(service_root=None):
    """Inspect persisted work and Docker without constructing or recovering an Agent."""
    root = _root(service_root)
    result = {'running': False, 'status': 'unknown', 'ready_for_update': False}
    try:
        result.update(status(root))
        if result['running'] and not supports_update_stop(result):
            return manual_stop_required(result, worker=True)
        if result.get('status') in ('starting', 'preparing', 'stopping', 'external_running', 'shutting_down', 'exit_failed'):
            return dict(result, ready_for_update=False, detail='代理正在准备、停止或由旧入口运行，请完成后重试')
        if result['running'] and (not result.get('online') or time.time() - result.get('updated_at', 0) > 45):
            return dict(result, ready_for_update=False, detail='代理尚未确认与管理端同步，请恢复连接并等待同步完成')
        settings = _settings(root)
        config_path = settings.get('config')
        if not config_path:
            if result['running'] or settings.get('pairing'):
                return dict(result, ready_for_update=False, detail='算力端配置尚未准备完成')
            return dict(result, ready_for_update=True, detail='算力端尚未配置，可以安装更新')
        candidate = _candidate(config_path)
        if candidate is None:
            return dict(result, ready_for_update=False, detail='无法读取原算力端配置，不能核查更新条件')
        database = Path(candidate['root']) / 'node.sqlite3'
        counts = {}
        if database.exists():
            from .agent import inspect_update_state
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('BEGIN')
                counts = inspect_update_state(db)
            if not counts['ready_for_update']:
                return dict(result, **counts, detail='仍有待执行实验、待扫描文件、未确认报告或文件回传，请完成后重试')
        elif result['running']:
            return dict(result, ready_for_update=False, detail='代理数据库缺失，无法核查实验状态')
        containers = _update_containers(candidate)
        if containers:
            return dict(result, ready_for_update=False, active_containers=len(containers),
                        detail=f'{len(containers)} 个受管 Docker 容器尚未退出，请等待实验完成')
        return {**result, **counts, 'ready_for_update': True, 'detail': '实验和文件回传已完成，可以安装更新'}
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError, subprocess.SubprocessError) as error:
        try:
            detail = _redact(root, str(error))[:300]
        except (OSError, ValueError, TypeError, AttributeError):
            detail = '配置或状态文件无法读取'
        return dict(result, ready_for_update=False, detail='无法核查更新条件：' + detail)


def install_status(service_root=None):
    """Manual installation may preserve queued work once every old process exits."""
    from .desktop import _lock_is_held
    root = _root(service_root)
    result = {'running': False, 'status': 'unknown', 'ready_for_install': False}
    try:
        result.update(status(root))
        if (result['running'] or result['status'] in ('starting', 'preparing', 'stopping', 'shutting_down')
                or _lock_is_held(root / 'supervisor.lock')):
            return dict(result, ready_for_install=False,
                        detail='旧代理或准备进程尚未退出，请完成保存退出后再安装')
        settings = _settings(root)
        if not settings.get('config'):
            return dict(result, ready_for_install=True, detail='没有运行中的旧代理，可以安装')
        candidate = _candidate(settings['config'])
        if candidate is None:
            return dict(result, ready_for_install=False, detail='无法读取原节点配置，不能确认运行中的实验')
        if agent_is_running(candidate['root']):
            return dict(result, running=True, ready_for_install=False, detail='原节点代理仍在运行，请先退出')
        live = _update_containers(candidate, execution_only=True)
        if live:
            return dict(result, ready_for_install=False, active_containers=len(live),
                        detail=f'{len(live)} 个受管容器仍在运行，请先保存并停止实验')
        return dict(result, ready_for_install=True,
                    detail='旧代理及实验已退出，可以安装；原实验队列和待回传数据将保留')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as error:
        return dict(result, ready_for_install=False, detail='无法核查安装条件：' + _redact(root, str(error))[:300])


def stop_for_update(service_root=None):
    """Request an idle tick-boundary stop, without sending SIGTERM or changing tasks."""
    _require_linux()
    root = _root(service_root)
    with InstanceLock(root / 'control.lock'):
        result = update_status(root)
        if not result['ready_for_update'] or not result['running']:
            return result
        request_id = uuid.uuid4().hex
        state = common.read_json(root / 'status.json', {})
        request_path = root / 'update-request.json'
        private_write(request_path, dict(request_id=request_id, pid=state.get('pid'),
                      process_identity=state.get('process_identity'), expires=time.time() + 35))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            reply = common.read_json(root / 'update-result.json', {})
            if reply.get('request_id') == request_id:
                return dict(result, **reply)
            time.sleep(0.1)
        current = common.read_json(request_path, {})
        if current.get('request_id') == request_id:
            request_path.unlink(missing_ok=True)
        return dict(result, ready_for_update=False,
                    detail='安全停止检查超时，尚未安装更新；请待同步完成后重试。旧版本请手动退出后安装。')


def _update_stop_at_boundary(root, agent):
    """Runs only on the agent thread, between complete durable ticks."""
    request = common.read_json(root / 'update-request.json', {})
    owner = common.read_json(root / 'status.json', {})
    if (not request.get('request_id') or request.get('expires', 0) <= time.time()
            or request.get('pid') != owner.get('pid')
            or request.get('process_identity') != owner.get('process_identity')):
        return False
    prior = common.read_json(root / 'update-result.json', {})
    if prior.get('request_id') == request['request_id']:
        return False
    if agent.preparation or agent.project_delivery.installing:
        result = dict(ready_for_update=False, detail='代理正在准备实验或安装项目，请完成后重试')
    else:
        result = update_status(root)
    # Stop acceptance is the last operation before leaving this loop; no sync,
    # launch or preparation can happen between the check and Agent.close().
    latest = common.read_json(root / 'update-request.json', {})
    if latest.get('request_id') != request['request_id'] or request.get('expires', 0) <= time.time():
        return False
    accepted = result['ready_for_update']
    if accepted:
        _write_status(root, status='stopping', detail='已确认空闲，正在安全停止以安装更新')
    private_write(root / 'update-result.json', dict(result, request_id=request['request_id'],
                  status='stopping' if accepted else owner.get('status', 'online')))
    return accepted


def _secret_values(root):
    settings = _settings(root)
    secrets = []
    for path in (settings.get('config'), settings.get('pairing')):
        if path:
            data = common.read_json(path, {})
            if data.get('token'):
                secrets.append(data['token'])
    return secrets


def _redact(root, text):
    for secret in _secret_values(root):
        text = text.replace(secret, '[redacted]')
    return text


def logs(service_root=None, lines=80):
    root = _root(service_root)
    path = root / 'worker.log'
    if not path.is_file():
        return {**status(root), 'lines': []}
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 128 * 1024))
        value = stream.read().decode('utf-8', 'replace')
    return {**status(root), 'lines': _redact(root, value).splitlines()[-max(1, min(lines, 1000)):]}


def hold(service_root=None):
    """Keep a foreground WSL client attached while its worker is active.

    Windows owns the foreground wsl.exe handle. This watcher never starts,
    stops or signals an agent and exits when the worker has stopped. Only one
    watcher is needed per installed client, including after reopening its UI.
    """
    _require_linux()
    root = _root(service_root)
    lock = InstanceLock(root / 'hold.lock')
    try:
        lock.__enter__()
    except RuntimeError:
        # Another Windows client already holds this worker's WSL session.
        return 0
    try:
        grace = time.monotonic() + 10
        while True:
            current = status(root)
            if current.get('running'):
                pass
            elif current.get('status') in ('starting', 'preparing', 'stopping', 'shutting_down') and time.monotonic() < grace:
                pass
            else:
                return 0
            time.sleep(0.5)
    finally:
        lock.__exit__(None, None, None)


def _run_agent(root, config_path):
    from .agent import Agent
    agent = Agent(config_path)
    previous = None
    try:
        while True:
            if _shutdown_requested(root):
                if _shutdown_step(root, agent):
                    return
                time.sleep(0.5)
                continue
            if _update_stop_at_boundary(root, agent):
                return
            snapshot = agent.tick(stop_requested=lambda: _shutdown_requested(root))
            if _shutdown_requested(root):
                continue
            online = bool(snapshot['online'])
            detail = ('Connected to controller / 已连接管理端' if online else
                      'Controller offline; cached tasks continue / 管理端离线，已缓存任务继续')
            if snapshot.get('error'):
                detail += ': ' + _redact(root, str(snapshot['error']))
            counts = {}
            for record in snapshot.get('jobs', []):
                state = record.get('state', 'unknown')
                counts[state] = counts.get(state, 0) + 1
            _write_status(root, status='online' if online else 'offline', online=online,
                          detail=detail, jobs=counts)
            if detail != previous:
                print(detail, flush=True)
                previous = detail
            time.sleep(max(0.1, float(agent.config.get('poll_seconds', 5))))
    finally:
        agent.close()


def serve(service_root=None, *, shutdown_only=False):
    _require_linux()
    root = _root(service_root)
    # Both detached and systemd paths acquire this lock before setting state.
    with InstanceLock(root / 'supervisor.lock'):
        settings = _settings(root)
        if common.read_json(root / 'status.json', {}).get('stop_requested') and not shutdown_only:
            return 0
        pid = os.getpid()
        identity = _process_identity(pid)
        _write_status(root, status='preparing', detail='Checking installation / 正在准备环境',
                      pid=pid, process_identity=identity, node_id=settings.get('node_id'),
                      online=False, version=__version__, update_stop_protocol=UPDATE_STOP_PROTOCOL,
                      update_stop_process_identity=identity, shutdown_protocol=SHUTDOWN_PROTOCOL,
                      shutdown_process_identity=identity)
        if shutdown_only:
            private_write(root / 'shutdown-request.json', dict(request_id=uuid.uuid4().hex,
                pid=pid, process_identity=identity))
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        prior = signal.signal(signal.SIGTERM, interrupted)
        try:
            config_path = Path(settings['config'])
            if not config_path.is_file():
                from .worker_setup import start as setup
                setup(argparse.Namespace(root=settings['worker_root'], pairing=settings['pairing'],
                      configure=False, gpu=None, prepare_only=True),
                      cancel_requested=lambda: _shutdown_requested(root))
            config = common.read_json(config_path)
            private_connection(config)
            endpoint = common.read_json(config_path.parent / 'setup-state.json', {}).get('docker_endpoint')
            if isinstance(endpoint, str) and endpoint.startswith('unix://'):
                os.environ.pop('DOCKER_CONTEXT', None)
                os.environ['DOCKER_HOST'] = endpoint
            _run_agent(root, str(config_path))
            detail = ('实验已保存或记录为中断，代理已退出；待回传数据保留在本机，下次启动继续同步。'
                      if _shutdown_requested(root) else '代理已安全停止，可以安装更新')
            _write_status(root, status='stopped', online=False, detail=detail)
        except KeyboardInterrupt:
            if _shutdown_requested(root):
                _write_status(root, status='exit_failed', online=False,
                              detail='保存退出过程被外部停止打断，尚未确认实验全部退出；请重试保存退出')
            else:
                _write_status(root, status='stopped', online=False,
                    detail='Agent stopped; Docker experiments keep running / 代理已停止，Docker 实验继续运行')
        except Exception as error:
            from .worker_setup import SetupCancelled
            if isinstance(error, SetupCancelled) and _shutdown_requested(root):
                _write_status(root, status='stopped', online=False,
                              detail='环境准备已取消，本服务启动的准备子进程已退出；已下载镜像层保留')
                return 0
            detail = _redact(root, str(error))
            print('Worker error / 算力端错误: ' + detail, flush=True)
            _write_status(root, status='failed', online=False, detail=detail)
            return 1
        finally:
            signal.signal(signal.SIGTERM, prior)
        return 0


def _systemd_available():
    if not shutil.which('systemctl'):
        return False
    try:
        return subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _quoted_unit(value):
    if any(char in str(value) for char in ('\n', '\r', '\0')):
        raise ValueError('Invalid systemd argument')
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _unit_path(value):
    # WorkingDirectory and append: destinations do not unquote/unescape their
    # values like ExecStart and Environment do. Only specifiers need escaping.
    value = str(value)
    if (any(char in value for char in ('\n', '\r', '\0')) or
            value.endswith('\\') or value != value.rstrip()):
        raise ValueError('Invalid systemd path')
    return value.replace('%', '%%')


def _installation_payload():
    package = Path(__file__).resolve().parent
    entries = [(path.relative_to(package), path.read_bytes()) for path in sorted(package.rglob('*'))
               if path.is_file() and '__pycache__' not in path.parts and path.suffix in
               ('.py', '.js', '.html', '.css')]
    identity = hashlib.sha256()
    marker = _release_role() or {'role': 'ubuntu-worker-x64', 'version': __version__}
    identity.update(json.dumps(marker, sort_keys=True).encode('utf-8') + b'\0')
    for relative, data in entries:
        identity.update(relative.as_posix().encode() + b'\0' + hashlib.sha256(data).digest())
    return entries, marker, identity.hexdigest()


def _matches_installation(installed, entries, marker):
    try:
        return (common.read_json(installed / 'release-role.json') == marker and
                all((installed / 'expman' / relative).read_bytes() == data for relative, data in entries))
    except (OSError, ValueError):
        return False


def install(service_root=None, *, config=None, pairing=None, worker_root=None, backend='auto', start_now=True):
    """Install immutable application files per user, without sudo or policy edits."""
    _require_linux()
    root = _root(service_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with InstanceLock(root / 'control.lock'):
        entries, marker, identity = _installation_payload()
        installed = root / 'software' / identity
        current = status(root)
        if current['running'] or current['status'] in ('starting', 'preparing'):
            saved_package = _settings(root).get('package_dir')
            if (saved_package and Path(saved_package).resolve() == installed.resolve() and
                    _matches_installation(installed, entries, marker)):
                # Start/login is idempotent for the currently installed bits.
                # Preserve the useful connected/preparing/offline detail.
                return current
            return {**current, 'detail': 'Stop the current agent before installing an update / 更新前请先停止当前代理'}
        settings = select_configuration(root, config, pairing, worker_root)
        if settings.get('status'):
            return {**current, **settings}
        for relative, data in entries:
            destination = installed / 'expman' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.read_bytes() != data:
                raise ValueError('An installed application version was modified')
            if not destination.exists():
                destination.write_bytes(data)
        marker_path = installed / 'release-role.json'
        if marker_path.is_file() and common.read_json(marker_path) != marker:
            raise ValueError('Installed application role changed')
        if not marker_path.exists():
            common.atomic_json(marker_path, marker)
        use_systemd = backend == 'systemd' or backend == 'auto' and _systemd_available()
        if use_systemd and not _systemd_available():
            raise RuntimeError('The systemd user session is unavailable; use --backend detached')
        settings.update(package_dir=str(installed), python=sys.executable,
                        backend='systemd' if use_systemd else 'detached', version=__version__)
        launch = root / 'Start-Worker.sh'
        launch.write_text('#!/usr/bin/env sh\nexport PYTHONPATH=' + shlex.quote(str(installed)) +
            '\nexec ' + shlex.quote(sys.executable) + ' -m expman.worker_service start --service-root ' +
            shlex.quote(str(root)) + '\n', encoding='utf-8')
        launch.chmod(0o700)
        desktop = Path.home() / '.local/share/applications' / (_unit_name(root)[:-8] + '.desktop')
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text('[Desktop Entry]\nType=Application\nName=Experiment Manager Worker\n'
            'Name[zh_CN]=实验台算力端\nComment=Start the background compute worker\n'
            'Exec=' + _quoted_unit(str(launch)) + '\nTerminal=false\nCategories=Development;Science;\n',
            encoding='utf-8')
        if use_systemd:
            unit_dir = Path.home() / '.config/systemd/user'
            unit_dir.mkdir(parents=True, exist_ok=True)
            unit = unit_dir / _unit_name(root)
            unit.write_text('[Unit]\nDescription=Experiment Manager compute worker\n'
                '\n[Service]\nType=simple\nWorkingDirectory=' + _unit_path(installed) +
                '\nEnvironment=' + _quoted_unit('PYTHONPATH=' + str(installed)) +
                '\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=' + _quoted_unit(sys.executable) +
                ' -u -m expman.worker_service _serve --service-root ' + _quoted_unit(root) +
                '\nKillMode=process\nTimeoutStopSec=20\nRestart=no\n'
                'StandardOutput=append:' + _unit_path(root / 'worker.log') +
                '\nStandardError=append:' + _unit_path(root / 'worker.log') +
                '\n\n[Install]\nWantedBy=default.target\n', encoding='utf-8')
            for command in (['systemctl', '--user', 'daemon-reload'],
                            ['systemctl', '--user', 'enable', _unit_name(root)]):
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                if result.returncode:
                    raise RuntimeError('Could not install user service: ' + result.stderr[-1000:])
            settings['session_note'] = ('Starts with the Linux user session; logout/reboot availability depends '
                'on systemd linger and WSL startup. / 随 Linux 用户会话启动；退出登录后及重启后的运行取决于 linger 和 WSL 启动。')
        else:
            settings['session_note'] = ('Background process; start from the application menu after reboot. '
                'The Windows client keeps WSL attached while the worker is active. '
                'Windows login/startup is still required after reboot. '
                '/ 后台进程；Windows 客户端在代理运行时保持 WSL 会话，重启后仍需登录并启动客户端。')
        _save_settings(root, settings)
        if not start_now:
            # Installing software while stopped is a completed operation, even
            # if the previous process failed. Retain its version as run history
            # while clearing stale failures. Keep delayed supervisors canceled
            # until an explicit start() clears the startup guard.
            _write_status(root, status='stopped', detail='Worker software installed; not started / 算力端软件已安装，代理保持停止',
                          node_id=settings['node_id'], pid=None, process_identity=None,
                          online=False, stop_requested=True, shutdown=None)
    return start(root) if start_now else status(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'start', 'stop', 'status', 'logs', '_serve', '_hold',
                                         'update-status', 'stop-for-update', 'install-status', 'shutdown',
                                         'gpu-status', 'gpu-enable'))
    parser.add_argument('--shutdown-only', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--service-root')
    parser.add_argument('--root', help='Worker data directory; separate from lifecycle settings')
    parser.add_argument('--config', help='Reuse this existing node configuration without changing it')
    parser.add_argument('--pairing', help='Credential file exported by the controller')
    parser.add_argument('--backend', choices=('auto', 'systemd', 'detached'), default='auto')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--lines', type=int, default=80)
    parser.add_argument('--gpu', help='GPU UUID to verify and enable from local settings')
    args = parser.parse_args(argv)
    try:
        _check_release_role()
        if args.action == '_serve':
            return serve(args.service_root, shutdown_only=args.shutdown_only)
        if args.action == '_hold':
            return hold(args.service_root)
        if args.action in ('start', 'install'):
            options = dict(config=args.config, pairing=args.pairing, worker_root=args.root)
            if args.action == 'install':
                options.update(backend=args.backend, start_now=not args.no_start)
            value = globals()[args.action](args.service_root, **options)
        elif args.action in ('gpu-status', 'gpu-enable'):
            from .worker_gpu import gpu_status, gpu_enable
            value = (gpu_enable(args.service_root, gpu=args.gpu) if args.action == 'gpu-enable'
                     else gpu_status(args.service_root))
        elif args.action == 'logs':
            value = logs(args.service_root, args.lines)
        else:
            value = globals()[args.action.replace('-', '_')](args.service_root)
        print(json.dumps(value, ensure_ascii=False))
        return 2 if value['status'] in ('failed', 'selection_required', 'pairing_required') else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'running': False, 'status': 'failed',
                          'detail': _redact(_root(args.service_root), str(error))}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
