# AUTOSAR Classic Platform 通信栈梳理

本文把本工程的手写实现对应到 AUTOSAR CP 的标准分层，目的是理解
"量产项目里的代码长什么样"。**本工程不是 AUTOSAR 实现**，也不使用 RTE，
只是模块职责刻意对齐，便于迁移理解。

---

## 一、AUTOSAR 的整体分层

```
┌─────────────────────────────────────────────────────────────┐
│  Application Layer                                          │
│  SWC (Software Component) — 应用软件组件，只关心业务逻辑      │
├─────────────────────────────────────────────────────────────┤
│  RTE (Runtime Environment)                                  │
│  虚拟功能总线 VFB 的落地实现：SWC 之间、SWC 与 BSW 之间的通信 │
│  对 SWC 屏蔽"对端在同一个 ECU 还是别的 ECU"                  │
├─────────────────────────────────────────────────────────────┤
│  BSW (Basic Software)                                       │
│  ┌───────────────────────┬─────────────────────────────┐    │
│  │ Services Layer        │ 系统/内存/诊断/通信服务      │    │
│  │ (Dcm, Dem, Com, ...)  │                             │    │
│  ├───────────────────────┼─────────────────────────────┤    │
│  │ ECU Abstraction       │ 把 MCU 外设抽象成统一接口    │    │
│  │ (CanIf, IoHwAb, ...)  │                             │    │
│  ├───────────────────────┼─────────────────────────────┤    │
│  │ MCAL (微控制器抽象层)  │ Can, Port, Dio, Adc, ...    │    │
│  └───────────────────────┴─────────────────────────────┘    │
├─────────────────────────────────────────────────────────────┤
│  Microcontroller Hardware                                   │
└─────────────────────────────────────────────────────────────┘
```

关键点：**越往下越依赖具体芯片，越往上越与硬件无关。**
换一颗 MCU，主要改 MCAL；上层 Com / Dcm / Dem 基本不动。

---

## 二、CAN 通信栈的数据流

### 发送方向（应用要发一条报文）

```
SWC
 │  Rte_Write_<port>_<data>()
 ▼
RTE
 │
 ▼
Com                          ← 把信号打包成 PDU，处理发送模式（周期/事件）
 │  PduR_ComTransmit()
 ▼
PduR (PDU Router)            ← 路由：这条 PDU 该走 CAN 还是 LIN？给谁？
 │  CanIf_Transmit()
 ▼
CanIf (CAN Interface)        ← 把 PDU 交给某个 CAN 控制器；HOH 句柄管理
 │  Can_Write()
 ▼
Can Driver (MCAL)            ← 写硬件寄存器 / 投递到发送邮箱
 ▼
CAN Controller → 总线
```

### 接收方向（总线上来一条报文）

```
CAN Controller → 中断
 ▼
Can Driver                   ← 读取邮箱，回调 CanIf_RxIndication
 ▼
CanIf                        ← 按 HRH 过滤，决定上交给谁
 ▼
PduR
 ▼
Com                          ← 按 DBC/ARXML 解包成信号
 ▼
RTE
 ▼
SWC                          ← 应用拿到物理值
```

### 诊断方向（UDS）

```
诊断仪 (Tester)
 │  0x7E0 请求
 ▼
Can Driver → CanIf
 ▼
CanTp                        ← ISO-TP：多帧重组（本工程 src/isotp.py）
 ▼
PduR
 ▼
Dcm (Diagnostic Communication Manager)
 │  ← 服务分发（本工程 src/uds.py 的 _dispatch）
 │  ← 会话状态机 / 安全等级 / S3 定时器
 ▼
  0x7E8 响应
```

---

## 三、模块对照表

| 本工程模块 | AUTOSAR CP 模块 | 职责 | 本工程实现位置 |
|---|---|---|---|
| `src/bus.py` | Can Driver + CanIf | 硬件抽象、报文收发、ID 过滤 | `create_bus` / `_recv_frame` |
| `src/isotp.py` | **CanTp** | ISO 15765-2 分段、重组、流控 | 全部 |
| `src/uds.py` (服务分发) | **Dcm** | UDS 服务调度、会话、安全、S3 | `UdsServer._dispatch` |
| `src/uds.py` (DTC) | **Dem** | 故障事件与 DTC 管理 | `_svc_read_dtc` / `_svc_clear_dtc` |
| `src/dbc_codec.py` | **Com** | 信号打包解包、物理值换算 | `encode` / `decode` |
| `src/can_monitor.py` | —（测试观测） | 周期与负载统计，不属于协议栈 | 全部 |
| `dbc/demo_ecu.dbc` | ARXML 通信矩阵 | 报文与信号定义（AUTOSAR 用 ARXML） | — |

一个直观的差别：**DBC 管通信，ARXML 管一切。**
AUTOSAR 项目里通信矩阵、SWC 接口、ECU 资源配置都写在 ARXML 里，
由工具链（DaVinci / EB tresos）生成代码。DBC 只描述报文层，
是测试和标定工具的通用格式，两种格式在测试岗都会碰到。

---

## 四、与本工程测试用例的对应

| AUTOSAR 概念 | 本工程对应测试 |
|---|---|
| CanTp 分段重组 | `test_isotp.py::TestRoundTrip` |
| CanTp 流控（BlockSize / STmin） | `test_block_size_forces_multiple_flow_controls` |
| CanTp 错误处理 | `test_sequence_error_detected` / `test_flow_control_overflow` |
| Dcm 会话状态机 | `test_uds_services.py::TestSessionControl` |
| Dcm 安全等级 | `test_uds_security.py` |
| Dcm S3 定时器 | `test_session_drops_after_s3_timeout` |
| Dcm NRC 优先级 | `test_write_services_blocked_in_default_session` |
| Dem DTC 状态位 | `test_read_dtcs_with_status_mask` |
| Com 信号换算 | `test_dbc_codec.py::TestSignalSpecs` |

---

## 五、诊断相关的几个"反常识"点

这几个是面试容易踩坑的地方：

1. **NRC 是有优先级的。** 一条请求同时满足"会话不对"和"没解锁"时，
   先回哪个？AUTOSAR Dcm 和 ISO 14229 的惯例是**先检查会话，再检查安全**。
   所以默认会话下访问写服务得到的是 `0x7F`（当前会话不支持），
   不是 `0x33`（安全拒绝）。本工程的 `_dispatch` 按这个顺序实现，
   并有 `test_write_services_blocked_in_default_session` 守住它。

2. **`0x78` 是肯定响应的"占位"，不是失败。** ECU 处理慢服务时先回
   `7F <SID> 78`，诊断仪必须继续等最终响应。把 `0x78` 当错误处理会导致
   误判。注意它是 NRC，格式上属于否定响应帧。

3. **流控帧和报文不是一回事。** ISO-TP 的 FC 帧不带 UDS 数据，
   接收方在解析"报文"时必须跳过 FC，否则会把 `0x30` 当成服务号，
   或者更隐蔽地——把首帧的长度低字节当成 SID。本工程在实跑时踩过这个坑，
   见 `test_flow_control_frames_are_skipped_on_recv` 的注释。

4. **ISO-TP 是握手协议，收发必须并发。** 发送方发出首帧后阻塞等 FC，
   如果对端还没开始收，就是死锁。真实 ECU 不会死锁是因为它常驻接收；
   仿真/测试时必须显式并发，见 `tests/conftest.py::BackgroundReceiver`。

---

## 六、CAN FD 的差异（本工程未覆盖）

| 项 | 经典 CAN | CAN FD |
|---|---|---|
| 净荷上限 | 8 字节 | 64 字节 |
| 数据段速率 | 与仲裁段相同 | 可切换更高波特率 (BRS) |
| ISO-TP 单帧上限 | 7 字节 | 62 字节 |
| ISO-TP 长度编码 | 12 位 | 超过 4095 用转义格式 (32 位) |
| CRC | 15 位 | 17 位（≤16 字节）/ 21 位 |
| 帧格式 | 标准帧 | 新增 FDF / BRS / ESI 位 |

迁移到 CAN FD 时，ISO-TP 的改动集中在：长度编码（转义）、
单帧净荷上限（62）、以及 DLC 到字节数的映射（12/16/20/24/32/48/64）。
