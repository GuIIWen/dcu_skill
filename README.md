# dcu_skill

Hygon DCU 节点的三个 agent skill（Claude Code / Codex 通用）的集合目录。三个 skill 都通过 [remote-linux MCP](https://github.com/GuIIWen/remote-mcp) 操作远端 Linux 节点；`dcu_skill` 本身不是一个 skill。

| 目录 | skill 名 | 作用 |
|---|---|---|
| [`deploy/node-setup/`](deploy/node-setup) | `node-setup` | 全新 GPU/DCU 节点初始化：勘察、挂盘、装 docker、装海光 DCU 驱动 |
| [`inference/sglang-pd-deploy/`](inference/sglang-pd-deploy) | `sglang-pd-deploy` | 在节点上部署 sglang PD 分离推理服务（xPyD + mooncake + sglang_router） |
| [`eval/aider-benchmark/`](eval/aider-benchmark) | `aider-benchmark` | 在容器里安装并运行 aider polyglot benchmark，对接 OpenAI 兼容的 sglang 服务；含参数配置、结果汇总、重复循环的单请求复现 |

## 前置依赖

- 本机已部署并启动 remote-linux MCP daemon（`remote-linux-mcp` skill 在 remote-mcp 仓库中），会话里能用 `exec`、`write_file`、`read_file`、`list_hosts`。
- 目标节点：Ubuntu + 海光 DCU + docker CE。
- `inference/sglang-pd-deploy/render.py` 需要本机 Python >= 3.11。

## 安装

从集合目录中复制实际的 skill 目录；实际 skill 目录名要和 `SKILL.md` 里的 `name` 一致：

```bash
# Claude Code
mkdir -p ~/.claude/skills
cp -r deploy/node-setup              ~/.claude/skills/node-setup
cp -r inference/sglang-pd-deploy     ~/.claude/skills/sglang-pd-deploy
cp -r eval/aider-benchmark           ~/.claude/skills/aider-benchmark

# Codex
mkdir -p ~/.codex/skills
cp -r deploy/node-setup              ~/.codex/skills/node-setup
cp -r inference/sglang-pd-deploy     ~/.codex/skills/sglang-pd-deploy
cp -r eval/aider-benchmark           ~/.codex/skills/aider-benchmark
```

之后直接用自然语言触发，例如“初始化这台新节点”“按 deploy.toml 部署 sglang 1P1D”。

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
- 盘名、挂载点、data-root、内网仓库、驱动版本都只是默认参考，以 survey 结果和用户指示为准。
- 局限：仅覆盖 Ubuntu + 海光 DCU + docker CE；部分分支（真实空白盘、ABORT 路径）未在真机验证，详见 `deploy/node-setup/SKILL.md`。

## inference/sglang-pd-deploy（sglang-pd-deploy）：sglang PD 分离部署

流程：填写 `deploy.toml` → `render.py` 渲染 → 卡检查与预检 → 用户确认方案 → 拉镜像、建容器 → 起 mooncake master、prefill / decode、router → 验收。

| 文件 | 说明 |
|---|---|
| `inference/sglang-pd-deploy/SKILL.md` | 流程与规则 |
| `inference/sglang-pd-deploy/deploy.example.toml` | 部署清单模板（默认 1P1D、每实例 8 卡、两台节点） |
| `inference/sglang-pd-deploy/deepep-config.json` | 默认 deepep 配置 |
| `inference/sglang-pd-deploy/render.py` | 展开拓扑，校验，生成脚本、`plan.md`、`params.json`、`plan.json` |

快速使用：

```bash
cp inference/sglang-pd-deploy/deploy.example.toml deploy.toml   # 按场景修改
python inference/sglang-pd-deploy/render.py deploy.toml          # 生成 plan.md / plan.json 等
```

要点：

- 所有启动命令都取自 `plan.json`，不手拼；改参数就改 `deploy.toml` 后重新渲染。
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
- 超时有三层：请求超时（yml `timeout`）、失败后重试几次（`benchmark.py` 的 `LONG_TIMEOUT`）、openai 客户端自带重试（yml `max_retries`），不要混。
- 统计超时数要数异常名 `litellm.Timeout`，不要数单词 `Timeout`。
- 局限：只覆盖 aider `5dc9490bb` 的 `benchmark/benchmark.py` 和 polyglot-benchmark；`docker load` 路径、数据集 `gradlew` 的修复命令等没有实测的部分，在 SKILL.md 里逐条标注。
