---
name: sglang-pd-deploy
description: 按渲染后的部署计划，在用户提供的连接方式上部署、重启、停止并验收 SGLang PD 分离推理服务（xPyD + Mooncake + sglang_router）。连接方式可以是 SSH、跳板机、Ansible、Kubernetes exec/cp、remote-linux MCP、CI runner 或其它能执行目标命令并传输文件的方案；本 skill 不要求或安装特定远端连接工具。仅限 SGLang PD 分离、单机实例；可切换模型和版本。
---

# sglang PD 分离部署

## 适用范围（声明）

- **只做 sglang 的 PD 分离部署**（prefill + decode + mooncake + sglang_router，xPyD）。其它推理框架、非 PD 分离（单实例 / 普通多副本）、其它部署形态都不适用，遇到就告诉用户本 skill 不支持，不要硬套。
- **支持切换的只有模型和 sglang 版本**：模型改 `[model] dir`（和 `served_model_name`），版本改 `[image] ref`（版本由镜像决定）。随之需要调整的 env / args 在 `[env.*]` / `[args.*]` 里改；模板默认值是 DeepSeek-V4-Flash + sglang 0.5.18 的组合，换模型或版本后这些参数不保证适用，渲染不会替你校验，起不来时按排障表查 cookbook。
- **模型路径要同时核对宿主机和容器映射**：`container.model_root` 会以只读方式挂到容器 `/model`，所以宿主机 `${model_root}/${model.dir}` 对应容器 `/model/${model.dir}`。例如宿主机模型在 `/data0/model/DeepSeek...` 且 `model_root = "/data0"` 时，`model.dir` 必须写 `model/DeepSeek...`，容器路径才是 `/model/model/DeepSeek...`。渲染后的目标容器检查必须确认容器内 `config.json` 存在。
- **当前只支持单机实例**：每个 prefill / decode 实例必须在一台节点内（`cards ≤ cards_per_node`，如 8 卡）；一个节点上可以放多个实例（卡不重叠），多节点上可以分别放不同实例。**一个实例跨多台节点（多机 TP / EP）暂不支持**：用户提出这类场景（`prefill_cards`/`decode_cards` 大于 `cards_per_node`，或显式写 `nodes = [...]`）时，告知不支持，不要生成也不要部署。render.py 里的跨节点代码未在真机验证，不得使用。
- 运行环境：节点是 Hygon DCU（hy-smi / ROCm 系），服务在容器内运行。节点连接由用户选择并负责提供；本 skill 只要求连接适配器能够在指定目标上执行计划命令、读取输出和日志、上传渲染脚本。

默认功能开关是 `features.hicache = true`、`features.mtp = true`。用户说“关闭 HICache”或“关闭 MTP”时，必须写入 `deploy.toml` 的 `[features]`，或写入 `--params` JSON 的 `features` 段后重新渲染，不能只在口头确认里记住：

- 关闭 HICache：移除 `--enable-hierarchical-cache`、`--hicache-ratio`、`--enable-two-batch-overlap`、`--hicache-mem-layout`、`--hicache-io-backend`、`--hicache-write-policy`、`--hicache-storage-backend`、`--hicache-storage-prefetch-policy`；保留 `--disaggregation-transfer-backend mooncake`，因为它仍负责 PD 的 KV 传输。
- HICache 关闭时，同时关闭 Mooncake **Store master / HTTP metadata 服务**，移除 `MOONCAKE_LOCAL_HOSTNAME`、`MOONCAKE_TE_META_DATA_SERVER`、`MOONCAKE_MASTER`、`MOONCAKE_GLOBAL_SEGMENT_SIZE`；不生成或上传 `mooncake_master.sh`，不检查 master 工具、端口或 Store segment 分配。`plan.mooncake.enabled=false`，该组件的启动/停止/检查命令为 `null`，`stop_order` 不包含它。保留 worker 内的 **Mooncake Transfer Engine**、RDMA 参数和传输库；当前 SGLang 0.5.18 镜像使用 `P2PHANDSHAKE`，无需外部 master/metadata。这里“关闭 Mooncake”指关闭 Store 服务，不能把 PD 传输库也删掉。详见 [功能开关与参数归属](references/features.md)。
- 关闭 MTP：移除 `--speculative-algorithm`、`--speculative-num-steps`、`--speculative-eagle-topk`、`--speculative-num-draft-tokens`、`--speculative-moe-a2a-backend`、`--speculative-moe-runner-backend`，以及模板列出的五项 `SGLANG_DSPARK_*` 和 `SGLANG_RAGGED_VERIFY_MODE`；由 `speculative-algorithm` 派生的 `--speculative-draft-model-path` 也必须消失。`SGLANG_DEEPEP_NUM_MAX_DISPATCH_TOKENS_PER_RANK` 是主模型也使用的通用 DeepEP 容量配置，关闭 MTP 时保留；参数归属和当前默认值见上述参考。

Prefill 与 Decode 模板默认均传入 `--json-model-override-args '{"dsv4_reasoning_effort_profile":"official"}'`。它是 DeepSeek-V4 的模型配置，与 HICache/MTP 开关无关，关闭这两个功能时保留。已有 `deploy.toml` / `--params` 文件不会自动继承新模板；更新两个角色的 `args` 后重新渲染；若已有其它 JSON model overrides，在同一个 JSON 对象中合并，不能覆盖掉用户原有键。切换模型时核对该模型专用项。

渲染器会在 `params.json` 和 `params.md` 中输出功能状态、关闭时的自动移除清单，以及每个 member 实际会使用的完整 env/args。参数确认时必须在对话中用 JSON 代码块展示渲染后的最终值；表格和文件路径可以辅助核对，不能代替 JSON 展示，也不能只确认模板默认值。

`KV Cache is allocated` 是普通 GPU KV 内存池日志，关闭 HICache 后仍会出现。判断开关须核对实际 `enable_hierarchical_cache` 和当前镜像日志中的 `hicache_attached`，详见[从日志确认是否关闭](references/features.md#从日志确认是否关闭)。

本 skill 不依赖 MCP 或其它特定连接工具。开始前，用户需要选择一种连接适配器，并把计划中的抽象操作映射到该适配器：

- `run(target, command, timeout, mode)`：在目标宿主机或目标容器执行命令，返回退出码、stdout、stderr；`mode` 标明只读或写操作。
- `upload(target, local_path, remote_path)`：把确认过的文件传到目标宿主机，并保持 LF 换行。
- `read_logs(target, path, tail)`：读取日志尾部和状态文件，用于轮询与验收。
- `list_targets()`（可选）：列出或验证目标别名；没有该能力时由用户直接核对 `deploy.toml` 中的目标。

适配器可以使用 SSH/ProxyJump、Ansible、Kubernetes `exec`/`cp`、remote-linux MCP、CI runner 或人工执行。适配器必须复用用户已有的认证和 known-hosts/访问控制，不把密码、token 或私钥写进 `deploy.toml`、脚本、日志或对话；适配器不可改变 `plan.json` 中的命令、目标和验收条件。本 skill 目录（下文 `<SKILL>`）里有：

- [assets/deploy.example.toml](assets/deploy.example.toml)：部署清单模板（默认 `[topology]` 1P1D、每实例 8 卡、两台节点），env/args 默认值也是“与默认值差异”的基线。复制到包外工作目录，填写用户提供的节点、镜像和目录；不把实际部署配置、端点或凭据提交到 Skill 包。
- [assets/deepep-config.json](assets/deepep-config.json)：默认 deepep 配置。
- [scripts/render.py](scripts/render.py)：读 `deploy.toml`（可选 `--params <json>` 覆盖参数），把场景展开成实例/卡号/端口/容器，校验并生成脚本、`plan.md`（部署方案确认）、`params.json`（参数确认/修改）、`plan.json`（给你执行）。输出放在包外工作目录，保持 Skill 包可独立安装。

**所有启动命令、docker run、检查命令都从 `plan.json` 取，不要自己手拼。** 要改参数就改 `deploy.toml` 后重新渲染。

## 必须遵守

- 连接适配器由用户选择和配置；本 skill 不安装、注册或启动连接服务。第 3 步用户明确确认之前，适配器只允许执行只读命令（预检脚本、`docker ps`、`docker image inspect` 之类）。
- **服务只在容器里运行**：启用的 mooncake master、sglang 各 rank、router 只能用 `plan.json` 里的 `launch_cmd`（`docker exec` 进对应容器）启动，绝不在宿主机上直接起任何服务进程，也不在宿主机上装 sglang 等依赖。运行期检查（health、端口监听、日志扫描、最终 curl）同样通过 `docker exec` 在容器内执行；宿主机只需要 docker。
- **环境检查只认镜像或目标容器**：检查容器内环境（sglang、curl、sglang_router、mooncake_master 等）时，目标容器（本次部署的 `<prefix>_<role>_<n>`）还没创建就用镜像起一次性容器检查（`image_tools_check_cmd`，`docker run --rm`，不挂设备、不起服务）；目标容器已存在才用 `docker exec` 进它检查（`tools_check_cmd`）。**不得借用节点上任何其它容器来判断环境**——别的容器里可能改过系统环境或装过/升级过包，结论不代表镜像。
- **启动阶段不中途停下**：第 8 步开始后，日志无报错就自动推进后续服务直到验收完成，只在致命报错、超时或完成时才停下找用户。
- **启动服务前参数必须由用户确认**：第 5 步的参数确认关口通过之前，不执行任何 `launch_cmd`。
- 不改 docker 配置、不重启 docker、不改宿主机网络。镜像拉不下来就停下报告，网络由用户负责。
- 不删容器（`docker rm`），除非用户在本次对话里明确要求删除某个容器；`on_conflict = "recreate"` 也要在执行前对具体容器名单独确认。
- 只停自己通过 `stop_cmd` 起的进程，不 `pkill`/`kill` 别的进程，不占用预检时已被占用的卡和端口。
- 失败处理：停掉本次已起的所有进程（按 `stop_order`），**保留容器**，报告失败步骤、相关日志尾部和日志路径。
- `exec` 默认超时 60 秒；所有长任务（pull、服务启动）都是后台起、再轮询，单次轮询命令保持在几秒内。

## 流程

### 0. 前置：连接适配器

先确认一种能访问每个目标的连接方式，并写清楚它如何实现本 skill 的四类操作：执行只读命令、执行写命令、上传文件、读取日志/轮询状态。可以直接使用现有 SSH/ProxyJump、Ansible inventory、Kubernetes API、CI runner、remote-linux MCP 或人工执行；没有必要为了本 skill 安装新的 MCP。

适配器的连接配置不放进 Skill 包。只检查以下条件：

1. 每个 `[nodes.<alias>]` 的 `target` 都能被适配器唯一定位。
2. 目标宿主机可执行 Docker、GPU/网络预检和计划命令；目标容器可执行容器内检查与服务命令。
3. 上传路径和 `container.work_dir` 对应，上传后的脚本能从目标容器中看到。
4. 适配器有超时、退出码、stdout/stderr 和日志尾部读取能力，能按步骤轮询，不吞掉失败输出。

如果适配器不可用，先修复连接或改用其它适配器；不要修改部署命令来绕过连接问题，也不要把凭据写进配置或日志。

### 1. 准备 deploy.toml

1. 问用户 deploy.toml 放在哪（本机路径）。没有就从 `<SKILL>/assets/deploy.example.toml` 复制一份到用户指定位置（建议工作目录下 `deploy.toml`）。
2. 先确认场景在适用范围内（见顶部声明）：不是 sglang PD 分离，或有实例需要跨多台节点，就直接告诉用户不支持，不进入后续步骤。然后让用户用自然语言说场景：几 P 几 D、每个实例几张卡、用哪些节点（例如“4 台机器 3P1D，每实例 8 卡”）。据此写 `[topology]`（`prefill`/`decode` 实例数、`prefill_cards`/`decode_cards`、`cards_per_node`、`nodes`）；卡号、端口、容器由 scripts/render.py 自动展开，不要自己算。只有用户给了具体卡号或端口、或要给单个实例单独改 env/args 时，才改用显式 `[[prefill]]`/`[[decode]]`；两种写法只能二选一，端口默认由 `[ports]` 自动分配。
3. 其余必须由用户给出的：`deploy.prefix`、每个节点的 `target` 和 `ip`、`image.ref`、`container.work_dir`、`container.model_root`、`model.dir`。用户没说的保持模板默认值，但在第 3 步的汇总里会全部列出。`[env.*]`/`[args.*]` 是所有实例共用的模板默认值，并行参数用 `"@cards"`（= 实例总卡数）；场景与文档验证场景（单节点 4 卡/实例）不同时 plan.md 顶部会有 WARN，要转述给用户。
4. 用选定适配器确认每个 `target` 都存在且可连接。`target` 是宿主机目标标识，不规定它是 SSH 主机名、跳板机别名、Kubernetes 节点、Ansible 主机还是 MCP 主机；适配器负责账号、跳板、白名单和认证，本 skill 不改这些连接配置。

### 执行与审核分工

- 使用子 agent 时，主流程负责分阶段调度、审核渲染结果和命令输出、向用户展示计划与参数、取得两道确认、决定推进或失败收尾；子 agent 负责执行，包括渲染、卡检查、预检，以及获得相应授权后的拉镜像、建容器、上传、启动、轮询和停止。
- 每次任务写明当前阶段、目标、配置/计划路径、指定命令、已取得的授权和停止条件。子 agent 只执行当前阶段，不自行改命令或参数，不跨过确认关口；检查任务不得顺手修复环境，写任务也不得扩大到其它容器、进程或文件。
- 子 agent 返回实际命令、目标、退出码、原始 `CHECK`/`SUMMARY`、异常输出和日志路径。主流程对照 `plan.json` 审核；卡状态、启动就绪和验收等关键状态由主流程使用计划中的只读命令复查，不凭子 agent 的“成功”结论推进。
- 顺序不能颠倒：主流程审核卡检查和无 `FAIL` 的预检结果 → 部署方案确认 → 拉镜像及镜像工具检查 → 最终参数确认 → 建容器、上传并检查文件 → 分阶段启动和验收。镜像工具检查可与参数确认并行；有依赖的阶段必须等主流程审核放行。
- 镜像工具检查使用 `docker run --rm` 临时容器，必须在部署方案确认后执行；它不启动模型服务，任务中明确允许这一项临时容器操作。不要同时要求执行它又声明“禁止建任何容器”。最终参数确认前仍不得创建/启动部署容器或上传部署脚本。
- 当前环境不支持子 agent，或本次选择单流程执行时，由主流程执行并逐阶段核对，保留相同顺序和确认关口；不要求为了使用本 skill 配置子 agent，也不按客户端名称推断是否支持。

### 2. 渲染 + 卡检查 + 预检

1. 由执行方在本机运行（Python ≥ 3.11）：`python <SKILL>/scripts/render.py <deploy.toml>`（已有参数文件时加 `--params`，见第 5 步）；主流程核对渲染的配置来源、拓扑和参数。校验失败就把错误告诉用户，改好再渲染，不要绕过。下文“执行方”指子 agent，或单流程模式下的主流程。
2. **卡检查（执行方执行，主流程复查，远端最先做）**：通过适配器对 `plan.json` 每个节点同时执行 `run(target=target, command=cards_check_cmd, timeout=60, mode=read-only)`。
   - `cards_check_cmd` 会先在宿主机 shell 中 `source /opt/hyhal/env.sh`（文件存在时），再查 `hy-smi`/`rocm-smi`；不要用其它容器里的工具代替，也不要用 `fuser` 作为卡状态结论。
   - 任一卡 `BUSY` → 停下，报告节点、卡号、显存占用和占用进程；不停别人的进程，让用户换卡/换节点或自行释放后重来。
   - `UNKNOWN`（没有 hy-smi/rocm-smi 或解析失败）→ 把原始输出给用户，由用户确认这些卡空闲后才继续。
3. **预检（执行方执行）**：主流程确认卡全部空闲后，执行方对每个节点跑 `preflight_cmd`（`timeout=120`）并返回全部 `CHECK`/`SUMMARY` 行和异常输出；当前任务只读，不建容器、不起服务、不改文件。预检是最小化检查：docker 可用、镜像存储位置（普通模式看 `DockerRootDir`；containerd image store / `io.containerd.snapshotter` 模式看 `/etc/containerd/config.toml` 的 `root`，缺省 `/var/lib/containerd`；与 `/` 同一文件系统即在系统盘，直接 FAIL）、镜像、同名容器、端口占用、模型 `config.json`、work_dir 可写。网卡/IB、GID、内存等在 `diag_cmd` 里，只在服务起不来或用户要求时跑。主流程同时整理 plan.md 要点，准备第 3 步。
4. 主流程审核并处理预检结果；结果未返回或有 `FAIL` 时不得建容器：
   - `FAIL`：停下，告诉用户原因和建议（比如端口被占就建议换端口），改 deploy.toml 后从第 2 步重来。
   - `WARN`：在第 3 步汇总里列出。

### 3. 部署方案确认（硬性关口 1）

拿到预检结果后，把 `plan.md` 的要点和预检的 WARN 一起交给用户，至少包括：

- 拓扑表（plan.md 第 1 节，展开后每个 member 一行：实例、rank、节点、卡、端口，另有未使用节点）；顶部若有场景 WARN 一并说明；对外地址和模型名；
- 卡检查结果；要拉镜像的节点、要创建/复用/启动的容器及完整 `docker run`；
- 与文档默认值的差异（env/args）摘要。完整参数在第 5 步单独确认。

用户明确说可以执行后才进入第 4 步。用户要改任何东西：改 deploy.toml → 重新渲染 → 重新卡检查和预检 → 重新确认。

### 4. 镜像（后台，与第 5 步并行）

部署方案确认后，由执行方对每个节点执行，独立节点可并行；主流程审核拉取和工具检查结果：

- 跑 `image.check_cmd`。存在且 `pull_policy != always` → 跳过。
- 需要拉：跑 `image.pull_cmd`（后台）后**立即进入第 5 步**，不要干等。之后在参数确认的间隙每 30 秒左右 `tail -n 3 <pull_log>` 并跑 `check_cmd`，直到镜像存在。`pull_log` 里出现 `Error`/`denied`/`connection refused`/`no such host`/`http: server gave HTTP response to HTTPS client` 等错误 → 停下报告，不自动反复重试、不改 Docker 配置；用户明确说明已在节点外修好 registry 配置后，可以重新跑一次 `check_cmd` 和 `pull_cmd`，继续原流程。
- `pull_policy = never` 且不存在 → 预检阶段就已是 FAIL。
- **镜像工具检查（执行方执行，主流程审核）**：哪个节点的镜像就绪，就在该节点跑 `image.tools_check_cmd`（仅允许计划中的一次性检查容器，不挂设备、不起模型服务），不用等所有节点。缺 `sglang`/`curl`/`sglang_router`，或 HICache 开启时缺 `mooncake_master` → 停下报告，不建部署容器。HICache 开启时没有 `rpc_port_ok` 的处理见第 6 步；关闭时不要求 master 工具。

### 5. 参数确认（硬性关口 2，启动前必须通过；在拉镜像期间进行）

这一关必须在本次对话中显式完成，不能用用户之前给过的零散参数、用户说“继续”、用户说“按默认端口启动”或用户同意部署方案来代替。必须先把这次渲染得到的最终参数快照交给用户，再等待明确确认。

- 确认内容至少包括：deploy.toml/params.json 路径和 diff、每个 member 的最终 env 与启动命令、卡号、端口、镜像、work_dir、mooncake、router，以及将要执行的目录创建、容器和上传操作。
- 必须明确询问“是否按这份最终参数启动？”。只有用户明确回复“确认按这份最终参数启动”或语义等价的确认，才允许执行 `launch_cmd`、部署容器的 `docker start`/`docker run` 和上传脚本；其它回复只能继续检查或等待（第 4 步已授权的镜像拉取和一次性工具检查可继续）。
- 用户要求去掉功能或修改参数时，写入 params 文件并重新渲染，把新 diff 和最终命令再次展示；不得静默采用默认值，也不得复用上一轮确认。

参数以 JSON 给用户确认和修改：

1. 第一次到这一步时，把渲染输出目录里的 `params.json` 复制到 deploy.toml 同目录，命名 `<deploy 文件名去扩展名>.params.json`（例如 `deploy.params.json`）。之后所有渲染都带 `--params <该文件>`：`python <SKILL>/scripts/render.py <deploy.toml> --params <deploy.params.json>`。
2. 在对话中直接展示 ```json 代码块，至少包含 `features`、`feature_policy` 中的功能移除清单、每个 member 的 `resolved` 完整最终 env/args，以及 `plan.json` 中 Mooncake/router 的启用状态和实际命令。内容较长时按角色或 member 拆成多个 JSON 代码块，不用省略号或摘要替代最终参数；仅说“已生成文件”、列出 `plan.md`/`params.md`/`params.json` 路径或让用户自行打开文件，不算完成参数展示。另给出参数文件路径，用户可以直接修改其中的 `features`、`env`、`args`、`instances`、`mooncake`、`router`，也可以在对话里提出修改；`derived` / `resolved` / `feature_policy` 为只读结果，不能通过修改它们覆盖配置。
3. 值的写法：`true` = 无值开关，`false` = 删除该参数，`"@cards"` = 实例总卡数；env 删掉某个键就是不设该变量。`instances` 里可以给单个实例加覆盖，但关闭的功能项仍由 feature policy 强制移除。
4. 用户在对话里提出修改：由你写进 `deploy.params.json`，然后用 `--params` 重新渲染（校验失败就把错误给用户），再把修改后的 JSON（至少是改动部分）给用户确认。用户直接改了文件：同样重新渲染、确认。
5. 用户明确确认后，参数即冻结；第 7 步上传的必须是这次确认对应的渲染结果。拓扑、端口、容器有变化的，改 deploy.toml 后还要重新做第 2 步并回到第 3 步。
6. 即使第 3 步用户已同意，也不能跳过这一步；改参数后再次启动同样要过这一关（只重启、参数未变的除外，但要告诉用户是用原参数重启）。

### 6. 容器（镜像就绪、镜像工具检查通过、参数已确认之后）

主流程审核镜像工具检查结果及参数确认后，交执行方按预检结论操作每个容器（`containers[]` 的每一项；跨节点实例每个节点各一项，在各自的 `target` 上操作）：

- 不存在：`run_cmd`。
- 存在且运行中、配置一致：复用。
- 存在、已退出、配置一致（`on_conflict = reuse`）：`start_cmd`。
- 用户确认过 recreate：`rm_cmd` 后 `run_cmd`。

先在每个节点以宿主机用户执行 `nodes[].prepare_dirs_cmd`，创建并检查 `<deploy_dir>/{scripts,logs,run,profile}` 可写；失败就停止，不能让容器里的 root 先创建这些目录。然后按预检结论创建/复用容器，再跑 `tools_check_cmd`、`write_check_cmd`（目标容器）：缺工具、容器内模型 `config.json` 不存在、work_dir 不可写 → 停下报告。主流程审核输出后才进入上传；`nodes[].upload_check_cmd` 要等第 7 步文件上传完再执行。复用的老容器以这一步为准（容器里环境可能和镜像不同）。仅在 HICache 开启时，prefill_1 若没有 `rpc_port_ok`，说明这版 mooncake_master 不支持 `--rpc_port`：告诉用户，并建议把 `mooncake.rpc_port` 改回默认 50051，同时在 `mooncake.extra_args` 里去掉相关参数，然后重新渲染；不要自己改脚本。

### 7. 上传文件

执行方对每个节点（只有 `plan.json` 的 `nodes` 里列出的节点），把 `uploads` 里的每个文件通过适配器的 `upload(target, local_path, remote_path)` 写到宿主机（work_dir 已挂进容器，宿主机写入即容器可见）。shell 文件上传时统一使用 LF 换行；不得把 PowerShell 的 CRLF 原样写入远端。上传的必须是第 5 步确认过的那次渲染结果。写完通过适配器执行 `nodes[].upload_check_cmd`，确认脚本存在、无 CRLF 且 `bash -n` 通过，再执行一次 `ls -l <deploy_dir>/scripts`。主流程复查上传结果后才允许启动。

### 8. 启动（严格按顺序，前一步就绪才走下一步）

**连续执行，不中途停下**：参数确认通过后，从计划中第一个启用的组件到 router 再到第 9 步验收一口气做完；HICache 关闭时直接从 Prefill/Decode 开始。日志里没有致命报错就直接推进下一个组件，不要每起一个组件就停下来向用户汇报状态、等用户回复或询问“是否继续”；轮询期间也不要结束当前回合。只有三种情况才停下来找用户：出现致命报错、超过 `startup_timeout_s`、全部验收完成。等待模型加载时可以简短输出进度（例如“prefill_1 加载中，已等 6 分钟，日志无报错”），但不要停。

执行方按下述阶段启动、轮询并扫描日志；主流程收到状态输出后复查并决定是否进入下一阶段，不把整套部署一次性交给子 agent 自行跑完。`diag_cmd` 只在启动异常或用户要求时执行；致命报错立即上报，由主流程安排按 `stop_order` 收尾。无需另起一个只读子 agent。

每个实例有 `members[]`（每个节点一项，单节点实例只有 rank 0）。`launch_cmd`/`stop_cmd`/`alive_cmd`/`ready_cmd`/`error_scan_cmd`/`warning_scan_cmd`/`log` 都在 member 上，在 member 自己的 `target` 上执行；`health_cmd`/`listen_check_cmd` 在实例顶层，只查 rank0。

1. **mooncake master（仅 HICache 开启时）**：`plan.mooncake.enabled=true` 时执行 `mooncake.launch_cmd`，轮询 `listen_check_cmd` 直到输出 `listening`（最多约 2 分钟）。`enabled=false` 时跳过 master、metadata、Store 检查，直接启动 workers。
2. **所有 prefill**（可同时起）：对每个实例的**每个 member**跑 `launch_cmd`。跨节点实例的所有 rank 必须同时启动（依次连续发出，不要等 rank0 就绪再起其它 rank）。
3. **所有 decode**（可同时起）：同上。
4. 轮询每个实例的每个 member，间隔 `run.poll_interval_s`，总超时 `run.startup_timeout_s`：
   - 任一 member 的 `alive_cmd` 为 `dead` → 失败；`alive_cmd` 检查 leader PID，不能只依赖负 PGID；端口监听和 ready marker 仍需分别核对；
   - 任一 member 的 `error_scan_cmd` 命中致命错误，或 member `log` 最后 100 行出现 `Traceback`、异常退出、OOM、端口占用 → 失败；
   - `multimem all-gather disabled` 伴随 `CUDA error: invalid argument` 是当前镜像已观察到的非致命告警；只要进程仍 alive 且 ready，继续启动并在验收报告中注明；
   - 实例就绪 = 以下三项**全部**满足，缺一不可，不能把“进程还活着”或“端口已监听”当作就绪：
     1. rank0 member 的 `ready_cmd` 输出 `ready`（本次日志出现 `The server is fired up and ready to roll!`）。rank>0 member 的 `ready_cmd` 为 `null`：sglang 在 node_rank≥1 上不做 warmup、不打印该行，rank>0 只看 `alive_cmd` 和错误扫描；rank0 ready 已隐含所有 rank 初始化完成。
     2. 实例顶层 `listen_check_cmd` 输出 `listening`（rank0 HTTP 端口在监听）。
     3. 实例顶层 `health_cmd` = `200`。sglang 在 warmup 完成前 `/health` 返回 503，PD 模式下 health 用假 bootstrap，不依赖 router；router 自己也只在 worker `/health` 200 后才注册它，所以这一项不会和 router 互相等。
   - **前两项都满足后才开始查 `health_cmd`**，在 ready 之前不要探 `/health`。`/health` 会在 prefill 里塞一个真请求；首次启动现编 JIT 时它可能要十几秒，`health_cmd` 的 curl 超时（30s）比服务端的 20s 长，不能自己改短，也不能用 `-m 5` 之类的手写 curl 代替：客户端先断开会让这个请求永久卡在 prefill 的 inflight 队列里，之后 `flush_cache` 一直失败（见排障）。
   - 超时（`startup_timeout_s`）前任一实例没就绪就按失败处理，**绝不能**因为等太久就先起 router 试试。
   - 每轮同时执行 `warning_scan_cmd`；它只报告已知的 `multimem all-gather disabled` 告警。该告警不计入 `error_scan_cmd` 的致命错误，但要在最终报告中注明。
5. **router**：所有 prefill/decode 实例按上面三项全部就绪后，才执行 `router.launch_cmd`。router 脚本本身还有一道门：启动前逐个等所有 worker 的 `/health` 到 200（超时 `startup_timeout_s`），没等到就在 router 日志写 `router gate: ... router not started` 并退出（`alive_cmd` 变 `dead`），不会提前对外服务。router 在等这道门时 `health_cmd` 不是 200，属正常。
   然后轮询 `router.health_cmd` 到 `200`，再轮询 `router.workers_check_cmd` 直到输出 `registered`（router health 200 只代表 router 进程活着，worker 是异步注册的；输出 `missing: <url>` 说明还没注册完，继续等，最多约 5 分钟）。两项都通过才执行第 9 步最终推理验收；在此之前**不得**向 router 发任何推理请求——worker 未注册或未就绪时进来的请求会卡在节点队列里，只能重启实例恢复。

任何一步失败：按 `stop_order` 对已启动的组件跑 `stop_cmd`（实例要停掉其全部 members），保留容器，报告。

### 9. 验收

执行方返回以下命令的原始结果；主流程复查关键状态、审核响应和日志后判定部署是否成功：

1. 每个 prefill/decode 的 `health_cmd` = `200`。
2. HICache 开启时，每个 prefill 实例的每个 member：`mooncake_ok_cmd` 结果 = 该 member 的 `mooncake_expected`（该节点上的卡数；各 member 之和 = 实例 `ranks`）。少了 → 检查 `--enable-hierarchical-cache`、`hicache-storage-backend` 是否被改掉。HICache 关闭时，`mooncake_ok_cmd`/`mooncake_expected` 为 `null`，不把没有 Mooncake store 分配判为失败。
3. HICache 开启时，`mooncake.mem_cmd` 显示的总量 = `mooncake.expected_total`（master 日志每 10 秒左右刷新一次，没出现就等一会再查）。HICache 关闭时不要求该 Store 内存分配，但仍要验收 PD transfer 和最终请求。
4. `final_curl_validate_batch_cmd`（在 prefill_1 rank0 容器内执行，通过选定适配器发到该容器所在宿主机，`timeout=660`）按 prefill 实例数发送请求；命令会解析每个 JSON 响应，并强制检查 `choices[0].message` 有非空 `content` 或 `reasoning_content`。同时检查 router 日志，确认请求按 `round_robin` 覆盖每个 prefill；`final_curl_batch_cmd` 仅用于需要保留原始响应的调试。把响应和路由覆盖摘要给用户。

5. `router.flush_cache_check_cmd`（final curl 通过后执行）：调用 router 的 `POST /flush_cache`，先打印响应体，最后一行是 `<状态码> ok` / `<状态码> failed=<...>` / `<状态码> bad`。必须是 `200 ok`（状态码 200，且响应里没有非空的 fail 类字段，如 `failed`）。否则部署不算成功，把最后一行和响应体报给用户：`failed=` 列出刷新失败的 worker 及原因（prefill `HTTP 400` 且日志是 `pending requests` 但队列为 0 的，按排障表重启该实例）；`000` = 连不上 router；`404` = 这版 router 不支持 flush_cache。

成功后汇报：对外地址（`router.url` + `/v1`）、模型名、各组件日志路径（`<deploy_dir>/logs/<名字>.log` 是指向最新日志的软链）、停服方法。

## 清理推理 cache

- 运行中的服务优先通过 `router.flush_cache_cmd` 调用 router 的 `POST /flush_cache`，由 router 广播到所有 HTTP worker；返回的 `workers_flushed` 必须等于 plan 中的后端数量。
- 该操作清理 sglang 的运行时 KV/radix cache，不删除模型文件、Docker 镜像或宿主机 page cache；有运行中或排队请求时，先等待请求结束再重试，并检查 router 和所有后端仍为健康。
- router 不支持 `/flush_cache` 时，才按实例逐个调用后端的 `POST /flush_cache`，并逐个核对 HTTP 200。

## 停服 / 重启

- 停全部：按 `plan.json` 的 `stop_order` 逐个停。`stop_order` 里的实例名要对该实例的**所有 members** 跑各自的 `stop_cmd`；`router`、`mooncake` 用顶层的 `stop_cmd`。容器保留。
- 重启单个实例：先对它的所有 members 跑 `stop_cmd`，再对所有 members 同时跑 `launch_cmd`，再按第 8 步第 4 点轮询。重启 prefill 后 router 一般不需要重启；改了实例端口必须重新渲染并重启 router。
- 改了 deploy.toml：重新渲染、卡检查和预检、第 3 步确认、第 5 步参数确认，然后停掉受影响的组件、重新上传脚本、再启动。
- 删除容器：只在用户明确要求时，对用户点名的容器跑 `rm_cmd`。

## 排障

| 现象 | 处理 |
|---|---|
| prefill 日志卡在 bootstrap / decode 一直等 prefill | 核对 router 里每个 prefill 的 bootstrap 端口与实例一致（都来自 plan，应一致）；看 `SGLANG_DISAGGREGATION_BOOTSTRAP_TIMEOUT` |
| HICache 开启时 mooncake `Mem Storage` 为 0 / prefill 日志没有 `Mooncake store setup successfully` | 检查是否缺 `--enable-hierarchical-cache`，或 `MOONCAKE_MASTER`/metadata 地址不通（在 prefill 容器里 `docker exec <容器> curl http://<p1 ip>:<metadata_port>/metadata`）。HICache 关闭时这些日志和分配不适用 |
| 服务起不来，需要看硬件 / 网络 | 对相关节点跑 `nodes[].diag_cmd`（只读）：卡占用、网卡、IB 设备与端口状态、GID、内存。按需执行，不要在部署前例行跑 |
| RDMA / rocshmem 初始化失败 | 跑 `diag_cmd`，看 GID 信息：`ROCSHMEM_IB_GID_INDEX`（默认 0）应指向 RoCE v2 + IPv4 的 index；核对 `ib_devices` 与实际 IB 设备 |
| 请求一直不返回 / 卡在 prefill 或 decode 队列 | 多半是 router 在 worker 就绪前就收到了请求。先 `stop_cmd` 停 router，按 `stop_order` 停掉并重启所有 prefill/decode，按第 8 步三项就绪判据等全部实例就绪，再起 router、等 `workers_check_cmd` = `registered` 后再发请求 |
| router 日志 `router gate: ... not 200 ... router not started` | 对应 worker 在超时内没就绪：查该实例 rank0 日志和 `error_scan_cmd`，修好后重新起 router |
| `flush_cache` 返回非 `200 ok`，某个 prefill 报 `HTTP 400`，其日志有 `Cache not flushed because there are pending requests. #queue-req: 0, #running-req: 0`、`Prefill batch` 行的 `#inflight-req` 一直不为 0 | 有 `/health` 请求在客户端超时后卡在 inflight 队列（通常是启动期用了过短的 curl 超时）。只对该实例的所有 members 跑 `stop_cmd` 再 `launch_cmd`，按第 8 步第 4 点等就绪（JIT 已有缓存，这次会快），再跑 `flush_cache_check_cmd`；router 和其它实例不用重启 |
| `Address already in use` | 端口被别的进程占了；换端口重新渲染 |
| `already running` | 该 member 的 pid 文件指向的进程还活着；先对它跑 `stop_cmd` |
| 跨节点实例卡在 `dist-init` / 等其它 rank | 确认所有 member 都已启动；核对 `--dist-init-addr`（rank0 节点 ip + `dist_init_port`）在各节点上可达、端口未被占 |
| 镜像拉取报 HTTPS 相关错误 | 节点 docker 没把仓库配成 insecure registry；交给用户处理 |
| 以上都对不上、服务仍起不来，或用户要求参考官方方案 | 查阅 Hygon 推理 cookbook：https://github.com/HYGON-AI/inference-cookbook-das ，按模型名、sglang/DTK 版本、报错关键字找对应的部署说明和环境变量，对比当前 env/args。只在这两种情况下查，平时不用；据此改参数时同样要改 deploy.toml、重新渲染并让用户确认 |
