# scope — 示波器以太网控制技能

用一根网线控制 **R&S RTB2000 系列**示波器（RTB2002 / RTB2004）：抓屏幕截图、
采集波形并固定输出 **CSV + 波形图**、离线分析频率/占空比/边沿时间、任意 SCPI 直通。

- **不需要 U 盘**（前面板 USB 口对杂牌盘兼容性极差，实测写数据必失败）
- **不需要 pyvisa / NI-VISA / 任何仪器驱动**
- 单一可执行入口，纯 Python 标准库；`matplotlib` 可选（仅影响波形图美观度）

## 触发词

`scope`、`示波器`、示波器截图、示波器抓屏、导出示波器波形、波形导 CSV、
网线连示波器、SCPI 控制示波器、RTB2004/RTB2002 连电脑。

## ⚠️ 先看这里：两种模式

选错就白抓，判断依据只有一条：**探头接了没有**。

| | **① 触发模式** | **② 连续模式** |
|---|---|---|
| 什么时候用 | **探头还没接、准备接**；要等信号出现那一刻的画面 | **探头已经接好**，屏幕上本来就有波形 |
| 有没有触发电平 | **有**，默认 **1 V** —— 高于探头接上瞬间的扰动杂波，杂波不会误触发 | **没有**，完全不配置触发电平（示波器已有数据，不需要靠触发刷新） |
| 仪器怎么跑 | `NORMal` + `RUN`（不触发就不刷新记录）→ 越过电平才冻结 | `AUTO` + `RUNContinous` → `--settle` 秒 → `STOP` 读一次 |
| 用哪个命令 | `watch`（**常驻等**，启动即触发模式）／`csv --mode trigger`（只抓一次） | `csv --mode continuous`（默认）／`shot` |

用 AI 工具时，正确姿势是先问"探头接好了吗"，再按上表选命令。

## 安装 / 使用

无需安装。克隆或拷贝本目录后直接运行：

```bash
python scripts/scope.py --help
```

要求 Python **3.10+**。任何平台（Windows / Linux / macOS）均可，脚本无硬编码路径。

## 快速开始

```bash
# 1. 找仪器（同一广播域）
python scripts/scope.py discover --cidr 169.254.0.0/16

# 2. 看仪器状态
python scripts/scope.py --ip 169.254.101.84 idn

# 3. 抓屏幕截图
python scripts/scope.py --ip 169.254.101.84 shot --out ./scope_out

# 4. 采集波形 → CSV + 波形图（固定输出）
python scripts/scope.py --ip 169.254.101.84 csv --out ./scope_out

# 5. 边沿触发单次采集（Auto / Normal）
python scripts/scope.py --ip 169.254.101.84 csv --mode trigger --trig-mode normal --trig-slope rise

# 6. 只保留 10 个完整周期
python scripts/scope.py --ip 169.254.101.84 csv --periods 10

# 6b. 直接改分辨率（时基），不用手拧旋钮
python scripts/scope.py --ip 169.254.101.84 csv --timebase 20us

# 6c. 或用用途预设：freq / detail / edge
python scripts/scope.py --ip 169.254.101.84 csv --preset edge

# 6d. 探头还没接好、等信号出现：先武装触发，再常驻等待
python scripts/scope.py --ip 169.254.101.84 watch --channel 1 --trig-slope rise --trig-level 1

# 7. 离线分析（不连仪器）
python scripts/scope.py analyze ./scope_out/scope_20261107_191500.csv
```

仪器地址：`--ip` > 环境变量 `SCOPE_IP` > `discover`。端口默认 5025（`--port` / `SCOPE_PORT`）。

## 输出格式（固定）

| 文件 | 必需 | 说明 |
|------|------|------|
| `scope_<时间戳>.csv` | 是 | `Time (s),CH1 (V)...`，首行表头，`pandas.read_csv` 直接可用 |
| `scope_<时间戳>.png` | 是 | 波形图，时间轴 ms / 电压轴 V / 网格 / 图例 / 标题带模式与参数 |
| `scope_<时间戳>.json` | 附加 | 元数据（IDN、模式、触发参数、采集设置、原设置快照、统计值）|

## 运行模式

- `--mode continuous`（默认）：`AUTO + RUN`，跑 `--settle` 秒取一帧，另按 `--observe`（默认 6 秒）
  做跨秒数值观察并输出 `_trend.csv`。适合探头已接好、信号持续输出的场景。
- `--mode trigger`：配置边沿触发后单次采集，数据以触发点对齐；调稳并稳定 10 个周期后才导出。
  适合已接好探头、要抓指定边沿。
- `watch`（常驻等待）：探头还没接好时用。先武装 `NORMal + RUN`，信号出现并越过触发电平后
  自动调刻度、确认稳定，然后导出 CSV/PNG 并继续等下一次。探头已接好则直接用 `csv`。

触发参数：`--trig-source`、`--trig-slope rise|fall`、`--trig-mode auto|normal`、
`--trig-level`（缺省自动取信号中点）、`--trig-wait`。

## 分辨率怎么提：用 `--timebase` 或 `--preset`

`--timebase 20us` 直接设定时基（s/div，支持 `ns/us/µs/ms/s` 后缀或纯秒数），脚本会回读实际档位，
采集结束后自动还原原来的时基。实测 1 kHz 方波：

| 时基 | 窗口 | 采样率 | 采样间隔 |
|------|------|--------|----------|
| 1 ms/div | 12 ms | 305.176 kSa/s | 3.2768 µs |
| 200 µs/div | 2.4 ms | 1.000 MSa/s | 1.0 µs |
| 20 µs/div | 0.24 ms | **10.081 MSa/s** | **99.2 ns** |
| 5 µs/div | 0.06 ms | **41.667 MSa/s** | **24 ns** |

不想算时基就用**用途预设**（脚本先测周期，再按时基档位自动选）：

| 预设 | 窗口 | 用途 | 实测（1 kHz）|
|------|------|------|--------------|
| `--preset freq` | ≈10 个周期 | 测频率、占空比 | 1 ms/div, 305 kSa/s |
| `--preset detail` | ≈1.5 个周期 | 看单周期细节 | 200 µs/div, 1.0 MSa/s |
| `--preset edge` | ≈1/20 个周期 | 测边沿（采样率拉满）| 5 µs/div, 41.67 MSa/s |

## 关于 `--periods N`（限周期）

默认**不建议**加。限周期不会提高采样率（12 ms 窗与 10 周期窗都是 305 kSa/s），
它只是"文件更小 + 周期数标准化"，代价是丢弃窗口外信息。
需要跨测量横向对比、或要标准化出图时再加。想真正提高分辨率，用上面的 `--timebase`。
两者可叠加：`--timebase 100us --periods 10`。

## 目录结构

```
scope/
├── SKILL.md                     # CodeBuddy / Claude 技能定义（含设计取舍）
├── AGENTS.md                    # Codex 及其他通用 AI 工具入口
├── README.md                    # 本文件（人读）
├── RECOVER.md                   # 换机 / 重装恢复指南
├── scope.json                   # 机器可读清单（名称/关键词/入口/子命令）
├── scripts/
│   └── scope.py                 # 唯一执行入口
├── tests/
│   └── test_scope.py            # 单元测试（16 项，不用真实仪器）
└── references/
    └── scpi-rtb2000.md          # SCPI 命令实测参考（含错误码与不存在的命令）
```

## 已知限制

- 目前只针对 **R&S RTB2000 系列**（实测固件 02.202）。其他厂商（Rigol/Siglent/Keysight）命令不同，
  需要另建对应的适配层；`Scpi` 通信层与 CSV/绘图输出层可直接复用。
- 波形数据以 ASCII 传输（约 12 字节/点）。记录长度极大时（>100 万点）耗时明显，
  建议先用 `--periods` 或缩小时基控制数据量。
- 边沿时间受采样率限制：算出的上升时间若正好等于一个采样间隔，说明真实边沿更陡，需提高采样率再测。

## 安全

- 脚本只做 SCPI 读写，不安装任何东西、不修改系统设置。
- 触发模式与 `watch` 结束后会还原时基、触发设置与原来的运行状态；
  **连续模式按设计保留调好的刻度与 `AUTO + RUN`**（下次直接可用）。
- 不要在他人正在测量的仪器上随意执行 `--mode trigger` 之外的破坏性命令；`scpi` 子命令是直通，
  写入命令会直接改变仪器状态，使用前确认。
