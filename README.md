# dcu_skill

Hygon DCU 节点的四个 agent skill（Claude Code / Codex 通用）的集合目录；`dcu_skill` 本身不是一个 skill。PD 部署支持用户提供的 SSH、跳板机、Ansible、Kubernetes、remote-linux MCP 等连接方式；节点初始化与 aider benchmark 当前通过 [remote-linux MCP](https://github.com/GuIIWen/remote-mcp) 操作远端 Linux 节点。

`sglang-pd-deploy` 以内部 `das/SkillHub` 的 `skills/sglang-pd-deploy/` 为主版本，本目录同步其运行文件。PD 的通用修改先在 SkillHub 维护，再同步到这里；`skill-card.md` 等目录注册元数据由 SkillHub 管理。另两个 skill 继续在本仓库维护。

| 目录 | skill 名 | 作用 |
|---|---|---|
| [`deploy/node-setup/`](deploy/node-setup) | `node-setup` | 全新 GPU/DCU 节点初始化：勘察、挂盘、装 docker、装海光 DCU 驱动 |
| [deploy/claude-mem-observer-ops/](deploy/claude-mem-observer-ops) | claude-mem-observer-ops | 检查 claude-mem observer、延迟批处理和经授权的历史队列恢复 |
| [`inference/sglang-pd-deploy/`](inference/sglang-pd-deploy) | `sglang-pd-deploy` | 在节点上部署 sglang PD 分离推理服务（xPyD + mooncake + sglang_router） |
| [`eval/aider-benchmark/`](eval/aider-benchmark) | `aider-benchmark` | 在容器里安装并运行 aider polyglot benchmark，对接 OpenAI 兼容的 sglang 服务；含参数配置、结果汇总、重复循环的单请求复现 |

## 前置依赖

- PD 部署：提供能执行目标命令、上传文件、读取日志和退出码的连接适配器，不要求安装特定 MCP。连接凭据由适配器管理。
- node-setup / aider-benchmark：本机已部署并启动 remote-linux MCP daemon（`remote-linux-mcp` skill 在 remote-mcp 仓库中），会话里能用 `exec`、`write_file`、`read_file`、`list_hosts`。
- 目标节点：Ubuntu + 海光 DCU + docker CE。
- `inference/sglang-pd-deploy/scripts/render.py` 需要本机 Python >= 3.11，可使用已有虚拟环境。

## 安装

在仓库根目录执行以下命令，复制实际 skill 目录的内容；目标目录名要和 `SKILL.md` 中的 `name` 一致。示例同时安装到 CC 的 `~/.claude/skills` 和 Codex 的 `~/.agents/skills`：

```bash
for skill_path in deploy/node-setup deploy/claude-mem-observer-ops inference/sglang-pd-deploy eval/aider-benchmark; do
  skill_name="${skill_path##*/}"
  for skill_root in "$HOME/.claude/skills" "$HOME/.agents/skills"; do
    mkdir -p "$skill_root/$skill_name"
    cp -R "$skill_path/." "$skill_root/$skill_name/"
  done
done
```

升级前备份已有同名包。旧版 PD 包根目录的 `render.py`、`deploy.example.toml`、`deepep-config.json` 已迁到 `scripts/` 和 `assets/`；新文件复制完整后再移除这三个旧文件。若以前装在 `~/.codex/skills`，选定一个 Codex 安装位置并迁移或更新，避免同名包保留不同版本。

之后直接用自然语言触发，例如“初始化这台新节点”“按 deploy.toml 部署 sglang 1P1D”。

## deploy/claude-mem-observer-ops

用于检查本地 worker、SQLite outbox 和批处理审计记录。历史 parked 消息只有在用户明确要求时才恢复；脚本会先校验数量、载荷和数据库备份，再将消息置为 released，由 patched worker 按正常归属和事务流程处理。

| 文件 | 说明 |
|---|---|
| SKILL.md | 运维边界、状态模型和恢复流程 |
| references/recovery.md | outbox 状态、Windows worker 命令和恢复不变量 |
| scripts/replay_parked_outbox.py | 带数量保护和 SQLite 备份的受控恢复脚本 |

## deploy/node-setup（node-setup）：节点初始化

流程：`survey.sh` 勘察 → 挂盘 → 装 docker → 装 DCU 驱动 → 收尾（重启前需确认）。

| 文件 | 说明 |
|---|---|
| `SKILL.md` | 流程与规则 |
| `survey.sh` | 只读勘察：空白盘、sudo、docker、驱动、网络 |
| `setup_mount.sh` | 格式化并挂载空白盘到 `/data0` `/data1`（UUID + `nofail`，备份 fstab） |
| `install_docker.sh` | 安装 docker，合并写 `daemon.json`，改 data-root，关闭 containerd-snapshotter |
| `install_driver.sh` | 安装海光 DCU 驱动（`.run` 文件，后台运行，日志轮询） |

要点：

- 先勘察后动手；脚本幂等，重复运行安全。
- 分区、格式化、改 fstab、装驱动（含 VBIOS 烧录）、重启都需要用户明确确认。
- 盘名、挂载点、data-root、驱动版本都只是默认参考，以 survey 结果和用户指示为准。内网仓库通过 `REGISTRY` 显式提供；省略或为空时不新增仓库，已有配置保留。
- 局限：仅覆盖 Ubuntu + 海光 DCU + docker CE；部分分支（真实空白盘、ABORT 路径）未在真机验证，详见 `deploy/node-setup/SKILL.md`。

## inference/sglang-pd-deploy（sglang-pd-deploy）：sglang PD 分离部署

流程：填写 `deploy.toml` → `scripts/render.py` 渲染 → 卡检查与预检 → 用户确认方案 → 镜像及工具检查 → 用户确认最终 env/args → 建容器、上传 → 启动已启用的组件 → 验收。使用子 agent 时由它执行当前授权阶段，主流程审核关键结果；也可单流程执行，保持相同顺序和两道确认。

| 文件 | 说明 |
|---|---|
| `inference/sglang-pd-deploy/SKILL.md` | 流程与规则 |
| `inference/sglang-pd-deploy/assets/deploy.example.toml` | 脱敏的部署清单模板（默认 1P1D、每实例 8 卡、两台节点） |
| `inference/sglang-pd-deploy/assets/deepep-config.json` | 默认 deepep 配置 |
| `inference/sglang-pd-deploy/scripts/render.py` | 展开拓扑，校验，生成脚本、`plan.md`、`params.md`、`params.json`、`plan.json` |
| `inference/sglang-pd-deploy/references/features.md` | HiCache/MTP 开关、Mooncake Store 与传输引擎、默认模型 profile、日志判断 |
| `inference/sglang-pd-deploy/agents/openai.yaml` | skill 展示信息与调用提示 |

快速使用：

```bash
cp inference/sglang-pd-deploy/assets/deploy.example.toml deploy.toml   # 按场景填写节点、镜像、目录
python inference/sglang-pd-deploy/scripts/render.py deploy.toml      # 生成 plan.md / params.json 等
```

要点：

- 所有启动命令都取自 `plan.json`，不手拼；改参数就改 `deploy.toml` 后重新渲染。
- `[features]` 默认 `hicache=true`、`mtp=true`；用户要求关闭时写入配置，再重新渲染。必须在对话中以 JSON 展示完整最终 env/args、功能移除清单及 Mooncake/router 状态和命令，再取得确认；文件路径和表格仅作辅助，不能代替 JSON 展示。
- HiCache 关闭时不启动 Mooncake Store master/metadata，不分配 Store segment；保留 worker 内的 Mooncake Transfer Engine 用于 PD KV 传输。
- `KV Cache is allocated` 是普通 GPU KV 内存池日志，关闭 HiCache 后仍会出现；核对实际 `enable_hierarchical_cache=False` 和当前镜像的 `hicache_attached=False` 判断开关。
- Prefill/Decode 默认包含 `--json-model-override-args '{"dsv4_reasoning_effort_profile":"official"}'`，已有配置需显式合并该键。
- 服务只在容器内运行；未经确认不执行任何写操作，不删容器，不动 docker 配置。
- 可切换的只有模型（`[model]`）和 sglang 版本（`[image]`）；模板默认值是 DeepSeek-V4-Flash + sglang 0.5.18，换模型后 env / args 需自行调整。
- 局限：仅支持 sglang PD 分离、单机实例（每个实例在一台节点内），多机 TP / EP 暂不支持。

## eval/aider-benchmark（aider-benchmark）：aider polyglot benchmark 的安装与测试

流程：确认现状 → 准备镜像（`docker load` 或从头构建，含工具链安装脚本）→ 起容器 → 跑 benchmark → 汇总结果 → 需要时用单请求复现“思考阶段重复循环”。

| 文件 | 说明 |
|---|---|
| `eval/aider-benchmark/SKILL.md` | 流程与规则 |
| `eval/aider-benchmark/references/params.md` | 参数配置详解：benchmark 命令行、yml、超时与重试、temperature、服务端 reasoning profile 与 MTP 参数、实测基线 |
| `eval/aider-benchmark/scripts/install_toolchains.sh` | 容器内装 go / rust / java / cmake 并预置 gradle 8.7（换国内源，幂等） |
| `eval/aider-benchmark/scripts/run_container.sh` | 用 `aider_run` 镜像起容器，带齐挂载和内存限制 |
| `eval/aider-benchmark/scripts/summarize_results.py` | 汇总结果目录，可两次运行逐题对照；口径与官方 `--stats` 一致 |
| `eval/aider-benchmark/scripts/single_req.py` | 单个流式请求，记录进度，用于复现重复循环 |
| `eval/aider-benchmark/configs/` | `think600.yml`（思考开、请求超时 600 秒）、`think600_temp1.yml`（再加 temperature=1） |
| `eval/aider-benchmark/repro/A_spiral-matrix_lint-fix.json` | 复现重复循环用的真实请求 |

要点：

- benchmark 会执行模型生成的代码，只在容器里跑；起任务前确认服务空闲，不动别人的服务和容器。
- 启动容器必须显式提供 `AIDER_DIR` 和 `OPENAI_API_BASE`，例如 `AIDER_DIR="/data/<用户名>/aider/aider" OPENAI_API_BASE="http://<服务IP>:30000/v1" bash eval/aider-benchmark/scripts/run_container.sh`；节点和个人路径不再作为运行默认值。
- 超时有三层：请求超时（yml `timeout`）、失败后重试几次（`benchmark.py` 的 `LONG_TIMEOUT`）、openai 客户端自带重试（yml `max_retries`），不要混。
- 统计超时数要数异常名 `litellm.Timeout`，不要数单词 `Timeout`。
- 局限：只覆盖 aider `5dc9490bb` 的 `benchmark/benchmark.py` 和 polyglot-benchmark；`docker load` 路径、数据集 `gradlew` 的修复命令等没有实测的部分，在 SKILL.md 里逐条标注。
