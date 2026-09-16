"""automotive-can-uds-lab: 车载 CAN 总线测试与 UDS 诊断实践。

模块划分：
    bus          —— CAN 总线初始化（SocketCAN/vcan 优先，跨平台回退 virtual）
    isotp        —— ISO 15765-2 (ISO-TP) 传输层，手写实现 SF/FF/CF/FC
    uds          —— ISO 14229 (UDS) 诊断服务，服务端 ECU 模拟 + 客户端
    dbc_codec    —— DBC 报文/信号编解码与信号范围校验
    can_monitor  —— 总线报文监控与负载统计
"""

__version__ = "1.0.0"
