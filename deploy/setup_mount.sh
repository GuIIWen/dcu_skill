#!/bin/bash
# 自动挂载两块新盘到 /data0 和 /data1（ext4, GPT, 自动 fstab）。
# 拒绝碰有任何签名、分区或挂载的盘。幂等：已挂载且 fstab 匹配则跳过。
# 用法：PAIRS="nvme0n1:/data0 nvme1n1:/data1" bash setup_mount.sh（盘名和挂载点取自 survey 并经用户确认）
set -e
# PAIRS 形如 "nvme0n1:/data0 nvme1n1:/data1"，必须由调用方根据 survey 结果和用户确认给出，不设默认值
pairs="${PAIRS:?set PAIRS, e.g. PAIRS=\"nvme0n1:/data0 nvme1n1:/data1\"}"
devs=""; mnts=""
for p in $pairs; do devs="$devs /dev/${p%%:*}"; mnts="$mnts ${p##*:}"; done

partition_for() {
  case "$1" in
    /dev/nvme*|/dev/mmcblk*) printf '%sp1\n' "$1" ;;
    *) printf '%s1\n' "$1" ;;
  esac
}

# 幂等判定：已挂载且 fstab 有匹配条目则跳过
already_done=1
for p in $pairs; do
  dev=/dev/${p%%:*}; mnt=${p##*:}; part=$(partition_for "$dev")
  mp=$(findmnt -no TARGET "$part" 2>/dev/null || true)
  fe=$(grep -cE "^UUID=.* $mnt " /etc/fstab || true)
  if [ "$mp" != "$mnt" ] || [ "$fe" = 0 ]; then already_done=0; fi
done
if [ $already_done = 1 ]; then
  echo "SKIP: all disks already mounted and in fstab"; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT $devs; exit 0
fi

# 安全检查：拒绝碰非空盘
for p in $pairs; do
  d=/dev/${p%%:*}
  if [ -n "$(sudo wipefs -n $d)" ] || [ -n "$(lsblk -nro MOUNTPOINT $d | tr -d '\n')" ] || [ "$(lsblk -nr $d | wc -l)" != 1 ]; then
    echo "ABORT: $d is not blank"; exit 1
  fi
done

sudo cp /etc/fstab /etc/fstab.bak.$(date +%Y%m%d%H%M%S)
for p in $pairs; do
  dev=/dev/${p%%:*}; mnt=${p##*:}; part=$(partition_for "$dev")
  echo "=== $dev -> $mnt"
  sudo mkdir -p $mnt
  sudo parted -s $dev mklabel gpt
  sudo parted -s $dev mkpart primary ext4 0% 100%
  sudo partprobe $dev; sleep 2
  sudo mkfs.ext4 -q $part
  sudo mount $part $mnt
  uuid=$(sudo blkid -s UUID -o value $part)
  echo "UUID=$uuid $mnt ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
  sudo chown root:root $mnt; sudo chmod 777 $mnt
done
sudo systemctl daemon-reload
echo "=== result"; lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINT $devs; df -hT $mnts
sudo findmnt --verify 2>&1 | tail -2
