# Experiment Manager 0.3.0-rc.5

## Windows 安装修复

修复 rc.4 在部分 Windows 机器上将已关闭端口误判为无法确认停止的问题：TCP 拒绝连接可能在约 2 秒后才返回，旧检查提前超时。延长确认等待时间，仍拒绝在后台运行或端口状态不明时安装。新增真实服务启动、停止、安装检查回归。

本版包含 rc.4 的实验计时及退出改进。已安装 rc.4 的用户同样建议更新。

## 实验计时

- 总览、实验列表和详情显示累计运行时长，运行中每秒更新。
- 只累计实际运行阶段，排除排队、环境准备与停止期间；恢复实验后继续累计。
- 算力端持久保存计时，管理端离线后可补传。Docker 使用容器的开始/结束时间恢复记录。
- 实验详情显示提交、首次开始、最近停止时间，CSV 导出包含计时字段。旧记录和无法确认的历史片段明确标注。

## 更新与退出

- 分别显示客户端和后台版本；旧后台不支持更新协议时给出具体迁移提示。
- 修复管理端网页暂时无响应时被误判为已退出，避免重复启动或提前交接安装器。
- 安装交接记录阶段与失败原因，便于定位下载完成后的安装、启动问题。
- 更新诊断保存在 `%LOCALAPPDATA%\ExperimentManager\desktop\Controller\last-update.json` 或对应的 `Worker` 目录；只有新版后台成功启动才记录完成。
- 关闭窗口仅收起到托盘，继续后台运行。托盘“退出”停止本机对应后台服务后退出。
- 算力端托盘退出先请求保存并停止实验；停止期间不再启动新实验。未回传记录保留，下次启动继续同步。无原生续训能力的算法不能保证恢复训练。
- 尚未开始的实验保留在队列；环境准备期间退出会取消本服务启动的准备进程，已下载的镜像层保留。
- 管理端退出不会停止远程算力端实验，也不会关闭普通浏览器窗口、WSL 或 Docker Desktop。
- 首次准备运行环境时显示下载、构建进度及等待时间，日志实时写入，便于区分下载较慢与失败。

## 从旧版迁移

**客户端窗口是新版，不代表后台已经更新。** 如果状态中的后台仍为 rc.1，旧更新器无法通过安全停止检查，需要手动安装本次新版一次。

1. 先暂停接单，等待实验和待回传完成。多机环境先更新各 Worker，再更新 Center。
2. 在旧客户端状态窗口点击“停止代理”或“停止主控”，确认停止，再从托盘退出。旧版托盘退出本身不会停止后台。
3. 运行对应角色的 rc.5 `Setup.exe`，保留原管理数据目录、Ubuntu 和算力节点配置。不要把安装目录改成实验数据目录。
4. 启动软件和后台代理，核对客户端与后台版本、历史实验和节点身份。

以后的更新仍可在软件中检查、下载并安装。管理端与算力端都更新后，才能完整记录新实验的运行时长。

## 下载

- Windows 管理端：`ExperimentManager-0.3.0-rc.5-windows-controller-x64-Setup.exe`
- Windows 算力端：`ExperimentManager-0.3.0-rc.5-windows-worker-x64-Setup.exe`
- Ubuntu 算力端：`ExperimentManager-0.3.0-rc.5-ubuntu-worker-x64.zip`

Windows 也提供完整 ZIP；`SHA256SUMS.txt` 包含上述应用文件的校验值。此版本仍为预览版。

## English

Fixes a Windows installation check that could time out before the operating system reported a closed port. Adds a real server stop/install regression test. Includes the rc.4 improvements below.

Adds persistent cumulative experiment runtime, pause/resume accounting, live display and CSV export. Fixes controller lifecycle detection and makes client/backend version mismatches and installer failures visible. Closing a window keeps the app in the tray; tray Exit stops the associated service. Worker Exit requests cooperative experiment stops before leaving, retaining pending uploads for the next start.

If the backend still reports rc.1, stop it using the old client's service controls, exit the tray client, and run the rc.5 installer directly once. Keep the existing data directory, Ubuntu distribution and worker configuration. Update workers before the controller.
