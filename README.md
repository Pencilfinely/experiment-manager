# ExLab upgrade compatibility channel

**Status: migration installers have passed local compatibility checks; publication is pending authorization. No migration release is available yet.**

**当前状态：迁移安装包已通过本地兼容验证，等待发布授权，暂未提供升级版本。**

The application and its source code have moved to **[Pencilfinely/exlab](https://github.com/Pencilfinely/exlab)**.

This repository preserves the exact update address used by Experiment Manager 0.4.5 and earlier desktop clients. Their updater rejects repository redirects, so this address provides a real migration release.

After the migration release is published, open the existing application's update window, check for updates, then download and install **ExLab 0.5.0**. Update Center and Worker separately. The installer keeps the existing data directory, WSL selection and node identity. Once upgraded, future updates use the ExLab repository.

The `ExperimentManager-0.5.0-…-Setup.exe` assets contain the same bytes as the corresponding ExLab installers. SHA-256 checksums are included. New installations should use the [ExLab releases](https://github.com/Pencilfinely/exlab/releases).

## 中文

本仓库保留旧客户端的升级入口。项目源码、后续开发和正式发布已迁移到 **[ExLab](https://github.com/Pencilfinely/exlab)**。

迁移版本发布后，在旧软件中选择“检查更新 → 下载更新 → 安装更新”即可升级到 ExLab 0.5.0。主控和算力端分别更新；安装后自动使用新仓库检查后续版本。

这里的旧文件名仅供旧更新器识别，安装包内容与 ExLab 发布逐字节一致。源码开发请将 Git remote 更新为 `https://github.com/Pencilfinely/exlab.git`；本仓库只维护兼容升级资产。
