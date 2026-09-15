"""Translate one managed run into the existing train.py command line."""
import json
import subprocess
import sys
from pathlib import Path

from expman.sdk import Run


def main():
    run = Run()
    # This example has no complete training checkpoint; fail instead of restarting.
    if run.resuming:
        raise RuntimeError('This example does not support resume; submit a new run')
    unknown = set(run.params) - {'lr', 'epochs', 'seed'}
    if unknown:
        raise ValueError('Unknown training parameters: ' + ', '.join(sorted(unknown)))
    if len(run.assets) != 1:
        raise ValueError('Register exactly one dataset folder for this example')
    data_dir = next(iter(run.assets.values()))
    command = [sys.executable, str(Path(__file__).with_name('train.py')),
               '--data-dir', str(data_dir), '--output-dir', str(run.output),
               '--lr', str(run.params.get('lr', 0.05)),
               '--epochs', str(run.params.get('epochs', 20)),
               '--seed', str(run.params.get('seed', 42))]
    # Inherit stdout/stderr so the existing training log reaches the controller.
    subprocess.run(command, check=True)
    result = json.loads((run.output / 'summary.json').read_text())
    run.metric(int(result['epochs']), loss=float(result['loss']))
    run.finish(result)


if __name__ == '__main__':
    main()
