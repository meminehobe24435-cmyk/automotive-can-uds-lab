"""ISO 15765-2 (ISO-TP) 传输层实现（经典 CAN，8 字节净荷）。

为什么手写而不用 can-isotp？
    因为这是车载诊断面试第一必考点。手写过一遍，你才能回答：
        - 为什么单帧最多只能装 7 字节？
        - 首帧的长度字段为什么是 12 位？
        - 流控帧里 BlockSize 和 STmin 分别控制什么？
        - 连续帧的序号为什么只到 15 就回绕？
    这些用自己的代码解释一遍，比背文档牢得多。

帧格式（ISO 15765-2:2016，经典 CAN 寻址）：

    单帧 SF   : [0x0L] [data...]                        L = 净荷长度 1..7
    首帧 FF   : [0x1H][0xLL] [data...]                  HLL = 总长度 12 位 (8..4095)
    连续帧 CF : [0x2N] [data...]                        N = 序号 0..15，循环
    流控帧 FC : [0x3F][BS][STmin]                       F = 流状态 0=CTS 1=Wait 2=OVFLW

一次完整多帧发送的时序：

    Tester                          ECU
      |--- FF (总长 100) ----------->|
      |<-- FC (CTS, BS=8, STmin=5) --|
      |--- CF#1 ------------------->|
      |--- CF#2 ------------------->|
      |        ... (共 8 帧)         |
      |<-- FC (CTS, BS=8, STmin=5) --|   ← 每发满 BlockSize 帧就要重新等一次 FC
      |--- CF#9 ------------------->|
      ...
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

import can

# 经典 CAN 单帧净荷上限：8 字节数据区 - 1 字节 PCI = 7 字节
CLASSIC_CAN_DLC = 8
SINGLE_FRAME_MAX_PAYLOAD = CLASSIC_CAN_DLC - 1  # 7
MULTI_FRAME_CHUNK = CLASSIC_CAN_DLC - 2  # 6：首帧/连续帧要留 2 字节 / 1 字节 PCI


class IsoTpError(Exception):
    """ISO-TP 协议层异常基类。"""


class IsoTpTimeout(IsoTpError):
    """等待帧超时。"""


class IsoTpOverflow(IsoTpError):
    """接收方缓冲区不足，或报文长度超出协议上限。"""


class IsoTpSequenceError(IsoTpError):
    """连续帧序号不连续。"""


class FrameType(IntEnum):
    """PCI 高 4 位标识的帧类型。

    枚举值是**帧类型序号**（0..3），等于 PCI 字节右移 4 位的结果，
    因此解码时可以直接与 ``msg.data[0] >> 4`` 比较。
    编码时要用 :attr:`pci_base` 拿到 PCI 字节的高 4 位。

    单帧的 PCI 高 4 位恰好是 0，所以 ``SINGLE.pci_base | length`` 和
    ``SINGLE | length`` 结果相同 —— 这正是当年这个 bug 能藏住的原因。
    """

    SINGLE = 0x0
    FIRST = 0x1
    CONSECUTIVE = 0x2
    FLOW_CONTROL = 0x3

    @property
    def pci_base(self) -> int:
        """PCI 字节的高 4 位掩码，用于编码帧。"""
        return self.value << 4


class FlowStatus(IntEnum):
    """流控帧的流状态（FC 帧第 1 个数据字节低 4 位）。"""

    CONTINUE_TO_SEND = 0x0  # CTS：允许对方继续发
    WAIT = 0x1  # WT：让对方等，稍后再发一个 FC
    OVERFLOW = 0x2  # OVFLW：接收方缓冲不足，中止


def decode_stmin(value: int) -> float:
    """把 STmin 字节解码成秒。

    ISO 15765-2 规定：
        0x00..0x7F -> 0..127 毫秒
        0xF1..0xF9 -> 100..900 微秒
        其余为保留值，按 0 处理
    """
    if 0x00 <= value <= 0x7F:
        return value / 1000.0
    if 0xF1 <= value <= 0xF9:
        return (value - 0xF0) / 10000.0
    return 0.0


def encode_stmin(seconds: float) -> int:
    """把秒编码成 STmin 字节，优先选毫秒档。"""
    if seconds <= 0:
        return 0x00
    if seconds < 0.127:
        return max(0, min(0x7F, int(round(seconds * 1000))))
    return 0x7F  # 上限 127 ms


@dataclass
class IsoTpConfig:
    """一条 ISO-TP 通道的参数。

    tx_id / rx_id 是"本端视角"的发送与接收 CAN ID。
    Tester 侧通常是 tx=0x7E0 / rx=0x7E8；ECU 侧正好相反。
    """

    tx_id: int
    rx_id: int
    block_size: int = 0  # 0 = 不再要求流控，一次发完
    stmin: float = 0.0  # 秒
    timeout: float = 1.0  # 秒
    padding: Optional[int] = None  # 填充字节，如 0xCC / 0x00 / None=不填充
    is_extended_id: bool = False
    max_payload: int = 4095  # 12 位长度字段的协议上限


class IsoTpConnection:
    """在一对 CAN ID 上收发完整 UDS 报文。

    用法：
        conn = IsoTpConnection(bus, IsoTpConfig(tx_id=0x7E0, rx_id=0x7E8))
        conn.send(bytes([0x22, 0xF1, 0x90]))
        resp = conn.recv()
    """

    def __init__(self, bus: can.BusABC, config: IsoTpConfig):
        self.bus = bus
        self.cfg = config
        # 运行时统计，供测试断言与可观测性使用
        self.stats = {
            "tx_frames": 0,
            "rx_frames": 0,
            "tx_messages": 0,
            "rx_messages": 0,
            "flow_control_sent": 0,
            "timeouts": 0,
        }

    # ------------------------------------------------------------------ #
    # 底层：单帧收发
    # ------------------------------------------------------------------ #

    def _send_frame(self, arbitration_id: int, data: bytes) -> None:
        if self.cfg.padding is not None and len(data) < CLASSIC_CAN_DLC:
            data = data + bytes([self.cfg.padding]) * (CLASSIC_CAN_DLC - len(data))
        msg = can.Message(
            arbitration_id=arbitration_id,
            data=data,
            is_extended_id=self.cfg.is_extended_id,
        )
        self.bus.send(msg)
        self.stats["tx_frames"] += 1

    def _recv_frame(self, timeout: float) -> Optional[can.Message]:
        """接收一帧，且只接受本通道 rx_id 的帧。

        不是本通道的帧直接丢弃（真实总线上会有大量其他报文）。
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stats["timeouts"] += 1
                return None
            msg = self.bus.recv(timeout=remaining)
            if msg is None:
                self.stats["timeouts"] += 1
                return None
            if msg.arbitration_id != self.cfg.rx_id:
                continue  # 别的节点的报文，忽略
            self.stats["rx_frames"] += 1
            return msg

    # ------------------------------------------------------------------ #
    # 发送方向
    # ------------------------------------------------------------------ #

    def send(self, payload: bytes) -> None:
        """发送一条完整报文，按长度自动选择单帧或分段。"""
        if len(payload) > self.cfg.max_payload:
            raise IsoTpOverflow(
                f"payload {len(payload)} bytes exceeds ISO-TP limit "
                f"{self.cfg.max_payload}"
            )
        if len(payload) <= SINGLE_FRAME_MAX_PAYLOAD:
            self._send_single_frame(payload)
        else:
            self._send_multi_frame(payload)
        self.stats["tx_messages"] += 1

    def _send_single_frame(self, payload: bytes) -> None:
        pci = FrameType.SINGLE.pci_base | len(payload)
        self._send_frame(self.cfg.tx_id, bytes([pci]) + payload)

    def _send_multi_frame(self, payload: bytes) -> None:
        total = len(payload)
        # 首帧：[0x1H][0xLL][前 6 字节]，HLL 组成 12 位总长度
        ff = bytes(
            [FrameType.FIRST.pci_base | ((total >> 8) & 0x0F), total & 0xFF]
        )
        self._send_frame(self.cfg.tx_id, ff + payload[:MULTI_FRAME_CHUNK])

        offset = MULTI_FRAME_CHUNK
        sequence = 1
        sent_in_block = 0
        block_size = self.cfg.block_size

        while offset < total:
            # 每发满 block_size 帧（或首帧之后），等一次流控
            if sent_in_block == 0:
                block_size = self._await_flow_control()

            chunk = payload[offset : offset + MULTI_FRAME_CHUNK]
            self._send_frame(
                self.cfg.tx_id,
                bytes([FrameType.CONSECUTIVE.pci_base | (sequence & 0x0F)]) + chunk,
            )
            offset += len(chunk)
            sequence = (sequence + 1) & 0x0F  # 序号 0..15 循环
            sent_in_block += 1
            if block_size != 0 and sent_in_block >= block_size:
                sent_in_block = 0

    def _await_flow_control(self) -> int:
        """等待流控帧，返回本块允许发送的帧数（0 表示不限制）。"""
        while True:
            msg = self._recv_frame(self.cfg.timeout)
            if msg is None:
                raise IsoTpTimeout(
                    f"no flow control frame from 0x{self.cfg.rx_id:03X} "
                    f"within {self.cfg.timeout}s"
                )
            if not msg.data:
                continue
            frame_type = msg.data[0] >> 4
            if frame_type != FrameType.FLOW_CONTROL:
                continue  # 多帧接收过程中夹带的其它帧，忽略

            status = FlowStatus(msg.data[0] & 0x0F)
            if status == FlowStatus.WAIT:
                continue  # 对方让我等，重新等下一个 FC
            if status == FlowStatus.OVERFLOW:
                raise IsoTpOverflow("receiver reported buffer overflow")

            block_size = msg.data[1] if len(msg.data) > 1 else 0
            stmin = decode_stmin(msg.data[2]) if len(msg.data) > 2 else 0.0
            if stmin > 0:
                time.sleep(stmin)  # 遵守最小间隔
            return block_size

    # ------------------------------------------------------------------ #
    # 接收方向
    # ------------------------------------------------------------------ #

    def recv(self, timeout: Optional[float] = None) -> bytes:
        """接收一条完整报文，自动处理流控与重组。超时抛 IsoTpTimeout。"""
        timeout = self.cfg.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout

        # 流控帧不是报文的起点。本端在多帧接收过程中可能发出 FC，
        # 若对方也恰好回 FC（或总线上有其它 FC），必须跳过而不是当成请求解析。
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IsoTpTimeout(
                    f"no ISO-TP frame on 0x{self.cfg.rx_id:03X} within {timeout}s"
                )
            msg = self._recv_frame(remaining)
            if msg is None:
                raise IsoTpTimeout(
                    f"no ISO-TP frame on 0x{self.cfg.rx_id:03X} within {timeout}s"
                )
            if not msg.data:
                raise IsoTpError("received empty CAN frame")
            if msg.data[0] >> 4 != FrameType.FLOW_CONTROL:
                break

        frame_type = msg.data[0] >> 4
        if frame_type == FrameType.SINGLE:
            length = msg.data[0] & 0x0F
            payload = bytes(msg.data[1 : 1 + length])
        elif frame_type == FrameType.FIRST:
            payload = self._recv_multi_frame(msg)
        elif frame_type == FrameType.CONSECUTIVE:
            raise IsoTpSequenceError("consecutive frame received without first frame")
        else:
            raise IsoTpError(f"unexpected frame type 0x{frame_type:X} in receive path")

        self.stats["rx_messages"] += 1
        return payload

    def _recv_multi_frame(self, first: can.Message) -> bytes:
        total = ((first.data[0] & 0x0F) << 8) | first.data[1]
        if total <= SINGLE_FRAME_MAX_PAYLOAD:
            raise IsoTpError(
                f"first frame declares {total} bytes, which should have been a single frame"
            )
        if total > self.cfg.max_payload:
            # 超出本端接收能力，必须回 OVFLW 让对方停
            self._send_flow_control(FlowStatus.OVERFLOW, 0, 0.0)
            raise IsoTpOverflow(f"incoming message of {total} bytes exceeds local limit")

        buffer = bytearray(first.data[2:])

        # 收到首帧后必须立刻回一次 CTS，否则发送方会因等不到流控而超时
        self._send_flow_control(FlowStatus.CONTINUE_TO_SEND, self.cfg.block_size, self.cfg.stmin)

        expected_sequence = 1
        frames_since_fc = 0

        while len(buffer) < total:
            msg = self._recv_frame(self.cfg.timeout)
            if msg is None:
                raise IsoTpTimeout(
                    f"lost consecutive frame at sequence {expected_sequence} "
                    f"({len(buffer)}/{total} bytes received)"
                )
            frame_type = msg.data[0] >> 4
            if frame_type != FrameType.CONSECUTIVE:
                continue  # 其它报文（如对方的否定响应）夹在中间，忽略

            sequence = msg.data[0] & 0x0F
            if sequence != expected_sequence:
                raise IsoTpSequenceError(
                    f"expected sequence {expected_sequence}, got {sequence}"
                )
            buffer.extend(msg.data[1:])
            expected_sequence = (expected_sequence + 1) & 0x0F  # 0..15 循环
            frames_since_fc += 1

            # BlockSize > 0 时，每收满一块要再回一次流控；0 表示一次放行
            if self.cfg.block_size and frames_since_fc >= self.cfg.block_size:
                self._send_flow_control(
                    FlowStatus.CONTINUE_TO_SEND, self.cfg.block_size, self.cfg.stmin
                )
                frames_since_fc = 0

        return bytes(buffer[:total])

    def _send_flow_control(self, status: FlowStatus, block_size: int, stmin: float) -> None:
        data = bytes(
            [FrameType.FLOW_CONTROL.pci_base | status, block_size, encode_stmin(stmin)]
        )
        self._send_frame(self.cfg.tx_id, data)
        self.stats["flow_control_sent"] += 1


def make_tester_connection(
    bus: can.BusABC,
    request_id: int = 0x7E0,
    response_id: int = 0x7E8,
    **kwargs,
) -> IsoTpConnection:
    """构造 Tester（诊断仪）侧连接，默认物理寻址 0x7E0/0x7E8。"""
    return IsoTpConnection(bus, IsoTpConfig(tx_id=request_id, rx_id=response_id, **kwargs))


def make_ecu_connection(
    bus: can.BusABC,
    request_id: int = 0x7E0,
    response_id: int = 0x7E8,
    **kwargs,
) -> IsoTpConnection:
    """构造 ECU 侧连接，收发 ID 与 Tester 相反。"""
    return IsoTpConnection(bus, IsoTpConfig(tx_id=response_id, rx_id=request_id, **kwargs))
