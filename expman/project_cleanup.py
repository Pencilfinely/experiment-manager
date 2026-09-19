"""Delete only installed project copies with independently verified ownership.

The controller never supplies filesystem paths or Docker identifiers. Cleanup
plans are derived from the worker's own receipts, templates and execution log.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

from . import common


def owned_path(root, relative):
    """Refuse symlinks/junctions, including any redirected parent directory."""
    root = Path(root).resolve()
    path = common.safe_child(root, relative)
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Refusing cleanup through a link or junction: ' + str(current))
    if path.resolve() == root or not path.resolve().is_relative_to(root):
        raise ValueError('Cleanup must stay inside managed worker storage')
    return path


def _validate_tree(path):
    if not path.exists():
        return
    for parent, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            item = Path(parent) / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Refusing cleanup of a snapshot containing links: ' + str(item))


def _remove(root, relative):
    path = owned_path(root, relative)
    if path.is_dir():
        _validate_tree(path)
        def retry_readonly(function, target, error):
            if not isinstance(error[1], PermissionError):
                raise error[1]
            actual = Path(target).resolve()
            if actual != path.resolve() and not actual.is_relative_to(path.resolve()):
                raise ValueError('Cleanup retry escaped its validated directory')
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            function(target)
        shutil.rmtree(path, onerror=retry_readonly)
    else:
        path.unlink(missing_ok=True)


def plan_cleanup(root, node_config, records, declaration):
    """Read-only planning; callers persist the plan before removing config/files."""
    project_id, bundle_id = declaration['project_id'], declaration['bundle_id']
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', project_id)
            or not re.fullmatch(r'[0-9a-f]{64}', bundle_id)):
        raise ValueError('Invalid installed project identity')
    relative = 'distributed-projects/' + project_id + '/' + bundle_id
    snapshot = owned_path(root, relative)
    repo = str(snapshot / 'repository')
    receipt = common.read_json(owned_path(root, relative + '/.expman-install.json'))
    templates = node_config.get('task_templates', [])
    matches = lambda spec: spec.get('project_bundle_id') == bundle_id
    selected = [item for item in templates if matches(item)]
    targets = [item for item in records if matches(item['spec'])]
    if any(item['state'] not in ('succeeded', 'failed', 'paused', 'interrupted', 'canceled') for item in targets):
        raise ValueError('Project experiments are still active on this worker; retry cleanup after they stop')
    if receipt is None and snapshot.exists():
        # Migration for workers installed before ownership receipts existed.
        # The authenticated, hash-verified download proves the deterministic copy.
        from .harness_project import read_bundle, _digest, _encoded
        archive = owned_path(root, 'project-downloads/' + declaration['digest'] + '.zip')
        if not archive.exists() or common.sha256_file(archive) != declaration['digest']:
            raise ValueError('Missing ownership receipt and verified bundle; refusing to guess which directory to delete')
        manifest = read_bundle(archive)
        receipt = {'schema': 1, 'project_id': manifest['project_id'], 'bundle_id': manifest['bundle_id'],
            'assets': ['hdata-' + project_id[:30] + '-' + alias[:20] + '-' +
                _digest(_encoded({name: value for name, value in manifest['files'].items()
                                 if name.startswith('assets/' + alias + '/')}))[:16]
                for alias in manifest.get('assets', {})], 'profiles': {}}
    receipt = receipt or {'schema': 1, 'project_id': project_id, 'bundle_id': bundle_id, 'assets': [], 'profiles': {}}
    if receipt.get('schema') != 1 or (receipt.get('project_id'), receipt.get('bundle_id')) != (project_id, bundle_id):
        raise ValueError('Installed ownership receipt does not match requested project')
    if any((item.get('source') or {}).get('repo') != repo for item in selected):
        raise ValueError('Project source mapping was changed; refusing to remove an unverified repository')
    config = copy.deepcopy(node_config)
    config['task_templates'] = [item for item in templates if not matches(item)]
    deleted_bundles = set(config.get('deleted_project_bundles', [])) | {bundle_id}
    config['deleted_project_bundles'] = sorted(deleted_bundles)
    others = config['task_templates'] + [item['spec'] for item in records if not matches(item['spec'])
        and (item['spec'].get('project_bundle_id') not in deleted_bundles
             or item['state'] not in ('succeeded', 'failed', 'paused', 'interrupted', 'canceled'))]
    if any((item.get('source') or {}).get('repo') == repo for item in others):
        raise ValueError('Another experiment still references this project repository')
    config['allowed_repos'] = [item for item in config.get('allowed_repos', []) if item != repo]
    paths = []
    used_assets = {name for item in others for name in item.get('assets', [])}
    for name in receipt.get('assets', []):
        if not isinstance(name, str) or not re.fullmatch(r'hdata-[A-Za-z0-9_.-]{1,80}', name):
            raise ValueError('Invalid managed asset receipt')
        asset_relative = 'distributed-projects/assets/' + name
        expected = str(owned_path(root, asset_relative))
        registered = config.get('assets', {}).get(name)
        if name not in used_assets and registered in (None, expected):
            config.get('assets', {}).pop(name, None)
            paths.append(asset_relative)
    candidate_profiles = dict(receipt.get('profiles', {}))
    for spec in selected + [item['spec'] for item in targets]:
        for choice in spec.get('environments', []):
            name = choice['profile']
            candidate_profiles.setdefault(name, config.get('profiles', {}).get(name, {}))
    for name, recorded in list(candidate_profiles.items()):
        profile = config.get('profiles', {}).get(name, recorded)
        if profile.get('managed_by') == 'external-harness-project':
            # A retained profile may have gained ownership metadata when another
            # project sharing this runtime was removed.
            if all(profile.get(key) == value for key, value in recorded.items()):
                candidate_profiles[name] = profile
            continue
        # Upgrade legacy profiles only with their exact deterministic build
        # context inside this proven project snapshot. Prefixes alone are not
        # sufficient evidence to delete an existing Docker image.
        base = config.get('profiles', {}).get(profile.get('base_profile'), {})
        requirements = profile.get('requirements')
        if not base.get('image') or not isinstance(requirements, list) or not re.fullmatch(r'harness-[0-9a-f]{20}-[0-9a-f]{6}', name):
            continue
        key = hashlib.sha256(json.dumps({'base': base['image'], 'requirements': requirements}, sort_keys=True).encode()).hexdigest()
        gpu_key = hashlib.sha256(json.dumps(sorted(profile.get('gpu_name_patterns', []))).encode()).hexdigest()[:6]
        context = relative + '/environment/environments/' + key
        requirements_file = owned_path(root, context + '/requirements.txt')
        dockerfile = owned_path(root, context + '/Dockerfile')
        if (name != 'harness-' + key[:20] + '-' + gpu_key or not requirements_file.is_file()
                or not dockerfile.is_file() or requirements_file.read_text() != '\n'.join(requirements) + '\n'
                or not dockerfile.read_text().startswith('ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n')):
            continue
        profile = {**profile, 'managed_by': 'external-harness-project',
                   'image_tag': profile.get('image', '').split('@', 1)[0] + ':' + key[:24]}
        config['profiles'][name] = profile
        candidate_profiles[name] = profile
    used_profiles = {env['profile'] for item in others for env in item.get('environments', [])}
    removable = {name for name, value in candidate_profiles.items()
                 if value.get('managed_by') == 'external-harness-project' and name not in used_profiles
                 and config.get('profiles', {}).get(name, value) == value}
    # Preserve dependencies of every retained runtime profile transitively.
    changed = True
    while changed:
        bases = {value.get('base_profile') for name, value in config.get('profiles', {}).items() if name not in removable}
        new = removable - bases
        changed, removable = new != removable, new
    for name in removable:
        config.get('profiles', {}).pop(name, None)
    used_images = {value.get('image') for value in config.get('profiles', {}).values()}
    used_images.update(env['image'] for item in others for env in item.get('environments', []))
    images = []
    for name in removable:
        value = candidate_profiles[name]
        if value.get('image') not in used_images:
            for image in (value.get('image_tag'), value.get('image')):
                if image and image not in used_images and image not in images:
                    if not re.fullmatch(r'(?:localhost|127\.0\.0\.1):[0-9]+/expman-harness(?::[0-9a-f]{24}|@sha256:[0-9a-f]{64})', image):
                        raise ValueError('Managed image receipt is not a local harness image')
                    images.append(image)
    # A dependency build can succeed before CUDA verification or installation
    # fails; those images may never have reached the node's profile registry.
    image_receipts = common.read_json(owned_path(root, relative + '/environment/environments/owned-images.json'), [])
    for value in image_receipts:
        if not isinstance(value, dict) or not re.fullmatch(r'[0-9a-f]{64}', value.get('build_key', '')):
            raise ValueError('Invalid project image ownership receipt')
        if value.get('image') in used_images:
            continue
        for image in (value.get('image_tag'), value.get('image')):
            if not isinstance(image, str) or not re.fullmatch(r'(?:localhost|127\.0\.0\.1):[0-9]+/expman-harness(?::[0-9a-f]{24}|@sha256:[0-9a-f]{64})', image):
                raise ValueError('Invalid managed image ownership reference')
            if image not in images and image not in used_images:
                images.append(image)
    job_ids = []
    for record in targets:
        identity = record['id']
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid local job identity')
        paths.append('worktrees/' + identity)
        if record['spec'].get('backend') == 'docker' and record.get('attempt', 0):
            job_ids.append(identity)
    paths.extend(['repos/' + hashlib.sha256(repo.encode()).hexdigest(), relative,
                  'project-downloads/' + declaration['digest'] + '.zip',
                  'project-downloads/' + declaration['digest'] + '.part'])
    for path in paths:
        _validate_tree(owned_path(root, path))
    return {'config': config, 'ownership_receipt': {
                'path': relative + '/.expman-install.json',
                'value': {**receipt, 'profiles': candidate_profiles}},
            'plan': {'paths': paths, 'job_ids': job_ids, 'images': images,
                     'node_id': node_config['node_id']}}


def execute_cleanup(root, plan, execute):
    """Idempotent removal; no force, volume removal or global Docker pruning."""
    for job_id in plan['job_ids']:
        result = execute(['docker', 'ps', '-aq', '--filter', 'label=expman.node=' + plan['node_id'],
                          '--filter', 'label=expman.job=' + job_id], timeout=20)
        named = execute(['docker', 'ps', '-aq', '--filter', 'name=^/expman-' + job_id + '-[0-9]+$'], timeout=20)
        # A deterministic name whose labels disappeared is an ownership error,
        # not evidence that an old deployment was successfully removed.
        for identity in sorted(set(result.stdout.split()) | set(named.stdout.split())):
            if not re.fullmatch(r'[0-9a-f]{12,64}', identity):
                raise ValueError('Docker returned an invalid container identity')
            inspected = execute(['docker', 'inspect', identity], timeout=20)
            container = json.loads(inspected.stdout)[0]
            labels = container.get('Config', {}).get('Labels', {}) or {}
            if labels.get('expman.node') != plan['node_id'] or labels.get('expman.job') != job_id:
                raise ValueError('Container ownership changed; refusing removal')
            if container.get('State', {}).get('Running') or container.get('State', {}).get('Restarting'):
                raise ValueError('Project container is still running; retry cleanup after it stops')
            execute(['docker', 'rm', identity], timeout=30)
    for image in plan['images']:
        checked = execute(['docker', 'image', 'inspect', image], check=False, timeout=20)
        if checked.returncode:
            execute(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=20)
            if 'No such' not in checked.stderr:
                raise RuntimeError(checked.stderr[-500:])
            continue
        execute(['docker', 'image', 'rm', image], timeout=60)
    for relative in plan['paths']:
        _remove(root, relative)
