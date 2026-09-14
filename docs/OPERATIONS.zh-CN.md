# 运行与算法接入说明

[English](OPERATIONS.md) · [简体中文](OPERATIONS.zh-CN.md) · [先完成安装](../README.zh-CN.md)

## 一个从头能跑的小项目

下面的例子使用人工生成的输入，实际在 GPU 上计算。它用于熟悉实验台，不用于评价科研模型。
在**算力机的 Ubuntu 终端**中执行：

```bash
mkdir -p ~/my-gpu-example
cd ~/my-gpu-example
git init
cat > train.py <<'PY'
import torch
from expman.sdk import Run

run = Run()
torch.manual_seed(int(run.params.get('seed', 42)))
model = torch.nn.Linear(16, 1).cuda()
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
x = torch.randn(256, 16, device='cuda')
y = x.sum(dim=1, keepdim=True)
for step in range(1, int(run.params.get('steps', 20)) + 1):
    optimizer.zero_grad()
    loss = (model(x) - y).square().mean()
    loss.backward()
    optimizer.step()
    run.metric(step, loss=loss.item())
torch.save(model.state_dict(), run.output / 'model.pt')
run.finish({'status': 'succeeded', 'loss': loss.item()})
PY
git add train.py
git -c user.name=Example -c user.email=example@localhost commit -m 'Add GPU example'
```

这段命令创建一个自己的 Git 仓库，将训练程序保存为 `train.py`，并记录版本。
退出算力端终端，启动 **Configure-Project** 入口，依次填写：

| 提示 | 填什么 |
|---|---|
| 项目名称 | `my-gpu-example` |
| 仓库目录 | 你实际的 `/home/你的Linux用户名/my-gpu-example` |
| 训练命令 | `python train.py` |
| 数据目录 | 直接回车；本例自己生成输入 |

重新启动算力端，在主控网页填入 `my-gpu-example` 模板，将 `params` 设置成 `{"seed":42,"steps":20}`。
参数组合保持 `{}`，提交一次。结束后能在详情里看到 loss 变化，并下载 `model.pt` 和 `result.json`。
这个简短例子**没有实现检查点恢复**，不能拿它验收训练的停止恢复功能。

想比较两个随机种子，在参数组合框填 `{"seed":[42,43]}`。这会创建两条独立实验。
已提交的实验定义保持不变，之后修改本地模板不会改写之前那条实验。

## 接入自己的 SASRec 项目

适配器需要用户提供兼容的实现：包含 `src/experiment.py`、`datasets.py`、`models.py`、`modules.py`、`trainers.py`、`utils.py`、`main.py`，以及适配器所调用的结构化回调和检查点接口。
它不是对所有名为 SASRec 的仓库都通用。具体接口可在源码仓库的 `expman/adapters/sasrec.py` 和部署辅助脚本 `scripts/sasrec_first_run.py` 中查看。

假设你的已提交 Git 仓库根目录包含 `SASRec_Original/src/…`，数据结构是：

```text
datasets/
  Video_Games/
    Video_Games.train.txt
    Video_Games.valid.txt
    Video_Games.test.txt
```

1. 用 Configure-Project 登记：项目名填 `sasrec-video`，选择这个 Git 仓库根目录，数据目录选 **datasets**，即 `Video_Games` 的上一层。
2. 训练命令填：`python -m expman.adapters.sasrec --project /workspace/code/SASRec_Original`。
3. 重新启动算力端，在主控网页填入它的项目模板。
4. 将 `algorithm` 设置为 `SASRec`、`metric_protocol` 设置为 `external_sasrec_original_v1`。把**自己的科研配置**中的训练参数填入 `params`；输入/输出定位字段由适配器管理，改用 `"dataset":"Video_Games"`、`"data_asset":"sasrec-video-data-v1"`、`"device":"cuda"`、`"gpu_id":0`。
5. 首先单独命名一条短跑任务，将 `epochs` 设为 3、`star_test` 设为 -1，使训练、验证和测试都能执行。资源预算使用实测值或保守起始值；申报预算不等于提前占用这么多显存。
6. 检查日志、结果和资源峰值，再新建正式任务，恢复原科研参数。不要把微型 smoke 测试用的小模型尺寸带进正式实验。

源码仓库 `scripts/` 中还有源码包和数据包辅助工具，供部署维护者按需使用。
这些工具根据用户自己的文件制作**私有传输包**；生成的算法/数据/节点包不应该上传公共 Release。

如果算法需要额外的 Python 或系统依赖，应先构建并验证相应 Docker 镜像，再登记环境。
预置环境是 PyTorch 2.7.1、CUDA 12.8，不是通用依赖推断器。不同架构显卡需要各自在容器中验收，不能继承另一张卡的“通过”声明。

## 资源分配的含义

自动安装会给识别到的显卡各登记一个任务槽，同时把**整个节点的并行任务上限设为 1**，先建立清楚的单任务基线。
这不表示程序只识别到一张卡。

高级部署的设置保存在算力端数据目录下的 `node.ready.json`。修改前退出算力端，并保留备份。
`policy.max_running` 限制整台机器；`gpu_policy[UUID].max_jobs` 限制单张卡，设成 0 表示不允许新任务使用这张卡。
实验自己的 `resources.exclusive:true` 表示不与其他受管理任务共用该卡。CPU、RAM、显存预算也必须同时满足。
不要为了让调度器放行，就悄悄改变科研参数。

同卡共享需要参与任务都允许 `exclusive:false`，同时单卡并发上限大于 1。
两张卡各跑一个实验，则需要节点上限至少为 2，并有足够的总 CPU/RAM。
应当比较每条实验的完成时间与单任务基线。显存占用小，不代表两条任务并行后会更快。
本版没有自动 DDP/FSDP 或跨机器合训。

## 停止、恢复和断线

在实验详情中使用 **“请求保存并停止”** 发出协作式停止请求。节点需要在线才能收到。
具有完整检查点支持的适配器会在安全边界保存，并能以新的 attempt 恢复。普通程序如果不检查 SDK 的停止信号，就不能承诺保留完整训练状态。

**“暂停接单”**用于暂停分配/启动新任务，不会杀死当前训练。
节点失联不会把同一个任务悄悄发给另一台机器。源码、镜像、数据都已经缓存的任务，可以在主控离线期间继续执行。
保持算力端运行，它会自动重连并补传。

## 常见问题

| 现象 | 处理方法 |
|---|---|
| 找不到 WSL 发行版 | 安装 WSL2 Ubuntu，首次打开并创建普通用户；安装要求重启 Windows 时先重启。 |
| WSL 中 `docker: command not found` | 启动 Docker Desktop，为启动器选中的那套 Ubuntu 开启 WSL 集成。 |
| Ubuntu 中 Docker 权限不足 | 配置普通用户的 Docker 权限；用户组变更后可能需要重新登录，不要把整个算力端改为 root 运行。 |
| 无法配对主控 | 从这台算力机打开配对文件里的主控地址，确认主控在运行、IP/端口正确、防火墙与 VPN 路由允许；主控需要 0.2 或更新版本。 |
| 镜像下载失败/超时 | 检查 Docker 自身的网络和代理，修复后重跑入口，不要跳过摘要校验。 |
| 显卡数量、UUID 或 CUDA 验收失败 | 查看 `setup.log`，检查驱动与 GPU 容器支持。WSL 下同时使用 Docker 选卡和 CUDA UUID 选卡；未通过的环境不会登记为可用。 |
| 实验一直排队 | 检查节点标签、源码白名单、数据资产 ID、已验证镜像、显卡槽位与资源预算。 |
| 程序向源码目录写文件时报错 | 将输出改为 `EXPERIMENT_OUTPUT` 或 `Run().output`；源码和数据目录只读。 |
| 状态已完成，但文件没齐 | 保持主控、算力端运行，等待回传数量为 0，再刷新详情。 |
| 数据目录被占用 | 检查已经运行的主控/算力端窗口，不要用同一数据目录启动第二个接单程序。 |

系统前提的官方说明：[WSL 安装](https://learn.microsoft.com/windows/wsl/install)、[Docker WSL 集成](https://docs.docker.com/desktop/features/wsl/)、[Ubuntu Docker](https://docs.docker.com/engine/install/ubuntu/)、[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)。

## 升级、备份与卸载

程序目录保存可以替换的应用文件；主控/算力端数据目录保存身份、配置、数据库和结果。

预览版升级前，先等待任务结束或暂停接单，并确认文件回传完成。退出程序，备份**整个数据目录**，将同角色的新版本解压到新程序目录，再启动。
启动器使用相同的默认数据目录。算力端版本变化时会重新核验环境，确认后启用任务。
保留旧程序和备份，直到新版本验收完成。重新配置会将安装器管理的并发策略恢复为每节点一个任务；有自定义显卡/并发策略时需要重新核对。

备份主控时先停止它，再复制整个数据目录，避免 SQLite 数据库与 WAL 文件不一致。
备份算力端时也应在训练结束、进程退出后进行。不要用网盘双向同步正在运行的数据库或整个 WSL 虚拟磁盘。

卸载程序时，先停止它，再删除解压出来的程序目录。持久数据会保留，确认不再需要实验或检查点后再自行删除。
Docker 镜像和卷同样按需保留。本预览版没有安装自动启动服务。
可选防火墙规则名为 `ExperimentManager-端口-算力机IP`，不再使用该节点时可在 Windows Defender 防火墙中删除对应规则。
