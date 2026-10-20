# dcu_skill

Hygon DCU 节点的两个 agent skill（Claude Code / Codex 通用），都通过 [remote-linux MCP](https://github.com/GuIIWen/remote-mcp) 操作远端 Linux 节点。

| 目录 | skill 名 | 作用 |
|---|---|---|
| [`deploy/`](deploy) | `node-setup` | 全新 GPU/DCU 节点初始化：勘察、挂盘、装 docker、装海光 DCU 驱动 |
| [`inference/`](inference) | `sglang-pd-deploy` | 在节点上部署 sglang PD 分离推理服务（xPyD + mooncake + sglang_router） |

## 前置依赖

- 本机已部署并启动 remote-linux MCP daemon（`remote-linux-mcp` skill 在 remote-mcp 仓库中），会话里能用 `exec`、`write_file`、`read_file`、`list_hosts`。
- 目标节点：Ubuntu + 海光 DCU + docker CE。
- `inference/render.py` 需要本机 Python >= 3.11。

## 安装

skill 目录名要和 `SKILL.md` 里的 `name` 一致，复制到用户级 skills 目录即可：

```bash
# Claude Code
mkdir -p ~/.claude/skills
cp -r deploy    ~/.claude/skills/node-setup
cp -r inference ~/.claude/skills/sglang-pd-deploy

# Codex
mkdir -p ~/.codex/skills
cp -r deploy    ~/.codex/skills/node-setup
cp -r inference ~/.codex/skills/sglang-pd-deploy
```

之后直接用自然语言触发，例如“初始化这台新节点”“按 deploy.toml 部署 sglang 1P1D”。

## deploy（node-setup）：节点初始化

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
- 局限：仅覆盖 Ubuntu + 海光 DCU + docker CE；部分分支（真实空白盘、ABORT 路径）未在真机验证，详见 `deploy/SKILL.md`。

## inference（sglang-pd-deploy）：sglang PD 分离部署

流程：填写 `deploy.toml` → `render.py` 渲染 → 卡检查与预检 → 用户确认方案 → 拉镜像、建容器 → 起 mooncake master、prefill / decode、router → 验收。

| 文件 | 说明 |
|---|---|
| `SKILL.md` | 流程与规则 |
| `deploy.example.toml` | 部署清单模板（默认 1P1D、每实例 8 卡、两台节点） |
| `deepep-config.json` | 默认 deepep 配置 |
| `render.py` | 展开拓扑，校验，生成脚本、`plan.md`、`params.json`、`plan.json` |

快速使用：

```bash
cp inference/deploy.example.toml deploy.toml   # 按场景修改
python inference/render.py deploy.toml          # 生成 plan.md / plan.json 等
```

要点：

- 所有启动命令都取自 `plan.json`，不手拼；改参数就改 `deploy.toml` 后重新渲染。
- 服务只在容器内运行；未经确认不执行任何写操作，不删容器，不动 docker 配置。
- 可切换的只有模型（`[model]`）和 sglang 版本（`[image]`）；模板默认值是 DeepSeek-V4-Flash + sglang 0.5.18，换模型后 env / args 需自行调整。
- 局限：仅支持 sglang PD 分离、单机实例（每个实例在一台节点内），多机 TP / EP 暂不支持。
