# DCU/HCU 专项参考

用于 Hygon DCU/HCU、HIP/DTK 及厂商分支。以下是检查方向，具体工具/参数按目标镜像与设备验证；本 skill 不捆绑模型、DTK、profiler 或测试执行器。

与 NVIDIA 对照时，按 [跨平台对照准入](cross-platform-parity.md) 先核对已有证据，把只有实测能确认的条件放进首轮最小对照。关注实际加载权重、量化、最终有效配置、请求编码与执行阶段；命令行字面相同不证明语义对齐。

## 最小运行信息

先读已有启动日志、镜像和项目脚本。按需确认物理卡与 gfx 架构、driver/DTK/HIP、torch/Triton/flash-attn/LightOp/AITER/DeepGEMM 的实际版本，源码 commit 与 dirty patch，以及模块/so 的加载路径。

可在已授权目标中用下列轻量探针；不用为已有日志重复执行：

```python
import torch
print('torch', torch.__version__, 'HIP', torch.version.hip,
      'CUDA build', torch.version.cuda)
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(torch.cuda.current_device())
    print('device', p.name, 'arch', getattr(p, 'gcnArchName', 'unknown'))
```

HIP 版 PyTorch 也使用 torch.cuda API，名称本身不证明是 NVIDIA。device ordinal 只在该进程可见设备映射中有效；同卡回切要记录物理设备标识。只列出任务相关环境变量，避免导出含凭据的完整 env。

## 实际路径优先

厂商补丁常增加 `_is_hcu`、`_is_hip`、架构条件、私有 `_out` 或 legacy 分支。重点确认：

- HCU 条件是否进入与上游不同的函数；上游已修复是否覆盖了私有入口。
- 同一 runner 名称下，实际量化类、GEMM、激活、scale/layout、通信 backend 是否相同。
- 同一 gfx 名称不代表另一张卡、另一 DTK 或编译产物等价；gfx936 与 gfx938 的代码对象、wave 与 dispatch 不混用结论。
- 修改 Python/JIT 源码后，进程是否加载新路径；查 so、JIT 依赖哈希和有限次入口计数。按必要条件重启，不能靠清缓存掩盖加载问题。
- eager/graph、prefill/decode/verify、TP/DP/EP 不同路径分别是否触发。启动日志与真实请求阶段都要能对应到本轮运行。

## 后端与融合开关实验

先检查该版本的参数解析、默认值、消费者及支持矩阵，再决定对照。启动脚本未写变量可能仍启用默认路径；删掉 export 不等于关闭功能。

保留原始权重、量化方法和有效输入。可选择项目已支持且有参考依据的较简单实现对照，但不预设 Triton、math 或 legacy 一定正确。FP8/INT8 使用各自有效参数，不统一改成某种量化选项。

大组开关二分只能在以下条件下用于筛查：组内变更已记录、实际 dispatch 验证、请求/算子可复现。组变更有效后继续拆分；无效只排除已验证的配置组合，不能排除所有融合算子。兼容性要求 page size、attention backend 等联动时，将它们视为一组计划变量并注明混杂。

按问题选择最小对照：

| 问题 | 对照与结论边界 |
|---|---|
| 新旧版本是否有回归 | 尽量固定物理卡、有效配置和输入，只换实际加载的版本或产物，比较同一条断言。若镜像、运行库或补丁一起变化，只归因到版本组合。 |
| 同版本下某个参数是否触发 Bug | 固定版本和输入，只改目标参数，核验两边的有效值和实际分派。对照两条路径都应满足的断言；参数有意改变语义时，输出差异本身不足以判 Bug。条件允许时用 A → B → A 排除随时间变化的因素。 |
| 是否只有新版的特定参数组合失败 | 先复现最有信息量的一对；需要区分版本效应和参数效应时，再补「旧版/新版 × 参数 A/B」。不支持、未触发或缺证据的格子标不可判定。只有新版 B 失败时，下一步检查它实际执行的路径，还不能直接指认某个 kernel。 |

## 按需复用通用技能

同一轮先保留原始证据。用可核验的运行、用例或请求标识及来源位置交接；以下技能各自可读原始证据，无需依次运行。没有相应技能时，可以直接填写本技能的 [定位模板](../assets/diagnosis-template.md)。

| 需要额外交付 | 可选技能 | 交给它的证据 |
|---|---|---|
| 两轮实际条件和可比范围 | `experiment-manifest` | 启动记录、实际加载产物、有效参数和路径证据。 |
| 目标路径是否触发、断言是否成立 | `regression-evidence` | 触发位置、有效观测、参考及判定规则；缺失记录单列。 |
| 输出变长、重试或缓存消耗的量化 | `token-ledger` | 带请求标识与用量的原始日志；仅在问题涉及用量时使用。账本数据不能作为数值正确性判据。 |

## DSV4 的针对性检查

- 配置要求 swiglu_limit 时，沿 config → MoE 配置 → runner → 激活/量化 kernel 核对传参与执行。两次 GEMM 间的 clamp 对象、位置及量化顺序必须一致。
- 固定 GEMM-1 输出，比较 legacy/new runner 的中间结果，特别是越过 clamp 边界的 gate/up 输入；阈值与语义由模型和可信实现确定。
- 投机正确性检查 verify 宽度、有效 token、ring 写读范围、回退位置。关闭投机是对照变量，不是证明所有剩余输出都正确。
- 纯 prefill 同样提前 EOS 时，候选仍包括共用的 MoE、权重/scale、归一化、attention/indexer；不把问题自动缩到 attention 或硬件。

## 工具按能力选择

| 目的 | 可检查的工具 | 适用边界 |
|---|---|---|
| 模块/算子与调用阶段 | 项目入口日志、有限 hook、torch profiler | 先确认构建支持、事件与相关 rank 可见性；数值定位不要求先 profiling。 |
| 实际 kernel/代码对象 | 目标 DTK 的 rocprofv2、hipprof 或项目已有 tracer | 先探测 command/help 和小范围采集；不同 DTK 参数、计数器名称与语义不能照抄。 |
| 已定位到设备越界/同步 | hipgdb、debug agent、相关 dump | 按调试工具实际可用性和授权范围使用；不把 kernel 崩溃的异步落点当成首个故障点。 |
| NVIDIA/Ascend 工具迁移 | ncu/nsys、CANN/ATB、npu-smi 等作为源平台背景 | 不作为 DCU 可执行方案；需要替换执行器并重新验证观测语义。 |
| AMD Magpie 等执行框架 | 独立的能力验证，之后再考虑复用 | 有 HIP 支持不代表 Hygon 架构、DTK、设备枚举和 profiler 支持，不能据此宣称兼容。 |

工具缺失时标记 NOT_AVAILABLE，继续源码、已有日志或离线张量分析。不要自动创建特权容器、装驱动、改设备时钟或压测共享服务。

## 本地能力验证的最小范围

按当前任务选一条链验证：固定输入是否可重放、目标中间量是否可采集、所需 kernel 路径是否可观测。逐项记录“方法可迁移 / 参数需适配 / 执行器未验证”；一次小样本通过不代表整个工具链或所有架构均通过。

HIP API 命名依据：[PyTorch HIP semantics](https://docs.pytorch.org/docs/2.15/notes/hip.html)。版本与目标构建的差异以实际运行证据为准。
