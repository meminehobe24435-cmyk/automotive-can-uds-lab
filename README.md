# automotive-can-uds-lab

车载 CAN 总线通信与 UDS 诊断的动手实践工程：**手写 ISO-TP 传输层 + 手写 UDS 诊断栈**，
配一套可自动化回归的测试用例，全部在虚拟总线上跑通，不需要任何硬件。

```
$ pytest
159 passed in 6.93s
```

---

## 这个项目解决什么问题

车载软件的测试岗，日常面对的是这几件事：

- 报文周期对不对（设计 10 ms，实测抖多少）
- 信号范围合不合规（越界值 ECU 会不会正确处理）
- 诊断服务能不能通（UDS 请求的肯定/否定响应是否符合 ISO 14229）
- 异常路径兜不兜得住（序号错乱、超时、缓冲溢出、安全访问锁定）

这些事情的共同点是：**协议本身是确定的，但只有自己实现过一遍，才知道每一字节为什么在那儿。**
所以本工程不用 `udsoncan`、不用 `can-isotp`，ISO-TP 和 UDS 全部手写，
只用 `python-can` 做 socket 封装、`cantools` 做 DBC 解析。

---

## 快速开始

### 方式一：Linux / WSL2（推荐，用真实 SocketCAN）

```bash
sudo ./scripts/run_tests.sh
```

脚本会依次装依赖、`modprobe vcan`、建 `vcan0`、跑全部测试。

手动做也可以：

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0

pip install -r requirements.txt
python3 scripts/smoke.py     # 端到端演示
pytest -v                    # 全部测试
```

### 方式二：Windows / macOS（虚拟总线，零依赖）

```bash
pip install -r requirements.txt
set CAN_INTERFACE=virtual     # Windows
export CAN_INTERFACE=virtual  # macOS / Linux
pytest -q
```

`python-can` 的 `virtual` 接口是进程内回环，行为与真实 CAN 一致
（同样的帧格式、同样的"发送方收不到自己的帧"语义），但没有电气层，
因此不会产生位错误和仲裁冲突。适合开发与 CI。

### 方式三：接真实硬件

装好 PCAN / Vector / Kvaser 驱动后，改一个环境变量即可，上层代码零改动：

```bash
export CAN_INTERFACE=pcan
export CAN_CHANNEL=PCAN_USBBUS1
pytest
```

---

## 端到端演示

```bash
python3 scripts/smoke.py
```

输出（节选）：

```
1. 环境
python-can 4.6.1
platform   linux

2. 总线
vcan0 available: True
resolved       : socketcan:vcan0 @ 500 kbit/s

3. DBC
messages: ['EngineData', 'VehicleStatus', 'BatteryData', 'DoorStatus', ...]
encoded : id=0x100 data=80 25 52 1c 80 55 e2 ff
decoded : {'EngineSpeed': 2400.0, 'VehicleSpeed': 72.5, 'CoolantTemp': 88, ...}

4. ISO-TP + UDS 端到端
0x10 extended  -> 50 03 00 32 01 f4
0x22 F190 VIN  -> LSNHBE1A2M0000001
0x27 security  -> unlocked
0x19 DTCs      -> ['0xa001', '0xb102', '0xc203']
multi-frame    -> 49 bytes: 62 f1 87 33 45 43 ...   ← 4 个 DID 一次读，ISO-TP 分段重组
ISO-TP stats   -> {'tx_frames': 10, 'rx_frames': 20, 'flow_control_sent': 3, 'timeouts': 0}
```

---

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  tests/          159 个用例：协议 / 信号 / 服务 / 异常路径  │
├──────────────────────────────────────────────────────────┤
│  src/can_monitor.py   报文周期 · 总线负载 · 丢帧检测         │
│  src/dbc_codec.py     DBC 编解码 · 信号范围校验             │
├──────────────────────────────────────────────────────────┤
│  src/uds.py           ISO 14229：会话 / 安全访问 / DTC      │
│                       ECU 服务端模拟 + Tester 客户端        │
├──────────────────────────────────────────────────────────┤
│  src/isotp.py         ISO 15765-2：SF / FF / CF / FC       │
│                       分段 · 重组 · 流控 · STmin            │
├──────────────────────────────────────────────────────────┤
│  src/bus.py           SocketCAN(vcan) / virtual / 真实硬件  │
└──────────────────────────────────────────────────────────┘
```

---

## 覆盖的协议点

### ISO 15765-2 (ISO-TP) 传输层

| 帧类型 | PCI | 本项目实现 |
|---|---|---|
| 单帧 SF | `0x0L` | 净荷 ≤ 7 字节，L 为长度 |
| 首帧 FF | `0x1H 0xLL` | 12 位总长度，携带前 6 字节 |
| 连续帧 CF | `0x2N` | 序号 0..15 循环，每帧 6 字节 |
| 流控帧 FC | `0x3F BS STmin` | CTS / WAIT / OVFLW 三种流状态 |

- BlockSize 分块流控（`BS=0` 表示一次放行）
- STmin 换算：`0x00-0x7F` 毫秒档、`0xF1-0xF9` 微秒档
- 异常处理：序号错乱、超时、缓冲溢出（OVFLW）

### ISO 14229 (UDS) 诊断服务

| SID | 服务 | 说明 |
|---|---|---|
| 0x10 | DiagnosticSessionControl | default / programming / extended，带 P2、P2* 时间参数 |
| 0x11 | ECUReset | hard / keyOffOn / soft，复位后清会话与安全状态 |
| 0x14 | ClearDiagnosticInformation | 清 DTC |
| 0x19 | ReadDTCInformation | 按状态掩码读 DTC |
| 0x22 | ReadDataByIdentifier | 支持一次读多个 DID，自动走多帧 |
| 0x27 | SecurityAccess | 种子-密钥，3 次失败锁定 |
| 0x28 | CommunicationControl | 通信使能控制 |
| 0x2E | WriteDataByIdentifier | 受会话 + 安全等级双重门控 |
| 0x31 | RoutineControl | start / stop / requestResults |
| 0x3E | TesterPresent | 含 suppressPosRspMsgIndicationBit |
| 0x7F | NegativeResponse | 完整 NRC 码表 |

关键机制：

- **S3 定时器**：默认 5 s 无 TesterPresent 自动退回默认会话并清除解锁状态
- **0x78 响应挂起**：慢服务先回 pending，客户端透明等待最终响应
- **NRC 门控顺序**：会话检查先于安全等级检查（默认会话回 `0x7F` 而不是 `0x33`）
- **只读 DID 保护**：VIN 等标识类数据即使解锁也拒绝写入

---

## 测试覆盖

| 测试文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `test_isotp.py` | 47 | PCI 编解码、STmin、多帧重组、序号回绕、异常路径、真实 vcan |
| `test_dbc_codec.py` | 25 | 信号位布局、分辨率换算、枚举翻译、范围校验、边界值 |
| `test_uds_services.py` | 49 | 各服务的肯定响应 + NRC、会话门控、长度校验、0x78 挂起 |
| `test_uds_security.py` | 24 | 种子-密钥、错误密钥、顺序错误、锁定与解锁生命周期 |
| `test_can_monitor.py` | 14 | 帧位长、周期测量、抖动、总线负载、丢帧检测 |

测试设计上的一个要点写在 `tests/conftest.py` 里：**ISO-TP 多帧是握手协议**，
发送方发出首帧后必须等接收方回流控帧，因此如果在同一个线程里先 `send()` 再 `recv()`
会直接死锁。测试里必须把接收侧放到后台线程。真实场景不会死锁，因为 ECU 和
诊断仪是两个独立节点，天然并发。

---

## 与 AUTOSAR 的对应关系

本工程的模块划分刻意对齐 AUTOSAR Classic Platform 的 CAN 通信栈，
便于迁移理解：

| 本工程 | AUTOSAR CP | 职责 |
|---|---|---|
| `src/bus.py` | Can Driver / CanIf | 硬件抽象、报文收发 |
| `src/isotp.py` | CanTp | 分段、重组、流控（ISO 15765-2） |
| `src/uds.py` | Dcm / Dem | 诊断服务调度、DTC 管理（ISO 14229） |
| `src/dbc_codec.py` | Com | 信号打包解包、物理值换算 |
| `src/can_monitor.py` | — | 测试观测，不属协议栈 |

详见 `docs/autosar-can-stack.md`。

---

## 目录结构

```
automotive-can-uds-lab/
├── README.md
├── requirements.txt
├── pytest.ini
├── dbc/
│   └── demo_ecu.dbc            自研 DBC：6 条报文、25 个信号
├── src/
│   ├── bus.py                  总线初始化（SocketCAN / virtual / 硬件）
│   ├── isotp.py                ISO 15765-2 传输层
│   ├── uds.py                  ISO 14229 诊断栈
│   ├── dbc_codec.py            DBC 编解码与校验
│   └── can_monitor.py          报文周期与总线负载监控
├── tests/                      159 个 pytest 用例
├── scripts/
│   ├── setup_vcan.sh           建 vcan 接口
│   ├── run_tests.sh            一键装依赖 + 建 vcan + 跑测试
│   ├── smoke.py                端到端演示
│   └── trace_debug.py          总线嗅探器（排查协议问题用）
├── docs/
│   ├── interview-qa.md         面试问答梳理
│   ├── autosar-can-stack.md    AUTOSAR 通信栈分层
│   └── iso26262-aspice.md      功能安全与过程模型
└── .github/workflows/ci.yml    CI：真实 vcan 跑 + 虚拟总线跑
```

---

## 已知边界（如实声明）

这个工程是一个**自建的协议实现与测试夹具**，不是真实 ECU 的台架测试。明确边界：

- **被测对象是软件模拟 ECU**，不是量产 ECU。协议行为按 ISO 标准实现，
  但不代表任何具体车型/ECU 的实现细节。
- **没有硬件在环**。vcan 没有电气层，因此不覆盖位错误、仲裁冲突、
  终端电阻、EMC 等物理层问题。
- **没有覆盖的功能安全流程**。ISO 26262 要求的危害分析、安全目标、
  ASIL 分解、安全案例等工作**不在本工程范围内**，`docs/iso26262-aspice.md`
  只做概念梳理。
- **UDS 服务是子集**。刷写流程（0x34 RequestDownload / 0x36 TransferData /
  0x37 RequestTransferExit）、0x23 ReadMemoryByAddress、0x2A/0x2C 周期读等
  未实现。
- **安全访问算法是演示用的**。真实项目的种子-密钥算法由主机厂保密，
  本工程用可解释的位运算版本，目的是跑通完整流程。
- **CAN FD 未覆盖**。当前实现按经典 CAN（8 字节净荷）编写；
  ISO-TP 的 CAN FD 扩展（转义长度、64 字节净荷）未实现。
- **DBC 是自研示例**，不是任何真实车型的通信矩阵。

---

## 参考标准

- ISO 11898-1:2015 — CAN 数据链路层与物理层
- ISO 15765-2:2016 — CAN 上的诊断传输层（ISO-TP）
- ISO 14229-1:2020 — 统一诊断服务（UDS）
- ISO 15031-5 / SAE J1979 — OBD-II 诊断服务
- AUTOSAR Classic Platform — CAN Communication Stack 规范

---

## 许可

MIT
