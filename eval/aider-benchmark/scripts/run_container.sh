#!/usr/bin/env bash
# 用 aider_run 镜像起 benchmark 容器。
# docker commit 不会保存挂载点和内存限制，所以每次起容器都要带上这些参数。
# 镜像里没有 aider 本体（editable 安装，只有指向 /aider 的 .pth）和数据集，必须挂载。
set -euo pipefail

IMAGE="${AIDER_IMAGE:-aider_run:latest}"
NAME="${CONTAINER_NAME:-aider_run_c}"
AIDER_DIR="${AIDER_DIR:-/data0/songjiajun/aider/aider}"        # aider 仓库: benchmark/ 目录和 aider 代码都在这里
BENCH_DIR="${BENCH_DIR:-${AIDER_DIR}/tmp.benchmarks}"     # 习题数据集 polyglot-benchmark + 全部结果目录
API_BASE="${OPENAI_API_BASE:-http://10.208.194.7:30000/v1}"
API_KEY="${OPENAI_API_KEY:-EMPTY}"
MEM="${MEMORY:-12g}"

[ -d "${AIDER_DIR}/benchmark" ] || { echo "AIDER_DIR 下没有 benchmark/: ${AIDER_DIR}" >&2; exit 1; }
[ -d "${BENCH_DIR}/polyglot-benchmark" ] || echo "WARN: ${BENCH_DIR}/polyglot-benchmark 不存在，跑 benchmark 前先放数据集" >&2
docker image inspect "${IMAGE}" >/dev/null 2>&1 || { echo "镜像不存在: ${IMAGE}（docker load -i <tar> 或重新构建）" >&2; exit 1; }
if docker ps -a --format '{{.Names}}' | grep -qx "${NAME}"; then
  echo "容器 ${NAME} 已存在，不覆盖: 用 docker start ${NAME}，或换 CONTAINER_NAME" >&2; exit 1
fi

docker run -itd --name "${NAME}" \
  --memory="${MEM}" --memory-swap="${MEM}" \
  -v "${AIDER_DIR}:/aider" \
  -v "${BENCH_DIR}:/benchmarks" \
  -e OPENAI_API_KEY="${API_KEY}" \
  -e OPENAI_API_BASE="${API_BASE}" \
  -e AIDER_OPENAI_API_BASE="${API_BASE}" \
  -e AIDER_DOCKER=1 \
  -e AIDER_BENCHMARK_DIR=/benchmarks \
  -w /aider \
  "${IMAGE}" bash

echo "容器 ${NAME} 已启动。检查:"
docker exec "${NAME}" python3 -c 'import aider, sys; print("aider:", aider.__file__)'
