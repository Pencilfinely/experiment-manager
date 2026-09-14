import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import zipfile

from scripts import node_installer as setup
from tests.support import temporary_directory


class NodeInstallerTests(unittest.TestCase):
    def fixture(self, root):
        source_path = root / 'source.zip'
        files = {'sasrec-source/SASRec_Original/src/experiment.py': b'# source\n',
                 'experiment_manager/expman/agent.py': b'# agent\n',
                 'experiment_manager/expman/__main__.py': b'# CLI\n',
                 'experiment_manager/deploy/Dockerfile.sasrec': b'FROM example\n'}
        manifest = {'files': {name: {'sha256': setup.sha(data), 'bytes': len(data)} for name, data in files.items()}}
        with zipfile.ZipFile(source_path, 'x') as source:
            for name, data in files.items():
                source.writestr(name, data)
            source.writestr('source-manifest.json', setup.json_bytes(manifest))
        config = root / 'node.json'
        config.write_bytes(setup.json_bytes({'node_id': 'test-node', 'hub_url': 'http://127.0.0.1:8765',
            'token': 'test-private-token', 'root': '/old/root', 'irrelevant': 'not exported'}))
        output = root / 'node-installer.zip'
        setup.build(source_path, config, output)
        with zipfile.ZipFile(output) as outer:
            package = root / 'ExperimentNode.pyz'
            package.write_bytes(outer.read('ExperimentNode.pyz'))
        return source_path, output, package

    def test_package_contains_platform_launchers_and_only_enrollment_settings(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            source, output, package = self.fixture(root)
            with zipfile.ZipFile(output) as outer:
                self.assertEqual(set(outer.namelist()), {'ExperimentNode.pyz', 'Start-Windows.cmd',
                    'Install-Windows.ps1', 'Start-Linux.sh', 'README.txt'})
            with zipfile.ZipFile(package) as inner:
                node = json.loads(inner.read('node.json'))
                self.assertEqual(set(node), {'node_id', 'hub_url', 'token'})
                self.assertEqual(node['token'], 'test-private-token')
            result = subprocess.run([sys.executable, str(package), '--help'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--prepare-only', result.stdout)

    def test_source_checksum_and_unsafe_paths_are_rejected(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            source, output, package = self.fixture(root)
            with self.assertRaises(ValueError):
                setup.safe_path(root, 'experiment_manager/../../outside')
            modified = io.BytesIO()
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(modified, 'w') as target:
                for name in original.namelist():
                    data = original.read(name)
                    target.writestr(name, data + b'x' if name.endswith('agent.py') else data)
            with self.assertRaisesRegex(ValueError, '摘要'):
                setup.validate_source(modified.getvalue())

    def test_state_bound_to_node_and_source_before_reuse(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            source, output, package = self.fixture(root)
            installer = setup.Installer(package, root / 'install')
            installer.save()
            again = setup.Installer(package, root / 'install')
            self.assertEqual(again.state['node_id'], 'test-node')
            installer.state['node_id'] = 'another-node'
            installer.save()
            with self.assertRaisesRegex(ValueError, '另一份'):
                setup.Installer(package, root / 'install')

    def test_resume_receipt_requires_matching_image_commit_and_report_hash(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            source, output, package = self.fixture(root)
            installer = setup.Installer(package, root / 'install')
            installer.commit = 'a' * 40
            gpu = {'uuid': 'GPU-11111111-1111-1111-1111-111111111111', 'name': 'GPU'}
            report = installer.root / 'previous-report.json'
            report.write_text('{"status":"passed","device":"cuda"}', encoding='utf-8')
            installer.state['gpu_checks'] = {gpu['uuid']: {'image': 'image@sha256:abc',
                'commit': installer.commit, 'report': report.name, 'sha256': setup.sha(report.read_bytes())}}
            with patch.object(installer, 'command', side_effect=AssertionError('Must reuse verified receipt')):
                installer.verify_gpus('image@sha256:abc', [gpu])
            report.write_text('changed', encoding='utf-8')
            installer.repo = installer.root / 'repo'
            with patch.object(installer, 'command', side_effect=ValueError('new GPU check required')), \
                    patch.object(setup.os, 'getuid', return_value=1000, create=True), \
                    patch.object(setup.os, 'getgid', return_value=1000, create=True):
                with self.assertRaisesRegex(ValueError, 'new GPU check required'):
                    installer.verify_gpus('image@sha256:abc', [gpu])

    def test_registry_digest_uses_push_output_and_restores_stopped_registry(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            source, output, package = self.fixture(root)
            installer = setup.Installer(package, root / 'install')
            installer.software = installer.root / 'software'
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                if argv[:3] == ['docker', 'container', 'inspect']:
                    return 0, json.dumps([{'HostConfig': {'PortBindings': {'5000/tcp': [
                        {'HostIp': '127.0.0.1', 'HostPort': '5000'}]}}, 'Config': {'Image': 'registry:3'},
                        'State': {'Running': False}}])
                if argv[:2] == ['docker', 'push']:
                    return 0, 'tag: digest: sha256:' + 'b' * 64 + ' size: 856'
                return 0, ''
            with patch.object(installer, 'command', side_effect=run):
                image = installer.image()
            self.assertEqual(image, 'localhost:5000/expman-sasrec@sha256:' + 'b' * 64)
            self.assertIn(['docker', 'start', 'expman-registry'], calls)
            self.assertNotIn('RepoDigests', str(calls))

    def test_center_package_preserves_existing_identity_and_requires_existing_center(self):
        from expman.hub import Hub
        with temporary_directory() as temporary:
            root = Path(temporary)
            source, output, package = self.fixture(root)
            center = root / 'center'
            hub = Hub(center)
            try:
                original_admin = hub.config['admin_token']
                original_node = hub.add_node('already-enrolled')
            finally:
                hub.close()
            target = root / 'new-node.zip'
            setup.build_from_center(source, center, 'new-node', 'http://127.0.0.1:8765', target)
            current = json.loads((center / 'hub.json').read_text())
            self.assertEqual(current['admin_token'], original_admin)
            self.assertEqual(current['nodes']['already-enrolled'], original_node)
            self.assertIn('new-node', current['nodes'])
            with self.assertRaisesRegex(ValueError, '输出包已存在'):
                setup.build_from_center(source, center, 'not-added', 'http://127.0.0.1:8765', target)
            self.assertNotIn('not-added', json.loads((center / 'hub.json').read_text())['nodes'])
            with self.assertRaisesRegex(ValueError, '已有中心'):
                setup.build_from_center(source, root / 'missing', 'new-node', 'http://127.0.0.1:8765', root / 'other.zip')
            self.assertFalse((root / 'missing').exists())


if __name__ == '__main__':
    unittest.main()
