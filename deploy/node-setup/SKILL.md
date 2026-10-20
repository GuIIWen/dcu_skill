---
name: node-setup
description: 用 remote-linux MCP 给全新的 GPU/DCU 节点做初始化：勘察磁盘/docker/驱动现状、把新盘格式化挂载到 /data0 /data1、安装 docker 并改 data-root、安装海光 DCU 驱动。当用户要初始化新节点、挂盘、装 docker、装 DCU 驱动或检查节点是否就绪时使用。每一步先勘察再动手，破坏性操作先让用户确认。
---

# 节点初始化 (node-setup)

通过 remote-linux MCP 的 `exec` 在目标节点上跑本目录的脚本。脚本应以 UTF-8、LF 换行传输；不要让 PowerShell 的文本管道把 CRLF 或本地编码带到远端。脚本是文本，把内容作为 `command` 传给节点即可，也可以先上传到 `/tmp`。

## 原则

- 先勘察，后动手。永远先跑 `survey.sh`，根据结果决定做哪几步，不凭文档里的值假设。
- 文档和历史里的盘名、挂载点、data-root、内网仓库、.run 文件名都只是默认参考。以 survey 结果和用户的话为准，拿不准就问。
- 脚本都是幂等的，已完成的步骤会自己跳过，重复跑安全。
- 分区、格式化、改 fstab、装驱动（会烧 VBIOS）、重启都不可逆或影响大。动手前把要碰的盘/路径列给用户，等明确确认。
- 多节点时每台独立勘察、独立确认，不要因为一台的结果套用到另一台。
- 远端默认工作目录不存在时，先用 `cwd=/` 或 `cwd=/tmp` 创建目标工作目录，再切换到用户确认的目录；不要为了绕过目录不存在而擅自修改 remote-linux config。

## 流程

1. 勘察：把 `survey.sh` 内容作为 command 发给节点（cwd 用 `/`）。看输出里的：
   - `BLANK_DISK=` 可格式化的空白盘；`USED_DISK=` 有数据迹象的盘，只报告，不碰。
   - `SUDO=`：不是 nopasswd 就停下来问用户。
   - `DOCKER_*`、`DAEMON_JSON`：已装的 docker 版本、data-root、已有容器数。
   - `NET_DOCKER_COM`：unreachable 时 docker 安装可能失败，提前告知用户。这个探测只试一次，腾讯云节点到 download.docker.com 时通时不通（03 已装好 docker 时也报过 unreachable），所以只当提示，安装脚本自带重试。
   - `HCU_PCI_COUNT`、`HYSMI_CARDS`、`HYCU_MODULE`：驱动是否已装并在线。
2. 挂盘（有 BLANK_DISK 且用户确认时）：
   `PAIRS="nvme0n1:/data0 nvme1n1:/data1" bash setup_mount.sh`
   PAIRS 由 survey 的空白盘和用户的意图决定，没有默认值。脚本拒绝碰有任何签名、分区或挂载的盘，备份 fstab，用 UUID + `nofail`，目录权限 777。
3. 装 docker：
   `DATA_ROOT=/data0/docker bash install_docker.sh`
   DATA_ROOT 必须是已挂载数据盘上的路径，先和用户确认。默认写入内网仓库 `42.228.13.241:5000`；如需覆盖，传 `REGISTRY="host:port"`，传 `REGISTRY=""` 时不新增（已有配置仍保留）。daemon.json 是合并写入（保留已有的其他键，改前备份为 `daemon.json.bak.<时间>`），并固定加 `"features": {"containerd-snapshotter": false}`：Docker 29 默认用 containerd 镜像存储，此时 data-root 管不到镜像层，镜像会写进系统盘的 `/var/lib/containerd`，系统盘很快撑满（03/04 实测一个 43GB 镜像占 40G）。已装且版本 ≥24 时只更新 daemon.json 并重启 docker，所以节点上有运行中的容器时要先提醒用户重启 docker 的影响。
   如果 containerd 存储里已有镜像，切换会让它们“看不见”，脚本会 ABORT（退出码 12）并列出镜像。此时和用户确认后：先 `docker rmi` 掉（或 save 备份），再 `FORCE_SWITCH=1` 重跑，最后重新 pull。装完用 `docker info -f '{{.Driver}}'` 确认是 `overlay2`，不是 `overlayfs`。安装有重试，网络差时可能要几分钟，timeout 给足（600s 以上）。
4. 获取并安装驱动：
   - 先向用户确认 GPU/OS、驱动版本和来源。优先让用户提供下载 URL、内部路径或 `.run` 文件；不能根据历史节点、文件名或本地记录猜版本并直接下载。
   - 用户没有提供来源、但明确要求检索时，才检索官方或用户指定的来源；先把候选版本、URL、文件大小和 SHA256 告知用户，得到确认后再下载。下载和安装是两个独立步骤，下载完成后再次校验哈希，未经明确授权不安装。
   - 安装使用：`RUN_FILE=/abs/path/rock-xxx.run bash install_driver.sh`。先 `ls` 确认文件存在和哈希正确。脚本后台运行并立即返回，之后用 `tail -n 5 <目录>/install_drv.log` 轮询，出现 `EXIT_CODE_DRIVER=0` 即表示安装脚本成功；安装含 VBIOS 烧录，期间不要中断或并发执行其他重操作。
5. 收尾：
   - 日志末尾提示 `Reboot ... to complete installation` 时，需要重启节点才算完成。重启要先问用户，不要自行 reboot。
   - 安装完成但重启前，`hycu` 未加载或 `hy-smi` 报错、部分卡 offline 都可能是预期状态。重启必须先问用户，不要自行 reboot。重启后重新跑 `survey.sh`，先 `. /opt/hyhal/env.sh`，确认 `HYSMI_CARDS` 等于 `HCU_PCI_COUNT`。
   - 临时文件：脚本已清理 `/tmp/get-docker.sh`。驱动目录里的日志和 .run 留给用户决定。

## 已知注意点

- 数据盘分区名由 `setup_mount.sh` 按设备类型处理：NVMe/mmc 使用 `p1`，sd/vd 使用 `1`。仍以 survey 输出为准，不要套用历史盘名。
- 驱动日志里 `apt ... plymouth.service` 重启失败和 `/usr/bin/bash ... is damage` 等 Warning 在已装成功的节点上出现过，不影响结果，以最终 `Install successful` 和 `EXIT_CODE_DRIVER` 为准。
- survey 的“空白盘”判定是保守的：宁可漏报也不误报。盘上哪怕有旧签名也不会进 BLANK_DISK，需要用户明确说要清掉才另行处理，本 skill 不提供擦盘功能。

## 局限

- `survey.sh` 的空白盘分支（真有空白盘时的输出）没有在真实空盘上验证过，只验证了“没有空白盘”的节点输出；`setup_mount.sh` 和 `install_docker.sh` 的 daemon.json 生成逻辑改成参数化后，没有重新在节点上跑过，只做了语法检查和 JSON 渲染检查。首次在新节点用时留意输出。
- `install_docker.sh` 的新版（合并写 daemon.json、关 containerd-snapshotter、FORCE_SWITCH 保护）只做了语法检查和 JSON 合并的本地模拟；同样的配置改动已在 03/04 上手动做过并验证 `Driver=overlay2`、`DockerRootDir=/data0/docker`，但脚本本身没在节点上整体重跑，ABORT 分支也没触发过。
- 仅覆盖 Ubuntu + 海光 DCU + docker CE。其他系统/加速卡需要另行适配。
