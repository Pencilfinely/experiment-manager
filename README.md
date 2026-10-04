# ExLab · 旧版升级入口

**简体中文** · [English](#english)

本仓库保留旧客户端的升级入口。项目源码、后续开发和正式发布已迁移到 **[ExLab](https://github.com/Pencilfinely/exlab)**。

旧软件中选择“检查更新 → 下载更新 → 安装更新”即可升级到 ExLab 0.5.2。主控和算力端分别更新，沿用原数据、节点身份和配置；安装后自动使用新仓库检查后续版本。

桌面快捷方式和程序描述统一为 **ExLab Center / ExLab Worker**。新版还包括移动端入口与配对页面优化、默认长期配对、自动接入与断线重连，以及手机 APK 的检查、下载、校验和安装更新。手机旧 APK 需先覆盖安装一次 [ExLab Monitor 0.5.2](https://github.com/Pencilfinely/exlab/releases/tag/v0.5.2)，之后可在应用内更新；不要先卸载，以保留应用数据。

如果旧主控提示远端实验尚未完成并阻止安装，请先正常停止主控、退出客户端，再运行安装包；远端训练继续。0.5.1 起已修正检查，远端实验记录和可续传文件不会再阻塞主控升级。

这里的旧文件名仅供旧更新器识别，安装包内容与 ExLab 发布逐字节一致。源码开发请将 Git remote 更新为 `https://github.com/Pencilfinely/exlab.git`；本仓库只维护兼容升级资产。

0.5.3 修复已不存在的旧容器阻塞 Worker 退出的问题，并新增“释放 WSL 内存”。可保留 Center 开启，按需停止本机算力并关闭 Ubuntu；其中的其他会话需确认关闭。手机 Monitor APK 仍为 0.5.2。

## English

Version 0.5.3 fixes shutdown blocked by missing historical containers and adds on-demand WSL memory release in Worker while Center stays open. Other Ubuntu sessions require confirmation before closure. Monitor APK remains at 0.5.2.

The application and its source code have moved to **[Pencilfinely/exlab](https://github.com/Pencilfinely/exlab)**.

This repository preserves the exact update address used by Experiment Manager 0.4.5 and earlier desktop clients. Their updater rejects repository redirects, so this address provides real migration releases.

Open the existing application's update window, check for updates, then download and install **ExLab 0.5.3**. Update Center and Worker separately. The installer keeps the existing data directory, WSL selection and node identity. Once upgraded, future updates use the ExLab repository.

Version 0.5.2 renames desktop shortcuts and application descriptions to **ExLab Center / ExLab Worker**. It adds persistent mobile pairing, automatic reconnect, a redesigned mobile entry and pairing page, and native APK update checks with download integrity and signing identity verification. Get the **ExLab Monitor 0.5.2 Android APK** from the [main release](https://github.com/Pencilfinely/exlab/releases/tag/v0.5.2); install over the previous APK to keep app data.

If an old Center reports unfinished remote experiments and blocks installation, stop the Center normally, exit the client, then run the installer. Remote training continues. ExLab 0.5.1 and newer correct this check so remote experiment records and resumable transfers no longer block Center updates.

The `ExperimentManager-0.5.3-…-Setup.exe` assets contain the same bytes as the corresponding ExLab installers. SHA-256 checksums are included. New installations should use the [ExLab releases](https://github.com/Pencilfinely/exlab/releases).
