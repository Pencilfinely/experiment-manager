# ExLab upgrade compatibility channel

The application and its source code have moved to **[Pencilfinely/exlab](https://github.com/Pencilfinely/exlab)**.

This repository preserves the exact update address used by Experiment Manager 0.4.5 and earlier desktop clients. Their updater rejects repository redirects, so this address provides a real migration release.

Open the existing application's update window, check for updates, then download and install **ExLab 0.5.1**. Update Center and Worker separately. The installer keeps the existing data directory, WSL selection and node identity. Once upgraded, future updates use the ExLab repository.

If an old Center reports unfinished remote experiments and blocks installation, stop the Center normally, exit the client, then run the installer. Remote training continues. ExLab 0.5.1 corrects this check so remote experiment records and resumable transfers no longer block Center updates.

The `ExperimentManager-0.5.1-…-Setup.exe` assets contain the same bytes as the corresponding ExLab installers. SHA-256 checksums are included. New installations should use the [ExLab releases](https://github.com/Pencilfinely/exlab/releases).

## 中文

本仓库保留旧客户端的升级入口。项目源码、后续开发和正式发布已迁移到 **[ExLab](https://github.com/Pencilfinely/exlab)**。

旧软件中选择“检查更新 → 下载更新 → 安装更新”即可升级到 ExLab 0.5.1。主控和算力端分别更新；安装后自动使用新仓库检查后续版本。

如果旧主控提示远端实验尚未完成并阻止安装，请先正常停止主控、退出客户端，再运行安装包；远端训练继续。0.5.1 已修正检查，远端实验记录和可续传文件不会再阻塞主控升级。

这里的旧文件名仅供旧更新器识别，安装包内容与 ExLab 发布逐字节一致。源码开发请将 Git remote 更新为 `https://github.com/Pencilfinely/exlab.git`；本仓库只维护兼容升级资产。
