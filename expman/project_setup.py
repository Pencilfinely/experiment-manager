"""Register an existing local Git project and generate a task without editing config."""
import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess

from .common import read_json, validate_task
from .launcher import InstanceLock
from .worker_setup import private_write


def register(root, name, repository, command, data=None):
    root = Path(root).expanduser().resolve()
    config_path = root / 'node.ready.json'
    config = read_json(config_path)
    original = config_path.read_bytes() if config else None
    if not config:
        raise ValueError('Run Start-Worker first / 请先完成算力端安装')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name):
        raise ValueError('Use letters/digits/_, ., - for the project name')
    repo = Path(repository).expanduser().resolve()
    def git(*args):
        result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('Choose a local Git repository with a committed version / 请选择已提交版本的 Git 仓库')
        return result.stdout.strip()
    if Path(git('rev-parse', '--show-toplevel')).resolve() != repo:
        raise ValueError('Select the root folder of the Git repository')
    if git('status', '--porcelain'):
        raise ValueError('Commit your intended changes first. The manager never commits or discards your research code.')
    commit = git('rev-parse', 'HEAD')
    profiles = [(key, value) for key, value in config.get('profiles', {}).items() if value.get('verified') is True]
    if not profiles:
        raise ValueError('No verified runtime. Run Start-Worker --configure first.')
    key, profile = profiles[0]
    assets = []
    if data:
        path = Path(data).expanduser().resolve()
        if not path.is_dir() or ',' in str(path):
            raise ValueError('Dataset path must be an existing folder without commas')
        asset_id = name + '-data-v1'
        existing = config['assets'].get(asset_id)
        if existing and existing != str(path):
            raise ValueError('Asset version already points to different data; use a new project/version name')
        config['assets'][asset_id] = str(path)
        assets.append(asset_id)
    task = validate_task({'name': name, 'algorithm': 'custom', 'backend': 'docker', 'group': name,
        'source': {'repo': str(repo), 'commit': commit}, 'command': command,
        'environments': [{'profile': key, 'image': profile['image']}], 'tags': config['tags'],
        'assets': assets, 'params': {}, 'resources': {'gpu_memory_mb': 2048, 'ram_mb': 2048, 'cpu': 1, 'exclusive': True}})
    if str(repo) not in config['allowed_repos']:
        config['allowed_repos'].append(str(repo))
    templates = config.get('task_templates', [])
    config['task_templates'] = [t for t in templates if t.get('name') != name] + [task]
    if len(json.dumps(config['task_templates']).encode()) > 128 * 1024:
        raise ValueError('Too many saved templates; export older templates before adding more')
    with InstanceLock(root / 'setup.lock'), InstanceLock(Path(config['root']) / 'agent.lock'):
        # All validation succeeded before the configuration is replaced.
        if config_path.read_bytes() != original:
            raise ValueError('Configuration changed during setup. Run the wizard again.')
        private_write(root / (name + '.task.json'), task)
        private_write(config_path, config)
    return task


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=str(Path.home() / '.local/share/experiment-manager/worker'))
    args = parser.parse_args()
    print('Stop the worker window first. This wizard preserves your repository. / 先退出算力端窗口，再登记项目。')
    name = input('Project name / 项目名称: ').strip()
    repo = input('Local Git repository folder / 本机 Git 仓库目录: ').strip().strip('"').strip("'")
    command = shlex.split(input('Training command (e.g. python train.py) / 训练命令: ').strip())
    data = input('Dataset folder (Enter skips) / 数据目录（可留空）: ').strip().strip('"').strip("'")
    register(args.root, name, repo, command, data or None)
    print('Saved. Restart Start-Worker, then use the project template on the controller. / 已保存，启动算力端后主控网页会出现任务模板。')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error))
