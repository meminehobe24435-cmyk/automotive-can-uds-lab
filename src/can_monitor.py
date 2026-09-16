"""CAN 总线监控与统计：报文周期、总线负载、信号漂移。

这三项就是测试岗的日常：
    - 报文周期测试：EngineData 应该每 10 ms 来一帧，实测抖动多少？
    - 总线负载率：当前负载占带宽百分比，超过 60% 就要警惕
    - 信号监控：某信号是否长期不变 / 越界 / 跳变

总线负载率算法（经典 CAN，500 kbit/s）：
    一帧的位数 = 帧头开销 + 数据位 + 填充位
    标准帧开销 47 位（SOF 1 + 仲裁 12 + 控制 6 + CRC 16 + ACK 2 + EOF 7 + IFS 3）
    数据位 = 8 * DLC
    位填充：连续 5 个同极性位后插 1 位，最坏情况约 +20%

    负载率 = Σ(每帧位数) / (波特率 * 统计时长)
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import can

# 标准帧固定开销（不含数据段与填充）
STANDARD_FRAME_OVERHEAD_BITS = 47
# 位填充最坏情况系数（ISO 11898-1 规定最多每 4 位插 1 位，约 1.2 倍）
BIT_STUFFING_FACTOR = 1.2


def frame_bit_length(dlc: int, stuffing: bool = True) -> float:
    """估算一帧占用的位数。"""
    bits = STANDARD_FRAME_OVERHEAD_BITS + 8 * dlc
    return bits * BIT_STUFFING_FACTOR if stuffing else float(bits)


@dataclass
class FrameStats:
    """单个 CAN ID 的统计结果。"""

    arbitration_id: int
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    dlc: int = 0
    cycles: list[float] = field(default_factory=list)

    @property
    def cycle_min(self) -> Optional[float]:
        return min(self.cycles) if self.cycles else None

    @property
    def cycle_max(self) -> Optional[float]:
        return max(self.cycles) if self.cycles else None

    @property
    def cycle_avg(self) -> Optional[float]:
        return sum(self.cycles) / len(self.cycles) if self.cycles else None

    @property
    def cycle_jitter(self) -> Optional[float]:
        """周期抖动：最大周期 - 最小周期。"""
        if len(self.cycles) < 2:
            return None
        return max(self.cycles) - min(self.cycles)

    def summary(self) -> str:
        if not self.cycles:
            return f"0x{self.arbitration_id:03X}  count={self.count} (single frame)"
        return (
            f"0x{self.arbitration_id:03X}  count={self.count:4d}  "
            f"cycle avg={self.cycle_avg * 1000:7.2f} ms  "
            f"min={self.cycle_min * 1000:7.2f}  max={self.cycle_max * 1000:7.2f}  "
            f"jitter={self.cycle_jitter * 1000:6.2f} ms"
        )


class CycleTimeViolation(Exception):
    """报文周期超出允许范围。"""

    def __init__(self, frame_id: int, measured: float, expected: float, tolerance: float):
        self.frame_id = frame_id
        self.measured = measured
        self.expected = expected
        self.tolerance = tolerance
        super().__init__(
            f"0x{frame_id:03X} cycle {measured * 1000:.2f} ms deviates from "
            f"expected {expected * 1000:.2f} ms by more than "
            f"{tolerance * 100:.0f}%"
        )


class BusMonitor:
    """被动监听总线，统计报文周期与总线负载。"""

    def __init__(self, bus: can.BusABC, bitrate: int = 500_000):
        self.bus = bus
        self.bitrate = bitrate
        self.stats: dict[int, FrameStats] = {}
        self.total_bits = 0.0
        self.total_frames = 0
        self.error_frames = 0
        self._start: Optional[float] = None
        self._end: Optional[float] = None
        self.samples: list[can.Message] = []

    # ------------------------------------------------------------------ #

    def capture(self, duration: float, keep_samples: bool = False) -> "BusMonitor":
        """监听总线 duration 秒。"""
        self._start = time.monotonic()
        deadline = self._start + duration
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = self.bus.recv(timeout=min(remaining, 0.2))
            if msg is None:
                continue
            self.observe(msg, keep_sample=keep_samples)
        self._end = time.monotonic()
        return self

    def observe(self, msg: can.Message, keep_sample: bool = False) -> None:
        """记录一帧。"""
        now = time.monotonic()
        if self._start is None:
            self._start = now
        self._end = now

        if msg.is_error_frame:
            self.error_frames += 1
            return

        self.total_frames += 1
        self.total_bits += frame_bit_length(len(msg.data))

        entry = self.stats.get(msg.arbitration_id)
        if entry is None:
            entry = FrameStats(
                arbitration_id=msg.arbitration_id, first_seen=now, dlc=len(msg.data)
            )
            self.stats[msg.arbitration_id] = entry
        else:
            entry.cycles.append(now - entry.last_seen)
        entry.count += 1
        entry.last_seen = now

        if keep_sample:
            self.samples.append(msg)

    # ------------------------------------------------------------------ #

    @property
    def duration(self) -> float:
        if self._start is None or self._end is None:
            return 0.0
        return max(self._end - self._start, 1e-9)

    def bus_load(self) -> float:
        """总线负载率，0.0 ~ 1.0+。"""
        window = self.duration
        if window <= 0:
            return 0.0
        return self.total_bits / (self.bitrate * window)

    def check_cycle_times(
        self,
        expected: dict[int, float],
        tolerance: float = 0.20,
        min_samples: int = 3,
    ) -> list[CycleTimeViolation]:
        """校验各报文的周期是否符合预期。

        expected: {CAN ID: 期望周期(秒)}
        tolerance: 允许偏差比例，默认 ±20%
        """
        violations: list[CycleTimeViolation] = []
        for frame_id, expected_cycle in expected.items():
            entry = self.stats.get(frame_id)
            if entry is None or len(entry.cycles) < min_samples:
                continue
            if entry.cycle_avg is None:
                continue
            deviation = abs(entry.cycle_avg - expected_cycle) / expected_cycle
            if deviation > tolerance:
                violations.append(
                    CycleTimeViolation(frame_id, entry.cycle_avg, expected_cycle, tolerance)
                )
        return violations

    def missing_messages(self, expected_ids: set[int]) -> set[int]:
        """期望出现但一帧都没收到的报文 ID。"""
        return {fid for fid in expected_ids if fid not in self.stats}

    def report(self) -> str:
        """生成人读的统计报告。"""
        lines = [
            f"capture window : {self.duration * 1000:.1f} ms",
            f"total frames   : {self.total_frames}",
            f"error frames   : {self.error_frames}",
            f"bus load       : {self.bus_load() * 100:.2f} %",
            "",
        ]
        for frame_id in sorted(self.stats):
            lines.append("  " + self.stats[frame_id].summary())
        return "\n".join(lines)
