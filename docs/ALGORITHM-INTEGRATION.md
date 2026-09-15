# Connect an algorithm and run it on another computer

[简体中文](ALGORITHM-INTEGRATION.zh-CN.md) · [Worker installation](../README.md)

**The real flow: browser parameters → worker receives a task → container runs your training entry → logs, metrics and model files return to the browser.** Connecting the entry and delivering code/data are separate steps.

**There is currently no browser feature that uploads an algorithm and synchronizes it to every worker.** Project registration applies to the machine performing it. Each recipient needs source, data and a compatible runtime.

The SASRec transfer tool below is new in the current source, **not included in the published v0.2.0-rc.1 installation ZIPs**. Generated project packages include their own installer, so importing does not require replacing the recipient's existing worker software. Public software packages do not include your research source or datasets.

## 1. What actually changed for the supported SASRec_Original?

Expected layout (`data` is a sibling of `SASRec_Original`, matching `data_root: "../data"`):

```text
SASRec_Original/
├── src/
│   ├── main.py
│   ├── experiment.py
│   ├── datasets.py
│   ├── models.py
│   ├── modules.py
│   ├── trainers.py
│   └── utils.py
└── config/video_games_full.json
data/Video_Games/
├── Video_Games.train.txt
├── Video_Games.valid.txt
└── Video_Games.test.txt
```

The ordinary training command is:

```bash
python src/main.py run-all --config config/video_games_full.json --run-id manual-001 --resume never
```

The managed template invokes this instead; you do not type it for every submission:

```bash
python -m expman.adapters.sasrec --project /workspace/code/SASRec_Original
```

The added entry is [expman/adapters/sasrec.py](../expman/adapters/sasrec.py). It converts task parameters into `sasrec-input.json`, passes that file to the original `load_run_config()`, maps the dataset to `data_root`, maps the run's writable directory to `output_root`, and calls the original `run_all()`.

The worker chooses a physical GPU, exposed as logical CUDA device 0. The adapter binds the original post-epoch hook to metric reporting and full checkpoint publication. Stop takes effect after a complete saved epoch; resume uses the original restore implementation. Model, loss, optimizer and data processing remain in the algorithm project.

This particular implementation already has configuration loading, full checkpoints and an epoch hook. Schema 3 and `external_sasrec_original_v1` identify that existing interface. Adding those constants to an unrelated SASRec does not implement the interface; arbitrary algorithms are not automatically compatible.

## 2. Transfer this SASRec project to another worker

### Step 1: Export on the computer holding source and data

In an **Ubuntu/WSL terminal at the current experiment-manager source root**:

```bash
python3 -m expman.sasrec_package export \
  --project /mnt/e/Research/SASRec_Original \
  --config config/video_games_full.json \
  --output ./SASRec-Video_Games-v1.zip
```

Replace `--project` with your algorithm folder. `--config` is relative to it; `--output` is the ZIP to send. Windows Python can also export using Windows paths. Existing output files are not overwritten; use `v2.zip` for the next export.

Success prints the path, size and SHA256. The ZIP contains Python source under `src`, research config and three dataset splits. It excludes old results, original Git history and sender node credentials. The original project need not be committed and is not modified. Only data/output paths are relocated in the packaged config; scientific parameters are preserved.

### Step 2: Send the ZIP with Xftp

For a Windows recipient:

1. Open the existing SSH/SFTP connection. Find the ZIP on the left.
2. On the right, enter `/E:/ExperimentTransfer`, drag the ZIP across and wait for completion.
3. On the recipient desktop, open `E:\ExperimentTransfer` and select **Extract All** on the ZIP.

Xftp `/E:/ExperimentTransfer`, Windows `E:\ExperimentTransfer` and WSL `/mnt/e/ExperimentTransfer` refer to the same directory. Substitute the recipient's actual drive if different. Do not run the installer from ZIP preview.

For native Ubuntu:

1. Run `mkdir -p "$HOME/ExperimentTransfer"` and `echo "$HOME/ExperimentTransfer"` on the server.
2. Use Xftp to upload the ZIP into the printed directory.
3. In the server terminal:

```bash
cd "$HOME/ExperimentTransfer"
python3 -m zipfile -e SASRec-Video_Games-v1.zip SASRec-Video_Games-v1
cd SASRec-Video_Games-v1
```

### Step 3: Install the project on the recipient

A paired worker with a verified GPU runtime must already exist. A project package does not perform initial worker installation.

1. Start Docker. Wait for that node's current jobs and pending uploads to finish, then close its worker window.
2. Windows: double-click **`Install-Project.cmd`** in the extracted folder.
3. Ubuntu: run **`bash Install-Project.sh`** there.
4. If prompted, choose the worker's usual WSL distribution. For multiple configs, select the actual file following `--config` in the worker startup command; do not guess from the list number.

The installer checks that the recipient's image imports PyTorch, NumPy and the SASRec adapter. It installs source/data locally and generates templates using **the recipient's own image, paths and tags**. It preserves identity and previous projects, and backs up the previous config.

Success prints:

```text
INSTALLED / 安装完成: sasrec-video-games-short, sasrec-video-games-formal
```

For a custom or legacy setup, run in the extracted folder's Ubuntu terminal:

```bash
python3 Install-Project.pyz install --node-config /absolute/path/to/node.ready.json
```

Replace the path with the config used by that recipient's existing agent. No sender config or credential needs to be copied.

### Step 4: Restart that worker and submit

1. Restart using its **usual launcher and config**, without pairing again.
2. Refresh the controller and locate **that node's card**.
3. Click **`Use: sasrec-video-games-short`** under it.
4. Leave the parameter grid at **`{}`** and click **Submit to queue**.
5. Check the node and epoch logs in task details; wait for success and complete artifact upload.
6. Then use the **`sasrec-video-games-formal`** template under the same node.

The short template changes only `epochs` to 3 and `star_test` to -1. The formal template preserves research settings. Both use the worker-selected GPU as logical device 0.

Task defaults are CPU 2, RAM 8192 MiB, GPU 6000 MiB and exclusive use of one card. Insufficient node budgets cause queuing. These are scheduling budgets, not memory preallocation or a guarantee that any model configuration fits. Perform a short run on every new hardware target.

### What requires another transfer?

| Change | Action |
|---|---|
| Only task parameters such as seed or learning rate | Edit them in the browser; no new package |
| Source or data changed | Export a new ZIP, transfer, wait for work/uploads to finish, close agent, install, restart, use new template |
| Another computer joins | Install/pair the worker, install the same project ZIP, perform a short run |
| New Python package or CUDA extension | Update and verify that node's image first; the project installer does not infer dependencies |

Submitted tasks retain their original snapshots. Saving a source file does **not** update other nodes. Synchronization currently means explicit export, transfer and installation.

## 3. Adapting an algorithm without an existing adapter

First make an ordinary training command accept parameters, data and output directories:

```bash
python train.py --data-dir ./example-data --output-dir ./manual-run --lr 0.05 --epochs 20 --seed 42
```

If those values are hardcoded, turn them into inputs first. Write models, logs and derived caches under the output directory; source and original datasets are read-only in managed containers. GPU programs use the assigned logical `cuda:0`.

### Run a complete example

[examples/managed-project](../examples/managed-project) contains:

```text
train.py          Ordinary training program, independent of the manager
expman_entry.py   Added managed entry
check_local.py    Compares direct and managed results
example-data/train.csv
```

It fits a line with standard Python to demonstrate the entire interface without a GPU. It is not a GPU benchmark or SASRec implementation. From the software source root:

```bash
python3 examples/managed-project/check_local.py
```

Use `python` on Windows if appropriate. Success prints:

```text
PASS: identical model and results; managed metrics/result emitted; unsupported resume rejected
```

### How the extra file invokes the original program

Read the complete [expman_entry.py](../examples/managed-project/expman_entry.py). Its central mapping is:

```python
run = Run()
data_dir = next(iter(run.assets.values()))
command = [sys.executable, str(Path(__file__).with_name('train.py')),
           '--data-dir', str(data_dir), '--output-dir', str(run.output),
           '--lr', str(run.params.get('lr', 0.05)),
           '--epochs', str(run.params.get('epochs', 20)),
           '--seed', str(run.params.get('seed', 42))]
subprocess.run(command, check=True)
```

Use the linked full file to run this. `Run()` reads the current task's parameters and paths. The example requires exactly one dataset; multi-input algorithms should select assets by ID.

Browser parameters `{"lr": 0.01, "epochs": 100, "seed": 7}` become CLI arguments `--lr 0.01 --epochs 100 --seed 7`. The browser does not infer how arbitrary algorithms name their arguments.

To adapt your own project:

1. Place a copy of this extra file beside your ordinary entry.
2. Replace `train.py` with your actual entry filename.
3. Map fields to your CLI arguments. If the CLI calls it `--learning-rate`, use that spelling; the task field can still be `lr`.
4. Update accepted parameter names and defaults at the top of the wrapper.
5. Replace the `summary.json` reader with your actual result format and call `run.finish(result)`. The example really writes this file; other projects may not.

The managed command becomes **`python expman_entry.py`**. A wrapper alone works when the original CLI accepts all inputs; otherwise remove hardcoded paths/parameters first.

### Logs, curves and resume are different capabilities

- Inherited stdout/stderr sends ordinary `print()` output to the browser log.
- The example reports a single final loss point. For per-epoch curves, call `run.metric(epoch, loss=float(loss))` when the value is computed, or expose an `on_epoch` callback for the wrapper. Arbitrary log text is not parsed into curves.
- Files written under `run.output` are archived by the worker.
- Resume also needs complete model, optimizer, epoch, RNG and applicable sampler/scheduler/AMP state. Weights alone are insufficient. This example explicitly rejects resume rather than silently restarting. The supported SASRec implementation already provides full checkpoints.

## 4. Register a general algorithm on multiple nodes

The SASRec transfer tool accepts only its supported interface. Other algorithms currently require transferring the **adapted project** and **dataset** to each node and registering them there.

For the complete example, run in the **recipient's Ubuntu terminal**, starting at the software source root:

```bash
mkdir -p "$HOME/ExperimentProjects/linear-example" "$HOME/ExperimentData/linear-example"
cp examples/managed-project/train.py examples/managed-project/expman_entry.py "$HOME/ExperimentProjects/linear-example/"
cp examples/managed-project/example-data/train.csv "$HOME/ExperimentData/linear-example/"
cd "$HOME/ExperimentProjects/linear-example"
git init
git add train.py expman_entry.py
git -c user.name=ExperimentUser -c user.email=local@example.invalid commit -m "Record runnable training entry"
```

These Git commands record a version only in the new example directory, without uploading to GitHub. For your actual algorithm, transfer adapted source and data with Xftp into the respective folders, and commit only intended files in its repository.

Close the worker. Open **`Configure-Project.cmd`** from its Windows installation, or run **`bash Configure-Project.sh`** from its Ubuntu installation. Answer:

| Prompt | Example answer |
|---|---|
| Project name | `linear-example` |
| Local Git repository folder | `~/ExperimentProjects/linear-example` |
| Training command | `python expman_entry.py` |
| Dataset folder | `~/ExperimentData/linear-example` |

These launchers use the default worker data directory. For a custom installation, run `python3 -m expman.project_setup --root PATH` from the software source root, using the directory containing `node.ready.json`. The generic wizard does not support other config filenames; do not casually rename active configs.

Registration selects an existing verified environment; it does not install algorithm dependencies. This standard Python example needs no extra packages. Host Conda packages are not automatically available in the container, and training containers have no network for ad hoc downloads.

Restart the worker, use its `linear-example` template, set task `params` to `{"lr": 0.05, "epochs": 20, "seed": 42}`, leave grid `{}`, and submit. The example uses CPU, although the current generic Docker scheduler still reserves a GPU slot; it is not a GPU throughput test.

On each additional node, repeat transfer, registration and restart, then select the template under **that node**. Later parameter-only changes need no transfer.

Publishing source to GitHub does not distribute datasets, install runtime dependencies or register nodes. Current templates contain node-specific paths and environments. Central browser publication, automatic distribution, one portable template across all compatible nodes, and automatic conversion to multi-GPU training are not implemented.
