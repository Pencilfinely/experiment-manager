"""Local desktop controller lifecycle. No shell command or algorithm is imported."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
import json
import math
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from . import __version__
from .common import atomic_json, read_json
from .launcher import InstanceLock, default_controller_root
from .update_protocol import UPDATE_STOP_PROTOCOL, manual_stop_required, supports_update_stop


def choose_directory():
    """Show the controller user's native folder picker, never on a remote host."""
    if sys.platform != 'win32':
        raise ValueError('Native folder selection is available on the Windows controller; enter a local path instead')
    # The script is constant: filesystem paths/user input are not interpolated.
    script = r'''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.Width = 1
$owner.Height = 1
$owner.ShowInTaskbar = $false
$owner.StartPosition = 'CenterScreen'
$picker = New-Object System.Windows.Forms.FolderBrowserDialog
$picker.Description = 'Choose the original algorithm folder / 选择原算法根目录'
$picker.ShowNewFolderButton = $false
try {
  $owner.Show()
  if ($picker.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::Write($picker.SelectedPath)
  }
} finally { $picker.Dispose(); $owner.Dispose() }
'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-EncodedCommand',
        base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
        capture_output=True, encoding='utf-8', errors='replace', timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError('The folder picker could not open; enter the path in the import page')
    selected = result.stdout.strip().lstrip('\ufeff')
    return str(Path(selected).resolve()) if selected else None


def controller_status(root):
    root = Path(root).expanduser().resolve()
    settings, hub = read_json(root / 'launcher.json', {}), read_json(root / 'hub.json', {})
    port = settings.get('port', 8765)
    state = read_json(root / 'desktop-process.json', {})
    # A failed HTTP probe says nothing about process exit. In particular, the
    # HTTP listener stops before imports, database handles and the lifecycle
    # lock are released, so installation must keep waiting for that lock.
    alive = _lock_is_held(root / 'controller.lock')
    result = {'running': alive, 'responsive': False,
              'status': 'unresponsive' if alive else 'stopped', 'root': str(root), 'port': port,
              'log_path': str(root / 'desktop.log'), 'managed': bool(state.get('nonce'))}
    result['detail'] = ('主控进程仍在运行，暂时无法连接；请稍候，或点击停止主控后重试'
                        if alive else '主控尚未启动')
    if not isinstance(port, int) or not hub.get('admin_token'):
        return result
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/state',
                                         headers={'Authorization': 'Bearer ' + hub['admin_token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
            value = json.load(response)
        if (not isinstance(value, dict) or not isinstance(value.get('nodes'), list)
                or not isinstance(value.get('jobs'), list)):
            raise ValueError('Invalid controller health response')
        # The request may have waited for startup. The owner file is written
        # before serve_forever, so refresh it after the successful health probe.
        state = read_json(root / 'desktop-process.json', {})
        result['managed'] = bool(state.get('nonce'))
        result.update(running=True, responsive=True, status='running', version=value.get('version'),
            nodes=len(value.get('nodes', [])), jobs=len(value.get('jobs', [])),
            detail='主控运行中；关闭应用窗口后继续在后台运行')
        if 'update_stop_protocol' in state and state.get('version') == value.get('version'):
            result['update_stop_protocol'] = state['update_stop_protocol']
    except (OSError, ValueError):
        # Startup or shutdown may complete while the health request is pending.
        alive = _lock_is_held(root / 'controller.lock')
        result.update(running=alive, status='unresponsive' if alive else 'stopped',
                      detail='主控进程仍在运行，暂时无法连接；请稍候，或点击停止主控后重试'
                      if alive else '主控尚未启动，或仍在启动中')
    return result


def controller_start(root, port=8765, host='0.0.0.0'):
    root = Path(root).expanduser().resolve()
    status = controller_status(root)
    if status['running']:
        return status
    root.mkdir(parents=True, exist_ok=True)
    with InstanceLock(root / 'desktop-start.lock'):
        status = controller_status(root)
        if status['running']:
            return status
        pending = read_json(root / 'desktop-pending.json', {})
        if time.time() - pending.get('started', 0) < 15:
            return dict(status, status='starting', detail='主控正在启动')
        atomic_json(root / 'desktop-pending.json', {'started': time.time()})
        executable = Path(sys.executable)
        windowless = executable.with_name('pythonw.exe')
        if os.name == 'nt' and windowless.is_file():
            executable = windowless
        argv = [str(executable), '-u', '-m', 'expman.desktop', 'controller-serve',
                '--root', str(root), '--port', str(port), '--host', host]
        kwargs = {'cwd': str(Path(__file__).resolve().parents[1]),
                  'stdin': subprocess.DEVNULL}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        with (root / 'desktop.log').open('ab', buffering=0) as log:
            process = subprocess.Popen(argv, stdout=log, stderr=log, **kwargs)
        for _ in range(30):
            status = controller_status(root)
            if status.get('responsive'):
                return status
            if process.poll() is not None:
                raise RuntimeError('主控启动失败，请在应用中打开日志查看原因：' + str(root / 'desktop.log'))
            time.sleep(0.15)
        return dict(status, status='starting', detail='主控正在启动，请稍候')


def controller_stop(root):
    root = Path(root).expanduser().resolve()
    status = controller_status(root)
    if not status['running']:
        return status
    process = read_json(root / 'desktop-process.json', {})
    if not process.get('nonce'):
        raise ValueError('主控由旧启动入口运行，请先退出旧主控窗口，再从本应用启动')
    atomic_json(root / 'desktop-stop.json', {'nonce': process['nonce']})
    return dict(status, status='stopping', detail='正在停止主控；节点中的 Docker 实验继续运行')


def _lock_is_held(path):
    """Probe an existing lifecycle lock without creating one or trusting a PID."""
    try:
        stream = Path(path).open('r+b')
    except FileNotFoundError:
        return False
    with stream:
        try:
            if os.name == 'nt':
                import msvcrt
                if not Path(path).stat().st_size:
                    return False
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True


def controller_update_status(root):
    root = Path(root).expanduser().resolve()
    result = {'running': False, 'status': 'unknown', 'ready_for_update': False, 'root': str(root)}
    try:
        result.update(controller_status(root))
        if result['running']:
            if result.get('responsive') is False:
                return dict(result, status='unknown', ready_for_update=False,
                            detail='主控进程尚未退出或无法连接，不能确认可以更新')
            if not supports_update_stop(result):
                return manual_stop_required(result, worker=False)
            if not result.get('managed'):
                return dict(result, ready_for_update=False, detail='请先退出旧主控启动窗口，再安装更新')
            config = read_json(root / 'hub.json')
            request = urllib.request.Request(f'http://127.0.0.1:{result["port"]}/api/update-status',
                headers={'Authorization': 'Bearer ' + config['admin_token']})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=4) as response:
                inspected = json.load(response)
            if not isinstance(inspected.get('ready_for_update'), bool):
                raise ValueError('主控未提供有效的更新检查结果')
            return dict(result, **inspected)
        if _lock_is_held(root / 'controller.lock') or _lock_is_held(root / 'desktop-start.lock'):
            return dict(result, status='unknown', ready_for_update=False,
                        detail='主控进程尚未退出或无法连接，不能确认可以更新')
        pending = read_json(root / 'desktop-pending.json', {})
        if time.time() - pending.get('started', 0) < 15:
            return dict(result, status='starting', ready_for_update=False, detail='主控正在启动，请稍候')
        database = root / 'hub.sqlite3'
        if not database.is_file():
            if (root / 'hub.json').exists():
                return dict(result, ready_for_update=False, detail='主控数据库缺失，无法核查实验状态')
            return dict(result, ready_for_update=True, detail='主控尚未配置，可以安装更新')
        from .hub import inspect_update_state
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('BEGIN')
            inspected = inspect_update_state(db)
        return dict(result, **inspected)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError) as error:
        return dict(result, ready_for_update=False, detail='无法核查更新条件，请恢复服务后重试：' + str(error)[:300])


def controller_install_status(root):
    """Allow side-by-side installation only after this local controller exits.

    This is distinct from stopping a running service for an automatic update:
    persisted experiments and remote workers survive a manual local migration,
    so their telemetry cannot decide whether local program files are in use.
    """
    root = Path(root).expanduser().resolve()
    result = {'running': False, 'responsive': False, 'status': 'unknown',
              'ready_for_install': False, 'root': str(root)}
    try:
        result.update(controller_status(root))
        if result['running']:
            return dict(result, ready_for_install=False, detail='请先停止本机主控，再安装新版本')
        if _lock_is_held(root / 'controller.lock') or _lock_is_held(root / 'desktop-start.lock'):
            return dict(result, running=True, status='starting',
                        detail='本机主控仍在启动或退出，请稍候再安装')
        pending_path = root / 'desktop-pending.json'
        pending = read_json(pending_path, {})
        if not isinstance(pending, dict):
            raise ValueError('主控启动记录无效')
        if pending_path.exists():
            started = pending.get('started')
            if (isinstance(started, bool) or not isinstance(started, (int, float))
                    or not math.isfinite(started) or started < 0):
                raise ValueError('主控启动记录无效')
            if time.time() - started < 15:
                return dict(result, status='starting', detail='本机主控正在启动，请稍候再安装')
        settings_path = root / 'launcher.json'
        settings = read_json(settings_path, {})
        if not isinstance(settings, dict):
            raise ValueError('主控端口配置无效')
        if settings_path.exists():
            port = settings.get('port')
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('主控端口配置无效')
            # An authentication or JSON error is not proof that a listener has
            # exited. Refuse migration while anything still owns its local port.
            try:
                connection = socket.create_connection(('127.0.0.1', port), timeout=2)
            except ConnectionRefusedError:
                pass
            else:
                connection.close()
                return dict(result, running=True, status='unresponsive',
                            detail='主控本机端口仍有服务运行，请停止后再安装')
        return dict(result, status='stopped', ready_for_install=True,
                    detail='本机主控已停止，可以安装新版本；实验数据将保留')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, OverflowError, AttributeError) as error:
        return dict(result, status='unknown', ready_for_install=False,
                    detail='无法确认本机主控已停止，暂不安装：' + str(error)[:300])


def controller_stop_for_update(root):
    """Ask the owning service to atomically check and fence new work before stop."""
    root = Path(root).expanduser().resolve()
    result = controller_update_status(root)
    if not result['ready_for_update'] or not result['running']:
        return result
    request_id = uuid.uuid4().hex
    owner = read_json(root / 'desktop-process.json', {})
    if not owner.get('nonce'):
        return dict(result, ready_for_update=False, detail='主控所有权已变化，请重试')
    request_path = root / 'desktop-update-request.json'
    atomic_json(request_path, {'nonce': owner['nonce'], 'request_id': request_id, 'expires': time.time() + 15})
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        reply = read_json(root / 'desktop-update-result.json', {})
        if reply.get('request_id') == request_id:
            return dict(result, **reply)
        time.sleep(0.1)
    # A late watcher must not shut down after the caller has abandoned installation.
    current = read_json(request_path, {})
    if current.get('request_id') == request_id:
        request_path.unlink(missing_ok=True)
    return dict(result, ready_for_update=False,
                detail='安全停止检查超时，尚未安装更新；请确认主控空闲后重试。旧版本请手动退出后安装。')


def controller_serve(root, port, host):
    from .hub import Hub, make_server
    root = Path(root).expanduser().resolve()
    with InstanceLock(root / 'controller.lock'):
        hub = Hub(root)
        server = None
        nonce = uuid.uuid4().hex
        try:
            saved = read_json(root / 'launcher.json', {})
            port = saved.get('port', port)
            for candidate in ([port] if saved else range(port, port + 20)):
                try:
                    server = make_server(hub, host, candidate)
                    break
                except OSError:
                    if saved or candidate == port + 19:
                        raise RuntimeError('主控端口已占用；请选择原主控数据目录，或关闭占用端口的程序') from None
            atomic_json(root / 'launcher.json', {'host': host, 'port': server.server_address[1]})
            atomic_json(root / 'desktop-process.json', {'pid': os.getpid(), 'nonce': nonce, 'started': time.time(),
                                                       'version': __version__,
                                                       'update_stop_protocol': UPDATE_STOP_PROTOCOL})
            root.joinpath('desktop-pending.json').unlink(missing_ok=True)
            stopped = threading.Event()

            def watch():
                while not stopped.wait(0.25):
                    try:
                        requested = read_json(root / 'desktop-stop.json', {})
                        update = read_json(root / 'desktop-update-request.json', {})
                    except (OSError, ValueError):
                        continue
                    if requested.get('nonce') == nonce:
                        server.shutdown()
                        return
                    if (update.get('nonce') == nonce and update.get('request_id')
                            and update.get('expires', 0) > time.time()):
                        previous = read_json(root / 'desktop-update-result.json', {})
                        if previous.get('request_id') == update['request_id']:
                            continue
                        try:
                            with hub.lock:
                                inspected = hub.update_status(stop=True)
                                latest = read_json(root / 'desktop-update-request.json', {})
                                if (latest.get('request_id') != update['request_id']
                                        or update.get('expires', 0) <= time.time()):
                                    hub.updating = False
                                    continue
                        except (OSError, ValueError, sqlite3.Error) as error:
                            inspected = dict(ready_for_update=False, detail='无法核查实验状态：' + str(error)[:300])
                        atomic_json(root / 'desktop-update-result.json', dict(inspected,
                            request_id=update['request_id'], status='stopping' if inspected['ready_for_update'] else 'running'))
                        if inspected['ready_for_update']:
                            server.shutdown()
                            return
            monitor = threading.Thread(target=watch, daemon=True)
            monitor.start()
            print('Controller running at http://127.0.0.1:' + str(server.server_address[1]), flush=True)
            try:
                server.serve_forever(poll_interval=0.25)
            finally:
                stopped.set()
                monitor.join(timeout=2)
        finally:
            if server:
                server.server_close()
            hub.close()
            current = read_json(root / 'desktop-process.json', {})
            if current.get('nonce') == nonce:
                root.joinpath('desktop-process.json').unlink(missing_ok=True)
                root.joinpath('desktop-stop.json').unlink(missing_ok=True)


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('controller-start', 'controller-stop', 'controller-status', 'controller-serve',
                                         'controller-open', 'controller-update-status', 'controller-install-status',
                                         'controller-stop-for-update'))
    parser.add_argument('--root', default=str(default_controller_root()))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--host', default='0.0.0.0')
    args = parser.parse_args()
    try:
        release = read_json(Path(__file__).resolve().parents[1] / 'release-role.json', {})
        if release and 'controller' not in release.get('role', ''):
            raise ValueError('This package contains the worker edition. Download the controller edition to manage experiments.')
        if args.action == 'controller-serve':
            return controller_serve(args.root, args.port, args.host)
        if args.action == 'controller-start':
            result = controller_start(args.root, args.port, args.host)
        elif args.action == 'controller-stop':
            result = controller_stop(args.root)
        elif args.action == 'controller-update-status':
            result = controller_update_status(args.root)
        elif args.action == 'controller-install-status':
            result = controller_install_status(args.root)
        elif args.action == 'controller-stop-for-update':
            result = controller_stop_for_update(args.root)
        elif args.action == 'controller-open':
            result = controller_start(args.root, args.port, args.host)
            if result.get('responsive'):
                from .launcher import browser_url
                result['url'] = browser_url(args.root, result['port'])
        else:
            result = controller_status(args.root)
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({'running': False, 'status': 'error', 'detail': str(error)}, ensure_ascii=False))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
