# R&S RTB2000 系列 SCPI 实测参考

所有响应均为 **2026-11-07 在本机 RTB2004（固件 02.202）实测原文**，不是抄手册。

仪器：`Rohde&Schwarz,RTB2004,1333.1005k04/113121,02.202`
连接：`TCPIP::169.254.101.84::5025::SOCKET`（原始套接字，`\n` 结尾）

---

## 1. 端口扫描结果

| 端口 | 状态 | 用途 |
|------|------|------|
| 80 | 开放，`HTTP/1.1 200 OK` | Web 界面（含仪器控制） |
| 111 | 开放 | ONC RPC portmapper（VXI-11 发现） |
| 1024 | 开放 | VXI-11 核心通道（示波器页面标注值） |
| 5025 | 开放 | **SCPI 原始套接字（本文档主用）** |
| 4880 | ConnectionRefused | HiSLIP 未启用 |
| 5024 | ConnectionRefused | 备用原始套接字未启用 |
| 21 | ConnectionRefused | FTP 未启用 |
| 5900 | ConnectionRefused | VNC 未启用 |

仪器页面上的 VISA 串：`TCPIP::169.254.101.84::INSTR`（VXI-11 方式，需 pyvisa）。

## 2. 身份与状态

| 命令 | 实测响应 | 说明 |
|------|----------|------|
| `*IDN?` | `Rohde&Schwarz,RTB2004,1333.1005k04/113121,02.202` | 厂商,型号,序列号,固件 |
| `ACQuire:STATe?` | `RUN` / `BRE` / `STOP` | `BRE` 是 BREak，等同冻结 |
| `ACQuire:POINts?` | `18750068` | **存储深度**（最长记录长度），非当前点数 |
| `ACQuire:SRATe?` | `3.05E+05` | 当前采样率（随时基变化） |
| `FORMat:DATA?` | `ASC,0` | 默认 ASCII 输出 |
| `FORMat:BORDer?` | `MSBF` | 二进制大端 |
| `TIMebase:SCALe?` | `5.00E-03` | 时基 s/div |
| `TIMebase:POSition?` | `0.00E+00` | 水平位置 s |

## 3. 通道设置

| 命令 | 实测响应 | 说明 |
|------|----------|------|
| `CHANnel1:STATe?` | `1` | 0=关 1=开 |
| `CHANnel1:SCALe?` | `5.00E+00` | 垂直档位 V/div |
| `CHANnel1:OFFSet?` | `0.00E+00` | 垂直偏移 V |
| `CHANnel1:COUPling?` | `DCL` | DC 耦合，低阻？实测 DCL |
| `CHANnel1:DATA:POINts?` | `18304` | **当前记录长度（真实点数）** |

## 4. 波形数据（核心）

### 4.1 头信息

```
CHANnel1:DATA:HEADer?      →  -2.998927E-02,2.998600E-02,18304,1
```

字段含义：`XStart[s], XStop[s], 点数, 记录号`

由 `XStart/XStop/点数` 可推算采样间隔：

```
dt = (XStop - XStart) / (N - 1)
```

实测 `N=18304`、`dt=3.2768e-06 s` → `1/dt = 305,176 Sa/s`，与 `ACQuire:SRATe?` 的 `3.05E+05` 一致。

### 4.2 采样数据

```
CHANnel1:DATA?             →  -1.443634E-01,-1.458893E-01,-1.497040E-01,...
```

- 逗号分隔 ASCII，**单位伏特**，末尾可能带逗号需 strip。
- 一条响应最长可达 1.5 MB，**中间没有换行**，必须读到 `\n` 为止。
- **必须先 `STOP`**，`RUN` 状态下无响应（一直等到超时）。

### 4.3 单位验证（怎么确认是伏特而不是格数）

| 条件 | 实测 |
|------|------|
| 垂直档位 0.5 V/div | 量化步长 4.8828 mV |
| 垂直档位 1.0 V/div（STOP 后改档只是重新显示，数据不变） | 同上 |
| 理论值 | `0.5 / 4.8828e-3 = 102.4` 码/格 → 10 bit ADC 满 10 格 1024 码 ✅ |

结论：数值 = 电压（V），量化步长 = `档位 ÷ 102.4`。

## 5. 屏幕截图

```
HCOPy:LANGuage?            →  PNG
HCOPy:DATA?                →  IEEE 488.2 块：#<位数><长度><PNG 字节>
```

块解析：首字节 `#`，第 2 字节是长度字段的**位数**（如 `#8000024444`），
随后该位数表示数据长度，再往后就是数据本体。

实测：24,444 字节，耗时 0.93 s，头部 `89 50 4E 47 0D 0A 1A 0A`。

## 6. 文件系统（内存储）

```
MMEMory:CDIRectory?               →  "/INT"
MMEMory:CATalog? "/INT"           →  1636800,387479664,"DEVICON.FIL,,113846","README.TXT,,181"
```

返回格式：`总容量,剩余容量,"文件名,标志,大小",...`

## 7. 明确不存在的命令（返回 `-113,"Undefined header"`）

以下命令在本机固件上**不存在**，别浪费时间：

| 命令 | 结果 |
|------|------|
| `CHANnel1:DATA:VALues?` | -113 Undefined header |
| `CHANnel1:DATA:FORMat?` | -113 Undefined header |
| `CHANnel1:WAVeform:DATA?` | -113 Undefined header |
| `CHANnel1:WAVeform:VALues?` | -113 Undefined header |
| `EXPort:WAVeform:FORMat?` | -113 Undefined header |
| `ACQuire:DATA?` | -113 Undefined header |

存在但**不建议**用于取波形：

| 命令 | 实测 | 为什么不用 |
|------|------|-----------|
| `TRACe1:DATA?` | 块数据 125,000 字节，首字节 `0x85` | 是**屏幕像素轨迹**，不是电压采样 |
| `EXPort:WAVeform:DATA?` | `9.76562E-05,-1.45508E-02,...`（1,569,052 字符） | 顺序不是时间序，疑似按像素列 |
| `EXPort:WAVeform:SOURce?` | `CH1` | 存在，但配套的数据命令顺序不可控 |

## 8. 错误查询

```
SYSTem:ERRor?   →  0,"No error"        # 队列空
                →  -113,"Undefined header"   # 命令拼错或该固件不支持
```

建议每批命令后查一次，能快速区分"命令不存在"与"命令执行了但没数据"。
注意：查询本身也会入队，需要在读取后再次查询直到 `0,"No error"`。

## 9. 运行模式与触发（实测）

### 9.1 两种运行模式

| 命令 | 作用 | 实测 |
|------|------|------|
| `RUNContinous` | 连续运行（同面板 Run/Stop 的 Run） | 之后 `ACQuire:STATe?` = `RUN` |
| `RUNSingle` | 单次采集：等一个触发事件后停下 | 0.3 s 内 `ACQuire:STATe?` = **`COMP`** |
| `STOP` | 冻结当前画面 | 状态 = `STOP` |
| `ACQuire:STATe?` | 读状态 | 取值 `RUN` / `BRE` / `STOP` / `COMP` |

`RUNContinous` / `RUNSingle` **不能加 `?`**：查询会返回 `-510,"Such a query not allowed"`。

### 9.2 触发设置

| 命令 | 实测响应 | 说明 |
|------|----------|------|
| `TRIGger:A:TYPe?` | `EDGE` | 触发类型 |
| `TRIGger:A:SOURce?` | `CH1` | 触发源 |
| `TRIGger:A:MODE?` | `AUTO` | 写 `AUTO` / `NORMal`，**回读为 `AUTO` / `NORM`** |
| `TRIGger:A:EDGE:SLOPe?` | `POS` | 写 `POS` / `NEG` |
| `TRIGger:A:LEVel?` | `0.00E+00` | 写伏特数值，回读 `1.0000E+00` 形式 |
| `TRIGger:A:LEVel1?` / `LEVel2?` | `0.00E+00` | 分源电平，存在 |
| `TRIGger:A:HOLDoff:TIME?` | `5.1200000684E-08` | 保持时间 |

不存在（`-113`）：`TRIGger:A:EDGE:SOURce?`、`TRIGger:A:AUTolevel?`、`TRIGger:STATe?`、`TRIGger:SWEep?`。

`TRIGger:A:FINDlevel` 只能作为设置命令（带 `?` 返回 `-510`），但实测执行后电平变成
**0 V**，对双极性/单极性信号都不理想 —— 自己算 `(P5 + P95) / 2` 更可靠。

### 9.3 采集模式（会影响采样率）

| 命令 | 实测 |
|------|------|
| `ACQuire:MODE?` | `RTIM`（实时采集）|
| `ACQuire:TYPE?` | `REFR`（刷新 / 高分辨率模式）|

## 10. 连接和查询故障

- 用仪器 LAN 页面或 `python scripts/scope.py discover` 确认当前 IP。上面的 `169.254.101.84` 是一次实测地址，不是默认地址；Auto-IP/DHCP 可能改变它。
- 电脑与仪器需网络可达。直连且仪器使用 `169.254.x.x/16` 时，电脑网卡也需要同一网段地址。先检查本机地址与网线，再检查 TCP 5025。
- `STOP` 和 `RUNContinous` 是写命令，不会返回一行数据。不要在发送 `STOP` 后直接调用 `recv()`；仅对以 `?` 结尾的查询读取响应。波形读取和状态恢复应使用 `scripts/scope.py` 的已验证实现。
- `CHANnel<n>:DATA?` 在运行状态可能不返回；先冻结，再读取。查询超时后清空迟到响应，再继续查询，避免后续结果错位。
- `ACQuire:POINts?` 是配置的存储深度；当前记录点数以 `CHANnel<n>:DATA:HEADer?` 和实际返回值为准。
