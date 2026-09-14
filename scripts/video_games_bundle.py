"""Build/run a self-contained, repeatable Video_Games data and task importer.

The generated .pyz uses only Python's standard library. It imports versioned
data and writes a NEW node configuration; it never changes a running agent's
configuration or submits a training task. --start-agent runs doctor first.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile

if Path(__file__).suffix == '.py':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from expman.common import validate_task

SPLITS = ('train', 'valid', 'test')
NAMES = tuple('Video_Games.' + split + '.txt' for split in SPLITS)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular JSON file: ' + str(path))
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object: ' + str(path))
    return value


def private_file(path, data):
    """Repeatable exact writes; changed files are never silently replaced."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing symbolic link: ' + str(path))
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise ValueError('Existing file differs; keep it and choose a new output: ' + str(path))
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data)


def checked_directory(path):
    path = Path(path).expanduser().absolute()
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Directory must not contain symbolic links: ' + str(part))
    return path


def build(data_root, research_config, task_template, output):
    data_root = checked_directory(data_root)
    config = read_json(research_config)
    if config.get('dataset') != 'Video_Games' or config.get('device') != 'cuda':
        raise ValueError('Expected the original Video_Games CUDA research configuration')
    template = validate_task(read_json(task_template))
    if template['algorithm'] != 'SASRec' or template['backend'] != 'docker':
        raise ValueError('Expected a validated SASRec Docker task as the deployment template')
    payload = {}
    files = {}
    for name in NAMES:
        path = data_root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing regular split: ' + name)
        data = path.read_bytes()
        if not data or len(data) > 512 * 1024 * 1024:
            raise ValueError('Split is empty or exceeds the importer size limit: ' + name)
        payload['data/' + name] = data
        files[name] = {'sha256': digest(data), 'bytes': len(data)}
    asset_id = 'video-games-' + digest(encoded(files))[:16]
    full = {key: copy.deepcopy(template[key]) for key in
            ('algorithm', 'backend', 'source', 'command', 'environments', 'tags')}
    full.update(name='SASRec-Video_Games-formal-seed' + str(config['seed']),
                group='sasrec-video-formal-v1', metric_protocol='external_sasrec_original_v1',
                assets=[asset_id], priority=0,
                resources={'gpu_memory_mb': 6000, 'cpu': 2, 'ram_mb': 8192, 'exclusive': True})
    full['params'] = {key: copy.deepcopy(value) for key, value in config.items()
                      if key not in ('data_root', 'output_root')}
    full['params'].update(data_asset=asset_id, torch_threads=2)
    full = validate_task(full)
    short = copy.deepcopy(full)
    short.update(name='SASRec-Video_Games-3epoch-check', group='sasrec-video-acceptance')
    short['params'].update(epochs=3, star_test=-1)
    short = validate_task(short)
    payload['tasks/short.task.json'] = encoded(short)
    payload['tasks/formal.task.json'] = encoded(full)
    payload['research-config.json'] = encoded(config)
    manifest = {'schema': 1, 'dataset': 'Video_Games', 'asset_id': asset_id,
                'data_files': files,
                'payload': {name: {'sha256': digest(data), 'bytes': len(data)}
                            for name, data in payload.items()},
                'purpose': 'Import real splits; preserve original formal parameters; no automatic submission.'}
    manifest['bundle_id'] = digest(encoded(manifest))[:16]
    software = Path(__file__).resolve().parents[1]
    path = Path(output).absolute()
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as package:
        package.writestr('__main__.py', Path(__file__).read_bytes())
        for relative in ('expman/__init__.py', 'expman/common.py'):
            package.writestr(relative, (software / relative).read_bytes())
        for name, data in payload.items():
            package.writestr(name, data)
        package.writestr('manifest.json', encoded(manifest))
    for name, task in (('short', short), ('formal', full)):
        private_file(path.with_name(path.stem + '.' + name + '.task.json'), encoded(task))
    return path, manifest


def load_bundle(bundle):
    with zipfile.ZipFile(bundle) as package:
        names = package.namelist()
        required = {'__main__.py', 'expman/__init__.py', 'expman/common.py', 'manifest.json',
                    'research-config.json', 'tasks/short.task.json', 'tasks/formal.task.json'}
        required.update('data/' + name for name in NAMES)
        if len(names) != len(set(names)) or set(names) != required:
            raise ValueError('Bundle contains unexpected, duplicate or missing paths')
        if any(item.file_size > 512 * 1024 * 1024 or
               (item.external_attr >> 16) & 0o170000 == 0o120000 for item in package.infolist()):
            raise ValueError('Bundle contains an oversized entry or symbolic link')
        if package.getinfo('manifest.json').file_size > 64 * 1024:
            raise ValueError('Manifest is too large')
        manifest = json.loads(package.read('manifest.json'))
        if manifest.get('schema') != 1 or manifest.get('dataset') != 'Video_Games':
            raise ValueError('Unsupported bundle manifest')
        identity = dict(manifest)
        bundle_id = identity.pop('bundle_id', None)
        if bundle_id != digest(encoded(identity))[:16]:
            raise ValueError('Bundle identity mismatch')
        asset_id = manifest.get('asset_id', '')
        if not re.fullmatch(r'video-games-[0-9a-f]{16}', asset_id):
            raise ValueError('Invalid data asset ID')
        expected = required - {'__main__.py', 'expman/__init__.py', 'expman/common.py', 'manifest.json'}
        if set(manifest.get('payload', {})) != expected or set(manifest.get('data_files', {})) != set(NAMES):
            raise ValueError('Manifest file list mismatch')
        payload = {}
        for name in expected:
            data = package.read(name)
            info = manifest['payload'][name]
            if info != {'sha256': digest(data), 'bytes': len(data)}:
                raise ValueError('Bundle checksum mismatch: ' + name)
            payload[name] = data
        for name in NAMES:
            if manifest['data_files'][name] != manifest['payload']['data/' + name]:
                raise ValueError('Data identity mismatch: ' + name)
        if asset_id != 'video-games-' + digest(encoded(manifest['data_files']))[:16]:
            raise ValueError('Data asset fingerprint mismatch')
    return manifest, payload


def prepare(bundle, config_path, home=None):
    manifest, payload = load_bundle(bundle)
    original = read_json(config_path)
    for name in ('root', 'node_id', 'hub_url', 'token'):
        if not isinstance(original.get(name), str) or not original[name]:
            raise ValueError('Node configuration is missing ' + name)
    tasks = {name: validate_task(json.loads(payload['tasks/' + name + '.task.json']))
             for name in ('short', 'formal')}
    for task in tasks.values():
        if task['source']['repo'] not in original.get('allowed_repos', []):
            raise ValueError('The bundle source repository is not registered on this node')
        if not set(task['tags']).issubset(original.get('tags', [])):
            raise ValueError('The bundle is intended for a different node')
        if not any(original.get('profiles', {}).get(env['profile'], {}).get('image') == env['image']
                   and original['profiles'][env['profile']].get('verified') is True
                   for env in task['environments']):
            raise ValueError('The exact task image has not been verified on this node')
    home = checked_directory(home or Path.home())
    asset = checked_directory(home / 'ExperimentResources' / 'datasets' / manifest['asset_id'])
    output = checked_directory(home / 'expman-first-run' / ('video-games-' + manifest['bundle_id']))
    config = copy.deepcopy(original)
    config.setdefault('assets', {})[manifest['asset_id']] = str(asset)
    files = {output / 'node.ready.json': encoded(config),
             output / 'short.task.json': encoded(tasks['short']),
             output / 'formal.task.json': encoded(tasks['formal']),
             output / 'import-record.json': encoded({'bundle_id': manifest['bundle_id'],
                 'asset_id': manifest['asset_id'], 'data_files': manifest['data_files'],
                 'original_config': str(Path(config_path).absolute()), 'original_config_unchanged': True})}
    files.update({asset / 'Video_Games' / name: payload['data/' + name] for name in NAMES})
    # Verify every existing destination before writing any new content.
    for path, data in files.items():
        checked_directory(path.parent)
        if path.is_symlink() or path.exists() and (not path.is_file() or path.read_bytes() != data):
            raise ValueError('Existing destination differs; refusing to overwrite: ' + str(path))
    # Publish the ready config last, after all referenced inputs are present.
    ready = output / 'node.ready.json'
    for path, data in sorted(files.items(), key=lambda item: item[0] == ready):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        private_file(path, data)
    return output


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    if Path(sys.argv[0]).suffix == '.pyz':
        parser.add_argument('--config', required=True, help='Existing working node configuration')
        parser.add_argument('--software', help='Existing experiment_manager directory (required with --start-agent)')
        parser.add_argument('--start-agent', action='store_true', help='After import, run doctor and start the agent')
        args = parser.parse_args()
        if sys.platform != 'linux':
            raise ValueError('请在训练节点的 Ubuntu/Linux 中运行这个导入包。')
        if args.start_agent and not args.software:
            raise ValueError('--start-agent requires --software')
        software = checked_directory(args.software) if args.software else None
        if software and not (software / 'expman' / 'agent.py').is_file():
            raise ValueError('找不到节点代理源码，请检查 --software 路径。')
        output = prepare(sys.argv[0], args.config)
        print('真实数据校验和导入通过。原节点配置保留。', flush=True)
        print('新配置：', output / 'node.ready.json', flush=True)
        print('三轮检查任务：', output / 'short.task.json', flush=True)
        print('正式任务：', output / 'formal.task.json', flush=True)
        if args.start_agent:
            env = dict(os.environ, PYTHONPATH=str(software), PYTHONUNBUFFERED='1')
            result = subprocess.run([sys.executable, '-m', 'expman', 'doctor', '--config', str(output / 'node.ready.json')],
                                    cwd=software, env=env, check=False)
            if result.returncode:
                raise ValueError('自检未通过，尚未启动代理。数据和配置已保留，可修复后重复执行。')
            print('自检通过，启动接单员；保持此窗口运行。', flush=True)
            os.chdir(software)
            os.execve(sys.executable, [sys.executable, '-m', 'expman', 'agent', '--config', str(output / 'node.ready.json')], env)
    else:
        parser.add_argument('--data-root', required=True)
        parser.add_argument('--research-config', required=True)
        parser.add_argument('--task-template', required=True)
        parser.add_argument('--output', required=True)
        args = parser.parse_args()
        path, manifest = build(args.data_root, args.research_config, args.task_template, args.output)
        print('Created:', path)
        print('SHA256:', digest(path.read_bytes()))
        print('Asset:', manifest['asset_id'])
        print('Bundle:', manifest['bundle_id'])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        print('Video_Games setup error:', error, file=sys.stderr)
        raise SystemExit(1)
