"""CAN 总线初始化与生命周期管理。

设计目标：同一套代码在三种环境下都能跑，不需要改任何东西。

1. Linux + SocketCAN(vcan)  —— 最真实，面试讲这个
       sudo modprobe vcan
       sudo ip link add dev vcan0 type vcan
       sudo ip link set up vcan0

2. Windows / macOS          —— python-can 的 virtual 总线（进程内回环）
       无需任何驱动和内核模块

3. CI (GitHub Actions)      —— ubuntu runner 有免密 sudo，直接建 vcan

为什么不用真实硬件？
    本仓库验证的是**协议栈与测试逻辑**（ISO-TP 分段、UDS 服务、DBC 信号、
    测试用例设计），这些与被测 ECU 是真实还是仿真无关。真实台架上把
    interface 换成 "vector"/"pcan"/"ixxat" 即可，上层代码零改动。
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import can

DEFAULT_CHANNEL = "vcan0"
DEFAULT_BITRATE = 500_000  # 500 kbit/s，乘用车 CAN 主流速率


@dataclass
class BusConfig:
    """总线配置。"""

    interface: str
    channel: str
    bitrate: int = DEFAULT_BITRATE
    receive_own_messages: bool = False

    def describe(self) -> str:
        return f"{self.interface}:{self.channel} @ {self.bitrate // 1000} kbit/s"


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _socketcan_channel_exists(channel: str) -> bool:
    """检查 SocketCAN 网络接口是否已存在。"""
    try:
        out = subprocess.run(
            ["ip", "-br", "link", "show", channel],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0 and channel in out.stdout
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def setup_vcan(channel: str = DEFAULT_CHANNEL, use_sudo: bool = True) -> bool:
    """在 Linux 上创建并启用一个 vcan 接口。

    返回 True 表示接口可用（本来就存在，或本次创建成功）。
    非 Linux 环境直接返回 False —— 调用方应回退到 virtual 总线。
    """
    if not _is_linux():
        return False

    if _socketcan_channel_exists(channel):
        return True

    prefix = ["sudo", "-n"] if use_sudo else []
    commands = [
        ["modprobe", "vcan"],
        ["ip", "link", "add", "dev", channel, "type", "vcan"],
        ["ip", "link", "set", "up", channel],
    ]
    for cmd in commands:
        full = prefix + cmd
        result = subprocess.run(full, capture_output=True, text=True)
        # "modprobe vcan" 在模块已内置时会返回非零，属正常情况，不阻断流程
        if result.returncode != 0 and cmd[0] != "modprobe":
            # 接口可能已被并发创建，再确认一次
            if _socketcan_channel_exists(channel):
                return True
            return False

    return _socketcan_channel_exists(channel)


def resolve_config(
    interface: Optional[str] = None,
    channel: Optional[str] = None,
    bitrate: int = DEFAULT_BITRATE,
) -> BusConfig:
    """决定用哪种总线。

    优先级：
        1. 显式传入的 interface / channel
        2. 环境变量 CAN_INTERFACE / CAN_CHANNEL
        3. Linux 且有 vcan → socketcan
        4. 其他 → python-can virtual（进程内回环）
    """
    interface = interface or os.environ.get("CAN_INTERFACE")
    channel = channel or os.environ.get("CAN_CHANNEL")

    if interface:
        return BusConfig(interface, channel or DEFAULT_CHANNEL, bitrate)

    if _is_linux():
        chan = channel or DEFAULT_CHANNEL
        if _socketcan_channel_exists(chan) or setup_vcan(chan):
            return BusConfig("socketcan", chan, bitrate)

    return BusConfig("virtual", channel or "vcan0", bitrate)


def create_bus(config: BusConfig, **kwargs) -> can.BusABC:
    """按配置创建 python-can 总线对象。"""
    if config.interface == "virtual":
        return can.Bus(
            interface="virtual",
            channel=config.channel,
            receive_own_messages=config.receive_own_messages,
            **kwargs,
        )
    return can.Bus(
        interface=config.interface,
        channel=config.channel,
        bitrate=config.bitrate,
        receive_own_messages=config.receive_own_messages,
        **kwargs,
    )


class BusManager:
    """总线上下文管理器，保证异常路径下也能正确 shutdown。"""

    def __init__(self, config: Optional[BusConfig] = None, **kwargs):
        self.config = config or resolve_config()
        self._kwargs = kwargs
        self.bus: Optional[can.BusABC] = None

    def __enter__(self) -> can.BusABC:
        self.bus = create_bus(self.config, **self._kwargs)
        return self.bus

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self.bus is not None:
            try:
                self.bus.shutdown()
            finally:
                self.bus = None


def make_virtual_bus_pair(channel_suffix: str = "test") -> tuple[can.BusABC, can.BusABC]:
    """创建一对互相可见的 virtual 总线，用于在两进程/两对象间收发。

    python-can 的 virtual 总线在同一 channel 名上共享消息，因此两个
    Bus 对象只要 channel 相同即可互相收发。
    """
    name = f"vcan-{channel_suffix}-{int(time.time() * 1000) % 100000}"
    a = can.Bus(interface="virtual", channel=name)
    b = can.Bus(interface="virtual", channel=name)
    return a, b
