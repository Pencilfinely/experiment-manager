# Connect an unchanged algorithm and distribute it to other computers

[简体中文](EXTERNAL-HARNESS.zh-CN.md) · [Back to README](../README.md)

You already have an algorithm that trains. The manager needs to know **which entry to run, which parameters to pass, where the data is, and where outputs belong**. An external harness records these details outside the algorithm directory and calls the original entry. Your model and training loop do not need SDK calls.

**0.3.0-rc.1 integrates import into the controller page.** Ordinary use does not require a separate import terminal, copying administrator credentials or switching between several files. Use matching new controller and worker versions; refreshing a browser does not upgrade an old agent. Existing deployments can retain their configuration using the [operations guide](OPERATIONS.md).

## Complete the workflow in the page first

1. On the controller computer, open **Experiment Center → Algorithm Projects → Import algorithm**.
2. Choose the original root folder. Do not copy out only a main.py file: preserve the project's module and dataset relationships.
3. Wait for static discovery and inspect entry candidates. The example below uses **src/main.py** with working directory **src**. Discovery does not launch training.
4. Review fixed parameters, experiment presets, datasets, dependencies and resource budgets in the same page. Each preset is a separate experiment configuration; duplicate a formal preset to make a short test.
5. Save and inspect the file preview. Dataset include patterns decide which files enter the package; deselect unnecessary files.
6. Confirm publication, then select workers and deploy from the project card. The software handles transfer, verification, recipient configuration and environment preparation.
7. Wait for successful installation, choose Create experiment, run the short test and then submit the formal preset.

The tables below explain the page's fields. JSON and command-line examples are advanced references, **not a sequence of mandatory manual steps**.
Page imports store independent drafts under the controller data directory's `imports` folder. Discovery and configuration do not modify the original project.
A remote browser cannot choose folders on the controller computer; upload a prepared project ZIP remotely.

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

## Where configuration lives; optional command-line entry

The page generates and edits external configuration for you. For scripts or legacy deployments, Windows packages retain **`Import-Algorithm.cmd`** and Ubuntu uses `bash Import-Algorithm.sh`. Source users can also run:

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

Initial preparation creates `experiments/default.json` plus a `discovery.json` report explaining the inferred settings. **In the page, review and confirm publication directly**; there is no need to edit `reviewed`. Only the standalone command-line workflow requires editing external files, setting `"reviewed": true` in `project.json` and running the build/upload commands.

Discovery reads Python syntax trees. It does not import the project or run `main.py --help`. This SASRec calls `main()` directly at module scope, so importing it could accidentally begin training.

The result is a **draft to review**. Preparing it starts no training and does not treat every inferred value as verified.

## Review discovered values: SASRec example

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

## Separate short and formal experiment presets

The page's default preset contains the discovered defaults and can serve as the formal preset. Add or duplicate a preset for the short test, give it a distinct name and change `epochs` and `star_test`. Each preset ID must be unique.

The equivalent external `experiments/short.json` is shown below; page users do not need to create this file manually:

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

## Publish once, then select recipient workers

**Page workflow: save configuration → inspect the file preview → confirm publication.** The project is now in the library. Select workers and deploy; there is no separate ZIP upload step.

For the standalone command-line/remote ZIP workflow, build manually:

```powershell
python -m expman.harness_project build --project "E:\ExperimentProjects\SASRec" --output "E:\ExperimentPackages\sasrec.zip"
```

The ZIP contains selected source, data, external configuration and experiment presets. Review its file list before sending it to workers.

For a separately generated ZIP, open **“算法项目 / Projects”**, click **“上传项目包 / Upload project”** and choose it. Command-line users can also upload from the **control computer** using its existing configuration:

```powershell
python -m expman.harness_project publish --bundle "E:\ExperimentPackages\sasrec.zip" --hub "http://127.0.0.1:8765" --center-root "E:\ExperimentCenter"
```

After upload, check the recipient nodes under the project and click **“分发到所选节点 / Deploy”**. Upload success means **the controller received the package**. Wait for **“已安装” (installed)** on each selected worker before using it there. A worker downloads and verifies contents, then creates its private source snapshot. Any Git initialization/commits occur inside its installation directory, never in your original research project.

You do not need to repeat Xftp transfers or log into each machine to edit paths. Offline nodes need to reconnect. If a runtime lacks dependencies or is incompatible, fix that environment and retry; receiving a package is not proof that training succeeds.

## Submit a short test, then the formal experiment

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

Select its original root folder in Algorithm Projects and review **entry, arguments, data, dependencies and logs**. Save, publish and deploy; the page workflow generates the external configuration and bundle. If the original entry accepts JSON/YAML, have the harness generate that supported configuration format and pass it through the existing `--config` option; no replacement training API is needed.

Review the generated draft once. Later experiments with the same interface only need different parameter presets. Code, data or invocation changes produce a new immutable package version; earlier experiments retain their original version reference.

This workflow does not add automatic multi-GPU acceleration or automatically find optimal concurrency. Missing native capabilities stay disabled while ordinary single-GPU experiments remain usable.

## What has been verified for this example?

The original `src/main.py` and complete original `Video_Games.test.txt` passed a three-epoch external-harness run on the main computer's RTX 5070 Ti, using real rather than synthetic data. Original project file hashes remained unchanged; model weights, stdout/stderr and extracted metrics were preserved. The additional dependency environment also passed an actual CUDA check. Resume remained disabled.

This is hardware acceptance on the main computer. It does not mean the remote 2080 Ti has been updated or has run this new project. That worker still needs the update entry, project deployment and its own short test.
