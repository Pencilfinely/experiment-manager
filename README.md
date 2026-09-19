# Experiment Manager

[English](README.md) · [简体中文](README.zh-CN.md)

Manage GPU experiments, computers and algorithm projects in one application window.
Choose an algorithm's original root folder, review discovered parameters, publish it and send experiments to your workers. Keep the original source unchanged.

**0.4.0 is available as a regular release.** Controller and worker are separate applications. Install both on a computer that should manage experiments and contribute its GPU.
This version adds experiment matrices, automatic/assisted/manual allocation, form-based import with optional AI assistance, managed project cleanup, and Markdown result reports. See the [release notes](docs/RELEASE-0.4.0.md). Updates start when you request them.

## Download and install

Get application installers or complete ZIPs from the [latest release](https://github.com/Pencilfinely/experiment-manager/releases/latest). GitHub's automatically generated Source code archives are not installers.

| Computer | Recommended asset suffix | Application | Prerequisites |
|---|---|---|---|
| Windows 11 controller | windows-controller-x64-Setup.exe | **Experiment Center**, ExperimentCenter.exe | Microsoft Edge; no WSL, Docker or separate Python installation |
| Windows 11 NVIDIA worker | windows-worker-x64-Setup.exe | **Experiment Worker**, ExperimentWorker.exe | WSL2, Ubuntu 22.04+, Docker Desktop with integration enabled for that Ubuntu, NVIDIA Windows driver |
| Native Ubuntu 22.04+ NVIDIA server | ubuntu-worker-x64.zip | Background worker after installation | Docker Engine, NVIDIA Linux driver and NVIDIA Container Toolkit; normal-user access to Docker |

Windows installers install for the current user without administrator privileges. Firewall or system dependency changes may still require elevation.
Portable Windows ZIPs provide the same .exe applications: extract the whole archive before opening them. Current builds target x86-64 and NVIDIA GPUs.
First worker setup downloads large images; allow at least 8 GiB plus space for code, datasets and experiment outputs.

## First use: three steps

### 1. Open Experiment Center

Start **Experiment Center**. Its sidebar contains Overview, Experiments, Experiment Matrices, Compute, Algorithm Projects and Settings.
The controller service runs in the background; closing the page window does not stop it. Reopen it through the application or tray.
Local application launch signs you in automatically. You do not need to copy an administrator token or keep a terminal open.

The controller keeps data separately from application files. For an existing deployment, select its original data directory, such as **E:/ExperimentCenter**, to retain experiment history.
Do not use the replaceable application directory as your data directory.

### 2. Connect a worker

In **Compute**, add a computer, give it a unique name and select a controller address **that the worker can reach**. Download its pairing file.
Copy the worker installer/package and **NAME.pairing.json** to that computer.

- **Windows:** install and open Experiment Worker, choose the pairing file and Ubuntu distribution, then start setup. The application checks prerequisites, verifies GPUs, creates configuration and accepts work in the background. Progress and logs stay available in its window.
- **Ubuntu:** extract the worker ZIP and run this command as your normal user from that directory, replacing the pairing-file path:

    bash Install-Worker.sh --pairing /path/to/node.pairing.json

The command returns while first setup continues in the background. Check progress with **bash Worker-Status.sh**; view logs with **bash Client-Worker.sh logs**.
After setup, the computer appears online in the controller. Subsequent starts reuse its identity and configuration.

A pairing file is one worker's credential and never contains the administrator token. Use a distinct identity for each computer; reuse the original identity when reinstalling that same computer.
Computers must already be reachable over a LAN or trusted private network. Do not use localhost for a remote worker. Sharing a campus network does not guarantee reachability.
Workers initiate connections to the controller and need no inbound worker port.

### 3. Import and run an algorithm in the same window

1. Open **Algorithm Projects → Import algorithm** and choose the original root folder. Select the discovered main.py or other supported original entry.
2. Review fixed parameters, separate experiment presets, datasets, extra dependencies and resource budgets in the page. Add or edit presets without opening several JSON files.
3. Review the selected-file preview and **publish to the project library**. This records a fixed version of the code, data and configuration; it does not start training.
4. Select online workers on the project card and **deploy**. Workers automatically prepare code, data and the Docker environment. Wait for installation to succeed.
5. Choose **Create experiment**, select a preset and edit parameters. Submit a short test first, then follow progress, logs, metrics and result files in **Experiments**.

Experiment matrices combine datasets and parameter values: save the configuration, preview allocation, launch a batch, and export Markdown results. Allocation supports automatic selection, candidate/preferred workers, and a manually selected worker/GPU. Import forms include optional AI advice, and project removal cleans managed deployments while preserving original files. See the [workflow guide (Chinese)](docs/EXPERIMENT-MATRICES.zh-CN.md).

Local-folder import is available in the application on the controller computer. A remote browser can upload a prepared project ZIP; it cannot browse the controller's filesystem.

**The original algorithm is not rewritten.** The external harness calls its original entry in an isolated working copy, passes configuration and collects outputs.
Native resume can be configured when the original entry supports it. Otherwise resume stays unavailable; a saved model alone is not advertised as full training recovery.
Discovery is a draft: dynamic arguments, dependency versions and log meanings need review. Windows .bat entries do not run directly in Linux GPU containers; choose the Python/bash entry they actually invoke.

For a concrete example, see [Review an unchanged SASRec_Original import](docs/EXTERNAL-HARNESS.md). It uses **E:/PythonProjects/SASRec_Original/src/main.py** and one **Video_Games.test.txt** file, not the different implementation containing experiment.py.

## Distribution, outputs and background operation

Publish a project once and let selected workers receive it. Current distribution sends **immutable code/data snapshots through the controller**, with private Git snapshots and Docker environments prepared on each worker. Routine use requires no node-side Git or Docker commands.
**Direct GitHub/GitLab account integration and third-party registry publishing controls are not implemented.** Publish another version when source or data changes; create another experiment when only learning rate, seed or similar parameters change.

Each experiment has its own configuration and output directory. Console output is saved, existing log files can be configured as metric sources, and models/results return with the experiment.
Task completion and file upload completion may occur at different times. Check pending uploads before shutting down.

The overview, experiment list and details show **cumulative runtime**. Timing starts with execution, freezes when it stops, and accumulates across resumed attempts; queueing, preparation and stopped periods are excluded. Workers persist timing, so closing the page or losing the controller connection does not reset it. Live values are estimates until confirmed by the worker. Details show submission, first start and latest stop times; CSV exports include timing fields. Update both Center and Worker for complete timing support. Old records remain unavailable, and uncertain history is marked incomplete.

The Windows worker can continue after its window closes, but still depends on the current user's WSL and Docker Desktop.
**Background operation does not mean training continues through sleep, logout or power-off.** Optional login startup is not a Windows service running without user login.
Native Ubuntu prefers user-level systemd when available and otherwise uses a detached process. Startup before login or persistence after logout depends on the machine's existing user-service/lingering settings; the installer does not silently change those system policies.

## Scheduling capabilities

| Mode | Current support |
|---|---|
| One experiment on one GPU | Supported; setup defaults to conservative concurrency |
| Separate experiments on separate GPUs | Supported when node concurrency and resource budgets allow |
| Several independent experiments on one GPU | Supported when tasks allow sharing, the GPU job limit allows it and budgets fit |
| One experiment across multiple GPUs or machines | Not implemented; requires algorithm and scheduler support |
| Automatically measure and choose the fastest allocation | Not implemented |

Free VRAM is not proof of free compute. The manager does not silently change batch size, precision or learning rate to make a task fit.
Workers can continue already assigned, cached tasks during a temporary controller outage and return records after reconnecting. An offline task is not silently duplicated onto another machine.

## Existing deployments and everyday use

**Upgrading from 0.3.0-rc.1 or earlier:** download and install this release manually; those versions have no in-app update entry. Finish tasks and pending uploads, stop the old controller/agent in its client and exit that client from its tray before installing the same-role package.

**From 0.3.0-rc.2 onward on Windows:** choose **Check for updates** in the application's status window or tray menu. Review the current/new versions and release notes, then download the matching Center or Worker installer. The application checks its size and SHA-256 before installation. You can download while busy and install later, after experiments and pending uploads finish. Installation checks that services can stop safely, exits the old client and opens the new installer. Your data-directory selection, Ubuntu distribution, node configuration and login-startup setting are retained.

Preview versions check for newer previews and stable releases; stable versions check for stable releases only. Ubuntu workers continue to use a downloaded package and the existing script/manual upgrade procedure.

Never run old and new agents against the same node directory at once. See [Everyday operations](docs/OPERATIONS.md) for upgrading, backups, background controls and troubleshooting.
Use the application on localhost or a trusted private network; it is not a public multi-tenant service.

## Development and license

Controller logic uses Python's standard library; a source checkout needs Python 3.10+.
Release builds select explicit application files. Credentials, user algorithms and research datasets are excluded from public packages.

    python -m unittest discover -s tests -t . -v
    python scripts/build_release.py --download-python

[Protocol](CONTRACT.md) · [Issues](https://github.com/Pencilfinely/experiment-manager/issues) · [Legacy SDK reference for developers](docs/ALGORITHM-INTEGRATION.md).

MIT — see [LICENSE](LICENSE). CPython and separately downloaded components retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
