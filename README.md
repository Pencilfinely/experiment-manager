# Experiment Manager

[English](README.md) · [简体中文](README.zh-CN.md)

Run GPU experiments on your computers and follow them from one browser. Keep the
code version, environment, parameters, logs, metrics and checkpoints together.

**Release: 0.2.0-rc.1 — a preview release.** The controller and worker are separate
applications. The controller does not train models. Install both editions on a
Windows computer if it should manage experiments and also contribute its GPU.

## Download the right edition

Get the ZIP assets from [Releases](https://github.com/Pencilfinely/experiment-manager/releases).
Download the named application ZIPs, rather than GitHub's automatically generated
“Source code” archives. Extract the whole ZIP before starting it.

| ZIP suffix | Install on | Start here | Prerequisites |
|---|---|---|---|
| `windows-controller-x64` | Your Windows 11 control computer | Double-click `Start-Controller.cmd` | A browser; Python is included. No WSL or Docker needed. |
| `windows-worker-x64` | A Windows 11 NVIDIA GPU computer | Double-click `Start-Worker.cmd` | WSL2 with Ubuntu 22.04+, Docker Desktop with WSL integration, NVIDIA Windows driver |
| `ubuntu-worker-x64` | A native Ubuntu 22.04+ NVIDIA GPU computer/server | Run `bash Start-Worker.sh` | Docker Engine, NVIDIA Linux driver and NVIDIA Container Toolkit; your normal user must be able to run Docker |

All three assets belong to the same release and use the same software version.
The current builds target x86-64. ARM and AMD/Intel GPU execution are not supported.
The Ubuntu asset is a worker, not a combined controller/worker installer.

Workers use Python 3.10+ and Git in Ubuntu; the launcher installs missing Python/Git
packages through `apt` and may ask for your normal `sudo` password. It does not
install or replace GPU drivers, WSL or Docker. Allow roughly 8 GiB of free disk
space for first setup, plus space for your code, datasets and experiment outputs.

## What happens where?

```text
Control computer                         GPU computer
Browser → Controller ── private network ── Worker → Docker → Your training program
           ↑                                  │
           └──────── logs, metrics, files ──────┘
```

The **controller** keeps the experiment list, assigns work and receives results.
The **worker** checks what its machine can run, prepares inputs and launches the
training container. Docker provides the experiment's Linux/Python environment;
its Ubuntu version need not match the host's Ubuntu version.

The computers must already be able to reach each other through a LAN or a trusted
private network such as your VPN. Being on the same campus is not sufficient by
itself. A worker initiates connections to the controller; no inbound worker port
is needed. The software discovers possible controller addresses but cannot make
an unreachable network routable.

## First setup, step by step

### 1. Start the controller

1. On the Windows control computer, extract the controller ZIP into a permanent
   folder, for example `C:\Apps\ExperimentManager-Controller`.
2. Double-click **`Start-Controller.cmd`**. It creates its own data directory and
   opens the experiment page with a local browser login already applied.
3. Keep its console window running. The browser may be closed and reopened;
   closing the controller console stops the service.

By default the data lives in `%LOCALAPPDATA%\ExperimentManager\controller`, outside
the application folder. The first available port between 8765 and 8784 is selected
on first launch and saved. Later launches keep it. Reopening the same installation
opens its existing controller instead of starting a duplicate.

### 2. Give each worker a pairing file

1. In the controller page click **“添加算力机 / Add worker”**.
2. Enter a unique name, such as `gpu-desktop` or `ubuntu-server`.
3. Choose or enter the controller URL **as reached from that worker**, including
   the port. Use the controller's LAN/private-VPN address. `localhost` on another
   machine points at that other machine. Windows and WSL also have distinct
   networking, so use a reachable Windows address when managing your own WSL GPU.
4. Click **“下载配对文件 / Download pairing file”**. Copy this small
   `NAME.pairing.json` file to the target worker.

The pairing file is that worker's credential. Each computer gets a different
worker name and file. Re-exporting an existing name preserves its identity for
reinstallation; do not give the same identity to two computers. The administrator
credential is never included in a worker's pairing file.

If Windows blocks a remote worker, run **`Allow-Worker-Connections.cmd`** in the
controller folder, enter that worker's IPv4 address and accept the Windows
administrator prompt. The helper allows only that source IP to the saved
controller TCP port. It does not change routers or VPN routes. If your campus or
VPN policies block the connection, that network policy must be resolved first.
An explicit Windows firewall **Block** rule overrides an Allow rule. If you
previously denied this controller's Windows network prompt, review its bundled
`runtime\python.exe` entry under Windows Defender Firewall → Advanced settings →
Inbound Rules. The helper reports such a block and leaves it for you to review.

### 3. Start the worker

**Windows worker**

1. Start Docker Desktop. Under **Settings → Resources → WSL Integration**, enable
   the Ubuntu distribution you will use.
2. Extract the worker ZIP into a permanent local folder. Put its one
   `NAME.pairing.json` file next to `Start-Worker.cmd`.
3. Double-click **`Start-Worker.cmd`**. If there are several WSL distributions,
   choose one once; the selection is remembered.

**Native Ubuntu worker**

1. Extract the Ubuntu worker ZIP and put its pairing file beside `Start-Worker.sh`.
2. Open a terminal in that directory, or connect over SSH and `cd` into it.
3. Run **`bash Start-Worker.sh`**, as your normal Linux user, not root.

Both workers then automatically:

1. Authenticate to the controller and check local Docker, NVIDIA GPUs and disk.
2. Prepare a private, versioned application copy and a small Git snapshot.
3. Build the runtime from a fixed PyTorch/CUDA image and record its immutable
   digest in a local registry bound to `127.0.0.1:5001`.
4. Run a real CUDA calculation on each enabled GPU, checking the GPU UUID inside
   the restricted training container.
5. Generate the node settings, conservative resource budgets and a GPU-check
   task template, then start accepting work.

First setup downloads a large image and can take several minutes. A failed step
prints the reason and log location. Run the same launcher again after fixing it;
completed image preparation is reused. Normal subsequent starts reuse the saved
configuration and reconnect, including after temporary controller outages.

Worker data lives in `~/.local/share/experiment-manager/worker` **inside Ubuntu**.
No manual node JSON editing is required for initial pairing and runtime setup.
Keep Docker and the worker console running while using the node.

### 4. Verify the whole path from the browser

1. Wait for the worker to appear online under **计算节点**.
2. Click its **“填入任务 / Use: GPU-check-…”** button.
3. Review the filled task and click **“提交到队列”** once. The parameter grid `{}`
   means one experiment.
4. Open the resulting experiment. It should become **已完成 / succeeded**.
5. Check that `result.json` is downloadable and contains `"status": "passed"`.
   Wait until the worker's pending upload count reaches zero.

This proves browser submission, assignment, a real GPU container and result
return work together. It is a deployment check, not a model-quality benchmark.

## Run your own experiments

For an implementation walkthrough using the real SASRec training code, see the [step-by-step adaptation lesson (Chinese)](docs/SASREC-ADAPTATION-WALKTHROUGH.zh-CN.md) and its executable scripts in `examples/sasrec-adaptation`.

Start with [the complete file-editing and cross-machine workflow](docs/ALGORITHM-INTEGRATION.md): a runnable added entry, a direct-versus-managed result check, and SASRec export, Xftp transfer, recipient installation and browser submission. The SASRec project transfer tool is new in the current source and is not in the published v0.2.0-rc.1 ZIPs.

Installing the worker prepares the manager's runtime. Your algorithm, dataset and
training parameters remain yours to choose; setup cannot infer every project's
dependencies or scientifically appropriate settings.

For a local Git project compatible with the supplied PyTorch environment:

1. Finish/stop current work, exit the worker console, then run
   **`Configure-Project.cmd`** on Windows, or **`bash Configure-Project.sh`** on Ubuntu.
2. Enter a project name, its local Git repository root, the training command and
   an optional dataset folder. The wizard requires a clean, committed repository;
   it does not commit or discard your research changes.
3. Restart the worker. Its new project template appears on the controller.
4. Fill that template, set your experiment parameters and realistic GPU/RAM/CPU
   budgets in the task editor, and submit. The generated budgets are starting
   values, not a measurement or a promise that any model will fit.

Code is mounted read-only at `/workspace/code`; outputs belong in
`/workspace/run`. The optional dataset is registered as `PROJECT-data-v1` and
mounted read-only under `/assets/PROJECT-data-v1`. The included SDK exposes these
paths, parameters, metrics and cooperative stop/checkpoint support. Programs that
write into their own source folder need an output-path adjustment.

See [operations and algorithm integration](docs/OPERATIONS.md) for a complete
small workload, SASRec setup, parameters, multiple independent tasks, troubleshooting,
backup and upgrades. The external SASRec implementation and research datasets are
not distributed in these application ZIPs.

## GPU scheduling: current capabilities

| Mode | Current support |
|---|---|
| One experiment on one GPU | Supported; the default setup uses one running task per node |
| Several independent experiments on separate GPUs | Supported after increasing node concurrency and meeting resource budgets |
| Several independent experiments on one GPU | Supported when tasks allow sharing, the GPU job limit allows it, and budgets fit |
| One experiment across multiple GPUs or machines | Not implemented; requires algorithm and scheduler support |
| Automatically choose the fastest allocation from measured performance | Not implemented |

Each experiment gets a selected physical GPU UUID and sees its own logical
`cuda:0`. This matters on WSL, where Docker's GPU selection alone may still expose
multiple devices. Process-level selection is not a hardware security boundary.
Free VRAM is not proof of free compute. The default prioritizes predictable
single-task execution; sharing should be justified by measured completion times.
The manager does not silently change batch size, precision or learning rate.

## Reliability and limits

- A worker can continue already assigned **and cached** tasks while the controller
  is offline, then send pending records and files after reconnecting. Unprepared
  jobs may still need Git/image access. New jobs cannot be submitted to an offline
  controller.
- Stop/resume requires an adapter that saves complete training state. A generic
  process cannot gain reliable model/optimizer/RNG recovery just by being wrapped
  in Docker. The SASRec adapter includes explicit recovery support.
- A completed task's files can still be uploading. Check both task status and
  pending uploads before shutting machines down.
- This preview uses foreground launchers, not installed system services. Windows
  sleep, logout, reboot or stopping Docker can interrupt compute. It does not
  include MSI signing, unattended OS installation or automatic updates.
- Use localhost or a trusted private network. Do not expose the controller
  directly to the public Internet; it uses bearer credentials and is not a
  multi-tenant public service.

## For developers

The Python application uses the standard library. A source checkout needs
Python 3.10+; PyTorch/NumPy are required only where actual SASRec/CUDA work runs.

```bash
python -m unittest discover -s tests -t . -v
python scripts/build_release.py --download-python
```

The release builder uses an explicit file allowlist, validates the official
embedded Python SHA256, produces per-file manifests and writes `SHA256SUMS.txt`.
Runtime data, pairing files, private deployment notes, external algorithms and
datasets are excluded. Native GPU tests remain a separate hardware acceptance
step; unit tests alone do not certify a GPU model.

Protocol reference: [CONTRACT.md](CONTRACT.md). Known issues and requests:
[GitHub Issues](https://github.com/Pencilfinely/experiment-manager/issues).

## License

MIT — see [LICENSE](LICENSE). Bundled CPython and separately installed/downloaded
components retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
