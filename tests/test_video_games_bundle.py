import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from scripts import video_games_bundle as bundle
from tests.support import temporary_directory


class VideoGamesBundleTests(unittest.TestCase):
    def fixture(self, root):
        data = root / 'input'
        data.mkdir()
        for i, name in enumerate(bundle.NAMES):
            (data / name).write_text('1 1 2 ' + str(i + 3) + '\n', encoding='utf-8')
        research = {'dataset': 'Video_Games', 'data_root': '../private-data', 'output_root': 'output',
                    'device': 'cuda', 'gpu_id': 0, 'seed': 42, 'epochs': 300, 'star_test': 100,
                    'batch_size': 256, 'hidden_size': 64, 'lr': 0.001, 'num_hidden_layers': 2,
                    'attention_probs_dropout_prob': 0.5, 'user_ratio': 1.0, 'patience': 20}
        research_path = root / 'research.json'
        research_path.write_bytes(bundle.encoded(research))
        task = {'name': 'Toy', 'algorithm': 'SASRec', 'backend': 'docker',
                'source': {'repo': '/node/source', 'commit': 'a' * 40},
                'command': ['python', '-m', 'expman.adapters.sasrec'],
                'environments': [{'profile': 'tested', 'image': 'local/sasrec@sha256:' + 'b' * 64}],
                'tags': ['node-a'], 'params': {'hidden_size': 8, 'epochs': 3, 'batch_size': 2}}
        task_path = root / 'toy.json'
        task_path.write_bytes(bundle.encoded(task))
        config = {'node_id': 'node-a', 'hub_url': 'http://127.0.0.1:8765', 'token': 'private-node-token',
                  'root': str(root / 'node-state'), 'allowed_repos': ['/node/source'], 'tags': ['node-a'],
                  'profiles': {'tested': {'image': task['environments'][0]['image'], 'verified': True}},
                  'assets': {'toy-v1': '/node/toy'}, 'policy': {'max_running': 1}}
        config_path = root / 'node.json'
        config_path.write_bytes(bundle.encoded(config))
        output = root / 'ready.pyz'
        bundle.build(data, research_path, task_path, output)
        return output, config_path, research

    def prepare(self, archive, config, home):
        mkdir = os.mkdir
        def inherited(path, mode=0o777, **kwargs):
            return mkdir(path, 0o777 if os.name == 'nt' else mode, **kwargs)
        with patch.object(os, 'mkdir', inherited):
            return bundle.prepare(archive, config, home)

    def rewrite(self, archive, path, change):
        with zipfile.ZipFile(archive) as original, zipfile.ZipFile(path, 'x') as modified:
            for name in original.namelist():
                modified.writestr(name, change(name, original.read(name)))

    def test_formal_task_preserves_research_params_and_short_changes_only_run_length(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            archive, config, research = self.fixture(root)
            manifest, payload = bundle.load_bundle(archive)
            full = json.loads(payload['tasks/formal.task.json'])
            short = json.loads(payload['tasks/short.task.json'])
            expected = {k: v for k, v in research.items() if k not in ('data_root', 'output_root')}
            expected.update(data_asset=manifest['asset_id'], torch_threads=2)
            self.assertEqual(full['params'], expected)
            expected.update(epochs=3, star_test=-1)
            self.assertEqual(short['params'], expected)
            self.assertTrue(full['resources']['exclusive'])
            self.assertNotIn(b'private-node-token', archive.read_bytes())
            self.assertEqual(json.loads(root.joinpath('ready.formal.task.json').read_text()), full)

    def test_prepare_is_repeatable_retains_original_and_never_exports_credentials_in_tasks(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            archive, config, research = self.fixture(root)
            before = config.read_bytes()
            output = self.prepare(archive, config, root / 'home')
            self.assertEqual(self.prepare(archive, config, root / 'home'), output)
            self.assertEqual(config.read_bytes(), before)
            prepared = json.loads((output / 'node.ready.json').read_text())
            self.assertEqual(prepared['token'], 'private-node-token')
            self.assertEqual(prepared['assets']['toy-v1'], '/node/toy')
            self.assertEqual(prepared['policy']['max_running'], 1)
            self.assertNotIn('private-node-token', (output / 'short.task.json').read_text())
            if os.name != 'nt':
                self.assertEqual((output / 'node.ready.json').stat().st_mode & 0o777, 0o600)

    def test_corrupt_payload_rejected_before_creating_any_destinations(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            archive, config, research = self.fixture(root)
            bad = root / 'bad.pyz'
            self.rewrite(archive, bad, lambda name, data: data + b'x' if name.startswith('data/') else data)
            with self.assertRaisesRegex(ValueError, 'checksum'):
                self.prepare(bad, config, root / 'home')
            self.assertFalse((root / 'home').exists())

    def test_extra_traversal_path_rejected_before_writes(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            archive, config, research = self.fixture(root)
            with zipfile.ZipFile(archive, 'a') as package:
                package.writestr('../escaped', b'bad')
            with self.assertRaisesRegex(ValueError, 'unexpected'):
                self.prepare(archive, config, root / 'home')
            self.assertFalse((root / 'home').exists())

    def test_unverified_environment_rejected_before_data_import(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            archive, config, research = self.fixture(root)
            node = bundle.read_json(config)
            node['profiles']['tested']['verified'] = False
            config.write_bytes(bundle.encoded(node))
            with self.assertRaisesRegex(ValueError, 'not been verified'):
                self.prepare(archive, config, root / 'home')
            self.assertFalse((root / 'home').exists())

    def test_changed_existing_files_are_preserved(self):
        with temporary_directory() as temporary:
            root = Path(temporary)
            archive, config, research = self.fixture(root)
            output = self.prepare(archive, config, root / 'home')
            target = output / 'formal.task.json'
            target.write_text('user edited this', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
                self.prepare(archive, config, root / 'home')
            self.assertEqual(target.read_text(), 'user edited this')


if __name__ == '__main__':
    unittest.main()
