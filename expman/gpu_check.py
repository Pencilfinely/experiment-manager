"""Small real CUDA workload for deployment checks; not a research benchmark."""
import json
import os
import time


def check():
    import torch
    expected = os.environ['CUDA_VISIBLE_DEVICES'].lower().removeprefix('gpu-')
    if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
        raise RuntimeError('Expected exactly one accessible CUDA device')
    gpu = torch.cuda.get_device_properties(0)
    if str(gpu.uuid).lower().removeprefix('gpu-') != expected:
        raise RuntimeError('CUDA selected a different GPU UUID')
    x = torch.ones((32, 32), device='cuda')
    if (x @ x).sum().item() != 32768.0:
        raise RuntimeError('CUDA matrix multiplication returned the wrong result')
    return {'status': 'passed', 'gpu_uuid': str(gpu.uuid), 'gpu_name': gpu.name,
            'torch': torch.__version__, 'cuda': torch.version.cuda,
            'compute_capability': list(torch.cuda.get_device_capability(0))}


def main():
    started = time.monotonic()
    result = check()
    if 'EXPERIMENT_OUTPUT' in os.environ:
        from .sdk import Run
        run = Run()
        run.metric(1, cuda_check_passed=1, elapsed_seconds=time.monotonic() - started)
        run.finish(result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
