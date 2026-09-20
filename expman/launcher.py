"""Release entry points. Controller and worker roles remain separate."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
import urllib.parse
import urllib.request
import webbrowser

from .common import atomic_json, read_json
from .hub import Hub, make_server


class InstanceLock:
    def __init__(self, path, *, timeout=0):
        self.path = Path(path)
        self.timeout = max(0, float(timeout))
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.stream.seek(0)
                if not self.stream.read(1):
                    self.stream.write(b'0')
                    self.stream.flush()
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.stream.close()
                    self.stream = None
                    raise RuntimeError('This data directory is already in use / 此数据目录已有程序运行')
                time.sleep(min(0.01, remaining))

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()


def default_controller_root():
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return base / 'ExperimentManager' / 'controller'


def browser_url(root, port):
    token = read_json(Path(root) / 'hub.json')['admin_token']
    # Fragment stays in the local browser: it is never sent in HTTP requests/logs.
    return f'http://127.0.0.1:{port}/#token=' + urllib.parse.quote(token, safe='')


def reopen_controller(root, open_browser=True):
    settings = read_json(Path(root) / 'launcher.json', {})
    config = read_json(Path(root) / 'hub.json', {})
    port = settings.get('port')
    if not isinstance(port, int) or not config.get('admin_token'):
        return False
    request = urllib.request.Request(f'http://127.0.0.1:{port}/api/state',
        headers={'Authorization': 'Bearer ' + config['admin_token']})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            if response.status != 200:
                return False
    except OSError:
        return False
    if open_browser:
        webbrowser.open(browser_url(root, port))
    print(f'Controller already running: http://127.0.0.1:{port}', flush=True)
    return True


def controller(root, port=None, host='0.0.0.0', open_browser=True):
    root = Path(root).expanduser().resolve()
    if reopen_controller(root, open_browser):
        return
    with InstanceLock(root / 'controller.lock'):
        hub = Hub(root)
        server = None
        try:
            saved = read_json(root / 'launcher.json', {})
            preferred = port if port is not None else saved.get('port', 8765)
            for candidate in ([preferred] if port is not None or saved else range(preferred, preferred + 20)):
                try:
                    server = make_server(hub, host, candidate)
                    break
                except OSError:
                    if port is not None or saved or candidate == preferred + 19:
                        raise RuntimeError('Controller port is occupied. Close its previous instance or use --port.') from None
            actual_port = server.server_address[1]
            atomic_json(root / 'launcher.json', {'port': actual_port, 'host': host})
            print(f'Experiment Manager controller / 主控端: http://127.0.0.1:{actual_port}', flush=True)
            print(f'Data / 数据目录: {root}', flush=True)
            print('Keep this window open. Ctrl+C stops the controller. / 保持窗口运行，Ctrl+C 退出。', flush=True)
            if open_browser:
                webbrowser.open(browser_url(root, actual_port))
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            print('Controller stopped. Data saved. / 主控端已停止，数据已保存。', flush=True)
        finally:
            if server:
                server.server_close()
            hub.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='role', required=True)
    center = sub.add_parser('controller')
    center.add_argument('--root', default=str(default_controller_root()))
    center.add_argument('--port', type=int)
    center.add_argument('--host', default='0.0.0.0')
    center.add_argument('--no-browser', action='store_true')
    worker = sub.add_parser('worker')
    worker.add_argument('--pairing')
    worker.add_argument('--root')
    worker.add_argument('--gpu', action='append')
    worker.add_argument('--prepare-only', action='store_true')
    worker.add_argument('--configure', action='store_true', help='Recheck GPUs and regenerate setup before starting')
    args = parser.parse_args()
    release = read_json(Path(__file__).resolve().parents[1] / 'release-role.json')
    if release:
        permitted = 'controller' if 'controller' in release['role'] else 'worker'
        if args.role != permitted:
            raise ValueError(f'This package contains the {permitted} edition. Download the other edition to use both roles.')
    if args.role == 'controller':
        controller(args.root, args.port, args.host, not args.no_browser)
    else:
        from .worker_setup import start
        start(args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        print('Cannot start / 无法启动: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
