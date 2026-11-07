# AGENTS.md — scope（示波器控制技能）

本文件是本技能对 **Codex 及其他通用 AI 工具** 的入口。内容与 `SKILL.md` 等价，
任何工具读完本文件即可直接操作，无需额外约定。

## 这个技能做什么

用网线（以太网 SCPI，TCP 5025）连接并控制 **R&S RTB2000 系列**示波器：
抓屏幕截图、采集波形并**固定输出 CSV + 波形图**、离线分析频率/占空比/边沿时间、任意 SCPI 直通。

**触发词**：`scope`、`示波器`、示波器截图、示波器抓屏、导出示波器波形、波形导 CSV、
网线连示波器、SCPI 控制示波器、RTB2004/RTB2002 连电脑。

## ⚠️ 先看这里：两种模式（动手前必须先跟用户讲清楚）

选错就白抓，判断依据只有一条：**探头接了没有**。

| | **① 触发模式** | **② 连续模式** |
|---|---|---|
| 什么时候用 | **探头还没接、准备接**；要等信号出现那一刻的画面 | **探头已经接好**，屏幕上本来就有波形 |
| 有没有触发电平 | **有**，默认 **1 V** —— 高于探头接上瞬间的扰动杂波，杂波不会误触发 | **没有**，完全不配置触发电平 |
| 仪器怎么跑 | `TRIGger:A:MODE NORMal` + `RUN`（不触发就不刷新记录）→ 越过电平才冻结 | `TRIGger:A:MODE AUTO` + `RUNContinous` → `--settle` 秒 → `STOP` 读一次 |
| 用哪个命令 | `watch`（**常驻等**，启动即触发模式）／`csv --mode trigger`（只抓一次） | `csv --mode continuous`（默认）／`shot` |

**硬性要求**：用户只说"用 scope 抓一下"时，先问"探头接好了吗"，把两种模式的区别讲给他再动手。

## 前置条件

1. Python 3.9+（无第三方依赖；`matplotlib` 可选，仅用于画更漂亮的波形图，缺失时自动降级）。
2. 示波器与电脑在同一二层网络（直连网线或同一交换机）。
3. 知道示波器 IP。不知道就跑 `discover`，或让用户在仪器 `[Setup] → Interface → LAN` 读给他。

## 唯一入口

```bash
python scripts/scope.py --help
```

仪器地址优先级：`--ip` > 环境变量 `SCOPE_IP` > `discover` 结果。端口 `--port`/`SCOPE_PORT`，默认 5025。

## 子命令

| 命令 | 作用 |
|------|------|
| `idn` | 读仪器标识、存储深度、采样率、时基、各通道档位 |
| `shot` | 抓屏幕截图 PNG |
| `csv` | **采集波形 → 固定输出 CSV + 波形图 PNG（+ JSON 元数据）** |
| `watch` | 常驻等待（**触发模式**）：探头没接时先武装 1 V 电平，信号越过就抓拍落盘并重新武装 |
| `stats` | 只算 min/max/Vpp/RMS/mean，不落盘 |
| `analyze <csv>` | 离线分析已导出的 CSV（电平/频率/占空比/边沿时间/抖动），不需要连仪器 |
| `scpi "<CMD>"` | 任意 SCPI 直通（`?` 结尾读响应，否则写入） |
| `discover` | 扫描网段找仪器并回读 `*IDN?` |

## 典型调用

```bash
# 找仪器
python scripts/scope.py discover --cidr 169.254.0.0/16

# 截图
python scripts/scope.py --ip 169.254.101.84 shot --out ./scope_out

# 连续运行模式采集（默认）
python scripts/scope.py --ip 169.254.101.84 csv --channels 1 --out ./scope_out

# 边沿触发单次采集（Auto / Normal）
python scripts/scope.py --ip 169.254.101.84 csv --mode trigger --trig-slope rise --trig-mode normal

# 只保留 10 个完整周期
python scripts/scope.py --ip 169.254.101.84 csv --mode trigger --periods 10

# 直接指定时基（提分辨率，脚本会自动还原）
python scripts/scope.py --ip 169.254.101.84 csv --timebase 20us

# 或按用途预设：freq≈10 周期 / detail≈1.5 周期 / edge≈1/20 周期
python scripts/scope.py --ip 169.254.101.84 csv --preset edge

# 离线分析
python scripts/scope.py analyze ./scope_out/scope_20261107_191500.csv
```

`--timebase` 接受 `20us` / `100µs` / `1ms` / `2e-5` 等写法（后缀 ns/us/µs/ms/s，纯数字按秒）。
实测 20 µs/div → 10.081 MSa/s（对比 1 ms/div 的 305 kSa/s）。

## 输出约定（固定格式，不可省略）

每次 `csv` **必定**产出：

| 文件 | 必需 | 内容 |
|------|------|------|
| `scope_<时间戳>.csv` | **是** | `Time (s),CH1 (V),...` 纯数值表，首行表头，无注释行 |
| `scope_<时间戳>.png` | **是** | 波形图（时间 ms / 电压 V / 网格 / 图例 / 标题含模式与参数）|
| `scope_<时间戳>.json` | 附加 | 元数据：IDN、模式、触发参数、采集设置、原始设置快照、通道统计 |

## 两种运行模式（实现细节，区别见文首）

| 模式 | 参数 | 仪器动作 |
|------|------|----------|
| 连续运行 | `--mode continuous`（默认） | `TRIGger:A:MODE AUTO`（**不设触发电平**）→ `RUNContinous` → 等 `--settle` 秒 → `STOP` → 读取 |
| 触发单次 | `--mode trigger` | 配边沿触发 → `RUNSingle` → 等状态 `COMP` → 读取 |
| 常驻等待 | `watch` | `NORMal` + `RUN` 预武装（电平默认 **1 V**）→ 轮询测量值 → 越过电平就冻结抓拍 → 重新武装 |

触发参数：`--trig-source CH1`、`--trig-slope rise|fall`、`--trig-mode auto|normal`、
`--trig-level <V>`（`csv --mode trigger` 缺省自动取信号中点；`watch` 缺省固定 1 V）、`--trig-wait <s>`。

## 必须知道的坑

1. **读波形前必须 `STOP`**：`RUN` 状态下 `CHANnel<n>:DATA?` 完全不返回，只会超时。
2. **首次连 5025 常超时一次**，需重试（脚本已内置 3 次）。
3. **`CHANnel<n>:DATA?` 返回的是伏特**（ASCII 逗号分隔，量化步长 = 档位 ÷ 102.4）。
4. **`ACQuire:POINts?` 是存储深度，不是记录长度**；真实点数看 `CHANnel<n>:DATA:POINts?`。
5. **`TRACe1:DATA?` 是屏幕像素轨迹**，不是波形数据。
6. **`RUNContinous` / `RUNSingle` 不能带 `?`**（`-510`），状态用 `ACQuire:STATe?` 读（`RUN`/`BRE`/`STOP`/`COMP`）。
7. **`TRIGger:A:FINDlevel` 会把电平设成 0 V**，不可靠，自己算中点。
8. 采集结束脚本会**自动还原**时基、触发设置与原有运行状态。

## 进一步

- 命令与响应原文、错误码、不存在的命令清单：`references/scpi-rtb2000.md`
- 中文完整说明（含设计取舍与取舍理由）：`SKILL.md`
- 机器可读清单：`scope.json`
