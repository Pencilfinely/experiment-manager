"""Exercise the lesson on real SASRec with generated data, without a controller."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from expman.common import atomic_json, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='Original folder containing src/experiment.py')
    parser.add_argument('--root', required=True, help='New results directory outside the algorithm')
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cpu')
    args = parser.parse_args()
    import torch
    from expman.adapters.sasrec_smoke import _equal

    project = Path(args.project).resolve()
    root = Path(args.root).resolve()
    if not (project / 'src/experiment.py').is_file():
        raise ValueError('Project must contain src/experiment.py')
    if root.exists() or root == project or root.is_relative_to(project):
        raise ValueError('Choose a NEW results directory outside the original project')
    sources = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (project / 'src').rglob('*.py')}
    data = root / 'data/Toy'
    data.mkdir(parents=True)
    for split, value in {
        'train': '1 1 2 3 4\n2 2 3 5\n',
        'valid': '1 1 2 3 4 5\n2 2 3 5 6\n',
        'test': '1 1 2 3 4 5 6\n2 2 3 5 6 7\n',
    }.items():
        (data / ('Toy.' + split + '.txt')).write_text(value, encoding='utf-8')
    params = {'dataset': 'Toy', 'data_asset': 'toy-v1', 'device': args.device,
              'torch_threads': 1, 'seed': 42, 'epochs': 3, 'star_test': -1,
              'batch_size': 2, 'max_seq_length': 5, 'hidden_size': 8,
              'num_hidden_layers': 1, 'num_attention_heads': 1,
              'hidden_dropout_prob': 0.2, 'attention_probs_dropout_prob': 0.2,
              'candidate_chunk_size': 4, 'patience': 10}
    software = Path(__file__).resolve().parents[2]
    entries = (
        ('lesson', [sys.executable, str(Path(__file__).with_name('managed_sasrec.py'))]),
        ('reference', [sys.executable, '-m', 'expman.adapters.sasrec']),
    )
    for name, entry in entries:
        output = root / name
        output.mkdir()
        atomic_json(output / 'params.json', params)
        env = dict(os.environ, EXPERIMENT_PARAMS=str(output / 'params.json'),
                   EXPERIMENT_OUTPUT=str(output), EXPERIMENT_ASSETS=json.dumps({'toy-v1': str(data.parent)}),
                   EXPERIMENT_RESUME='0', EXPERIMENT_ATTEMPT='1',
                   PYTHONPATH=str(software), PYTHONDONTWRITEBYTECODE='1')
        print('Running:', name, flush=True)
        with (output / 'console.log').open('wb') as stream:
            result = subprocess.run(entry + ['--project', str(project)], env=env, cwd=software,
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=180)
        if result.returncode:
            raise RuntimeError('Training failed; read ' + str(output / 'console.log'))
    states = [torch.load(root / name / 'sasrec/Toy/managed/sasrec_last.pt',
                         map_location='cpu', weights_only=False) for name in ('lesson', 'reference')]
    fields = ('model', 'optimizer', 'rng_state', 'early_stopping', 'epoch', 'global_step')
    for field in fields:
        _equal(states[0][field], states[1][field], field)
    _equal(read_json(root / 'lesson/result.json')['test_metrics'],
           read_json(root / 'reference/result.json')['test_metrics'], 'test_metrics')
    records = [json.loads(line) for line in (root / 'lesson/metrics.jsonl').read_text().splitlines()]
    assert [r['step'] for r in records if 'train_loss' in r] == [1, 2, 3]
    assert sources == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (project / 'src').rglob('*.py')}
    atomic_json(root / 'lesson-report.json', {'status': 'passed', 'device': args.device,
        'synthetic_data': True, 'epochs': 3, 'state_fields_compared': fields,
        'test_metrics_match': True, 'epoch_metrics': [1, 2, 3],
        'original_source_unchanged': True, 'tutorial_resume_supported': False})
    print('PASS: lesson matches reference training; epoch metrics 1,2,3; original source unchanged')
    print('Inspect your generated config, metrics and models in:', root / 'lesson')


if __name__ == '__main__':
    main()
