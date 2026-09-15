"""Carry a compatible SASRec source/config/data snapshot to another paired worker.

The transfer does not contain a sender's node configuration, credentials, Git
history, old results or Docker image. Installation uses the recipient's config.
"""
import argparse
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import zipfile

from .common import read_json
from .project_launchers import FILES as LAUNCHERS
from .sasrec_setup import digest, encoded, import_project, inspect_project, local_path

LIMIT = 512 * 1024**2


def export_package(project, research_config, output, name=None):
    sources, data, params, research = inspect_project(project, research_config)
    name = name or 'sasrec-' + params['dataset'].lower().replace('_', '-')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise ValueError('Invalid project name')
    payload = {'project/' + path: value for path, value in sources.items()}
    payload.update({'project/data/' + path: value for path, value in data.items()})
    # Relocate paths in the COPY, keeping all scientific parameters unchanged.
    research = dict(research, data_root='../data', output_root='output')
    payload['project/SASRec_Original/config/research.json'] = encoded(research)
    manifest = {'schema': 1, 'kind': 'sasrec-project', 'name': name,
                'files': {path: {'sha256': digest(value), 'bytes': len(value)}
                          for path, value in sorted(payload.items())}}
    manifest['bundle_id'] = digest(encoded(manifest))
    buffer = io.BytesIO()
    software = Path(__file__).resolve().parent
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('__main__.py', 'from expman.sasrec_package import main\nmain()\n')
        for path in sorted(software.rglob('*.py')):
            if '__pycache__' not in path.parts:
                if path.is_symlink():
                    raise ValueError('Installer source cannot be a symbolic link')
                bundle.writestr('expman/' + path.relative_to(software).as_posix(), path.read_bytes())
        for path, value in payload.items():
            bundle.writestr(path, value)
        bundle.writestr('project-manifest.json', encoded(manifest))
    target = local_path(output).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('Install-Project.pyz', buffer.getvalue())
        for filename, content in LAUNCHERS.items():
            archive.writestr(filename, content)
        archive.writestr('project-manifest.json', encoded(manifest))
    return {'file': str(target), 'sha256': digest(target.read_bytes()),
            'bundle_id': manifest['bundle_id'], 'name': name,
            'source_files': len(sources), 'data_files': len(data), 'bytes': target.stat().st_size}


def read_payload(bundle_path):
    with zipfile.ZipFile(bundle_path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate bundle paths')
        if archive.getinfo('project-manifest.json').file_size > 1024**2:
            raise ValueError('Manifest too large')
        manifest = json.loads(archive.read('project-manifest.json'))
        identity = dict(manifest)
        bundle_id = identity.pop('bundle_id', None)
        if (manifest.get('schema') != 1 or manifest.get('kind') != 'sasrec-project'
                or bundle_id != digest(encoded(identity))):
            raise ValueError('Unsupported or changed project manifest')
        files = manifest.get('files')
        if not isinstance(files, dict) or not 1 <= len(files) <= 10000:
            raise ValueError('Invalid payload list')
        if set(files) != {name for name in names if name.startswith('project/')}:
            raise ValueError('Payload list mismatch')
        if sum(archive.getinfo(name).file_size for name in files) > 2 * 1024**3:
            raise ValueError('Project payload exceeds 2 GiB')
        payload = {}
        for name, expected in files.items():
            path = PurePosixPath(name)
            info = archive.getinfo(name)
            if (not name.startswith('project/') or '\\' in name or ':' in name or '\x00' in name
                    or path.is_absolute() or '..' in path.parts or path.as_posix() != name
                    or info.file_size > LIMIT or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('Invalid project payload path or size: ' + name)
            value = archive.read(name)
            if expected != {'sha256': digest(value), 'bytes': len(value)}:
                raise ValueError('Project checksum mismatch: ' + name)
            payload[name] = value
    return manifest, payload


def install_package(bundle_path, node_config, profile=None):
    manifest, payload = read_payload(bundle_path)
    node_config = local_path(node_config).resolve()
    with tempfile.TemporaryDirectory(prefix='expman-project-') as folder:
        for name, value in payload.items():
            path = Path(folder) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        return import_project(node_config.parent, Path(folder) / 'project/SASRec_Original',
                              'config/research.json', manifest['name'], profile,
                              node_config=node_config)


def choose_config(value=None):
    if value:
        return local_path(value).resolve()
    home = Path.home()
    default = home / '.local/share/experiment-manager/worker/node.ready.json'
    candidates = ([default] if default.is_file() else [])
    candidates += sorted((home / 'expman-first-run').glob('*/node.ready.json'))
    candidates += sorted((home / 'expman-first-run').glob('*/win2080.ready.json'))
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        print('Worker config / 使用节点配置: ' + str(candidates[0]))
        return candidates[0]
    if candidates:
        print('Several worker configurations found. Choose the one used by your worker launcher.')
        print('找到多份配置，请选当前代理启动命令使用的那份；不要根据编号猜。')
        for index, path in enumerate(candidates, 1):
            print(str(index) + ': ' + str(path))
        choice = input('Configuration number / 配置序号: ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(candidates):
            return candidates[int(choice) - 1]
        raise ValueError('Invalid configuration number')
    value = input('Existing node config file / 代理启动命令中 --config 后的完整路径: ').strip()
    if not value:
        raise ValueError('Pair and configure a worker first / 请先完成算力端安装与配对')
    return local_path(value).resolve()


def check_runtime(config_path, profile=None):
    config = read_json(config_path)
    if not config:
        raise ValueError('Worker configuration not found')
    profiles = [(name, entry) for name, entry in config.get('profiles', {}).items()
                if entry.get('verified') is True and (not profile or profile == name)]
    if len(profiles) != 1:
        raise ValueError('Select one verified environment with --profile; run worker setup if none exists')
    name, environment = profiles[0]
    check = ('import importlib.util; import torch,numpy; '
             'assert importlib.util.find_spec("expman.adapters.sasrec") is not None; '
             'print("Runtime imports OK",torch.__version__,numpy.__version__)')
    result = subprocess.run(['docker', 'run', '--rm', '--pull=never', '--network=none',
                             '--read-only', '--tmpfs', '/tmp:rw,nosuid,size=512m',
                             environment['image'], 'python', '-c', check],
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('Target Docker environment is not ready. Start Docker / check the worker runtime.\n'
                         + result.stderr[-1200:])
    print(result.stdout.strip())
    print('Use target runtime / 使用目标机环境: ' + name)
    return name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    export = sub.add_parser('export')
    export.add_argument('--project', required=True)
    export.add_argument('--config', default='config/video_games_full.json')
    export.add_argument('--output', required=True)
    export.add_argument('--name')
    install = sub.add_parser('install')
    install.add_argument('--node-config')
    install.add_argument('--profile')
    install.add_argument('--bundle', default=sys.argv[0])
    args = parser.parse_args()
    try:
        if args.action == 'export':
            print(json.dumps(export_package(args.project, args.config, args.output, args.name),
                             ensure_ascii=False, indent=2))
        else:
            if sys.platform != 'linux':
                raise ValueError('Run Install-Project.cmd on Windows or use Ubuntu/Linux')
            config = choose_config(args.node_config)
            print('Close the worker after its jobs and uploads finish. / 请先等任务和回传完成，再退出代理窗口。')
            profile = check_runtime(config, args.profile)
            receipt = install_package(args.bundle, config, profile)
            print('INSTALLED / 安装完成: ' + ', '.join(receipt['tasks']))
            print('Restart your usual worker launcher, then refresh the controller and use the short template.')
            print('按原来的方式启动这个算力节点，刷新实验台，在该节点下先选 short 模板。')
            print('Source/data installed locally. No jobs submitted. / 已安装代码和数据，未提交训练。')
    except (ValueError, RuntimeError, OSError, KeyError, zipfile.BadZipFile, EOFError,
            subprocess.TimeoutExpired) as error:
        raise SystemExit('Project setup failed / 项目安装失败: ' + str(error))


if __name__ == '__main__':
    main()
