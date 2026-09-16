# ISO 26262 与 ASPICE 概念梳理

> ⚠️ **范围声明**：本文只是概念梳理，用于理解汽车软件行业的两个基础框架。
> **本工程没有、也不打算做功能安全开发**。真实的 ISO 26262 工作需要
> 危害分析、安全目标定义、ASIL 分解、安全案例等一系列流程性产物，
> 那是整车厂和 Tier1 的安全团队的工作，不是一个练习工程能覆盖的。

面试里这两个词通常只要求"了解"，但**答错方向比不知道更糟**。
下面把最容易混淆的点讲清楚。

---

## 一、ISO 26262 —— 功能安全

### 它管什么

**电气/电子系统故障导致的安全风险。** 核心问题是：
"这个东西坏了，会不会伤人？如果会，我们要求它有多可靠？"

它**不管**：
- 常规质量问题（那是 IATF 16949 / APQP 的领域）
- 预期功能安全（那是 ISO 21448 SOTIF，管"没坏但设计不足"）
- 网络安全（那是 ISO/SAE 21434）

### ASIL —— 安全完整性等级

ASIL 是 ISO 26262 的核心概念，分四档：**A < B < C < D**，D 最严。
另有一个 **QM**（Quality Management），表示没有安全要求，按常规质量管理即可。

ASIL 等级由三个维度的组合决定：

| 维度 | 取值 | 含义 |
|---|---|---|
| **S**everity 严重度 | S0–S3 | 伤害的严重程度（S0 无伤害，S3 致命） |
| **E**xposure 暴露率 | E0–E4 | 运行场景出现的概率（E0 几乎不可能，E4 高概率） |
| **C**ontrollability 可控性 | C0–C3 | 驾驶员能否及时避免伤害（C0 一般可控，C3 几乎不可控） |

S、E、C 越高，ASIL 越高。例如安全气囊触发属于典型 ASIL D，
后排车窗升降可能是 ASIL A 或 QM。

> 常见误解：**ASIL 不是给"整个 ECU"定一个等级**。
> 它是按**功能**分配的，同一个 ECU 上不同功能可以有不同 ASIL。
> 而且 ASIL 会沿着"安全目标 → 功能安全需求 → 技术安全需求"逐层分解。

### ASIL 分解（decomposition）

一个 ASIL D 的需求可以分解成两个独立的 ASIL B(D) 实现，
前提是两者**充分独立**（不共享电源、时钟、内存等）。
这是为了在成本与冗余之间取平衡。

### 开发流程：V 模型

```
概念阶段    ── 相关项定义 → 危害分析与风险评估(HARA) → 安全目标
                    │
系统阶段    ── 技术安全需求 → 系统设计 → 系统集成与测试
                    │
硬件/软件   ── 软硬件安全需求 → 设计 → 实现 → 单元测试 → 集成测试
                    │
生产运行    ── 生产、运行、维护、报废
```

ISO 26262 的结构（第二版 2018）：

| Part | 内容 |
|---|---|
| Part 3 | 概念阶段（相关项定义、HARA、安全目标） |
| Part 4 | 系统级产品开发 |
| Part 5 | 硬件级产品开发 |
| Part 6 | **软件级产品开发** ← 软件测试岗最相关 |
| Part 7 | 生产、运行、维护与报废 |
| Part 8 | 支持过程（配置管理、变更管理、验证） |
| Part 9 | ASIL 导向与安全导向分析 |

### Part 6 里与测试直接相关的要求

- 软件安全需求必须**可追溯**到技术安全需求，再追溯到安全目标
- 按 ASIL 等级要求不同的**结构覆盖率**：
  - ASIL A/B：语句覆盖、分支覆盖
  - ASIL C/D：再增加 MC/DC（修正条件判定覆盖）
- 安全机制（safety mechanism）要能检测并处理故障：
  看门狗、ECC 内存、CRC 校验、双核锁步、合理性检查

**回到本工程**：`test_isotp.py` 里的 CRC 与序号校验、
`test_uds_security.py` 里的锁定机制，思路与"安全机制 + 故障检测"是一致的，
但**本工程不构成任何 ASIL 合规证据**。

---

## 二、ASPICE —— 过程能力

### 它管什么

**开发过程本身好不好。** ISO 26262 问"产品够不够安全"，
ASPICE 问"你的开发过程能不能稳定产出合格产品"。

ASPICE = **A**utomotive **S**oftware **P**rocess **I**mprovement and
**C**apability d**E**termination。它是 VDA QMC 基于 ISO/IEC 33001 做的
汽车行业版本。整车厂通常要求 Tier1 达到 **Level 2 或 Level 3**。

### 能力等级（Capability Level）

| Level | 名称 | 含义 |
|---|---|---|
| 0 | Incomplete | 过程没执行或没达到目的 |
| 1 | Performed | 过程执行了，产出了工作产品 |
| 2 | **Managed** | 过程被**管理**：有计划、有资源、有责任人、有评审 |
| 3 | **Established** | 过程被**标准化**：有组织级标准流程，项目裁剪自标准 |
| 4 | Predictable | 过程在**量化**控制下运行 |
| 5 | Innovating | 持续改进 |

> 关键区分：**Level 1 是"做了"，Level 2 是"管着做"，Level 3 是"按标准做"。**
> 大多数公司卡在 Level 2 冲刺 Level 3。

### 主过程域

**系统（SYS）**

| 过程 | 名称 |
|---|---|
| SYS.1 | Requirements Elicitation 需求获取 |
| SYS.2 | System Requirements Analysis 系统需求分析 |
| SYS.3 | System Architectural Design 系统架构设计 |
| SYS.4 | System Integration and Integration Verification 系统集成与集成验证 |
| SYS.5 | System Verification 系统验证 |

**软件（SWE）—— V 模型左半边设计与右半边验证**

| 过程 | 名称 | V 模型位置 |
|---|---|---|
| SWE.1 | Software Requirements Analysis | 左上 |
| SWE.2 | Software Architectural Design | 左中 |
| SWE.3 | Software Detailed Design and Unit Construction | 左下 |
| SWE.4 | Software Unit Verification | 右下（单元测试） |
| SWE.5 | Software Component Verification and Integration Verification | 右中（集成测试） |
| SWE.6 | Software Verification | 右上（合格性测试） |

**支持与管理过程**

| 过程 | 名称 |
|---|---|
| SUP.1 | Quality Assurance 质量保证 |
| SUP.8 | Configuration Management 配置管理 |
| SUP.9 | Problem Resolution Management 问题解决管理 |
| SUP.10 | Change Request Management 变更请求管理 |
| MAN.3 | Project Management 项目管理 |

### 测试岗最需要记住的两条

1. **双向可追溯性（bidirectional traceability）**
   需求 ↔ 架构 ↔ 详细设计 ↔ 测试用例，必须双向可追溯。
   测试用例要能回答"你为什么要测这条"，需求要能回答"这条需求谁在验"。
   **没有追溯矩阵，ASPICE 评审直接不过。**

2. **测试层级的划分**
   单元测试（SWE.4）测函数级、集成测试（SWE.5）测接口与组件协作、
   合格性测试（SWE.6）验证软件需求。
   同一个功能在不同层级测不同的东西，**不能拿集成测试替代单元测试**。

---

## 三、ISO 26262 与 ASPICE 的关系

这两个框架经常被混为一谈，实际是**互补**的：

| | ISO 26262 | ASPICE |
|---|---|---|
| 关注点 | 产品**安全性** | 过程**能力** |
| 问题 | "会不会伤人？" | "过程稳不稳？" |
| 强制性 | 法规/合同要求，必须合规 | 客户（整车厂）要求 |
| 产出 | 安全案例、安全目标、ASIL 分解 | 过程证据、追溯矩阵、评审记录 |
| 与测试 | 提出**覆盖率**与**安全机制**要求 | 提出**过程与追溯性**要求 |

实践中两者会同时落到一个项目上：整车厂既要求 Tier1 达到 ASPICE Level 2，
又要求交付满足 ISO 26262 的安全案例。**ASPICE 的过程能力是达成
ISO 26262 合规的基础设施。**

---

## 四、面试里怎么答

如果被问到"你了解 ISO 26262 吗"，一个有分寸的回答是：

> "了解基本框架。ISO 26262 是功能安全标准，核心是把危害按严重度、
> 暴露率和可控性定出 ASIL 等级，A 到 D 逐级加严，再沿安全目标、
> 功能安全需求、技术安全需求逐层分解。对软件测试来说，ASIL 等级
> 直接决定要求的结构覆盖率——A/B 级要分支覆盖，C/D 级要 MC/DC。
>
> 我没有参与过真正的功能安全项目，这些是自学标准和做协议栈实现时
> 接触到的。我实际做过的是 CAN 总线与 UDS 诊断的测试，包括正常流程、
> 边界值和各种异常路径的用例设计，也用 pytest 做成了可回归的自动化。
> 如果贵司有 ISO 26262 的项目，我希望能在实际项目里补齐流程性的部分。"

**这样的回答比硬说"我熟悉 ISO 26262"安全得多**，也更容易被继续追问
你真正会的部分（测试用例设计、自动化）。

---

## 五、参考

- ISO 26262:2018 — Road vehicles — Functional safety（Part 1–12）
- ISO 21448:2022 — Safety of the intended functionality (SOTIF)
- Automotive SPICE Process Assessment Model (PAM) v4.0, VDA QMC
- ISO/IEC 33001 — Process assessment 基础标准
