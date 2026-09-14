# Experiment Manager 0.2.0-rc.1

First public preview with separate controller and worker editions.

| Download | Entry point |
|---|---|
| Windows controller x64 | `Start-Controller.cmd`; Python is bundled |
| Windows worker x64 | `Start-Worker.cmd`; runs in WSL2 Ubuntu |
| Native Ubuntu worker x64 | `bash Start-Worker.sh` |

The controller now exports per-worker pairing files from the browser. Workers
automatically prepare their application directories, build and pin the runtime,
verify GPU UUIDs with actual CUDA calculations, generate configuration and expose
a GPU-check task template on the controller. Configure-Project registers local
Git projects without hand-editing node settings. Complete English and Chinese
READMEs are included in every ZIP.

This is a **preview**, not a signed MSI or an OS dependency installer. Prepare WSL,
Docker and GPU drivers/toolkit first. Multiple GPUs per experiment and automatic
performance optimization are not implemented. GPU/runtime support is established
by on-device checks, not by card-name assumptions.

Validation includes the full local Python test suite, actual embedded-Python
controller startup, browser login/pairing, shell/PowerShell/JS syntax checks, and
archive manifest/credential-exclusion checks. Source CI covers Windows and Ubuntu
on Python 3.10 and 3.13; external SASRec CPU tests require the user's compatible
research source and are skipped when it is absent. See the final repository CI
status and README for hardware limitations.

## 中文

首个公开预览版，提供分离的 Windows 主控端、Windows 算力端、Ubuntu 算力端三个包。
同一台 Windows 电脑要调度自己的显卡，需要安装主控和算力两个版本。

主控网页可导出专属配对文件；算力端自动准备目录、构建并固定镜像、实际验卡、生成配置并接单。
项目登记向导可以接入已有 Git 项目，无需手改节点 JSON。每个包均附完整中英文 README。

首次使用前仍需要具备 WSL/Docker/显卡驱动等系统依赖。本版采用一键入口 ZIP，没有签名 MSI、系统服务或自动更新；单实验多卡及自动性能优化尚未实现。

下载具体的三个应用 ZIP，不要将 GitHub 自动附带的 Source code 当作安装包。
包内没有私人节点凭证、科研数据、训练模型或外部算法源码。许可证为 MIT。
