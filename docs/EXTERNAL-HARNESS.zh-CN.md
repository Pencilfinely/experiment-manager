# 不改源码接入算法，并分发到其他机器

[English](EXTERNAL-HARNESS.md) · [返回 README](../README.zh-CN.md)

你已有一个能训练的算法。实验台需要知道四件事：**从哪里启动、传哪些参数、数据在哪、结果去哪。** 外置 harness 把这些信息写在算法目录之外，训练时调用原入口；不要求你给模型或训练循环增加 SDK 代码。

**v0.2.0-rc.2 主控包和算力包包含本流程。旧 v0.2.0-rc.1 不包含项目分发。** 主控端和目标算力端都要更新；节点需上报 `project-bundle-v1` 能力，旧代理不能因为网页更新就自动获得下载、安装项目包的能力。

已有算力机升级：等当前任务及回传完成，在旧代理窗口按 **Ctrl+C**，保持 Docker 运行；将新算力包完整解压到新的长期保留目录，Windows 双击 **`Update-Worker.cmd`**，Ubuntu 运行 **`bash Update-Worker.sh`**。程序自动寻找原配置；有多个候选时，根据菜单中的节点名和完整路径，选择原启动命令 `--config` 使用的文件。它继续使用原节点身份、镜像、策略和记录，无需重装 WSL/Docker、重新配对或手改配置。以后仍从这个新包的更新入口启动原节点。

## 先认准这份 SASRec

本例的原项目是 **`E:\PythonProjects\SASRec_Original`**：

```text
SASRec_Original/
├─ src/
│  ├─ main.py       ← 已有训练入口
│  ├─ trainers.py
│  ├─ models.py
│  └─ …
└─ data/
   └─ Video_Games.test.txt
```

它从 `src` 目录运行 `python main.py`，通过 `argparse` 接受 `--data_dir`、`--data_name`、`--lr`、`--epochs` 等参数。这里的数据示例是**一个序列文件**，不要套用另一份实现的 train/valid/test 三文件配置。

旧文档里的 `E:\PythonProjects\IntentPreference\SASRec_Original` 是另一份实现，它才有 `load_run_config()`、`run_all()` 和专门的恢复协议。那份[旧适配练习](SASREC-ADAPTATION-WALKTHROUGH.zh-CN.md)不适用于本例。

## 第一步：生成一份放在外面的配置

v0.2.0-rc.2 Windows 主控包解压目录里有 **`Import-Algorithm.cmd`**；Ubuntu 对应 `bash Import-Algorithm.sh`。本次本地部署另有 **`local-app/04-Import-Algorithm.cmd`**，它是个人入口，不在公共源码中。双击 Windows 入口即可打开导入向导；普通源码用户直接运行：

```powershell
python -m expman.harness_project wizard
```

如果要看清每一步，在实验台源码目录打开 PowerShell，先运行：

```powershell
python -m expman.harness_project prepare --source "E:\PythonProjects\SASRec_Original" --output "E:\ExperimentProjects\SASRec"
```

`--source` 是你现有的算法，`--output` 是新建的外部配置目录，**两者分开**。以下提到的文件都在 `E:\ExperimentProjects\SASRec`，不在原算法目录：

| 文件 | 你在这里决定什么 | 一般什么时候改 |
|---|---|---|
| `project.json` | 哪些代码和数据进入包、使用哪个运行环境、任务需要多少资源 | 首次接入；增加数据或依赖时 |
| `harness.json` | 原入口、参数如何传入、固定路径、指标怎么读取、是否支持原生续训 | 首次接入；原算法接口变化时 |
| `experiments/*.json` | 每个实验自己的学习率、seed、epoch 等参数 | 新建实验或保存参数预设时 |

首次会生成 `experiments/default.json` 和记录识别依据的 `discovery.json`。向导打开外部配置目录后，你检查这些文件，把 `project.json` 的 `"reviewed": false` 改成 `true`；再次运行同一个入口、填写同一个目录，程序才打包。随后填写主控数据目录 `E:\ExperimentCenter` 和实验台地址即可上传；主控数据目录留空则只生成 ZIP。下面的命令是向导各阶段的对应操作，平时无需全部手打。

扫描器只读 Python 语法树，不导入原程序，也不执行 `main.py --help`。这份 SASRec 在文件末尾直接调用 `main()`，贸然导入就可能启动训练，因此静态读取尤其必要。

此时生成的是**待检查的草稿**，尚未启动训练，也没有自动把所有识别结果当成正确设置。

## 第二步：检查向导提取出的内容

这份 SASRec 可以静态读出 24 个参数。你主要检查下面这张表，不需要编写 Python 适配器。

| 项目 | 本例应该是什么 | 原因 |
|---|---|---|
| 入口 `command` | `["{python}", "main.py"]` | 调用算法原入口；`{python}` 使用任务环境里的 Python |
| 工作目录 `cwd` | `src` | 与你手动训练时进入的目录一致 |
| 固定 `data_dir` | `{assets.dataset}/` | 换成目标节点自己的数据目录；原代码用字符串相加，末尾 `/` 要保留 |
| 固定 `output_dir` | `{output}/outputs` | 每次实验写到自己的结果目录 |
| 固定 `gpu_id` | `{env.CUDA_VISIBLE_DEVICES}` | 原入口会重写这个环境变量，把调度分配的 GPU 传给它 |
| 固定 `do_eval` / `no_cuda` | 都是 `false` | 这个项目包用于 GPU 训练，保持评估专用和 CPU 开关关闭 |
| 实验 `data_name` | `Video_Games.test` | 原程序会自行拼接 `.txt` |
| 依赖 | `torch`、`numpy`、`scipy`、`tqdm` | 需要任务镜像中实际安装，不能只看主机上的 Python |
| `resume.supported` | `false` | 这份源码只有模型权重的保存/评估，没有完整训练续跑入口 |

例如，`harness.json` 中的这部分表示“所有实验固定使用节点分配的路径和 GPU”：

```json
"fixed_params": {
  "data_dir": "{assets.dataset}/",
  "output_dir": "{output}/outputs/",
  "gpu_id": "{env.CUDA_VISIBLE_DEVICES}",
  "do_eval": false,
  "no_cuda": false
}
```

它们通过 `bindings` 变成原程序本来就认识的命令行参数。例如绑定 `lr` 和 `--lr` 后，一次实验中的 `lr: 0.001` 会变成 `--lr 0.001`。`store_true` 开关按真假决定是否加入命令，不会生成错误的 `--do_eval False`。

在 `project.json` 中，`source` 是原算法目录，`include` 是要打包的文件匹配规则，`exclude_directories` 是跳过的目录。数据单独放在 `assets`；把本例的 `assets` 配置为：

```json
"assets": {
  "dataset": {
    "path": "E:\\PythonProjects\\SASRec_Original\\data",
    "include": ["Video_Games.test.txt"]
  }
}
```

这样只传这一份数据，避免把整个 `data` 目录全部发送。已有输出、虚拟环境、缓存和 `.git` 不应随算法发送。**路径只用于本机打包**，其他机器接收的是包内文件，不需要存在同样的 `E:` 盘。

`runtime.imports` 记录要检查的模块；需要安装的额外依赖在 `runtime.requirements` 中用 `包名==确切版本` 指定。算力端先检查自己已经验证并固定镜像摘要的环境；缺依赖时，按这份清单自动构建一个补充环境并验证导入和 CUDA，保存新的镜像摘要。你无需手写 Docker 命令；无法推断的包名、版本仍要填写。`resources` 中的 CPU、RAM、显存预算也要符合真实训练需求。

本例保留节点已有的 PyTorch/NumPy 环境，补充依赖。将 `project.json` 的 `runtime` 字段设置为：

```json
"runtime": {
  "imports": ["torch", "numpy", "scipy", "tqdm"],
  "requirements": ["scipy==1.15.3", "tqdm==4.67.1"]
}
```

原生 Windows 的 `.bat` 不能直接当作 Linux 容器入口；有对应 `main.py` 或 `.sh` 时选那个入口。

## 第三步：把短测试和正式实验分开

生成的 `experiments/default.json` 已包含提取的默认参数，可以作为正式预设保留。需要另起名字时，同时改文件名和其中的 `id`，使每个 `id` 唯一。

在同一目录新增 `short.json`，完整内容如下：

```json
{
  "id": "short",
  "name": "Video_Games - 3 epoch check",
  "params": {
    "epochs": 3,
    "star_test": -1
  }
}
```

`id` 是预设的唯一名称，`name` 是网页显示名称，`params` 才是本次实验参数。上面只覆盖两项，其余使用 `harness.json` 中提取的默认值。正式预设保留原科学参数。

`star_test` 不能漏改：原程序只有开始验证后才会保存模型，结束时又会加载这个模型；如果只把 300 轮改成 3 轮，却保留 `star_test=100`，最后就可能找不到权重文件。这个值是阅读本例后确认的，通用扫描器不会替每个算法猜测类似的训练约束。

## 第四步：上传一次，选择节点分发

先构建项目包：

```powershell
python -m expman.harness_project build --project "E:\ExperimentProjects\SASRec" --output "E:\ExperimentPackages\sasrec.zip"
```

项目包包含选中的代码、数据、外部配置和实验预设。上传前检查包清单，确认这是你要发给那些算力机的内容。

打开实验台的 **“算法项目 / Projects”**，点击 **“上传项目包 / Upload project”** 选择 ZIP。也可以在**主控电脑**使用已有主控配置上传：

```powershell
python -m expman.harness_project publish --bundle "E:\ExperimentPackages\sasrec.zip" --hub "http://127.0.0.1:8765" --center-root "E:\ExperimentCenter"
```

上传后，在该项目下勾选目标节点，点击 **“分发到所选节点 / Deploy”**。上传完成表示**主控已经收到包**；还要等选中节点显示 **“已安装”**，才表示该节点准备好了。节点会自行下载、检查文件并建立私有源码快照，所需 Git 操作发生在节点自己的安装目录，不会对你的原算法执行 `git init` 或提交。

因此，这个流程不用逐台 Xftp 复制项目、不用登录另一台机器再改路径。节点离线时需等它上线接收；运行环境缺依赖或不兼容时，应修正环境后重试，不能把“收到包”当作“训练验收通过”。

## 第五步：提交短测试，再用正式参数训练

1. 确认目标节点显示项目已安装。
2. 点击项目的 **“创建实验 / New experiment”**，在 **“节点与实验配置”** 中选择目标节点的 `Video_Games - 3 epoch check`。确认“本次实验参数”后，点击 **“提交实验 / Submit experiment”**。
3. 打开实验详情，看原程序日志、退出状态和输出文件。`stdout.log`、`stderr.log` 保存原控制台输出，`harness-invocation-1.json` 保存第一次执行的实际命令和参数。原程序正常结束且所需结果齐全后，再提交正式预设。
4. 以后换 seed、学习率、batch size，可以直接在网页修改实验参数；固定的数据路径、GPU 路径仍由 harness 填入。

任务运行过程是：

```text
本次实验参数 + 固定配置
          ↓
harness 生成命令/配置文件
          ↓
在独立工作副本里运行原 main.py
          ↓
收集 stdout、stderr、原日志与结果文件
          ↓
实验台显示状态、曲线和可下载结果
```

原源码保持不变；即使原程序向相对路径写文件，也写在这次任务的工作副本里。通过已有 `output_dir` 参数明确指定结果目录，查找和回传更直接。

## 原日志和断点怎么接？

**日志不要求改训练循环。** 控制台原文会保存；网页曲线通过 `harness.json` 中的规则，从 stdout、stderr 或输出日志文件提取数字。本例的训练输出已有 `epoch`、`rec_avg_loss`，评估输出有 `Epoch`、`HIT@10`、`NDCG@10` 等字段。扫描器能给出字段候选，你检查实际一行日志后确定匹配规则。原日志对验证集和测试集使用同一格式，所以本例曲线命名为 `eval/HIT@10`、`eval/NDCG@10`，不声称能够自动区分两者。没有曲线规则时仍能运行并看原日志。

**恢复只调用原程序已有的功能。** 默认关闭。如果另一个算法本来就有 `--resume checkpoint.pt`，可以在外部配置中声明恢复命令、需要保存的文件，以及原程序已有的安全停止方式；不用改它的代码。要确认该选项真的恢复训练进度、优化器等状态，而不是只加载模型做推理。本例的 `--do_eval` 就是评估，不作为恢复入口。

## 换成其他算法，重复哪些操作？

选择另一个源码目录，生成另一个外部配置目录，然后检查**入口、参数、数据、依赖、日志**五项，再打包、上传、选节点分发。原入口接受 JSON/YAML 时，可让 harness 生成它本来支持的配置文件，再通过原有 `--config` 参数传入；无需改成另一套训练 API。

第一次需要检查自动提取草稿。之后，同一接口的实验只换参数预设；代码、数据或调用规则更新则发布新项目包，旧实验仍引用它当时的固定版本。

这个流程没有自动多卡加速或自动寻找最优并发的功能。源程序没有的能力会保持关闭，不妨碍普通单卡实验接入。

## 本例已经验证到哪一步？

已在主力机 RTX 5070 Ti 上，用上述原版 `src/main.py` 和完整的原 `Video_Games.test.txt` 跑通外置 harness 的三轮测试；这是原数据，未替换成合成数据。原项目文件哈希保持不变，模型权重、stdout/stderr 和解析出的指标都已保存，依赖补充环境也完成了实际 CUDA 检查。恢复功能保持关闭。

这是主力机上的硬件验收，不代表远程 2080 Ti 已更新或已运行这份新项目。远程节点仍需要先运行更新入口，再接收项目并执行自己的短测试。
