"""Prepare, transport and install external harness projects without editing source."""
from __future__ import annotations

import argparse
import base64
import copy
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import uuid
import zipfile

from . import common
from .harness import validate_manifest
from .harness_discovery import discover_project
from .launcher import InstanceLock


MAX_BYTES = 32 * 1024**3
MAX_FILES = 20000
MANIFEST = 'project-manifest.json'
_SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}')


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + '\n').encode('utf-8')


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _slug(value):
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError('Project/experiment/asset ID needs 1-64 letters, digits, _, . or -')
    return value


def _local(value):
    return Path(value).expanduser().resolve()


def _path(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value or '\0' in value
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/'))):
        raise ValueError('Unsafe project path: ' + str(value))
    # Portable packages must remain safe when read or inspected on Windows too.
    for part in value.split('/'):
        if part.endswith((' ', '.')) or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?', part, re.I):
            raise ValueError('Nonportable project path: ' + value)
    return value


def _matches(path, patterns):
    return any(fnmatch.fnmatchcase(path, pattern) or
               (pattern.startswith('**/') and fnmatch.fnmatchcase(path, pattern[3:])) for pattern in patterns)


def _files(root, include, excluded):
    if not root.is_dir() or root.is_symlink():
        raise ValueError('Expected a regular source/asset directory: ' + str(root))
    excluded = {value.lower() for value in excluded}
    for base, directories, files in os.walk(root, followlinks=False):
        base = Path(base)
        directories[:] = sorted(name for name in directories if name.lower() not in excluded)
        for name in directories + files:
            child = base / name
            if child.is_symlink() or not child.resolve().is_relative_to(root):
                raise ValueError('Project contains a link outside its snapshot: ' + str(child))
        for name in sorted(files):
            child = base / name
            relative = child.relative_to(root).as_posix()
            if _matches(relative, include):
                if not stat.S_ISREG(child.stat().st_mode):
                    raise ValueError('Special files cannot be bundled')
                yield _path(relative), child


def prepare_project(source, output, entry=None, project_id=None):
    """Create a reviewable draft outside the original project. Never overwrite edits."""
    source, output = _local(source), _local(output)
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Choose a separate external configuration folder, outside the algorithm source')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Configuration folder already exists; edit it or choose another folder')
    report = discover_project(source, entry)
    if not report['selected_entry']:
        raise ValueError('No entry found. Specify --entry with the original entry file')
    project_id = _slug(project_id or re.sub(r'[^A-Za-z0-9_.-]', '-', source.name).strip('-')[:64] or 'algorithm')
    harness = {'schema_version': 1, 'name': source.name, 'command': report['command'],
        'cwd': report['cwd'], 'fixed_params': {}, 'parameters': {}, 'bindings': [],
        'environment': {'PYTHONUNBUFFERED': '1'}, 'config_files': [], 'metrics': [],
        'resume': {'supported': False}}
    assets, params, review = {}, {}, []
    types = {'str': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean'}
    trailing = {hint['dest'] for hint in report['path_hints'] if hint.get('trailing_separator')}
    for argument in report['arguments']:
        name, action = argument['dest'], argument['action']
        if (argument['dynamic_fields'] or action not in ('store', 'store_true', 'store_false')
                or argument.get('nargs') is not None or not argument['flags'][0].startswith('-')
                or argument['type'] not in types):
            review.append('Configure argument manually: ' + name)
            continue
        default = argument['default']
        spec = {'type': types[argument['type']], 'required': argument['required']}
        if argument['default_provided'] and default is not None:
            spec['default'] = default
        if argument['choices'] is not None:
            spec['choices'] = argument['choices']
        if argument['help']:
            spec['description'] = argument['help']
        fixed = None
        role = argument['inferred_role']
        if report['gpu_visibility'].get('argument') == name:
            fixed = '{env.CUDA_VISIBLE_DEVICES}'
        elif role == 'output_path':
            fixed = '{output}/outputs/' + ('' if 'dir' in name or 'root' in name else name)
        elif role == 'data_path' and isinstance(default, str):
            data = (source / report['cwd'] / default).resolve()
            if data.is_dir():
                alias = 'dataset' if not assets else _slug(name)
                assets[alias] = {'path': str(data), 'include': ['**/*']}
                fixed = '{assets.' + alias + '}' + ('/' if name in trailing else '')
            elif data.is_file():
                alias = 'dataset' if not assets else _slug(name)
                assets[alias] = {'path': str(data.parent), 'include': [data.name]}
                fixed = '{assets.' + alias + '}/' + data.name
            else:
                review.append('Set the dataset path for: ' + name)
        if fixed is not None:
            harness['fixed_params'][name] = fixed
        else:
            harness['parameters'][name] = spec
            if 'default' in spec:
                params[name] = spec['default']
            if spec['required'] and 'default' not in spec:
                review.append('Supply required parameter: ' + name)
        harness['bindings'].append({'param': name, 'flag': next(
            (flag for flag in argument['flags'] if flag.startswith('--')), argument['flags'][0]),
            'mode': 'value' if action == 'store' else action})
    if report['entries'][0]['kind'] == 'windows-batch':
        review.append('Select the equivalent Python/bash entry for Linux GPU containers')
    project = {'schema_version': 1, 'project_id': project_id, 'name': source.name,
        'source': str(source), 'include': report['source_policy']['include'],
        'exclude_directories': report['source_policy']['exclude_directories'], 'assets': assets,
        'runtime': {'imports': report['dependency_hints'], 'requirements': []},
        'resources': {'gpu_memory_mb': 4096, 'cpu': 2, 'ram_mb': 4096, 'exclusive': False},
        'reviewed': False, 'review_notes': review + report['warnings']}
    output.mkdir(parents=True, exist_ok=True)
    common.atomic_json(output / 'project.json', project)
    common.atomic_json(output / 'harness.json', validate_manifest(harness))
    common.atomic_json(output / 'experiments/default.json', {'id': 'default', 'name': 'Default', 'params': params})
    common.atomic_json(output / 'discovery.json', report)
    return {'directory': str(output), 'entry': report['selected_entry'],
            'parameters': len(harness['parameters']), 'review_notes': project['review_notes']}


def build_project(project, output):
    project, output = _local(project), _local(output)
    config = common.read_json(project / 'project.json')
    if not config or config.get('schema_version') != 1:
        raise ValueError('Missing project.json; prepare an external configuration first')
    if config.get('reviewed') is not True:
        raise ValueError('Review project.json, harness.json and experiments, then set project.json reviewed to true')
    harness = validate_manifest(common.read_json(project / 'harness.json'))
    if harness['command'][0].lower() in ('cmd', 'cmd.exe'):
        raise ValueError('Windows batch cannot run in Linux GPU containers; select the original Python/bash entry')
    experiments = [common.read_json(path) for path in sorted((project / 'experiments').glob('*.json'))]
    if not experiments or len(experiments) > 100:
        raise ValueError('Provide 1-100 separate experiment JSON files')
    seen = set()
    for experiment in experiments:
        key = _slug(experiment.get('id'))
        if key in seen or not isinstance(experiment.get('params'), dict):
            raise ValueError('Each experiment needs a unique id and params object')
        seen.add(key)
        # Validate types and fixed overrides before a bundle is dispatched.
        from .harness import _parameters
        placeholders = {'python': 'python', 'workspace': '/workspace', 'output': '/output',
            'assets': {alias: '/assets/' + alias for alias in config.get('assets', {})},
            'env': {'CUDA_VISIBLE_DEVICES': 'GPU-assigned'}}
        _parameters(harness, experiment['params'], placeholders)
    payload = {}
    source = _local(config['source'])
    if output.is_relative_to(source):
        raise ValueError('The bundle must be written outside the original source')
    for relative, path in _files(source, config['include'], config['exclude_directories']):
        payload['source/' + relative] = path
    if not payload:
        raise ValueError('Source selection is empty')
    assets = {}
    for alias, asset in config.get('assets', {}).items():
        _slug(alias)
        selected = list(_files(_local(asset['path']), asset.get('include', ['**/*']),
                               ['.git', '__pycache__', '.venv']))
        if not selected:
            raise ValueError('Asset selection is empty: ' + alias)
        assets[alias] = {'files': len(selected)}
        for relative, path in selected:
            payload['assets/' + alias + '/' + relative] = path
    for name in ('harness.py', 'sdk.py'):
        payload['runtime/' + name] = Path(__file__).with_name(name)
    if len(payload) > MAX_FILES or sum(path.stat().st_size for path in payload.values()) > MAX_BYTES:
        raise ValueError('Project is too large; maximum 20,000 files / 32 GiB')
    files = {name: {'sha256': common.sha256_file(path), 'bytes': path.stat().st_size,
                   'executable': bool(path.stat().st_mode & 0o111)} for name, path in sorted(payload.items())}
    manifest = {'schema': 1, 'kind': 'external-harness-project',
        'project_id': _slug(config['project_id']), 'name': config['name'], 'harness': harness,
        'experiments': experiments, 'assets': assets, 'files': files,
        'runtime': config.get('runtime', {}), 'resources': config['resources']}
    manifest['bundle_id'] = _digest(_encoded(manifest))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        archive.writestr(MANIFEST, _encoded(manifest))
        for name, path in payload.items():
            archive.write(path, name)
    try:
        read_bundle(output)  # Detect source changes during snapshot creation.
    except Exception:
        output.unlink()
        raise
    return {'file': str(output), 'sha256': common.sha256_file(output),
        'bundle_id': manifest['bundle_id'], 'project_id': manifest['project_id'],
        'bytes': output.stat().st_size, 'files': len(files), 'experiments': [e['id'] for e in experiments]}


def read_bundle(bundle):
    """Validate all archive members and hashes before installation or publication."""
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) > MAX_FILES + 1 or len({name.casefold() for name in names}) != len(names):
            raise ValueError('Duplicate paths or too many project files')
        if MANIFEST not in names or archive.getinfo(MANIFEST).file_size > 4 * 1024**2:
            raise ValueError('Missing or oversized project manifest')
        manifest = json.loads(archive.read(MANIFEST))
        if not isinstance(manifest, dict):
            raise ValueError('Project manifest must be an object')
        identity = dict(manifest)
        bundle_id = identity.pop('bundle_id', None)
        if (manifest.get('schema') != 1 or manifest.get('kind') != 'external-harness-project'
                or bundle_id != _digest(_encoded(identity))):
            raise ValueError('Unsupported or changed project identity')
        _slug(manifest['project_id'])
        if not isinstance(manifest.get('name'), str) or not 1 <= len(manifest['name']) <= 100:
            raise ValueError('Invalid project display name')
        validate_manifest(manifest.get('harness'))
        from .harness_environment import validate_runtime
        validate_runtime(manifest.get('runtime', {}))
        files = manifest.get('files')
        if not isinstance(files, dict) or set(files) != set(names) - {MANIFEST}:
            raise ValueError('Project file manifest mismatch')
        if not {'runtime/harness.py', 'runtime/sdk.py'} <= set(files):
            raise ValueError('Project is missing its external runtime')
        if sum(info.file_size for info in infos) > MAX_BYTES:
            raise ValueError('Project exceeds 32 GiB')
        experiments = manifest.get('experiments')
        if not isinstance(experiments, list) or not 1 <= len(experiments) <= 100:
            raise ValueError('Invalid experiment presets')
        seen = set()
        for experiment in experiments:
            if not isinstance(experiment, dict):
                raise ValueError('Experiment preset must be an object')
            name = _slug(experiment.get('id'))
            if name in seen or not isinstance(experiment.get('params'), dict):
                raise ValueError('Invalid or duplicate experiment preset')
            seen.add(name)
        if not isinstance(manifest.get('assets', {}), dict):
            raise ValueError('Assets must be an object')
        for alias in manifest.get('assets', {}):
            _slug(alias)
        case_paths = {name.casefold() for name in files}
        for name, expected in files.items():
            _path(name)
            if any('/'.join(name.split('/')[:index]).casefold() in case_paths
                   for index in range(1, len(name.split('/')))):
                raise ValueError('A file conflicts with a directory in the project')
            if any(part.casefold() == '.git' for part in name.split('/')):
                raise ValueError('Git metadata cannot be included in a project snapshot')
            if not name.startswith(('source/', 'assets/', 'runtime/')):
                raise ValueError('Unknown project payload: ' + name)
            if name.startswith('assets/') and name.split('/')[1] not in manifest.get('assets', {}):
                raise ValueError('Undeclared asset payload')
            info = archive.getinfo(name)
            mode = (info.external_attr >> 16) & 0o170000
            if info.is_dir() or mode not in (0, stat.S_IFREG) or info.flag_bits & 1:
                raise ValueError('Links, special files and encrypted entries are unsupported')
            if not isinstance(expected, dict) or expected.get('bytes') != info.file_size:
                raise ValueError('Project file size mismatch: ' + name)
            digest = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if digest.hexdigest() != expected.get('sha256'):
                raise ValueError('Project checksum mismatch: ' + name)
    return manifest


def _exact(path, content):
    if path.is_symlink():
        raise ValueError('Installed snapshot cannot contain symbolic links')
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError('Installed immutable snapshot changed: ' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(content)


def _git(repo, *args):
    result = subprocess.run(['git', '-c', 'safe.directory=' + repo.as_posix(), '-C', str(repo), *args],
        capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError('Could not create the private code snapshot: ' + result.stderr[-1000:])
    return result.stdout.strip()


def bundle_asset_aliases(manifest):
    """Stable asset identifiers shared by every deployment of a bundle."""
    return {alias: 'hdata-' + manifest['project_id'][:30] + '-' + alias[:20] + '-' +
            _digest(_encoded({name: value for name, value in manifest['files'].items()
                             if name.startswith('assets/' + alias + '/')}))[:16]
            for alias in manifest.get('assets', {})}


def install_bundle(bundle, storage_root, node_config):
    """Install a content-addressed copy; return config without touching a live agent."""
    manifest = read_bundle(bundle)
    storage_root = _local(storage_root)
    storage_root.mkdir(parents=True, exist_ok=True)
    with InstanceLock(storage_root / 'install.lock'):
        root = common.safe_child(storage_root, manifest['project_id'] + '/' + manifest['bundle_id'])
        repo = common.safe_child(root, 'repository')
        aliases = bundle_asset_aliases(manifest)
        # Durable ownership receipt also covers an interrupted installation.
        root.mkdir(parents=True, exist_ok=True)
        receipt = {'schema': 1, 'project_id': manifest['project_id'], 'bundle_id': manifest['bundle_id'],
                   'assets': list(aliases.values()), 'profiles': {}}
        receipt_path = root / '.expman-install.json'
        previous_receipt = common.read_json(receipt_path)
        if previous_receipt and (previous_receipt.get('project_id'), previous_receipt.get('bundle_id')) != (manifest['project_id'], manifest['bundle_id']):
            raise ValueError('Installed project ownership receipt does not match')
        common.atomic_json(receipt_path, previous_receipt or receipt)
        with zipfile.ZipFile(bundle) as archive:
            for name, expected in manifest['files'].items():
                if name.startswith('source/'):
                    target = common.safe_child(repo / 'project', name[len('source/'):])
                elif name.startswith('runtime/'):
                    target = common.safe_child(repo / '.expman', name)
                else:
                    _, alias, relative = name.split('/', 2)
                    target = common.safe_child(storage_root / 'assets' / aliases[alias], relative)
                if target.exists():
                    if target.is_symlink() or common.sha256_file(target) != expected['sha256']:
                        raise ValueError('Installed immutable file changed: ' + str(target))
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                    target.chmod(0o755 if expected.get('executable') else 0o644)
        def relocate(value):
            if isinstance(value, str):
                for alias, asset_id in aliases.items():
                    value = value.replace('{assets.' + alias + '}', '{assets.' + asset_id + '}')
                return value
            if isinstance(value, dict):
                return {key: relocate(item) for key, item in value.items()}
            if isinstance(value, list):
                return [relocate(item) for item in value]
            return value
        _exact(repo / '.expman/harness.json', _encoded(relocate(manifest['harness'])))
        for experiment in manifest['experiments']:
            _exact(repo / '.expman/experiments' / (experiment['id'] + '.json'), _encoded(experiment))
        if not (repo / '.git').exists():
            _git(repo, 'init', '--quiet')
            _git(repo, 'config', 'core.autocrlf', 'false')
        if _git(repo, 'status', '--porcelain'):
            # Only a new, owned snapshot may be committed. Never accept edits to a previous commit.
            head = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'],
                                  capture_output=True, text=True)
            if head.returncode == 0:
                raise ValueError('Installed snapshot has uncommitted changes')
            _git(repo, 'add', '--all')
            _git(repo, '-c', 'user.name=Experiment Manager', '-c', 'user.email=snapshot@localhost',
                 '-c', 'commit.gpgsign=false', 'commit', '--quiet', '-m', 'External project snapshot')
        commit = _git(repo, 'rev-parse', 'HEAD')
        from .harness_environment import ensure_environment
        environment = ensure_environment(manifest.get('runtime', {}), copy.deepcopy(node_config), root / 'environment')
        config = environment['config']
        config['deleted_project_bundles'] = [item for item in config.get('deleted_project_bundles', [])
                                             if item != manifest['bundle_id']]
        receipt['profiles'] = {choice['profile']: config.get('profiles', {}).get(choice['profile'], {})
                               for choice in environment['environments']}
        common.atomic_json(receipt_path, receipt)
        for alias, asset_id in aliases.items():
            config.setdefault('assets', {})[asset_id] = str(storage_root / 'assets' / asset_id)
        config['allowed_repos'] = list(dict.fromkeys(config.get('allowed_repos', []) + [str(repo)]))
        node_tag = 'hnode-' + _digest(str(config['node_id']).encode())[:20]
        config['tags'] = list(dict.fromkeys(config.get('tags', []) + [node_tag]))
        templates = []
        for experiment in manifest['experiments']:
            templates.append(common.validate_task({'name': manifest['name'] + ' / ' + experiment.get('name', experiment['id']),
                'algorithm': manifest['name'], 'group': manifest['project_id'], 'backend': 'docker',
                'project_id': manifest['project_id'], 'project_bundle_id': manifest['bundle_id'],
                'experiment_id': experiment['id'], 'source': {'repo': str(repo), 'commit': commit},
                'command': ['python', '/workspace/code/.expman/runtime/harness.py', 'run',
                    '--manifest', '/workspace/code/.expman/harness.json', '--source', '/workspace/code/project'],
                'params': experiment['params'], 'assets': list(aliases.values()), 'asset_aliases': aliases,
                'environments': environment['environments'], 'tags': config.get('tags', []),
                'deployment_tags': config.get('tags', []),
                'resources': manifest['resources'], 'metric_protocol': 'external-log-rules-v1',
                'resume_supported': manifest['harness'].get('resume', {}).get('supported', False)}))
        config['task_templates'] = [t for t in config.get('task_templates', [])
                                    if t.get('project_bundle_id') != manifest['bundle_id']] + templates
        if len(config['task_templates']) > 100 or len(_encoded(config['task_templates'])) > 128 * 1024:
            raise ValueError('Too many saved templates; remove unused presets before installing')
        return {'config': config, 'templates': templates, 'project': {
            'project_id': manifest['project_id'], 'name': manifest['name'],
            'bundle_id': manifest['bundle_id'], 'root': str(root)}}


def publish_bundle(bundle, hub, token):
    read_bundle(bundle)
    bundle = _local(bundle)
    size, digest, upload_id = bundle.stat().st_size, common.sha256_file(bundle), uuid.uuid4().hex
    offset, result = 0, None
    with bundle.open('rb') as stream:
        while block := stream.read(512 * 1024):
            result = common.api_request(hub.rstrip('/') + '/api/projects/upload', token,
                {'upload_id': upload_id, 'sha256': digest, 'size': size, 'offset': offset,
                 'data': base64.b64encode(block).decode('ascii')}, timeout=120)
            offset += len(block)
            if result.get('offset') != offset:
                raise ValueError('Controller upload offset mismatch')
            print(f'Upload / 上传 {offset} / {size}', flush=True)
    if not result or not result.get('complete'):
        raise ValueError('Controller did not finalize the project upload')
    return result['project']


def _wizard():
    print('External algorithm import / 外置算法接入（保留原源码）')
    source = input('Algorithm folder / 算法文件夹: ').strip().strip('"')
    default = str(Path.home() / 'ExperimentProjects' / (Path(source).name + '-harness'))
    output = input('External configuration folder / 外置配置目录 [回车 ' + default + ']: ').strip().strip('"') or default
    if not (Path(output) / 'project.json').exists():
        result = prepare_project(source, output)
        print('Entry / 识别入口: ' + result['entry'])
    print('Configuration / 配置目录: ' + output)
    print('Review project.json (files, data, dependencies), harness.json (entry), experiments/*.json (parameters).')
    print('检查 project.json 的文件、数据、依赖，harness.json 的入口，以及 experiments 中的实验参数。')
    print('Set reviewed=true in project.json when ready. Run this entry again to package and publish.')
    print('检查后把 project.json 中 reviewed 改为 true；再运行本入口即可打包上传。')
    if common.read_json(Path(output) / 'project.json').get('reviewed') is not True:
        if os.name == 'nt':
            os.startfile(output)
        return
    bundle = Path(output) / ('project-' + uuid.uuid4().hex[:8] + '.zip')
    result = build_project(output, bundle)
    print('BUNDLE: ' + result['file'])
    center = input('Controller data folder / 主控数据目录 [回车只生成ZIP]: ').strip().strip('"')
    if center:
        config = common.read_json(Path(center) / 'hub.json')
        if not config or not config.get('admin_token'):
            raise ValueError('Controller hub.json was not found')
        hub = input('Controller URL / 实验台地址 [http://127.0.0.1:8765]: ').strip() or 'http://127.0.0.1:8765'
        print(json.dumps(publish_bundle(bundle, hub, config['admin_token']), ensure_ascii=False, indent=2))
        print('Open Projects on the controller, select nodes and click Deploy. / 打开实验台项目库，勾选节点并分发。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('prepare')
    prepare.add_argument('--source', required=True)
    prepare.add_argument('--output', required=True)
    prepare.add_argument('--entry')
    prepare.add_argument('--project-id')
    build = sub.add_parser('build')
    build.add_argument('--project', required=True)
    build.add_argument('--output', required=True)
    publish = sub.add_parser('publish')
    publish.add_argument('--bundle', required=True)
    publish.add_argument('--hub', default='http://127.0.0.1:8765')
    publish.add_argument('--center-root', required=True)
    sub.add_parser('wizard')
    args = parser.parse_args()
    try:
        if args.action == 'prepare':
            result = prepare_project(args.source, args.output, args.entry, args.project_id)
        elif args.action == 'build':
            result = build_project(args.project, args.output)
        elif args.action == 'publish':
            center = common.read_json(Path(args.center_root) / 'hub.json')
            if not center or not center.get('admin_token'):
                raise ValueError('Controller hub.json was not found')
            result = publish_bundle(args.bundle, args.hub, center['admin_token'])
        else:
            return _wizard()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, RuntimeError, subprocess.TimeoutExpired) as error:
        raise SystemExit('External project / 外置项目: ' + str(error))


if __name__ == '__main__':
    main()
