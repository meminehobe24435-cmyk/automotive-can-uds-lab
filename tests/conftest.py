"""pytest 共享夹具。

测试设计原则：
    1. 单元测试不依赖真实总线 —— ISO-TP 的 PCI 编解码、STmin 换算、
       DBC 信号布局这些都是纯函数，直接断言，跑得飞快且跨平台。
    2. 集成测试用 python-can 的 virtual 总线 —— 每个用例一条独立 channel，
       互不干扰，也不依赖 vcan 和 root，CI 里开箱即用。
    3. 真实 SocketCAN 另设一个标记为 socketcan 的用例，没有 vcan 时自动跳过，
       用于验证"同一套代码在真实内核 CAN 栈上行为一致"。
"""

from __future__ import annotations

import threading
import uuid

import can
import pytest

from src.bus import resolve_config, setup_vcan
from src.dbc_codec import DbcCodec
from src.isotp import IsoTpConfig, IsoTpConnection
from src.uds import UdsClient, UdsServer, UdsServerConfig

TESTER_TX = 0x7E0
ECU_TX = 0x7E8


class BackgroundReceiver:
    """在后台线程里执行 ``conn.recv()``。

    为什么测试里必须有它 —— 这是 ISO-TP 的协议特性，不是测试技巧：

        ISO-TP 多帧传输是**握手**协议。发送方发出首帧后，必须停下来等
        接收方回一个流控帧（FC），才允许继续发连续帧。因此如果在同一个
        线程里先调 ``send()`` 再调 ``recv()``：

            send()  -> 发首帧 -> 阻塞等 FC  ┐
                                            ├─ 死锁
            recv()  -> 还没开始跑，没人回 FC ┘

        真实场景不会死锁，因为 ECU 和诊断仪是两个独立节点，天然并发。
        测试里必须显式把接收侧放到另一个线程。
    """

    def __init__(self, conn: IsoTpConnection, timeout: float = 3.0):
        self.conn = conn
        self.timeout = timeout
        self.result: bytes | None = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "BackgroundReceiver":
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self.result = self.conn.recv(timeout=self.timeout)
        except BaseException as exc:  # 断言时需要看到原始异常类型
            self.error = exc

    def get(self, timeout: float = 6.0) -> bytes:
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                f"background receiver still running after {timeout}s "
                f"(likely a protocol deadlock)"
            )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.fixture
def receive():
    """工厂夹具：``rx = receive(conn)`` 立刻开始后台接收，``rx.get()`` 取结果。"""

    def factory(conn: IsoTpConnection, timeout: float = 3.0) -> BackgroundReceiver:
        return BackgroundReceiver(conn, timeout=timeout).start()

    return factory


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "socketcan: 需要真实 SocketCAN/vcan 的用例")


@pytest.fixture
def channel() -> str:
    """每个用例一条独立虚拟通道，避免用例间串帧。"""
    return f"pytest-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def bus_pair(channel: str):
    """一对互相可见的 virtual 总线。"""
    tester_bus = can.Bus(interface="virtual", channel=channel)
    ecu_bus = can.Bus(interface="virtual", channel=channel)
    yield tester_bus, ecu_bus
    for bus in (tester_bus, ecu_bus):
        try:
            bus.shutdown()
        except Exception:
            pass


@pytest.fixture
def connections(bus_pair):
    """Tester 侧与 ECU 侧的 ISO-TP 连接。"""
    tester_bus, ecu_bus = bus_pair
    tester_conn = IsoTpConnection(
        tester_bus, IsoTpConfig(tx_id=TESTER_TX, rx_id=ECU_TX, timeout=1.0)
    )
    ecu_conn = IsoTpConnection(
        ecu_bus, IsoTpConfig(tx_id=ECU_TX, rx_id=TESTER_TX, timeout=1.0)
    )
    return tester_conn, ecu_conn


@pytest.fixture
def ecu(connections):
    """一个后台运行的模拟 ECU。"""
    _tester_conn, ecu_conn = connections
    server = UdsServer(ecu_conn, UdsServerConfig(security_delay=0.3))
    server.start_thread()
    yield server
    server.stop()


@pytest.fixture
def client(connections, ecu) -> UdsClient:
    """连到模拟 ECU 的诊断客户端。"""
    tester_conn, _ecu_conn = connections
    return UdsClient(tester_conn, timeout=1.0)


@pytest.fixture
def unlocked_client(client: UdsClient) -> UdsClient:
    """已切到扩展会话并解锁安全访问的客户端。"""
    from src.uds import SessionType

    client.diagnostic_session_control(SessionType.EXTENDED)
    client.unlock(level=1)
    return client


@pytest.fixture(scope="session")
def codec() -> DbcCodec:
    return DbcCodec()


@pytest.fixture
def socketcan_config():
    """真实 SocketCAN 配置；不可用时跳过用例。"""
    if not setup_vcan("vcan0"):
        pytest.skip("vcan0 不可用（非 Linux 或无 root 权限）")
    config = resolve_config(interface="socketcan", channel="vcan0")
    return config
