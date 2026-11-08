---
name: scope
description: 用以太网 SCPI 控制 R&S RTB2000 示波器，进行连续数值观测、触发采集、波形 CSV 导出与离线分析；图片仅作输出。已在 RTB2004 固件 02.202 实测，其他型号须先适配并验证命令。
---

# scope

## 先告知并选择模式

控制仪器前，先向用户说明两种模式和本次选择。优先根据探头是否已接好、是否要等待接入后的信号来选；目标不明时再确认。

| 模式 | 探头与信号前提 | 操作前要确定 | 命令 |
| --- | --- | --- | --- |
| 连续 | **探头已接好**，信号正在持续输出。 | 计划观察/采样多久：按预计变化时间设置 `--observe`，并从数值数据选择显示时基。 | `csv --mode continuous`（默认；`AUTO + RUNContinous`） |
| 触发 | **探头尚未接好，准备接入**，需要先等信号；已接好但要抓指定边沿也可用。 | **先设置合适的触发电平** `--trig-level` 和边沿，再武装等待。 | 未接好用 `watch`；已接好抓一次边沿用 `csv --mode trigger` |

预计第 5 秒变化时，连续模式建议 `--observe 10`，覆盖变化前后；未给预计时长时先用默认 6 秒。`--observe` 是跨秒数值观察总时长，最终波形的短时间窗另按实测周期设置。探头未接好时用 `watch` 的 `NORMal + RUN` 预先武装；不要用 `RUNSingle` 代替长期等待。

连接、采样和控制命令针对 **R&S RTB2000 系列**编写，已在 **RTB2004 固件 02.202** 实测。脚本在型号专用操作前检查 `*IDN?`，仅接受 RTB2002/RTB2004；`scpi` 保留显式命令直通。SCPI 语法通用不代表具体命令与响应通用；其他型号必须先适配并验证。本技能导出的 CSV 可以离线分析。

## 数值判断规则

设置 X/Y 刻度、计算参数、识别跳变和判定能否保存时，只用 SCPI 数值：采样数组、波形头、仪器设置和数值测量响应。不得依据照片、截图或生成的 PNG 做测量判断。图片仅作输出；`shot` 仅在用户明确要求截屏时使用，`csv` 和 `watch` 的波形图由数值生成。

## 执行入口

要求 Python **3.10+**，无需必装的第三方包。Matplotlib 可选；缺失时使用标准库生成 PNG。在本技能目录运行：

```text
python scripts/scope.py --help
python scripts/scope.py discover
python scripts/scope.py --ip <IP> idn
python scripts/scope.py --ip <IP> csv --mode continuous --channels 1 --observe 10 --out <DIR>
python scripts/scope.py --ip <IP> csv --mode trigger --trig-source CH1 --trig-slope rise --trig-level <V> --out <DIR>
python scripts/scope.py --ip <IP> watch --channel 1 --trig-slope rise --trig-level <V> --out <DIR>
python scripts/scope.py analyze <CSV>
```

通过 `--ip` 或 `SCOPE_IP` 提供仪器地址；`discover` 只列出候选地址，不自动选中。原始 TCP 默认端口 5025，可用 `--port` 或 `SCOPE_PORT` 覆盖。首次连接新仪器时先用 `idn` 确认型号。

## 采集行为

- **连续：**探头接好后设为 `AUTO`，读取实际波形，用数值调整 X/Y 刻度；按预期变化时间确定 `--observe`，默认 6 秒。比较均值、幅度、频率，稳定至少 10 个周期才导出。成功后保留调好的刻度及 `AUTO + RUN`。`--observe-interval` 控制每次运行时长，趋势 CSV 的实际时间还包含读数开销。短促事件可能落在快照间隙，应改用触发或仪器长记录。
- **触发：**探头准备接入时，先依据预期信号高低电平设 `--trig-level`，使电平处于信号范围内且高于噪声，再用 `watch` 的 `NORMal + RUN` 武装等待。若没有预期幅度，先确认电平；脚本未指定时沿用面板电平，读不到才回退 1 V，不能假定 1 V 对所有信号有效。探头已接好但要抓指定边沿时可用 `csv --mode trigger`。从数值记录确认真正越阈，再调刻度，稳定至少 10 个周期才导出；完成后恢复原设置。孤立脉冲可能无法满足稳定要求。
- **其他操作：**`--timebase` 指定时基，`--preset` 按用途选时基，`--periods` 在记录允许时截取完整周期。`stats` 只读数值不落盘；`scpi` 直通指定命令；`shot` 仅导出屏幕图片。

只有数值有效性与稳定性检查通过才导出。未获得稳定波形或有效越阈边沿时，报告本次未保存；不能把旧的冻结记录当作新采集。

## 输出

`csv` 输出波形 CSV（`Time (s)` 与通道电压）、由同一批数值生成的 PNG、以及 JSON 元数据。连续观察另输出 `_trend.csv`（实际经过时间、均值、极值、峰峰值、RMS、频率）和由这些数值生成的 `_trend.png`。图像只交付、不回读做判断。绘图失败时说明原因并保留已写出的数值 CSV。

## 按需参考与验证

- 适配或诊断仪器时，再读 [references/scpi-rtb2000.md](references/scpi-rtb2000.md) 中的命令、响应和故障记录。
- 修改采集逻辑后运行 `python tests/test_scope.py`；修改 `SKILL.md` 后用内置 `skill-creator/scripts/quick_validate.py` 校验。
