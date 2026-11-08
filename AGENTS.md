# AGENTS.md — scope（示波器控制技能）

本文件是 **Codex 及其他通用 AI 工具** 的入口，内容与 `SKILL.md` 等价。
任何工具读完本文件即可直接操作，无需额外约定。

## 这个技能做什么

用网线（以太网 SCPI，TCP 5025）控制 **R&S RTB2000 系列**示波器：
连续数值观测、触发采集、波形 CSV 导出与离线分析。**图片只作输出，不作判断依据。**

**触发词**：`scope`、`示波器`、示波器截图、导出示波器波形、波形导 CSV、
网线连示波器、SCPI 控制示波器、RTB2004/RTB2002 连电脑。

## 动手前先选模式

控制仪器前先向用户说明两种模式并说明本次选择。目标不明时再确认。

| 模式 | 前提 | 操作前要确定 | 命令 |
|------|------|--------------|------|
| 连续 | **探头已接好**，信号持续输出 | 观察多久：按预计变化时间设 `--observe`（默认 6 秒） | `csv --mode continuous`（默认） |
| 触发 | **探头还没接好、准备接入** | **先设好触发电平** `--trig-level` 与边沿，再武装等待 | 未接好用 `watch`；已接好抓一次边沿用 `csv --mode trigger` |

探头未接好时应使用 `watch` 的 `NORMal + RUN` 预先武装，**不要用 `RUNSingle` 代替长期等待**。

## 前置条件

- Python **3.10+**，无需必装第三方包；`matplotlib` 可选（缺失时用标准库生成 PNG）
- 仪器地址：`--ip` 或环境变量 `SCOPE_IP`；`discover` 只列出候选地址，不自动选中
- 首次连接新仪器先用 `idn` 确认型号（脚本仅接受 RTB2002/RTB2004）

## 子命令

| 命令 | 作用 |
|------|------|
| `idn` | 读仪器标识、存储深度、采样率、时基、各通道档位 |
| `shot` | 抓屏幕截图（**仅用户明确要求截屏时使用**）|
| `csv` | 采集波形 → CSV + PNG（+ JSON）；支持连续/触发两种模式 |
| `stats` | 只算 min/max/Vpp/RMS/mean，不落盘 |
| `watch` | 常驻等待信号出现：武装触发 → 越过电平 → 自动调稳 → 稳定 N 周期后导出 |
| `analyze <csv>` | 离线分析已导出的 CSV，不需要连仪器 |
| `scpi "<CMD>"` | 任意 SCPI 直通（`?` 结尾读响应）|
| `discover` | 扫描网段找仪器并回读 `*IDN?` |

## 典型调用

```text
python scripts/scope.py --help
python scripts/scope.py discover
python scripts/scope.py --ip <IP> idn
python scripts/scope.py --ip <IP> csv --mode continuous --channels 1 --observe 10 --out <DIR>
python scripts/scope.py --ip <IP> csv --mode trigger --trig-slope rise --trig-level <V> --out <DIR>
python scripts/scope.py --ip <IP> watch --channel 1 --trig-slope rise --trig-level <V> --out <DIR>
python scripts/scope.py analyze <CSV>
```

## 数值判断规则

设置刻度、计算参数、识别跳变、判定能否保存时，**只用 SCPI 数值**（采样数组、波形头、
仪器设置、数值测量响应）。不得依据照片、截图或生成的 PNG 做测量判断。

## 采集行为要点

- **连续**：`AUTO + RUNContinous`，读实际波形后用数值调 X/Y 刻度；按预计变化时间定 `--observe`；
  比较均值/幅度/频率，**稳定至少 10 个周期才导出**；成功后保留调好的刻度与 `AUTO + RUN`。
- **触发**：先依预期信号高低设 `--trig-level`（使电平处在信号范围内且高于噪声），
  用 `watch` 的 `NORMal + RUN` 武装；未指定时沿用面板电平，读不到才回退 1 V。
- **其他**：`--timebase` 指定时基，`--preset freq|detail|edge` 按用途选，`--periods` 截完整周期。

只有数值有效性与稳定性检查通过才导出。未获得稳定波形或有效越阈边沿时，如实报告"本次未保存"，
**不能把旧的冻结记录当作新采集**。

## 输出

| 文件 | 说明 |
|------|------|
| `<prefix>.csv` | 波形数值：`Time (s)` + 通道电压 |
| `<prefix>.png` | 由同一批数值生成的波形图 |
| `<prefix>.json` | 元数据（模式、触发参数、采集设置、通道统计）|
| `<prefix>_trend.csv` / `_trend.png` | 连续观察模式另有长时趋势 |

绘图失败时说明原因并保留已写出的数值 CSV。

## 必须知道的坑

1. 读波形前必须 `STOP`；`RUN` 状态下 `CHANnel<n>:DATA?` 不返回。
2. 首次连 5025 常超时一次，需重试。
3. `CHANnel<n>:DATA?` 单位是**伏特**；`ACQuire:POINts?` 是存储深度不是记录长度。
4. `TRACe1:DATA?` 是屏幕像素，不是波形。
5. `RUNContinous` / `RUNSingle` 不能带 `?`；状态用 `ACQuire:STATe?`（`RUN`/`BRE`/`STOP`/`COMP`）。
6. `TRIGger:A:FINDlevel` 会把电平设成 0 V，不可靠。

## 进一步

- 命令、响应原文与故障记录：`references/scpi-rtb2000.md`
- 修改采集逻辑后跑 `python tests/test_scope.py`
- 人读文档：`README.md`；换机恢复：`RECOVER.md`
