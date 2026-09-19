# Experiment Manager 0.4.0

本版作为正式 Release 发布，包含实验矩阵、算力分配、算法导入和项目清理改进。Windows 管理端、Windows 算力端和 Ubuntu 算力端需要使用对应角色的安装包。

## 新功能与改进

- **实验矩阵：**新建、导入、查看、编辑、复制和删除配置，组合数据集与参数，一次预览并启动整批实验。五个数据集 × 两个 seed 可生成十个独立实验；每批最多 256 个组合。历史批次保存当时的配置，后续编辑不会改变已经提交的任务；同一次启动请求重试不会重复创建任务。
- **自动、半自动和手动分配：**按可用资源、负载和节点配置选择已部署兼容项目的算力机，也可设置候选范围、偏好顺序、优先级，或指定机器/GPU。硬性指定的资源不可用时排队等待。保留 CPU、内存、显存、并发和运行时段限制，不自动改变训练参数。
- **Markdown 结果：**矩阵一键导出状态、批次、数据集、参数、指标、节点和运行时长。未完成或缺失的结果明确标注，不混合不同指标协议排名。
- **简化导入：**通过表单配置入口命令、固定/实验参数、类型、命令行绑定、数据文件与日志规则，减少手改 JSON。支持日志模板与真实示例提取测试。
- **可选 AI 辅助：**设置中可配置 DeepSeek 或兼容服务的地址、模型和 API Key。建议经过校验后展示差异，由用户应用到草稿；默认关闭，不自动执行、发布或启动实验。仅在明确勾选时发送入口源码片段，不发送数据集文件。
- **删除算法项目：**从管理界面移除项目，持久记录各节点的清理任务，清理管理端发布包及算力端受管理的部署副本、已停止容器、缓存和可确认独占的派生镜像。离线节点上线后继续，失败原因可查看并重试。导入前的原始源码和数据、归档实验结果保留。
- **界面：**重新设计侧栏收起按钮；“应用 JSON”的成功/失败提示放到按钮旁；监控图表适配高 DPI 和容器尺寸变化，修复模糊与拉伸。

详细操作见[实验矩阵与导入指南](https://github.com/Pencilfinely/experiment-manager/blob/v0.4.0/docs/EXPERIMENT-MATRICES.zh-CN.md)。继续包含此前的累计实验计时、后台退出和更新诊断修复。

## 更新现有安装

1. 暂停接单，等待实验和待回传完成。**先更新所有 Worker，再更新 Center。** 指定 GPU 和项目清理需要新版算力端支持；旧节点不会被当成已完成清理。
2. 在客户端停止对应后台服务，然后从托盘退出。确认旧版已停止，再安装相同角色的 0.4.0 安装包。Windows rc.2 及以后也可使用“检查更新”。如果界面已更新但后台仍是 rc.1，需要手动停止旧后台并安装一次。
3. 保留原管理数据目录、Ubuntu、算力节点配置和身份，启动后核对客户端与后台都为 0.4.0。不要将程序安装目录设为实验数据目录，也不要同时运行使用同一节点目录的新旧代理。
4. Ubuntu 算力端按[日常维护说明](https://github.com/Pencilfinely/experiment-manager/blob/v0.4.0/docs/OPERATIONS.zh-CN.md)使用新版 ZIP 更新，保留原配置和数据目录。

## 下载

| 用途 | 文件 |
| --- | --- |
| Windows 管理端 | `ExperimentManager-0.4.0-windows-controller-x64-Setup.exe` |
| Windows 算力端 | `ExperimentManager-0.4.0-windows-worker-x64-Setup.exe` |
| Ubuntu 算力端 | `ExperimentManager-0.4.0-ubuntu-worker-x64.zip` |

同时提供两个 Windows 完整 ZIP。`SHA256SUMS.txt` 包含五个应用文件的校验值；`build-info.json` 记录构建信息。GitHub 的 Source code 下载项是源码，不是安装包。Windows 管理端内置 Python；Windows 算力端仍需 WSL2、Ubuntu、Docker Desktop 和 NVIDIA 驱动。Windows 安装包尚未代码签名。

## 使用边界

- 矩阵提交前先向目标节点分发并安装算法项目；自动分配不自动为未部署项目的节点安装环境。
- 调度采用确定规则；AI 目前用于辅助导入，结果整理按规则完成。单个实验跨多卡/多机训练尚未实现。
- 删除不会执行全局 Docker prune。共享基础镜像、共享镜像仓库、构建缓存，以及无法证明独占归属的旧版镜像会保留，因此磁盘释放量不一定等于部署总大小。
- 项目删除后不能恢复该部署下暂停/失败的任务；实验历史和归档输出仍可查看。有活动任务或未确认操作时拒绝删除。
- AI 配置文件包含 API Key，存放在管理端数据目录中；界面不回显密钥。复杂动态参数、依赖和日志含义仍需检查。

## English

This regular release adds persistent experiment matrices with dataset/parameter combinations, batch launch, allocation preview and Markdown reports. Scheduling supports automatic allocation, constrained/preferred workers, priorities and explicit worker/GPU selection. Import forms, log extraction tests and optional DeepSeek-compatible AI suggestions reduce JSON editing. Project deletion tracks managed deployment cleanup across workers while preserving original inputs and archived results. Sidebar controls, inline JSON feedback and high-DPI monitoring charts are improved.

Update workers before the controller. Finish experiments and pending uploads, stop the old services and tray clients, then install the matching role while keeping existing data and node configuration. Windows preview releases can upgrade to this regular release. Existing rc.1 backends require a manual service stop and installation. Project deployment to candidate workers remains a prerequisite for scheduling. Shared Docker resources and legacy resources without verifiable ownership are retained.
