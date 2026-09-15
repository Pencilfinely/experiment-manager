"""Compare direct and managed invocation without a controller, Docker or GPU."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    project = Path(__file__).resolve().parent
    software = project.parents[1]
    with tempfile.TemporaryDirectory(prefix='expman-example-') as folder:
        root = Path(folder)
        params = {'lr': 0.05, 'epochs': 20, 'seed': 42}
        params_path = root / 'params.json'
        params_path.write_text(json.dumps(params))
        data = project / 'example-data'
        env = dict(os.environ, PYTHONPATH=str(software), EXPERIMENT_OUTPUT=str(root / 'managed'),
                   EXPERIMENT_PARAMS=str(params_path), EXPERIMENT_ASSETS=json.dumps({'example-data': str(data)}),
                   EXPERIMENT_RESUME='0')
        subprocess.run([sys.executable, str(project / 'train.py'), '--data-dir', str(data),
                        '--output-dir', str(root / 'direct'), '--lr', '0.05', '--epochs', '20', '--seed', '42'],
                       check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, str(project / 'expman_entry.py')], env=env, check=True,
                       stdout=subprocess.DEVNULL)
        for name in ('model.json', 'summary.json'):
            if (root / 'direct' / name).read_bytes() != (root / 'managed' / name).read_bytes():
                raise RuntimeError('Direct and managed results differ: ' + name)
        if json.loads((root / 'managed/result.json').read_text()) != json.loads((root / 'direct/summary.json').read_text()):
            raise RuntimeError('Managed result is missing or changed')
        env['EXPERIMENT_RESUME'] = '1'
        failed = subprocess.run([sys.executable, str(project / 'expman_entry.py')], env=env,
                                capture_output=True, text=True)
        if failed.returncode == 0 or 'does not support resume' not in failed.stderr:
            raise RuntimeError('Incomplete resume was not rejected')
        print('PASS: identical model and results; managed metrics/result emitted; unsupported resume rejected')


if __name__ == '__main__':
    main()
