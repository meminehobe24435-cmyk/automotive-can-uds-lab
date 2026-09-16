#!/usr/bin/env bash
# 一键：装依赖 -> 建 vcan -> 跑全部测试。
#
# 用法：
#     sudo ./scripts/run_tests.sh              # 建 vcan 跑真实 SocketCAN 测试
#     ./scripts/run_tests.sh --virtual         # 不建 vcan，用 python-can 虚拟总线
#
# 在 GitHub Actions 里用 sudo 调用，效果等同于本地。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

USE_VIRTUAL=0
if [[ "${1:-}" == "--virtual" ]]; then
    USE_VIRTUAL=1
fi

echo "==> 安装依赖"
python3 -m pip install -q -r requirements.txt

if [[ "$USE_VIRTUAL" -eq 0 ]]; then
    if [[ "$(uname -s)" == "Linux" ]] && [[ "$(id -u)" -eq 0 ]]; then
        echo "==> 创建 vcan 接口"
        bash "$REPO_ROOT/scripts/setup_vcan.sh" vcan0
        export CAN_INTERFACE=socketcan
        export CAN_CHANNEL=vcan0
    else
        echo "==> 非 root 或非 Linux，回退到 virtual 总线"
        export CAN_INTERFACE=virtual
    fi
else
    echo "==> 强制使用 virtual 总线"
    export CAN_INTERFACE=virtual
fi

echo "==> 运行测试（interface=${CAN_INTERFACE:-auto}）"
python3 -m pytest tests -v --tb=short
