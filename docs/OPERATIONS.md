# Operations and algorithm integration

[English](OPERATIONS.md) · [简体中文](OPERATIONS.zh-CN.md) · [Install first](../README.md)

## A complete small project

This example performs real GPU calculations with synthetic inputs. It is useful
for learning the manager, not for evaluating a research model. On the **worker's
Ubuntu terminal**, create your own Git repository:

```bash
mkdir -p ~/my-gpu-example
cd ~/my-gpu-example
git init
cat > train.py <<'PY'
import torch
from expman.sdk import Run

run = Run()
torch.manual_seed(int(run.params.get('seed', 42)))
model = torch.nn.Linear(16, 1).cuda()
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
x = torch.randn(256, 16, device='cuda')
y = x.sum(dim=1, keepdim=True)
for step in range(1, int(run.params.get('steps', 20)) + 1):
    optimizer.zero_grad()
    loss = (model(x) - y).square().mean()
    loss.backward()
    optimizer.step()
    run.metric(step, loss=loss.item())
torch.save(model.state_dict(), run.output / 'model.pt')
run.finish({'status': 'succeeded', 'loss': loss.item()})
PY
git add train.py
git -c user.name=Example -c user.email=example@localhost commit -m 'Add GPU example'
```

Exit the worker console, run the **Configure-Project** launcher, and answer:

| Prompt | Answer |
|---|---|
| Project name | `my-gpu-example` |
| Repository folder | Your actual `/home/YOUR_LINUX_USER/my-gpu-example` folder |
| Command | `python train.py` |
| Dataset folder | Press Enter; this example generates inputs |

Restart the worker, fill its `my-gpu-example` template on the controller, and set
`params` to `{"seed":42,"steps":20}`. Leave the parameter grid `{}` and submit once.
When it finishes, the detail page contains the loss trace, `model.pt` and
`result.json`. This sample does **not** implement checkpoint resume; do not use it
to validate training recovery.

For two separate seeds, put `{"seed":[42,43]}` in the parameter-grid box. This creates
two immutable experiments. Changing a task before submission affects that new
task; editing a local template does not rewrite an already submitted experiment.

## Connect your own SASRec project

The adapter expects a user-provided compatible implementation with
`src/experiment.py`, `datasets.py`, `models.py`, `modules.py`, `trainers.py`,
`utils.py` and `main.py`, including the structured callbacks/checkpoint interface
described in the adapter. It is not a universal adapter for every repository named
SASRec. See `expman/adapters/sasrec.py` and `scripts/sasrec_first_run.py` in the
source repository for the expected interface and deployment helper.

Suppose your committed Git root contains `SASRec_Original/src/…`, and your data
folder contains:

```text
datasets/
  Video_Games/
    Video_Games.train.txt
    Video_Games.valid.txt
    Video_Games.test.txt
```

1. Use Configure-Project with name `sasrec-video`, that Git root and the **datasets**
   folder (the parent of `Video_Games`).
2. Enter the command:
   `python -m expman.adapters.sasrec --project /workspace/code/SASRec_Original`.
3. Restart the worker and fill its template on the controller.
4. Set `algorithm` to `SASRec`, `metric_protocol` to `external_sasrec_original_v1`,
   and copy the scientific parameters from **your own research configuration**
   into `params`. Replace external input/output path fields with
   `"dataset":"Video_Games"`, `"data_asset":"sasrec-video-data-v1"`,
   `"device":"cuda"`, `"gpu_id":0`. The adapter manages output paths.
5. First submit a separately named short task with `epochs:3` and `star_test:-1`
   to exercise training, validation and test. Use measured/conservative resource
   budgets; a budget does not allocate that much VRAM in advance.
6. Check logs, result files and resource peaks. Then submit a new formal task with
   the original scientific settings restored. Do not carry tiny smoke-test model
   dimensions into the formal experiment.

The versioned source-pack and dataset-pack helpers in `scripts/` are optional
tools for deployment owners. They build **private** transfer packages from
user-supplied inputs; those packages do not belong in a public release.

If your project needs additional Python/system packages, build and validate an
appropriate Docker image before registering it as a profile. The bundled runtime
is PyTorch 2.7.1 with CUDA 12.8; it is not an automatic dependency solver. Different
GPU architectures must pass actual container checks rather than inheriting
another card's verification statement.

## Resource allocation

The automatic worker setup enables discovered GPUs with one job slot each and
sets the node's total running limit to **one**. This establishes a clear baseline.
It does not mean the machine has only one GPU.

For advanced deployments, the settings are in the worker data directory's
`node.ready.json`. Stop the worker before changing settings and keep a backup.
`policy.max_running` limits the whole node; `gpu_policy[UUID].max_jobs` limits each
card. `max_jobs:0` disables a card for new tasks. Tasks also have
`resources.exclusive`: `true` prevents another managed task sharing that card.
CPU, RAM and VRAM admission checks must all pass. Do not change scientific
parameters merely to make a scheduling budget fit.

Sharing a card requires both `exclusive:false` on participating tasks and a
per-card limit greater than one. Running separate tasks across two cards requires
a node limit of at least two and enough total CPU/RAM. Compare per-experiment
completion times with the single-task baseline. Low VRAM usage does not prove
that two tasks will finish faster together. There is no automatic DDP/FSDP or
cross-machine training in this release.

## Stop, resume and disconnection

Use the experiment's **请求保存并停止** button for a cooperative stop. The worker must
be online to receive the command. An adapter with complete checkpoints can stop
at a safe boundary and later resume as a new attempt. Generic code that does not
check the SDK stop signal cannot promise a saved training state.

**暂停接单** pauses assignment/start of new tasks; it does not kill a running
experiment. Losing contact with a worker does not silently send its job to
another machine. Once a task has cached its source, image and assets, it can
continue through a controller outage. Leave the worker running to reconnect and
finish uploading.

## Troubleshooting

| What you see | What to do |
|---|---|
| No WSL distribution | Install WSL2 Ubuntu, open it once and create a normal user. Restart Windows if installation requests it. |
| `docker: command not found` in WSL | Start Docker Desktop and enable integration for the distribution selected by the launcher. |
| Docker permission denied on native Ubuntu | Configure your ordinary user's Docker access; sign out/in if group membership changed. Do not start the worker as root. |
| Cannot pair with controller | Open the exact controller URL from that worker. Check that the controller is running, its IP/port are correct, and firewall/VPN routes allow it. Use controller 0.2 or newer. |
| Image download fails or times out | Check that Docker can reach the image registry. Fix Docker's proxy/network configuration and rerun the launcher; do not weaken digest checks. |
| GPU count/UUID/CUDA check fails | Inspect `setup.log`; check driver and GPU-container support. On WSL, CUDA UUID masking is applied in addition to Docker selection. Unsupported runtimes are not marked verified. |
| Task remains queued | Check the worker's tags, source whitelist, data asset IDs, verified image/profile, GPU slots and resource budgets. |
| Training tries to write into source | Set its output folder to `EXPERIMENT_OUTPUT` or use `Run().output`. Source/data mounts are read-only. |
| Task says succeeded but files are missing | Keep controller and worker running until pending uploads are zero, then refresh the task. |
| Directory already in use | Reopen the existing worker/controller window; do not start a second agent with the same data directory. |

Official prerequisites:
[WSL installation](https://learn.microsoft.com/windows/wsl/install),
[Docker Desktop WSL integration](https://docs.docker.com/desktop/features/wsl/),
[Docker on Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

## Updates, backups and removal

The application directory contains replaceable program files. Controller and
worker data directories contain identities, settings, databases and outputs.

For this preview, finish/drain work and wait for uploads before an upgrade. Stop
the process, back up its **whole data directory**, extract the newer matching
edition to a new application folder, and start it. The launcher uses the same
default data directory. A changed worker version rechecks its runtime before
enabling work. Keep the old application and backup until the upgrade is accepted.
Custom GPU/concurrency policy should be reviewed after reconfiguration; the
installer returns its automatically managed defaults to one task per node.

To back up a controller safely, stop it before copying its directory so the
SQLite database and WAL state are captured together. Back up worker data when
its process has stopped and training is complete. Do not two-way-sync live
databases or WSL virtual disks through a cloud drive.

To remove the application, stop it and delete its extracted program folder.
Persistent data remains intentionally. Remove data or Docker images/volumes only
after verifying you no longer need their experiments/checkpoints. The preview
does not install an automatic-start service. The optional Windows firewall rule
is named `ExperimentManager-PORT-WORKER_IP` and can be removed in Windows Defender
Firewall when that worker no longer needs access.
