# 历史参考：另一份 SASRec 实现的 SDK 适配练习

> 更正：本篇对应 `E:\PythonProjects\IntentPreference\SASRec_Original`，不是你指定的 `E:\PythonProjects\SASRec_Original`。下面的 `run-all`、`run_all()` 等接口不适用于后者。新项目请使用[不改源码的外置 harness 流程](EXTERNAL-HARNESS.zh-CN.md)；本篇仅保留给已经使用旧接口的项目。

本篇回答：**拿到一个算法项目后，怎样判断要改哪里，并亲手把它改成实验台能调用的形式。**

我们使用 SASRec_Original 的真实训练函数，重新推导并编写一个接入入口。顺序是教学顺序，不是声称重现早期开发的 Git 历史。完整练习文件是 [managed_sasrec.py](../examples/sasrec-adaptation/managed_sasrec.py)，里面按本篇顺序标了 1～6。

这份基础练习完成：读取任务参数、定位数据、隔离输出、执行原算法、逐轮上报指标、保存最终结果。最后一节再解释如何在此基础上做停止和恢复。基础练习明确拒绝恢复，不能把它当成完整生产适配器使用。

## 第 1 步：沿原来的启动命令，找到训练入口

先写出自己平常确实能运行的命令。SASRec 是：

```bash
python src/main.py run-all --config config/video_games_full.json --run-id manual-001 --resume never
```

打开 `SASRec_Original/src/main.py`，找到 `main()`。它做的核心事情是：

```python
config = load_run_config(args.config, args.run_id)
metrics = run_all(config, resume_mode=args.resume)
```

这两行告诉你：**已有程序可以通过“给一份配置，调用一个函数”来执行。** 因此新入口可以调用这两个函数，不必复制训练循环。

再顺着 `run_all` 读下去，实际调用关系是：

```text
src/main.py: main()
  → src/experiment.py: load_run_config()  读取和检查配置
  → src/experiment.py: run_all()          运行一次完整实验
    → _train_sasrec()                     epoch 循环、优化器、早停和断点
      → trainer.train_epoch()            执行这一轮的训练
    → _evaluate_sasrec_unlocked()         用最佳模型测试
```

拿到其他算法，先完成相同的追踪。搜索 `parse_args` 找启动参数，搜索 `for epoch` 找每轮循环，搜索 `optimizer.step` 和 `torch.save` 找训练和保存位置。如果整个项目都在脚本顶层执行，先把它收进 `main()`/`train()`，并通过 `if __name__ == '__main__':` 调用，使新增入口可以导入而不立即开始训练。

**本步完成标准：你能指出原项目哪一个函数真正负责跑一次实验。**

## 第 2 步：列出原算法需要的输入，再对接实验台

先读 `config/video_games_full.json` 和 `experiment.py` 的 `_DEFAULTS`，写出这个对应表：

| 原 SASRec 需要的输入 | 新入口从哪里取得 |
|---|---|
| `lr`、`seed`、`epochs`、`batch_size` 等科研参数 | `run.params` |
| 数据集名称，例如 `Video_Games` | `run.params['dataset']` |
| 数据所在根目录 | `run.assets[run.params['data_asset']]` |
| 结果写到哪里 | `run.output / 'sasrec'` |
| 用哪张 GPU | 节点已选择好；算法使用容器内逻辑 GPU 0 |

`Run` 从 `expman.sdk` 导入。它读取节点已经准备好的本地文件和环境变量，不是替你猜参数，也不要求训练程序自己连接管理中心。

例如一条任务提供：

```json
{
  "dataset": "Video_Games",
  "data_asset": "sasrec-data",
  "lr": 0.001,
  "seed": 42,
  "epochs": 3,
  "star_test": -1,
  "device": "cuda"
}
```

这里 `data_asset` 是节点登记的数据名称，假设叫 `sasrec-data`。节点把它挂载到容器后，`run.assets['sasrec-data']` 可能是 `/assets/sasrec-data`。`dataset` 则告诉原算法去这个目录下的 `Video_Games` 子目录读文件。这两个字段用途不同。

在你自己的算法中，**原参数叫什么就明确对应什么**。例如原参数是 `learning_rate`，你的启动文件需要把 `run.params['lr']` 赋给它，二者不会自动关联。

**本步完成标准：每个必要输入都有明确来源，没有把你自己电脑上的绝对路径写进训练程序。**

## 第 3 步：新建启动文件，把任务参数转换成原配置

为练习新建 `managed_sasrec.py`，不改模型文件。仓库已提供可运行全文；这段是其中的参数转换：

```python
run = Run()
data_root = run.assets[run.params['data_asset']]
output_root = run.output / 'sasrec'

raw = {
    key: value
    for key, value in run.params.items()
    if key in experiment._DEFAULTS
}
raw.update(
    dataset=run.params['dataset'],
    data_root=str(data_root),
    output_root=str(output_root),
    device=run.params.get('device', 'cuda'),
    gpu_id=0,
)

config_path = run.output / 'sasrec-input.json'
config_path.write_text(json.dumps(raw), encoding='utf-8')
```

**为什么要写一个 JSON？** 因为第 1 步发现原来的 `load_run_config()` 接受配置文件路径。我们把实验台输入转成它已经认识的格式，就能继续用原来的配置校验和默认值。

过滤 `_DEFAULTS` 是为了不把 `data_asset` 这种实验台字段当成算法参数。完整练习还检查了未知字段，避免学习率拼错却悄悄采用默认值。

如果其他算法接受 Python 配置对象，就直接构造对象；如果只接受命令行，就构造命令行参数列表。**这三种接法取决于原项目入口，不要求把所有算法都改成 SASRec 的配置格式。**

## 第 4 步：解决这个项目真实存在的输出限制

不能写完 `output_root` 就假定接好了。SASRec 的 `load_run_config()` 里实际有：

```python
output_root = _project_path(config.get('output_root', 'output'))
if output_root != OUTPUT_ROOT:
    raise ValueError('output_root 必须是 SASRec_Original/output')
```

如果直接传 `/workspace/run/sasrec`，这段检查会拒绝它。因此新入口在调用原配置加载函数之前，要做：

```python
experiment.OUTPUT_ROOT = output_root
output_root.mkdir()
config = experiment.load_run_config(config_path, 'managed')
```

第一行只调整这次独立 Python 进程里原模块的变量，没有改源码文件。第二行先创建根目录，因为原算法还要在下面创建运行锁目录。实际模型会写到：

```text
run.output/sasrec/Video_Games/managed/sasrec_last.pt
run.output/sasrec/Video_Games/managed/sasrec_best.pt
```

`managed` 是这次配置中的 run ID；不同任务有不同 `run.output`，所以不会互相覆盖。

现在可以真正调用：

```python
metrics = experiment.run_all(config, resume_mode='never')
run.finish({'test_metrics': metrics})
```

做到这里，就已经完成“能启动、读取正确输入、保存结果”的基本接入。

**迁移到其他算法时，不要照抄 `experiment.OUTPUT_ROOT`。** 应检查它所有输出路径来自哪里，把模型、日志和派生缓存都导向本次输出目录；如果有其他路径校验，也要一起处理。

## 第 5 步：在真正产生指标的地方接入曲线

在 `experiment.py` 的 `_train_sasrec()` 中，每轮训练末尾实际先保存断点，再写一条结构化日志：

```python
save_checkpoint(last_path, final_state)
_append_log(output_dir, {
    'stage': 'train',
    'epoch': epoch,
    'global_step': global_step,
    'train_loss': train_loss,
    'valid_metrics': valid_metrics,
})
```

上面只摘出了本步需要看的字段；原函数还记录耗时、早停等字段。这个位置已经拥有准确的 epoch、loss 和验证结果，所以它是接入点。

新入口保留原日志功能，再把同一份数据交给实验台：

```python
original_log = experiment._append_log

def on_log(path, payload):
    original_log(path, payload)
    if payload.get('stage') == 'train' and 'global_step' in payload:
        run.metric(
            int(payload['epoch']),
            train_loss=float(payload['train_loss']),
        )

experiment._append_log = on_log
try:
    metrics = experiment.run_all(config, resume_mode='never')
finally:
    experiment._append_log = original_log
```

理解成：**原训练程序每轮仍调用同一个日志函数；这次运行给该函数增加了“顺便上报指标”的动作。** 全文还把验证指标写成 `valid/HR@10` 等名称。

SASRec 恰好有这个统一日志函数，所以可以从外面接。其他项目如果没有现成的事件出口，最直接的改法是在每轮得到指标后增加一行 `run.metric(epoch, loss=float(loss))`。若希望训练代码不依赖实验台，则给原训练函数增加可选的 `on_epoch` 参数，再在同一位置调用它，让新增入口传入指标上报函数。

**本步完成标准：只训练三轮时，`metrics.jsonl` 中能找到 step 1、2、3 的训练指标；不是等全部训练完才报一个数。**

## 第 6 步：验证接入有没有改变实验

不要仅以“没有报错”判断接入正确。按顺序检查：

1. 固定代码、数据、seed 和参数，先用小模型、小数据、少量 epoch；不要把教学小参数覆盖到正式科研配置。
2. 检查生成的 `sasrec-input.json`：学习率、batch size、seed 是否和输入相同；数据和输出路径是否指向这次任务。
3. 检查原项目没有新增输出，所有模型、日志和最终 `result.json` 都写到任务目录。
4. 检查每轮指标的数量和 step。
5. 在相同运行环境中，对比原训练流程与新入口的模型、优化器、随机状态、epoch、global step 和测试指标。对本例已做与现有生产适配器的对照；两者都调用原 SASRec 训练函数。

完整练习的命令形状是：

```bash
python managed_sasrec.py --project /workspace/code/SASRec_Original
```

节点会提供 `EXPERIMENT_PARAMS`、`EXPERIMENT_OUTPUT`、`EXPERIMENT_ASSETS`。普通终端没有这些变量，所以直接执行会报缺少变量；这不是算法代码坏了。

为此配套提供了完整的 [check_lesson.py](../examples/sasrec-adaptation/check_lesson.py)。它自动生成小数据、三轮参数和这些变量，并执行教学入口与参考入口进行对比。在已安装 PyTorch、NumPy 的 Python 环境中，从 experiment-manager 源码根目录执行（`--project` 换成你的算法路径）：

```bash
python3 examples/sasrec-adaptation/check_lesson.py \
  --project /mnt/e/Research/SASRec_Original \
  --root ./sasrec-lesson-01 \
  --device cpu
```

`--root` 必须是不存在的新目录；修改入口后再测，换成 `sasrec-lesson-02`。该练习用原 SASRec 真正训练，只是数据和模型尺寸很小。CPU 检查不证明 GPU 部署通过；在已经分配好单张 GPU 的容器中可换成 `--device cuda`。

成功时显示 `PASS`。打开结果目录中的 `lesson/sasrec-input.json`、`lesson/metrics.jsonl`、`lesson/result.json` 和 `lesson/sasrec/Toy/managed`，能亲眼看到转换后的配置、每轮指标和模型。测试不会启动实验台，也不会提交任务。

要自己写测试时，关键原理如下（完整自动处理的版本以上述脚本为准）：

```python
env = dict(os.environ)
env['EXPERIMENT_PARAMS'] = str(params_json_path)
env['EXPERIMENT_OUTPUT'] = str(output_dir)
env['EXPERIMENT_ASSETS'] = json.dumps({'sasrec-data': str(data_root)})
subprocess.run(command, env=env, check=True)
```

这里是测试接口的说明片段，变量必须指向你已创建的参数文件、数据目录和新输出目录；不要把它当成可以单独粘贴运行的完整脚本。

## 第 7 步：需要“保存后停止、继续训练”时，再补这条链

基础练习明确拒绝恢复。完整接入还必须打通：

```text
原算法把完整状态保存到文件
  → 新入口向实验台登记这个断点
  → 检查是否收到停止请求，收到才结束训练
  → 下次运行读取、校验并恢复同一断点
  → 从下一轮继续
```

### 7.1 先检查原算法的断点是否完整

SASRec 的 `build_checkpoint()` 保存了 `model`、`optimizer`、`early_stopping`、`rng_state`、`epoch`、`global_step`、配置等。这里 `rng_state` 又包含 Python、NumPy、Torch 和 CUDA 的随机状态。

读 `_train_sasrec()` 的恢复分支，实际有：

```python
model.load_state_dict(last['model'])
optimizer.load_state_dict(last['optimizer'])
stopper.load_state_dict(last['early_stopping'])
restore_rng_state(last['rng_state'], device)
global_step = int(last['global_step'])
start_epoch = int(last['epoch']) + 1
```

这说明它已具备算法自己的续训能力。**其他算法如果只保存 `model.state_dict()`，必须先把缺失状态补上。** 学习率调度器、AMP scaler、独立随机生成器、采样器等是否需要保存，取决于它们是否参与决定接下来的训练状态。随机状态通常在创建模型/优化器等对象后、下一次训练迭代前恢复，以免初始化再次消耗随机数。

### 7.2 把算法断点交给实验台

`run.publish_checkpoint(file, step=epoch)` 登记的是一个已经写完的文件；它不会替你保存模型。

SASRec 的恢复还需要彼此一致的 `sasrec_last.pt`、可能存在的 `sasrec_best.pt` 和 `config.json`，因此生产适配器的 `publish_bundle()` 把它们打进同一个 ZIP，再调用 `run.publish_checkpoint()`。恢复时 `restore_bundle()` 验证摘要、配置并解包。对其他算法，若一个完整断点文件已经包含全部必要状态，就不需要照搬 SASRec 的三个文件布局。

### 7.3 把停止检查放在保存成功之后

在第 5 步的每轮回调里，顺序应为：

```python
publish_bundle(run, directory, epoch)
run.metric(epoch, train_loss=float(payload['train_loss']))
if run.should_stop():
    raise SavedStop()
```

这里 `publish_bundle` 是生产适配器中实际存在的打包函数，`directory` 是 `Path(config['output_dir'])`，`SavedStop` 是该适配器定义的专用异常。外层只捕获这个异常以正常停止，其他错误继续报错；停止时不调用表示实验完成的 `run.finish()`。

新一轮执行若 `run.resuming` 为真，就先用 `run.checkpoint()` 取得并校验登记的断点，恢复所需文件，再调用 `run_all(config, resume_mode='require')`。缺断点或配置不一致应报错，不能悄悄从头训练。

### 7.4 用同一实验做 A/B 验证

- A：连续训练三轮。
- B：完成第一轮、保存并停止，再恢复到第三轮。

比较完整训练状态和最终指标；对于本 SASRec，在相同环境、代码、参数和数据下已经能逐项精确比较。其他算法应根据其确定性条件定义合理检查标准，不能把“恢复后还能打印 loss”当作正确恢复。

## 换到下一个算法时，按这张表找对应位置

| 在 SASRec 中做的事情 | 下一个项目中要找什么、要改什么 |
|---|---|
| 找 `main → load_run_config → run_all` | 找它的启动参数和完整训练入口 |
| `run.params → raw → 配置 JSON` | 写它自己的参数名/配置格式映射 |
| `run.assets → data_root` | 替换数据定位方式，保留原数据处理逻辑 |
| `run.output → OUTPUT_ROOT/output_root` | 找齐输出、缓存路径和相关校验，导向任务目录 |
| 在 `_append_log` 接指标 | 找每轮完成且指标已算好的位置，增加上报或回调 |
| 检查 `build_checkpoint` 和恢复分支 | 保存、恢复全部会影响后续训练的状态 |
| 保存后登记断点，再检查停止 | 在安全边界完成停止和继续训练 |
| 三轮短测、连续/恢复对照 | 验证接入没有改变原实验行为 |

最后需要交给任务配置的是：**这份新增启动文件的执行命令、固定版本的代码、数据资产名称、运行依赖和科研参数。** 把这些放到哪台机器属于部署步骤，不是再次适配算法。
