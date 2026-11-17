# 测试运行、停止与验收

## 定义一次实验

为每个 arm 保存一份脱敏 manifest，至少包含：

```yaml
dataset: {name: SWE-bench Pro V2, commit: null, ids_file: null, ids_sha256: null, count: null}
protocol: diagnostic # 或 locked，按实际验证结果填写
execution: {node: null, workdir: null, docker_context: null, image_manifest: null}
agent: {name: null, version: null, config_sha256: null, cli_binary_sha256: null}
gateway: {url: null, model_alias: null, version: null, config_sha256: null, hook_sha256: null}
backend: {endpoint: null, model_revision: null, quantization: null, engine_version: null, topology: null}
generation: {effort_requested: null, effort_observed: null, temperature: null, max_tokens: null}
budget: {concurrency: null, task_timeout_seconds: null, step_limit: null, cost_limit: null}
network: {egress_node: null, proxy_group: null, selected_proxy: null, allowed_agent_hosts: []}
cache: {state: unknown, routing_policy: null, dp_affinity: null, router_parameters: null}
run: {job_name: null, repeat: null, start_utc: null, end_utc: null, exit_code: null}
```

`null/unknown` 表示未知，不以推测补齐。URL 不携带密钥；采集环境只列必要字段，不导出完整 env。各轮命令、manifest、stderr/stdout、job config 与原始结果保存到独立目录。配置哈希需覆盖 agent 模板、网关钩子、实际 tokenizer/chat template 和本地补丁等会影响对照的输入。

## 冒烟与运行模板

下列命令在**执行节点**运行，`REPO/SKILL_DIR/IDS/RUN_DIR` 为绝对路径，`IDS` 是经核对的完整 task 目录名清单。先预览；真正运行时加 `--execute`。脚本设置 `cwd=REPO` 并给子进程加入 `PYTHONPATH=REPO/v2/tooling`。

先用同一小样本基线闭环：

```bash
python3 "$SKILL_DIR/scripts/run_subset.py" --repo "$REPO" --ids "$SMOKE_IDS" \
  --job smoke-oracle --output "$RUN_DIR/jobs" --agent oracle --concurrency 1 --execute
python3 "$SKILL_DIR/scripts/run_subset.py" --repo "$REPO" --ids "$SMOKE_IDS" \
  --job smoke-nop --output "$RUN_DIR/jobs" --agent nop --concurrency 1 --execute
```

oracle/nop 不调用模型；验收看 verifier artifacts 与结果，不能因 exit code 0 就认为 baseline 正确。大型题集可先选有代表性的基线样本，遇到 verifier 异常再扩充验证。

### A. Claude Code + Anthropic 网关

```bash
# ANTHROPIC_API_KEY 已由本次安全环境加载；确认当前集成会注入沙箱
python3 "$SKILL_DIR/scripts/run_subset.py" --repo "$REPO" --ids "$IDS" \
  --job cc-armA-r1 --output "$RUN_DIR/jobs" --agent claude-code \
  --model "$MODEL_ALIAS" --base-url "$GW_ROOT" --allow-agent-host "$GW_HOST" \
  --ak reasoning_effort=max --concurrency 4
```

内部受控沙箱确需免交互时，可按用户授权和该 agent 支持情况追加 `--ak permission_mode=bypassPermissions`。此设置只针对任务沙箱，不是给宿主机扩大权限。

离线安装 CLI 时，先准备匹配架构、libc 和依赖的二进制/发行包，保存版本及 SHA256。挂载目标需按当前 agent 的 HOME、PATH 和安装跳过逻辑确定；以下示例适用于目标路径为 `/root/.local/bin/claude` 的沙箱：

```bash
--mounts-json '[{"type":"bind","source":"/absolute/path/bin/claude","target":"/root/.local/bin/claude","read_only":true}]'
```

检查每类任务镜像能否 `claude --version`；不同镜像的 HOME/PATH/libc 差异可能导致安装失败和零模型调用。仅挂载一个文件不保证 npm shim 或动态链接二进制能工作。

### B. host mini-swe-agent + OpenAI-compatible endpoint

从该 checkout 的 `v2/tooling/configs/mini_toolcall.yaml` 复制成运行专用配置，保留完整 system/instance/tool 模板；修改其 `model.model_kwargs` 的 `api_base`、effort 和 generation 参数，密钥使用实际集成支持的主机环境。不要用只有 model 段的 YAML 覆盖完整 agent 模板。

```bash
python3 "$SKILL_DIR/scripts/run_subset.py" --repo "$REPO" --ids "$IDS" \
  --job mini-armA-r1 --output "$RUN_DIR/jobs" \
  --agent host_mini_swe:HostMiniSweAgent --model "$OPENAI_MODEL" \
  --ak "config_file=$MINI_CONFIG" --ak step_limit=250 --concurrency 4
```

`OPENAI_MODEL` 在历史版本形如 `openai/<后端 served name>`。step_limit=250 是诊断示例，会改变评测预算；正式对比按已选协议对齐。费用上限依赖价格映射，未知价格不能靠 cost_limit=0 的表面低费用保证有界。至少测试真实超时和退出；旧 host mini 的 `asyncio.to_thread(agent.run)` 不会自动随外层协程取消而停止模型调用。

### C. locked agents 与复评分

把 `--agent` 替换为 checkout 的准确模块类；常见配置键：

| Agent | 额外设置 |
|---|---|
| `locked_claude_code:LockedClaudeCode` | `--ak reasoning_effort=... --ak disallowed_tools=WebFetch,WebSearch` |
| `locked_mini_swe:LockedMiniSwe` | `--ak config_file=<该 checkout 的完整配置>` |
| `locked_codex:LockedCodex` | `--ak config=<该 checkout 的 Codex TOML>` |

核对当前 agent 的环境注入规则。官方示例中的 `--ae OPENAI_API_KEY=... --ae OPENAI_API_BASE=...` 不应把真实 key 写进命令预览或共享日志；需要时用现有 secret 机制/运行时生成的权限受控 job config。当前辅助启动脚本不提供 `--ae`，遇到必须使用它的集成，使用 Harbor 原生配置启动，保留同样的精确 `-i` 清单和元数据。

选定 sandbox 必须实际执行阶段网络限制。旧 Docker/Harbor 与现代版本能力不同，不能仅看到 task.toml 的 no-network 就声称已隔离。运行该 checkout 的网络/历史 probe，确认求解阶段只能访问指定模型入口、测试/金标不可见。Modal 的 allowlist 不能访问执行主机 loopback；本地 Docker 网关需要相应可达地址与防火墙规则。

locked 的主分数来自干净镜像复评分：

```bash
python3 "$SKILL_DIR/scripts/run_subset.py" --repo "$REPO" --ids "$IDS" \
  --job armA-r1-regrade --output "$RUN_DIR/jobs" \
  --agent patch_replay:PatchReplayAgent --model replay \
  --ak "source_job=$SOURCE_JOB_ABSOLUTE" --concurrency 4
```

确认每道题的 model.patch 能映射到 source job；source_job 里的缺失、空 patch 和应用失败分别记录。原始评分和 regrade 都发布，复评分为主。内部诊断数据保留 protocol 偏差，不标为官方榜单等价分数。

## 矩阵与监控

先跑 1–2 题/并发 1，再扩大到指定集合和并发。轮次使用 `r1/r2/r3` 独立 job，前一轮出错先检查，不无条件自动串跑。比较 agent 时同一模型和服务；比较引擎/网关时尽量固定 agent 和真实请求。硬件、引擎、模型、路由一起变化时，只报告整体对比和混杂因素。

每次状态检查至少看：新 trial 是否启动、最后轨迹时间是否更新、上游请求是否真实出现、错误重试是否堆积、磁盘/内存/容器数量、计划内有效时间是否超限。首批全是 401、模型不存在、缺 agent CLI 或零调用时先停扩大，不等整批跑完。

失败分类：image pull/load、agent install、DNS/TLS/connect、auth/model routing、schema/tool/SSE、AgentTimeout、sandbox crash、verifier setup/test、模型未解决。429/503 重试次数、超时和重跑按 task×arm 单列，不把重试当新独立样本。Harbor 主进程成功退出与每个 trial 成功是两个条件。

## 停止与缓存

先定位本 job 的 driver/Harbor/agent/网关 PID 和关联容器，优先正常停止并保存 patch。检查宿主机 agent 线程与上游请求是否仍存活。终止 driver 并不保证 decode 已结束；禁止用宽泛进程名杀一整机作业。

专属 SGLang 实例如存在 orphan 请求，按该版本的真实管理 API 定向 abort 后再检查 idle。历史 `/abort_request` 的 `abort_all=true` 会取消实例所有请求，只能在本任务独占且有授权时用，不照抄到共享服务。清缓存是实验处理，应记录冷/热条件；`/flush_cache` 不能假设清掉 Mooncake L3。停止作业不默认清共享缓存或重启服务。

## 分数与 token 验收

- 核对计划题数 N、实际启动数、真正有模型调用数、可评分数、resolved 数、错误数以及原始/复评分映射。oracle/nop 本来不调模型，不套用零调用异常判定。
- 报告计划题集上的 resolved/N，并单列基础设施失败；若补充过滤后子集结果，明确分母与过滤规则，不能只报告“有效样本”提高成功率。
- 缺调用、缺 usage、usage=0、null 是不同情况。Harbor result.json 的 token 字段可能为空，改查原始轨迹；不要把缺失填 0。没有模型请求的题不纳入模型 token 均值，但仍进入运行完整性对账。
- Claude Code：读取主会话和 subagents 原始 JSONL；同文件按 `message.id` 去重，用同一响应最后一条非空 usage；排除 synthetic/API error。跨文件重复副本需按可验证的请求 ID 处理，不能随意按文本去重。
- mini：原始 `assistant.extra.response.usage` 是历史可靠位置；检查 trajectory 版本。重试、compaction、subagents 的纳入范围明确说明。
- OpenAI：prompt_tokens 通常含缓存，cached_tokens 是子集；Anthropic：总输入通常为 input_tokens + cache_read_input_tokens + cache_creation_input_tokens。先核对该网关转换，再统计，不能把两者 input 名字相同就直接相加。reasoning_tokens 缺失不等于 0。
- 汇总缓存命中率用 Σcache_read/Σtotal_input，不对各题百分比做无权平均。累计输入包含每轮重发的历史，不代表唯一文本、实际 prefill 计算量或费用。
- 以 agent_execution 时间窗和上游请求时间裁剪有效期；超时后遗留调用单独列为基础设施浪费。reward=1 只说明 verifier 通过，不证明 agent 正常结束。

最终交付至少有：每个 job 的配置/命令、题集/镜像/agent/网关指纹、逐题状态和评分、调用/token/耗时口径、错误及补跑清单、比较限制。资源中断导致不能继续时，交接已有产物与可恢复的精确下一步。

参考：[Harbor job 参数](https://docs.harborframework.com/jobs/run-a-job)、[阶段网络策略及 sandbox 能力](https://docs.harborframework.com/tasks/network-policies)。
