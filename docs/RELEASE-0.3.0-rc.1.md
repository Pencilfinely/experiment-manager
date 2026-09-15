# 0.3.0-rc.1 · Desktop preview / 桌面预览版

## 中文

把管理端、算法导入和节点部署收进日常软件操作。Windows 管理端与算力端仍然独立，自己调度自己的电脑需要同时安装两个版本。

- 两个 Windows Setup.exe 安装程序，按当前用户安装；也提供可直接解压运行的 ExperimentCenter.exe、ExperimentWorker.exe。
- 实验台统一侧栏：总览、实验、算力、算法项目、设置。本机打开自动登录，管理服务和算力代理可在后台运行。
- 页面内选择原算法根目录、静态识别入口、检查参数/数据/依赖、保存实验预设、发布和分发；保留源码不变。
- 算力端导入管理端提供的配对文件。Windows 选择 Ubuntu 后在后台准备；Ubuntu 安装入口后台运行，优先使用用户级 systemd。
- 现有节点继续使用原身份、配置、镜像和记录；升级前先停止旧服务/代理，再从托盘退出旧客户端。安装器不会覆盖仍运行的同角色客户端，不提供热升级；不同版本的运行程序也不会因网页更新自动升级。

公开资产为三个应用 ZIP、两个 Windows 安装程序和校验信息。公开包不包含个人算法、研究数据或凭证。

分发目前通过管理端传输固定代码/数据包，再由节点建立私有 Git 快照和 Docker 环境；尚无直接连接 GitHub/GitLab 账号的项目导入界面。
本版本未增加多卡合跑一个实验、自动选择最快资源分配或原算法没有的续训功能。
Windows 仍依赖用户登录后的 WSL/Docker；后台运行不会跨越关机或睡眠。Ubuntu 无登录运行取决于系统用户服务和 lingering 配置，安装器不擅自修改这些系统策略。
安装程序尚未代码签名，也不提供自动更新；请从项目 Release 下载并核对校验文件。

安装与使用：[中文 README](../README.zh-CN.md) · [日常操作](OPERATIONS.zh-CN.md) · [原算法接入示例](EXTERNAL-HARNESS.zh-CN.md)。

## English

This preview puts controller operation, algorithm import and worker setup into application workflows. Windows controller and worker remain separate; install both to manage and compute on the same computer.

- Two per-user Windows Setup.exe installers, plus portable ExperimentCenter.exe and ExperimentWorker.exe applications in complete ZIPs.
- One controller sidebar for Overview, Experiments, Compute, Algorithm Projects and Settings. Local launch signs in automatically; controller and worker can run in the background.
- Choose an original algorithm root folder in the page, discover entries statically, review parameters/data/dependencies, save experiment presets, publish and deploy. Original source remains unchanged.
- Workers import a controller-provided pairing file. Windows selects Ubuntu and prepares in the background; Ubuntu installs a background worker, preferring user-level systemd.
- Existing nodes retain identity, configuration, images and records. Before upgrading, stop the old service/agent and exit its tray client. The installer does not replace a running same-role client or perform hot upgrades. Updating the page alone does not upgrade an old worker program.

Public assets include three application ZIPs, two Windows installers and checksums. User algorithms, research datasets and credentials are excluded.

Distribution currently transfers fixed code/data bundles through the controller and prepares private Git snapshots and Docker environments on recipients. Direct GitHub/GitLab account import is not included.
This version does not add one-experiment multi-GPU training, automatic fastest-allocation selection or resume that the original algorithm lacks.
Windows still requires the user's WSL/Docker session; background operation does not survive power-off or sleep. Ubuntu operation without login depends on system user-service/lingering settings, which the installer does not silently change.
Installers are not code-signed and automatic updates are not included. Download from this project's Releases and check the published checksums.

[English README](../README.md) · [Everyday operations](OPERATIONS.md) · [Unchanged algorithm example](EXTERNAL-HARNESS.md).
