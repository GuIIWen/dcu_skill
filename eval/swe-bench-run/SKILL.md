---
name: swe-bench-run
description: 准备、运行和验收 SWE-bench 测试，提供 Pro V2 / Harbor 的环境准备、镜像站映射与批量下载、Claude Code / mini-swe-agent / Codex 及原生 API / LiteLLM 组合流程。用于冒烟、分组对照、重复测试和结果排障；不负责部署推理集群或初始化机器。
---

# SWE-bench 测试

将选定题集跑成可复现、可核验的实验。脚本面向 **SWE-bench Pro V2 + Harbor**；使用 Verified/Lite 或 Pro v1 时保留用户选择，按对应 harness 调整，不直接套用 V2 命令。

## 先确定输入

先查当前目录、连接配置和已有任务，只补问影响执行的缺项：

- 数据集、仓库 commit、完整题目 ID 清单、SHA256 和实际题数。
- 执行节点、连接、工作目录、Docker context/root、磁盘余量和下载出口。
- agent 类型/版本、diagnostic 或 locked 协议、并发、重复次数、超时和预算。
- 模型别名、实际后端、入口/API 协议、鉴权来源、generation 参数及网关改写。

## 执行流程

1. **准备环境。** 新环境读 [Harbor 准备](references/harbor-setup.md)，固定 checkout 与依赖，保存版本和本地修改。
2. **准备镜像。** 用 `scripts/prepare_images.py` 生成官方镜像清单，再用 `scripts/pull_images.py` 预览或执行下载。镜像站、代理和离线导入见 [镜像与网络](references/images-and-proxy.md)。
3. **验证模型路径。** 按 [Agent 与网关](references/agents-and-gateways.md) 检查宿主机、任务沙箱和安装路径，验证实际 streaming、thinking、工具往返、usage 和有效 effort；`/v1/models` 不能替代这些检查。
4. **跑小样本。** oracle/nop 基线后，跑 1–2 个代表性 agent trial；确认后端请求、patch、verifier 和退出行为。异构镜像逐类检查 agent CLI。
5. **运行矩阵并验收。** 用 `scripts/run_subset.py` 构造精确题集，默认预览，加 `--execute` 执行。按 [运行与证据](references/run-and-evidence.md) 保存各组条件，对账题数、有效评分、错误和 usage；locked 主报干净镜像复评分。

## 常用命令

在执行节点填写绝对路径：`SKILL_DIR` 为本 skill 目录，`REPO` 为 checkout，`IDS` 为完整 task ID 清单，`RUN_DIR` 为本轮输出目录。脚本使用 Python 3.11+；Harbor 的 Python 要求见环境准备文档。

```bash
python3 "$SKILL_DIR/scripts/prepare_images.py" \
  --tasks-root "$REPO/v2/tasks" --ids "$IDS" \
  --output "$RUN_DIR/images.json" --images-out "$RUN_DIR/images.txt"

# 预览镜像下载；需要镜像站时追加 --registry-map "$REGISTRY_MAP"
python3 "$SKILL_DIR/scripts/pull_images.py" \
  --manifest "$RUN_DIR/images.json" --output "$RUN_DIR/image-pull.json"
# 相同参数加 --execute 才拉取；再次执行会重新检查并跳过已有镜像

python3 "$SKILL_DIR/scripts/run_subset.py" \
  --repo "$REPO" --ids "$IDS" --job cc-armA-r1 --output "$RUN_DIR/jobs" \
  --agent claude-code --model "$MODEL_ALIAS" \
  --base-url "$GW_ROOT" --allow-agent-host "$GW_HOST" \
  --ak reasoning_effort=max --concurrency 4
```

## 执行边界

按已授权的题集和预算推进。失败先定位镜像、安装、网络/鉴权、API、agent、超时或 verifier；小样本同类错误持续出现时先修复，不扩大批次。共享 Docker 重启、他人作业终止和共享缓存清理需要独立授权。密钥不写入命令预览或共享产物，停止按本 job/PID/容器定位，不用宽泛进程名。

复现旧版本或查来源时读 [版本与来源](references/history-and-sources.md)；LiteLLM 旧版补丁仅在对应问题复现时按网关参考文档使用。
