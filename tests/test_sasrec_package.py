import io
from pathlib import Path
import zipfile
import unittest

from expman.common import atomic_json, read_json
from expman.launcher import InstanceLock
from expman.sasrec_package import export_package, install_package, read_payload
from expman.sasrec_setup import digest, encoded
from tests import test_sasrec_setup as fixtures


class SasrecPackageTests(unittest.TestCase):
    def package(self, base):
        root, project, _ = fixtures.SasrecImportTests().fixture(base)
        output = base / 'transfer.zip'
        export_package(project, 'config/video_games_full.json', output)
        with zipfile.ZipFile(output) as archive:
            bundle = base / 'Install-Project.pyz'
            bundle.write_bytes(archive.read('Install-Project.pyz'))
        return root, project, bundle

    def test_portable_bundle_uses_recipient_identity_image_and_paths(self):
        with fixtures.git_directory() as folder:
            base = Path(folder)
            root, project, bundle = self.package(base)
            original = {str(p): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                self.assertFalse(any('/.git/' in n or 'private-result' in n or 'node.ready.json' in n for n in names))
            for label in ('a', 'b'):
                destination = base / ('target-' + label)
                config = read_json(root / 'node.ready.json')
                config.update(root=str(destination / 'runtime'), node_id=label, tags=[label],
                              token='recipient-secret-' + label,
                              profiles={label: {'verified': True, 'image': 'target/runtime@sha256:' + label * 64}})
                config_path = destination / 'custom-node.json'
                atomic_json(config_path, config)
                before = config_path.read_bytes()
                receipt = install_package(bundle, config_path)
                self.assertEqual(receipt, install_package(bundle, config_path))
                installed = read_json(config_path)
                self.assertEqual(installed['token'], 'recipient-secret-' + label)
                task = installed['task_templates'][1]
                self.assertEqual(task['tags'], [label])
                self.assertEqual(task['environments'][0]['profile'], label)
                self.assertTrue(Path(task['source']['repo']).is_relative_to(destination))
                self.assertEqual(task['params']['seed'], 77)
                asset = Path(installed['assets'][task['params']['data_asset']])
                self.assertEqual((asset / 'Video_Games/Video_Games.train.txt').read_text(), '1 1 2 3\n')
                self.assertFalse((destination / 'node.ready.json').exists())
                backup = destination / 'imports/sasrec-video-games' / ('node-config-before-' + digest(before)[:16] + '.json')
                self.assertEqual(read_json(backup), config)
            self.assertEqual(original, {str(p): p.read_bytes() for p in project.rglob('*') if p.is_file()})

    def rewrite_bundle(self, path, mutate):
        with zipfile.ZipFile(path) as archive:
            files = {n: archive.read(n) for n in archive.namelist()}
        mutate(files)
        result = io.BytesIO()
        with zipfile.ZipFile(result, 'w') as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        path.write_bytes(result.getvalue())

    def test_tampered_data_rejected_before_node_changes(self):
        with fixtures.git_directory() as folder:
            root, _, bundle = self.package(Path(folder))
            before = (root / 'node.ready.json').read_bytes()
            self.rewrite_bundle(bundle, lambda files: files.update({
                'project/data/Video_Games/Video_Games.train.txt': b'changed data'}))
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                install_package(bundle, root / 'node.ready.json')
            self.assertEqual(before, (root / 'node.ready.json').read_bytes())
            self.assertFalse((root / 'projects').exists())

    def test_traversal_rejected_even_with_recomputed_manifest(self):
        import json
        with fixtures.git_directory() as folder:
            _, _, bundle = self.package(Path(folder))
            def mutate(files):
                name = 'project/../../outside.py'
                files[name] = b'bad'
                manifest = json.loads(files['project-manifest.json'])
                manifest['files'][name] = {'sha256': digest(b'bad'), 'bytes': 3}
                manifest.pop('bundle_id')
                manifest['bundle_id'] = digest(encoded(manifest))
                files['project-manifest.json'] = encoded(manifest)
            self.rewrite_bundle(bundle, mutate)
            with self.assertRaisesRegex(ValueError, 'Invalid project payload path'):
                read_payload(bundle)

    def test_running_recipient_is_not_reconfigured(self):
        with fixtures.git_directory() as folder:
            root, _, bundle = self.package(Path(folder))
            before = (root / 'node.ready.json').read_bytes()
            with InstanceLock(root / 'runtime/agent.lock'):
                with self.assertRaisesRegex(RuntimeError, 'already in use'):
                    install_package(bundle, root / 'node.ready.json')
            self.assertEqual(before, (root / 'node.ready.json').read_bytes())
            self.assertFalse((root / 'projects').exists())
