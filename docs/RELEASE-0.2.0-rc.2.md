# Experiment Manager 0.2.0-rc.2

Algorithms can now use their existing entry points through a separate, configurable harness. You no longer need to write a Python adapter for each algorithm or manually copy each project to every worker.

- Import wizard discovers entry candidates and literal argparse defaults without importing or executing the algorithm.
- Separate project, invocation and per-experiment JSON files control inputs, fixed arguments, experiment parameters, console/file metrics and optional native resume.
- Projects upload once to the controller; selected workers download, verify and install immutable source/data snapshots in the background.
- Missing explicitly pinned dependencies are installed in an additional image derived from the worker's verified runtime, followed by a real assigned-GPU CUDA check.
- The browser selects an installed project preset, edits its parameters and submits directly to the selected node.
- Existing workers can use `Update-Worker.cmd` / `Update-Worker.sh` with their original configuration. Software updates preserve enrollment and resource policy.

The corrected example is `E:\PythonProjects\SASRec_Original/src/main.py`. A real three-epoch test used its original single `Video_Games.test.txt` dataset and produced logs, metrics and model weights with original file hashes unchanged. Earlier documentation described a different implementation under IntentPreference; it is now explicitly marked as legacy.

This remains a preview: dependency versions, ambiguous arguments, log meanings and data selection need review. Windows batch scripts require an equivalent Python/bash entry for Linux GPU containers. The harness only exposes native resume when explicitly configured; it does not invent missing resume or distributed training support. Current scheduling remains one GPU per task. Old worker software must be updated before automatic project delivery is available.

## 中文

现在可以保留算法已有入口，通过外置配置接入实验台，不再要求每个算法编写 Python 适配器或逐台手工复制项目。

- 双击主控包的 `Import-Algorithm.cmd`，静态提取入口和参数；检查外部配置后打包上传。
- `project.json` 管代码、数据和依赖，`harness.json` 管入口和日志，`experiments/*.json` 分别保存每个实验。
- 实验台上传一次，勾选节点分发；节点后台校验、安装，自动生成可直接选择的实验配置。
- 缺少的明确版本依赖自动补入独立镜像，并实际检查指定 GPU 的 CUDA 运算。
- 已有节点用 `Update-Worker.cmd` / `Update-Worker.sh` 复用原配置更新，不必重装 WSL、Docker 或重新配对。

真实原版 SASRec 的三轮完整数据短实验已验证，保留原始日志、指标和模型文件，原源码哈希不变。该源码没有原生续训入口，因此不会提供虚假的恢复功能。旧文档引用另一份 IntentPreference 实现的问题已更正。

三个应用 ZIP 仍分为 Windows 主控、Windows 算力和 Ubuntu 算力；算法与私有数据不包含在公共安装包内。
