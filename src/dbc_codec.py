"""DBC 报文/信号编解码与校验。

DBC 是车载行业描述 CAN 报文的标准格式，测试岗每天都在用：
    - 按 DBC 把物理值打包成报文（模拟 ECU 发报文）
    - 按 DBC 把报文还原成物理值（解析 ECU 发的报文）
    - 按 DBC 校验信号是否越界（信号范围测试）

信号定义一行读懂：
    SG_ EngineSpeed : 0|16@1+ (0.25,0) [0|16383.75] "rpm" Gateway
                      │  │  │ │   │    │  │          │      └ 接收节点
                      │  │  │ │   │    │  │          └ 单位
                      │  │  │ │   │    │  └ 最大值 / 最小值（物理值）
                      │  │  │ │   │    └ 偏移量
                      │  │  │ │   └ 分辨率/精度：物理值 = 原始值 * 0.25 + 0
                      │  │  │ └ 1=Intel(小端) 0=Motorola(大端)
                      │  │  └ 位长度
                      │  └ 起始位
                      └ 信号名

注意：DBC 里的 "@1+" 前缀 `+` 表示无符号，`-` 表示有符号。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import can
import cantools
from cantools.database.can import Message, Signal

DEFAULT_DBC = Path(__file__).resolve().parent.parent / "dbc" / "demo_ecu.dbc"


class DbcError(Exception):
    """DBC 编解码异常。"""


class SignalRangeError(DbcError):
    """信号物理值超出 DBC 定义的范围。"""

    def __init__(self, message: str, signal: str, value: float, low: float, high: float):
        self.signal = signal
        self.value = value
        self.low = low
        self.high = high
        super().__init__(
            f"{message}.{signal} = {value} out of DBC range [{low}, {high}]"
        )


@dataclass
class SignalSpec:
    """信号的静态定义，便于生成测试报告。"""

    name: str
    start_bit: int
    length: int
    byte_order: str
    is_signed: bool
    scale: float
    offset: float
    minimum: Optional[float]
    maximum: Optional[float]
    unit: str

    @property
    def bit_range(self) -> str:
        return f"{self.start_bit}..{self.start_bit + self.length - 1}"

    def to_physical(self, raw: int) -> float:
        return raw * self.scale + self.offset

    def to_raw(self, physical: float) -> int:
        return int(round((physical - self.offset) / self.scale))


class DbcCodec:
    """基于 cantools 的编解码器，附加信号范围校验与信号清单导出。"""

    def __init__(self, dbc_path: Optional[Path | str] = None):
        self.path = Path(dbc_path) if dbc_path else DEFAULT_DBC
        if not self.path.exists():
            raise DbcError(f"DBC file not found: {self.path}")
        self.db = cantools.database.load_file(str(self.path))
        self._by_id = {msg.frame_id: msg for msg in self.db.messages}

    # ------------------------------------------------------------------ #
    # 报文/信号查询
    # ------------------------------------------------------------------ #

    @property
    def message_names(self) -> list[str]:
        return [msg.name for msg in self.db.messages]

    def message(self, name: str) -> Message:
        try:
            return self.db.get_message_by_name(name)
        except KeyError as exc:
            raise DbcError(f"message '{name}' not in DBC") from exc

    def message_by_id(self, frame_id: int) -> Message:
        try:
            return self._by_id[frame_id]
        except KeyError as exc:
            raise DbcError(f"frame id 0x{frame_id:X} not in DBC") from exc

    def signal_specs(self, message_name: str) -> list[SignalSpec]:
        msg = self.message(message_name)
        specs = []
        for sig in msg.signals:
            specs.append(
                SignalSpec(
                    name=sig.name,
                    start_bit=sig.start,
                    length=sig.length,
                    byte_order="Intel" if sig.byte_order == "little_endian" else "Motorola",
                    is_signed=sig.is_signed,
                    scale=sig.scale,
                    offset=sig.offset,
                    minimum=sig.minimum,
                    maximum=sig.maximum,
                    unit=sig.unit or "",
                )
            )
        return specs

    # ------------------------------------------------------------------ #
    # 编解码
    # ------------------------------------------------------------------ #

    def encode(self, message_name: str, signals: dict[str, Any], strict: bool = True) -> can.Message:
        """把物理值字典打包成 CAN 报文。

        strict=True 时先做范围校验，越界直接抛 SignalRangeError ——
        这正是"信号范围测试"用例要的行为。
        """
        msg = self.message(message_name)
        if strict:
            violations = self.validate(message_name, signals)
            if violations:
                raise violations[0]

        unknown = set(signals) - {s.name for s in msg.signals}
        if unknown:
            raise DbcError(
                f"unknown signals for {message_name}: {sorted(unknown)}. "
                f"valid: {[s.name for s in msg.signals]}"
            )

        data = msg.encode(signals, strict=strict, padding=True)
        return can.Message(
            arbitration_id=msg.frame_id, data=data, is_extended_id=msg.is_extended_frame
        )

    def decode(self, frame: can.Message) -> dict[str, Any]:
        """把 CAN 报文还原成物理值字典。

        注意：DBC 里没定义的多路复用信号或越界原始值不会在这里报错，
        需要调用方配合 validate_decoded() 使用。
        """
        msg = self.message_by_id(frame.arbitration_id)
        try:
            return msg.decode(bytes(frame.data), decode_choices=False)
        except Exception as exc:  # cantools 对长度不符会抛多种异常
            raise DbcError(
                f"failed to decode frame 0x{frame.arbitration_id:X} "
                f"({len(frame.data)} bytes) as {msg.name}: {exc}"
            ) from exc

    def decode_with_names(self, frame: can.Message) -> dict[str, Any]:
        """解码并把枚举值翻译成文本（EngineState: 2 -> "Running"）。"""
        msg = self.message_by_id(frame.arbitration_id)
        decoded = msg.decode(bytes(frame.data), decode_choices=True)
        return dict(decoded)

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #

    def validate(self, message_name: str, signals: dict[str, Any]) -> list[Exception]:
        """逐信号校验物理值是否落在 DBC 范围内，返回所有违规项。"""
        msg = self.message(message_name)
        violations: list[Exception] = []
        for sig in msg.signals:
            if sig.name not in signals:
                continue
            value = signals[sig.name]
            if sig.minimum is not None and value < sig.minimum:
                violations.append(
                    SignalRangeError(message_name, sig.name, value, sig.minimum, sig.maximum)
                )
            elif sig.maximum is not None and value > sig.maximum:
                violations.append(
                    SignalRangeError(message_name, sig.name, value, sig.minimum, sig.maximum)
                )
        return violations

    def validate_decoded(self, message_name: str, decoded: dict[str, Any]) -> list[Exception]:
        """校验一条已解码报文的物理值是否越界。"""
        return self.validate(message_name, decoded)

    def expected_dlc(self, message_name: str) -> int:
        return self.message(message_name).length

    def sender_of(self, message_name: str) -> Optional[str]:
        return self.message(message_name).senders[0] if self.message(message_name).senders else None
