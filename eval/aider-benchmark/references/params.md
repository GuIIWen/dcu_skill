# aider-benchmark 参数与配置说明

这里的数字、行为都来自 2026-10-22 / 10-23 在 tencent05/06 上的实测或读源码，写明了依据；没验证的单独标出。

## 1. benchmark.py 命令行参数（用过的）

| 参数 | 本环境用法 | 说明 |
|---|---|---|
| `<运行名>`（位置参数） | `think-full`、`max-effort-16` 等 | 结果目录名 = `<UTC 时间>--<运行名>` |
| `--model` | `openai/<模型名>` | 模型名要和 `--read-model-settings` yml 里的 `name` 一致，yml 才会生效 |
| `--edit-format` | `diff`（全量测试）；早期试跑用过 `whole` | |
| `--threads` | 8 | 并发的习题数 |
| `--tries` | 2 | 每题最多两轮；第一轮没过，把测试报错发回再试第二轮 |
| `--num-tests` | 16、8，或不设（全部 225） | 先随机打乱再取前 N 个，所以不加 `--keywords` 时每次抽到的题不同 |
| `--keywords` | `go/exercises/practice/counter,java/...` | 逗号分隔，按**子串**匹配习题相对路径；要复现同一批题就精确列出 |
| `--new` | 每次都加 | 新建带时间戳的结果目录并复制整个数据集（约 105 万文件，要几分钟） |
| `--reasoning-effort` | `max` | 见第 3 节，进请求体的 `reasoning_effort` |
| `--read-model-settings` | `/benchmarks/repro/think600.yml` | 见第 2 节 |
| `--exercises-dir` | `/benchmarks/polyglot-benchmark` | 数据集目录（容器里的路径） |
| `--stats <结果目录>` | 看汇总 | 目录不足 225 题时会提示 incomplete，正常 |
| 环境变量 `AIDER_BENCHMARK_DIR` | `/benchmarks` 或 `/benchmarks/<子目录>` | 结果放哪里；用子目录可以把不同实验分开 |
| `--thinking-tokens` | **没用** | 会把 `use_temperature` 置 False（`models.py` 的 `set_thinking_tokens`），改变请求；本环境思考模式靠 yml 里的 `chat_template_kwargs.thinking` |

## 2. 模型设置 yml（`configs/`）

```yaml
- name: openai/DeepSeek-V4-Flash-0731-Channel-FP8-w8a8
  extra_params:
    timeout: 600          # 单次请求超时（秒），默认值是 600
    max_retries: 0        # 关掉 openai 客户端自己的重试
    temperature: 1        # 只有 think600_temp1.yml 有；不写就是 0
    extra_body:
      chat_template_kwargs:
        thinking: true    # 思考模式
```

- `extra_params` 是在 aider 设好 `temperature` 之后才 `kwargs.update` 的（`aider/models.py`），所以能覆盖它。
- `extra_body` 里的字段会进请求体顶层；`timeout`、`max_retries` 是客户端参数，**不进请求体**。
- **不关 `max_retries` 的后果**：openai 客户端默认自己再重试 2 次，一次 aider 调用会变成最多 3 个请求。用假服务（只接收连接、从不回复）实测：不设 `max_retries: 0` 时，一次调用连到服务 6 次（aider 层 2 次尝试 × 客户端 3 次）；设了 0 之后是 2 次。
- 请求体最终长什么样，用假服务抓过（`think600_temp1.yml` 加 `--reasoning-effort max`）：

```json
{"model": "DeepSeek-V4-Flash-0731-Channel-FP8-w8a8", "temperature": 1,
 "chat_template_kwargs": {"thinking": true}, "reasoning_effort": "max"}
```

不带 `--reasoning-effort` 和 temperature 覆盖时，请求体里只有 `model`、`temperature: 0` 和 `chat_template_kwargs`。非流式（`benchmark.py` 里写死 `stream=False`）。

## 3. temperature

- 默认是 **0**：`use_temperature` 默认 `True`，发送时 `True` 对应 `temperature = 0`（`aider/models.py`）。只有某些模型族会把它设成 `False`（不发 temperature），本环境的模型名在 `model-settings.yml` 里没有条目，不命中。实际保存下来的请求体里也是 0。
- 改成别的值：用 `configs/think600_temp1.yml` 那样在 `extra_params` 里写 `temperature`。已用假服务确认请求体里是 1。
- 做对照时，temperature 为 0 也不保证两次输出一致：同一个 prompt 多次发送，结果会不同（可能与投机解码、数值误差等有关，没有验证原因）。

## 4. 超时与重试：有三层，别混

| 层 | 在哪里 | 默认 | 本环境 |
|---|---|---|---|
| 请求超时 | yml `extra_params.timeout`（`models.py` 的 `request_timeout`） | 600 秒 | 600 秒（需要时在 yml 里改，如 300、1800） |
| 失败后重试几次 | `benchmark.py` 里的 `LONG_TIMEOUT`（`RETRY_TIMEOUT`） | `24*60*60` | 改成了 `0.25`，即只重试 1 次 |
| openai 客户端自带重试 | yml `max_retries` | 2 | 0 |

`LONG_TIMEOUT` **不是**请求超时，它是退避重试的放弃门槛：每次失败后延迟翻倍（从 0.125 秒起），延迟超过 `LONG_TIMEOUT` 就放弃。所以重试次数 ≈ `floor(log2(LONG_TIMEOUT / 0.125))`：

| `LONG_TIMEOUT` | 重试次数 |
|---|---|
| < 0.25（如 0.1） | 0 |
| 0.25 | 1 |
| 0.5 | 2 |
| 1 | 3 |
| 4 | 5 |
| 180 | 10 |
| 86400（原值） | 19 |

- 用假服务实测了 `0.25` 加 `max_retries: 0`：一次调用发 2 个请求（1 次原请求 + 1 次重试）。
- 一次 aider 调用最坏等 `(重试次数 + 1) × 请求超时`。一道题最多两轮，每轮至少一次调用：全部超时的习题是 `2 轮 × 2 个请求 × 600 秒 = 2400` 秒，实测恰好是 2401 秒。但模型回复之后 aider 还可能再发请求（如修 lint 提示），所以并不是上限，实测单题最长到过 5505 秒。
- 重试时日志里会打印 `Retrying in 0.2 seconds`。

## 5. 服务端参数（sglang，影响 benchmark 结果）

以下来自读容器里 sglang 0.5.18 的源码，加上启动命令行和日志的核对。

**`--json-model-override-args '{"dsv4_reasoning_effort_profile":"official"}'`**

- 启动时读取一次，不能按请求切换，换 profile 只能改参数后重启服务。
- 两种 profile（`encoding_dsv4.py`）：`official` 接受 low/high/max、没传 effort 时默认 low；`preview` 接受 high/max、默认 high。
- 不写这个参数时，sglang 会去模型目录里找 `encoding/encoding_dsv4.py` 自动判断，找不到就回退成 `preview`。06 上的模型目录里没有 `encoding/` 目录（按代码应回退到 `preview`，没有在运行时核对）。
- 两种 profile 的映射关系不同（例如 `high` 在 `official` 下用的是 `preview` 的 max 文本；`max` 在两边对应代码里两个不同名字的常量 `REASONING_EFFORT_OFFICIAL_MAX` 和 `REASONING_EFFORT_PREVIEW_MAX`，具体文本我没有读）。所以拿结果做对照前要确认两边 profile 一致。
- 实测 prompt A 的 `prompt_tokens`（`official`）：不传 effort 和 `low` 都是 2989，`high` 是 3068，`max` 是 3081。

**`reasoning_effort` 会隐式开思考**：请求里只要带了不是 `none` 的 `reasoning_effort`，`protocol.py` 会把 `chat_template_kwargs.thinking` 设为 true（请求里显式写了 `thinking` 则以显式为准）。effort 的提示文本只在思考模式下才拼进 prompt。实测：不带思考开关、只发 `reasoning_effort=max`，`prompt_tokens` 是 3081，即也进了思考模式。思考模式的默认开关是环境变量 `SGLANG_DEFAULT_THINKING`（默认 False，两台服务上都没设）。

**投机解码（MTP，sglang 里叫 DSPARK）**

| 参数 | 说明 |
|---|---|
| `--speculative-algorithm DSPARK` | 启用；不传任何 `--speculative-*` 就不做投机解码，逐 token 解码 |
| `--speculative-draft-model-path` | 本环境草稿模型就是主模型同一个 checkpoint |
| gamma | 来自 checkpoint 的 `config.json` 里 `dspark_block_size`，本环境是 5；也可用 `--speculative-dspark-block-size` 覆盖 |
| `--speculative-num-draft-tokens` | 必须等于 gamma + 1（本环境 6）。显式传了不等的值，启动直接报错；不传则自动取 6 |
| `--speculative-num-steps`、`--speculative-eagle-topk` | DSPARK 下只支持 1，传别的值会被强制改成 1 |

- 关掉投机解码：去掉所有 `--speculative-*` 参数。05 上不带 MTP 的服务日志核对过：`speculative_algorithm=None`，`Decode batch` 行里没有 `accept len`，启动耗时统计里 `draft_*` 全是 0。
- 日志里 `accept len` 上限是 6.00（gamma 5 加 1）。

**其他**

- `--context-length`：模型 `max_position_embeddings` 是 1048576，设 307200 或 262144 都合法；超过模型推导出的上限要设环境变量 `SGLANG_ALLOW_OVERWRITE_LONGER_CONTEXT_LEN=1`。06 的服务在 10-23 当天从 307200 变成过 262144，做对照前先确认。
- `--reasoning-parser deepseek-v4`：思考内容走 `reasoning_content`，答案走 `content`。

## 6. litellm 价格表与 `aider/main.py` 的本地改动

- litellm 默认在 import 时去 `raw.githubusercontent.com` 拉价格表，**超时 5 秒**后回退到自带备份，不会卡死，但每次 import 多约 5 秒（实测 `import litellm` 7.1 秒，设 `LITELLM_LOCAL_MODEL_COST_MAP=True` 后 1.7 秒）。
- 本环境里 `aider/main.py` 被改过两处（用户改的）：设置 `LITELLM_LOCAL_MODEL_COST_MAP=True`，并在 import 时 `open("model_prices.json")`（**相对路径**，要求当前目录里有这个文件）再 `litellm.register_model`。
- **这处改动只影响直接运行 `aider` 命令（`aider.main`）。`benchmark.py` 的 import 链不会加载 `aider.main`**：在没有 `model_prices.json` 的目录里 `benchmark.py --help` 正常（实测）。所以跑 benchmark 不需要这个文件；想去掉 5 秒等待可以直接给容器设环境变量 `LITELLM_LOCAL_MODEL_COST_MAP=True`（只验证了 import 耗时，没有在完整 benchmark 里跑过）。
- 如果要保留 `main.py` 的改动，`model_prices.json` 来自用户本地执行 `curl -L -o model_prices.json https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json` 后上传。节点上直连也能下，但很慢（06 实测 112 秒，2,967,850 字节，与用户那份逐字节相同）；`mirror.ghproxy.com` 镜像地址不通（10 秒超时）。需要这个文件时，**先问用户：跳过，还是用户上传**。跳过的话写一个 `{}` 就能 import（实测）；不要自己编数据。
- 这份文件和 litellm 包里自带的备份内容不同（4245 条对 2582 条）。

## 7. 本环境对 aider 仓库的本地改动

仓库 `5dc9490bb`（`Merge pull request #5173`）之上，`git diff` 里有改动的是下面两个文件，只列出新增/修改的行（另有未跟踪的 `model_prices.json`、`benchmark/docker_10*.sh` 等）：

```python
# aider/main.py：在 from pathlib import Path 之后新增
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"  # 必须在 import litellm 之前
# aider/main.py：在 from .dump import dump 之后新增
with open("model_prices.json") as f:
    litellm.register_model(json.load(f))  # 用 dict 覆盖/补充内置的价格表

# benchmark/benchmark.py：main() 里
-    LONG_TIMEOUT = 24 * 60 * 60
+    LONG_TIMEOUT = 0.25
# benchmark/benchmark.py：另有一处只加了一个空行（from aider.io import InputOutput 之后），无功能影响
```

改 `LONG_TIMEOUT` 之前的原文件在节点上备份为 `benchmark/benchmark.py.bak_longtimeout`。

## 8. 实测基线（只当参考：一次运行、样本小、模型输出不确定）

| 运行 | 配置 | 结果 |
|---|---|---|
| `think-full` | 225 题、8 线程、`--tries 2`、diff、思考开、请求超时 600 秒、重试 1 次、经 06:30000 router 的 PD 服务（带 MTP）、没传 effort | 约 12.5 小时；pass_rate_1 18.2%（41）、pass_rate_2 36.9%（83）；`litellm.Timeout` 538 次；48 题模型一个 token 都没返回；每题平均约 1543 秒 |
| `max-effort-16` | 16 题、同上并加 `--reasoning-effort max`、经 06:30000 router 的 PD 服务（带 MTP；与 think-full 当时的服务不是同一次部署） | 约 50 分钟；pass_rate_1 50.0%（8/16）、pass_rate_2 62.5%（10/16）；22 次超时；每题平均约 1088 秒 |

- 05 上不带 MTP 的服务，单请求的生成速度约 13.5 token/s，所以 600 秒只够生成约 8000 token；06 带 MTP 的服务单请求速度是它的 2.5 倍以上（粗略对比，并发不同）。
- MTP 的 `accept len`：2026-10-19 到 10-22 中午的普通流量平均 3.73（accept rate 0.55，0.47% 的采样 accept rate ≥ 0.97）；10-22 晚上到 10-23 跑测试期间平均 5.40（0.88，48.7% 的采样 ≥ 0.97），接近 6.00 的满值一般对应重复文本。这是从数字推断的，没有逐个请求核对输出。
