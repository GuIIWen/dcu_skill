#!/bin/bash
# 自动安装 docker CE，重试 5 次，接受网络波动。配置 data-root 和内网 registry。
# 幂等：已装且版本 ≥24 则跳过安装，只更新 daemon.json、重启并验证 data-root 和 insecure-registries。
# 用法：DATA_ROOT=<用户确认的路径> [REGISTRY=host:port ...空格分隔] bash install_docker.sh
# 默认使用公司的内网仓库；REGISTRY 可覆盖，显式设为空字符串时不新增（已有配置仍保留）。
set -e
DR=${DATA_ROOT:?set DATA_ROOT, e.g. /data0/docker (confirmed with user)}
DEFAULT_REGISTRY="42.228.13.241:5000"
REG=${REGISTRY-$DEFAULT_REGISTRY}

# 幂等判定：docker 已存在且版本 ≥24
if command -v docker >/dev/null 2>&1; then
  v=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+' | head -1)
  if [ -n "$v" ] && awk "BEGIN{exit !($v>=24)}"; then
    echo "docker $v already installed, skipping install step"
    skip_install=1
  fi
fi

if [ -z "$skip_install" ]; then
  cd /tmp
  ok=""
  for i in 1 2 3 4 5; do
    rm -f get-docker.sh
    if curl -fsSL get.docker.com -o get-docker.sh && [ -s get-docker.sh ]; then ok=1; break; fi
    echo "download try $i failed"; sleep 3
  done
  [ -n "$ok" ] || { echo "DOWNLOAD FAILED: get.docker.com unreachable"; exit 10; }
  echo "script downloaded: $(wc -c < get-docker.sh) bytes"
  inst=""
  for i in 1 2 3 4 5; do
    if sudo bash get-docker.sh >/tmp/docker-install.log 2>&1; then inst=$i; break; fi
    echo "install try $i failed: $(tail -1 /tmp/docker-install.log)"; sleep 5
  done
  [ -n "$inst" ] || { echo "INSTALL FAILED after 5 tries. last lines:"; tail -6 /tmp/docker-install.log; exit 11; }
  echo "docker installed on try $inst: $(sudo docker --version)"
  sudo usermod -aG docker ${USER:-ubuntu}
  rm -f /tmp/get-docker.sh
fi

# Docker 29 全新安装默认启用 containerd 镜像存储，此时 data-root 管不到镜像层（层在 /var/lib/containerd，占系统盘）。
# 所以 daemon.json 里关掉 containerd-snapshotter，让镜像回到 data-root。
# 已在 containerd 存储里有镜像时，关掉会让这些镜像"消失"（数据还在 /var/lib/containerd），需要用户确认后设 FORCE_SWITCH=1，
# 并在切换前自行 docker rmi（或 save 备份）、切换后重新 pull。
if command -v docker >/dev/null 2>&1 && sudo docker info -f '{{.DriverStatus}}' 2>/dev/null | grep -q containerd.snapshotter \
   && [ -n "$(sudo docker images -q 2>/dev/null)" ] && [ "${FORCE_SWITCH:-0}" != 1 ]; then
  echo "ABORT: containerd image store holds images; switching hides them. Remove/save them first, then rerun with FORCE_SWITCH=1"
  sudo docker images --format '  {{.Repository}}:{{.Tag}} {{.Size}}'
  exit 12
fi

sudo mkdir -p /etc/docker
[ -f /etc/docker/daemon.json ] && sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)
# 合并写入：保留已有的其他键，只设 data-root、features 和默认/指定 registry
sudo DR="$DR" REG="$REG" python3 - <<'PY'
import json, os
p = '/etc/docker/daemon.json'
try:
    d = json.load(open(p))
except (FileNotFoundError, ValueError):
    d = {}
d['data-root'] = os.environ['DR']
d.setdefault('features', {})['containerd-snapshotter'] = False
regs = os.environ['REG'].split()
if regs:
    d['insecure-registries'] = regs
json.dump(d, open(p, 'w'), indent=2)
open(p, 'a').write('\n')
PY
python3 -m json.tool /etc/docker/daemon.json >/dev/null && echo "daemon.json valid"
sudo systemctl daemon-reload
sudo systemctl restart docker
sudo systemctl enable docker >/dev/null 2>&1 || true
echo "=== verify"
sudo systemctl is-active docker
sudo docker info 2>/dev/null | grep -E "Docker Root Dir|Server Version|Storage Driver|Insecure Registries" -A2 || true
# Storage Driver 应为 overlay2；若仍是 overlayfs（containerd snapshotter），说明 features 没生效，镜像会占系统盘
sudo docker info -f '{{.Driver}}' | grep -qx overlay2 || echo "WARN: storage driver is not overlay2, images will NOT use data-root"
cat /etc/docker/daemon.json
ls -d "$DR" 2>&1 | head -1
df -h "$DR" 2>&1 | tail -1
