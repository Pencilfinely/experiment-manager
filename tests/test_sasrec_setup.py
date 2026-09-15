from contextlib import contextmanager
from pathlib import Path
import unittest

from expman.common import atomic_json, read_json
from expman.launcher import InstanceLock
from expman.sasrec_setup import import_project, local_path
from tests.support import temporary_directory


@contextmanager
def git_directory():
    with temporary_directory() as path:
        try:
            yield path
        finally:
            # Git object files are read-only on Windows; this is our isolated fixture.
            for item in Path(path).rglob('*'):
                if item.is_file() and not item.is_symlink():
                    item.chmod(0o600)


class SasrecImportTests(unittest.TestCase):
    def fixture(self, base):
        root = base / 'worker'
        project = base / 'research' / 'SASRec_Original'
        source = project / 'src'
        source.mkdir(parents=True)
        (source / 'experiment.py').write_text('''CHECKPOINT_SCHEMA_VERSION = 3
TRAINING_PROTOCOL = 'external_sasrec_original_v1'
_DEFAULTS = {'device': 'auto', 'gpu_id': 0, 'seed': 42, 'epochs': 300, 'star_test': 100, 'batch_size': 256}
def load_run_config(): pass
def run_all(): pass
def _append_log(): pass
''')
        for name in ('datasets', 'models', 'modules', 'trainers', 'utils', 'main'):
            (source / (name + '.py')).write_text('# example source\n')
        (project / 'output').mkdir()
        (project / 'output/private-result.txt').write_text('do not import')
        cfg = {'dataset': 'Video_Games', 'data_root': '../data', 'output_root': 'output',
               'device': 'cuda', 'gpu_id': 1, 'seed': 77, 'epochs': 200, 'batch_size': 128}
        atomic_json(project / 'config/video_games_full.json', cfg)
        for split in ('train', 'valid', 'test'):
            path = project.parent / 'data/Video_Games' / ('Video_Games.' + split + '.txt')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('1 1 2 3\n')
        atomic_json(root / 'node.ready.json', {'node_id': 'test', 'token': 'test', 'root': str(root / 'runtime'),
            'profiles': {'test': {'verified': True, 'image': 'example/runtime@sha256:' + 'a' * 64}},
            'tags': ['test'], 'assets': {}, 'allowed_repos': [], 'task_templates': []})
        return root, project, cfg

    def test_import_preserves_research_and_creates_repeatable_private_snapshot(self):
        with git_directory() as path:
            root, project, cfg = self.fixture(Path(path))
            original = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
            first = import_project(root, project)
            second = import_project(root, project)
            self.assertEqual(first, second)
            self.assertFalse((project / '.git').exists())
            self.assertEqual(original, {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()})
            node = read_json(root / 'node.ready.json')
            self.assertEqual(len(node['task_templates']), 2)
            self.assertEqual(len(node['allowed_repos']), 1)
            short, formal = node['task_templates']
            for key in ('seed', 'batch_size', 'epochs'):
                self.assertEqual(formal['params'][key], cfg[key])
            self.assertEqual(formal['params']['gpu_id'], 0)
            self.assertEqual({k for k in formal['params'] if formal['params'][k] != short['params'][k]}, {'epochs', 'star_test'})
            repo = Path(formal['source']['repo'])
            self.assertFalse((repo / 'SASRec_Original/output').exists())
            self.assertTrue((repo / '.git').is_dir())
            self.assertEqual(len(formal['source']['commit']), 40)

    def test_invalid_config_does_not_install_partial_snapshot(self):
        with git_directory() as path:
            root, project, cfg = self.fixture(Path(path))
            cfg['misspelled_learning_rate'] = 0.01
            atomic_json(project / 'config/video_games_full.json', cfg)
            original = (root / 'node.ready.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'Unrecognized'):
                import_project(root, project)
            self.assertEqual((root / 'node.ready.json').read_bytes(), original)
            self.assertFalse((root / 'projects').exists())

    def test_running_worker_prevents_import(self):
        with git_directory() as path:
            root, project, _ = self.fixture(Path(path))
            original = (root / 'node.ready.json').read_bytes()
            with InstanceLock(root / 'runtime/agent.lock'):
                with self.assertRaisesRegex(RuntimeError, 'already in use'):
                    import_project(root, project)
            self.assertEqual((root / 'node.ready.json').read_bytes(), original)
            self.assertFalse((root / 'projects').exists())

    def test_reject_changed_installed_snapshot(self):
        with git_directory() as path:
            root, project, _ = self.fixture(Path(path))
            import_project(root, project)
            node = read_json(root / 'node.ready.json')
            (Path(node['allowed_repos'][0]) / 'SASRec_Original/src/main.py').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'snapshot was changed'):
                import_project(root, project)


if __name__ == '__main__':
    unittest.main()
