"""Import a compatible SASRec project without editing its code or Git history."""
import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .common import read_json, validate_task
from .launcher import InstanceLock
from .worker_setup import private_write


def local_path(value):
    value = str(value).strip().strip('"').strip("'")
    if os.name != 'nt' and re.match(r'^[A-Za-z]:[\\/]', value):
        value = '/mnt/' + value[0].lower() + '/' + value[3:].replace('\\', '/')
    return Path(value).expanduser()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def regular_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Expected a regular file: ' + str(path))
    return path.read_bytes()


def exact_file(path, data):
    if path.is_symlink():
        raise ValueError('Snapshot contains a symbolic link')
    if path.exists():
        if regular_bytes(path) != data:
            raise ValueError('An installed snapshot was changed: ' + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)


def inspect_project(project, config_path):
    project = local_path(project).resolve()
    source = project / 'src'
    tree = ast.parse(regular_bytes(source / 'experiment.py').decode('utf-8-sig'))
    constants = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in ('CHECKPOINT_SCHEMA_VERSION', 'TRAINING_PROTOCOL', '_DEFAULTS'):
                    constants[target.id] = ast.literal_eval(statement.value)
    functions = {item.name for item in tree.body if isinstance(item, ast.FunctionDef)}
    if (constants.get('CHECKPOINT_SCHEMA_VERSION') != 3 or
            constants.get('TRAINING_PROTOCOL') != 'external_sasrec_original_v1' or
            not {'load_run_config', 'run_all', '_append_log'} <= functions or
            not isinstance(constants.get('_DEFAULTS'), dict)):
        raise ValueError('This is not the supported SASRec_Original interface. Review an adapter for this implementation first.')
    for filename in ('datasets.py', 'models.py', 'modules.py', 'trainers.py', 'utils.py', 'main.py'):
        regular_bytes(source / filename)
    config_path = local_path(config_path)
    if not config_path.is_absolute():
        config_path = project / config_path
    research = json.loads(regular_bytes(config_path).decode('utf-8-sig'))
    if not isinstance(research, dict):
        raise ValueError('Research config must be a JSON object')
    allowed = set(constants['_DEFAULTS']) | {'dataset', 'data_root', 'output_root', 'torch_threads'}
    if set(research) - allowed:
        raise ValueError('Unrecognized research parameters: ' + ', '.join(sorted(set(research) - allowed)))
    dataset = research.get('dataset', '')
    if not isinstance(dataset, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', dataset):
        raise ValueError('Invalid dataset name in the research config')
    params = copy.deepcopy(constants['_DEFAULTS'])
    params.update({k: v for k, v in research.items() if k not in ('data_root', 'output_root')})
    if params.get('device') not in ('auto', 'cuda'):
        raise ValueError('This GPU import requires device cuda or auto; use a CPU workflow for a CPU configuration')
    params.update(device='cuda', gpu_id=0)
    params.setdefault('torch_threads', 2)
    sources = {}
    for folder, directories, files in os.walk(source, followlinks=False):
        directories[:] = sorted(n for n in directories if n != '__pycache__')
        if any((Path(folder) / n).is_symlink() for n in directories):
            raise ValueError('Source directories cannot be symbolic links')
        for filename in sorted(files):
            if filename.endswith('.py'):
                path = Path(folder) / filename
                sources['SASRec_Original/src/' + path.relative_to(source).as_posix()] = regular_bytes(path)
    data_root = local_path(research.get('data_root', '../data'))
    if not data_root.is_absolute():
        data_root = project / data_root
    data = {}
    for split in ('train', 'valid', 'test'):
        relative = dataset + '/' + dataset + '.' + split + '.txt'
        path = data_root / relative
        if path.stat().st_size > 512 * 1024**2:
            raise ValueError('A dataset split exceeds this importer\'s 512 MiB limit')
        data[relative] = regular_bytes(path)
        if not data[relative]:
            raise ValueError('Dataset split is empty: ' + str(path))
    return sources, data, params, research


def import_project(root, project, config_path='config/video_games_full.json', name=None, profile=None,
                   node_config=None):
    root = local_path(root).resolve()
    node_config = local_path(node_config).resolve() if node_config else root / 'node.ready.json'
    config = read_json(node_config)
    if not config:
        raise ValueError('Start and pair the worker first / 请先完成算力端配对')
    original = node_config.read_bytes()
    sources, data, params, research = inspect_project(project, config_path)
    name = name or 'sasrec-' + params['dataset'].lower().replace('_', '-')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise ValueError('Project name must use 1-64 letters, digits, _, . or -')
    profiles = [(key, value) for key, value in config.get('profiles', {}).items()
                if value.get('verified') is True and (not profile or key == profile)]
    if len(profiles) != 1:
        raise ValueError('Choose exactly one verified runtime with --profile')
    profile_name, environment = profiles[0]
    source_manifest = {n: digest(b) for n, b in sorted(sources.items())}
    data_manifest = {n: digest(b) for n, b in sorted(data.items())}
    repo = root / 'projects' / name / digest(encoded(source_manifest)) / 'repository'
    asset_id = name + '-data-' + digest(encoded(data_manifest))[:16]
    asset_path = root / 'datasets' / asset_id
    params['data_asset'] = asset_id
    def git(*args):
        result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=30)
        if result.returncode:
            raise RuntimeError('Could not prepare the private Git snapshot: ' + result.stderr[-700:])
        return result.stdout.strip()
    with InstanceLock(root / 'setup.lock'), InstanceLock(Path(config['root']) / 'agent.lock'):
        if node_config.read_bytes() != original:
            raise ValueError('Worker configuration changed; rerun the importer')
        if repo.is_symlink() or asset_path.is_symlink():
            raise ValueError('Import destinations cannot be symbolic links')
        for n, b in sources.items():
            exact_file(repo / n, b)
        exact_file(repo / 'source-manifest.json', encoded(source_manifest))
        if not (repo / '.git').exists():
            git('init')
            git('add', 'SASRec_Original', 'source-manifest.json')
            git('-c', 'user.name=ExperimentManager', '-c', 'user.email=snapshot@localhost',
                'commit', '-m', 'Record imported SASRec source snapshot')
        if (repo / '.git').is_symlink() or not (repo / '.git').is_dir() or git('status', '--porcelain'):
            raise ValueError('Private source snapshot was modified')
        commit = git('rev-parse', 'HEAD')
        for n, b in data.items():
            exact_file(asset_path / n, b)
        base = {'name': name + '-formal', 'algorithm': 'SASRec', 'backend': 'docker', 'group': name,
                'metric_protocol': 'external_sasrec_original_v1',
                'source': {'repo': str(repo), 'commit': commit},
                'command': ['python', '-m', 'expman.adapters.sasrec', '--project', '/workspace/code/SASRec_Original'],
                'environments': [{'profile': profile_name, 'image': environment['image']}],
                'tags': config['tags'], 'assets': [asset_id], 'params': params,
                'resources': {'gpu_memory_mb': 6000, 'ram_mb': 8192, 'cpu': 2, 'exclusive': False}}
        formal = validate_task(base)
        short = copy.deepcopy(formal)
        short['name'] = name + '-short'
        short['params'].update(epochs=3, star_test=-1)
        templates = [short, formal]
        config['assets'][asset_id] = str(asset_path)
        config['allowed_repos'] = list(dict.fromkeys(config['allowed_repos'] + [str(repo)]))
        config['task_templates'] = [t for t in config.get('task_templates', [])
                                    if t.get('name') not in {short['name'], formal['name']}] + templates
        if len(config['task_templates']) > 100 or len(encoded(config['task_templates'])) > 128 * 1024:
            raise ValueError('Too many saved templates')
        receipt = {'source_files': source_manifest, 'data_files': data_manifest,
                   'research_config': research, 'tasks': [t['name'] for t in templates],
                   'source_commit': commit, 'original_project_modified': False,
                   'training_submitted': False, 'runtime_changes': {'device': 'cuda', 'gpu_id': 0},
                   'short_run_changes': {'epochs': 3, 'star_test': -1}}
        for label, task in (('short', short), ('formal', formal)):
            private_write(root / 'imports' / name / (label + '.task.json'), task)
        private_write(root / 'imports' / name / 'import-report.json', receipt)
        if encoded(config) != original:
            backup = root / 'imports' / name / ('node-config-before-' + digest(original)[:16] + '.json')
            if not backup.exists():
                private_write(backup, json.loads(original.decode('utf-8-sig')))
        private_write(node_config, config)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path.home() / '.local/share/experiment-manager/worker'))
    parser.add_argument('--project')
    parser.add_argument('--config')
    parser.add_argument('--name')
    parser.add_argument('--profile')
    args = parser.parse_args()
    project = args.project or input('SASRec_Original folder / 算法目录（可粘贴 Windows 路径）: ')
    config = args.config or input('Research config / 科研配置（回车使用 config/video_games_full.json）: ').strip() or 'config/video_games_full.json'
    receipt = import_project(args.root, project, config, args.name, args.profile)
    print('Imported / 已导入: ' + ', '.join(receipt['tasks']))
    print('Restart the worker; use the short template first. / 启动算力端，在网页先选择 short 模板。')
    print('No training submitted; original project unchanged. / 未提交训练，原项目保持不变。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, RuntimeError, EOFError) as error:
        raise SystemExit('Import failed / 导入失败: ' + str(error))
