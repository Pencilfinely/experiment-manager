"""SASRec lesson: parameters, paths, epoch metrics and final results.

This is deliberately a first integration exercise, not the production adapter:
it rejects resume and does not implement cooperative stop/checkpoint publication.
No original algorithm source is modified. Run in a fresh Python process.
"""
import argparse
import json
from pathlib import Path
import sys

from expman.sdk import Run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    args = parser.parse_args()

    # 1. Find the original algorithm's callable entry.
    project = Path(args.project).resolve()
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(project / 'src'))
    import experiment
    import torch

    # 2. Receive this task's parameters and paths from the worker.
    run = Run()
    if run.resuming:
        raise ValueError('This first integration exercise does not implement resume')
    allowed = set(experiment._DEFAULTS) | {'dataset', 'data_asset', 'torch_threads'}
    unknown = set(run.params) - allowed
    if unknown:
        raise ValueError('Unknown SASRec parameters: ' + ', '.join(sorted(unknown)))
    asset_id = run.params['data_asset']
    if asset_id not in run.assets:
        raise ValueError('data_asset is not in this task\'s registered assets')
    data_root = run.assets[asset_id]
    output_root = run.output / 'sasrec'
    if output_root.exists():
        raise ValueError('Use a fresh output directory for each practice run')

    # 3. Convert worker inputs into the configuration the original code expects.
    raw = {key: value for key, value in run.params.items() if key in experiment._DEFAULTS}
    raw.update(dataset=run.params['dataset'], data_root=str(data_root),
               output_root=str(output_root), device=run.params.get('device', 'cuda'), gpu_id=0)
    if raw['device'] not in ('cpu', 'cuda'):
        raise ValueError('Choose explicit cpu or cuda')
    if raw['device'] == 'cuda' and (not torch.cuda.is_available() or torch.cuda.device_count() != 1):
        raise ValueError('Expected exactly one assigned CUDA GPU')
    torch.set_num_threads(int(run.params.get('torch_threads', 2)))
    config_path = run.output / 'sasrec-input.json'
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')

    # 4. SASRec validates output paths against this module variable too.
    # Assign it only in this dedicated process; no source file is rewritten.
    experiment.OUTPUT_ROOT = output_root
    output_root.mkdir()
    config = experiment.load_run_config(config_path, 'managed')

    # 5. Translate the algorithm's existing epoch log into manager metrics.
    original_log = experiment._append_log
    latest_epoch = 0

    def on_log(path, payload):
        nonlocal latest_epoch
        original_log(path, payload)
        if payload.get('stage') != 'train' or 'global_step' not in payload:
            return
        latest_epoch = int(payload['epoch'])
        values = {'train_loss': float(payload['train_loss'])}
        values.update({'valid/' + key: float(value)
                       for key, value in (payload.get('valid_metrics') or {}).items()})
        run.metric(latest_epoch, **values)

    experiment._append_log = on_log
    try:
        # 6. Call the ORIGINAL training function, then publish its final result.
        metrics = experiment.run_all(config, resume_mode='never')
        if metrics:
            run.metric(latest_epoch, **{'test/' + key: float(value) for key, value in metrics.items()})
        run.finish({'test_metrics': metrics, 'epochs_completed': latest_epoch})
    finally:
        experiment._append_log = original_log


if __name__ == '__main__':
    main()
