# Harbor 环境准备

以下命令在选定的 Linux 执行节点运行。先使用现有 SSH/远端连接确认 Python、Docker、工作目录和磁盘；不要在本地 Windows 主机上照抄远端 Bash 命令。

## 选择运行方式

- 本地 Docker：适合环境调试与内部诊断；要求当前用户能操作目标 Docker daemon。正式 locked 仍需验证实际阶段隔离，不能仅看 task.toml。
- Modal：SWE-bench Pro V2 官方 locked 示例使用此后端；所选 checkout 的 README 优先。当前 V2 示例要求 `harbor[modal]>=0.22`、Modal >=1.5.1，并执行 `modal setup`。

## 勘察与冻结 checkout

```bash
python3.12 --version
git --version
docker version
docker context show
docker info --format '{{.DockerRootDir}}'
df -h "$WORK_DIR"

# 已有 checkout 则直接复用并检查，不重新覆盖
git clone https://github.com/scaleapi/SWE-bench_Pro-os.git "$REPO"
git -C "$REPO" rev-parse HEAD
git -C "$REPO" status --porcelain
```

`WORK_DIR/REPO/HARNESS_VENV/RUN_DIR` 使用本次节点的绝对路径；`WORK_DIR` 需事先存在。先按选定数据集版本确认 commit，再冻结题集。存在 `v2/SHA256SUMS` 时在 `v2` 目录用可用的 SHA256 工具核验。镜像导入需预留归档与解包层的双份空间。

## 独立安装

当前 Harbor 发行包要求 Python >=3.12；下载辅助脚本本身只要求 >=3.11。复现旧实验时按其依赖约束选择 Python/Harbor，不能只装最新版。

```bash
python3.12 -m venv "$HARNESS_VENV"
# 新环境可从稳定版开始；已选 checkout 有版本约束时用其准确 spec
HARBOR_SPEC='harbor'
"$HARNESS_VENV/bin/python" -m pip install "$HARBOR_SPEC"
"$HARNESS_VENV/bin/python" -m pip check
"$HARNESS_VENV/bin/python" -c 'from importlib.metadata import version; print(version("harbor"))'
"$HARNESS_VENV/bin/harbor" run --help
```

Modal 分支将 `HARBOR_SPEC` 换为与 checkout 对齐的 extras spec，例如 `'harbor[modal]>=0.22'`；在同一 venv 中核对 Modal 版本并执行 `"$HARNESS_VENV/bin/modal" setup`。它会使用本次使用者的 Modal 登录与资源。

进入运行流程前保存实际安装版本：

```bash
mkdir -p "$RUN_DIR"
"$HARNESS_VENV/bin/python" -m pip freeze > "$RUN_DIR/harness.freeze.txt"
"$HARNESS_VENV/bin/harbor" run --help > "$RUN_DIR/harbor-run-help.txt"
HARBOR_BIN="$HARNESS_VENV/bin/harbor"
```

后续 `run_subset.py` 加 `--harbor-bin "$HARBOR_BIN"`，避免 PATH 指到其它环境。repo 自带的自定义 agent 由启动脚本加入 `PYTHONPATH=$REPO/v2/tooling`；其它 agent 依赖按选中集成安装并重新检查。LiteLLM bridge 使用另一个 venv，避免 proxy extras 改坏 harness 依赖。

## 准备镜像与验收

本地 Docker 路径按 [镜像准备](images-and-proxy.md) 生成清单、预览并下载；Modal 镜像由其后端获取，本地 Docker 缓存不能代替 Modal 的 registry 可达性检查。

从指定题集选 1–2 个完整 ID 写入 `SMOKE_IDS`，先预览以下命令，再加 `--execute`：

```bash
python3 "$SKILL_DIR/scripts/run_subset.py" \
  --repo "$REPO" --ids "$SMOKE_IDS" --output "$RUN_DIR/jobs" \
  --harbor-bin "$HARBOR_BIN" --environment docker \
  --agent oracle --job smoke-oracle --concurrency 1

python3 "$SKILL_DIR/scripts/run_subset.py" \
  --repo "$REPO" --ids "$SMOKE_IDS" --output "$RUN_DIR/jobs" \
  --harbor-bin "$HARBOR_BIN" --environment docker \
  --agent nop --job smoke-nop --concurrency 1
```

Modal 使用 `--environment modal`。oracle 应通过、nop 应不通过，且都必须有真实 verifier 产物；它们不需要模型 API key。完成后按 [Agent 与网关](agents-and-gateways.md) 验证模型入口与沙箱访问，再跑一次真实 agent trial。基线通过不能证明模型路径或 locked 网络策略已就绪。

来源：[Harbor 安装](https://docs.harborframework.com/getting-started/installation)、[Python 版本要求](https://pypi.org/project/harbor/)、[SWE-bench Pro V2 运行说明](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md)。
