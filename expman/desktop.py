"""Local desktop controller lifecycle. No shell command or algorithm is imported."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from .common import atomic_json, read_json
from .launcher import InstanceLock, default_controller_root


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
    result = {'running': False, 'status': 'stopped', 'root': str(root), 'port': port,
              'log_path': str(root / 'desktop.log'), 'managed': bool(state.get('nonce'))}
    if not isinstance(port, int) or not hub.get('admin_token'):
        return result
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{port}/api/state',
                                         headers={'Authorization': 'Bearer ' + hub['admin_token']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=2) as response:
            value = json.load(response)
        # The request may have waited for startup. The owner file is written
        # before serve_forever, so refresh it after the successful health probe.
        state = read_json(root / 'desktop-process.json', {})
        result['managed'] = bool(state.get('nonce'))
        result.update(running=True, status='running', version=value.get('version'),
            nodes=len(value.get('nodes', [])), jobs=len(value.get('jobs', [])),
            detail='主控运行中；关闭应用窗口后继续在后台运行')
    except (OSError, ValueError):
        result['detail'] = '主控尚未启动，或仍在启动中'
    return result


def controller_start(root, port=8765, host='0.0.0.0'):
    root = Path(root).expanduser().resolve()
    if controller_status(root)['running']:
        return controller_status(root)
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
            if status['running']:
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
            atomic_json(root / 'desktop-process.json', {'pid': os.getpid(), 'nonce': nonce, 'started': time.time()})
            root.joinpath('desktop-pending.json').unlink(missing_ok=True)
            stopped = threading.Event()

            def watch():
                while not stopped.wait(0.25):
                    try:
                        requested = read_json(root / 'desktop-stop.json', {})
                    except (OSError, ValueError):
                        continue
                    if requested.get('nonce') == nonce:
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
    parser.add_argument('action', choices=('controller-start', 'controller-stop', 'controller-status', 'controller-serve', 'controller-open'))
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
        elif args.action == 'controller-open':
            result = controller_start(args.root, args.port, args.host)
            if result.get('running'):
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
