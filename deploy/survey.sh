#!/bin/bash
# 只读勘察：不改磁盘、不改配置、不装东西。输出 KEY=VALUE 行和 "## 小节"，供 node-setup skill 判断。
# 用法：把本文件内容作为 exec 的 command 原样传给节点（bash -c）。
export LC_ALL=C
. /etc/os-release 2>/dev/null
echo "OS=${PRETTY_NAME:-unknown}"
echo "KERNEL=$(uname -r)"
echo "USER=$(id -un) GROUPS=$(id -nG | tr ' ' ',')"
if sudo -n true 2>/dev/null; then echo "SUDO=nopasswd"; else echo "SUDO=no"; fi
echo "ROOT_SRC=$(findmnt -no SOURCE /)"

echo "## lsblk"
lsblk -o NAME,SIZE,TYPE,FSTYPE,PTTYPE,UUID,MOUNTPOINT,MODEL 2>&1

echo "## fstab"
grep -vE '^\s*(#|$)' /etc/fstab

echo "## mounts"
df -hT -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null

echo "## blank disk detection"
# 空白盘 = 整盘无子分区、无文件系统、无分区表、未挂载。其余有数据迹象的盘只报告，不当作可格式化。
for d in $(lsblk -dnpo NAME,TYPE | awk '$2=="disk"{print $1}'); do
  kids=$(lsblk -nr "$d" | wc -l)
  fs=$(lsblk -dno FSTYPE "$d")
  pt=$(lsblk -dno PTTYPE "$d")
  mp=$(lsblk -dno MOUNTPOINT "$d")
  sz=$(lsblk -dno SIZE "$d" | xargs); md=$(lsblk -dno MODEL "$d" | xargs)
  if [ "$kids" -le 1 ] && [ -z "$fs" ] && [ -z "$pt" ] && [ -z "$mp" ]; then
    echo "BLANK_DISK=$d size=$sz model=$md"
  else
    echo "USED_DISK=$d size=$sz model=$md children=$((kids-1)) fstype=${fs:--} pttype=${pt:--}"
  fi
done

echo "## docker"
if command -v docker >/dev/null 2>&1; then
  echo "DOCKER_BIN=$(docker --version 2>&1)"
  echo "DOCKER_ACTIVE=$(systemctl is-active docker 2>&1) ENABLED=$(systemctl is-enabled docker 2>&1)"
  echo "DOCKER_ROOT=$(sudo -n docker info -f '{{.DockerRootDir}}' 2>&1 | head -1)"
  echo "DOCKER_CONTAINERS=$(sudo -n docker ps -aq 2>/dev/null | wc -l) IMAGES=$(sudo -n docker images -q 2>/dev/null | wc -l)"
  echo "DAEMON_JSON:"; cat /etc/docker/daemon.json 2>&1
  echo "DOCKER_GROUP_MEMBER=$(id -nG | tr ' ' '\n' | grep -cx docker)"
else
  echo "DOCKER_BIN=none"
fi
echo "DOCKER_APT_SRC=$(ls /etc/apt/sources.list.d 2>/dev/null | grep -i docker | tr '\n' ' ')"
if timeout 8 curl -fsSI -o /dev/null https://download.docker.com/linux/ubuntu/gpg 2>/dev/null; then
  echo "NET_DOCKER_COM=ok"; else echo "NET_DOCKER_COM=unreachable"; fi

echo "## driver (Hygon DCU)"
# 0b40 = Co-processor 类，1d94 = Hygon；不能只按厂商数，PCI bridge 等也是 1d94
echo "HCU_PCI_COUNT=$(lspci -d 1d94::0b40 2>/dev/null | wc -l)"
echo "HYHAL_DIR=$([ -d /opt/hyhal ] && echo yes || echo no)"
echo "HYCU_MODULE=$(lsmod | grep -c '^hycu')"
if [ -f /opt/hyhal/env.sh ]; then
  . /opt/hyhal/env.sh 2>/dev/null
  echo "HYSMI_CARDS=$(timeout 30 hy-smi 2>/dev/null | grep -cE '^[0-9]+ ')"
else
  echo "HYSMI_CARDS=n/a"
fi
echo "BUILD_TOOLS: cmake=$(command -v cmake >/dev/null && echo yes || echo no) plocate=$(command -v locate >/dev/null && echo yes || echo no) gcc=$(command -v gcc >/dev/null && echo yes || echo no)"
echo "HEADERS=$([ -d /lib/modules/$(uname -r)/build ] && echo yes || echo no)"
