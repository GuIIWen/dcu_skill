# Agent 与网关组合

## 选择组合

先检查所用 checkout 的 `v2/tooling`、Harbor CLI 帮助和 agent 源码，不因当前文档有某个功能就假设旧版支持。

| 用途 | Agent | 模型入口与配置 | 关键验证 |
|---|---|---|---|
| harness 基线 | `oracle` / `nop` | 无模型 | 参考补丁通过、空补丁失败、verifier 真执行 |
| 内部诊断，省沙箱安装 | `host_mini_swe:HostMiniSweAgent` | 主机上的 OpenAI-compatible API；`--ak config_file=<绝对路径>` | API key 留主机；主机 agent 线程退出/超时取消要另测 |
| 内部诊断，Claude Code | `claude-code` | Anthropic `/v1/messages`；原生服务或 LiteLLM bridge | CLI 在每类镜像可执行，thinking/tool/SSE round trip |
| 官方协议 mini | `locked_mini_swe:LockedMiniSwe` | 按 checkout 的 config 与安装逻辑，通常可指向 OpenAI-compatible endpoint | 运行时隔离、预算、patch 捕获和 regrade |
| 官方协议 Claude Code | `locked_claude_code:LockedClaudeCode` | Anthropic-compatible endpoint | 只放行模型网关，禁用 WebFetch/WebSearch，regrade |
| 官方协议 Codex | `locked_codex:LockedCodex` | 所选 Codex 配置的 provider 和 API | 二进制/配置与 endpoint 的 chat/responses 能力相符 |
| 其它 agent | 当前 Harbor 支持的名称或自定义 module:Class | 原生 agent 配置和 harness 集成 | 先读集成源码，按同样探测验收，不声称已验证 |

内置 `mini-swe-agent` 与仓库自带 host/locked 实现不同；`codex` 与 `locked_codex` 也不同。agent 与模型不是同一维度：Claude Code 可以通过兼容网关调用非 Claude 模型，但协议适配要独立验证。

## 地址和鉴权

区分四个字段：agent 面向的模型别名、网关 `model_name`、后端 `litellm_params.model`、后端 `api_base`。它们不必相同。用 `/v1/models` **解析 JSON 精确匹配**模型 ID，不用子串 grep 决定正确性。

- Claude Code / Anthropic 请求：网关 root 例如 `http://<gw-host>:<port>`，实际接口 `/v1/messages`。不要重复拼成 `/v1/v1/messages`。`ANTHROPIC_API_KEY` / token 的注入以当前 Harbor 集成为准，避免 CLI 自动回落到宿主机账号或其它 provider。
- OpenAI-compatible 请求：常见 base 是 `http://<host>:<port>/v1`；`OPENAI_API_BASE`、`OPENAI_BASE_URL` 或 agent config 的 `api_base/base_url` 由实际实现决定。host mini 在主机建模型，不能假设 `--ae` 会配置主机进程；检查其 LiteLLM model kwargs/主机环境。
- Codex：核对所选 `wire_api` 是 `responses` 还是 `chat`（若该版本支持），网关是否实现该接口。仅支持 `/chat/completions` 的服务不能直接假定兼容 Responses；也不能为了绕过错误静默换成另一个模型。
- `--base-url` 和 `--allow-agent-host` 的支持、优先级和自动放行随 Harbor 版本变化。allowlist 填实际 host/IP，不是完整 URL。内部网关还需 `NO_PROXY`、监听地址和容器到宿主机路由正确。

密钥从现有环境或权限受控文件加载；示例中的变量只表示来源，不写入 skill、命令预览或归档。要求沙箱注入时检查该集成的 secret/env 机制及日志脱敏。

## LiteLLM bridge

网关使用独立 venv；先确认选定版本支持目标 API 转换，再按其 schema 配置后端和鉴权。当前版本原生行为通过时无需历史钩子。

填好本次网关 YAML 中的后端地址和模型名；使用自定义钩子时将其目录加入 PYTHONPATH。使用已配置的 master key，以独立端口启动并保留实际 PID/日志：

```bash
cd "$GW_DIR" || exit 1
export PYTHONPATH="$GW_DIR${PYTHONPATH:+:$PYTHONPATH}"
# LITELLM_MASTER_KEY 已由当前安全环境加载
"$GW_VENV/bin/litellm" --config "$GW_CONFIG" \
  --host "$GW_BIND" --port "$GW_PORT"
```

上例前台运行便于看首次启动错误；持续运行改用已有进程管理器或无交互的后台方式，并记录 PID。网关独立重启不等于允许重启上游推理服务。不要把设置弱 key/无 key 当默认解决方案。

旧版 LiteLLM / DSV4 的固定 max 修复、disabled 语义限制和模型别名匹配见 [历史钩子](litellm-legacy-hook.md)。仅在本次问题复现时读取并使用，不默认加载。

## 最小验收矩阵

按 agent 实际请求形状测试，包括 stream=true：

| 探针 | 必须观察的行为 |
|---|---|
| 普通消息 | 正确模型，文本与 usage 可读，没有鉴权/参数报错 |
| Thinking | enabled、disabled（模型支持时）、指定 effort 的上游值及响应形状相符 |
| 工具调用 | 产生一个合法 tool call/tool_use，参数是对象，ID/名称可对应 |
| 工具回传 | 发送同一 ID 的 tool result 后模型能继续；stop reason 与 agent 契约相符 |
| 流式 | 首事件/增量/结束事件齐全，工具 JSON 分片可拼接，取消/超时后上游请求能终止 |
| Usage/cache | 冷/热请求各一次；字段口径正确，缺失与 0 区分，缓存可报告时记录 |

首选在隔离测试网关抓实际上游请求，脱敏保存 `model/effort/thinking/tools` 等必要字段；工具 schema 需对齐 input_schema、strict/defer_loading、工具名和 tokenizer/encoder，不能假设字段重排一定改变 token。完整真实 SWE 轨迹可能包含源码和密钥，不盲目全量写 DEBUG 日志。

输出长度、模型自述、网关返回成功或 job 名含 max 都不足以证明 effort 生效；以实际上游参数和响应契约为准。

## 缓存路由

DP attention 存在分 rank radix tree 时，可以评估按 session 固定 rank；历史方案为 `crc32(session_id) % DP_SIZE` 写 `extra_body.routed_dp_rank`。是否需要、后端是否支持、session header 是否到达，都须实测。单实例 prefix reuse 可以跨 session，不能把会话亲和等同于缓存 key。

PD 多 prefill 路由需同时记录 cache_aware/round_robin、阈值、拓扑和负载。历史低并发、长公共前缀曾全部挤到一个 P，调整阈值后的数据不可冒充仅改 effort 的消融。先探测，部署修改在本 skill 范围之外。

参考：[Harbor agent 列表](https://docs.harborframework.com/agents/pre-integrated-agents)、[Base URL 路由](https://docs.harborframework.com/agents/base-url-management)。
