# Third-party components

Experiment Manager source code is licensed under the MIT license in `LICENSE`.

The Windows controller ZIP redistributes the official CPython 3.13.15 embeddable
distribution from the Python Software Foundation. Its license text, historical
licenses and notices are preserved in `runtime/LICENSE.txt`. The application
changes only `runtime/python313._pth` to include its own application directory.
Source and archive checksum: [Python 3.13.15](https://www.python.org/downloads/release/python-31315/).

Workers download Docker images at setup time. PyTorch, CUDA, cuDNN, NumPy and
the OCI registry retain their upstream licenses; those images are not included
in these ZIPs. Docker, WSL, Ubuntu and NVIDIA drivers/toolkit are separately
installed prerequisites, not part of this project's MIT grant.

The SASRec adapter calls user-provided algorithm code. No external SASRec
implementation, private research repository, dataset or trained model is bundled
in the public source repository or the three release ZIPs. Users retain
responsibility for the licenses of the projects and datasets they choose to run.
