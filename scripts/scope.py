#!/usr/bin/env python3
"""scope.py - 通过以太网 SCPI 控制 R&S RTB2000 系列示波器（零第三方依赖）。

子命令:
    idn        读取仪器标识
    shot       抓取屏幕截图 (PNG)
    csv        导出波形数据 (CSV + JSON 元数据)
    stats      只读取波形并统计（min/max/Vpp/RMS/均值），不落盘
    scpi       任意 SCPI 命令直通
    discover   扫描网段寻找示波器

设计约束:
    - 只用 Python 标准库（socket / argparse / csv / json），不依赖 pyvisa 或 NI-VISA。
    - 不硬编码仪器地址：--ip > 环境变量 SCOPE_IP > 由 discover 得出。
    - 读波形前示波器必须处于 STOP；脚本会自动冻结并在结束后恢复原状态。
    - 所有输出为 UTF-8；脚本与文件名保持 ASCII，便于跨平台与跨仓库迁移。
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import ipaddress
import json
import os
import socket
import sys
import time
from datetime import datetime

try:  # Windows 控制台默认 GBK，强制 UTF-8 避免 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_PORT = 5025
DEFAULT_TIMEOUT = 15.0
DATA_TIMEOUT = 90.0
ENV_IP = "SCOPE_IP"
ENV_PORT = "SCOPE_PORT"


# --------------------------------------------------------------------------- #
# SCPI over raw socket
# --------------------------------------------------------------------------- #
class ScpiError(RuntimeError):
    """SCPI 通信或仪器响应错误。"""


class Scpi:
    """基于 TCP 原始套接字（默认 5025）的 SCPI 客户端。

    首次连接偶发超时，connect() 内部做 3 次重试；读写均带超时保护。
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = 3) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._buf = b""
        self._sock: socket.socket | None = None
        self._connect(retries)

    def _connect(self, retries: int) -> None:
        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                sock.settimeout(self.timeout)
                self._sock = sock
                return
            except OSError as exc:  # 首次连接常见瞬时超时
                last = exc
                if attempt < retries:
                    time.sleep(1.0 * attempt)
        raise ScpiError(f"无法连接 {self.host}:{self.port} -> {last}")

    # -- 底层 ------------------------------------------------------------ #
    def _fill(self, deadline: float) -> None:
        assert self._sock is not None
        remain = deadline - time.time()
        self._sock.settimeout(max(0.2, min(2.0, remain)))
        try:
            chunk = self._sock.recv(262144)
        except socket.timeout:
            return
        if not chunk:
            raise ScpiError("连接被仪器关闭")
        self._buf += chunk

    def write(self, cmd: str) -> None:
        assert self._sock is not None
        self._sock.sendall(cmd.encode("ascii") + b"\n")

    def read_line(self, timeout: float = DATA_TIMEOUT) -> str:
        """读取一条以换行结束的响应（可能非常长，例如 1.5 MB 的波形文本）。"""
        deadline = time.time() + timeout
        while b"\n" not in self._buf:
            if time.time() > deadline:
                raise ScpiError(f"等待响应超时（已收 {len(self._buf)} 字节）")
            self._fill(deadline)
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii", "replace").strip()

    def read_block(self, timeout: float = DATA_TIMEOUT) -> bytes:
        """读取 IEEE 488.2 块数据：#<位数><长度><数据>。"""
        deadline = time.time() + timeout
        while len(self._buf) < 2:
            if time.time() > deadline:
                raise ScpiError("等待块头超时")
            self._fill(deadline)
        if self._buf[:1] != b"#":
            head = self._buf[:64]
            raise ScpiError(f"期望块数据，实际收到 {head!r}")

        ndigits = int(self._buf[1:2])
        while len(self._buf) < 2 + ndigits:
            if time.time() > deadline:
                raise ScpiError("等待块长度字段超时")
            self._fill(deadline)
        length = int(self._buf[2:2 + ndigits])
        total = 2 + ndigits + length
        while len(self._buf) < total:
            if time.time() > deadline:
                raise ScpiError(f"块数据未读完 {len(self._buf)}/{total}")
            self._fill(deadline)
        data = self._buf[2 + ndigits:total]
        self._buf = self._buf[total:]
        return data

    # -- 便捷 ------------------------------------------------------------ #
    def query(self, cmd: str, timeout: float = DATA_TIMEOUT) -> str:
        self.write(cmd)
        return self.read_line(timeout)

    def query_block(self, cmd: str, timeout: float = DATA_TIMEOUT) -> bytes:
        self.write(cmd)
        return self.read_block(timeout)

    def query_float(self, cmd: str) -> float:
        return float(self.query(cmd, DEFAULT_TIMEOUT))

    def query_int(self, cmd: str) -> int:
        return int(float(self.query(cmd, DEFAULT_TIMEOUT)))

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


# --------------------------------------------------------------------------- #
# 采样状态保护
# --------------------------------------------------------------------------- #
class Frozen:
    """上下文管理器：确保波形读取期间仪器处于 STOP，退出时恢复原采集状态。"""

    def __init__(self, inst: Scpi) -> None:
        self.inst = inst
        self.original = "STOP"

    def __enter__(self) -> "Frozen":
        try:
            self.original = self.inst.query("ACQuire:STATe?", DEFAULT_TIMEOUT).strip().upper()
        except Exception:
            self.original = "RUN"
        if self.original not in ("STOP", "BRE"):
            self.inst.write("STOP")
            time.sleep(0.6)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.original == "RUN":
                self.inst.write("RUN")
            elif self.original in ("BRE", "STOP"):
                # BRE 与 STOP 都能稳定呈现画面，保持冻结状态即可
                pass
            time.sleep(0.2)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 波形读取
# --------------------------------------------------------------------------- #
def read_channel(inst: Scpi, ch: int) -> dict | None:
    """读取一个通道的波形。返回 None 表示该通道未开启。"""
    if inst.query(f"CHANnel{ch}:STATe?", DEFAULT_TIMEOUT).strip() not in ("1", "ON"):
        return None

    header = inst.query(f"CHANnel{ch}:DATA:HEADer?", DEFAULT_TIMEOUT)
    parts = [p for p in header.split(",") if p.strip()]
    if len(parts) < 3:
        raise ScpiError(f"CH{ch} 头信息异常: {header}")
    x_start, x_stop = float(parts[0]), float(parts[1])

    raw = inst.query(f"CHANnel{ch}:DATA?", DATA_TIMEOUT)
    values = [float(tok) for tok in raw.strip().strip(",").split(",") if tok.strip()]
    if not values:
        raise ScpiError(f"CH{ch} 波形为空")

    n = len(values)
    dt = (x_stop - x_start) / (n - 1) if n > 1 else 0.0
    vmin, vmax = min(values), max(values)
    mean = sum(values) / n
    rms = (sum(v * v for v in values) / n) ** 0.5

    return {
        "channel": ch,
        "points": n,
        "t_start_s": x_start,
        "t_stop_s": x_stop,
        "dt_s": dt,
        "sample_rate_hz": (1.0 / dt) if dt else 0.0,
        "values": values,
        "scale_v_per_div": float(inst.query(f"CHANnel{ch}:SCALe?", DEFAULT_TIMEOUT)),
        "offset_v": float(inst.query(f"CHANnel{ch}:OFFSet?", DEFAULT_TIMEOUT)),
        "unit": "V",
        "min_v": vmin,
        "max_v": vmax,
        "vpp_v": vmax - vmin,
        "mean_v": mean,
        "rms_v": rms,
    }


def resample(values: list[float], n_out: int) -> list[float]:
    """把任意长度序列线性插值到 n_out 点（纯 Python，避免依赖 numpy）。"""
    n_in = len(values)
    if n_in == n_out or n_in == 0:
        return values[:n_out]
    if n_in == 1:
        return [values[0]] * n_out
    out = []
    for i in range(n_out):
        pos = i * (n_in - 1) / (n_out - 1)
        lo = int(pos)
        hi = min(lo + 1, n_in - 1)
        frac = pos - lo
        out.append(values[lo] + (values[hi] - values[lo]) * frac)
    return out


def inst_meta(inst: Scpi) -> dict:
    return {
        "idn": inst.query("*IDN?", DEFAULT_TIMEOUT),
        "acquire_points": inst.query_int("ACQuire:POINts?"),
        "sample_rate_hz": inst.query_float("ACQuire:SRATe?"),
        "timebase_scale_s_per_div": inst.query_float("TIMebase:SCALe?"),
        "timebase_position_s": inst.query_float("TIMebase:POSition?"),
    }


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_idn(args, inst: Scpi) -> int:
    meta = inst_meta(inst)
    print(f"仪器      : {meta['idn']}")
    print(f"存储点数  : {meta['acquire_points']}")
    print(f"采样率    : {meta['sample_rate_hz']:.6g} Sa/s")
    print(f"时基      : {meta['timebase_scale_s_per_div']:.6g} s/div")
    print(f"水平位置  : {meta['timebase_position_s']:.6g} s")
    for ch in range(1, 5):
        state = inst.query(f"CHANnel{ch}:STATe?", DEFAULT_TIMEOUT).strip()
        if state in ("1", "ON"):
            scale = inst.query_float(f"CHANnel{ch}:SCALe?")
            offset = inst.query_float(f"CHANnel{ch}:OFFSet?")
            print(f"CH{ch}      : 开  {scale:.6g} V/div  offset {offset:.6g} V")
    return 0


def cmd_shot(args, inst: Scpi) -> int:
    out_dir = ensure_dir(args.out)
    with Frozen(inst):
        lang = inst.query("HCOPy:LANGuage?", DEFAULT_TIMEOUT).strip().upper()
        original_lang = lang
        if lang != "PNG":
            inst.write("HCOPy:LANGuage PNG")
            time.sleep(0.3)
        t0 = time.time()
        data = inst.query_block("HCOPy:DATA?")
        elapsed = time.time() - t0
        if original_lang and original_lang != "PNG":
            try:
                inst.write(f"HCOPy:LANGuage {original_lang}")
            except Exception:
                pass

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ScpiError("返回数据不是 PNG")

    name = args.name or datetime.now().strftime("shot_%Y%m%d_%H%M%S")
    if not name.lower().endswith(".png"):
        name += ".png"
    path = unique_path(os.path.join(out_dir, name))
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"截图已保存: {path}  ({len(data)} 字节, {elapsed:.2f}s)")
    return 0


# --------------------------------------------------------------------------- #
# 运行模式：连续运行 / 触发（单次）
# --------------------------------------------------------------------------- #
DIVISIONS = 12.0                       # RTB2000 水平 12 格
TIME_LADDER = (200e-9, 500e-9, 1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6,
               200e-6, 500e-6, 1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3)


def snapshot_acq(inst: Scpi) -> dict:
    snap: dict[str, str | None] = {}
    for key, cmd in (
        ("state", "ACQuire:STATe?"),
        ("trig_type", "TRIGger:A:TYPe?"),
        ("trig_mode", "TRIGger:A:MODE?"),
        ("trig_slope", "TRIGger:A:EDGE:SLOPe?"),
        ("trig_source", "TRIGger:A:SOURce?"),
        ("trig_level", "TRIGger:A:LEVel?"),
        ("timebase", "TIMebase:SCALe?"),
        ("position", "TIMebase:POSition?"),
    ):
        try:
            snap[key] = inst.query(cmd, DEFAULT_TIMEOUT).strip()
        except Exception:
            snap[key] = None
    return snap


def restore_acq(inst: Scpi, snap: dict) -> None:
    """按快照还原触发/时基，并恢复原来的运行状态。"""
    mode = snap.get("trig_mode") or ""
    if mode.upper().startswith("NORM"):        # 回读是 NORM，写回去要用 NORMal
        mode = "NORMal"
    try:
        if snap.get("trig_type"):
            inst.write(f"TRIGger:A:TYPe {snap['trig_type']}")
        if snap.get("trig_source"):
            inst.write(f"TRIGger:A:SOURce {snap['trig_source']}")
        if snap.get("trig_slope"):
            inst.write(f"TRIGger:A:EDGE:SLOPe {snap['trig_slope']}")
        if mode:
            inst.write(f"TRIGger:A:MODE {mode}")
        if snap.get("trig_level"):
            inst.write(f"TRIGger:A:LEVel {snap['trig_level']}")
        if snap.get("timebase"):
            inst.write(f"TIMebase:SCALe {snap['timebase']}")
        if snap.get("position"):
            inst.write(f"TIMebase:POSition {snap['position']}")
        time.sleep(0.2)
        if snap.get("state") == "RUN":
            inst.write("RUNContinous")
        elif snap.get("state") in ("STOP", "BRE", "COMP"):
            inst.write("STOP")
    except Exception:
        pass


def capture_continuous(inst: Scpi, settle: float) -> None:
    """连续运行：跑一段时间后冻结，供读取。"""
    inst.write("RUNContinous")
    time.sleep(max(0.2, settle))
    inst.write("STOP")
    time.sleep(0.35)


def capture_trigger(inst: Scpi, args) -> dict:
    """触发模式：配置边沿触发 → RUNSingle → 等采集完成。"""
    slope = "POS" if args.trig_slope.lower().startswith("r") else "NEG"
    tmode = "AUTO" if args.trig_mode.lower().startswith("a") else "NORMal"
    # 触发电平：未指定时先抓一帧，取信号高低电平中点（比 FINDlevel 稳，跨机型一致）
    level = args.trig_level
    if level is None:
        try:
            ch_digits = "".join(c for c in str(args.trig_source) if c.isdigit()) or "1"
            capture_continuous(inst, min(max(args.settle, 0.4), 1.0))
            probe_wave = read_channel(inst, int(ch_digits))
            if probe_wave:
                lo = percentile(probe_wave["values"], 0.05)
                hi = percentile(probe_wave["values"], 0.95)
                if hi - lo > 0.01:
                    level = (lo + hi) / 2.0
        except Exception:
            level = None

    inst.write("TRIGger:A:TYPe EDGE")
    inst.write(f"TRIGger:A:SOURce {args.trig_source}")
    inst.write(f"TRIGger:A:EDGE:SLOPe {slope}")
    inst.write(f"TRIGger:A:MODE {tmode}")
    if level is not None:
        inst.write(f"TRIGger:A:LEVel {level:g}")
        print(f"触发电平: {level:.4f} V（按信号中点自动设定）")
    else:
        inst.write("TRIGger:A:FINDlevel")
        print("触发电平: 使用仪器 FINDlevel 自动查找")
    time.sleep(0.3)

    inst.write("RUNSingle")
    deadline = time.time() + args.trig_wait
    state = ""
    while time.time() < deadline:
        time.sleep(0.15)
        try:
            state = inst.query("ACQuire:STATe?", DEFAULT_TIMEOUT).strip()
        except Exception:
            state = ""
        if state in ("COMP", "STOP", "BRE"):
            break

    info = {
        "mode": "trigger",
        "trigger_source": args.trig_source,
        "trigger_slope": "rise" if slope == "POS" else "fall",
        "trigger_mode": "auto" if tmode == "AUTO" else "normal",
        "trigger_level_v": None,
        "final_state": state,
        "timed_out": state not in ("COMP", "STOP", "BRE"),
    }
    try:
        info["trigger_level_v"] = float(inst.query("TRIGger:A:LEVel?", DEFAULT_TIMEOUT))
    except Exception:
        pass
    if info["timed_out"]:
        print(f"警告: 等待触发超时（{args.trig_wait:g}s，状态 {state}），按当前画面导出")
        inst.write("STOP")
        time.sleep(0.3)
    else:
        print(f"触发完成: 状态 {state}，电平 {info['trigger_level_v']} V，"
              f"{info['trigger_slope']} 沿，{info['trigger_mode']} 模式")
    return info


def edge_indices(values: list[float], low: float, high: float) -> list[int]:
    """返回上升沿（越过中值）的样本下标。"""
    mid = (low + high) / 2.0
    hyst = (high - low) * 0.05
    out: list[int] = []
    state = values[0] > mid
    for i in range(1, len(values)):
        if state and values[i] < mid - hyst:
            state = False
        elif (not state) and values[i] > mid + hyst:
            state = True
            out.append(i)
    return out


def survey_period(inst: Scpi, args, ch: int = 1) -> float:
    """扫描时基直到能看到足够边沿，返回信号周期（秒）。"""
    for tb in (1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 500e-6, 50e-3, 100e-3):
        inst.write(f"TIMebase:SCALe {tb:.6g}")
        inst.write("TIMebase:POSition 0")
        capture_continuous(inst, args.settle)
        wave = read_channel(inst, ch)
        if wave is None:
            continue
        low = percentile(wave["values"], 0.05)
        high = percentile(wave["values"], 0.95)
        if (high - low) < 0.05:
            continue
        rises = edge_indices(wave["values"], low, high)
        print(f"  扫描时基 {tb * 1e3:.3f} ms/div: {len(rises)} 个上升沿")
        if len(rises) >= 3:
            period = (rises[-1] - rises[0]) * wave["dt_s"] / (len(rises) - 1)
            print(f"  实测周期 {period * 1e6:.3f} µs（{1 / period:.4f} Hz）")
            return period
    raise ScpiError("扫描多个时基仍找不到足够边沿，无法测周期")


def pick_timebase_value(seconds: float) -> float:
    """把目标时基（s/div）归到示波器的 1-2-5 档位。"""
    for step in TIME_LADDER:
        if step >= seconds:
            return step
    return TIME_LADDER[-1]


def pick_timebase(period: float, periods: int) -> float:
    return pick_timebase_value(period * periods / DIVISIONS)


PRESET_RATIO = {          # 预设 → 窗口相当于多少个信号周期
    "freq": 10.0,         # 看频率/占空比：约 10 个周期
    "detail": 1.5,        # 看一个周期的细节
    "edge": 0.05,         # 看边沿：约 1/20 个周期，采样率拉满
}


def parse_seconds(text: str) -> float:
    """把 '20us' / '20µs' / '0.00002' / '1ms' 解析成秒。"""
    raw = str(text).strip().lower().replace("µ", "u").replace("μ", "u").replace(" ", "")
    for unit in ("ns", "us", "ms", "s"):
        if raw.endswith(unit):
            return float(raw[: -len(unit)]) * {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}[unit]
    return float(raw)


def apply_timebase(inst: Scpi, seconds: float) -> float:
    """设置时基（s/div），回读并返回仪器实际采用的值。"""
    inst.write(f"TIMebase:SCALe {seconds:.6g}")
    time.sleep(0.3)
    actual = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
    if abs(actual - seconds) > 1e-12 * max(1.0, abs(seconds)):
        print(f"提示: 请求时基 {seconds * 1e6:.4g} µs/div，仪器落在 {actual * 1e6:.4g} µs/div")
    else:
        print(f"时基: {actual * 1e6:.4g} µs/div（{actual * DIVISIONS * 1e3:.4g} ms 窗口）")
    return actual


# --------------------------------------------------------------------------- #
# 波形图输出（固定格式之一：CSV + PNG）
# --------------------------------------------------------------------------- #
CH_COLORS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#d62728")


def save_plot_png(png_path: str, times: list[float], cols: list[list[float]],
                  header: list[str], title: str, subtitles: list[str]) -> str:
    """生成波形图。优先 matplotlib；没有则用内置纯标准库 PNG 渲染（无文字）。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return _png_fallback(png_path, times, cols, header)

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    t_ms = [t * 1e3 for t in times]
    fig, ax = plt.subplots(figsize=(12, 6.6), dpi=110)
    for i, col in enumerate(cols):
        ax.plot(t_ms, col, lw=0.9, color=CH_COLORS[i % len(CH_COLORS)], label=header[i + 1])
    ax.set_xlabel("时间 (ms)")
    ax.set_ylabel("电压 (V)")
    ax.grid(True, which="both", ls=":", lw=0.6, alpha=0.7)
    ax.legend(loc="upper right", fontsize=9)
    ax.set_title(title + ("\n" + " ｜ ".join(subtitles) if subtitles else ""), fontsize=10)
    fig.tight_layout()
    fig.savefig(png_path, dpi=110)
    plt.close(fig)
    return png_path


def _png_fallback(png_path: str, times: list[float], cols: list[list[float]],
                  header: list[str]) -> str:
    """无 matplotlib 时的兜底：纯 zlib 生成 PNG（画坐标框 + 曲线，无文字）。"""
    import struct
    import zlib

    w, h = 1200, 650
    m = 60
    px = bytearray(b"\xff" * (w * h * 3))

    def put(x: int, y: int, rgb: tuple[int, int, int]) -> None:
        if 0 <= x < w and 0 <= y < h:
            o = (y * w + x) * 3
            px[o:o + 3] = bytes(rgb)

    flat = [v for c in cols for v in c]
    vmin, vmax = min(flat), max(flat)
    if vmax - vmin < 1e-9:
        vmin, vmax = vmin - 1.0, vmax + 1.0
    tmin, tmax = times[0], times[-1]
    if tmax - tmin < 1e-12:
        tmax = tmin + 1e-3

    def mx(t: float) -> int:
        return m + int((t - tmin) / (tmax - tmin) * (w - 2 * m))

    def my(v: float) -> int:
        return h - m - int((v - vmin) / (vmax - vmin) * (h - 2 * m))

    for x in range(m, w - m + 1):
        put(x, m, (170, 170, 170))
        put(x, h - m, (170, 170, 170))
    for y in range(m, h - m + 1):
        put(m, y, (170, 170, 170))
        put(w - m, y, (170, 170, 170))

    for i, col in enumerate(cols):
        rgb = tuple(int(CH_COLORS[i % len(CH_COLORS)][k:k + 2], 16) for k in (1, 3, 5))
        prev = None
        for t, v in zip(times, col):
            cur = (mx(t), my(v))
            if prev is not None:
                x0, y0 = prev
                x1, y1 = cur
                steps = max(abs(x1 - x0), abs(y1 - y0), 1)
                for s in range(steps + 1):
                    put(int(x0 + (x1 - x0) * s / steps), int(y0 + (y1 - y0) * s / steps), rgb)
            prev = cur

    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack("!I", len(data)) + typ + data +
                struct.pack("!I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    ihdr = struct.pack("!IIBBBBB", w, h, 8, 2, 0, 0, 0)
    with open(png_path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                 + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
    return png_path


# --------------------------------------------------------------------------- #
# csv 子命令（固定输出：CSV + 波形图 + JSON 元数据）
# --------------------------------------------------------------------------- #
def cmd_csv(args, inst: Scpi) -> int:
    out_dir = ensure_dir(args.out)
    channels = [int(c) for c in args.channels.split(",") if c.strip()] if args.channels else [1, 2, 3, 4]

    meta = inst_meta(inst)
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    snap = snapshot_acq(inst)
    original_fmt = inst.query("FORMat:DATA?", DEFAULT_TIMEOUT).strip()
    trig_info: dict | None = None
    n_periods = 0
    used_tb = float(snap.get("timebase") or 0)
    used_srate = meta["sample_rate_hz"]

    try:
        if not original_fmt.upper().startswith("ASC"):
            inst.write("FORMat:DATA ASC")
            time.sleep(0.3)

        if args.timebase is not None:
            apply_timebase(inst, parse_seconds(args.timebase))
            inst.write("TIMebase:POSition 0")
        elif args.preset:
            period = survey_period(inst, args, ch=channels[0])
            ratio = PRESET_RATIO[args.preset]
            target = max(period * ratio / DIVISIONS, 200e-9)
            tb = pick_timebase_value(target)
            print(f"预设 {args.preset}: 窗口约 {ratio:g} 个周期 → 采用 {tb * 1e6:.4g} µs/div")
            apply_timebase(inst, tb)
            inst.write("TIMebase:POSition 0")
        elif args.periods:
            period = survey_period(inst, args, ch=channels[0])
            tb = pick_timebase(period, args.periods)
            print(f"采用时基 {tb * 1e3:.4f} ms/div（窗口 {tb * DIVISIONS * 1e3:.3f} ms）")
            inst.write(f"TIMebase:SCALe {tb:.6g}")
            inst.write("TIMebase:POSition 0")

        if args.mode == "trigger":
            trig_info = capture_trigger(inst, args)
        else:
            capture_continuous(inst, args.settle)

        waves = []
        for ch in channels:
            wave = read_channel(inst, ch)
            if wave is not None:
                waves.append(wave)

        # 记录“实际采集时”的时基与采样率（finally 里才会还原）
        used_tb = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
        used_srate = float(inst.query("ACQuire:SRATe?", DEFAULT_TIMEOUT))
    finally:
        try:
            if not original_fmt.upper().startswith("ASC"):
                inst.write(f"FORMat:DATA {original_fmt.split(',')[0]}")
        except Exception:
            pass
        restore_acq(inst, snap)

    if not waves:
        raise ScpiError("没有开启任何指定通道")

    n = max(w["points"] for w in waves)
    base = waves[0]
    t0 = base["t_start_s"]
    dt = base["dt_s"]
    times = [t0 + i * dt for i in range(n)]
    cols = [w["values"] if w["points"] == n else resample(w["values"], n) for w in waves]

    # 限定周期：按第一个通道的上升沿精确截取
    if args.periods:
        vals = cols[0]
        low = percentile(vals, 0.05)
        high = percentile(vals, 0.95)
        rises = edge_indices(vals, low, high)
        if len(rises) >= args.periods + 1:
            i0, i1 = rises[0], rises[args.periods]
            times = times[i0:i1 + 1]
            cols = [c[i0:i1 + 1] for c in cols]
            n_periods = args.periods
            print(f"已截取 {args.periods} 个完整周期：{len(times)} 点，"
                  f"{(times[-1] - times[0]) * 1e3:.4f} ms")
        else:
            print(f"警告: 窗口内只有 {len(rises)} 个上升沿，不足 {args.periods + 1} 个，"
                  f"按整窗导出")
        n = len(times)

    header = ["Time (s)"] + [f"CH{w['channel']} (V)" for w in waves]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = unique_base(out_dir, args.name or f"scope_{stamp}", (".csv", ".json", ".png"))
    csv_path = os.path.join(out_dir, base_name + ".csv")
    json_path = os.path.join(out_dir, base_name + ".json")
    png_path = os.path.join(out_dir, base_name + ".png")

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for i in range(n):
            writer.writerow([f"{times[i]:.9e}"] + [f"{c[i]:.6e}" for c in cols])

    mode_desc = "连续运行" if args.mode != "trigger" else "触发单次"
    title = f"{meta['idn'].split(',')[1] if ',' in meta['idn'] else 'Scope'} 波形"
    subtitles = [
        f"{mode_desc}",
        f"{len(times)} 点 / {1 / dt / 1e3:.3f} kSa/s",
        f"时基 {used_tb * 1e3:.3g} ms/div",
    ]
    if trig_info and trig_info.get("trigger_level_v") is not None:
        subtitles.append(f"触发电平 {trig_info['trigger_level_v']:.3g} V "
                         f"{trig_info['trigger_slope']}")
    if n_periods:
        subtitles.append(f"{n_periods} 个周期")

    plot_err = None
    try:
        save_plot_png(png_path, times, cols, header, title, subtitles)
    except Exception as exc:                       # 绘图失败不影响 CSV
        plot_err = str(exc)
        print(f"警告: 波形图生成失败 -> {exc}", file=sys.stderr)

    meta_out = {
        "instrument": meta["idn"],
        "captured_at": captured_at,
        "host": args.ip,
        "port": args.port,
        "run": trig_info or {"mode": "continuous", "settle_s": args.settle},
        "acquisition": {
            "memory_points": meta["acquire_points"],
            "sample_rate_hz": used_srate,
            "timebase_scale_s_per_div": used_tb,
            "timebase_position_s": meta["timebase_position_s"],
            "restored_settings": snap,
        },
        "export": {
            "csv": os.path.basename(csv_path),
            "png": os.path.basename(png_path) if plot_err is None else None,
            "json": os.path.basename(json_path),
            "periods": n_periods or None,
            "points": len(times),
            "dt_s": dt,
            "t_start_s": times[0],
            "columns": header,
        },
        "channels": [{k: v for k, v in w.items() if k != "values"} for w in waves],
    }
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(meta_out, fh, ensure_ascii=False, indent=2)

    print(f"CSV   : {csv_path}")
    print(f"波形图: {png_path}" + ("" if plot_err is None else "（失败）"))
    print(f"元数据: {json_path}")
    print(f"点数  : {len(times)}   采样间隔 {dt:.6g} s（{1 / dt / 1e3:.3f} kSa/s）")
    print(f"时间窗: {times[0]:.6e} s ~ {times[-1]:.6e} s")
    for w in waves:
        print(f"CH{w['channel']}: min {w['min_v']:+.4f} V  max {w['max_v']:+.4f} V  "
              f"Vpp {w['vpp_v']:.4f} V  RMS {w['rms_v']:.4f} V  mean {w['mean_v']:+.4f} V")
    return 0


def cmd_stats(args, inst: Scpi) -> int:
    channels = [int(c) for c in args.channels.split(",") if c.strip()] if args.channels else [1, 2, 3, 4]
    original_fmt = inst.query("FORMat:DATA?", DEFAULT_TIMEOUT).strip()
    with Frozen(inst):
        try:
            if not original_fmt.upper().startswith("ASC"):
                inst.write("FORMat:DATA ASC")
                time.sleep(0.3)
            for ch in channels:
                wave = read_channel(inst, ch)
                if wave is None:
                    continue
                print(f"CH{ch}: {wave['points']} 点  "
                      f"min {wave['min_v']:+.6g} V  max {wave['max_v']:+.6g} V  "
                      f"Vpp {wave['vpp_v']:.6g} V  RMS {wave['rms_v']:.6g} V  "
                      f"mean {wave['mean_v']:+.6g} V")
        finally:
            if original_fmt and not original_fmt.upper().startswith("ASC"):
                try:
                    inst.write(f"FORMat:DATA {original_fmt.split(',')[0]}")
                except Exception:
                    pass
    return 0


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def cmd_analyze(args, inst: Scpi | None = None) -> int:
    """离线分析 csv 子命令导出的文件：电平、Vpp、频率、占空比、边沿时间。"""
    import statistics as st

    path = args.file
    times: list[float] = []
    volts: list[float] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            times.append(float(row[0]))
            volts.append(float(row[1]))
    if len(times) < 10:
        raise ScpiError("CSV 点数太少，无法分析")

    n = len(times)
    dt = times[1] - times[0]
    low = percentile(volts, 0.05)
    high = percentile(volts, 0.95)
    mid = (low + high) / 2.0
    amp = high - low
    hyst = amp * 0.05

    state = volts[0] > mid
    rise_times: list[float] = []
    fall_times: list[float] = []
    for i in range(1, n):
        v_prev, v = volts[i - 1], volts[i]
        if v == v_prev:
            continue
        if state and v < mid - hyst:
            fall_times.append(times[i - 1] + (mid - v_prev) / (v - v_prev) * dt)
            state = False
        elif (not state) and v > mid + hyst:
            rise_times.append(times[i - 1] + (mid - v_prev) / (v - v_prev) * dt)
            state = True

    periods = [rise_times[i + 1] - rise_times[i] for i in range(len(rise_times) - 1)]
    duties: list[float] = []
    for i, rt in enumerate(rise_times):
        nxt_fall = [ft for ft in fall_times if ft > rt]
        if not nxt_fall:
            continue
        period_here = periods[i] if i < len(periods) else None
        if period_here and period_here > 0:
            duties.append((nxt_fall[0] - rt) / period_here)

    def edge_time(start_idx: int, rising: bool) -> float | None:
        lo_thr = low + amp * (args.edge_pct / 100.0)
        hi_thr = high - amp * (args.edge_pct / 100.0)
        t_lo = t_hi = None
        for i in range(start_idx, min(start_idx + 5000, n)):
            if rising:
                if t_lo is None and volts[i] >= lo_thr:
                    t_lo = times[i]
                elif t_lo is not None and volts[i] >= hi_thr:
                    t_hi = times[i]
                    break
            else:
                if t_lo is None and volts[i] <= hi_thr:
                    t_lo = times[i]
                elif t_lo is not None and volts[i] <= lo_thr:
                    t_hi = times[i]
                    break
        return None if (t_lo is None or t_hi is None) else abs(t_hi - t_lo)

    rise_idx = [i for i in range(1, n) if volts[i - 1] < mid <= volts[i]]
    fall_idx = [i for i in range(1, n) if volts[i - 1] > mid >= volts[i]]
    rts = [v for v in (edge_time(i, True) for i in rise_idx[:5]) if v]
    fts = [v for v in (edge_time(i, False) for i in fall_idx[:5]) if v]

    print(f"文件            : {path}")
    print(f"点数/采样率     : {n} 点 / {1 / dt:.6g} Sa/s（间隔 {dt * 1e9:.1f} ns）")
    print(f"时间窗          : {times[0]:.6g} s ~ {times[-1]:.6g} s")
    print()
    print(f"低电平 / 高电平 : {low:+.4f} V / {high:+.4f} V")
    print(f"Vpp             : {amp:.4f} V   (Vmin {min(volts):+.4f} / Vmax {max(volts):+.4f})")
    print(f"均值 / RMS      : {st.fmean(volts):+.4f} V / {(sum(v * v for v in volts) / n) ** 0.5:.4f} V")
    print()
    print(f"上升沿 / 下降沿 : {len(rise_times)} / {len(fall_times)}")
    if periods:
        print(f"周期            : {st.fmean(periods) * 1e6:.3f} µs  (抖动 σ = {st.pstdev(periods) * 1e6:.3f} µs)")
        print(f"频率            : {1.0 / st.fmean(periods):.6f} Hz")
    if len(rise_times) >= 2:
        span = rise_times[-1] - rise_times[0]
        print(f"频率(全窗回归)  : {(len(rise_times) - 1) / span:.6f} Hz  "
              f"（{len(rise_times) - 1} 个周期 / {span * 1e3:.3f} ms）")
    if duties:
        print(f"占空比          : {st.fmean(duties) * 100:.2f} %  "
              f"(min {min(duties) * 100:.2f} / max {max(duties) * 100:.2f})")
    if rts:
        print(f"上升时间        : {st.fmean(rts) * 1e6:.3f} µs  "
              f"(限值: 采样间隔 {dt * 1e6:.3f} µs，小于此值说明边沿快于采样率)")
    if fts:
        print(f"下降时间        : {st.fmean(fts) * 1e6:.3f} µs")
    return 0


def cmd_scpi(args, inst: Scpi) -> int:
    command = " ".join(args.command)
    if command.strip().endswith("?"):
        print(inst.query(command, DATA_TIMEOUT))
    else:
        inst.write(command)
        time.sleep(0.2)
        print("OK（写入命令已发送）")
    return 0


def cmd_discover(args, inst_unused=None) -> int:
    cidrs = [args.cidr] if args.cidr else local_cidrs(full=args.full)

    hosts = []
    for c in cidrs:
        try:
            net = ipaddress.ip_network(c, strict=False)
        except ValueError:
            print(f"跳过无效网段: {c}")
            continue
        hosts.extend(str(h) for h in net.hosts())

    print(f"扫描 {len(hosts)} 个地址的 5025 端口 ...")
    found = []
    with futures.ThreadPoolExecutor(max_workers=args.threads) as pool:
        futs = {pool.submit(probe_port, h, args.port, args.timeout): h for h in hosts}
        for fut in futures.as_completed(futs):
            host = futs[fut]
            try:
                if fut.result():
                    found.append(host)
            except Exception:
                pass

    if not found:
        print("未发现应答的示波器（可试 --cidr 指定网段，或 --full 扫整个 /16）")
        return 1
    for host in sorted(found, key=lambda x: ipaddress.ip_address(x)):
        probe = None
        try:
            probe = Scpi(host, args.port, timeout=3.0, retries=1)
            print(f"{host}  ->  {probe.query('*IDN?', 5.0)}")
        except Exception as exc:
            print(f"{host}  ->  端口开放但查询失败: {exc}")
        finally:
            if probe is not None:
                probe.close()
    return 0


def probe_port(host: str, port: int, timeout: float) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def local_cidrs(full: bool = False) -> list[str]:
    """由本机 IPv4 地址推导待扫描网段。/16 仅在 --full 时整段扫描。"""
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except Exception:
        pass
    # 兜底：一条 UDP connect 拿到出口地址
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        addrs.add(probe.getsockname()[0])
        probe.close()
    except Exception:
        pass

    cidrs = []
    for addr in addrs:
        if addr.startswith("127."):
            continue
        if addr.startswith("169.254."):
            cidrs.append(f"169.254.0.0/16" if full else str(ipaddress.ip_network(f"{addr}/24", strict=False)))
        else:
            cidrs.append(str(ipaddress.ip_network(f"{addr}/24", strict=False)))
    return sorted(set(cidrs))


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def ensure_dir(path: str | None) -> str:
    target = path or os.path.join(os.getcwd(), "scope_out")
    os.makedirs(target, exist_ok=True)
    return target


def unique_path(path: str) -> str:
    """同名文件已存在时追加 _2 / _3，避免同一秒内两次采集互相覆盖。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def unique_base(out_dir: str, base_name: str, exts: tuple[str, ...]) -> str:
    if not any(os.path.exists(os.path.join(out_dir, base_name + e)) for e in exts):
        return base_name
    i = 2
    while any(os.path.exists(os.path.join(out_dir, f"{base_name}_{i}{e}")) for e in exts):
        i += 1
    return f"{base_name}_{i}"


def resolve_ip(arg_ip: str | None) -> str:
    ip = arg_ip or os.environ.get(ENV_IP)
    if not ip:
        raise SystemExit(
            "未指定仪器地址。请用 --ip 169.254.x.x，或设置环境变量 "
            f"{ENV_IP}，或先运行：scope.py discover"
        )
    return ip


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scope.py",
        description="R&S RTB2000 系列示波器以太网 SCPI 控制（零第三方依赖）",
        epilog=f"仪器地址优先级: --ip > 环境变量 {ENV_IP} > discover 结果",
    )
    parser.add_argument("--ip", help="仪器 IP，例如 169.254.101.84")
    parser.add_argument("--port", type=int, default=int(os.environ.get(ENV_PORT, DEFAULT_PORT)),
                        help=f"SCPI 端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="连接超时秒数")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("idn", help="读取仪器标识与通道设置")

    p_shot = sub.add_parser("shot", help="抓取屏幕截图 PNG")
    p_shot.add_argument("--out", help="输出目录，默认 ./scope_out")
    p_shot.add_argument("--name", help="文件名（不含扩展名亦可）")

    p_csv = sub.add_parser("csv", help="采集波形并输出 CSV + 波形图（固定输出，另附 JSON 元数据）")
    p_csv.add_argument("--channels", default="", help="通道列表，如 1,2；默认全部开启的通道")
    p_csv.add_argument("--out", help="输出目录，默认 ./scope_out")
    p_csv.add_argument("--name", help="文件名前缀")
    p_csv.add_argument("--mode", choices=["continuous", "trigger"], default="continuous",
                       help="运行模式：continuous=连续运行（默认）；trigger=边沿触发单次采集")
    p_csv.add_argument("--trig-source", default="CH1", help="触发源，默认 CH1")
    p_csv.add_argument("--trig-slope", choices=["rise", "fall"], default="rise",
                       help="触发沿：rise=上升沿（默认）/ fall=下降沿")
    p_csv.add_argument("--trig-level", type=float, default=None,
                       help="触发电平（V）；不给则先抓一帧，取信号 5%%/95%% 分位的中点")
    p_csv.add_argument("--trig-mode", choices=["auto", "normal"], default="auto",
                       help="触发方式：auto=自动（默认）/ normal=标准（只认真实触发）")
    p_csv.add_argument("--trig-wait", type=float, default=10.0, help="等待触发的超时秒数，默认 10")
    p_csv.add_argument("--settle", type=float, default=1.2, help="连续运行采集时长（秒），默认 1.2")
    p_csv.add_argument("--periods", type=int, default=0,
                       help="只截取 N 个完整周期（自动扫描时基提高采样率），0=不限制")
    p_csv.add_argument("--timebase", default=None,
                       help="直接指定时基（s/div），支持 20us / 100us / 1ms / 2e-5 写法；"
                            "指定后不再自动扫时基，采集结束自动还原")
    p_csv.add_argument("--preset", choices=["freq", "detail", "edge"], default=None,
                       help="按用途自动选时基：freq≈10 个周期(默认看频率/占空比)；"
                            "detail≈1.5 个周期(看单周期细节)；edge≈1/20 个周期(看边沿，采样率最高)")

    p_stats = sub.add_parser("stats", help="统计波形（不落盘）")
    p_stats.add_argument("--channels", default="", help="通道列表，如 1,2")

    p_an = sub.add_parser("analyze", help="离线分析已导出的 CSV：电平/频率/占空比/边沿时间")
    p_an.add_argument("file", help="csv 子命令产出的 CSV 路径")
    p_an.add_argument("--edge-pct", type=float, default=10.0,
                      help="边沿时间取 10%%~90%% 区间，默认 10")

    p_scpi = sub.add_parser("scpi", help="任意 SCPI 命令直通")
    p_scpi.add_argument("command", nargs=argparse.REMAINDER, help="要发送的命令，以 ? 结尾则读取响应")

    p_disc = sub.add_parser("discover", help="扫描网段寻找示波器")
    p_disc.add_argument("--cidr", help="指定扫描网段，如 169.254.101.0/24")
    p_disc.add_argument("--full", action="store_true", help="169.254.x.x 时扫描整个 /16（较慢）")
    p_disc.add_argument("--threads", type=int, default=256, help="并发数，默认 256")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "discover":
        try:
            return cmd_discover(args)
        except KeyboardInterrupt:
            print("\n已中断", file=sys.stderr)
            return 130
        except Exception as exc:                      # noqa: BLE001 - 统一收敛为可读错误
            print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    if args.cmd == "analyze":  # 纯离线分析，不需要连仪器
        try:
            return cmd_analyze(args)
        except Exception as exc:                      # noqa: BLE001
            print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2

    ip = resolve_ip(args.ip)
    args.ip = ip

    handlers = {
        "idn": cmd_idn,
        "shot": cmd_shot,
        "csv": cmd_csv,
        "stats": cmd_stats,
        "scpi": cmd_scpi,
    }

    inst = None
    try:
        inst = Scpi(ip, args.port, timeout=args.timeout)
        return handlers[args.cmd](args, inst)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except ScpiError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:                          # noqa: BLE001
        print(f"错误: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if inst is not None:
            inst.close()


if __name__ == "__main__":
    sys.exit(main())
