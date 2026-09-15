import json
from pathlib import Path
import unittest

from expman.harness_discovery import discover_project
from tests.support import temporary_directory


class HarnessDiscoveryTests(unittest.TestCase):
    def source(self, root, relative, text):
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def test_never_executes_entry_and_extracts_literal_and_dynamic_arguments(self):
        with temporary_directory() as root:
            entry = self.source(root, 'src/main.py', '''import argparse
from pathlib import Path
import definitely_uninstalled_dependency
Path(__file__).with_name('MUST_NOT_EXIST').write_text('executed')
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--learning-rate', '-l', type=float, default=-0.125, help='step size')
    parser.add_argument('--feature', action='store_true')
    parser.add_argument('--enabled', action='store_false')
    parser.add_argument('--mode', choices=['fast', 'slow'], default='fast', required=True)
    parser.add_argument('--computed', default=expensive_function(), type=custom_type)
    parser.add_argument('input_file')
    parser.add_argument('--optional', default=None)
    parser.add_argument(*dynamic_flags)
main()
''')
            before = entry.read_bytes()
            report = discover_project(root)
            self.assertFalse((entry.parent / 'MUST_NOT_EXIST').exists())
            self.assertEqual(before, entry.read_bytes())
            self.assertEqual(report['selected_entry'], 'src/main.py')
            self.assertEqual(report['command'], ['{python}', 'main.py'])
            self.assertEqual(report['cwd'], 'src')
            args = {item['dest']: item for item in report['arguments']}
            self.assertEqual(args['learning_rate']['default'], -0.125)
            self.assertEqual(args['learning_rate']['flags'], ['--learning-rate', '-l'])
            self.assertEqual(args['learning_rate']['type'], 'float')
            self.assertEqual(args['learning_rate']['confidence'], 'literal')
            self.assertFalse(args['feature']['default'])
            self.assertTrue(args['enabled']['default'])
            self.assertEqual(args['mode']['choices'], ['fast', 'slow'])
            self.assertTrue(args['mode']['required'])
            self.assertTrue(args['input_file']['required'])
            self.assertTrue(args['optional']['default_provided'])
            self.assertEqual(args['computed']['dynamic_fields'], ['default', 'type'])
            self.assertEqual(args['computed']['confidence'], 'review')
            self.assertEqual(report['dependency_hints'], ['definitely_uninstalled_dependency'])
            self.assertTrue(any('module scope' in message for message in report['warnings']))
            self.assertTrue(any('dynamic add_argument' in message for message in report['warnings']))
            json.dumps(report, allow_nan=False)

    def test_gpu_override_path_concatenation_and_metrics_are_reviewable_hints(self):
        with temporary_directory() as root:
            self.source(root, 'src/main.py', '''import argparse, os
import torch
from models import Model
p = argparse.ArgumentParser()
p.add_argument('--data_dir', default='../data/', type=str)
p.add_argument('--data_name', default='demo', type=str)
p.add_argument('--output_dir', default='output/', type=str)
p.add_argument('--gpu_id', default='0', type=str)
p.add_argument('--do_eval', action='store_true')
args = p.parse_args()
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_id
data_file = args.data_dir + args.data_name + '.txt'
output_file = args.output_dir + '/result.json'
if args.do_eval:
    model.load_state_dict(torch.load('weights.pt'))
''')
            self.source(root, 'src/models.py', 'import numpy as np\nimport scipy.sparse\n')
            self.source(root, 'src/trainers.py', '''import json
post_fix = {'epoch': epoch, 'average_loss': '{:.4f}'.format(loss), 'ignored_string': 'x'}
print(post_fix)
scores = {'Epoch': epoch, 'NDCG@10': ndcg, 'HIT@10': hit}
''')
            self.source(root, 'data/main.py', 'raise RuntimeError("data should be excluded")')
            self.source(root, 'src/output/train.py', 'raise RuntimeError("outputs should be excluded")')
            report = discover_project(root)
            self.assertEqual(report['dependency_hints'], ['numpy', 'scipy', 'torch'])
            self.assertEqual(report['gpu_visibility']['argument'], 'gpu_id')
            self.assertTrue(report['gpu_visibility']['overwritten'])
            self.assertEqual([h['dest'] for h in report['path_hints']], ['data_dir'])
            self.assertTrue(report['path_hints'][0]['trailing_separator'])
            self.assertFalse(report['resume']['supported'])
            self.assertEqual(report['resume']['candidates'], [])
            self.assertTrue(any('not evidence' in message for message in report['warnings']))
            self.assertEqual(len(report['entries']), 1)
            self.assertEqual(report['metric_hints'][0]['value_keys'], ['average_loss'])
            self.assertEqual(report['metric_hints'][1]['step_key'], 'Epoch')
            self.assertEqual(report['metric_hints'][1]['value_keys'], ['NDCG@10', 'HIT@10'])

    def test_native_resume_candidate_stays_disabled_and_manual_entry_can_be_selected(self):
        with temporary_directory() as root:
            self.source(root, 'main.py', 'raise RuntimeError("wrong entry")')
            self.source(root, 'jobs/fit.py', '''import argparse
p = argparse.ArgumentParser()
p.add_argument('--resume-from', default=None)
p.add_argument('--checkpoint', default=None)
''')
            self.source(root, 'start.bat', '@echo off\r\npython jobs/fit.py\r\n')
            self.source(root, 'run.sh', '#!/bin/sh\npython jobs/fit.py\n')
            report = discover_project(root, 'jobs/fit.py')
            self.assertEqual(report['selected_entry'], 'jobs/fit.py')
            self.assertEqual(report['resume']['candidates'], ['resume_from', 'checkpoint'])
            self.assertFalse(report['resume']['supported'])
            self.assertEqual(report['command'], ['{python}', 'fit.py'])
            self.assertEqual(report['cwd'], 'jobs')
            batch = discover_project(root, 'start.bat')
            self.assertEqual(batch['command'], ['cmd.exe', '/d', '/c', 'start.bat'])
            self.assertTrue(any('Linux GPU containers' in message for message in batch['warnings']))
            shell = discover_project(root, 'run.sh')
            self.assertEqual(shell['command'], ['bash', 'run.sh'])

    def test_rejects_outside_entry_and_reports_incomplete_parse(self):
        with temporary_directory() as root:
            project = Path(root) / 'project'
            project.mkdir()
            outside = self.source(root, 'outside.py', '')
            with self.assertRaisesRegex(ValueError, 'inside'):
                discover_project(project, outside)
            self.source(project, 'main.py', 'def broken(:\n')
            report = discover_project(project)
            self.assertEqual(report['arguments'], [])
            self.assertTrue(any('Cannot statically parse main.py' in message for message in report['warnings']))
            self.assertFalse(report['resume']['supported'])

    def test_no_entry_does_not_assume_first_python_file_is_executable(self):
        with temporary_directory() as root:
            self.source(root, 'models.py', 'class Model: pass\n')
            report = discover_project(root)
            self.assertIsNone(report['selected_entry'])
            self.assertEqual(report['command'], [])
            self.assertTrue(any('No conventional entry' in message for message in report['warnings']))


if __name__ == '__main__':
    unittest.main()
