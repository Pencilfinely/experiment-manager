# Experiment Manager 0.4.2

修复管理端更新时，在实验与中心侧文件传输已完成的情况下，仍因离线节点而提示“节点尚未确认空闲和回传完成”的问题。

## 修复

- 已连接过的算力节点离线、心跳超过 45 秒或缺少新版空闲标记，不再单独阻止管理端更新。节点记录和最近一次状态保留，不伪造心跳或删除节点。
- 仍然阻止尚未完成的实验、未确认命令、中心已登记的待上传文件、项目上传/分发，以及正在处理的请求、导入和上传操作。接受更新停止后继续阻止新写入，持久保存的数据在重启后恢复。
- 算力端自身的更新检查保持严格：运行中的实验、未完成回传或无法确认容器状态，仍会阻止算力端更新。

同时包含 0.4.1 的修复：Windows 算力端即使原本停止，也会在安装时同步 WSL 后台代码，保留停止状态；界面区分已安装后台版本、上次运行版本与运行中进程版本。

## 旧版管理端被挡住时如何更新

旧客户端和旧后台会继续使用原有更新检查，因此仅下载新版不一定能通过旧版检查。遇到上述提示且仅有离线节点阻断时，可以使用已有的手动安装流程：

1. 在管理端状态窗口点击 **停止主控**，确认主控停止后，从托盘选择 **退出**。
2. 下载并运行 `ExperimentManager-0.4.2-windows-controller-x64-Setup.exe`。
3. 保留原管理数据目录（例如 `E:\ExperimentCenter`），安装并启动。不需要重新配对或删除离线节点。

手动安装仍会核对本机后台、生命周期锁和端口已停止，不跳过本机安装检查。不要让新旧管理端同时使用同一数据目录。升级到本版后，后续更新不再需要让无未完成工作的离线节点重新上线证明空闲。

本次主要更新管理端；已安装 0.4.1 的算力端可继续使用。同时提供全部三个角色的 ZIP、两个 Windows 安装器及 SHA256SUMS.txt。

## English

Controller updates no longer fail solely because previously connected workers are offline, have stale heartbeats, or lack a fresh quiescence flag. Existing task, command, controller-side transfer and local-operation checks remain in force, as does the atomic stop fence. Worker update requirements are unchanged.

An old controller may still apply its old check before launching the new installer. If that prevents this update, stop the controller service, exit its tray client, and run the 0.4.2 controller installer directly while preserving the existing data directory. The manual installer still verifies that the local backend and listening port have stopped. Includes the 0.4.1 stopped-worker installation and version reporting fixes; 0.4.1 workers remain compatible.
