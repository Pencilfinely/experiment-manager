# Everyday operations: experiments, workers and projects

[简体中文](OPERATIONS.zh-CN.md) · [Back to installation](../README.md)

This guide covers the **0.3.0-rc.5 desktop preview**. Start ordinary work through Experiment Center and Experiment Worker. Older terminal entries remain for compatibility and diagnosis.

## Open, close a window, or stop a service

| Goal | Control | Effect |
|---|---|---|
| Open the controller | Experiment Center shortcut or tray | Reopens the same service and experiment history |
| Put the window away | Close the application window | Background work continues; reopen through the tray or application |
| Exit the controller completely | Center tray menu → Exit | Stops the local controller service and closes its dedicated experiment window; remote experiments continue |
| Exit the worker completely | Worker tray menu → Exit | Requests cooperative experiment stops, waits for execution to stop, then exits the agent and client; algorithms without native resume cannot promise recovery |
| Stop accepting new experiments | Controller → Compute → Pause accepting work | Current experiments continue; new assignments/starts pause |
| Stop the controller | Experiment Center stop control | Controller operations become unavailable; already running worker Docker experiments continue |
| Stop the worker agent | Experiment Worker stop control | Acceptance/reporting stops; existing Docker experiments are not forcibly killed and can be managed after agent restart |
| End an experiment | Experiment detail controls | Uses that project's supported stop/cancel behavior |

Before shutdown or upgrade, finish tasks and pending uploads. Stopping the agent does not mean all training containers have ended.
Tray exit shows progress and keeps the client available if shutdown cannot be confirmed. Pending reports and files remain on the worker for synchronization after the next start. It does not close WSL or Docker Desktop.
Windows sleep, logout, Docker exit, WSL shutdown or power-off can affect training. Background application operation does not remove these dependencies.

From the Ubuntu worker package directory:

    bash Worker-Status.sh
    bash Client-Worker.sh logs
    bash Stop-Worker.sh
    bash Start-Background-Worker.sh

The installer prefers a user-level systemd service and otherwise uses a detached process. It does not change system lingering policy.
Running without login requires the machine's user-service, Docker and GPU conditions to be configured accordingly. Windows login startup applies only after that Windows user logs in.

## What belongs to an algorithm project?

An **algorithm project** records code, data, invocation settings and reusable experiment presets. An **experiment** runs one project version with one concrete parameter set.

1. Open **Algorithm Projects → Import algorithm** and choose the original root folder.
2. Review the detected entry, fixed parameters, experiment presets, datasets and dependencies. Discovery does not execute source.
3. Save and review the selected-file preview, then publish.
4. Deploy from the project card to selected workers and wait for installation.
5. Choose Create experiment, select a preset, edit parameters if needed, and submit.

For parameter-only changes, create another experiment. For changes to code, data, dependencies or invocation, publish and deploy another project version.
Submitted experiments retain their original version. Editing local source does not silently replace code in a running task.

Ordinary use does not require editing JSON files. The page's advanced settings correspond to:

| Purpose | External configuration |
|---|---|
| Code, datasets, extra dependencies and budgets | project.json |
| Original entry, fixed parameters, argument bindings, log rules and native resume | harness.json |
| Separate experiment parameter sets | experiments/*.json |

Drafts live separately under the controller's data directory; the harness is not written into the original algorithm folder.
Errors appear in the import page and can be corrected before retrying. Scanning and packaging run in the background while existing experiments remain available.
A browser on another computer uses project ZIP upload; original-folder selection is limited to the controller computer.

Concrete example: [Review the original SASRec arguments and dataset](EXTERNAL-HARNESS.md).

## Logs, metrics and resume

Experiment details show console output and returned files. External parsing rules can turn an algorithm's existing logs into metrics; labels should reflect what those logs actually mean.
For example, identical validation/test HIT@10 output must not automatically be labeled as a test-only metric.

Use the original algorithm's existing capability:
- Native resume entry: configure its checkpoint/arguments and verify it before using recovery.
- Model weights only: model download remains useful, but does not imply optimizer/RNG/training-state recovery.
- No resume: training and output collection still work; interrupted experiments start again.

The example **E:/PythonProjects/SASRec_Original** lacks complete native training resume, so recovery is unavailable. The old adapter for another implementation with the same name does not change that fact.

## Resources and networking

New projects allow GPU sharing. External programs using a GPU do not themselves prevent admission. Validate a short experiment, then edit node/GPU concurrency, CPU/RAM budgets and VRAM headroom under **Compute → Resource settings**. Single experiments and matrices can override their resources and sharing mode. Exclusive use applies only to experiments managed by this software. Admission checks current free resources and conservatively reserves existing experiments' declared budgets.
Upgrades preserve existing settings; a node previously limited to one experiment can now be adjusted in the form. Offline updated nodes receive saved settings after reconnecting, with applied status shown after acknowledgement. Lower limits do not stop running experiments. Remote resource settings require both Center and Worker 0.4.3 or newer.
Multi-GPU execution of a single experiment and automatic fastest-allocation selection are not implemented. Free VRAM is not free compute.

A pairing address must be reachable from the worker; a remote computer's localhost is that computer itself. Update connection settings when the controller address/port changes.
If Windows Firewall blocks a worker, the controller package's **Allow-Worker-Connections.cmd** can allow the saved controller port from that specific worker IPv4 address. It requires elevation and does not change campus/VPN routing.

Workers may continue assigned tasks whose code, data and images are already cached during a temporary controller outage. They report records/files after reconnecting.
An offline worker does not silently cause a duplicate copy of its experiment to run elsewhere.

## Upgrade, backup and removal

### Windows: check, download and install in the application

Available in Experiment Center and Experiment Worker from **0.3.0-rc.2**. Each application updates its own role; update both if both are installed on the same computer.

**Workers may be updated before Center in a multi-computer deployment.** From 0.4.2, Center is not blocked solely by disconnected nodes without unfinished work or by missing fresh snapshots. Active experiments, unacknowledged commands and registered pending transfers still block installation. If a running older Center is blocked only by disconnected nodes, stop that old management service and manually install the new version using the original data directory.

1. Open the application's status window or tray menu and choose **Check for updates**. The update window shows your current version, the available version and its release notes.
2. Choose to download the update. The application retrieves the same-role Windows `Setup.exe` from [this project's GitHub Releases](https://github.com/Pencilfinely/experiment-manager/releases) and verifies its published size and SHA-256. A failed check prevents installation; retry the download or use the manual procedure below.
3. You can leave the downloaded installer staged while work continues. Before installing, pause accepting work, finish experiments and pending uploads, and back up your data directories.
4. Choose to install the downloaded update. The application checks for work that prevents a safe stop. If it reports busy or cannot establish that stopping is safe, finish the reported work and retry later.
5. Once the service/agent has stopped safely, the old client exits and the downloaded installer opens. Follow the installer to complete the upgrade. The saved controller data-directory selection, Windows Ubuntu distribution, node configuration and login-startup preference are retained. A worker agent that was running restarts after the update; one that was stopped remains stopped.
6. Open the updated application, verify its version, experiment history and worker identity, then resume accepting work.

Updates require your action; checking or downloading alone does not install anything. Preview builds accept newer preview and stable releases; stable builds accept stable releases only. The release metadata and installers must be reachable from the computer through GitHub. SHA-256 detects a download that differs from the published checksum; installers are not code-signed.

### First upgrade from an older version, Ubuntu and manual installation

**0.3.0-rc.1 and earlier have no Check for updates entry.** Download the current version manually once; future Windows releases can then use the application workflow above. Ubuntu workers continue to use downloaded packages and the existing scripts. Use this procedure for manual Windows upgrades as well:

Opening a newer client does not update an already running backend. If it still
connects to an rc.1 worker or controller, the safe-stop check can time out because
that backend does not support the request. Waiting for synchronization will not
resolve this. Stop the old backend, exit the tray client, then run the downloaded
installer as below. After installing a worker, choose **Start background agent**
to run the new software with the existing node configuration.

1. Pause accepting work and finish active experiments and pending uploads.
2. Stop the old controller/agent in its client, then exit that client from the system tray. For a terminal deployment, press Ctrl+C in its old window. The installer refuses to replace a running same-role client; this is not a hot upgrade.
3. Back up controller and worker data directories.
4. Install the new package for the same role. Select the original controller directory and original Windows Ubuntu distribution. A unique existing node configuration is reused automatically; choose by node name/path when several are found.
5. Verify experiment history, node identity and project status, then resume accepting work.

Updating a web page alone cannot upgrade an old worker's distribution protocol. Upgrade that computer's worker application when the project card requests it.
Do not run two agents with one identity. **Update-Worker.cmd / Update-Worker.sh** reuse existing worker configuration with a package you have already downloaded; they do not check GitHub or download a release themselves.

Replacing/removing application files and deleting experiment data are separate actions. Keep backups before deciding whether to remove data. Do not delete Docker virtual disks to perform an application upgrade.

## Troubleshooting

| Symptom | Next step |
|---|---|
| Worker still preparing | Open application logs; initial image downloads can take time |
| Ubuntu not found | Finish WSL2 Ubuntu installation, create its normal user and select that distribution |
| Docker missing inside WSL | Start Docker Desktop and enable WSL Integration for the selected Ubuntu |
| Docker itself cannot start | Restore Docker Desktop first; do not reset/delete stored images, volumes or virtual disks |
| Missing Python/Git or system prerequisites | Follow the worker dependency checks; interactive sudo installation cannot complete inside a hidden background process |
| Pairing fails | Check the worker-visible address, port, firewall and private-network route |
| Incomplete parameter discovery | Complete fixed parameters, argument bindings or config templates in the page; discovery does not run source to guess dynamic values |
| Project dependency errors | Correct module names and exact package versions in the import configuration, then publish again |
| Experiment stays queued | Check worker connectivity, project installation and resource budgets |
| Task complete but files missing | Wait for pending uploads to reach zero, then refresh details |
| Old agent already running | Stop its original entry and reuse its configuration rather than creating another identity |
| No Check for updates entry | Install 0.3.0-rc.2 or later manually; Ubuntu workers use the package/script procedure |
| Update check or download fails | Check access to GitHub Releases and try again; manual same-role installation remains available |
| Update size or checksum does not match | Do not run that download; retry and use the checksum published with the intended release |
| Update is downloaded but installation is blocked | Finish active work and pending uploads, then retry; check the application logs if service state cannot be verified |
| Center cannot confirm a worker is idle | Check active experiments and pending transfers. If an older Center is blocked only by disconnected nodes, stop its service and manually install 0.4.2 or newer using the original data directory |
