# Connect an unchanged algorithm and distribute it to other computers

[简体中文](EXTERNAL-HARNESS.zh-CN.md) · [Back to README](../README.md)

You already have an algorithm that trains. The manager needs to know **which entry to run, which parameters to pass, where the data is, and where outputs belong**. An external harness records these details outside the algorithm directory and calls the original entry. Your model and training loop do not need SDK calls.

**The v0.2.0-rc.2 controller and worker packages include this workflow. Older v0.2.0-rc.1 ZIPs do not include project distribution.** Update both controller and workers. Workers must advertise `project-bundle-v1`; updating the browser alone does not give an old agent package-download or installation support.

For an existing worker, wait until current tasks and uploads finish, then press **Ctrl+C** in the old agent console while leaving Docker running. Extract the new worker ZIP into a new permanent directory. Run **`Update-Worker.cmd`** on Windows or **`bash Update-Worker.sh`** on Ubuntu. It discovers the existing configuration. If several candidates appear, use the menu's node names and full paths to select the old command's `--config` file. Identity, images, policies and records are reused; no WSL/Docker reinstall, re-pairing or manual configuration editing is required. Use this new package's update entry for future starts of the existing node.

## Identify the actual SASRec project

This example uses **`E:\PythonProjects\SASRec_Original`**:

```text
SASRec_Original/
├─ src/
│  ├─ main.py       ← existing training entry
│  ├─ trainers.py
│  ├─ models.py
│  └─ …
└─ data/
   └─ Video_Games.test.txt
```

It runs `python main.py` from `src` and accepts `argparse` options such as `--data_dir`, `--data_name`, `--lr` and `--epochs`. Its example dataset is **one sequence file**, not the train/valid/test three-file layout used by a different implementation.

Earlier documentation targeted **`E:\PythonProjects\IntentPreference\SASRec_Original`**, which has `load_run_config()`, `run_all()` and a dedicated recovery protocol. That [legacy adapter lesson](SASREC-ADAPTATION-WALKTHROUGH.zh-CN.md) does not describe this project.

## Step 1: Generate configuration outside the source

A v0.2.0-rc.2 Windows controller package provides **`Import-Algorithm.cmd`** in its extraction directory; Ubuntu uses `bash Import-Algorithm.sh`. This user's local deployment also has **`local-app/04-Import-Algorithm.cmd`**, a personal entry not included in public source. The Windows launcher opens the import wizard; source users can run it directly:

```powershell
python -m expman.harness_project wizard
```

To follow each stage separately, open PowerShell in the manager source checkout and run:

```powershell
python -m expman.harness_project prepare --source "E:\PythonProjects\SASRec_Original" --output "E:\ExperimentProjects\SASRec"
```

`--source` is your existing algorithm. `--output` is a **separate** directory for external configuration. The following files belong in `E:\ExperimentProjects\SASRec`, outside the algorithm:

| File | What you decide here | When to edit |
|---|---|---|
| `project.json` | Source/data selection, runtime and resource budgets | Initial setup; data or dependency changes |
| `harness.json` | Original entry, parameter bindings, fixed paths, log parsing and native-resume capability | Initial setup; entry/interface changes |
| `experiments/*.json` | Separate learning-rate, seed, epoch and other experiment presets | New experiments or reusable parameter presets |

Initial preparation creates `experiments/default.json` plus a `discovery.json` report explaining the inferred settings. Review these files, change `"reviewed": false` to `true` in `project.json`, and run the same launcher with the same directory again to build. Enter the controller data directory `E:\ExperimentCenter` and its URL to upload, or leave the controller directory blank to produce only a ZIP. The commands below expose the individual wizard stages; you need not type each one during ordinary use.

Discovery reads Python syntax trees. It does not import the project or run `main.py --help`. This SASRec calls `main()` directly at module scope, so importing it could accidentally begin training.

The result is a **draft to review**. Preparing it starts no training and does not treat every inferred value as verified.

## Step 2: Review the discovered values

This SASRec exposes 24 statically readable arguments. Review the following values rather than writing a Python adapter:

| Setting | Value for this example | Why |
|---|---|---|
| `command` | `["{python}", "main.py"]` | Calls the original entry using the task environment's Python |
| `cwd` | `src` | Matches the directory used for manual training |
| Fixed `data_dir` | `{assets.dataset}/` | Uses the recipient's dataset path; preserve the trailing `/` because the entry concatenates strings |
| Fixed `output_dir` | `{output}/outputs` | Writes results into this experiment's output directory |
| Fixed `gpu_id` | `{env.CUDA_VISIBLE_DEVICES}` | Passes the assigned GPU through the argument that overwrites this environment variable |
| Fixed `do_eval` / `no_cuda` | Both `false` | This package trains on a GPU, with evaluation-only and CPU switches disabled |
| Experiment `data_name` | `Video_Games.test` | The original code appends `.txt` |
| Dependencies | `torch`, `numpy`, `scipy`, `tqdm` | Must be installed inside the task image, not merely on the host |
| `resume.supported` | `false` | This implementation saves/evaluates model weights but has no complete training-resume entry |

For example, this portion of `harness.json` fixes paths and GPU selection across experiments:

```json
"fixed_params": {
  "data_dir": "{assets.dataset}/",
  "output_dir": "{output}/outputs/",
  "gpu_id": "{env.CUDA_VISIBLE_DEVICES}",
  "do_eval": false,
  "no_cuda": false
}
```

`bindings` converts values to the original CLI options. Binding `lr` to `--lr` turns an experiment's `lr: 0.001` into `--lr 0.001`. A `store_true` option is included only when enabled; it does not become the invalid `--do_eval False`.

In `project.json`, `source` identifies the original directory, `include` selects source files, and `exclude_directories` skips directories. Data is selected separately under `assets`. For this example, set `assets` to:

```json
"assets": {
  "dataset": {
    "path": "E:\\PythonProjects\\SASRec_Original\\data",
    "include": ["Video_Games.test.txt"]
  }
}
```

This selects the one dataset file instead of distributing everything in `data`. Existing outputs, virtual environments, caches and `.git` should not be included. Local paths are used **only for packaging**. Recipients get package contents and do not need the same Windows drive letters.

`runtime.imports` lists modules to check. Specify additional dependencies using exact `package==version` entries in `runtime.requirements`. A worker first checks its existing verified, digest-pinned environment. If dependencies are missing, it builds an additional environment from the declared requirements, verifies imports and CUDA, and records a new image digest. No manual Docker commands are needed, but unresolved package names/versions still need your input. Set `resources` CPU, RAM and GPU-memory budgets for the actual workload.

This example retains the worker's existing PyTorch/NumPy environment and adds the extra dependencies. Set the `runtime` field in `project.json` to:

```json
"runtime": {
  "imports": ["torch", "numpy", "scipy", "tqdm"],
  "requirements": ["scipy==1.15.3", "tqdm==4.67.1"]
}
```

A native Windows `.bat` entry cannot run directly in a Linux container; select its corresponding Python/bash entry when available.

## Step 3: Separate short and formal experiment presets

The generated `experiments/default.json` contains the discovered defaults and can serve as the formal preset. If renaming it, update both the filename and its `id`; each preset ID must be unique.

Create `short.json` in the same directory with this complete content:

```json
{
  "id": "short",
  "name": "Video_Games - 3 epoch check",
  "params": {
    "epochs": 3,
    "star_test": -1
  }
}
```

`id` identifies the preset, `name` is its display name, and `params` contains experiment values. Only two values are overridden above; the rest come from defaults in `harness.json`. Keep the formal preset's original scientific parameters.

Do not omit `star_test`: the original code saves a checkpoint only once validation begins and loads that checkpoint at the end. Changing 300 epochs to 3 while keeping `star_test=100` can leave no checkpoint to load. This constraint was established by reading this implementation; static discovery cannot guess equivalent training constraints for every algorithm.

## Step 4: Upload once, then select recipient workers

Build the package:

```powershell
python -m expman.harness_project build --project "E:\ExperimentProjects\SASRec" --output "E:\ExperimentPackages\sasrec.zip"
```

The ZIP contains selected source, data, external configuration and experiment presets. Review its file list before sending it to workers.

In **“算法项目 / Projects”**, click **“上传项目包 / Upload project”** and choose the ZIP. Alternatively, on the **control computer**, upload using the existing controller configuration:

```powershell
python -m expman.harness_project publish --bundle "E:\ExperimentPackages\sasrec.zip" --hub "http://127.0.0.1:8765" --center-root "E:\ExperimentCenter"
```

After upload, check the recipient nodes under the project and click **“分发到所选节点 / Deploy”**. Upload success means **the controller received the package**. Wait for **“已安装” (installed)** on each selected worker before using it there. A worker downloads and verifies contents, then creates its private source snapshot. Any Git initialization/commits occur inside its installation directory, never in your original research project.

You do not need to repeat Xftp transfers or log into each machine to edit paths. Offline nodes need to reconnect. If a runtime lacks dependencies or is incompatible, fix that environment and retry; receiving a package is not proof that training succeeds.

## Step 5: Submit a short test, then the formal experiment

1. Confirm that the target worker reports the project installed.
2. Click **“创建实验 / New experiment”**. Under **“节点与实验配置 / Worker and preset”**, choose the target worker's `Video_Games - 3 epoch check`. Review **“本次实验参数 / Parameters”**, then click **“提交实验 / Submit experiment”**.
3. Inspect original logs, exit status and output files. `stdout.log` and `stderr.log` contain original console output; `harness-invocation-1.json` records the first attempt's command and parameters. Once the short run finishes correctly and produces the expected results, submit the formal preset.
4. Change seeds, learning rates or batch sizes in later browser submissions. The harness continues to fill fixed data and GPU paths.

Each task follows this path:

```text
Experiment parameters + fixed configuration
                      ↓
Harness generates command/configuration files
                      ↓
Original main.py runs in an independent working copy
                      ↓
stdout, stderr, original logs and output files are collected
                      ↓
Controller shows status, curves and downloadable results
```

Original source remains unchanged. Relative writes occur in the task's working copy. Using an existing `output_dir` option makes result collection more direct.

## Logs and native resume

**No training-loop edits are required for logs.** Original console text is saved. Rules in `harness.json` extract numeric metrics from stdout, stderr or output log files. This example already prints `epoch`/`rec_avg_loss` for training and `Epoch`/`HIT@10`/`NDCG@10` for evaluation. Discovery suggests fields; inspect a real log line before confirming the matching rule. Validation and test logs use the same format, so example curves are named `eval/HIT@10` and `eval/NDCG@10`; they do not claim to distinguish the two automatically. Without a metric rule, training and raw-log access still work.

**Resume uses only capabilities the original program already has.** It is disabled by default. For a different algorithm with an existing `--resume checkpoint.pt`, explicitly configure its resume command, files to preserve and existing safe-stop mechanism. Confirm that it restores training progress and optimizer state rather than loading weights for inference. This example's `--do_eval` is evaluation, not resume.

## Repeat for another algorithm

Select its source directory, generate a separate external configuration, and review **entry, arguments, data, dependencies and logs**. Then build, upload and deploy. If the original entry accepts JSON/YAML, have the harness generate that supported configuration format and pass it through the existing `--config` option; no replacement training API is needed.

Review the generated draft once. Later experiments with the same interface only need different parameter presets. Code, data or invocation changes produce a new immutable package version; earlier experiments retain their original version reference.

This workflow does not add automatic multi-GPU acceleration or automatically find optimal concurrency. Missing native capabilities stay disabled while ordinary single-GPU experiments remain usable.

## What has been verified for this example?

The original `src/main.py` and complete original `Video_Games.test.txt` passed a three-epoch external-harness run on the main computer's RTX 5070 Ti, using real rather than synthetic data. Original project file hashes remained unchanged; model weights, stdout/stderr and extracted metrics were preserved. The additional dependency environment also passed an actual CUDA check. Resume remained disabled.

This is hardware acceptance on the main computer. It does not mean the remote 2080 Ti has been updated or has run this new project. That worker still needs the update entry, project deployment and its own short test.
