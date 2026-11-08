# RECOVER.md — scope 恢复指南

> 目标：**只要拿到这个仓库，任何电脑、任何 AI 工具都能在 5 分钟内恢复可用。**

## 0. 一分钟版

```bash
git clone https://github.com/sabeeeer/scope-skill.git
cd scope-skill
python scripts/scope.py --help          # 能看到 8 个子命令就说明可用
```

把 `scope/` 目录放进 AI 工具的技能目录即可（CodeBuddy/Claude: `~/.codebuddy/skills/scope`；
其他工具：按该工具约定放置，或直接当作普通脚本调用）。

## 1. 依赖

| 项 | 要求 | 缺失后果 |
|----|------|----------|
| Python | **3.10+** | 无法运行 |
| 第三方包 | **无**（`matplotlib` 可选） | 没装 matplotlib 时波形图自动降级为内置渲染（无文字标注），CSV/JSON 不受影响 |
| 网络 | 与示波器同二层网络（直连或同一交换机） | 连不上仪器 |
| 仪器 | R&S RTB2000 系列（RTB2002 / RTB2004），固件 02.202 实测 | 其他型号命令不同，需自写适配层 |

安装 matplotlib（可选，仅影响出图美观度）：

```bash
pip install matplotlib
```

## 2. 找回仪器地址

```bash
python scripts/scope.py discover --cidr 169.254.0.0/16
```

或在仪器上 `[Setup] → Interface → LAN` 读 IP。也可以设环境变量避免每次输入：

```powershell
# PowerShell
$env:SCOPE_IP = '169.254.101.84'
```

```bash
# bash
export SCOPE_IP=169.254.101.84
```

## 3. 链路自检（按顺序）

```bash
python scripts/scope.py --ip <IP> idn                      # 1. 能否通信
python scripts/scope.py --ip <IP> shot --out ./scope_out   # 2. 截图通道
python scripts/scope.py --ip <IP> csv  --out ./scope_out   # 3. 波形通道（应产出 csv+png）
```

三步全通过 = 链路完好。

## 4. 连不上时的排查顺序

| 现象 | 原因 | 处理 |
|------|------|------|
| `无法连接 <ip>:5025 -> timed out` | 不在同一网段 / 端口未开 | 用 `discover` 重找；确认网线；仪器 LAN 页 `IP port` 应为 5025 |
| 第一次连超时、第二次通 | 仪器刚上电或网络刚协商 | 正常现象，脚本已内置 3 次重试 |
| 能连上但 `CHANnel:DATA?` 一直超时 | 仪器处于 `RUN` 状态 | 必须先 `STOP`；`csv` 子命令已自动处理 |
| 截图返回不是 PNG | 截屏格式被改成别的 | 脚本会自动设 `HCOPy:LANGuage PNG` 并在结束后还原 |
| 触发模式一直等不到 `COMP` | 触发电平不在信号范围 / 信号无跳变 | 用 `--trig-level <V>` 手动指定，或改 `--trig-mode auto` |
| 波形图是空框/无文字 | 没装 matplotlib，走了降级渲染 | 装 matplotlib 即可 |

## 5. 关键事实（换机后最容易忘的）

- 传输是 **SCPI over TCP 5025**（原始套接字），不是 VXI-11，**不需要 pyvisa / NI-VISA**。
- 波形数据是 **ASCII 逗号分隔、单位伏特**，约 12 字节/点。
- `ACQuire:POINts?` 是**存储深度**，真实点数看 `CHANnel<n>:DATA:POINts?`。
- 采集结束脚本会自动还原：时基、水平位置、触发类型/源/沿/模式/电平、运行状态。
- 完整命令与响应原文见 `references/scpi-rtb2000.md`。

## 6. 仓库结构

```
scope-skill/
├── SKILL.md                     # CodeBuddy / Claude 技能定义
├── AGENTS.md                    # Codex 及通用 AI 工具入口
├── README.md                    # 人读文档
├── RECOVER.md                   # 本文件
├── scope.json                   # 机器可读清单
├── scripts/scope.py             # 唯一执行入口
└── references/scpi-rtb2000.md   # SCPI 实测参考
```

## 7. 回滚 / 版本

本仓库无外部依赖、无构建步骤，任何历史版本都可直接使用：

```bash
git log --oneline          # 找可用版本
git checkout <commit> -- scripts/scope.py
```
