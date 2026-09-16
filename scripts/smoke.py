"""冒烟测试脚本：验证环境、vcan、以及各模块能否正常 import。

用法（WSL/Linux）：
    python3 scripts/smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can  # noqa: E402

from src.bus import resolve_config, BusManager, setup_vcan  # noqa: E402
from src.dbc_codec import DbcCodec  # noqa: E402
from src.can_monitor import BusMonitor  # noqa: E402
from src.isotp import IsoTpConfig, IsoTpConnection  # noqa: E402
from src.uds import UdsClient, UdsServer, SessionType  # noqa: E402


def main() -> int:
    print("=" * 62)
    print("1. 环境")
    print("=" * 62)
    print(f"python-can {can.__version__}")
    print(f"platform   {sys.platform}")

    print()
    print("=" * 62)
    print("2. 总线")
    print("=" * 62)
    created = setup_vcan("vcan0")
    print(f"vcan0 available: {created}")
    config = resolve_config()
    print(f"resolved       : {config.describe()}")

    print()
    print("=" * 62)
    print("3. DBC")
    print("=" * 62)
    codec = DbcCodec()
    print(f"messages: {codec.message_names}")
    frame = codec.encode(
        "EngineData",
        {
            "EngineSpeed": 2400.0,
            "VehicleSpeed": 72.5,
            "CoolantTemp": 88.0,
            "ThrottlePosition": 34.0,
            "EngineState": 2,
            "MilStatus": 0,
        },
    )
    print(f"encoded : id=0x{frame.arbitration_id:X} data={frame.data.hex(' ')}")
    decoded = codec.decode(frame)
    print(f"decoded : {decoded}")

    print()
    print("=" * 62)
    print("4. ISO-TP + UDS 端到端")
    print("=" * 62)
    with BusManager(config) as tester_bus, BusManager(config) as ecu_bus:
        tester_conn = IsoTpConnection(
            tester_bus, IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, timeout=2.0)
        )
        ecu_conn = IsoTpConnection(ecu_bus, IsoTpConfig(tx_id=0x7E8, rx_id=0x7E0, timeout=2.0))

        server = UdsServer(ecu_conn)
        server.start_thread()
        try:
            client = UdsClient(tester_conn, timeout=2.0)
            resp = client.diagnostic_session_control(SessionType.EXTENDED)
            print(f"0x10 extended  -> {resp.hex(' ')}")

            vin = client.read_data_by_identifier(0xF190)
            print(f"0x22 F190 VIN  -> {vin.decode('ascii')}")

            client.unlock(level=1)
            print("0x27 security  -> unlocked")

            dtcs = client.read_dtc_information()
            print(f"0x19 DTCs      -> {[hex(c) for c, _ in dtcs]}")

            # 长报文触发多帧：读 6 个 DID
            long_resp = client.request(bytes([0x22, 0xF1, 0x87, 0xF1, 0x8C, 0xF1, 0x95, 0xF1, 0x91]))
            print(f"multi-frame    -> {len(long_resp)} bytes: {long_resp.hex(' ')}")

            print(f"ISO-TP stats   -> {tester_conn.stats}")
        finally:
            server.stop()

    print()
    print("=" * 62)
    print("冒烟测试全部通过")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
