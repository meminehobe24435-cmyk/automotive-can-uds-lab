"""ISO 15765-2 传输层测试。

覆盖重点：
    - 四种帧的 PCI 编码（这里曾经藏过一个 bug，见 test_pci_encoding_* 注释）
    - STmin 编解码
    - 单帧/多帧端到端重组
    - 异常路径：序号错乱、超长报文、超时、流控溢出
"""

from __future__ import annotations

import time

import pytest

from src.isotp import (
    IsoTpConfig,
    IsoTpConnection,
    IsoTpOverflow,
    IsoTpSequenceError,
    IsoTpTimeout,
    FlowStatus,
    FrameType,
    decode_stmin,
    encode_stmin,
)


# --------------------------------------------------------------------------- #
# PCI 编码：回归测试
# --------------------------------------------------------------------------- #


class TestPciEncoding:
    """PCI 字节编码。

    历史 bug：FrameType 的枚举值是"帧类型序号"（0..3），
    曾把它当成 PCI 字节（0x00/0x10/0x20/0x30）直接用，
    导致首帧被编码成 0x01 而不是 0x10。因为单帧的枚举值恰好是 0，
    单帧路径是对的，只有多帧出错 —— 这类 bug 只有真跑多帧才暴露。
    """

    def test_pci_base_values(self):
        assert FrameType.SINGLE.pci_base == 0x00
        assert FrameType.FIRST.pci_base == 0x10
        assert FrameType.CONSECUTIVE.pci_base == 0x20
        assert FrameType.FLOW_CONTROL.pci_base == 0x30

    def test_enum_value_is_nibble_for_decoding(self):
        """枚举值必须等于 PCI 字节右移 4 位，解码比较才能直接用。"""
        for frame_type in FrameType:
            assert frame_type == (frame_type.pci_base >> 4)

    def test_single_frame_pci_is_length(self):
        assert FrameType.SINGLE.pci_base | 7 == 0x07
        assert FrameType.SINGLE.pci_base | 1 == 0x01

    def test_first_frame_pci_is_0x10_plus_length_high_nibble(self):
        total = 20
        pci = FrameType.FIRST.pci_base | ((total >> 8) & 0x0F)
        assert pci == 0x10  # 曾经的 bug 会得到 0x01

    def test_first_frame_pci_for_long_message(self):
        total = 0x123  # 291 字节
        pci = FrameType.FIRST.pci_base | ((total >> 8) & 0x0F)
        assert pci == 0x11

    def test_consecutive_frame_pci(self):
        assert FrameType.CONSECUTIVE.pci_base | 1 == 0x21
        assert FrameType.CONSECUTIVE.pci_base | 15 == 0x2F

    def test_flow_control_pci(self):
        assert FrameType.FLOW_CONTROL.pci_base | FlowStatus.CONTINUE_TO_SEND == 0x30
        assert FrameType.FLOW_CONTROL.pci_base | FlowStatus.WAIT == 0x31
        assert FrameType.FLOW_CONTROL.pci_base | FlowStatus.OVERFLOW == 0x32

    def test_decode_frame_type_from_pci_byte(self):
        assert 0x0F >> 4 == FrameType.SINGLE
        assert 0x10 >> 4 == FrameType.FIRST
        assert 0x2A >> 4 == FrameType.CONSECUTIVE
        assert 0x30 >> 4 == FrameType.FLOW_CONTROL


# --------------------------------------------------------------------------- #
# STmin
# --------------------------------------------------------------------------- #


class TestStmin:
    @pytest.mark.parametrize(
        "byte,seconds",
        [
            (0x00, 0.000),
            (0x01, 0.001),
            (0x0A, 0.010),
            (0x7F, 0.127),
            (0xF1, 0.0001),
            (0xF5, 0.0005),
            (0xF9, 0.0009),
        ],
    )
    def test_decode(self, byte, seconds):
        assert decode_stmin(byte) == pytest.approx(seconds, abs=1e-9)

    def test_reserved_values_treated_as_zero(self):
        # 0x80..0xF0 是保留值，按 0 处理
        assert decode_stmin(0x80) == 0.0
        assert decode_stmin(0xF0) == 0.0

    @pytest.mark.parametrize("seconds", [0.0, 0.001, 0.005, 0.010, 0.100, 0.127])
    def test_roundtrip(self, seconds):
        assert decode_stmin(encode_stmin(seconds)) == pytest.approx(seconds, abs=1e-3)

    def test_clamped_at_max(self):
        assert decode_stmin(encode_stmin(5.0)) == pytest.approx(0.127)


# --------------------------------------------------------------------------- #
# 端到端收发
# --------------------------------------------------------------------------- #


class TestRoundTrip:
    def test_single_frame(self, connections):
        tester_conn, ecu_conn = connections
        payload = bytes([0x22, 0xF1, 0x90])
        tester_conn.send(payload)
        assert ecu_conn.recv(timeout=1.0) == payload

    def test_single_frame_max_payload_is_7(self, connections):
        tester_conn, ecu_conn = connections
        payload = bytes(range(1, 8))  # 7 字节
        tester_conn.send(payload)
        assert ecu_conn.recv(timeout=1.0) == payload

    @pytest.mark.parametrize("length", [8, 9, 20, 49, 100, 291, 1000])
    def test_multi_frame_lengths(self, connections, receive, length):
        """8 字节起必须走多帧，覆盖 1~3 位长度字段的各种边界。

        接收侧必须跑在后台线程 —— 见 tests/conftest.py 的 BackgroundReceiver 说明。
        """
        tester_conn, ecu_conn = connections
        payload = bytes((i * 7 + 3) & 0xFF for i in range(length))

        rx = receive(ecu_conn)
        tester_conn.send(payload)
        assert rx.get() == payload

    def test_bidirectional(self, connections, receive):
        """同一对连接上双向收发都要正确。"""
        tester_conn, ecu_conn = connections
        up = bytes(range(50))
        down = bytes(range(100, 180))

        rx_ecu = receive(ecu_conn)
        tester_conn.send(up)
        assert rx_ecu.get() == up

        rx_tester = receive(tester_conn)
        ecu_conn.send(down)
        assert rx_tester.get() == down

    def test_sequence_number_wraps_at_16(self, connections, receive):
        """超过 15 个连续帧时序号要回绕到 0，不能断。"""
        tester_conn, ecu_conn = connections
        # 每帧 6 字节，150 字节需要 25 个连续帧，必然跨过序号回绕点
        payload = bytes((i * 3) & 0xFF for i in range(150))

        rx = receive(ecu_conn, timeout=5.0)
        tester_conn.send(payload)
        assert rx.get() == payload

    def test_block_size_forces_multiple_flow_controls(self, connections, receive):
        """BlockSize=4 时，每 4 帧必须重新等一次流控。"""
        tester_conn, ecu_conn = connections
        tester_conn.cfg.block_size = 4
        ecu_conn.cfg.block_size = 4

        payload = bytes(range(60))  # 1 首帧 + 9 连续帧

        rx = receive(ecu_conn, timeout=5.0)
        tester_conn.send(payload)
        assert rx.get() == payload

        # 9 个连续帧 / 每块 4 帧 -> 首帧后 1 次 + 中途至少 2 次
        assert ecu_conn.stats["flow_control_sent"] >= 3

    def test_stats_accounting(self, connections, receive):
        tester_conn, ecu_conn = connections
        rx = receive(ecu_conn)
        tester_conn.send(bytes(20))
        rx.get()

        assert tester_conn.stats["tx_messages"] == 1
        assert tester_conn.stats["tx_frames"] >= 4  # 首帧 + 3 连续帧
        assert ecu_conn.stats["rx_messages"] == 1
        assert ecu_conn.stats["flow_control_sent"] >= 1


# --------------------------------------------------------------------------- #
# 异常路径
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_oversized_payload_rejected_on_send(self, connections):
        tester_conn, _ = connections
        with pytest.raises(IsoTpOverflow):
            tester_conn.send(bytes(5000))  # 超过 12 位长度字段的 4095 上限

    def test_timeout_when_no_response(self, connections):
        tester_conn, _ecu_conn = connections
        tester_conn.cfg.timeout = 0.2
        with pytest.raises(IsoTpTimeout):
            tester_conn.recv(timeout=0.2)
        assert tester_conn.stats["timeouts"] >= 1

    def test_flow_control_overflow(self, connections, channel, receive):
        """接收方缓冲不足时应回 OVFLW（0x32），发送方必须立刻中止。

        这里额外挂一个嗅探总线抓原始帧 —— 发送方会把 OVFLW 消费掉，
        所以只能从旁路确认它真的上过线。
        """
        import can

        tester_conn, ecu_conn = connections
        ecu_conn.cfg.max_payload = 10  # 接收方只肯收 10 字节

        sniffer = can.Bus(interface="virtual", channel=channel)
        try:
            rx = receive(ecu_conn)

            # 发送方收到 OVFLW 后应立即中止并报错
            with pytest.raises(IsoTpOverflow):
                tester_conn.send(bytes(50))

            # 接收侧同样应因超出本端上限而报错
            with pytest.raises(IsoTpOverflow):
                rx.get()

            seen = []
            while True:
                msg = sniffer.recv(timeout=0.2)
                if msg is None:
                    break
                seen.append(msg.data[0])
            assert (FrameType.FLOW_CONTROL.pci_base | FlowStatus.OVERFLOW) in seen
        finally:
            sniffer.shutdown()

    def test_sequence_error_detected(self, connections, bus_pair):
        """连续帧序号不连续必须被发现。

        这个用例由测试自己驱动全部发送、不做并发，因此结果完全确定：
        接收方读完首帧后回一次流控（不会阻塞），随后读到序号错误的连续帧。
        """
        import can

        _tester_conn, ecu_conn = connections
        tester_bus, _ = bus_pair
        ecu_conn.cfg.timeout = 0.5

        # 首帧：声明总长 30 (0x01E)，携带前 6 字节
        tester_bus.send(
            can.Message(
                arbitration_id=0x7E0,
                data=bytes([0x10, 0x1E]) + bytes(range(6)),
                is_extended_id=False,
            )
        )
        # 期望序号是 1，故意发序号 7
        tester_bus.send(
            can.Message(
                arbitration_id=0x7E0,
                data=bytes([0x27]) + bytes(6),
                is_extended_id=False,
            )
        )

        with pytest.raises(IsoTpSequenceError):
            ecu_conn.recv(timeout=1.0)

    def test_consecutive_frame_without_first_is_error(self, connections, bus_pair):
        """收到连续帧但从未收到首帧 —— 必须报错，不能拼出半个报文。"""
        import can

        _tester_conn, ecu_conn = connections
        tester_bus, _ = bus_pair

        # 从 tester 侧的总线发，才能被 ecu_bus 收到
        # （virtual 总线与真实 CAN 一致：发送方收不到自己发的帧）
        tester_bus.send(
            can.Message(
                arbitration_id=0x7E0,
                data=bytes([0x21]) + bytes(6),
                is_extended_id=False,
            )
        )
        with pytest.raises(IsoTpSequenceError):
            ecu_conn.recv(timeout=1.0)

    def test_flow_control_frames_are_skipped_on_recv(self, connections, bus_pair):
        """流控帧不是报文起点，收到时必须跳过而不是当成请求。

        这是实跑时抓到的真实 bug：首帧 PCI 编码错误时，接收端把首帧的长度
        低字节当成了 SID，报出"unexpected response SID 0x14"。协议层必须
        能区分"帧"和"报文起点"。
        """
        import can

        _tester_conn, ecu_conn = connections
        tester_bus, _ = bus_pair

        # 先灌一个流控帧，再灌一个正常的单帧请求
        tester_bus.send(
            can.Message(
                arbitration_id=0x7E0, data=bytes([0x30, 0, 0]), is_extended_id=False
            )
        )
        tester_bus.send(
            can.Message(
                arbitration_id=0x7E0,
                data=bytes([0x02, 0x10, 0x03]),
                is_extended_id=False,
            )
        )

        assert ecu_conn.recv(timeout=1.0) == bytes([0x10, 0x03])


# --------------------------------------------------------------------------- #
# 真实 SocketCAN
# --------------------------------------------------------------------------- #


@pytest.mark.socketcan
class TestRealSocketCAN:
    """在真实 Linux SocketCAN(vcan) 上验证同一套协议逻辑。"""

    def test_multi_frame_on_vcan(self, socketcan_config, receive):
        from src.bus import create_bus

        tester_bus = create_bus(socketcan_config)
        ecu_bus = create_bus(socketcan_config)
        try:
            tester_conn = IsoTpConnection(
                tester_bus, IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8, timeout=2.0)
            )
            ecu_conn = IsoTpConnection(
                ecu_bus, IsoTpConfig(tx_id=0x7E8, rx_id=0x7E0, timeout=2.0)
            )

            payload = bytes((i * 11) & 0xFF for i in range(64))
            rx_ecu = receive(ecu_conn, timeout=4.0)
            tester_conn.send(payload)
            assert rx_ecu.get() == payload

            # 反向：ECU 回一条长报文给 tester
            response = bytes((i * 5 + 1) & 0xFF for i in range(40))
            rx_tester = receive(tester_conn, timeout=4.0)
            ecu_conn.send(response)
            assert rx_tester.get() == response
        finally:
            tester_bus.shutdown()
            ecu_bus.shutdown()
