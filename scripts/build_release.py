"""Build three public ZIP assets and two native Windows installers.

No node credentials, databases, datasets, checkpoints or local deployment diaries
are inputs. The controller includes the official Windows embeddable CPython.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import urllib.request
import uuid
import zipfile

try:
    from scripts.build_desktop import compile_desktop, find_compiler
except ModuleNotFoundError:
    from build_desktop import compile_desktop, find_compiler

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
PYTHON_VERSION = '3.13.15'
PYTHON_URL = f'https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip'
PYTHON_SHA256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'
ROLES = {
    'windows-controller-x64': ('Start-Controller.cmd', 'Allow-Worker-Connections.cmd', 'Allow-Worker-Connections.ps1', 'Import-Algorithm.cmd'),
    'windows-worker-x64': ('Client-Worker.sh', 'Install-Worker.sh', 'Start-Background-Worker.sh', 'Stop-Worker.sh', 'Worker-Status.sh', 'Start-Worker.cmd', 'Start-Worker.ps1', 'Start-Worker.sh', 'Configure-Project.cmd', 'Configure-Project.sh', 'Update-Worker.cmd', 'Update-Worker.ps1', 'Update-Worker.sh'),
    'ubuntu-worker-x64': ('Install-Worker.sh', 'Client-Worker.sh', 'Start-Background-Worker.sh', 'Stop-Worker.sh', 'Worker-Status.sh', 'Start-Worker.sh', 'Configure-Project.sh', 'Import-Algorithm.sh', 'Update-Worker.sh'),
}

NATIVE_ENTRIES = {'windows-controller-x64': 'ExperimentCenter.exe',
                  'windows-worker-x64': 'ExperimentWorker.exe'}


def application_files(root=ROOT):
    files = {}
    for path in sorted((root / 'expman').rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py', '.js', '.css', '.html'):
            if path.is_symlink():
                raise ValueError('Release inputs cannot be symlinks')
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in ('README.md', 'README.zh-CN.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRACT.md', 'docs/OPERATIONS.md', 'docs/OPERATIONS.zh-CN.md', 'docs/ALGORITHM-INTEGRATION.md', 'docs/ALGORITHM-INTEGRATION.zh-CN.md', 'docs/SASREC-ADAPTATION-WALKTHROUGH.zh-CN.md', 'docs/EXTERNAL-HARNESS.md', 'docs/EXTERNAL-HARNESS.zh-CN.md', 'docs/RELEASE-0.3.0-rc.1.md'):
        files[name] = (root / name).read_bytes()
    files['expman/static/favicon.ico'] = (root / 'expman/static/favicon.ico').read_bytes()
    files['docs/RELEASE-0.3.0-rc.2.md'] = (root / 'docs/RELEASE-0.3.0-rc.2.md').read_bytes()
    files['docs/EXPERIMENT-MATRICES.zh-CN.md'] = (root / 'docs/EXPERIMENT-MATRICES.zh-CN.md').read_bytes()
    release_notes = 'docs/RELEASE-' + VERSION + '.md'
    files[release_notes] = (root / release_notes).read_bytes()
    for name in ('examples/managed-project/train.py', 'examples/managed-project/expman_entry.py',
                 'examples/managed-project/check_local.py', 'examples/managed-project/example-data/train.csv',
                 'examples/sasrec-adaptation/managed_sasrec.py', 'examples/sasrec-adaptation/check_lesson.py'):
        files[name] = (root / name).read_bytes()
    return files


def python_runtime(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != PYTHON_SHA256:
        raise ValueError('Embedded Python archive hash does not match the official release')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or any('/' in n or '\\' in n or n in ('.', '..') or ':' in n for n in names):
            raise ValueError('Unexpected embedded Python archive paths')
        files = {'runtime/' + name: archive.read(name) for name in names}
    if 'runtime/LICENSE.txt' not in files or 'runtime/python.exe' not in files:
        raise ValueError('Incomplete embedded Python distribution')
    # Embedded Python otherwise ignores the application folder and PYTHONPATH.
    files['runtime/python313._pth'] = b'python313.zip\n.\n..\n'
    return files


def write_zip(path, files):
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            item = zipfile.ZipInfo(name, date_time=(2026, 9, 15, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            archive.writestr(item, data)


def build(output, python_zip, compiler=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    common = application_files()
    runtime = python_runtime(python_zip)
    compiler = find_compiler(compiler)
    hashes = []
    for role, entries in ROLES.items():
        files = dict(common)
        # Physical role marker is enforced by the launch module, even via the CLI.
        files['release-role.json'] = (json.dumps({'version': VERSION, 'role': role}) + '\n').encode()
        for name in entries:
            files[name] = (ROOT / 'deploy/release' / name).read_bytes()
        if role == 'windows-controller-x64':
            files.update(runtime)
        entry = NATIVE_ENTRIES.get(role, entries[0])
        if role in NATIVE_ENTRIES:
            # Compile in a unique scratch directory, never include build inputs.
            temporary = output / ('.release-desktop-' + uuid.uuid4().hex)
            temporary.mkdir()
            try:
                native = compile_desktop(temporary / entry,
                                         'controller' if 'controller' in role else 'worker', compiler=compiler)
                files[entry] = native.read_bytes()
            finally:
                if temporary.parent.resolve() != output.resolve() or not temporary.name.startswith('.release-desktop-'):
                    raise ValueError('Unsafe release build cleanup path')
                shutil.rmtree(temporary)
        files['START-HERE.txt'] = (
            f'Experiment Manager {VERSION} / {role}\n\n'
            f'Extract the whole ZIP. Entry point: {entry}\n'
            f'请先完整解压。启动入口：{entry}\n\n'
            'Windows controller: Python is included; no Docker/WSL required.\n'
            'Windows worker: install WSL2 Ubuntu, Docker Desktop and NVIDIA driver first.\n'
            'Ubuntu worker: install Docker Engine, NVIDIA driver and Container Toolkit first.\n'
            'Windows: double-click the EXE. The app keeps running in the tray when its window closes.\n'
            'Windows：双击 EXE 启动；关闭窗口后软件继续在托盘运行。\n'
            'Windows worker: choose the credential exported by the controller, then click Start.\n'
            'Windows 算力端：选择管理端导出的凭证文件，然后点击启动。\n'
            'Linux: bash Install-Worker.sh --pairing /path/to/node.pairing.json (normal user, no sudo).\n'
            'Linux：用普通用户运行上面的安装命令，软件自动配置并在后台运行。\n'
            'Check progress: bash Worker-Status.sh ; logs: bash Client-Worker.sh logs\n'
            'Linux user services start with the user session; running after logout/reboot depends on linger/WSL startup.\n'
            'Linux 用户服务随用户会话启动；注销、重启后的运行取决于 linger/WSL 启动。\n'
            'Existing worker: select its old config to retain identity, policies and experiment state.\n'
            '旧算力端：选择原配置可继续使用原身份、资源策略和实验记录。\n'
            'Legacy CMD/shell launchers remain available for compatibility.\n'
            'Read README.md (English) / README.zh-CN.md（中文） for complete instructions.\n'
        ).encode('utf-8')
        manifest = {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)} for name, data in files.items()}
        files['manifest.json'] = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
        target = output / f'ExperimentManager-{VERSION}-{role}.zip'
        write_zip(target, files)
        hashes.append(f'{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}')
        print(f'Built: {target.name} ({target.stat().st_size:,} bytes)')
        if role in NATIVE_ENTRIES:
            installer = output / f'ExperimentManager-{VERSION}-{role}-Setup.exe'
            compile_desktop(installer, 'controller' if 'controller' in role else 'worker',
                            payload=target, compiler=compiler)
            hashes.append(f'{hashlib.sha256(installer.read_bytes()).hexdigest()}  {installer.name}')
            print(f'Built: {installer.name} ({installer.stat().st_size:,} bytes)')
    (output / 'SHA256SUMS.txt').write_text('\n'.join(hashes) + '\n', encoding='utf-8')
    (output / 'build-info.json').write_text(json.dumps({'version': VERSION, 'python_version': PYTHON_VERSION,
        'python_source': PYTHON_URL, 'python_archive_sha256': PYTHON_SHA256,
        'native_windows_apps': True, 'windows_architecture': 'x64',
        'windows_code_signed': False, 'artifact_count': len(hashes)}, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='dist/' + VERSION)
    parser.add_argument('--python-zip', default='.runtime/build-cache/python-embed.zip')
    parser.add_argument('--download-python', action='store_true')
    parser.add_argument('--compiler', help='Path to the Windows .NET Framework C# compiler')
    args = parser.parse_args()
    runtime = Path(args.python_zip)
    if not runtime.exists() and args.download_python:
        runtime.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(PYTHON_URL, timeout=120) as response:
            data = response.read(32 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != PYTHON_SHA256:
            raise ValueError('Official Python download failed checksum validation')
        runtime.write_bytes(data)
    build(args.output, runtime, args.compiler)


if __name__ == '__main__':
    main()
