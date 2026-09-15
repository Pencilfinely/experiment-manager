"""User-owned background worker, with optional systemd user-session integration.

The service directory stores only lifecycle settings and logs. The existing node
configuration and its experiment directory remain the source of truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

from . import __version__, common
from .launcher import InstanceLock
from .pairing import validate_pairing
from .worker_setup import private_connection, private_write
from .worker_upgrade import _candidate, agent_is_running, discover_configs


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
               'online', 'jobs', 'version') if key in state}
    result.update(running=running, backend=settings.get('backend', 'detached'),
                  node_id=settings.get('node_id'), config=settings.get('config'),
                  service_root=str(root), log_path=str(root / 'worker.log'),
                  session_note=settings.get('session_note',
                    'Background process; auto-start at login is not configured / 后台运行，未配置登录自启'))
    if running:
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
    elif state.get('status') == 'failed':
        result.update(status='failed')
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
                      stop_requested=False)
        if settings.get('backend') == 'systemd':
            reply = subprocess.run(['systemctl', '--user', 'start', _unit_name(root)],
                                   capture_output=True, text=True, timeout=15)
            if reply.returncode:
                _write_status(root, status='failed', detail='Could not start user service: ' + reply.stderr[-1000:])
            return status(root)
        package = settings.get('package_dir') or str(Path(__file__).resolve().parents[1])
        environment = dict(os.environ, PYTHONPATH=package, PYTHONUNBUFFERED='1')
        with (root / 'worker.log').open('a', encoding='utf-8') as output:
            subprocess.Popen([settings.get('python', sys.executable), '-u', '-m', 'expman.worker_service',
                              '_serve', '--service-root', str(root)], cwd=package, env=environment,
                             stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
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
            elif current.get('status') in ('starting', 'preparing', 'stopping') and time.monotonic() < grace:
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
            snapshot = agent.tick()
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


def serve(service_root=None):
    _require_linux()
    root = _root(service_root)
    # Both detached and systemd paths acquire this lock before setting state.
    with InstanceLock(root / 'supervisor.lock'):
        settings = _settings(root)
        if common.read_json(root / 'status.json', {}).get('stop_requested'):
            return 0
        pid = os.getpid()
        identity = _process_identity(pid)
        _write_status(root, status='preparing', detail='Checking installation / 正在准备环境',
                      pid=pid, process_identity=identity, node_id=settings.get('node_id'),
                      online=False, version=__version__)
        def interrupted(signum, frame):
            raise KeyboardInterrupt
        prior = signal.signal(signal.SIGTERM, interrupted)
        try:
            config_path = Path(settings['config'])
            if not config_path.is_file():
                from .worker_setup import start as setup
                setup(argparse.Namespace(root=settings['worker_root'], pairing=settings['pairing'],
                      configure=False, gpu=None, prepare_only=True))
            config = common.read_json(config_path)
            private_connection(config)
            endpoint = common.read_json(config_path.parent / 'setup-state.json', {}).get('docker_endpoint')
            if isinstance(endpoint, str) and endpoint.startswith('unix://'):
                os.environ.pop('DOCKER_CONTEXT', None)
                os.environ['DOCKER_HOST'] = endpoint
            _run_agent(root, str(config_path))
        except KeyboardInterrupt:
            _write_status(root, status='stopped', online=False,
                detail='Agent stopped; Docker experiments keep running / 代理已停止，Docker 实验继续运行')
        except Exception as error:
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
                '\n[Service]\nType=simple\nWorkingDirectory=' + _quoted_unit(installed) +
                '\nEnvironment=' + _quoted_unit('PYTHONPATH=' + str(installed)) +
                '\nEnvironment=PYTHONUNBUFFERED=1\nExecStart=' + _quoted_unit(sys.executable) +
                ' -u -m expman.worker_service _serve --service-root ' + _quoted_unit(root) +
                '\nKillMode=process\nTimeoutStopSec=20\nRestart=no\n'
                'StandardOutput=' + _quoted_unit('append:' + str(root / 'worker.log')) +
                '\nStandardError=' + _quoted_unit('append:' + str(root / 'worker.log')) +
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
    return start(root) if start_now else status(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'start', 'stop', 'status', 'logs', '_serve', '_hold'))
    parser.add_argument('--service-root')
    parser.add_argument('--root', help='Worker data directory; separate from lifecycle settings')
    parser.add_argument('--config', help='Reuse this existing node configuration without changing it')
    parser.add_argument('--pairing', help='Credential file exported by the controller')
    parser.add_argument('--backend', choices=('auto', 'systemd', 'detached'), default='auto')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--lines', type=int, default=80)
    args = parser.parse_args(argv)
    try:
        _check_release_role()
        if args.action == '_serve':
            return serve(args.service_root)
        if args.action == '_hold':
            return hold(args.service_root)
        if args.action in ('start', 'install'):
            options = dict(config=args.config, pairing=args.pairing, worker_root=args.root)
            if args.action == 'install':
                options.update(backend=args.backend, start_now=not args.no_start)
            value = globals()[args.action](args.service_root, **options)
        elif args.action == 'logs':
            value = logs(args.service_root, args.lines)
        else:
            value = globals()[args.action](args.service_root)
        print(json.dumps(value, ensure_ascii=False))
        return 2 if value['status'] in ('failed', 'selection_required', 'pairing_required') else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'running': False, 'status': 'failed',
                          'detail': _redact(_root(args.service_root), str(error))}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
