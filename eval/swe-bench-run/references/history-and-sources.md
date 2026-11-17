# 版本与来源

历史复现候选 commit：`66f92766bba642462d4bbe5479e83f91f9211862`。它用于核对旧实验，不是新任务的默认版本。

复现时使用对应运行的 manifest、配置、镜像指纹和原始结果；缺少材料时明确证据缺口。旧版网关补丁的行为和文件身份见 [历史钩子](litellm-legacy-hook.md)。

## 权威来源

命令与协议以本次所选 commit 和已安装版本为准；在线 main 可能变化。

- [SWE-bench Pro 官方仓库](https://github.com/scaleapi/SWE-bench_Pro-os)：区分 Pro V2 / v1。
- [V2 协议与运行入口](https://github.com/scaleapi/SWE-bench_Pro-os/blob/main/v2/README.md)：locked、复评分和题集。
- [历史固定 commit 的 host mini 实现](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tooling/host_mini_swe.py)：host 与 sandbox 的边界。
- [同 commit 的 locked Codex](https://github.com/scaleapi/SWE-bench_Pro-os/blob/66f92766bba642462d4bbe5479e83f91f9211862/v2/tooling/locked_codex.py)：安装和超时实现应按版本读源码。
- [Harbor agents](https://docs.harborframework.com/agents/pre-integrated-agents)、[Base URL](https://docs.harborframework.com/agents/base-url-management)、[jobs](https://docs.harborframework.com/jobs/run-a-job)、[network policies](https://docs.harborframework.com/tasks/network-policies)：CLI 和环境能力可能与旧版不同。
- [Docker daemon proxy](https://docs.docker.com/engine/daemon/proxy/)：daemon 与 CLI/容器代理分层。
