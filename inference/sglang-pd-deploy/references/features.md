# 功能开关与参数归属

默认 `hicache=true`、`mtp=true`。功能关闭时以 `scripts/render.py` 的 feature policy 为准；最终值在 `params.json.resolved` 和 `params.md` 中确认。仅生成脚本的请求不授权启动服务。

## 关闭 HICache

当前模板只在 Prefill 启用 HICache，所以其默认配置移除以下八项；若实例覆盖给 Decode 添加了同组参数，关闭功能时同样移除：

```text
--enable-hierarchical-cache
--hicache-ratio 3
--enable-two-batch-overlap
--hicache-mem-layout page_first
--hicache-io-backend kernel
--hicache-write-policy write_back
--hicache-storage-backend mooncake
--hicache-storage-prefetch-policy timeout
```

`--enable-two-batch-overlap` 是计算/通信重叠优化，并非 HICache 专用依赖；按本部署模板约定与上述配置一并移除，不把这一约定推广为 SGLang 的通用要求。关闭 HICache 不等于关闭模型自身的 KV cache。

### 从日志确认是否关闭

`KV Cache is allocated`、`Memory pool end` 和 `Initialize ...TokenToKVPool` 是模型推理所需的 GPU KV 内存池记录，关闭 HICache 后仍会出现。不能根据这些日志或 GPU 显存占用判断 HICache 已开启。

核对本次启动的实际 `server_args`：关闭时应为 `enable_hierarchical_cache=False`；当前镜像的 `Tree cache initialized` 记录同时应为 `hicache_attached=False`。`hicache_ratio`、`hicache_mem_layout` 等字段仍可能打印镜像内置默认值，默认值的存在不表示该功能已启用。HICache 的主机内存/外部 Store 缓存层与 GPU KV 内存池分开判断；PD Transfer Engine 的保留也不表示启用了 HICache。

## Mooncake：传输引擎与 Store 分开

| 项目 | HICache 开启（模板使用 Mooncake Store） | HICache 关闭 |
| --- | --- | --- |
| worker 内的 Mooncake Transfer Engine | 保留，用于 PD KV 传输 | 保留，用于 PD KV 传输 |
| `--disaggregation-transfer-backend mooncake` | 保留 | 保留 |
| RDMA 设备、`MC_ALLOWED_IBV_DEVICES`、`MOONCAKE_PROTOCOL=rdma` | 保留 | 保留 |
| Store master / HTTP metadata | 生成脚本并启动 | 不生成、不上传、不启动 |
| master 工具、固定端口、Store segment 检查 | 执行 | 跳过 |
| `MOONCAKE_LOCAL_HOSTNAME` | Prefill 派生 | 不注入 |
| `MOONCAKE_TE_META_DATA_SERVER` | Prefill 派生 | 不注入 |
| `MOONCAKE_MASTER` | Prefill 派生 | 不注入 |
| `MOONCAKE_GLOBAL_SEGMENT_SIZE` | Prefill 派生，模板 25GB/rank | 不注入，不分配 Store segment |

当前核对的 SGLang 0.5.18 / DTK 26041 镜像，`sglang/srt/distributed/device_communicators/mooncake_transfer_engine.py` 的 `MooncakeTransferEngine.initialize()` 使用 `P2PHANDSHAKE`，不连接外部 Store master/metadata。`MOONCAKE_CHECK_SERVER` 和 `SGLANG_KV_LAYOUT_HCU_FA` 保留模板值，不属于这四项 Store 地址/容量配置。

`plan.json` 保留 `mooncake` 对象以明确状态：`enabled=false`、`hicache_enabled=false`，`launch_cmd`、`stop_cmd`、`listen_check_cmd`、`mem_cmd`、`log`、`expected_total` 为 `null`，`ports=[]`、`ranks=0`；`stop_order` 不包含 `mooncake`。输入 `[mooncake]` 的端口、segment 和 extra_args 可保留，关闭时不用于启动或检查。旧输出目录中的 `scripts/mooncake_master.sh` 在重新渲染时移除，远端已有组件必须另按已确认的旧计划停服，渲染器不操作远端。

因此这里“关闭 Mooncake”指关闭 Store 服务；若用户要求连传输引擎也关闭，须另行选择并确认受当前模型/镜像支持的 PD 传输后端，不能只删传输库。

参考：[Mooncake 官方 PD 部署](https://kvcache-ai.github.io/Mooncake/deployment/integrations/sglang/pd-disaggregation.html)、[SGLang 传输引擎源码](https://github.com/sgl-project/sglang/blob/main/python/sglang/srt/distributed/device_communicators/mooncake_transfer_engine.py)。版本切换后核对实际镜像实现。

## 当前模板的 MTP / DSPARK 参数

以下是推测解码/草稿模型专用 server 参数；`mtp=false` 时全部移除：

| 参数 | Prefill 默认值 | Decode 默认值 | 用途 |
| --- | --- | --- | --- |
| `--speculative-algorithm` | `DSPARK` | `DSPARK` | 选择推测解码算法 |
| `--speculative-num-steps` | `1` | `1` | 推测步骤配置 |
| `--speculative-eagle-topk` | `1` | `1` | 草稿分支 top-k 配置，是否实际使用由算法实现决定 |
| `--speculative-num-draft-tokens` | `6` | `6` | 草稿 token 预算 |
| `--speculative-draft-model-path` | 派生为当前模型路径 | 派生为当前模型路径 | 草稿模型路径，关闭 MTP 时不派生 |
| `--speculative-moe-a2a-backend` | `none` | `deepep` | 草稿 MoE 通信后端 |
| `--speculative-moe-runner-backend` | 未显式传入 | `deep_gemm` | 草稿 MoE 计算后端 |

相关环境变量；`mtp=false` 时移除这些显式设置：

| 环境变量 | 当前两角色默认值 | 用途 |
| --- | --- | --- |
| `SGLANG_DSPARK_CONFIDENCE_RELAY_LAG_STEPS` | `2` | DSPARK 置信度传递的延迟步数 |
| `SGLANG_DSPARK_OPT_MARKOV_W2_TP_SHARD` | `1` | DSPARK Markov W2 的 TP 分片优化 |
| `SGLANG_DSPARK_ENABLE_MULTI_STREAM` | `1` | DSPARK 多 stream 优化开关，还受其它实现条件约束 |
| `SGLANG_DSPARK_FAST_KERNEL` | `1` | DSPARK 快速 kernel 路径 |
| `SGLANG_DSPARK_FAST_SAMPLING` | `1` | DSPARK 草稿快速采样 |
| `SGLANG_RAGGED_VERIFY_MODE` | `static` | 推测解码 verify 布局；也可能被其它推测解码算法使用 |

移除显式 env 设置并不意味着把镜像内置默认值强制设为 false；关闭入口是移除 `--speculative-algorithm` 等 server 参数。

下列配置虽与 MTP 性能/资源有关，主模型也使用，关闭 MTP 时保留：

- `SGLANG_DEEPEP_NUM_MAX_DISPATCH_TOKENS_PER_RANK`：通用 DeepEP dispatcher 容量，当前 Prefill `256`、Decode `512`；可能随草稿 token 数调优，但并非 MTP 专属。
- `--moe-a2a-backend`、`--moe-runner-backend`、`--deepep-mode`、DeepEP 配置文件；以及 `SGLANG_USE_FP8_W8A8_MOE`、`SGLANG_USE_DEEPGEMM_MOE`、ROCSHMEM/网络等通用运行配置。
- TP/DP/EP、CUDA Graph、显存比例、上下文长度等通用 server 参数；不能仅因与 MTP 一起出现在示例中就移除。

这些归属已通过当前使用镜像内的 `srt/models/deepseek_v4_dspark.py`、`srt/speculative/dspark_components/`、`srt/managers/overlap_utils.py`、`srt/layers/moe/token_dispatcher/deepep.py` 和 `srt/server_args.py` 核对，不能据名称推断其它版本的实现。

## 默认模型 profile

Prefill、Decode 模板默认均加入：

```bash
--json-model-override-args '{"dsv4_reasoning_effort_profile":"official"}'
```

这是 DeepSeek-V4 chat reasoning effort profile 的模型配置；不是 HICache 或 MTP 参数。两项功能关闭时仍保留。镜像内对应实现为 `srt/entrypoints/openai/chat_encoding.py` 的 `resolve_dsv4_reasoning_effort_profile()`，由 `serving_chat.py` 读取配置。

已有参数文件需显式迁移：将键 `dsv4_reasoning_effort_profile` 合并进两个角色已有的 `json-model-override-args` JSON 对象，再重新渲染。保留用户的其它 override 键，并把最终 JSON 交给用户核对。
