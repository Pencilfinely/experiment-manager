# Experiment Manager 0.4.1

修复算力端更新后出现“客户端 0.4.0、后台 0.3.0rc5”的问题，保留 0.4.0 的实验矩阵、算法导入、调度、结果导出和项目清理功能。

## 修复

- **停止状态也更新后台代码。** 此前 Windows 算力端更新时，如果后台本来就没有运行，安装器只安装 Windows 客户端，WSL 后台要等下次点击“启动后台代理”才会同步。现在安装器会直接部署并核对新版 WSL 代码，保留原 Ubuntu、节点身份、资源策略和数据目录。原本停止的后台继续保持停止，不自动启动实验。
- **显示真实运行状态。** 已停止的代理不再把上次进程的版本当成当前运行版本。界面分别显示已安装后台版本和上次运行版本；后台运行时显示实际进程报告的版本。上次运行版本仍为旧版属于历史信息。
- **更新结果有明确校验。** 只有 WSL 已安装版本与安装包一致、节点配置未变且后台保持停止，安装器才继续完成客户端安装；失败时显示原因，不把仅更新客户端当成全部完成。

## 更新

Windows 用户可在“检查更新”中安装 0.4.1，或下载同角色的 `Setup.exe` 手动安装。0.4.0 客户端也能直接升级到本版。请先完成实验和文件回传；保留原配置和数据目录，不需要重新配对。

如果暂时仍使用 0.4.0，点击“启动后台代理”也会先安装当前包中的后台代码，再启动代理。仅关闭或重新打开状态窗口不会执行这一步。

本次主要修复算力端；同时提供管理端和 Ubuntu 算力端包，便于统一版本。下载 `windows-worker-x64-Setup.exe` 用于 Windows 算力端，`windows-controller-x64-Setup.exe` 用于管理端。`SHA256SUMS.txt` 提供五个应用文件的校验值。

## English

Fixes Windows worker updates leaving the WSL backend on an older version when the agent was stopped. The installer now deploys and verifies the new backend without starting experiments, preserving the existing node configuration and startup backend. Status distinguishes installed software, the last process version and the version of a currently running process. Upgrade directly from 0.4.0 using Check for updates or the matching installer; existing pairing and experiment data are retained.
