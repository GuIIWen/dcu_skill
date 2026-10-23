#!/usr/bin/env bash
# 在容器内以 root 运行：安装 polyglot benchmark 需要的非 Python 工具链（go / rust / java / cmake），并预置 gradle 8.7。
# 基础镜像 ubuntu 22.04（docker.1ms.run/library/buildpack-deps:jammy）。幂等，重复运行安全。
#
# 为什么要换源、预置 gradle：腾讯云节点到官方 apt 源约 46 KB/s，到 services.gradle.org 约 12 KB/s，
# 装完整个工具链要几个小时；换清华源后约 2 MB/s 以上，gradle 从腾讯镜像下载约 20 MB/s。
set -euo pipefail

UBUNTU_MIRROR="${UBUNTU_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn/ubuntu}"
GRADLE_ZIP_URL="${GRADLE_ZIP_URL:-https://mirrors.cloud.tencent.com/gradle/gradle-8.7-bin.zip}"
# 习题里 gradle/wrapper/gradle-wrapper.properties 写的地址；wrapper 用它的 md5 决定缓存目录名
WRAPPER_URL="https://services.gradle.org/distributions/gradle-8.7-bin.zip"

if grep -qE 'archive\.ubuntu\.com|security\.ubuntu\.com' /etc/apt/sources.list 2>/dev/null; then
  cp -n /etc/apt/sources.list /etc/apt/sources.list.bak_orig
  sed -i "s#http://archive.ubuntu.com/ubuntu#${UBUNTU_MIRROR}#g; s#http://security.ubuntu.com/ubuntu#${UBUNTU_MIRROR}#g" /etc/apt/sources.list
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 golang-go cargo rustc default-jdk-headless cmake unzip curl ca-certificates

HASH=$(python3 - "$WRAPPER_URL" <<'PY'
import hashlib, sys
n = int(hashlib.md5(sys.argv[1].encode()).hexdigest(), 16)
a = "0123456789abcdefghijklmnopqrstuvwxyz"
s = ""
while n:
    n, r = divmod(n, 36)
    s = a[r] + s
print(s)
PY
)
D="/root/.gradle/wrapper/dists/gradle-8.7-bin/${HASH}"
if [ ! -f "${D}/gradle-8.7-bin.zip.ok" ]; then
  mkdir -p "${D}"
  curl -fL --retry 3 -o "${D}/gradle-8.7-bin.zip" "${GRADLE_ZIP_URL}"
  (cd "${D}" && unzip -q -o gradle-8.7-bin.zip && touch gradle-8.7-bin.zip.ok)
fi

echo "---- installed"
go version
rustc --version
cargo --version
java -version 2>&1 | head -1
cmake --version | head -1
echo "gradle dist dir: ${D}"
ls "${D}"
