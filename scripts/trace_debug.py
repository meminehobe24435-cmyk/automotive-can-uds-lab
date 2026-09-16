"""临时调试脚本：在 vcan0 上挂一个嗅探 socket，打印全部原始 CAN 帧。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import can  # noqa: E402

from src.bus import resolve_config, BusManager  # noqa: E402
from src.isotp import IsoTpConfig, IsoTpConnection  # noqa: E402
from src.uds import UdsClient, UdsServer, SessionType  # noqa: E402

FRAMES: list[tuple[float, str, int, str]] = []
STOP = threading.Event()
T0 = time.monotonic()


def sniffer(bus: can.BusABC) -> None:
    while not STOP.is_set():
        msg = bus.recv(timeout=0.1)
        if msg is None:
            continue
        FRAMES.append(
            (time.monotonic() - T0, "RX", msg.arbitration_id, msg.data.hex(" "))
        )


def tag(mark: str) -> None:
    FRAMES.append((time.monotonic() - T0, "--", 0, f"===== {mark} ====="))


def main() -> int:
    config = resolve_config()
    print(f"bus: {config.describe()}")

    with BusManager(config) as sniff_bus, BusManager(config) as tester_bus, BusManager(config) as ecu_bus:
        th = threading.Thread(target=sniffer, args=(sniff_bus,), daemon=True)
        th.start()

        tester_conn = IsoTpConnection(
            tester_bus, IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, timeout=2.0)
        )
        ecu_conn = IsoTpConnection(ecu_bus, IsoTpConfig(tx_id=0x7E8, rx_id=0x7E0, timeout=2.0))

        server = UdsServer(ecu_conn)
        server.start_thread()

        client = UdsClient(tester_conn, timeout=2.0)

        tag("request 0x10 03")
        print("0x10 ->", client.diagnostic_session_control(SessionType.EXTENDED).hex(" "))
        time.sleep(0.2)

        tag("request 0x22 F190 (VIN, 20-byte response -> multi-frame)")
        try:
            vin = client.read_data_by_identifier(0xF190)
            print("VIN  ->", vin)
        except Exception as exc:
            print("FAILED:", type(exc).__name__, exc)

        time.sleep(0.3)
        STOP.set()
        th.join(timeout=1.0)
        server.stop()

    print()
    print("=" * 70)
    print("raw frame trace")
    print("=" * 70)
    for ts, direction, arb, data in FRAMES:
        if direction == "--":
            print(f"\n{data}")
        else:
            print(f"  t+{ts * 1000:8.2f} ms  0x{arb:03X}  {data}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
