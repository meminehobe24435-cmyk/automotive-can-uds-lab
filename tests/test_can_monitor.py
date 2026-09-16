"""总线监控测试：报文周期、总线负载、丢帧检测。

这些是台架测试报告里最常见的三类结论，测试用例要能自动判定：
    - 周期是否符合设计值（±20% 容差）
    - 总线负载率是多少
    - 该来的报文有没有来
"""

from __future__ import annotations

import time

import can
import pytest

from src.can_monitor import (
    STANDARD_FRAME_OVERHEAD_BITS,
    BusMonitor,
    CycleTimeViolation,
    frame_bit_length,
)


class TestFrameBitLength:
    def test_zero_dlc(self):
        # 47 位开销 + 0 数据位 = 47，乘位填充系数
        assert frame_bit_length(0, stuffing=False) == 47
        assert frame_bit_length(0, stuffing=False) == STANDARD_FRAME_OVERHEAD_BITS

    def test_dlc_8(self):
        # 47 + 64 = 111 位
        assert frame_bit_length(8, stuffing=False) == 111

    def test_stuffing_adds_20_percent(self):
        assert frame_bit_length(8, stuffing=True) == pytest.approx(111 * 1.2)

    @pytest.mark.parametrize("dlc", range(0, 9))
    def test_monotonic_in_dlc(self, dlc):
        if dlc == 0:
            return
        assert frame_bit_length(dlc) > frame_bit_length(dlc - 1)


class TestMonitorRecording:
    def test_counts_frames_by_id(self):
        monitor = BusMonitor.__new__(BusMonitor)
        monitor.__init__(bus=None)  # type: ignore[arg-type]
        for _ in range(5):
            monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        for _ in range(3):
            monitor.observe(can.Message(arbitration_id=0x200, data=bytes(8)))
        assert monitor.stats[0x100].count == 5
        assert monitor.stats[0x200].count == 3
        assert monitor.total_frames == 8

    def test_error_frames_counted_separately(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8), is_error_frame=True))
        assert monitor.error_frames == 1
        assert monitor.total_frames == 0

    def test_dlc_recorded(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        monitor.observe(can.Message(arbitration_id=0x300, data=bytes(4)))
        assert monitor.stats[0x300].dlc == 4


class TestCycleTime:
    def _feed(self, monitor: BusMonitor, frame_id: int, period: float, count: int, dlc: int = 8):
        """按指定周期喂帧，用真实 sleep 制造时间间隔。"""
        for _ in range(count):
            monitor.observe(can.Message(arbitration_id=frame_id, data=bytes(dlc)))
            time.sleep(period)

    def test_average_cycle_measured(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        self._feed(monitor, 0x100, period=0.01, count=6)
        stats = monitor.stats[0x100]
        assert stats.cycle_avg == pytest.approx(0.010, abs=0.006)
        assert len(stats.cycles) == 5  # 6 帧产生 5 个间隔

    def test_jitter_computed(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        time.sleep(0.005)
        monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        time.sleep(0.020)
        monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        stats = monitor.stats[0x100]
        assert stats.cycle_jitter is not None
        assert stats.cycle_jitter > 0.010

    def test_no_violation_within_tolerance(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        self._feed(monitor, 0x100, period=0.01, count=5)
        assert monitor.check_cycle_times({0x100: 0.010}, tolerance=0.60) == []

    def test_violation_detected_when_too_slow(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        self._feed(monitor, 0x100, period=0.03, count=5)
        violations = monitor.check_cycle_times({0x100: 0.010}, tolerance=0.20)
        assert len(violations) == 1
        assert isinstance(violations[0], CycleTimeViolation)
        assert violations[0].frame_id == 0x100

    def test_not_enough_samples_skips_check(self):
        """样本太少时不判定，避免误报。"""
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        self._feed(monitor, 0x100, period=0.03, count=2)
        assert monitor.check_cycle_times({0x100: 0.010}, tolerance=0.20) == []


class TestCoverage:
    def test_missing_messages(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        missing = monitor.missing_messages({0x100, 0x200, 0x300})
        assert missing == {0x200, 0x300}

    def test_empty_summary_for_single_frame(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        assert "single frame" in monitor.stats[0x100].summary()

    def test_summary_contains_cycle(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        self._feed_helper(monitor)
        assert "cycle avg" in monitor.stats[0x100].summary()

    @staticmethod
    def _feed_helper(monitor: BusMonitor) -> None:
        for _ in range(3):
            monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
            time.sleep(0.005)


class TestBusLoad:
    def test_load_formula(self):
        """手工验算：100 帧 8 字节，1 秒窗口，500 kbit/s。"""
        monitor = BusMonitor(bus=None, bitrate=500_000)  # type: ignore[arg-type]
        monitor._start = 0.0
        monitor._end = 1.0
        for _ in range(100):
            monitor.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        monitor._start = 0.0
        monitor._end = 1.0

        expected_bits = 100 * frame_bit_length(8)  # 100 * 133.2 = 13320
        expected_load = expected_bits / 500_000
        assert monitor.bus_load() == pytest.approx(expected_load, rel=1e-6)
        assert monitor.bus_load() == pytest.approx(0.02664, rel=1e-3)

    def test_load_increases_with_more_frames(self):
        low = BusMonitor(bus=None)  # type: ignore[arg-type]
        high = BusMonitor(bus=None)  # type: ignore[arg-type]
        for _ in range(10):
            low.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        for _ in range(100):
            high.observe(can.Message(arbitration_id=0x100, data=bytes(8)))
        # 两者 duration 都极小，用固定窗口比较更稳
        low._start, low._end = 0.0, 1.0
        high._start, high._end = 0.0, 1.0
        assert high.bus_load() > low.bus_load()

    def test_zero_duration_does_not_crash(self):
        monitor = BusMonitor(bus=None)  # type: ignore[arg-type]
        assert monitor.bus_load() == 0.0


class TestLiveCapture:
    """在 virtual 总线上做一次真实抓包。"""

    def test_capture_cycle_and_load(self, channel):
        sender = can.Bus(interface="virtual", channel=channel)
        listener = can.Bus(interface="virtual", channel=channel)
        try:
            import threading

            stop = threading.Event()

            def emit():
                while not stop.is_set():
                    sender.send(
                        can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False)
                    )
                    time.sleep(0.01)

            thread = threading.Thread(target=emit, daemon=True)
            thread.start()

            monitor = BusMonitor(listener, bitrate=500_000).capture(duration=0.4)
            stop.set()
            thread.join(timeout=1.0)

            assert monitor.total_frames >= 10
            assert 0x100 in monitor.stats
            stats = monitor.stats[0x100]
            assert stats.cycle_avg == pytest.approx(0.010, abs=0.008)
            assert monitor.bus_load() > 0
            assert monitor.report().count("0x100") >= 1
        finally:
            sender.shutdown()
            listener.shutdown()
