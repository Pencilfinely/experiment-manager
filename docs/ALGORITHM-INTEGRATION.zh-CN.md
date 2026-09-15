# 算法接入和跨机器运行：从改文件到提交实验

[English](ALGORITHM-INTEGRATION.md) · [安装算力端](../README.zh-CN.md)

**实际流程：网页填写参数 → 算力机接到任务 → 容器运行你的训练入口 → 日志、指标和模型回传网页。**

这里有两个独立问题：训练程序怎样接收任务，以及算力机怎样拿到代码和数据。本篇把两条流程都走完。

先说明现状：**网页没有“上传算法并自动同步到所有机器”的功能。** 原来的“登记项目”只登记执行操作的那台机器。在主力机上登记后，另一台机器不会自动获得项目。

本文的 SASRec 转移工具是当前源码新增功能，**不在已发布的 v0.2.0-rc.1 安装 ZIP 中**。生成的项目包自带安装工具，接收机器不需要为了导入而覆盖原来的算力端软件。公开软件不附带你的算法和数据；项目包由你从自己的项目生成。

## 一、你这份 SASRec_Original，究竟改了哪里？

支持的项目结构如下。`data` 和 `SASRec_Original` 是同级目录，所以配置写的是 `data_root: "../data"`。

```text
SASRec_Original/
├── src/
│   ├── main.py
│   ├── experiment.py
│   ├── datasets.py
│   ├── models.py
│   ├── modules.py
│   ├── trainers.py
│   └── utils.py
└── config/video_games_full.json
data/Video_Games/
├── Video_Games.train.txt
├── Video_Games.valid.txt
└── Video_Games.test.txt
```

原来手动运行：

```bash
python src/main.py run-all --config config/video_games_full.json --run-id manual-001 --resume never
```

实验台的任务模板改为调用下面这个入口，用户提交时不需要自己输入它：

```bash
python -m expman.adapters.sasrec --project /workspace/code/SASRec_Original
```

**新增入口就是本软件的 [expman/adapters/sasrec.py](../expman/adapters/sasrec.py)。** 它是一份转换输入输出的 Python 文件，具体做这些事情：

| 实验台提供什么 | 这份启动文件怎样交给原算法 |
|---|---|
| 参数 `lr`、`seed`、`epochs` 等 | 写入本次的 `sasrec-input.json`，用原 `load_run_config()` 加载 |
| 节点登记的数据目录 | 转成原配置的 `data_root`，原算法照常读取三个文件 |
| 本次实验专属的输出目录 | 转成 `output_root`，原算法把模型和日志写到这里 |
| 节点分配的显卡 | 设置 `device="cuda"`、`gpu_id=0`，0 是容器内编号 |
| 每轮的 loss、验证指标和断点 | 在原算法每轮结束的回调中登记，交给网页展示和归档 |
| 保存后停止 / 恢复命令 | 完整 epoch 保存成功后退出；再次执行时调用原算法恢复入口 |

最后它调用原来的 `run_all()`。模型结构、损失函数、优化器和数据处理仍由原项目实现。

**为什么这份 SASRec 不用再改训练代码？因为它已经有配置加载、完整断点和每轮回调。** `experiment.py` 中的 schema 3 和 `external_sasrec_original_v1` 用来识别这个已有接口。不能往另一个 SASRec 随便添加这两行就算完成适配；这也不是说任意算法都能零修改接入。

## 二、把这份 SASRec 交给 2080 Ti、笔记本或 A6000

### 第 1 步：在存放算法和数据的机器上导出

在 **Ubuntu/WSL 终端，experiment-manager 当前源码目录**中执行：

```bash
python3 -m expman.sasrec_package export \
  --project /mnt/e/Research/SASRec_Original \
  --config config/video_games_full.json \
  --output ./SASRec-Video_Games-v1.zip
```

三个参数分别是：算法文件夹、相对算法文件夹的科研配置、准备发送的 ZIP。`--project` 上面写的是 Windows E 盘经 WSL 访问的路径示例，换成你的实际目录。Windows Python 也能导出，此时可以直接用 `E:\...` 路径。

**成功标志：显示 ZIP 的完整路径、大小和 SHA256。** 已有同名文件不会覆盖，下次改用 `v2.zip`。

包内带上 `src` 下的 Python 源码、配置和 train/valid/test 三个数据文件；不包含旧结果、原 Git 历史或节点令牌。原项目不需要提交 Git，也不会被改动。包内仅重定位数据和输出路径，保留科研参数。

### 第 2 步：用 Xftp 传这个 ZIP

**目标为 Windows（例如 2080 Ti）：**

1. Xftp 打开已有的目标机器连接；左侧找到导出的 ZIP。
2. 右侧进入 `/E:/ExperimentTransfer`，把 ZIP 拖到右侧，等传输完成。
3. 用 Moonlight 打开目标机器桌面，在资源管理器进入 `E:\ExperimentTransfer`。
4. 右键 ZIP → **全部解压缩**。不要在 ZIP 预览窗口直接运行。

Xftp 的 `/E:/ExperimentTransfer`、Windows 的 `E:\ExperimentTransfer`、WSL 的 `/mnt/e/ExperimentTransfer` 指向同一个目录。如果目标机器使用其他盘，按实际位置替换。

**目标为原生 Ubuntu（例如 A6000）：**

1. 在服务器终端执行 `mkdir -p "$HOME/ExperimentTransfer"`，再执行 `echo "$HOME/ExperimentTransfer"` 查看完整路径。
2. Xftp 连接服务器，右侧进入这个目录，把 ZIP 上传进去。
3. 在服务器终端执行：

```bash
cd "$HOME/ExperimentTransfer"
python3 -m zipfile -e SASRec-Video_Games-v1.zip SASRec-Video_Games-v1
cd SASRec-Video_Games-v1
```

### 第 3 步：在目标机器安装项目

前提：该机器的算力端已安装、已配对并通过 GPU 环境检查。项目包不能代替首次安装算力端。

1. 打开 Docker。等该节点现有实验完成，网页“待回传文件”为 0，再退出它的算力代理窗口。
2. Windows：在解压目录双击 **`Install-Project.cmd`**。
3. Ubuntu：在刚才的解压目录执行 **`bash Install-Project.sh`**。
4. 如果要求选择 WSL，选择代理使用的 Ubuntu。如果列出多份配置，选择代理启动命令中 `--config` 后面的那份；不要凭编号猜。

工具会检查目标机现有镜像能否导入 PyTorch、NumPy 和 SASRec 适配器，再把源码和数据装到节点自己的目录，生成它自己的模板。**镜像、节点标签和身份取自目标机，不从发送方复制。** 修改前的配置会备份，已有项目和身份保留。

成功标志：

```text
INSTALLED / 安装完成: sasrec-video-games-short, sasrec-video-games-formal
```

旧版/自定义安装可以直接指定配置。在目标机器 **Ubuntu 终端、解压目录内**执行：

```bash
python3 Install-Project.pyz install --node-config "$HOME/.local/share/experiment-manager/worker/node.ready.json"
```

上面是新版默认安装的路径示例。自定义或旧版部署填它自己代理启动命令实际使用的配置；不要为套用示例而移动配置文件。

### 第 4 步：启动该节点，网页提交

1. 用该节点**原来的入口、原来的配置**重启代理，不需要重新配对。
2. 刷新实验台，找到这个节点卡片，例如 `win2080`。
3. 点击该节点下的 **`填入任务 / Use: sasrec-video-games-short`**。
4. 参数组合保持 **`{}`**，点击 **“提交到队列”**。
5. 打开任务详情，核对执行节点。日志开始出现 epoch，等状态“已完成”并确认模型/结果已回传。
6. 短跑成功，再选择该节点下的 **`sasrec-video-games-formal`** 提交正式实验。

`short` 只把 `epochs` 改成 3、`star_test` 改成 -1；`formal` 使用原科研参数。两者使用节点选择的 GPU，在容器里表现为逻辑卡 0。

任务默认预算是 CPU 2、RAM 8192 MiB、GPU 6000 MiB、独占单卡。节点预算不满足会排队；预算不是显存预分配，也不保证任意模型大小都能放下。每种新硬件都要先短跑。

### 以后哪些情况要重新传？

| 变化 | 你要做的事 |
|---|---|
| 只换 seed、lr 等，代码和数据没变 | 在网页改任务参数或参数组合，直接提交 |
| 改模型/训练代码 | 导出新 ZIP → 传到目标节点 → 等任务和回传结束后退出代理 → 安装 → 重启代理 → 用新模板提交 |
| 修改数据文件 | 同上，数据版本按实际文件内容生成 |
| 增加一台机器 | 先安装并配对算力端，再传同一个项目包，安装并短跑 |
| 新增第三方包或 CUDA 扩展 | 先更新并验证目标节点运行镜像；项目包不会自动猜测这些依赖 |

已提交的实验保留原版本。**保存原项目文件不会实时更新其他机器；目前“同步”就是显式导出、传输、安装。**

## 三、如果是一个从未接入过的算法，具体改哪几个文件？

先做到：**一条普通训练命令能接收超参数、数据目录和输出目录。** 例如：

```bash
python train.py --data-dir ./example-data --output-dir ./manual-run --lr 0.05 --epochs 20 --seed 42
```

如果你的训练程序把这些值写死了，就先把它们改成参数。模型、日志、临时缓存都写到输出目录；容器内的源码和原始数据只读。GPU 程序使用节点分给容器的 `cuda:0`。

### 1. 先运行一份完整接入例子

仓库的 [examples/managed-project](../examples/managed-project) 内有：

```text
train.py          原来的训练程序，没有引用实验台 SDK
expman_entry.py   接入时新增的启动文件
check_local.py    比较接入前后结果的检查脚本
example-data/train.csv
```

这是用纯 Python 拟合直线的 CPU 小例子，方便完整验证输入输出；它不是 GPU 性能测试，也不是 SASRec。

在 **experiment-manager 源码根目录**运行：

```bash
python3 examples/managed-project/check_local.py
```

Windows 可用 `python` 代替 `python3`。出现下面的输出表示普通入口和接入入口产生相同模型和结果，指标和结果已按实验台格式写出：

```text
PASS: identical model and results; managed metrics/result emitted; unsupported resume rejected
```

### 2. 新增文件怎样调用旧文件？

打开完整的 [expman_entry.py](../examples/managed-project/expman_entry.py)。核心转换如下；要运行请使用链接中的完整文件：

```python
run = Run()
data_dir = next(iter(run.assets.values()))
command = [sys.executable, str(Path(__file__).with_name('train.py')),
           '--data-dir', str(data_dir), '--output-dir', str(run.output),
           '--lr', str(run.params.get('lr', 0.05)),
           '--epochs', str(run.params.get('epochs', 20)),
           '--seed', str(run.params.get('seed', 42))]
subprocess.run(command, check=True)
```

`Run()` 就是读取这次任务的参数、数据位置和输出位置。例子要求恰好一个数据目录，因此可直接取出它；多个数据输入应按资产 ID 分别选择。

网页的 `{"lr": 0.01, "epochs": 100, "seed": 7}` 会被这段代码转成原程序认识的 `--lr 0.01 --epochs 100 --seed 7`。**网页字段不会自动变成任意算法的参数名，需要这个明确的转换。**

接入你自己的项目时，实际修改清单是：

1. 把这个新增文件放到原训练入口旁边。
2. 把 `train.py` 换成实际入口文件名。
3. 按原算法 CLI 修改参数列表。例如原程序叫 `--learning-rate`，就把命令中的 `--lr` 换成它，网页仍可叫 `lr`。
4. 同时更新文件顶部允许的参数集合和默认值，避免新增参数被判为拼写错误。
5. 把末尾读取 `summary.json` 的部分换成原算法实际输出的结果格式，再调用 `run.finish(result)`。例子中的 `train.py` 确实会产生此文件；不能假定任意项目都有它。

以后实验台执行的命令就是 **`python expman_entry.py`**。原程序已支持这些输入时，可以只新增启动文件；路径和参数写死时，还需要先解除硬编码。

### 3. 日志、曲线、暂停恢复各需要什么？

- **终端日志**：例子继承子进程输出，原来的 `print()` 会进入网页日志。
- **指标曲线**：例子训练完上报一个最终 loss 点。需要每轮曲线时，在训练程序得到该轮 loss 后调用 `run.metric(epoch, loss=float(loss))`，或者暴露 `on_epoch` 回调让启动文件接收指标。日志里的数字不会自动变成曲线。
- **模型文件**：写到 `run.output` 下，代理会归档。写回源码目录会失败。
- **保存后继续训练**：要额外保存并还原模型、优化器、epoch、随机状态，以及算法用到的采样器、学习率调度器和 AMP 状态。只有模型权重不够。例子明确拒绝恢复，避免把重新训练冒充恢复。

不要为“接入”修改模型公式或添加不正确的断点逻辑。你这份 SASRec 已有完整实现，所以使用第一节的专用启动文件即可。

## 四、普通算法怎样放到多个节点？

SASRec 转移工具只支持第一节的接口。普通算法目前要把**已适配项目目录**和**数据目录**传给每个节点，然后在节点登记。

先用完整例子走一次：在**目标机器 Ubuntu**中，从 experiment-manager 源码根目录执行：

```bash
mkdir -p "$HOME/ExperimentProjects/linear-example" "$HOME/ExperimentData/linear-example"
cp examples/managed-project/train.py examples/managed-project/expman_entry.py "$HOME/ExperimentProjects/linear-example/"
cp examples/managed-project/example-data/train.csv "$HOME/ExperimentData/linear-example/"
cd "$HOME/ExperimentProjects/linear-example"
git init
git add train.py expman_entry.py
git -c user.name=ExperimentUser -c user.email=local@example.invalid commit -m "Record runnable training entry"
```

这几条 Git 命令只给刚创建的例子目录记录版本，没有上传 GitHub。换成真实算法时，用 Xftp 把改好的代码和数据放到这两个目录；在真实仓库中只提交你确实要运行的文件。

退出代理，再打开算力端安装包中的 **`Configure-Project.cmd`**（Windows）或 **`bash Configure-Project.sh`**（Ubuntu），按下面回答：

| 提问 | 例子中的回答 |
|---|---|
| Project name / 项目名称 | `linear-example` |
| Local Git repository folder / Git 仓库目录 | `~/ExperimentProjects/linear-example` |
| Training command / 训练命令 | `python expman_entry.py` |
| Dataset folder / 数据目录 | `~/ExperimentData/linear-example` |

入口使用默认算力端数据目录。自定义安装应在软件源码根目录执行 `python3 -m expman.project_setup --root 路径`，路径填保存 `node.ready.json` 的目录；通用登记器不支持其他配置文件名，不要随意重命名运行中的配置。

登记会选择节点已有的一个已验证环境，不会补装算法依赖。这个纯 Python 例子无需额外包。真实算法必须确认依赖在容器镜像里；宿主机 Conda 包不会自动进入容器，训练容器也没有网络用来临时下载依赖或数据。

重启代理后，在网页该节点下选择 `linear-example` 模板，把任务 `params` 改为 `{"lr": 0.05, "epochs": 20, "seed": 42}`，参数组合保持 `{}`，提交。这个例子实际用 CPU，但当前通用 Docker 调度仍为它占用一个 GPU 槽位，不要用它测 GPU 吞吐。

另一台机器重复“传文件 → 登记 → 重启代理”，选择**那台节点下面**的模板。之后只换参数无需重新传输。

**上传 GitHub 只让代码可以被获取，不会自动分发数据、安装容器依赖或登记节点。** 当前模板包含节点自己的路径和环境，复制模板后只改机器名通常不够。从网页统一发布项目、自动分发、一个模板自动匹配所有兼容节点，目前仍未实现；单卡算法也不会自动变成多卡合训。
