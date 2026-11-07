---
name: scope
description: scope ｜ 示波器通用控制技能。用网线（以太网 SCPI，TCP 5025）连接并控制 R&S RTB2000 系列示波器：抓屏幕截图、采集波形并固定输出「CSV + 波形图」、离线分析频率/占空比/边沿时间、任意 SCPI 直通。触发词：scope、示波器、示波器截图、示波器抓屏、导出示波器波形、波形导 CSV、网线连示波器、SCPI 控制示波器、RTB2004/RTB2002 连电脑、示波器存不进 U 盘。零第三方依赖（matplotlib 可选，缺失时自动降级），不需要 pyvisa / NI-VISA / 驱动的。
keywords: [scope, 示波器, oscilloscope, SCPI, RTB2000, RTB2004, waveform, csv]
---

# 示波器以太网控制（R&S RTB2000 系列）

用一根网线把示波器和电脑直连，通过 SCPI over TCP（5025 端口）抓截图、导波形 CSV。
**不需要 U 盘**，也**不需要 pyvisa / NI-VISA / 任何第三方库**。

> 引擎要求：Python 3.9+（本机 `F:\anaconda3\python.exe`，3.14.x）。脚本只用标准库，
> 换电脑、换 Python 版本、换平台（Windows / Linux / macOS）都能直接跑。

## 跨 AI 工具使用（通用 skill）

本目录是**工具中立**的：同一份 `scripts/scope.py` 可被任何 AI 工具或人直接调用，不绑定 CodeBuddy。

| 文件 | 谁读它 |
|------|--------|
| `SKILL.md` | CodeBuddy / Claude（YAML frontmatter + 技能正文）|
| `AGENTS.md` | Codex 及其他读取仓库级 AGENTS.md 的工具 |
| `README.md` | 人、以及任何没约定入口文件的工具 |
| `scope.json` | 需要机器可读清单的程序（名称/关键词/入口/子命令）|
| `scripts/scope.py` | **唯一执行入口**，跨平台、零硬编码路径 |

任何工具只需要一样东西就能用：`python <此目录>/scripts/scope.py --help`。
仪器地址用 `--ip` 或环境变量 `SCOPE_IP` 传入，脚本里没有任何写死的 IP、路径或账号。

## 快速开始

```bash
# 1. 找仪器（同一广播域内）
python scope.py discover --cidr 169.254.0.0/16

# 2. 确认身份与通道状态
python scope.py --ip 169.254.101.84 idn

# 3. 抓屏幕截图
python scope.py --ip 169.254.101.84 shot --out ./scope_out

# 4. 采集波形 → 固定输出 CSV + 波形图（+ JSON）
#    4a 连续运行模式（默认）
python scope.py --ip 169.254.101.84 csv --mode continuous --channels 1 --out ./scope_out
#    4b 边沿触发单次采集（Auto / Normal 触发）
python scope.py --ip 169.254.101.84 csv --mode trigger --trig-slope rise --trig-mode normal
#    4c 只截取 10 个完整周期（自动扫时基提高采样率）
python scope.py --ip 169.254.101.84 csv --mode trigger --periods 10 --out ./scope_out
#    4d 直接指定时基（改采样率/分辨率，不用手拧旋钮）
python scope.py --ip 169.254.101.84 csv --timebase 20us --out ./scope_out

# 5. 只看统计，不落盘
python scope.py --ip 169.254.101.84 stats --channels 1,2

# 6. 分析已导出的 CSV（离线，不连仪器，不需要 --ip）
python scope.py analyze ./scope_out/wave_20261107_185624.csv

# 7. 任意 SCPI 直通
python scope.py --ip 169.254.101.84 scpi "CHANnel1:SCALe?"
```

仪器地址优先级：`--ip` > 环境变量 `SCOPE_IP` > `discover` 的结果。端口默认 5025，
可用 `--port` 或 `SCOPE_PORT` 覆盖。

## 能力一览（均已实机验证）

| 子命令 | 功能 | 实现方式 | 实测 |
|--------|------|----------|------|
| `idn` | 读标识、存储点数、采样率、时基、各通道档位 | `*IDN?` `ACQuire:POINts?` `ACQuire:SRATe?` `TIMebase:SCALe?` `CHANnel<n>:SCALe?` | ✅ |
| `shot` | 抓当前屏幕 PNG，带时间戳 | `HCOPy:LANGuage PNG` + `HCOPy:DATA?` | ✅ 24 KB / 0.93 s |
| `csv` | **采集波形 → 固定输出 CSV + 波形图 PNG（+ JSON）**；支持连续运行 / 触发单次两种模式、可限定 N 个周期 | `RUNContinous` / `RUNSingle` + `TRIGger:A:*` + `CHANnel<n>:DATA:HEADer?` + `CHANnel<n>:DATA?` | ✅ 两种模式均实拍验证 |
| `stats` | 只算 min/max/Vpp/RMS/mean，不落盘 | 同上，本地统计 | ✅ |
| `analyze` | 离线分析 CSV：低/高电平、Vpp、频率、占空比、上升/下降时间、周期抖动 | 纯本地计算，不需要连仪器 | ✅ 实测 1 kHz 方波：999.9879 Hz / 50.00 % |
| `watch` | **常驻等待**（探头没接时用）：先武装触发电平，信号越过电平就自动冻结、截图、导出 CSV/波形图，再重新武装 | `TRIGger:A:MODE NORMal` + `RUN` 预武装 + `MEASurement<n>:RESult?` 轮询 + `STOP` | ✅ 实拍验证 |
| `scpi` | 任意 SCPI 直通（`?` 结尾读响应） | 原始套接字 | ✅ |
| `discover` | 扫网段找仪器并回读 `*IDN?` | TCP 5025 并发探测 | ✅ |

## 两种运行模式

| 模式 | 参数 | 仪器行为 | 适用 |
|------|------|----------|------|
| **连续运行** | `--mode continuous`（默认） | `RUNContinous` 跑 `--settle` 秒（默认 1.2 s）→ `STOP` 后读数据 | 看整体波形、测频率/占空比 |
| **触发单次** | `--mode trigger` | 配置边沿触发 → `RUNSingle` → 等状态到 `COMP` → 读数据 | 要稳定的相位起点、周期对齐、统计抖动 |

触发模式补充参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `--trig-source` | `CH1` | 触发源 |
| `--trig-slope` | `rise` | `rise`=上升沿 / `fall`=下降沿 |
| `--trig-mode` | `auto` | `auto`=AUTO（无触发也刷新）/ `normal`=NORMal（只认真实触发）|
| `--trig-level` | 自动 | 不给时先抓一帧，取信号 5%/95% 分位的中点；比仪器 `FINDlevel` 稳（实测 FINDlevel 会给出 0 V）|
| `--trig-wait` | `10` | 等触发超时秒数，超时按当前画面导出并告警 |

实测（1 kHz 方波，`--periods 10`）：

```
continuous → 时间窗 -5.498 ms ~ +4.499 ms（10 个周期，起点随机）
trigger    → 时间窗 -5.000397 ms ~ +5.000394 ms（10 个周期，以触发点对称居中）
```

## 常驻等待 `watch`（只有触发模式）

按用途分工，**别用 `watch` 干一次性的活**：

| 场景 | 用什么 | 行为 |
|------|--------|------|
| **探头还没接、准备接** | `watch` | `TRIGger:A:MODE NORMal` + `RUN`（不触发就不刷新记录）→ 轮询 `MEASurement` 判"信号到了" → `STOP` 读记录 → 复核真跨越电平 → 落盘 → 重新武装，一直等 |
| **探头已经接好，直接抓当前波形** | `csv --mode continuous` / `shot` | 跑 `--settle` 秒后 `STOP` 冻结并读数据，**抓一次就结束**，没有轮询等待窗口 |

示例（探头未接，接上就抓，电平 0.15 V 上升沿，抓 1 次就停）：

```powershell
python scripts/scope.py --ip 169.254.101.84 watch `
    --trig-level 0.15 --trig-slope rise `
    --interval 0.5 --max-events 1 --out .\scope_out
```

示例（探头已接好，直接抓一次）：

```powershell
python scripts/scope.py --ip 169.254.101.84 csv --mode continuous --periods 10 --out .\scope_out
```

要点：

- 电平不给（`--trig-level`）时按当前测量值估：`MEAN + 10 %×Vpp`，并按屏幕范围限幅；
  探头没接时 MEAN≈0、Vpp≈0 → 电平就是 **0 V**，接上任意信号都会立即触发。
- `--min-vpp`（默认 5 mV）防噪声误触发；`--trig-wait 0`（默认）表示一直等。
- 抓完会 `AUToscale` + 按频率重设时基再重新武装，所以**第一次抓的是接上瞬间当前时基下的画面**，
  想要好看的时间窗就先给 `--timebase`（探头没接时测不到频率，只能手给）。
- 落盘前会复核记录里**确实有越过触发电平的边沿**（`crossing_ok`），不复核就可能在"仪器没真触发"时存下一张废图。
- 结束时默认恢复监测前的量程/偏移/运行状态（`--keep-view` 保持调好的显示）。

## 分辨率 / 时基控制（`--timebase` / `--preset`）

三种方式，优先级 `--timebase` > `--preset` > `--periods`（后者只裁剪不改时基）：

| 方式 | 写法 | 行为 | 实测（1 kHz 方波）|
|------|------|------|-------------------|
| 手动指定 | `--timebase 20us` | 直接设这个时基 | 20 µs/div → 10.08 MSa/s |
| 用途预设 | `--preset freq` | 自动测周期 → 窗口≈10 个周期 | 1 ms/div → 305 kSa/s |
| | `--preset detail` | 窗口≈1.5 个周期 | 200 µs/div → 1.0 MSa/s |
| | `--preset edge` | 窗口≈1/20 个周期（采样率拉满） | 5 µs/div → **41.67 MSa/s**（24 ns/点）|
| 仅裁剪 | `--periods 10` | 不动时基，只截前 N 个周期 | 采样率不变 |

**不要靠 `--periods` 提分辨率**，直接指定时基：

```bash
python scope.py --ip 169.254.101.84 csv --timebase 20us   # 20 µs/div
python scope.py --ip 169.254.101.84 csv --timebase 100us  # 100 µs/div
python scope.py --ip 169.254.101.84 csv --timebase 2e-5   # 等价写法
```

- 支持 `ns` / `us` / `µs` / `ms` / `s` 后缀，也接受纯数字（单位=秒）。
- 脚本会回读仪器实际落点；1-2-5 档位外会自动归到最近档并提示。
- 采集结束**自动还原**原来的时基与水平位置（快照在 `restore_acq` 里）。

实测对照（1 kHz 方波，12 格窗口）：

| 时基 | 窗口 | 采样率 | 采样间隔 | 100 µs 内的点数 |
|------|------|--------|----------|-----------------|
| 1 ms/div | 12 ms | 305.176 kSa/s | 3.2768 µs | 30 |
| 200 µs/div | 2.4 ms | 1.000 MSa/s | 1.0 µs | 100 |
| 20 µs/div | 0.24 ms | **10.081 MSa/s** | **99.2 ns** | 1008 |
| 5 µs/div | 0.06 ms | **41.667 MSa/s** | **24 ns** | 4167 |

注意：即使到 41.67 MSa/s，实测 1 kHz 方波的边沿仍只占 **1 个采样点**，说明真实边沿 **< 24 ns**，
已超出该时基能分辨的范围 —— 想更细只能再缩时基（`--timebase 1us`），或接受"边沿 < 24 ns"这个结论。

所以：**时间分辨率只由时基决定**；`--periods` 只负责"周期数标准化"，可与时基叠加
（`--timebase 100us --periods 10` 表示"用这个时基采，再截前 10 个完整周期"）。

## 输出约定（固定格式）

一次 `csv` **必定**产出 **CSV + 波形图**，另附一份 JSON 元数据（同名前缀）：

| 文件 | 必需 | 内容 |
|------|------|------|
| `scope_YYYYMMDD_HHMMSS.csv` | **是** | 纯数值表：`Time (s),CH1 (V),CH2 (V)...`；首行表头、无注释行，`pandas.read_csv` 直接可用 |
| `scope_YYYYMMDD_HHMMSS.png` | **是** | 波形图：时间轴 ms、电压轴 V、网格、通道图例；标题栏含模式/点数/采样率/时基/触发电平/周期数 |
| `scope_YYYYMMDD_HHMMSS.json` | 附加 | 元数据：IDN、抓取时间、运行模式与触发参数、采集设置、还原前的原始设置、每通道统计值 |

- CSV 数值格式：时间 `%.9e`，电压 `%.6e`（6 位有效数字，不丢 10 bit ADC 精度）。
- 波形图优先用 matplotlib（本机已装，中文字体已配）；**若环境没有 matplotlib，自动降级为内置纯标准库 PNG 渲染**（画坐标框 + 曲线，无文字），保证"一定有图"。
- `--name` 可自定义前缀；截图仍为 `shot_YYYYMMDD_HHMMSS.png`。

## 本机已固化的环境事实

| 项目 | 值 |
|------|-----|
| 仪器 | `Rohde&Schwarz,RTB2004,1333.1005k04/113121,02.202`（固件 02.202）|
| 连接方式 | 网线直连电脑网口 + Auto-IP（169.254.x.x）|
| 开放端口 | **80** (Web)、**111** (portmapper)、**1024** (VXI-11)、**5025** (SCPI 原始套接字) |
| 关闭端口 | 4880 (HiSLIP)、5024、21 (FTP)、5900 (VNC) 均 `ConnectionRefused` |
| 本机辅助地址 | `以太网` 网卡已加 **169.254.10.10/16**（与示波器同网段），原 `192.168.0.4/24` 保留不受影响 |

## 关键坑（都踩过，别重犯）

1. **读波形前必须 STOP**。`CHANnel<n>:DATA?` 在 `RUN` 状态下**不返回任何数据**（等到超时），
   必须先 `STOP`。脚本用 `Frozen` 上下文管理器自动冻结，结束时按原状态恢复 `RUN`。

2. **首次连 5025 可能超时一次**。示波器刚上电或网络刚协商完时，第一次 `connect` 常常 timeout，
   重试一次就通。脚本内置 3 次重试（间隔 1s、2s）。

3. **链路本地地址要两边同网段**。示波器 Auto-IP 拿到 `169.254.101.84/16` 时，
   电脑网口若是 `192.168.0.4/24` 是**不通**的（TCP 会超时）。
   给网卡加一个辅助地址即可（需管理员）：
   ```powershell
   New-NetIPAddress -InterfaceAlias '以太网' -IPAddress 169.254.10.10 -PrefixLength 16
   ```
   加完可以直接 `arp -d <IP>` 清一下缓存。

4. **`ACQuire:POINts?` 是存储深度，不是记录长度**。实测该值 `18,750,068`，而实际波形点数
   `CHANnel<n>:DATA:POINts?` 只有 `18,304`（时基决定）。**导出前用记录长度判断数据量**：
   ASC 文本约 12 字节/点，100 万点 ≈ 12 MB 文本、耗时约 10 s。

5. **数据单位是伏特**，不是 ADC 码。量化步长 = `垂直档位 ÷ 102.4`（RTB 系列 10 bit ADC）。
   实测 0.5 V/div 时步长 4.8828 mV，与公式吻合。因此 CSV 里的电压值可直接用，无需换算。

6. **多通道点数可能不一致**。脚本取最大点数为主网格，点数不同的通道做线性插值（纯 Python 实现，
   不依赖 numpy），元数据里保留各通道原始 `points`。

7. **`CHANnel<n>:DATA?` 的 ASCII 响应是一条可达 1.5 MB 的超长行**（无中间换行），
   必须按"读到 `\n` 为止"的方式接收，不能用 `recv` 一次就返回。

8. **不要用 `TRACe1:DATA?` 取波形**。它返回的是**屏幕像素轨迹**（实测 125,000 字节，首字节 `0x85`），
   不是电压采样点。

9. **`EXPort:WAVeform:DATA?` 能用但顺序不是时间序**（实测返回 `9.76e-05,-1.46e-02,...`，
   疑似按像素列组织）。要时间序波形请用 `CHANnel<n>:DATA?`。

10. **Windows 控制台是 GBK**。脚本开头统一 `sys.stdout.reconfigure(encoding="utf-8")`，
    否则中文提示或 `µ` 等字符会抛 `UnicodeEncodeError`。

11. **边沿时间受采样率限制**。时基 5 ms/div 时采样率只有 305 kSa/s，一个采样间隔就是
    **3.2768 µs**，实测方波上升/下降时间算出来正好等于一个采样间隔 —— 这不是真实边沿时间，
    而是**采样率上限**。要测真实上升时间，把时基调小：直接加 `--timebase 20us`
    （实测 20 µs/div → **10.08 MSa/s**，比 305 kSa/s 高 33 倍，采样间隔 99.2 ns），
    脚本会自动设定并在采集后还原原来的时基。`analyze` 输出里会直接打印这个限值提醒。

12. **占空比必须用"本上升沿 → 下一个上升沿"作分母**。用"下一个上升沿 → 再下一个"会得到
    2 倍周期，占空比算出来正好是一半（曾把 50 % 的方波算成 25 %）。交叉核对方法：
    `均值 ≈ 低电平 + 占空比 × Vpp`，50 % 方波的均值应该正好落在两极中点。

13. **触发命令的真实名字**（实测，别猜）：存在 `TRIGger:A:MODE`（写 `AUTO`/`NORMal`，回读 `AUTO`/`NORM`）、
    `TRIGger:A:SOURce`、`TRIGger:A:EDGE:SLOPe`（`POS`/`NEG`）、`TRIGger:A:LEVel`、`TRIGger:A:HOLDoff:TIME`。
    **不存在**（返回 `-113 Undefined header`）：`TRIGger:A:EDGE:SOURce`、`TRIGger:A:AUTolevel?`、
    `TRIGger:STATe?`、`TRIGger:SWEep?`。

14. **运行状态有四种**：`RUN`（连续运行中）、`BRE`（冻结）、`STOP`、**`COMP`（单次采集已完成）**。
    `RUNContinous` / `RUNSingle` 是**设置命令，不能带 `?`**（会返回 `-510 Such a query not allowed`），
    状态统一用 `ACQuire:STATe?` 读。触发模式下读完数据的状态就是 `COMP`，可以正常读取。

15. **`TRIGger:A:FINDlevel` 不可靠**。它作为查询返回 `-510`（说明是设置命令），但实测执行后把电平
    设成 **0 V** —— 对 0~2.6 V 的方波来说落在下沿附近。本 skill 改为先抓一帧、取 5 %/95 % 分位中点
    （实测 1.2516 V，触发稳定）。

16. **`ACQuire:MODE?`=`RTIM`、`ACQuire:TYPE?`=`REFR`** 是两个独立设置（实时采集 / 刷新模式），
    它们会影响实际采样率；本 skill **不擅自改动**这两项，只调时基与触发。

17. **`RUNSingle` 不能用来"等触发"**。实测：`TRIGger:A:MODE NORMal` + 电平设到信号达不到的位置
    （信号 Vpp 0.34 V、电平 3 V），`RUNSingle` 之后 `ACQuire:STATe?` **立刻就是 `STOP`**（连续轮询
    2.4 s 全是 STOP），并不会停在"等待触发"。要"先武装、信号来了再抓"，只能
    `TRIGger:A:MODE NORMal` + **`RUN`**（NORMal 模式下不触发就不刷新记录，信号一到立刻刷新）。

18. **查询超时后必须清缓冲（drain）**。某条查询超时（例如 AUTOscale 刚做完、测量结果还没算出来时的
    `MEASurement<n>:RESult?`），仪器往往仍会把响应晚一点发过来；不清理，后面的查询就整体错位 ——
    典型症状是 `could not convert string to float: 'Rohde&Schwarz,...'`（`*IDN?` 的字符串被当成点数/档位解析）。
    本 skill 的 `Scpi.drain()` 在测量查询失败处和 `csv` 入口各清一次。

## 与 U 盘方案的取舍

示波器前面板 USB 口对 U 盘很挑：实测一块杂牌 32 GB USB 3.0 盘能挂载、能建目录项，
但**写数据块必失败**（留下 0 字节 `SCR01.PNG`，示波器报 `Save failed (112;-24;-107)`），
换 4 KB / 8 KB / 16 KB 簇、Windows / fat32format / 示波器自带格式化都无效。
网线方案稳定得多：截图 0.9 s、CSV 十几秒，且不受容量和主控限制。

## 文件结构

```
scope/
├── SKILL.md                      # 本文档（CodeBuddy / Claude）
├── AGENTS.md                     # Codex 及其他通用 AI 工具入口
├── README.md                     # 人读文档
├── RECOVER.md                    # 换机恢复指南
├── scope.json                    # 机器可读清单
├── scripts/
│   └── scope.py                  # 唯一入口，零第三方依赖
└── references/
    └── scpi-rtb2000.md           # SCPI 命令实测参考（含响应原文与错误码）
```

## 自检清单

- [ ] `idn` 能返回 `Rohde&Schwarz,RTB...`
- [ ] `shot` 生成的 PNG 能被看图软件打开（头部 `89 50 4E 47`）
- [ ] `csv` 行数 = 点数 + 1（表头），时间轴单调递增且覆盖屏幕时窗
- [ ] 抓取结束后示波器回到 `RUN`（`scpi "ACQuire:STATe?"` 确认）
- [ ] 元数据 JSON 里 `export.points` 与 CSV 行数一致
