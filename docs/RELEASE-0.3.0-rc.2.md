# 0.3.0-rc.2 · Application updates / 应用内更新

## 中文

Windows 的 Experiment Center 和 Experiment Worker 现在可以在应用内检查、下载并安装新版，并使用已定稿的独立应用图标。

- 在应用状态窗口或托盘菜单选择**检查更新**，查看当前版本、新版本及发布说明。
- 从本项目 GitHub Releases 下载同角色 `Setup.exe`，检查文件大小和 SHA-256；校验成功后才允许安装。
- 实验运行时可以先下载暂存。点击安装后检查实验、待回传和服务状态，安全停止服务/代理并退出旧客户端，再由新版安装器完成升级；忙碌时稍后重试。
- 保留原数据目录选择、Ubuntu、节点配置和登录启动选项。更新前运行中的算力代理在更新后自动重启；原本停止的保持停止。两端独立更新，同一电脑装了两端时分别操作。
- 预览版可接收更新的预览版和正式版，正式版只接收正式版。检查、下载和安装由你主动操作，不提供后台静默升级。
- 新的 Center / Worker 图标用于 Windows 应用、窗口、托盘及安装程序，Center 网页同步使用新图标。
- 管理界面的桌面侧栏支持收起与展开，并记住选择。

### 如何升到本版

**多机环境先更新 Worker，恢复在线并完成同步，再更新 Center。** Center 应用内安装需要每个算力端提供最新空闲确认；离线或旧版代理无法提供确认、状态未知时会阻止安装，处理后需手动重试。

**0.3.0-rc.1 及更早版本没有应用内更新入口，首次需要手动安装本版。**等实验与回传完成，停止旧服务/代理并从托盘退出旧客户端，再安装同角色新版。保留原数据目录和节点配置；安装后即可检查后续更新。

Windows 管理端下载 `windows-controller-x64-Setup.exe`，Windows 算力端下载 `windows-worker-x64-Setup.exe`。同时管理实验和提供显卡的电脑需要安装两端。也提供完整 Windows ZIP。
原生 Ubuntu 算力端下载 `ubuntu-worker-x64.zip`，继续使用已有脚本或手动升级；本次应用内更新面向 Windows。

公开资产包括三个应用 ZIP、两个 Windows 安装程序和校验信息，不包含用户算法、研究数据或凭证。安装程序尚未代码签名；请使用本项目 Release 资产。升级前建议备份数据目录。

[中文安装说明](https://github.com/Pencilfinely/experiment-manager/blob/v0.3.0-rc.2/README.zh-CN.md) · [更新与日常操作](https://github.com/Pencilfinely/experiment-manager/blob/v0.3.0-rc.2/docs/OPERATIONS.zh-CN.md)

## English

Experiment Center and Experiment Worker for Windows now check, download and install application updates, and use their finalized, distinct application icons.

- Choose **Check for updates** in the application status window or tray menu to review your current version, the available version and its release notes.
- Download the same-role `Setup.exe` from this project's GitHub Releases. Installation is enabled only after its published size and SHA-256 are verified.
- Download while experiments continue and install later. Installing checks experiments, pending uploads and service state, safely stops the service/agent, exits the old client and hands over to the new installer. Retry later when work prevents a safe stop.
- Retain the saved data-directory selection, Ubuntu distribution, node configuration and login-startup preference. A running worker agent restarts after the update; a stopped agent remains stopped. Each application updates its own role; update both when both are installed.
- Preview versions accept newer previews and stable releases; stable versions accept stable releases only. You initiate checking, downloading and installation; unattended background upgrades are not included.
- New Center / Worker icons appear in Windows applications, windows, trays and installers, with the Center icon also used by its web page.
- The desktop sidebar can collapse or expand and remembers the selection.

### Upgrade to this release

**In multi-computer deployments, update workers first, let them reconnect and complete synchronization, then update Center.** Center's in-app installation requires a fresh idle confirmation from every worker. An offline or older agent with unknown status blocks installation; resolve its status and retry manually.

**0.3.0-rc.1 and earlier have no in-app update entry: install this release manually once.** Finish experiments and pending uploads, stop the old service/agent and exit its tray client before installing the same-role package. Keep your existing data directories and node configuration. Subsequent Windows updates can use the new application workflow.

Download `windows-controller-x64-Setup.exe` for a Windows controller or `windows-worker-x64-Setup.exe` for a Windows worker. Install both to manage and compute on the same computer. Complete Windows ZIPs are also available.
Native Ubuntu workers use `ubuntu-worker-x64.zip` and the existing script/manual upgrade procedure; this in-app update feature targets Windows.

Public assets include three application ZIPs, two Windows installers and checksums. User algorithms, research datasets and credentials are excluded. Installers are not code-signed; use this project's release assets. Back up your data directories before upgrading.

[English installation guide](https://github.com/Pencilfinely/experiment-manager/blob/v0.3.0-rc.2/README.md) · [Updates and everyday operations](https://github.com/Pencilfinely/experiment-manager/blob/v0.3.0-rc.2/docs/OPERATIONS.md)
