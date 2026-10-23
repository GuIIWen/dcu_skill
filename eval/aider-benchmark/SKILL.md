---
name: aider-benchmark
description: 用 remote-linux MCP 在 GPU/DCU 节点的容器里安装并运行 aider 的 polyglot benchmark（225 道 Exercism 习题，cpp/go/java/javascript/python/rust），对接 OpenAI 兼容的 sglang 服务。覆盖：镜像与工具链安装、起容器、跑测试时用到的参数（线程数、超时、思考模式与 reasoning_effort、temperature）、汇总结果、复现"思考阶段重复循环"的单请求。当用户要装 aider benchmark 环境、跑 benchmark、调这些参数、看结果或排查超时/循环时使用。
---

# aider polyglot benchmark：安装与测试

## 适用范围

- 只覆盖 aider 仓库 `5dc9490bb`（v0.86.3.dev53）自带的 `benchmark/benchmark.py` 和 polyglot-benchmark 数据集（225 题：cpp 26、go 39、java 47、javascript 49、python 34、rust 30）。其他 aider 版本、其他 benchmark 不适用。
- 对接的模型服务是 OpenAI 兼容接口（本环境是 sglang PD 分离部署的 DeepSeek-V4-Flash，经 router 的 `:30000/v1`）。
- 运行环境：Ubuntu 22.04 容器（镜像 `aider_run`），宿主机是 Hygon DCU 节点（tencent05/06）。
- 依赖 remote-linux MCP 的 `exec`、`write_file`、`read_file`。下文 `<SKILL>` 是本 skill 目录，里面有 `scripts/`、`configs/`、`repro/`、`references/`。参数的完整说明和实测数据见 `references/params.md`。

## 必须遵守

1. benchmark 会执行模型生成的代码，**只在容器里跑，不在宿主机上跑**。
2. 节点上有别人的服务和容器。起任务前先看服务是否空闲（`curl <host>:<端口>/get_load`，`num_reqs` 不是 0 就告诉用户）；不停、不重启别人的服务和容器；只结束自己起的进程，按 pid 或带方括号的模式。
3. 判断进程是否还在，**不能用普通的 `pgrep -f 关键词`**：exec 外壳的命令行本身含这个关键词，会匹配到自己。用 `pgrep -f '[b]enchmark.py 运行名'`，或看结果文件。
4. exec 单次调用静默超过约 300 秒会被中止。长任务用 `docker exec -d ... setsid nohup ... &` 放后台，轮询每次不超过约 100 秒；hop/ssh 偶发失败，等 20 秒重试。
5. 结果目录里的文件是容器里的 root 写的（宿主机上是 root:root），删除要 sudo。不要随意删历史结果。
6. 改 `benchmark.py` 之前先备份。本环境里改过 `LONG_TIMEOUT`（见 `references/params.md`）。
7. 起容器、`docker commit`、`docker load`、改 docker/containerd 配置都先告诉用户；删镜像、删容器只在用户明确要求时做。

## 流程

### 1. 先确认现状（只读）

- 镜像：`docker images aider_run`。
- aider 仓库（`<AIDER_DIR>`，本环境 `/data0/songjiajun/aider/aider`）和数据集（`<AIDER_DIR>/tmp.benchmarks/polyglot-benchmark`，含六个语言目录）。
- 服务可达：`curl http://<服务>:30000/v1/models`。
- **数据集的两个坑**（本环境实测）：
  - zip 解压出来的那份 `/data0/songjiajun/polyglot-benchmark`，47 个 Java 习题的 `gradlew` 全是 CRLF 换行且没有可执行位（shebang 变成 `#!/bin/sh\r`，运行报“找不到命令”，返回 127）。正在使用的 `tmp.benchmarks/polyglot-benchmark` 没有这个问题。检查：`head -1 gradlew | od -c`（应是 `#!/bin/sh` 加 `\n`）和 `ls -l gradlew`（应有 x）。修复思路是 `sed -i 's/\r$//' gradlew && chmod +x gradlew`，**这个修复我没有实测过**，用之前先在一个习题上试。
  - JS 习题的依赖：本环境每个习题目录自带 `node_modules`（49/49），镜像里**没有** `/npm-install`；`npm-test.sh` 只在习题目录没有 `node_modules` 时才去链接 `/npm-install`。数据集副本如果没有 `node_modules`，在容器里 `cd <数据集>/javascript/exercises/practice && for d in */; do (cd "$d" && npm install); done`（shell 历史里的做法，npm 源用 npmmirror，本次没有重跑）。

### 2. 准备镜像

**快路径**：已有镜像 tar 时 `docker load -i <tar>`。本环境的 tar 在 tencent06 的 `/data0/songjiajun/airun_songj_261023.tar`（约 1.5 GB，含 `aider_run:20261023` 和 `aider_run:latest`，sha256 `c1087efbb8622f6f2d38fefb8a3f5b0d82987635d910b3aa3e7448e50e9f764e`，blob 校验通过）。**`docker load` 这一步没有在新节点上实测过。**

**从头构建**（基础镜像 `docker.1ms.run/library/buildpack-deps:jammy`）：

1. 起基础容器，挂 `<AIDER_DIR>:/aider` 和 `<BENCH_DIR>:/benchmarks`，设 `PYPI_INDEX_URL`（本环境用 tuna 或 aliyun 的 pypi 镜像）。
2. 容器内：`python3 -m pip install -e /aider -i $PYPI_INDEX_URL` 和 `pip install pytest`。`benchmark.py` 还要 `lox`、`pandas`、`matplotlib`、`pillow`、`imgcat`（缺 `lox` 时 import 直接失败；aider README 的 `pip install -e .[dev]` 可能一次装齐，**没有验证**）。
3. `git config --global --add safe.directory /aider`（宿主机文件属主和容器 root 不同，不加会报 dubious ownership）。
4. 工具链：容器内以 root 运行 `bash <SKILL>/scripts/install_toolchains.sh`。它换 apt 源、装 go/rust/java/cmake、预置 gradle 8.7，幂等。已在干净的基础镜像里验证：约 75 秒装完，第二次运行不重复下载，Java、C++、Go、Rust 习题都能编译并跑测试（stub 按预期失败）。
5. Node 20：apt 的 nodejs 太旧。shell 历史里的做法：`wget https://registry.npmmirror.com/-/binary/node/v20.18.0/node-v20.18.0-linux-x64.tar.xz`，`tar -xf ... -C /usr/local --strip-components=1`，然后 `hash -r; node -v`（该地址当天 HEAD 返回 302，没有完整下载验证）。
6. `docker commit` 成镜像，需要时 `docker save -o <tar> <镜像>`。

镜像里已有的版本（实测）：python 3.10.12、litellm 1.82.3、openai 2.28.0、pytest 9.1.1、go 1.18.1、rustc/cargo 1.75.0、OpenJDK 11.0.32.1、cmake 3.22.1、gradle 8.7、node 20.18.0。

**镜像源速度**：腾讯云节点到官方 apt 源约 46 KB/s，到 `services.gradle.org` 约 12 KB/s，装完整个工具链要几个小时。国内源的速度波动很大：2026-10-23 下午实测清华 1.7 MB/s（容器里曾低到 141 KB/s）、中科大 15.5 MB/s、阿里云 221 KB/s。装之前先对几个源各下 5 MB 看速度（`curl -r 0-5000000 -o /dev/null -w '%{speed_download}'`），用 `UBUNTU_MIRROR=...` 覆盖脚本默认值。

### 3. 起容器

`bash <SKILL>/scripts/run_container.sh`（环境变量见脚本头部：`AIDER_IMAGE`、`CONTAINER_NAME`、`AIDER_DIR`、`BENCH_DIR`、`OPENAI_API_BASE`、`MEMORY`）。`docker commit` 不保存挂载和内存限制，所以必须每次带上；aider 本体是 editable 安装、数据集也不在镜像里，不挂载 `import aider` 会失败。脚本在容器已存在、目录不存在时拒绝执行。

### 4. 跑 benchmark

先把 `configs/think600.yml` 放到容器能读到的位置（如 `<BENCH_DIR>/repro/think600.yml`），再在 `<BENCH_DIR>` 下建一个新的结果目录，避免和别的运行混在一起：

```bash
mkdir -p <BENCH_DIR>/<目录>
docker exec -d -w /aider <容器> bash -lc "export AIDER_BENCHMARK_DIR=/benchmarks/<目录> \
  OPENAI_API_BASE=http://<服务>:30000/v1 AIDER_OPENAI_API_BASE=http://<服务>:30000/v1 OPENAI_API_KEY=EMPTY; \
  setsid nohup ./benchmark/benchmark.py <运行名> --model openai/<模型名> --edit-format diff \
  --threads 8 --tries 2 --num-tests 16 --reasoning-effort max \
  --read-model-settings /benchmarks/repro/think600.yml \
  --exercises-dir /benchmarks/polyglot-benchmark --new > /benchmarks/<目录>/bench.log 2>&1 < /dev/null &"
```

- 工作目录必须是 `/aider`（`-w /aider`）。
- 结果目录是 `<AIDER_BENCHMARK_DIR>/<UTC 时间>--<运行名>/`，**目录名里的时间是 UTC**（CST 12:06 启动，目录叫 `04-06-19`）。
- `--new` 会先复制整个数据集（约 105 万个文件，主要是 `node_modules`），第一个请求发出前要几分钟，不是卡住。
- `--num-tests N` 是先随机打乱再取 N 题。要复现同一批题，用 `--keywords` 精确列出，逗号分隔，按子串匹配习题相对路径，写成 `语言/exercises/practice/习题名`。已验证用这种写法 16 题和 8 题的列表都恰好匹配、没有多配。
- 不加 `--keywords` 和 `--num-tests` 就是全部 225 题。本环境 8 线程全量跑了约 12.5 小时，容器内存限制 12GB，225 题都有结果文件、没有崩溃的。
- temperature 默认是 0，要改成 1 用 `configs/think600_temp1.yml`（见 `references/params.md`）。
- 想让 benchmark 带着其他参数运行，**先用假服务抓一次 aider 实际发出的请求体再启动**，确认参数真的进了请求：参数写在 yml 的 `extra_params` 里，`extra_body` 里的内容会进请求体顶层，`timeout`、`max_retries` 是客户端参数，不进请求体。

### 5. 看结果

```bash
python3 <SKILL>/scripts/summarize_results.py <结果目录>               # 总体 + 分语言 + 需留意的题
python3 <SKILL>/scripts/summarize_results.py <A> --vs <B> --tsv x.tsv  # 两次运行逐题对照
docker exec -w /aider <容器> ./benchmark/benchmark.py --stats /benchmarks/<目录>/<结果目录>   # 官方汇总
```

对 think-full 算出的数字和官方 `--stats` 完全一致（41/83，18.2%/36.9%）。读结果时注意：

- `pass_rate_2` 是**累计**（第 1 次或第 2 次通过都算），含第一次就通过的。
- `.aider.results.json` 里只有 `exception`、没有 `tests_outcomes`，表示这道题跑崩了，多半是工具链缺失、`gradlew` 问题或数据集问题；汇总脚本会列出最后一行异常。
- **零 token 却显示通过**的题（模型一个 token 都没返回，请求全部超时）多半是起始代码本来就能过测试（本环境的 `ledger`、`tree-building`、`counter` 这类），不是模型的功劳。汇总脚本会单独标出。
- 统计超时用异常名：`grep -c 'litellm.Timeout' bench.log`。**不要数单词 `Timeout`**：Java 测试堆栈里的 `TimeoutExtension`、Rust 拉依赖的网络警告都会混进来（实测 637 对 538）。
- `num_error_outputs` 之和（think-full 是 498）和日志里 `litellm.Timeout` 的数量（538）差 40，原因没有查清。
- 请求是非流式的（`benchmark.py` 里写死 `stream=False`），aider 在生成结束前收不到任何数据，所以一个请求要么在超时前整个返回，要么什么都看不到；看不到模型在中途输出了什么。

### 6. 复现"思考阶段重复循环"

`repro/A_spiral-matrix_lint-fix.json` 是 benchmark 里 `cpp/spiral-matrix` 的“修复 lint 提示”那一轮的真实请求（14 条消息，最后一条指向 `spiral_matrix.h` 第 8 行，那一行本身没有错）。模型对它**经常**陷入反复输出同一句 `Maybe the error is ...` 而不结束，不是每次都会。

```bash
python3 <SKILL>/scripts/single_req.py <SKILL>/repro/A_spiral-matrix_lint-fix.json \
  http://<服务>:30000/v1/chat/completions <输出前缀> 1800 max
```

- 只用标准库，**在宿主机上跑即可，不必进容器**。参数依次是请求文件、地址、输出前缀、客户端超时秒数、可选的 effort。
- 脚本强制 `thinking=true`、按参数加 `reasoning_effort`、去掉 `max_tokens`、用流式；每 15 秒往 `<前缀>.progress.tsv` 记一行，结束后写 `<前缀>.summary.json`、`.reasoning.txt`、`.content.txt`、实际发出的 `.req.json`。
- 判断循环：`finish_reason` 不是 `stop`、`.content.txt` 为空、`.reasoning.txt` 里 `Maybe the error is` 重复几百次。
- 注意 prompt A 里 `temperature` 是 0；要测别的值先复制一份改掉，不要改这个文件。

观测数据（2026-10-22/23，样本都很小，只能当参考，**不要据此下概率结论**）：

| 服务 | 方式 | 结果 |
|---|---|---|
| tencent06，带 MTP（DSPARK） | 10 并发、600 秒超时，effort 取 low/high/max 各 10 次 | 600 秒内结束的：low 1/10、high 6/10、max 5/10，其余全部在循环 |
| tencent05，不带 MTP | 3 个并发、1200 秒超时 | 2 个循环到超时，1 个 140 秒结束 |
| tencent05，不带 MTP | 3 个并发、1800 秒超时 | 2 个循环到超时（各约 2.46 万个 chunk，答案为空），1 个 109 秒结束 |

没带 MTP 的服务上同样会出现循环，说明循环**不依赖** MTP。两边的并发、超时、`--context-length` 等条件都不一样，没有控制变量，所以不能用这几组数字比较两边的发生率。05 上单个请求的生成速度约 13.5 token/s，所以 600 秒只够生成约 8000 token，思考加答案超过这个长度的请求，不管是不是循环都会超时。

## 已知问题与局限

- 镜像 `docker load` 路径、数据集 gradlew 的修复命令、`pip install -e .[dev]` 一次装齐、Node tarball 的完整下载，都没有在本环境实测，文中已标注。
- `LITELLM_LOCAL_MODEL_COST_MAP=True` 只验证了能让 `import litellm` 从 7.1 秒降到 1.7 秒，没有在完整 benchmark 里跑过（`references/params.md` 里有细节）。
- 结果受服务端配置、并发、上下文长度影响，不同次运行之间的服务端配置不一定一致（06 的 `--context-length` 在 10-23 当天从 307200 变成过 262144），做对照前先确认服务端参数。
- 只测过 DeepSeek-V4-Flash 这一个模型，思考模式靠请求里的 `chat_template_kwargs.thinking`，其他模型要另行确认。
- 脚本里的默认路径、IP、模型名都是本环境的值，换环境要通过环境变量或参数覆盖。
