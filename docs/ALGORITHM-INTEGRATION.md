# Algorithm integration: what actually changes?

[简体中文](ALGORITHM-INTEGRATION.zh-CN.md) · [Installation](../README.md)

Separate three activities: adapt an algorithm once, register it on each intended worker, then submit as many experiments as needed. Changing a seed should not require another Docker installation or another manual deployment.

## Start with a program that already trains

The manager does not replace the model, loss, sampling method or optimizer. It needs explicit input and output conventions:

| Previously selected manually | Supplied at execution | How the program receives it |
|---|---|---|
| Learning rate, seed, epoch count | A parameter JSON file | Read `EXPERIMENT_PARAMS` or `Run().params` |
| Dataset location | Read-only registered directories | Use the paths in `Run().assets` |
| Output location | A writable directory for this experiment | Write to `Run().output`, not the source tree |
| Physical GPU | A GPU already selected by the worker | Use logical `cuda:0` inside the container |

If the existing CLI already accepts these values, an external entry wrapper can translate the manager's inputs into its existing arguments. Hardcoded paths and hyperparameters need to become inputs. The browser's `params.lr` does not automatically change an unrelated program's `args.lr`; implement that mapping once.

## Execution, metrics and recovery are separate capabilities

- Basic execution needs a working container command and correctly placed input/output files. Terminal output is recorded automatically.
- Curves require explicit calls such as `run.metric(epoch, loss=loss_value)` where the algorithm produces metrics. Arbitrary printed text is not automatically interpreted as a metric.
- `run.finish({...})` publishes a structured final result.
- Cooperative stop/resume requires a complete checkpoint and algorithm-specific restore logic, including the optimizer and random state as applicable. A wrapper cannot safely invent these semantics.

Import `Run` from `expman.sdk`; the supplied runtime includes it. See the [complete GPU example](OPERATIONS.md#a-complete-small-project) for a runnable basic integration. That example does not implement recovery.

## A compatible SASRec_Original already has an adapter

`expman.adapters.sasrec` connects the manager to the supported implementation, including epoch-boundary checkpoint recovery. You do not need to edit its training loop again.

The supported `src/experiment.py` declares checkpoint schema 3 and `external_sasrec_original_v1`, and exposes configuration loading, the training entry point and the epoch callback. This is not a universal adapter for every repository named SASRec. The importer checks the interface statically; runtime compatibility still needs a short run or smoke test.

### Simplified importer (new in the source checkout, not in the v0.2.0-rc.1 ZIPs)

After setting up a worker, exit its console. In the **experiment-manager source checkout inside Ubuntu**, run:

```bash
python3 -m expman.sasrec_setup
```

Answer two prompts:

1. Select `SASRec_Original` itself: the directory immediately containing `src`. On WSL, pasted Windows paths such as `E:\...\SASRec_Original` are accepted.
2. Select the research configuration, relative to that algorithm directory. Enter defaults to `config/video_games_full.json`.

The importer reads `dataset` and `data_root` from that configuration and finds the three train/valid/test files. Relative data paths use the algorithm directory as their base, matching this implementation's own behavior.

It copies Python sources into a private snapshot, creates a Git commit in that copy, registers a content-based data version and generates all task wiring. Your original project need not be a Git repository or have a clean working tree. Original files, existing outputs and Git history are left untouched.

Two templates are generated: `sasrec-video-games-short` and `sasrec-video-games-formal`. The formal template preserves the configured research parameters; the short template changes only `epochs=3` and `star_test=-1`. GPU selection belongs to the worker, and the container's logical GPU index is 0.

Importing submits no training. Start the worker, choose its short template in the controller, keep the parameter grid at `{}`, then submit once. Check training, validation, test results and completed uploads before selecting the formal template.

The default budget is CPU 2, RAM 8192 MiB and GPU 6000 MiB, with exclusive GPU use. These are starting values for the compatible SASRec workflow, not capacity guarantees or advance memory allocation. Adjust them in the task editor based on measurements; insufficient node budgets cause the task to wait.

When source or parameters change, stop the worker, import again and restart it. Existing experiments retain their original snapshots; new templates use the new version. Do not edit the private snapshot manually. Use `--profile NAME` if the node has multiple verified environments, or `--name NAME` to keep separate configurations as different projects.

## Running it on another machine

Algorithm adaptation is reusable, but every target node still needs the intended code, data and compatible runtime. The current importer registers the node on which it runs; it does not distribute projects to every worker.

A template imported on your main computer selects that computer. Changing only its node tag does not make the source whitelist, local image and dataset registrations valid on another node. Cross-node distribution is deployment work, not a change to the model. Automatic multi-GPU training is also not implemented.
