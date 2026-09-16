"""ISO 14229 (UDS) 诊断服务实现：ECU 服务端模拟 + Tester 客户端。

手写实现，不依赖 udsoncan。原因同 ISO-TP：UDS 是车载诊断的核心考点，
每一字节的含义都要能讲清楚。

已实现服务：

    0x10 DiagnosticSessionControl   会话切换（default / programming / extended）
    0x11 ECUReset                    ECU 复位
    0x14 ClearDiagnosticInformation  清除 DTC
    0x19 ReadDTCInformation          读取 DTC
    0x22 ReadDataByIdentifier        按 DID 读数据
    0x27 SecurityAccess              安全访问（种子-密钥）
    0x28 CommunicationControl        通信控制
    0x2E WriteDataByIdentifier       按 DID 写数据
    0x31 RoutineControl              例程控制
    0x3E TesterPresent               诊断仪在线保持
    0x7F NegativeResponse            否定响应

关键机制：
    - 会话状态机：默认会话下大部分服务不可用
    - S3 定时器：默认 5 s 内没有 TesterPresent，自动退回默认会话
    - 安全访问：先请求种子（0x27 0x01），再送密钥（0x27 0x02），
      连续错误超过阈值锁定
    - 0x78 响应挂起：处理耗时较长时，先回 0x7F .. 0x78，再回最终响应
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Optional

from .isotp import IsoTpConnection, IsoTpError, IsoTpTimeout

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

POSITIVE_RESPONSE_OFFSET = 0x40
NEGATIVE_RESPONSE_SID = 0x7F

S3_TIMEOUT_DEFAULT = 5.0  # 秒，ISO 14229 规定默认值
SECURITY_MAX_ATTEMPTS = 3
SECURITY_DELAY_AFTER_FAIL = 10.0  # 秒


class SessionType(IntEnum):
    """诊断会话类型（0x10 服务的子功能）。"""

    DEFAULT = 0x01
    PROGRAMMING = 0x02
    EXTENDED = 0x03
    SAFETY_SYSTEM = 0x04


class ResetType(IntEnum):
    """ECU 复位类型（0x11 服务的子功能）。"""

    HARD = 0x01
    KEY_OFF_ON = 0x02
    SOFT = 0x03


class Nrc(IntEnum):
    """否定响应码（Negative Response Code），ISO 14229-1 表 A.1。"""

    GENERAL_REJECT = 0x10
    SERVICE_NOT_SUPPORTED = 0x11
    SUB_FUNCTION_NOT_SUPPORTED = 0x12
    INCORRECT_MESSAGE_LENGTH = 0x13
    RESPONSE_TOO_LONG = 0x14
    BUSY_REPEAT_REQUEST = 0x21
    CONDITIONS_NOT_CORRECT = 0x22
    REQUEST_SEQUENCE_ERROR = 0x24
    REQUEST_OUT_OF_RANGE = 0x31
    SECURITY_ACCESS_DENIED = 0x33
    INVALID_KEY = 0x35
    EXCEED_NUMBER_OF_ATTEMPTS = 0x36
    REQUIRED_TIME_DELAY_NOT_EXPIRED = 0x37
    RESPONSE_PENDING = 0x78
    SUB_FUNCTION_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7E
    SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION = 0x7F


class UdsError(Exception):
    """UDS 层异常基类。"""


class NegativeResponseError(UdsError):
    """ECU 返回了否定响应。"""

    def __init__(self, request_sid: int, nrc: int):
        self.request_sid = request_sid
        self.nrc = nrc
        super().__init__(
            f"service 0x{request_sid:02X} rejected with NRC 0x{nrc:02X} ({Nrc(nrc).name})"
            if nrc in set(item.value for item in Nrc)
            else f"service 0x{request_sid:02X} rejected with NRC 0x{nrc:02X}"
        )


# --------------------------------------------------------------------------- #
# 种子-密钥算法（演示用）
# --------------------------------------------------------------------------- #


def demo_seed_to_key(seed: bytes) -> bytes:
    """把 4 字节种子转成 4 字节密钥。

    真实项目的算法由主机厂保密，通常是 AES 或私有混淆表。
    这里用一个可解释的位运算版本，便于演示完整流程：

        key = ((seed << 3) ^ 0xA5A5_A5A5) + 0x1234_5678   （32 位截断）
    """
    if len(seed) != 4:
        raise ValueError("seed must be 4 bytes")
    value = int.from_bytes(seed, "big")
    mixed = ((value << 3) & 0xFFFFFFFF) ^ 0xA5A5A5A5
    key = (mixed + 0x12345678) & 0xFFFFFFFF
    return key.to_bytes(4, "big")


# --------------------------------------------------------------------------- #
# ECU 服务端
# --------------------------------------------------------------------------- #


@dataclass
class UdsServerConfig:
    """ECU 侧可配置项，方便针对不同测试场景调整。"""

    s3_timeout: float = S3_TIMEOUT_DEFAULT
    security_max_attempts: int = SECURITY_MAX_ATTEMPTS
    security_delay: float = SECURITY_DELAY_AFTER_FAIL
    # 需要 0x78 挂起模拟的服务 -> 挂起秒数
    pending_services: dict[int, float] = field(default_factory=dict)
    vin: str = "LSNHBE1A2M0000001"


DEFAULT_DIDS: dict[int, bytes] = {
    0xF190: b"LSNHBE1A2M0000001",  # VIN，17 字节 ASCII
    0xF187: b"3EC907321A",  # 备件号
    0xF18C: b"ECU2408001234",  # ECU 序列号
    0xF195: b"SW01.03.02",  # 软件版本
    0xF191: b"HW02.01",  # 硬件版本
    0xF186: bytes([SessionType.DEFAULT]),  # 当前激活会话
    0x0100: bytes([12, 0x80]),  # 自定义：蓄电池电压 12.80 V（0.01 V/LSB）
    0x0101: bytes([0x02]),  # 自定义：点火状态 Run
}

DEFAULT_DTCS: list[tuple[int, int, int]] = [
    # (DTC 3 字节, 状态字节, 严重度)
    (0x00A001, 0x24, 0x20),
    (0x00B102, 0x08, 0x40),
    (0x00C203, 0x2F, 0x60),
]

# 只读 DID：标定/标识类数据，即使解锁安全访问也不允许通过 0x2E 改写。
# 真实 ECU 里这类数据要么在标定区，要么由刷写流程（0x34/0x36/0x37）写入。
READ_ONLY_DIDS: frozenset[int] = frozenset(
    {0xF186, 0xF187, 0xF18C, 0xF190, 0xF191, 0xF195}
)

# 可通过 0x2E 写入的 DID（应用层可配置参数）
WRITABLE_DIDS: frozenset[int] = frozenset({0x0100, 0x0101})


class UdsServer:
    """一个软件模拟的 ECU，通过 ISO-TP 连接对外提供 UDS 服务。

    单线程轮询模型：调用 run_once() 处理一条请求，或 serve_forever() 常驻。
    测试里用 start_thread() 起后台线程，然后客户端就能像对真实 ECU 一样交互。
    """

    def __init__(
        self,
        connection: IsoTpConnection,
        config: Optional[UdsServerConfig] = None,
    ):
        self.conn = connection
        self.cfg = config or UdsServerConfig()
        self.dids: dict[int, bytes] = dict(DEFAULT_DIDS)
        self.dids[0xF190] = self.cfg.vin.encode("ascii")

        self.session = SessionType.DEFAULT
        self.security_level: Optional[int] = None
        self._last_activity = time.monotonic()
        self._failed_key_attempts = 0
        self._lockout_until = 0.0
        self.routines_run: list[tuple[int, bytes]] = []
        self.reset_count = 0
        self.dtcs: list[tuple[int, int, int]] = list(DEFAULT_DTCS)
        self._seed_counter = 0

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 后台线程里最后一次未预期异常，供测试断言；不直接抛出以免整个 ECU 停摆
        self.thread_error: Optional[BaseException] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def _check_s3(self) -> None:
        """S3 超时：长时间没有 TesterPresent 就退回默认会话并锁安全。"""
        if self.session == SessionType.DEFAULT:
            return
        if time.monotonic() - self._last_activity > self.cfg.s3_timeout:
            self.session = SessionType.DEFAULT
            self.security_level = None

    def run_once(self, timeout: float = 0.1) -> bool:
        """处理一条请求。返回 False 表示本轮没有收到请求。"""
        self._check_s3()
        try:
            request = self.conn.recv(timeout=timeout)
        except IsoTpTimeout:
            return False
        except IsoTpError:
            return False

        if not request:
            return False

        sid = request[0]
        self._last_activity = time.monotonic()

        # 需要较长时间处理的服务：先回 0x78 告诉诊断仪"收到了，还在处理"，
        # 否则诊断仪会因为超过 P2 时间而误判超时。
        # 注意这与服务成功与否无关 —— 慢服务既可能成功也可能最终失败。
        pending = self.cfg.pending_services.get(sid)
        if pending:
            self._send([NEGATIVE_RESPONSE_SID, sid, Nrc.RESPONSE_PENDING])
            time.sleep(pending)

        try:
            response = self._dispatch(sid, request)
        except NegativeResponseError as exc:
            self._send([NEGATIVE_RESPONSE_SID, sid, exc.nrc])
            return True

        if response is not None:
            self._send(response)
        return True

    def serve_forever(self, poll: float = 0.05) -> None:
        """常驻循环。

        单条请求处理失败（总线抖动、对端提前挂断等）不应该让整个 ECU 服务
        停摆 —— 真实 ECU 也不会因为一次诊断异常就死机。所以这里兜住异常，
        记在 thread_error 里供测试与排障使用。
        """
        while not self._stop.is_set():
            try:
                self.run_once(timeout=poll)
            except Exception as exc:  # noqa: BLE001 - 有意兜底
                self.thread_error = exc
                time.sleep(poll)

    def start_thread(self, poll: float = 0.02) -> threading.Thread:
        self._thread = threading.Thread(target=self.serve_forever, args=(poll,), daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _send(self, payload: list[int]) -> None:
        self.conn.send(bytes(payload))

    # ------------------------------------------------------------------ #
    # 请求路由
    # ------------------------------------------------------------------ #

    def _dispatch(self, sid: int, request: bytes) -> Optional[list[int]]:
        handler = {
            0x10: self._svc_session_control,
            0x11: self._svc_ecu_reset,
            0x14: self._svc_clear_dtc,
            0x19: self._svc_read_dtc,
            0x22: self._svc_read_data_by_id,
            0x27: self._svc_security_access,
            0x28: self._svc_communication_control,
            0x2E: self._svc_write_data_by_id,
            0x31: self._svc_routine_control,
            0x3E: self._svc_tester_present,
        }.get(sid)

        if handler is None:
            raise NegativeResponseError(sid, Nrc.SERVICE_NOT_SUPPORTED)

        # 默认会话下，写入类与例程类服务不允许执行
        if self.session == SessionType.DEFAULT and sid in (0x2E, 0x31, 0x27, 0x28):
            raise NegativeResponseError(sid, Nrc.SERVICE_NOT_SUPPORTED_IN_ACTIVE_SESSION)

        return handler(request)

    # ------------------------------------------------------------------ #
    # 各服务实现
    # ------------------------------------------------------------------ #

    def _svc_session_control(self, request: bytes) -> list[int]:
        if len(request) < 2:
            raise NegativeResponseError(0x10, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1] & 0x7F
        try:
            target = SessionType(sub)
        except ValueError:
            raise NegativeResponseError(0x10, Nrc.SUB_FUNCTION_NOT_SUPPORTED)

        self.session = target
        self.dids[0xF186] = bytes([target])
        if target == SessionType.DEFAULT:
            self.security_level = None

        p2, p2_star = (50, 5000) if target == SessionType.DEFAULT else (50, 500)
        return [
            0x10 | POSITIVE_RESPONSE_OFFSET,
            target,
            (p2 >> 8) & 0xFF,
            p2 & 0xFF,
            (p2_star >> 8) & 0xFF,
            p2_star & 0xFF,
        ]

    def _svc_ecu_reset(self, request: bytes) -> list[int]:
        if len(request) < 2:
            raise NegativeResponseError(0x11, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1] & 0x7F
        if sub not in set(item.value for item in ResetType):
            raise NegativeResponseError(0x11, Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        self.reset_count += 1
        self.session = SessionType.DEFAULT
        self.security_level = None
        self.dids[0xF186] = bytes([SessionType.DEFAULT])
        return [0x11 | POSITIVE_RESPONSE_OFFSET, sub]

    def _svc_clear_dtc(self, request: bytes) -> list[int]:
        if len(request) != 4:
            raise NegativeResponseError(0x14, Nrc.INCORRECT_MESSAGE_LENGTH)
        self.dtcs = []
        return [0x14 | POSITIVE_RESPONSE_OFFSET]

    def _svc_read_dtc(self, request: bytes) -> list[int]:
        if len(request) < 2:
            raise NegativeResponseError(0x19, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1]
        if sub != 0x02:  # reportDTCByStatusMask
            raise NegativeResponseError(0x19, Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        mask = request[2] if len(request) > 2 else 0xFF
        matched = [dtc for dtc in self.dtcs if dtc[1] & mask]
        # 可用性掩码：报告支持哪些 DTC 状态位
        availability = 0xFF
        out = [0x19 | POSITIVE_RESPONSE_OFFSET, sub, availability]
        for dtc, status, _severity in matched:
            out += [(dtc >> 16) & 0xFF, (dtc >> 8) & 0xFF, dtc & 0xFF, status]
        return out

    def _svc_read_data_by_id(self, request: bytes) -> list[int]:
        if len(request) < 3 or (len(request) - 1) % 2 != 0:
            raise NegativeResponseError(0x22, Nrc.INCORRECT_MESSAGE_LENGTH)
        out = [0x22 | POSITIVE_RESPONSE_OFFSET]
        for i in range(1, len(request), 2):
            did = (request[i] << 8) | request[i + 1]
            if did not in self.dids:
                raise NegativeResponseError(0x22, Nrc.REQUEST_OUT_OF_RANGE)
            data = self.dids[did]
            out += [request[i], request[i + 1]] + list(data)
        return out

    def _svc_write_data_by_id(self, request: bytes) -> list[int]:
        if len(request) < 4:
            raise NegativeResponseError(0x2E, Nrc.INCORRECT_MESSAGE_LENGTH)
        did = (request[1] << 8) | request[2]
        if did not in self.dids:
            raise NegativeResponseError(0x2E, Nrc.REQUEST_OUT_OF_RANGE)
        # 标识/标定类 DID 只读，解锁也不能改（真实项目里要走刷写流程）
        if did in READ_ONLY_DIDS or did not in WRITABLE_DIDS:
            raise NegativeResponseError(0x2E, Nrc.CONDITIONS_NOT_CORRECT)
        if self.security_level is None:
            raise NegativeResponseError(0x2E, Nrc.SECURITY_ACCESS_DENIED)
        self.dids[did] = bytes(request[3:])
        return [0x2E | POSITIVE_RESPONSE_OFFSET, request[1], request[2]]

    def _svc_security_access(self, request: bytes) -> list[int]:
        if len(request) < 2:
            raise NegativeResponseError(0x27, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1]
        level = (sub + 1) // 2  # 01/02 -> 1，03/04 -> 2

        # 锁定中
        if time.monotonic() < self._lockout_until:
            raise NegativeResponseError(0x27, Nrc.REQUIRED_TIME_DELAY_NOT_EXPIRED)

        if sub % 2 == 1:  # 奇数：请求种子
            if len(request) != 2:
                raise NegativeResponseError(0x27, Nrc.INCORRECT_MESSAGE_LENGTH)
            seed = self._make_seed(level)
            self._pending_seed = seed
            return [0x27 | POSITIVE_RESPONSE_OFFSET, sub] + list(seed)

        # 偶数：送密钥
        if len(request) != 6:
            raise NegativeResponseError(0x27, Nrc.INCORRECT_MESSAGE_LENGTH)
        pending = getattr(self, "_pending_seed", None)
        if pending is None:
            raise NegativeResponseError(0x27, Nrc.REQUEST_SEQUENCE_ERROR)

        expected = demo_seed_to_key(pending)
        if bytes(request[2:6]) != expected:
            self._failed_key_attempts += 1
            self._pending_seed = None
            if self._failed_key_attempts >= self.cfg.security_max_attempts:
                self._lockout_until = time.monotonic() + self.cfg.security_delay
                self._failed_key_attempts = 0
                raise NegativeResponseError(0x27, Nrc.EXCEED_NUMBER_OF_ATTEMPTS)
            raise NegativeResponseError(0x27, Nrc.INVALID_KEY)

        self.security_level = level
        self._failed_key_attempts = 0
        self._pending_seed = None
        return [0x27 | POSITIVE_RESPONSE_OFFSET, sub]

    def _make_seed(self, level: int) -> bytes:
        """生成种子。

        真实 ECU 用硬件随机数；这里用"毫秒时间戳 ^ 自增计数 ^ 安全等级"，
        保证同一毫秒内的连续请求也能拿到不同种子（防止重放），
        同时保持可复现，便于写确定性测试。
        """
        self._seed_counter = (self._seed_counter + 1) & 0xFFFF
        nonce = int(time.time() * 1000) & 0xFFFFFFFF
        value = (nonce ^ (self._seed_counter << 16) ^ (level * 0x01010101)) & 0xFFFFFFFF
        return value.to_bytes(4, "big")

    def _svc_communication_control(self, request: bytes) -> list[int]:
        if len(request) < 3:
            raise NegativeResponseError(0x28, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1] & 0x7F
        if sub not in (0x00, 0x01, 0x02, 0x03):
            raise NegativeResponseError(0x28, Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        return [0x28 | POSITIVE_RESPONSE_OFFSET, sub]

    def _svc_routine_control(self, request: bytes) -> list[int]:
        if len(request) < 4:
            raise NegativeResponseError(0x31, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1]
        if sub not in (0x01, 0x02, 0x03):  # start / stop / requestResults
            raise NegativeResponseError(0x31, Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        routine_id = (request[2] << 8) | request[3]
        if routine_id not in (0x0203, 0xFF00, 0xFF01):
            raise NegativeResponseError(0x31, Nrc.REQUEST_OUT_OF_RANGE)
        if self.security_level is None and routine_id != 0xFF01:
            raise NegativeResponseError(0x31, Nrc.SECURITY_ACCESS_DENIED)
        self.routines_run.append((routine_id, bytes(request[4:])))
        return [0x31 | POSITIVE_RESPONSE_OFFSET, sub, request[2], request[3], 0x00]

    def _svc_tester_present(self, request: bytes) -> Optional[list[int]]:
        if len(request) < 2:
            raise NegativeResponseError(0x3E, Nrc.INCORRECT_MESSAGE_LENGTH)
        sub = request[1]
        suppress = bool(sub & 0x80)
        if sub & 0x7F != 0x00:
            raise NegativeResponseError(0x3E, Nrc.SUB_FUNCTION_NOT_SUPPORTED)
        if suppress:
            return None  # suppressPosRspMsgIndicationBit 置位时不回响应
        return [0x3E | POSITIVE_RESPONSE_OFFSET, 0x00]


# --------------------------------------------------------------------------- #
# Tester 客户端
# --------------------------------------------------------------------------- #


class UdsClient:
    """诊断仪侧客户端，封装各服务的请求构造与否定响应处理。"""

    def __init__(self, connection: IsoTpConnection, timeout: float = 1.0):
        self.conn = connection
        self.timeout = timeout

    # ---------------------------------------------------------------- #

    def request(self, payload: bytes, allow_pending: bool = True) -> bytes:
        """发送原始请求并返回肯定响应。收到否定响应抛 NegativeResponseError。

        allow_pending=True 时自动处理 0x78（响应挂起），继续等待最终响应。
        """
        self.conn.send(payload)
        sid = payload[0]

        while True:
            response = self.conn.recv(timeout=self.timeout)
            if not response:
                raise UdsError("empty response from ECU")

            if response[0] == NEGATIVE_RESPONSE_SID:
                if len(response) < 3:
                    raise UdsError("malformed negative response")
                nrc = response[2]
                if nrc == Nrc.RESPONSE_PENDING and allow_pending:
                    continue  # ECU 还在处理，继续等
                raise NegativeResponseError(sid, nrc)

            if response[0] != sid | POSITIVE_RESPONSE_OFFSET:
                raise UdsError(
                    f"unexpected response SID 0x{response[0]:02X} "
                    f"for request 0x{sid:02X}"
                )
            return response

    # ---------------------------------------------------------------- #
    # 服务封装
    # ---------------------------------------------------------------- #

    def diagnostic_session_control(self, session: SessionType) -> bytes:
        return self.request(bytes([0x10, session]))

    def ecu_reset(self, reset_type: ResetType = ResetType.HARD) -> bytes:
        return self.request(bytes([0x11, reset_type]))

    def clear_diagnostic_information(self, group: int = 0xFFFFFF) -> bytes:
        return self.request(bytes([0x14, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF]))

    def read_dtc_information(self, status_mask: int = 0xFF) -> list[tuple[int, int]]:
        response = self.request(bytes([0x19, 0x02, status_mask]))
        dtcs = []
        for i in range(3, len(response), 4):
            code = (response[i] << 16) | (response[i + 1] << 8) | response[i + 2]
            dtcs.append((code, response[i + 3]))
        return dtcs

    def read_data_by_identifier(self, did: int) -> bytes:
        response = self.request(bytes([0x22, (did >> 8) & 0xFF, did & 0xFF]))
        return response[3:]

    def write_data_by_identifier(self, did: int, data: bytes) -> bytes:
        return self.request(bytes([0x2E, (did >> 8) & 0xFF, did & 0xFF]) + data)

    def security_access_request_seed(self, level: int = 1) -> bytes:
        sub = level * 2 - 1
        response = self.request(bytes([0x27, sub]))
        return response[2:]

    def security_access_send_key(self, key: bytes, level: int = 1) -> bytes:
        sub = level * 2
        return self.request(bytes([0x27, sub]) + key)

    def unlock(self, level: int = 1) -> None:
        """一步完成种子-密钥交换。"""
        seed = self.security_access_request_seed(level)
        key = demo_seed_to_key(seed)
        self.security_access_send_key(key, level)

    def routine_control(self, control_type: int, routine_id: int, data: bytes = b"") -> bytes:
        return self.request(
            bytes([0x31, control_type, (routine_id >> 8) & 0xFF, routine_id & 0xFF]) + data
        )

    def tester_present(self, suppress_response: bool = False) -> Optional[bytes]:
        sub = 0x80 if suppress_response else 0x00
        payload = bytes([0x3E, sub])
        if suppress_response:
            self.conn.send(payload)
            return None
        return self.request(payload)
