#!/usr/bin/env bash
# 在 Linux（含 WSL2 / GitHub Actions runner）上创建并启用 vcan 接口。
#
# 用法：
#     sudo ./scripts/setup_vcan.sh              # 默认建 vcan0
#     sudo ./scripts/setup_vcan.sh vcan1        # 指定接口名
#
# 说明：
#     vcan 是 Linux 内核提供的虚拟 CAN 接口，收发行为与真实 CAN 控制器一致
#     （同样的 SocketCAN 套接字接口、同样的帧格式），但不需要任何硬件。
#     唯一区别是没有真实电气层，因此不会产生位错误和总线仲裁冲突。

set -euo pipefail

CHANNEL="${1:-vcan0}"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "setup_vcan.sh: 仅在 Linux 上可用（当前 $(uname -s)）。" >&2
    echo "Windows/macOS 请使用 python-can 的 virtual 接口，见 README。" >&2
    exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
    echo "setup_vcan.sh: 需要 root 权限，请用 sudo 运行。" >&2
    exit 1
fi

# 模块可能已内置进内核，modprobe 失败不算错误
modprobe vcan 2>/dev/null || true

if ip link show "$CHANNEL" &>/dev/null; then
    echo "接口 $CHANNEL 已存在，重新启用。"
    ip link set up "$CHANNEL"
else
    ip link add dev "$CHANNEL" type vcan
    ip link set up "$CHANNEL"
    echo "已创建并启用 $CHANNEL。"
fi

ip -details link show "$CHANNEL"
