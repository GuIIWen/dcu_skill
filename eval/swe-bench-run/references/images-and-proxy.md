# 镜像准备与网络路径

## 生成清单

`prepare_images.py` 从 task.toml 的 `environment.docker_image` 或单一 Dockerfile FROM 读取准确镜像名，拒绝空题集、重复 ID、冲突和无法明确解析的引用。它只生成清单或 inspect 本地镜像，不下载。

```bash
python3 "$SKILL_DIR/scripts/prepare_images.py" \
  --tasks-root "$REPO/v2/tasks" --ids "$IDS" \
  --output "$RUN_DIR/images.json" --images-out "$RUN_DIR/images.txt"
```

保留任务配置中的 tag，不从短 ID 手拼。官方 V2 镜像通常为 `ghcr.io/scaleapi/swe-bench_pro-v2:<完整 instance_id>`，以选定 checkout 为准。

## 指定镜像站并批量下载

`pull_images.py` 读取上述 JSON。`--registry-map SOURCE=DEST[/PREFIX]` 只替换 registry，保留 repository、tag；可重复传入多个映射。不带映射时使用官方地址。示例站点须替换为已验证的真实地址：

```bash
# 原地址：ghcr.io/scaleapi/swe-bench_pro-v2:<tag>
# 下载地址：mirror.example/ghcr.io/scaleapi/swe-bench_pro-v2:<tag>
REGISTRY_MAP='ghcr.io=mirror.example/ghcr.io'

# 默认预览，不调用 Docker
python3 "$SKILL_DIR/scripts/pull_images.py" \
  --manifest "$RUN_DIR/images.json" --output "$RUN_DIR/image-pull.json" \
  --registry-map "$REGISTRY_MAP"

# 核对映射后执行
python3 "$SKILL_DIR/scripts/pull_images.py" \
  --manifest "$RUN_DIR/images.json" --output "$RUN_DIR/image-pull.json" \
  --registry-map "$REGISTRY_MAP" --concurrency 2 --retries 2 --timeout 1800 --execute
```

若站点直接将官方 registry 映射为自己的域名，使用 `ghcr.io=mirror.example`，不要添加多余路径。Docker Hub 的映射源为 `docker.io`，短名 `ubuntu:24.04` 在映射时展开为 `library/ubuntu:24.04`。映射不能包含 `https://`、凭据或查询参数。

执行时先检查 Docker daemon，再逐项处理：

- 官方名字已存在且平台匹配：跳过；默认平台 `linux/amd64`，可用 `--platform` 指定。
- 缺失或平台不匹配：从映射后的地址 pull，检查平台；用其 image ID 恢复官方 tag，再 inspect 核对 ID。
- 已有匹配平台的镜像站副本：直接检查并恢复官方 tag，避免中断后的重复下载。
- pull 失败或超时：有限重试。`--retries 2` 表示最多 3 次尝试；`--retry-delay` 默认 2 秒，每次等待不超过 30 秒。权限/daemon/inspect 错误不会当作镜像缺失。

原命令再次执行即可补拉缺失项；恢复依据是本次 Docker inspect，不信任旧报告的成功标记。已有 tag 不主动刷新；需要不同版本时先核对任务引用和 digest。`--timeout` 限制每次 pull 客户端等待时间，daemon 下载可能仍在继续。

`--output` 持续保存逐镜像状态、来源/官方名字、image ID、RepoDigests、平台和每次尝试的日志路径；再次运行时旧报告保存到同名 `.history` 目录。退出码：`0` 为预览成功或全部就绪，`1` 为输入/全局错误，`2` 为部分镜像失败。

脚本沿用 Docker 当前环境，或用 `--docker-context NAME` 明确指定。不会修改 daemon 代理或重启 Docker。检查报告中的 context 和 daemon ID，确保操作目标正确。

retag 的 ID 校验只证明本地别名一致，不证明镜像站内容与官方相同；使用站点前独立核对映射与目标 digest。Docker tag 无法恢复 `仓库@digest` 别名，因此脚本拒绝对 digest 引用改写 registry；此类引用使用官方地址配合 daemon 代理。

## 选择网络出口

优先测试当前已有出口：官方 registry 直连或现有代理、候选镜像站、可下载的中转机。下载路径与模型网关分别记录。

对实际目标镜像检查 `/v2/`、鉴权 challenge、manifest 和 blob。401 往往是正常鉴权 challenge；读取 `WWW-Authenticate` 的 realm/service/scope，并兼容 `token` / `access_token`，不要猜 token 路径。404 需区分域名、路径映射和 tag 缺失。

使用同一路径做 20–30 秒受限 blob 下载或代表镜像 pull，记录吞吐和错误；关键对比复测一次，最终以目标 Docker daemon 的实际 pull 为准。节点延迟低不代表大文件下载快。

Clash/Mihomo 等控制器的地址、端口、鉴权和 selector 组从实际配置发现。保存原选择，避免切换影响共享下载；临时选择在无依赖任务时恢复。

## 分清代理作用范围

| 层次 | 配置要点 |
|---|---|
| shell/下载器 | HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 只影响支持它们的进程；curl 测速不能代表 Docker |
| Docker daemon | pull 由 daemon 发起；shell 的 HTTPS_PROXY 通常不能改变已有 daemon。活跃作业期间可用中转导入，避免重启 |
| 任务容器 | 127.0.0.1 是容器自己；从实际网络查宿主机网关，服务需监听可达地址 |
| agent 求解阶段 | locked 仅放行模型端点，不用通用代理绕过隔离；setup/verifier 的依赖访问另测 |

内网模型/网关加入该层支持的 NO_PROXY/no_proxy。本地代理不会自动转发到远端 Docker；需已有隧道、远端可达代理或下载中转机。

## 离线导入

在可下载节点用 `docker save -o archive.tar "$IMAGE"` 导出规范 tag，记录 image ID、RepoDigests 和归档 SHA256。经已有 SSH/对象存储传输，目标校验归档后 `docker load -i archive.tar`。归档位置和访问方式由本次环境填写，凭据或带签名链接不进入共享日志。

导入后逐项对账：

```bash
python3 "$SKILL_DIR/scripts/prepare_images.py" \
  --tasks-root "$REPO/v2/tasks" --ids "$IDS" \
  --output "$RUN_DIR/images-checked.json" --inspect
```

检查平台和传输前 image ID。save/load 可能不保留 RepoDigests，可用归档哈希和来源 manifest 补足证据。最后启动代表容器，检查工作目录、工具和 agent 安装路径。

依据：[Docker pull](https://docs.docker.com/reference/cli/docker/image/pull/)、[tag](https://docs.docker.com/reference/cli/docker/image/tag/)、[daemon 代理](https://docs.docker.com/engine/daemon/proxy/)。
