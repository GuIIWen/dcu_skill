#!/bin/bash
# 后台安装海光 DCU 驱动 (.run)。装驱动要几分钟到十几分钟（含 VBIOS 烧录），所以用 nohup 放后台，立刻返回，之后轮询日志。
# 用法：RUN_FILE=/data0/songj/rock-6.3.31-V1.5.6.run bash install_driver.sh
#   RUN_FILE  必填，驱动 .run 的绝对路径（来自用户给的文件，不猜）
#   WORK_DIR  可选，日志和中间脚本放哪，默认 .run 所在目录
# 幂等：同一个 .run 已有日志且含 EXIT_CODE_DRIVER=0 则不重复装；已有同名进程在跑则不重复起。
# 轮询：tail -n 3 <WORK_DIR>/install_drv.log ；完成标志是日志里出现 EXIT_CODE_DRIVER=<n>。
set -e
RUN=${RUN_FILE:?set RUN_FILE to the absolute path of the driver .run}
[ -f "$RUN" ] || { echo "ABORT: $RUN not found"; exit 1; }
WD=${WORK_DIR:-$(dirname "$RUN")}
LOG=$WD/install_drv.log

if [ -f "$LOG" ] && grep -q '^EXIT_CODE_DRIVER=0' "$LOG"; then
  echo "SKIP: driver already installed ($LOG shows EXIT_CODE_DRIVER=0). Reboot may still be pending."; exit 0
fi
if pgrep -f "$WD/install_drv.sh" >/dev/null; then
  echo "SKIP: install already running"; exit 0
fi

chmod +x "$RUN"
cat > "$WD/install_drv.sh" <<EOF
cd "$WD"
set -x
sudo DEBIAN_FRONTEND=noninteractive apt install -y cmake
cmake --version
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y plocate
sudo "$RUN" -A
echo "EXIT_CODE_DRIVER=\$?"
EOF
nohup bash "$WD/install_drv.sh" > "$LOG" 2>&1 &
sleep 2
echo "started pid $!, log: $LOG"
