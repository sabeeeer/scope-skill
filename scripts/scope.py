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
import math
import os
import sys
import time
from datetime import datetime


def _ensure_windows_socket_environment() -> None:
    """补齐被精简启动器省略的 Windows 目录变量，仅影响当前进程。"""
    if os.name != "nt" or (os.environ.get("SystemRoot") and os.environ.get("WINDIR")):
        return
    import ctypes

    directory = ctypes.create_unicode_buffer(32768)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(directory, len(directory)):
        raise RuntimeError("无法获取 Windows 目录，Winsock 可能无法初始化")
    os.environ.setdefault("SystemRoot", directory.value)
    os.environ.setdefault("WINDIR", directory.value)


_ensure_windows_socket_environment()
import socket

try:  # Windows 控制台默认 GBK，强制 UTF-8 避免 UnicodeEncodeError
    # line_buffering：输出被重定向/管道时默认是全缓冲，进程结束前一行都不打印，
    # 看起来就像"卡死"。改成按行刷新，进度能实时看到。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

DEFAULT_PORT = 5025
DEFAULT_TIMEOUT = 8.0             # 单条查询超时：宁可快速失败并报清楚，也不要让人以为死机
                                  # （20 s 的默认值会让"仪器无响应"看起来像脚本卡死）
COOL_AFTER_STOP = 0.45            # 每次 STOP 后的冷却时间：高频 RUN/STOP 会把仪器的 SCPI 服务打崩（实测掉线两次）
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

    def drain(self, quiet: float = 0.12) -> int:
        """丢弃滞留的"迟到响应"，防止后续查询整体错位。

        查询超时后仪器往往仍会把响应发过来；不清理的话，下一条查询就会读到上一条的
        结果（典型症状：把 *IDN? 的字符串当浮点数解析）。
        """
        assert self._sock is not None
        dropped = len(self._buf)
        self._buf = b""
        while True:
            self._sock.settimeout(quiet)
            try:
                chunk = self._sock.recv(262144)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            dropped += len(chunk)
        return dropped

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
            time.sleep(COOL_AFTER_STOP)
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
    """读取当前冻结的波形；读操作不得重新运行采集或替换记录。

    先查记录点数：仪器处于 STOP/BRE 且还没采到数据时点是 0，
    此时发 `CHANnel<n>:DATA?` 会一直等不到响应（实测空等到 90 s 超时），
    所以必须先判 0 并立刻返回 None，让调用方去调时基重采。
    """
    if inst.query(f"CHANnel{ch}:STATe?", DEFAULT_TIMEOUT).strip() not in ("1", "ON"):
        return None

    try:
        points = int(float(inst.query(f"CHANnel{ch}:DATA:POINts?", DEFAULT_TIMEOUT)))
    except Exception:                      # noqa: BLE001  老固件可能不支持，退化为直接读
        points = -1
    if points == 0:
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
# 屏幕格数：RTB2000 是水平 12 格 / 垂直 8 格；其他机型不同，可用环境变量覆盖
DIVISIONS = float(os.environ.get("SCOPE_HDIV", "12"))
VERT_DIVISIONS = float(os.environ.get("SCOPE_VDIV", "8"))
TIME_LADDER = (200e-9, 500e-9, 1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6,
               200e-6, 500e-6, 1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3,
               200e-3, 500e-3, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0,
               200.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0, 25000.0,
               50000.0, 100000.0)
MAX_TIMEBASE = 1e5                     # 时基搜索上限（s/div）：按"大量程仪器"留足余量，
                                       # 不同机型真实上限由仪器自己拒绝并回读校正
MIN_TIMEBASE = 1e-12                   # 时基搜索下限（s/div）：同样留余量给高带宽机型
# 垂直档位（V/div）：从 µV/div 级一路到 kV/div 级，覆盖小信号与大电压探头
SCALE_LADDER = (1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4,
                1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 0.1, 0.2, 0.5,
                1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0)
DEFAULT_TRIG_LEVEL_V = 1.0             # watch 默认触发电平：高于探头接上瞬间的扰动杂波


def snapshot_acq(inst: Scpi, channels: tuple[int, ...] | list[int] = ()) -> dict:
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
        ("acquire_points", "ACQuire:POINts?"),
    ):
        try:
            snap[key] = inst.query(cmd, DEFAULT_TIMEOUT).strip()
        except Exception:
            snap[key] = None
    for ch in channels:
        for suffix, command in (("scale", "SCALe?"), ("offset", "OFFSet?")):
            key = f"ch{ch}_{suffix}"
            try:
                snap[key] = inst.query(f"CHANnel{ch}:{command}", DEFAULT_TIMEOUT).strip()
            except Exception:
                snap[key] = None
    return snap


def prepare_acquisition_depth(inst: Scpi, max_points: int = 10000) -> None:
    """在新采集开始前限制存储深度，避免读取时替换已冻结的记录。"""
    points = inst.query_int("ACQuire:POINts?")
    if points > max_points:
        inst.write(f"ACQuire:POINts {max_points}")


def restore_acq(inst: Scpi, snap: dict, preserve_continuous_display: bool = False) -> None:
    """还原临时设置；连续模式成功采集后保留数据调好的显示刻度。"""
    mode = "AUTO" if preserve_continuous_display else (snap.get("trig_mode") or "")
    if mode.upper().startswith("NORM"):        # 回读是 NORM，写回去要用 NORMal
        mode = "NORMal"
    commands = []
    for key, name in (
        ("trig_type", "TRIGger:A:TYPe"),
        ("trig_source", "TRIGger:A:SOURce"),
        ("trig_slope", "TRIGger:A:EDGE:SLOPe"),
        ("trig_level", "TRIGger:A:LEVel"),
        ("timebase", "TIMebase:SCALe"),
        ("position", "TIMebase:POSition"),
        ("acquire_points", "ACQuire:POINts"),
    ):
        if snap.get(key) and not (preserve_continuous_display and key in ("timebase", "position")):
            commands.append(f"{name} {snap[key]}")
    if mode:
        commands.append(f"TRIGger:A:MODE {mode}")
    for ch in range(1, 5):
        for suffix, name in (("scale", "SCALe"), ("offset", "OFFSet")):
            value = snap.get(f"ch{ch}_{suffix}")
            if value and not preserve_continuous_display:
                commands.append(f"CHANnel{ch}:{name} {value}")
    if preserve_continuous_display or snap.get("state") == "RUN":
        commands.append("RUNContinous")
    elif snap.get("state") in ("STOP", "BRE", "COMP"):
        commands.append("STOP")
    errors = []
    for command in commands:
        try:
            inst.write(command)
        except Exception as exc:
            errors.append(f"{command}: {exc}")
    if commands:
        time.sleep(0.2)
    for key, query in (("acquire_points", "ACQuire:POINts?"), ("state", "ACQuire:STATe?")):
        expected = "RUN" if preserve_continuous_display and key == "state" else snap.get(key)
        if expected is None:
            continue
        try:
            actual = inst.query(query, DEFAULT_TIMEOUT).strip()
            if key == "acquire_points":
                matches = int(float(actual)) == int(float(expected))
            elif key == "state" and expected in ("STOP", "BRE", "COMP"):
                matches = actual in ("STOP", "BRE", "COMP")
            else:
                matches = actual == expected
            if not matches:
                errors.append(f"{query} 回读 {actual}，预期 {expected}")
        except Exception as exc:
            errors.append(f"{query}: {exc}")
    if errors:
        raise ScpiError(f"还原示波器设置失败: {'; '.join(errors)}")


def nudge_timebase(inst: Scpi, attempt: int = 1, origin: float | None = None) -> float | None:
    """记录为空时的双向盲调：以"起点时基"为基准，先连变快 3 次，再连变慢 3 次。

    为什么要以 origin 为基准而不是当前值：以当前值做 ±10 会来回打转，
    永远出不来（实测 0.1↔1 s/div 死循环）。以起点为基准、单向前进，
    就能保证 6 步覆盖 origin 的 10^-3 ~ 10^3 倍，两个极端都能逃出来。
    """
    try:
        tb = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
    except Exception:                      # noqa: BLE001
        return None
    base = origin if origin and origin > 0 else tb
    if attempt <= 3:
        nxt = base / (10.0 ** attempt)     # 先往"更快"方向找（空记录最常见的原因）
    else:
        nxt = base * (10.0 ** (attempt - 3))   # 再往"更慢"方向找（慢信号场景）
    nxt = max(MIN_TIMEBASE, min(MAX_TIMEBASE, nxt))
    if abs(nxt - tb) < 1e-15:
        return None
    inst.write(f"TIMebase:SCALe {nxt:g}")
    inst.write("TIMebase:POSition 0")
    return nxt


def converge_display(inst: Scpi, ch: int, args, level: float | None,
                     capture_fn, label: str = "", max_rounds: int = 12) -> dict | None:
    """先读波形数据 → 粗调 → 验证 → 细调 → 验证，多步收敛（连续/触发通用）。

    每一步都重新采集并读回数值来判断上一步是否有效，判据全部是数值：
    是否顶到上/下轨、记录里能否数出 >=2 个周期、Vpp 占几格。不使用任何图片。
    """
    fine_done = False
    prev_freq: float | None = None
    wave = None
    nudge_origin: float | None = None
    nudge_count = 0
    for attempt in range(1, max_rounds + 1):
        try:
            wave = capture_fn()
        except Exception as exc:              # noqa: BLE001
            print(f"  {label}第 {attempt} 轮采集失败：{exc}")
            return None
        if wave is None:
            # 记录为空（仪器停在 STOP、或时基太慢进入滚动采集）：先用测量位取频率
            # （RUN 下可读、不打断采集），能取到就直接算时基，省掉反复 RUN/STOP。
            freq_now = measure_frequency(inst, ch)
            if freq_now:
                sp = max(int(getattr(args, "screen_periods", 10) or 0),
                         int(getattr(args, "stable_periods", 10) or 0) + 2)
                tb = pick_timebase(1.0 / freq_now, sp)
                inst.write(f"TIMebase:SCALe {tb:.6g}")
                inst.write("TIMebase:POSition 0")
                print(f"  {label}第 {attempt} 轮：记录为空，但测量框读到 {freq_now:.6g} Hz"
                      f" → 直接设时基 {tb * 1e6:.4g} µs/div")
                continue
            if nudge_origin is None:
                try:
                    nudge_origin = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
                except Exception:             # noqa: BLE001
                    nudge_origin = None
            nudge_count += 1
            nxt = nudge_timebase(inst, nudge_count, nudge_origin)
            print(f"  {label}第 {attempt} 轮：记录为空（仪器可能处于 STOP/滚动采集）"
                  f"→ 盲调时基到 {nxt:.4g} s/div 后重试" if nxt else
                  f"  {label}第 {attempt} 轮：记录为空，且无法读取当前时基")
            continue

        lvl = level if level is not None else (wave["min_v"] + wave["max_v"]) / 2.0

        # 超慢时基（>1 s/div）下采样率极低，记录会"看起来像"一个缓慢的假波形
        # （实测：1 kHz 方波在 500 s/div 被读成 0.004 Hz、68 V）。这种记录一律不采信，
        # 先往更快的方向调，直到进入可信范围再判周期。
        try:
            tb_now = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
        except Exception:                     # noqa: BLE001
            tb_now = 0.0
        if tb_now > 1.0:
            if nudge_origin is None:
                nudge_origin = tb_now
            nudge_count += 1
            nxt = nudge_timebase(inst, nudge_count, nudge_origin)
            print(f"  {label}第 {attempt} 轮：时基 {tb_now:g} s/div 过慢，记录不可信"
                  f"（采样率过低/滚动采集）→ 先调到 {nxt:.4g} s/div" if nxt else
                  f"  {label}第 {attempt} 轮：时基 {tb_now:g} s/div 过慢且无法调整")
            continue

        hit_hi, hit_lo = detect_clipping(wave)
        freq = estimate_frequency(wave["values"], wave["dt_s"], lvl)
        need_coarse = hit_hi or hit_lo or freq is None

        if need_coarse:
            reason = ("上下轨触顶" if (hit_hi and hit_lo)
                      else "单侧触顶" if (hit_hi or hit_lo) else "数不出 2 个周期")
            print(f"  {label}第 {attempt} 轮：{reason} → 粗调")
            tune_from_record(inst, ch, wave, lvl, args)
            fine_done = False
            continue

        # 跨轮频率一致性：采样率过低时会出现混叠（例如真实 1 kHz 被测成 667 Hz），
        # 频率跳变超过 20% 说明上一轮视图不可信，按"未收敛"继续调。
        if freq and prev_freq and abs(freq - prev_freq) / max(freq, prev_freq) > 0.2:
            print(f"  {label}第 {attempt} 轮：频率 {prev_freq:.6g} → {freq:.6g} Hz"
                  f"（变化 {abs(freq - prev_freq) / max(freq, prev_freq) * 100:.0f}%，疑混叠）"
                  f"→ 视为未收敛，继续调")
            prev_freq = freq
            tune_from_record(inst, ch, wave, lvl, args)
            fine_done = False
            continue
        if freq:
            prev_freq = freq

        if not fine_done:
            print(f"  {label}第 {attempt} 轮：可测（{freq:.6g} Hz，Vpp {wave['vpp_v']:.4g} V）→ 细调")
            tune_from_record(inst, ch, wave, lvl, args)
            fine_done = True
            continue

        sig = waveform_signature(wave, lvl, getattr(args, "trig_slope", "rise"))
        problems = validate_signal_quality(sig, args)
        jm = sig.get("jitter_max")
        spp = sig.get("samples_per_period")
        fill = sig.get("fill_ratio")
        extra = ""
        if spp is not None:
            extra += f"，每周期 {spp:.0f} 点"
        if fill is not None:
            extra += f"，填充 {fill * 100:.0f}%"
        if jm is not None:
            extra += f"，周期内偏差 {jm * 100:.2f}%（{sig.get('periods_n', 0)} 个周期）"

        if problems:
            print(f"  {label}第 {attempt} 轮：未通过有效性判据（{'；'.join(problems)}）→ 继续调")
            tune_from_record(inst, ch, wave, lvl, args)
            fine_done = False
            continue

        print(f"  {label}第 {attempt} 轮：验证通过（{freq:.6g} Hz，Vpp {wave['vpp_v']:.4g} V，"
              f"{wave['scale_v_per_div']:.4g} V/div{extra}）")
        return wave

    print(f"  {label}达到最大轮数 {max_rounds}，按当前记录继续")
    return wave


def capture_continuous(inst: Scpi, settle: float, verbose: bool = True) -> None:
    """连续运行：跑一段时间后冻结，供读取。

    连续模式不参与触发判定：先把触发方式设为 AUTO（无有效触发也按内部时基强制刷新），
    否则仪器若停在 NORMal 且电平不可达，RUNContinous 也不会刷新记录，读到的还是旧数据。
    """
    prepare_acquisition_depth(inst)
    inst.write("TRIGger:A:MODE AUTO")
    inst.write("RUNContinous")
    if verbose:
        try:
            mode = inst.query("TRIGger:A:MODE?", DEFAULT_TIMEOUT).strip()
        except Exception:
            mode = "?"
        print(f"连续运行 {max(0.2, settle):g}s（触发方式 {mode}；稳定性检查全程 AUTO）")
    time.sleep(max(0.2, settle))
    inst.write("STOP")
    time.sleep(COOL_AFTER_STOP)


def observe_continuous(inst: Scpi, ch: int, seconds: float, interval: float) -> tuple[list[dict], dict | None]:
    """用重复读取的数值记录覆盖较长时间；不读取或分析任何图片。"""
    if seconds <= 0:
        return [], None
    started = time.monotonic()
    rows: list[dict] = []
    last_wave = None
    while True:
        remaining = seconds - (time.monotonic() - started)
        if rows and remaining <= 0:
            break
        capture_continuous(inst, max(0.2, min(interval, max(remaining, 0.2))), verbose=False)
        wave = read_channel(inst, ch)
        if wave is not None:
            level = (wave["min_v"] + wave["max_v"]) / 2.0
            rows.append({
                "elapsed_s": time.monotonic() - started,
                "mean_v": wave["mean_v"],
                "min_v": wave["min_v"],
                "max_v": wave["max_v"],
                "vpp_v": wave["vpp_v"],
                "rms_v": wave["rms_v"],
                "frequency_hz": estimate_frequency(wave["values"], wave["dt_s"], level),
            })
            last_wave = wave
    return rows, last_wave


def observation_events(rows: list[dict]) -> list[dict]:
    """相邻数值快照中均值、幅度或频率的明显变化。"""
    events = []
    for before, after in zip(rows, rows[1:]):
        ref = max(before["vpp_v"], after["vpp_v"], 0.01)
        changed = []
        if abs(after["mean_v"] - before["mean_v"]) > max(0.02, 0.05 * ref):
            changed.append("mean_v")
        if abs(after["vpp_v"] - before["vpp_v"]) > max(0.02, 0.05 * ref):
            changed.append("vpp_v")
        f0, f1 = before["frequency_hz"], after["frequency_hz"]
        if f0 and f1 and abs(f1 - f0) / max(f0, f1) > 0.03:
            changed.append("frequency_hz")
        if changed:
            events.append({"between_s": [before["elapsed_s"], after["elapsed_s"]],
                           "changed": changed})
    return events


def capture_trigger(inst: Scpi, args, verbose: bool = True) -> dict:
    """触发模式：配置边沿触发 → RUNSingle → 等采集完成。"""
    slope = "POS" if args.trig_slope.lower().startswith("r") else "NEG"
    tmode = "AUTO" if args.trig_mode.lower().startswith("a") else "NORMal"
    # 触发模式先武装，不进行 AUTO 预采集；默认沿用面板电平。
    level = args.trig_level
    if level is None:
        level = read_panel_level(inst)
    if level is None:
        level = DEFAULT_TRIG_LEVEL_V

    inst.write("TRIGger:A:TYPe EDGE")
    inst.write(f"TRIGger:A:SOURce {args.trig_source}")
    inst.write(f"TRIGger:A:EDGE:SLOPe {slope}")
    inst.write(f"TRIGger:A:MODE {tmode}")
    if level is not None:
        inst.write(f"TRIGger:A:LEVel {level:g}")
        if verbose:
            print(f"触发电平: {level:.4f} V（命令行或面板电平；无 AUTO 预采集）")
    else:
        raise ScpiError("无法从波形估算触发电平，请指定 --trig-level")
    time.sleep(0.3)

    prepare_acquisition_depth(inst)
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
        inst.write("STOP")
        raise ScpiError(f"等待触发超时（{args.trig_wait:g}s，状态 {state}），未导出旧记录")
    if state != "COMP":
        raise ScpiError(f"单次采集未完成（状态 {state}），未导出旧记录")
    wave = read_channel(inst, int("".join(c for c in str(args.trig_source) if c.isdigit()) or "1"))
    actual_level = info["trigger_level_v"]
    if wave is None or actual_level is None or not crossing_ok(
            wave["values"], actual_level, info["trigger_slope"]):
        raise ScpiError(f"触发状态 {state}，但记录未跨越 {actual_level} V，未导出")
    if verbose:
        print(f"触发完成: 状态 {state}，电平 {actual_level} V，"
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
        capture_continuous(inst, args.settle, verbose=False)
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

    long_span = times[-1] - times[0] >= 1.0
    plot_times = times if long_span else [t * 1e3 for t in times]
    fig, ax = plt.subplots(figsize=(12, 6.6), dpi=110)
    for i, col in enumerate(cols):
        ax.plot(plot_times, col, lw=0.9, color=CH_COLORS[i % len(CH_COLORS)], label=header[i + 1])
    ax.set_xlabel("时间 (s)" if long_span else "时间 (ms)")
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
    if not getattr(args, "already_captured", False) and args.stable_periods < 10:
        raise ScpiError("记录前至少保持 10 个周期")
    inst.drain(0.05)                    # 清掉可能滞留的响应，避免查询错位
    out_dir = ensure_dir(args.out)
    channels = [int(c) for c in args.channels.split(",") if c.strip()] if args.channels else [1, 2, 3, 4]

    # 运行模式必须对用户明示：连续与触发的采集语义不同，选错会拿到不可比的数据
    explicit = getattr(args, "mode", None)
    if explicit is None:
        args.mode = "continuous"
        print("运行模式：连续运行（未指定 --mode，取默认值）")
        print("          如需按触发沿对齐的单次采集，请加 --mode trigger")
    else:
        label = ("触发单次（等触发后按边沿对齐采集）" if args.mode == "trigger"
                 else "连续运行（自由采集，不依赖触发）")
        print(f"运行模式：{label}  [--mode {args.mode}]")

    meta = inst_meta(inst)
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    snap = snapshot_acq(inst, channels)
    original_fmt = inst.query("FORMat:DATA?", DEFAULT_TIMEOUT).strip()
    trig_info: dict | None = None
    n_periods = 0
    used_tb = float(snap.get("timebase") or 0)
    used_srate = meta["sample_rate_hz"]
    preserve_display = False
    observation: list[dict] = []
    events: list[dict] = []

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
            if getattr(args, "already_captured", False):
                trig_info = getattr(args, "trig_info", None) or {"mode": "trigger"}
                print("使用已触发采集的画面直接导出（不再重新采集）")
            else:
                trig_info = capture_trigger(inst, args)
                trigger_ch = int("".join(c for c in str(args.trig_source) if c.isdigit()) or "1")
                probe = read_channel(inst, trigger_ch)
                if probe is None:
                    raise ScpiError(f"CH{trigger_ch} 没有可用波形，未导出")
                level = trig_info.get("trigger_level_v")

                def _capture_trig() -> dict | None:
                    capture_trigger(inst, args, verbose=False)   # 每轮都重新武装单次触发
                    try:
                        return read_channel(inst, trigger_ch)
                    except Exception:                # noqa: BLE001
                        return None

                tuned = converge_display(inst, trigger_ch, args, level, _capture_trig,
                                         label="触发模式 ")
                if tuned is not None:
                    probe = tuned
                    if level is None:
                        level = (probe["min_v"] + probe["max_v"]) / 2.0
                if level is None:
                    level = (probe["min_v"] + probe["max_v"]) / 2.0
                if stabilize_waveform(inst, trigger_ch, level, args.trig_slope, args) is None:
                    raise ScpiError("触发波形未稳定保持指定周期数，未写出 CSV/PNG")
        else:
            capture_continuous(inst, args.settle)
            # 读数据 → 自动调到稳定 → 保持 N 个周期 → 才允许写 CSV/PNG
            probe = None
            active_ch = None
            for ch in channels:
                try:
                    probe = read_channel(inst, ch)
                except Exception:             # noqa: BLE001
                    continue
                if probe is not None:
                    active_ch = ch
                    break
            if probe is None or active_ch is None:
                # 记录可能为空（仪器停在 STOP / 时基太慢进入滚动采集）：
                # 不直接报错，交给收敛流程靠盲调时基救回来。
                for ch in channels:
                    try:
                        if inst.query(f"CHANnel{ch}:STATe?", DEFAULT_TIMEOUT).strip() in ("1", "ON"):
                            active_ch = ch
                            break
                    except Exception:         # noqa: BLE001
                        continue
                if active_ch is None:
                    active_ch = channels[0]
                print(f"  当前没有可用记录（仪器可能停在 STOP），进入自动调整：CH{active_ch}")
            if args.trig_level is not None:
                level = args.trig_level
            elif probe is not None:
                level = (probe["min_v"] + probe["max_v"]) / 2.0
            else:
                level = None

            def _capture_cont() -> dict | None:
                capture_continuous(inst, max(0.4, args.settle), verbose=False)
                try:
                    return read_channel(inst, active_ch)
                except Exception:             # noqa: BLE001
                    return None

            tuned = converge_display(inst, active_ch, args, level, _capture_cont,
                                     label="连续模式 ")
            if tuned is not None:
                probe = tuned
                level = ((probe["min_v"] + probe["max_v"]) / 2.0
                         if args.trig_level is None else args.trig_level)
            observation, latest = observe_continuous(
                inst, active_ch, args.observe, args.observe_interval)
            events = observation_events(observation)
            if observation:
                print(f"  数值观察 {observation[-1]['elapsed_s']:.2f}s："
                      f"{len(observation)} 条记录，{len(events)} 处变化")
            if latest is not None:
                probe = latest
                level = (probe["min_v"] + probe["max_v"]) / 2.0
                tune_from_record(inst, active_ch, probe, level, args)
            if stabilize_waveform(inst, active_ch, level, args.trig_slope,
                                  args, acquisition_mode="continuous") is None:
                raise ScpiError("波形未稳定保持指定周期数，未写出 CSV/PNG")

        waves = []
        for ch in channels:
            wave = read_channel(inst, ch)
            if wave is not None:
                waves.append(wave)

        # 记录“实际采集时”的时基与采样率（finally 里才会还原）
        used_tb = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
        used_srate = float(inst.query("ACQuire:SRATe?", DEFAULT_TIMEOUT))
        preserve_display = args.mode == "continuous"
    finally:
        format_error = None
        if not original_fmt.upper().startswith("ASC"):
            try:
                inst.write(f"FORMat:DATA {original_fmt.split(',')[0]}")
            except Exception as exc:
                format_error = exc
        restore_acq(inst, snap, preserve_continuous_display=preserve_display)
        if format_error is not None:
            raise ScpiError(f"还原波形数据格式失败: {format_error}")

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
    trend_csv_path = os.path.join(out_dir, base_name + "_trend.csv") if observation else None
    trend_png_path = os.path.join(out_dir, base_name + "_trend.png") if len(observation) >= 2 else None

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for i in range(n):
            writer.writerow([f"{times[i]:.9e}"] + [f"{c[i]:.6e}" for c in cols])

    if trend_csv_path:
        with open(trend_csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(observation[0]))
            writer.writeheader()
            writer.writerows(observation)
    if trend_png_path:
        try:
            save_plot_png(trend_png_path, [r["elapsed_s"] for r in observation],
                          [[r["mean_v"] for r in observation]],
                          ["Time (s)", "Mean (V)"], "连续观察：数值均值趋势",
                          [f"{observation[-1]['elapsed_s']:.2f} s / {len(observation)} 条记录"])
        except Exception as exc:
            print(f"警告: 数值趋势图生成失败（{exc}），趋势 CSV 已保存", file=sys.stderr)
            trend_png_path = None

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
        "run": trig_info or {"mode": "continuous", "settle_s": args.settle,
                             "observe_s": args.observe,
                             "observe_interval_s": args.observe_interval},
        "observation": {"samples": observation, "events": events,
                        "trend_csv": os.path.basename(trend_csv_path) if trend_csv_path else None,
                        "trend_png": os.path.basename(trend_png_path) if trend_png_path else None},
        "acquisition": {
            "memory_points": meta["acquire_points"],
            "sample_rate_hz": used_srate,
            "timebase_scale_s_per_div": used_tb,
            "timebase_position_s": meta["timebase_position_s"],
            "settings_before_capture": snap,
            "display_settings_retained": preserve_display,
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
    if trend_csv_path:
        print(f"观察表: {trend_csv_path}")
    if trend_png_path:
        print(f"趋势图: {trend_png_path}")
    print(f"点数  : {len(times)}   采样间隔 {dt:.6g} s（{1 / dt / 1e3:.3f} kSa/s）")
    print(f"时间窗: {times[0]:.6e} s ~ {times[-1]:.6e} s")
    for w in waves:
        print(f"CH{w['channel']}: min {w['min_v']:+.4f} V  max {w['max_v']:+.4f} V  "
              f"Vpp {w['vpp_v']:.4f} V  RMS {w['rms_v']:.4f} V  mean {w['mean_v']:+.4f} V")
    if plot_err is not None:
        raise ScpiError(f"CSV 已保存，但波形图生成失败: {plot_err}")
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


# --------------------------------------------------------------------------- #
# 常驻等待（watch）：探头还没接时用。先按 NORMal + RUN 预武装触发，
# 用"不停采集"的测量值感知信号是否到达，越过电平后冻结读数、抓拍截图 + CSV，
# 再自动量程并按频率选时基后重新武装。探头已接好时直接用 csv/shot 抓一次。
# --------------------------------------------------------------------------- #
MEAS_SLOTS = ((1, "RMS"), (2, "FREQuency"), (3, "AMPLitude"), (4, "MEAN"))
MEAS_INVALID = 1e36            # R&S 用 9.91E+37 表示"无有效结果"


def meas_setup(inst: Scpi, ch: int) -> None:
    """打开 4 个测量位（RMS / 频率 / 幅度 / 均值），源设为目标通道。

    实测：RTB2004 只有 4 个测量位（5/6 报 -114 Header suffix out of range）；
    `MEASurement<n>:MAIN <类型>` 是**单参数**写法（写成 `CH1,RMS` 会报 -104）；
    测量框画在波形区下方的状态条里，不遮挡波形，可以一直开着。
    """
    for n, typ in MEAS_SLOTS:
        inst.write(f"MEASurement{n}:SOURce CH{ch}")
        inst.write(f"MEASurement{n}:MAIN {typ}")
    time.sleep(0.25)


def meas_clear(inst: Scpi) -> None:
    for n, _ in MEAS_SLOTS:
        try:
            inst.write(f"MEASurement{n}:MAIN NONE")
        except Exception:
            pass


def meas_read(inst: Scpi) -> dict:
    """读 4 个测量位当前值。全程只查询，不改变采集状态（不打断 RUN）。

    实测：RUN 状态下 `MEASurement<n>:RESult?` 正常返回；返回 9.91E+37 表示无有效结果
    （例如没有周期信号时的频率/周期测量），统一折算成 None。
    """
    out: dict[str, float | None] = {}
    for n, typ in MEAS_SLOTS:
        value: float | None = None
        try:
            raw = inst.query(f"MEASurement{n}:RESult?", 6.0)
            value = float(raw)
        except Exception:
            value = None
            inst.drain(0.08)           # 迟到的响应必须清掉，否则后续查询错位
        if value is None or abs(value) >= MEAS_INVALID:
            value = None
        out[typ] = value
    return out


def fmt_meas(value: float | None, unit: str, digits: int = 4) -> str:
    if value is None:
        return "--"
    return f"{value:.{digits}g}{unit}"


def wait_autoset_done(inst: Scpi, timeout: float = 12.0) -> None:
    """等 AUToscale 落定：档位/时基连续两次读回一致即认为完成。"""
    deadline = time.time() + timeout
    prev = None
    time.sleep(1.0)
    while time.time() < deadline:
        try:
            cur = (inst.query("CHANnel1:SCALe?", DEFAULT_TIMEOUT),
                   inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
        except Exception:
            return
        if prev is not None and cur == prev:
            return
        prev = cur
        time.sleep(0.35)


def estimate_frequency(values: list[float], dt: float, level: float) -> float | None:
    """从记录里数上升沿估算频率，信号断续时也能用（不依赖仪器测量框）。"""
    if dt <= 0:
        return None
    edges = [i * dt for i in range(1, len(values))
             if values[i - 1] < level <= values[i]]
    if len(edges) < 2:
        return None
    span = edges[-1] - edges[0]
    if span <= 0:
        return None
    return (len(edges) - 1) / span


def seek_timebase_for_period(inst: Scpi, ch: int, args, tries: int = 8) -> float | None:
    """记录里看不到 2 个周期时，按 10 倍步进放宽时基，直到能估出频率。

    触发场景：时基过快（如 1 ns/div，窗口只有十几 ns）或过慢（记录 0 点，进入滚动采集）时，
    直接读记录既估不出频率也判不了稳定性。此时必须先把视图放宽到"至少 2 个周期"，
    测到频率后再由调用方精调时基。全程只用数值判断。
    """
    settle = max(0.4, min(1.5, float(getattr(args, "settle", 1.2) or 1.2)))

    # 先用测量位取频率（RUN 状态下可读，不打断采集），拿到就不必逐个时基试
    fast = measure_frequency(inst, ch)
    if fast:
        print(f"    测量框读到 {fast:.6g} Hz（未打断采集），直接算时基")
        return fast

    try:
        current = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
    except Exception:
        return None

    # 双向搜索：时基太快会"看不到 1 个周期"，太慢会"记录为空/采样率过低"，
    # 所以同时准备"变慢 ×10"和"变快 ÷10"两类候选，并按**离当前值的倍数由近到远**排序
    # （倍数取 ≥1 的那个），这样通常 1~3 次就能命中，而不是按 ×10/÷10 交替乱序试。
    # 候选顺序：① 常用时基优先（绝大多数信号落在 1 ms~100 ms，从极端起点能一步到位）
    # ② 再按"离当前值的倍数"双向铺开，覆盖快/慢两侧
    preferred = (1e-3, 1e-2, 1e-4, 1e-1, 1e-5, 1.0, 1e-6, 10.0, 1e-7, 100.0,
                 1e-8, 1e-9, 1e-10, 1e-11, 1e3, 2500.0)
    exp: list[float] = []
    for k in range(1, 9):
        exp.append(current / (10 ** k))             # 变快
        exp.append(current * (10 ** k))             # 变慢
    # 对数距离排序：|log10(候选/当前)| 越小越先试，快慢两个方向对称看待
    exp = sorted((c for c in exp if MIN_TIMEBASE <= c <= MAX_TIMEBASE),
                 key=lambda c: abs(math.log10(c / current)))
    raw: list[float] = [c for c in preferred if MIN_TIMEBASE <= c <= MAX_TIMEBASE] + exp
    seen: set[float] = set()
    candidates: list[float] = []
    for c in raw:
        key = round(c, 18)
        if key in seen or not (MIN_TIMEBASE <= c <= MAX_TIMEBASE):
            continue
        if abs(c - current) / max(current, c) < 1e-9:   # 跳过与当前相同的值
            continue
        seen.add(key)
        candidates.append(c)
    candidates = candidates[:max(tries, 10)]
    if candidates:
        print("    候选时基（由近到远）：" +
              "、".join(f"{c:.4g}" for c in candidates))

    for nxt in candidates:
        inst.write(f"TIMebase:SCALe {nxt:g}")
        inst.write("TIMebase:POSition 0")
        capture_continuous(inst, settle, verbose=False)
        try:
            wave = read_channel(inst, ch)
        except Exception:
            wave = None
        if not wave or not wave.get("points"):
            print(f"    时基 {nxt:g} s/div：记录为空，换下一个")
            continue
        level = (percentile(wave["values"], 0.05) + percentile(wave["values"], 0.95)) / 2.0
        freq = estimate_frequency(wave["values"], wave["dt_s"], level)
        if freq:
            print(f"    时基 {nxt:g} s/div：测到 {freq:.6g} Hz（{wave['points']} 点）")
            return freq
        print(f"    时基 {nxt:g} s/div：数不出 2 个周期，换下一个")
    return None


def detect_clipping(wave: dict) -> tuple[bool, bool]:
    """判断记录是否顶到上/下轨。

    触顶时记录里的 max/min 只是"可见范围"，Vpp 不是真实幅值，不能再用 vpp/6 估档位。
    返回 (顶到上轨, 顶到下轨)。
    """
    half = VERT_DIVISIONS / 2.0 * wave["scale_v_per_div"]
    hi_rail = wave["offset_v"] + half
    lo_rail = wave["offset_v"] - half
    tol = 0.02 * (2.0 * half) if half else 0.0
    return (wave["max_v"] >= hi_rail - tol, wave["min_v"] <= lo_rail + tol)


def seek_vertical_scale(inst: Scpi, ch: int, wave: dict) -> float:
    """按"是否触顶"决定垂直档位的调整幅度（与时基寻找同一套思路）。

    - 上下轨都顶到（硬削波）：真实幅值未知 → 按 **10 倍**粗调，一步到位
    - 只顶到一侧（软削波）：按 **4 倍**粗调，避免过冲太多
    - 未触顶：按 vpp/6 精调（此时 vpp 可信）
    """
    old_scale = wave["scale_v_per_div"]
    hit_hi, hit_lo = detect_clipping(wave)
    if hit_hi and hit_lo:
        return pick_scale_value(old_scale * 10.0)
    if hit_hi or hit_lo:
        return pick_scale_value(old_scale * 4.0)

    vpp = wave["vpp_v"]
    full = VERT_DIVISIONS * old_scale
    half = full / 2.0
    # 几乎平线：可能信号在屏幕外（均值贴边 → 往大档位找），也可能信号本身很小（往小档位找）
    if vpp < 0.02 * full:
        near_rail = abs(wave["mean_v"] - wave["offset_v"]) >= 0.6 * half
        return (pick_scale_value(old_scale * 10.0) if near_rail
                else pick_scale_value(old_scale / 10.0))

    return pick_scale_value(max(vpp / 6.0, SCALE_LADDER[0]))


def tune_from_record(inst: Scpi, ch: int, wave: dict, level: float, args) -> bool:
    """用已读取的记录调量程和时基；下一次采集再验证调整效果。

    返回值 `coarse=True` 表示本次是"触顶粗调"（档位一次跳很多倍），调用方必须再采集一轮
    用新记录精调并复核，不能拿粗调后的记录直接判稳定导出。
    """
    coarse = False
    if not getattr(args, "no_autoscale", False):
        old_scale = wave["scale_v_per_div"]
        hit_hi, hit_lo = detect_clipping(wave)
        scale = seek_vertical_scale(inst, ch, wave)
        coarse = hit_hi or hit_lo
        inst.write(f"CHANnel{ch}:SCALe {scale:.6g}")
        inst.write(f"CHANnel{ch}:OFFSet {wave['mean_v']:.6g}")
        if hit_hi and hit_lo:
            why = "上下轨都触顶→10 倍粗调"
        elif hit_hi or hit_lo:
            why = "单侧触顶→4 倍粗调"
        else:
            why = "未触顶→按 vpp/6 精调"
        print(f"  按记录调量程：CH{ch} {scale:.6g} V/div，偏移 {wave['mean_v']:.6g} V"
              f"（{why}，旧 {old_scale:.6g} V/div）")

    # 触顶时记录是失真的，频率/周期都不可信：先只把幅值调回去，
    # 等下一轮用未削顶的新记录再判断时基，避免拿坏数据去试时基（白耗 RUN/STOP）。
    if coarse:
        return coarse

    if any(getattr(args, key, None) for key in ("timebase", "preset", "periods")):
        return coarse
    freq = estimate_frequency(wave["values"], wave["dt_s"], level)
    if freq is None:
        print("  当前记录不足两个周期，按 10 倍步进放宽时基寻找波形…")
        freq = seek_timebase_for_period(inst, ch, args)
        coarse = True
    if freq is None:
        print("  仍未找到可测周期的视图，保留原时基")
        return coarse
    screen_periods = max(int(getattr(args, "screen_periods", 10) or 0),
                         int(getattr(args, "stable_periods", 10) or 0) + 2)
    tb = pick_timebase(1.0 / freq, screen_periods)
    inst.write(f"TIMebase:SCALe {tb:.6g}")
    inst.write("TIMebase:POSition 0")
    print(f"  按记录调时基：{tb * 1e6:.6g} µs/div，约覆盖 {screen_periods} 周期")
    return coarse


def autoscale_by_record(inst: Scpi, ch: int, args) -> dict | None:
    """纯数据定量程/时基：先用测量值（每次查询 ~0.3 s）定档，再读一条记录复核。

    为什么不能只靠读记录迭代：仪器当前若是 50 V/div + 5 s/div，一条记录能到
    十几万点，`CHANnel1:DATA?` 会直接 0 字节超时——量程/时基本身就把读取堵死了。
    所以先用 AMPL/MEAN/FREQ 三个测量值把仪器调到“读得动”的状态：
      * 量程 = Vpp / 6（占满 6 格，8 位 ADC 不会只剩几个码值）；
      * 偏移 = 均值（波形居中，避免贴边削顶）；
      * 时基 = 4 个周期 / 12 格（点数回到屏幕采样，~几千点）。
    之后读一条记录，检查是否仍贴边；贴边再放大一档重读一次。
    """
    inst.write("TRIGger:A:MODE AUTO")
    inst.write("RUN")
    time.sleep(0.35)
    ampl = meas_read_one(inst, 3)
    mean = meas_read_one(inst, 4)
    freq = meas_read_one(inst, 2)

    if ampl is not None and ampl > 0:
        scale = pick_scale_value(max(ampl / 6.0, 1e-4))
        inst.write(f"CHANnel{ch}:SCALe {scale:.6g}")
        if mean is not None:
            inst.write(f"CHANnel{ch}:OFFSet {mean:.6g}")
        print(f"  数据定量程：测得 Vpp {ampl:.4g} V → {scale:.4g} V/div，"
              f"offset {mean if mean is not None else 0:.4g} V")
    if freq and freq > 1e-3:
        tb = pick_timebase(1.0 / freq, args.screen_periods)
        inst.write(f"TIMebase:SCALe {tb:.6g}")
        inst.write("TIMebase:POSition 0")
        print(f"  数据定时基：测得 {freq:.6g} Hz → {tb * 1e6:.4g} µs/div"
              f"（约 {args.screen_periods} 个周期）")
    time.sleep(0.4)
    inst.write("STOP")
    time.sleep(COOL_AFTER_STOP)

    wave = None
    for _ in range(2):
        try:
            wave = read_channel(inst, ch)
        except Exception as exc:             # noqa: BLE001
            print(f"  警告: 定量程后读记录失败（{exc}）")
            return None
        if wave is None:
            return None
        scale = wave["scale_v_per_div"]
        half = VERT_DIVISIONS / 2.0 * scale
        clipped = (wave["max_v"] >= wave["offset_v"] + half - 0.05 * scale
                   or wave["min_v"] <= wave["offset_v"] - half + 0.05 * scale)
        if not clipped:
            return wave
        bigger = pick_scale_value(scale * 2.0)
        print(f"  记录仍贴边 → 量程 {scale:.4g} → {bigger:.4g} V/div 重读")
        inst.write(f"CHANnel{ch}:SCALe {bigger:.6g}")
        inst.write("TRIGger:A:MODE AUTO")
        inst.write("RUN")
        time.sleep(0.4)
        inst.write("STOP")
        time.sleep(COOL_AFTER_STOP)
    return wave


def fit_view(inst: Scpi, args, m: dict | None, wave: dict | None = None) -> dict:
    """调好显示：有触发记录就按记录定量程/偏移，时基按记录估出的频率设。

    不依赖 AUToscale 的原因：实测信号在 AUToscale 那一瞬间断掉/断续时，
    它会按噪声算出 50 mV/div 这种荒谬档位，波形直接削顶。
    触发到的那条记录里 Vpp/均值/频率都是确定的，按它调最稳。
    """
    info: dict = {"autoscale": False, "timebase_s": None}
    meas_clear(inst)                      # AUToscale 会清掉测量位，先关再开
    if wave is not None:
        vpp = wave["vpp_v"]
        mean = wave["mean_v"]
        scale = pick_scale_value(max(vpp / 6.0, 1e-3))
        inst.write(f"CHANnel{args.channel}:SCALe {scale:.6g}")
        inst.write(f"CHANnel{args.channel}:OFFSet {mean:.6g}")
        info["scale_v_per_div"] = scale
        info["offset_v"] = mean
        print(f"  按触发记录定量程：Vpp {vpp:.4g} V → {scale:.4g} V/div，"
              f"offset {mean:.4g} V")
        time.sleep(0.2)
    elif not args.no_autoscale:
        t0 = time.time()
        inst.write("AUToscale")
        wait_autoset_done(inst)
        info["autoscale"] = True
        info["autoscale_s"] = round(time.time() - t0, 2)
        print(f"  自动量程完成（{info['autoscale_s']}s）")
        time.sleep(0.3)

    if m is None:                          # 调用方没有测量值：现在读一次
        meas_setup(inst, args.channel)
        time.sleep(0.2)
        m = meas_read(inst)

    freq = m.get("FREQuency")
    if args.screen_periods and freq and freq > 1e-3:
        tb = pick_timebase(1.0 / freq, args.screen_periods)
        inst.write(f"TIMebase:SCALe {tb:.6g}")
        inst.write("TIMebase:POSition 0")
        time.sleep(0.3)
        actual = float(inst.query("TIMebase:SCALe?", DEFAULT_TIMEOUT))
        info["timebase_s"] = actual
        print(f"  按实测 {freq:.6g} Hz 设时基 {actual * 1e6:.4g} µs/div"
              f"（窗口 {actual * DIVISIONS * 1e3:.4g} ms ≈ {args.screen_periods} 个周期）")
    elif args.timebase:
        actual = apply_timebase(inst, parse_seconds(args.timebase))
        info["timebase_s"] = actual

    meas_setup(inst, args.channel)
    scale = inst.query_float(f"CHANnel{args.channel}:SCALe?")
    offset = inst.query_float(f"CHANnel{args.channel}:OFFSet?")
    info["scale_v_per_div"] = scale
    info["offset_v"] = offset
    print(f"  CH{args.channel}: {scale:.6g} V/div, offset {offset:.6g} V")
    return info


def pick_scale_value(volts_per_div: float) -> float:
    """把需要的 V/div 对齐到 RTB2000 的 1-2-5 档位（不小于给定值）。"""
    for s in SCALE_LADDER:
        if s >= volts_per_div - 1e-12:
            return s
    return SCALE_LADDER[-1]


def ensure_level_visible(inst: Scpi, ch: int, level: float) -> None:
    """确保触发电平落在屏幕纵向范围内（否则信号越过电平也看不到波形）。"""
    scale = inst.query_float(f"CHANnel{ch}:SCALe?")
    offset = inst.query_float(f"CHANnel{ch}:OFFSet?")
    if abs(level - offset) <= 4.0 * scale:          # 屏幕中心 ±5 格，留 1 格余量
        return
    need = pick_scale_value(abs(level - offset) / 4.0)
    if need <= scale:
        return
    inst.write(f"CHANnel{ch}:SCALe {need:.6g}")
    time.sleep(0.2)
    print(f"  触发电平 {level:.4g} V 超出当前屏幕 → CH{ch} 改到 {need:.4g} V/div")


def read_panel_level(inst: Scpi) -> float | None:
    """读示波器当前面板上的触发电平（用户在仪器上设的值）。"""
    try:
        value = inst.query_float("TRIGger:A:LEVel?")
    except Exception:
        return None
    if value is None or abs(value) > 200.0:      # 读回异常值当作没读到
        return None
    return value


def resolve_trigger_level(inst: Scpi, args) -> tuple[float, str]:
    """触发电平：显式 `--trig-level` 优先，否则**跟随示波器面板上的当前电平**。

    面板电平就是用户亲手设的那个值（默认 1 V，用户也可能在仪器上改成 10 V），
    软件不覆盖它；只有面板电平读不回来时才退回 1 V。
    """
    if args.trig_level is not None:
        return float(args.trig_level), "命令行指定"
    panel = read_panel_level(inst)
    if panel is not None:
        return panel, "跟随示波器面板"
    return DEFAULT_TRIG_LEVEL_V, "默认值（面板读回失败）"


def arm_trigger(inst: Scpi, ch: int, slope_word: str, level: float,
                write_level: bool = True) -> dict:
    """配置边沿触发（EDGE / CH / 沿 / NORMal / 电平）并 RUN 武装。

    实测要点：RTB2004 的 `RUNSingle` 在电平不可达时会**立刻** STOP（不是保持等待），
    所以"预先武装等信号"只能用 MODE NORMal + RUN —— 不触发就不刷新屏幕，
    信号一到（越过电平）立即触发并刷新记录。
    `write_level=False` 时不改写电平（跟随用户在面板上设的值）。
    """
    slope = "POS" if slope_word.lower().startswith("r") else "NEG"
    inst.write("TRIGger:A:TYPe EDGE")
    inst.write(f"TRIGger:A:SOURce CH{ch}")
    inst.write(f"TRIGger:A:EDGE:SLOPe {slope}")
    inst.write("TRIGger:A:MODE NORMal")
    if write_level:
        inst.write(f"TRIGger:A:LEVel {level:.6g}")
    time.sleep(0.2)
    actual = level
    try:
        actual = inst.query_float("TRIGger:A:LEVel?")
    except Exception:
        pass
    inst.write("RUN")
    return {
        "slope": "rise" if slope == "POS" else "fall",
        "slope_cmd": slope,
        "level_v": actual,
        "state": "RUN",
        "armed": True,
    }


def level_reachable(m: dict, level: float, min_vpp: float) -> bool:
    """用测量值判断"当前记录里已经有能跨越触发电平的信号"。"""
    ampl = m.get("AMPLitude")
    if ampl is None or ampl < min_vpp:
        return False
    mean = m.get("MEAN")
    if mean is None:
        return True
    return (mean - ampl / 2.0) <= level <= (mean + ampl / 2.0)


def crossing_ok(values: list[float], level: float, slope_word: str) -> bool:
    """复核记录里真的越过了触发电平（RUNSingle 在未触发时也可能立刻停，必须验证）。"""
    if len(values) < 16:
        return False
    lo, hi = min(values), max(values)
    if not (lo <= level <= hi):
        return False
    span = max(hi - lo, 1e-12)
    band = max(span * 0.02, abs(level) * 1e-3, 1e-9)
    rise = slope_word.lower().startswith("r")
    for i in range(1, len(values)):
        prev, cur = values[i - 1], values[i]
        if rise and prev < level <= cur:
            return True
        if (not rise) and prev > level >= cur:
            return True
    # 采样点太稀时退化为"电平附近确有样本"
    return any(abs(v - level) <= band for v in values)


def waveform_signature(wave: dict, level: float, slope: str) -> dict:
    """从记录数值提取稳定指纹：均值、幅度、频率、相位、边沿数、周期内误差。"""
    values = wave["values"]
    dt = wave["dt_s"]
    edges = [i * dt for i in range(1, len(values))
             if values[i - 1] < level <= values[i]]

    # 周期内误差：逐个周期间隔与平均周期的相对偏差。
    # 十来个周期里若明显参差（例如脉宽抖动、间歇丢沿），单看平均值会漏判，
    # 所以这里同时给出平均偏差和最大单周期偏差，供稳定判据使用。
    periods = [edges[i + 1] - edges[i] for i in range(len(edges) - 1)]
    jitter = jitter_max = None
    if len(periods) >= 2:
        mean_period = sum(periods) / len(periods)
        if mean_period > 0:
            devs = [abs(p - mean_period) / mean_period for p in periods]
            jitter = sum(devs) / len(devs)
            jitter_max = max(devs)

    freq = estimate_frequency(values, dt, level)
    # 每周期采样点数 = fs / f（也是判断是否混叠的直接依据：工程实践要求 ≥5，测边沿要 ≥10）
    samples_per_period = ((1.0 / freq) / dt) if (freq and dt) else None
    # 垂直填充率：Vpp 占满几屏（<50% 量化误差偏大，>90% 有削顶风险）
    scale = wave.get("scale_v_per_div") or 0.0
    fill_ratio = (wave["vpp_v"] / (VERT_DIVISIONS * scale)) if scale else None

    return {
        "vpp": wave["vpp_v"],
        "mean": wave["mean_v"],
        "freq": freq,
        "phase": (wave["t_start_s"] + edges[0]) if edges else None,
        "n_edges": len(edges),
        "cross": crossing_ok(values, level, slope),
        "periods_n": len(periods),
        "jitter": jitter,
        "jitter_max": jitter_max,
        "samples_per_period": samples_per_period,
        "fill_ratio": fill_ratio,
    }


def validate_signal_quality(sig: dict, args=None) -> list[str]:
    """单次采集有效性判据（不属于仪器校准，只判"这条记录能不能用来报数"）。

    依据工程实践与采样定理：
      * 每周期采样点数 ≥10（测边沿/脉宽的最低要求，也是混叠的直接判据）
      * 垂直填充率 50%~90%（太低量化误差大，太高有削顶风险）
      * 记录内完整周期数 ≥ (设定的稳定周期数 - 2)
    返回未通过项的文字说明；空列表表示全部通过。
    """
    min_spp = float(getattr(args, "min_spp", 10.0) or 10.0)
    fill_min = float(getattr(args, "fill_min", 0.35) or 0.35)
    fill_max = float(getattr(args, "fill_max", 0.95) or 0.95)
    # 单条记录只需够算频率/抖动（3~5 个周期）；"保持 10 个周期"由持续时间维度保证，
    # 不能拿它要求单条记录，否则短记录会被误判。
    min_periods = max(3, min(5, int(getattr(args, "stable_periods", 10) or 10) - 2))

    problems: list[str] = []
    spp = sig.get("samples_per_period")
    if spp is not None and spp < min_spp:
        problems.append(f"每周期仅 {spp:.1f} 点（<{min_spp:g}，存在混叠风险）")
    fill = sig.get("fill_ratio")
    if fill is not None:
        if fill < fill_min:
            problems.append(f"垂直填充 {fill * 100:.0f}%（<{fill_min * 100:.0f}%，量化误差偏大）")
        elif fill > fill_max:
            problems.append(f"垂直填充 {fill * 100:.0f}%（>{fill_max * 100:.0f}%，有削顶风险）")
    pn = int(sig.get("periods_n") or 0)
    if pn < min_periods:
        problems.append(f"记录内仅 {pn} 个完整周期（<{min_periods}）")
    return problems


def signature_is_stable(a: dict, b: dict | None, tol: float = 0.05,
                       require_phase: bool = True,
                       period_tol: float = 0.02) -> bool:
    """两条记录的幅度、频率、周期内误差及可选触发相位是否一致。

    `period_tol` 是"十个周期内误差"的判据：任何一条记录里，单个周期相对平均
    周期的最大偏差都必须 ≤ period_tol，否则视为还没调稳（值可能是混叠或抖动）。
    """
    if b is None:
        return False
    if not (a["cross"] and b["cross"] and a["freq"] and b["freq"]):
        return False
    if a["n_edges"] < 2 or b["n_edges"] < 2:
        return False
    if abs(a["freq"] - b["freq"]) / max(a["freq"], 1e-9) > 0.03:
        return False
    if abs(a["vpp"] - b["vpp"]) / max(a["vpp"], 1e-12) > tol:
        return False
    if abs(a["mean"] - b["mean"]) > max(0.02, tol * max(a["vpp"], b["vpp"])):
        return False
    for rec in (a, b):
        jm = rec.get("jitter_max")
        if jm is not None and jm > period_tol:
            return False
    if not require_phase:
        return True
    if a["phase"] is None or b["phase"] is None:
        return False
    period = 1.0 / a["freq"]
    delta = abs(a["phase"] - b["phase"]) % period
    return min(delta, period - delta) <= period * 0.1


def stabilize_waveform(inst: Scpi, ch: int, level: float, slope: str, args,
                       tune: bool = False, max_rounds: int = 10,
                       acquisition_mode: str = "trigger") -> dict | None:
    """纯数据闭环：把波形调到稳定，连续稳定保持 N 个周期后才允许导出。

    continuous 模式只用 AUTO 自由采集并比较幅度/频率，不配置触发电平或切 NORMal。
    之后每轮都是一次真实采集，读回记录并比对数值指纹：
      * 每条记录都必须真的越过触发电平，且至少有 2 个沿；
      * 幅度/频率/触发相位连续一致（容差见 signature_is_stable）；
      * 累计采集覆盖时间 ≥ N 个信号周期，真实等待时间也 ≥ N 个周期。
    达不到就返回 None：调用方必须跳过 CSV/PNG，不写任何文件。
    """
    raw_periods = getattr(args, "stable_periods", 10)
    periods = 10 if raw_periods is None else int(raw_periods)
    if periods <= 0:                      # 0 = 显式跳过稳定检查
        try:
            return read_channel(inst, ch)
        except Exception:                 # noqa: BLE001
            return None
    if tune:
        autoscale_by_record(inst, ch, args)

    slope_cmd = "POSitive" if slope.startswith("r") else "NEGative"
    prev: dict | None = None
    streak = 0
    stable_since: float | None = None
    for round_no in range(1, max_rounds + 1):
        instrument_settle = max(0.5, float(getattr(args, "settle", 1.0)))
        wait = instrument_settle if prev is None or not prev.get("freq") else \
            max(instrument_settle, min(15.0, max(2.0 / prev["freq"],
                                             periods / prev["freq"] / max_rounds * 1.5)))
        if acquisition_mode == "continuous":
            inst.write("TRIGger:A:MODE AUTO")
            inst.write("RUNContinous")
        else:
            inst.write("TRIGger:A:TYPe EDGE")
            inst.write(f"TRIGger:A:SOURce CH{ch}")
            inst.write(f"TRIGger:A:EDGE:SLOPe {slope_cmd}")
            inst.write("TRIGger:A:MODE NORMal")
            inst.write(f"TRIGger:A:LEVel {level:.6g}")
            inst.write("RUN")
        time.sleep(wait)
        inst.write("STOP")
        time.sleep(COOL_AFTER_STOP)
        try:
            wave = read_channel(inst, ch)
        except Exception as exc:           # noqa: BLE001
            print(f"  稳定检测第 {round_no} 轮读记录失败（{exc}）")
            try:
                inst.drain(0.1)
            except Exception:
                pass
            prev, streak, stable_since = None, 0, None
            continue
        if wave is None:
            prev, streak, stable_since = None, 0, None
            continue
        current_level = ((wave["min_v"] + wave["max_v"]) / 2.0
                         if acquisition_mode == "continuous" else level)
        sig = waveform_signature(wave, current_level, slope)
        if not (sig["cross"] and sig["freq"] and sig["n_edges"] >= 2):
            prev, streak, stable_since = None, 0, None
            continue
        quality = validate_signal_quality(sig, args)
        if quality:
            if streak == 0:
                print(f"  记录未通过有效性判据：{'；'.join(quality)}")
            prev, streak, stable_since = None, 0, None
            continue
        if signature_is_stable(sig, prev, require_phase=acquisition_mode != "continuous",
                               period_tol=float(getattr(args, "period_tol", 0.02) or 0.02)):
            streak += 1
        else:
            streak, stable_since = 1, time.monotonic()
        prev = sig
        need = periods / sig["freq"]
        held = time.monotonic() - stable_since
        if streak >= 3 and held >= need:
            jm = sig.get("jitter_max")
            extra = (f"，周期内最大偏差 {jm * 100:.2f}%（{sig.get('periods_n', 0)} 个周期）"
                     if jm is not None else "")
            print(f"  波形稳定：连续 {streak} 条记录数值一致，保持 "
                  f"{held * 1e3:.4g} ms ≥ {periods} 个周期{extra} → 允许导出")
            return wave
    print(f"  警告: {max_rounds} 轮内未稳定保持 {periods} 个周期，"
          f"跳过本次记录（不写 CSV/PNG）")
    return None


def export_capture(inst: Scpi, args, ch: int, out_dir: str, prefix: str,
                   level: float, state: str) -> None:
    """数值判断已完成；图片仅在此作为输出，图片失败不阻止数值导出。"""
    if args.capture in ("shot", "both"):
        try:
            cmd_shot(argparse.Namespace(out=out_dir, name=prefix + "_shot"), inst)
        except Exception as exc:
            if args.capture == "shot":
                raise
            print(f"  警告: 附加截图失败（{exc}），继续导出数值 CSV/波形图")
    if args.capture in ("csv", "both"):
        csv_args = argparse.Namespace(
            channels=str(ch), out=out_dir, name=prefix,
            mode="trigger", trig_source=f"CH{ch}",
            trig_slope=args.trig_slope, trig_level=level, trig_mode="normal",
            trig_wait=args.trig_wait, settle=max(0.6, args.settle), periods=0,
            timebase=None, preset=None, ip=args.ip, port=args.port,
            already_captured=True,
            trig_info={"mode": "trigger", "trigger_source": f"CH{ch}",
                       "trigger_slope": "rise" if args.trig_slope.startswith("r") else "fall",
                       "trigger_mode": "normal", "trigger_level_v": level,
                       "final_state": state, "timed_out": False},
        )
        cmd_csv(csv_args, inst)


def meas_read_one(inst: Scpi, slot: int) -> float | None:
    """只查一个测量位（实测单次 ~300 ms，比读 4 个槽快 4 倍）。"""
    try:
        value = float(inst.query(f"MEASurement{slot}:RESult?", 6.0))
    except Exception:
        inst.drain(0.08)
        return None
    if abs(value) >= MEAS_INVALID:
        return None
    return value


def measure_frequency(inst: Scpi, ch: int) -> float | None:
    """用测量位 2 取频率：RUN 状态下即可读，**不需要 STOP**。

    为什么重要：读波形必须 STOP，而"逐级试时基"的做法会让仪器前面板的
    RUN/STOP 反复跳。测量位在 RUN 下就能给出频率，先拿到频率再一次性算时基，
    可以把 STOP 次数压到最少。
    """
    try:
        meas_setup(inst, ch)
        freq = meas_read_one(inst, 2)
    except Exception:                      # noqa: BLE001
        return None
    return freq if (freq and freq > 0) else None


def monitor_for_signal(inst: Scpi, ch: int, level: float, args,
                       started: float) -> tuple[dict, str, float]:
    """NORMal + RUN 武装后轮询测量值，等待真实触发。

    返回 (测量值, 中止原因, 实际在用的电平)；中止原因非空表示没等到信号。
    NORMal 未触发时测量值无效；信号触发并刷新记录后才有有效测量值。
    等待时只查“幅度”一个测量位（实测单次 ~300 ms）：读整条记录在某些状态下会
    拖到几十秒（CHA:DATA? 超时），不能用来当轮询心跳；测量查询则稳定得多。
    等待期间每 3 秒重读一次面板电平：你在仪器上把电平 5 V 改成 1 V 也能跟上。
    """
    inst.write("TRIGger:A:TYPe EDGE")
    inst.write(f"TRIGger:A:SOURce CH{ch}")
    inst.write("TRIGger:A:MODE NORMal")
    inst.write(f"TRIGger:A:LEVel {level:.6g}")
    inst.write(f"TRIGger:A:EDGE:SLOPe {'POS' if args.trig_slope.startswith('r') else 'NEG'}")
    inst.write("RUN")
    time.sleep(0.25)
    t_report = time.time()
    t_level = time.time()
    ampl = None
    mean = None
    while True:
        ampl = meas_read_one(inst, 3)        # 槽 3 = 幅度，一次查询即一轮心跳
        if ampl is not None and ampl >= args.min_vpp:
            mean = meas_read_one(inst, 4)    # 槽 4 = 均值，够幅了再查
            m = {"AMPLitude": ampl, "MEAN": mean, "FREQuency": None}
            if level_reachable(m, level, args.min_vpp):
                m["FREQuency"] = meas_read_one(inst, 2)
                return m, "", level
        if args.duration and time.time() - started >= args.duration:
            return {"AMPLitude": ampl, "MEAN": mean}, "到达 --duration", level
        if args.trig_level is None and time.time() - t_level >= 3.0:
            t_level = time.time()
            panel = read_panel_level(inst)
            if panel is not None and abs(panel - level) > 1e-6:
                level = panel
                print(f"[{datetime.now():%H:%M:%S}] 面板电平改为 "
                      f"{level:.4g} V，按新电平继续等…")
        if time.time() - t_report >= 10.0:
            t_report = time.time()
            print(f"[{datetime.now():%H:%M:%S}] 监视中：Vpp "
                  f"{fmt_meas(ampl, 'V')}，均值 "
                  f"{fmt_meas(mean, 'V')} —— 还够不到 {level:.4g} V，继续等…")
        time.sleep(max(0.05, args.interval / 3.0))


def capture_on_trigger(inst: Scpi, ch: int, level: float, args,
                       basis: str) -> tuple[bool, dict | None, float]:
    """NORMal + 面板电平武装，等触发后 STOP 读回记录并复核边沿。

    返回 (是否命中, 记录, 实际用电平)。命中后顺手把显示调好（AUToscale + 按频率设时基），
    保证随后截的图和导出的 CSV 是同一条“调好之后”的记录。
    """
    info = arm_trigger(inst, ch, args.trig_slope, level,
                       write_level=not basis.startswith("跟随"))
    used = info["level_v"]
    time.sleep(max(0.5, args.settle))        # NORMal 不触发不刷新，给触发一点时间落定
    inst.write("STOP")
    time.sleep(COOL_AFTER_STOP)
    wave = None
    try:
        wave = read_channel(inst, ch)
    except Exception as exc:                 # noqa: BLE001
        print(f"  警告: 读取记录失败（{exc}）")

    def _capture_any() -> dict | None:
        """采集一条记录用于"把显示调对"，不要求越过电平。

        这里必须用 **AUTO + RUNContinous**：NORMal 在没有触发事件时不会刷新记录，
        读回来永远是空的（实测会一直空转）。
        """
        inst.write("TRIGger:A:TYPe EDGE")
        inst.write(f"TRIGger:A:SOURce CH{ch}")
        inst.write(f"TRIGger:A:EDGE:SLOPe {info['slope_cmd']}")
        inst.write("TRIGger:A:MODE AUTO")
        inst.write(f"TRIGger:A:LEVel {used:.6g}")
        inst.write("RUNContinous")
        time.sleep(max(0.4, args.settle))
        inst.write("STOP")
        time.sleep(COOL_AFTER_STOP)
        try:
            return read_channel(inst, ch)
        except Exception:                    # noqa: BLE001
            return None

    crossed = wave is not None and crossing_ok(wave["values"], used, args.trig_slope)
    if not crossed and wave is not None:
        hit_hi, hit_lo = detect_clipping(wave)
        no_freq = estimate_frequency(wave["values"], wave["dt_s"], used) is None
        if hit_hi or hit_lo or no_freq:
            print("      记录不可信（触顶或数不出周期）→ 先按数值把显示调好再判")
            fixed = converge_display(inst, ch, args, used, _capture_any,
                                     label="触发等待 ")
            if fixed is not None:
                wave = fixed
                crossed = crossing_ok(wave["values"], used, args.trig_slope)

    if not crossed:
        print("      记录里没有越过该电平的边沿（信号还没到），回到监视")
        return False, None, used

    # 多步收敛：粗调 → 验证 → 细调 → 验证，与 csv 的两种模式共用同一套逻辑
    slope_cmd = info["slope_cmd"]

    def _capture() -> dict | None:
        inst.write("TRIGger:A:TYPe EDGE")
        inst.write(f"TRIGger:A:SOURce CH{ch}")
        inst.write(f"TRIGger:A:EDGE:SLOPe {slope_cmd}")
        inst.write("TRIGger:A:MODE NORMal")
        inst.write(f"TRIGger:A:LEVel {used:.6g}")
        inst.write("RUN")
        time.sleep(max(0.4, args.settle))
        inst.write("STOP")
        time.sleep(COOL_AFTER_STOP)
        try:
            got = read_channel(inst, ch)
        except Exception:                    # noqa: BLE001
            return None
        if got is None or not crossing_ok(got["values"], used, args.trig_slope):
            return None
        return got

    tuned = converge_display(inst, ch, args, used, _capture, label="触发等待 ")
    if tuned is not None:
        wave = tuned
    scale = inst.query_float(f"CHANnel{ch}:SCALe?")
    tb = inst.query_float("TIMebase:SCALe?")
    print(f"      CH{ch} {scale:.4g} V/div，时基 {tb * 1e6:.4g} µs/div，记录已冻结")
    return True, wave, used


def run_trigger_mode(args, inst: Scpi, ch: int, out_dir: str) -> int:
    """NORMal + RUN 先武装；触发后读记录调量程/时基，稳定 N 周期再导出。"""
    snap = snapshot_acq(inst, (ch,))
    ch_scale = inst.query_float(f"CHANnel{ch}:SCALe?")
    ch_offset = inst.query_float(f"CHANnel{ch}:OFFSet?")
    started = time.time()
    events = 0
    reason = "达到 --max-events 上限"

    level, basis = resolve_trigger_level(inst, args)
    slope_cn = "上升沿" if args.trig_slope.startswith("r") else "下降沿"
    if args.timebase:                  # 探头没接时测不到频率，允许先固定时基
        tb = apply_timebase(inst, parse_seconds(args.timebase))
        print(f"  预置时基 {tb * 1e3:.4g} ms/div")
    meas_setup(inst, ch)
    ensure_level_visible(inst, ch, level)
    print("触发模式：NORMal + RUN 已武装；不会先切到 AUTO 连续采集")
    print(f"触发电平 {level:.4g} V（{basis}），{slope_cn}；"
          f"Vpp 判定门限 {args.min_vpp * 1e3:g} mV")
    print("-" * 96)

    try:
        prepare_acquisition_depth(inst)
        while True:
            if args.duration and time.time() - started >= args.duration:
                reason = "到达 --duration"
                break
            if args.max_events and events >= args.max_events:
                break

            # ---- 第一段：AUTO 自由采集，把信号“看见” ----
            m, stop_reason, level = monitor_for_signal(inst, ch, level, args, started)
            if stop_reason:
                reason = stop_reason
                break

            print(f"[{datetime.now():%H:%M:%S}] 信号已到：Vpp "
                  f"{fmt_meas(m.get('AMPLitude'), 'V')}，均值 "
                  f"{fmt_meas(m.get('MEAN'), 'V')}，频率 "
                  f"{fmt_meas(m.get('FREQuency'), 'Hz')} → 按 {level:.4g} V 触发抓拍")

            # ---- 第二段：NORMal + 面板电平真触发，STOP 读记录复核 ----
            ok, wave, used_level = capture_on_trigger(inst, ch, level, args, basis)
            if not ok:
                continue
            stable = stabilize_waveform(inst, ch, used_level, args.trig_slope, args)
            if stable is None:
                print("-" * 96)
                continue
            events += 1
            prefix = datetime.now().strftime("watch_%H%M%S")
            print(f"      触发命中并稳定：记录 Vpp {stable['vpp_v']:.4g} V，"
                  f"{stable['points']} 点 → CSV + 波形图")
            export_capture(inst, args, ch, out_dir, prefix, used_level, "STOP")
            print(f"      已落盘 {prefix}.csv / {prefix}.png（同目录）")
            print("-" * 96)
    except KeyboardInterrupt:
        reason = "用户中断"
    finally:
        meas_clear(inst)
        if not args.keep_view:
            try:
                inst.write(f"CHANnel{ch}:SCALe {ch_scale:.6g}")
                inst.write(f"CHANnel{ch}:OFFSet {ch_offset:.6g}")
            except Exception:
                pass
            restore_acq(inst, snap)
        else:
            if snap.get("acquire_points"):
                inst.write(f"ACQuire:POINts {snap['acquire_points']}")
            inst.write("RUNContinous")

    print(f"触发模式结束（{reason}）：共抓到 {events} 次，"
          f"运行 {time.time() - started:.1f}s")
    print(f"输出目录: {out_dir}")
    return 0


def cmd_watch(args, inst: Scpi) -> int:
    if args.stable_periods < 10:
        raise ScpiError("记录前至少保持 10 个周期")
    ch = args.channel
    state = inst.query(f"CHANnel{ch}:STATe?", DEFAULT_TIMEOUT).strip()
    if state not in ("1", "ON"):
        raise ScpiError(f"CH{ch} 未开启（CHANnel{ch}:STATe? = {state}），无法监测")

    out_dir = ensure_dir(args.out)
    return run_trigger_mode(args, inst, ch, out_dir)


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


def is_supported_idn(idn: str) -> bool:
    fields = [part.strip() for part in idn.split(",")]
    return (len(fields) >= 2 and fields[0].lower() == "rohde&schwarz"
            and fields[1].upper() in ("RTB2002", "RTB2004"))


def at_least_ten_periods(value: str) -> int:
    try:
        periods = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("需要整数周期数") from exc
    if periods < 10:
        raise argparse.ArgumentTypeError("记录前至少保持 10 个周期")
    return periods


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scope.py",
        description="R&S RTB2000 系列示波器以太网 SCPI 控制（零第三方依赖）",
        epilog=(
            "按采集目标选模式:\n"
            "  ① 等信号出现: watch；默认电平 1 V，稳定达到指定周期后导出。\n"
            "     抓指定边沿一次: csv --mode trigger，可指定 --trig-level。\n"
            "  ② 已有持续波形: csv --mode continuous（默认）；只抓屏幕用 shot。\n"
            "     连续采集全程 AUTO，判断只使用数值波形；图片仅作输出。\n"
            "仪器地址: --ip 或环境变量 "
            f"{ENV_IP}；discover 仅列出候选地址"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--ip", help="仪器 IP；先用 discover 或仪器 LAN 页面确认")
    parser.add_argument("--port", type=int, default=int(os.environ.get(ENV_PORT, DEFAULT_PORT)),
                        help=f"SCPI 端口，默认 {DEFAULT_PORT}")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="连接超时秒数")
    parser.add_argument("--query-timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"单条 SCPI 查询超时秒数，默认 {DEFAULT_TIMEOUT}；"
                             f"极慢时基下仪器应答变慢可调大（调大后无响应时等待更久）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("idn", help="读取仪器标识与通道设置")

    p_shot = sub.add_parser("shot", help="抓取屏幕截图 PNG")
    p_shot.add_argument("--out", help="输出目录，默认 ./scope_out")
    p_shot.add_argument("--name", help="文件名（不含扩展名亦可）")

    p_csv = sub.add_parser("csv", help="采集波形并输出 CSV + 波形图（固定输出，另附 JSON 元数据）")
    p_csv.add_argument("--channels", default="", help="通道列表，如 1,2；默认全部开启的通道")
    p_csv.add_argument("--out", help="输出目录，默认 ./scope_out")
    p_csv.add_argument("--name", help="文件名前缀")
    p_csv.add_argument("--mode", choices=["continuous", "trigger"], default=None,
                       help="运行模式：continuous=连续运行；trigger=抓边沿、调稳后导出一次。"
                            "不写按 continuous 处理，且会在输出里明确提示（建议先与用户确认用哪种）")
    p_csv.add_argument("--trig-source", default="CH1", help="触发源，默认 CH1（仅 --mode trigger）")
    p_csv.add_argument("--trig-slope", choices=["rise", "fall"], default="rise",
                       help="触发及稳定性检查的边沿：rise=上升沿（默认）/ fall=下降沿")
    p_csv.add_argument("--trig-level", type=float, default=None,
                       help="触发电平（V）；trigger 模式不给则读面板电平，读不到回退 1 V；"
                            "continuous 模式不设置仪器触发电平")
    p_csv.add_argument("--trig-mode", choices=["auto", "normal"], default="normal",
                       help="触发方式：normal=标准（默认）/ auto=自动（仅 --mode trigger）")
    p_csv.add_argument("--trig-wait", type=float, default=10.0,
                       help="等待触发的超时秒数，默认 10（仅 --mode trigger）")
    p_csv.add_argument("--settle", type=float, default=1.2, help="连续运行采集时长（秒），默认 1.2")
    p_csv.add_argument("--cooldown", type=float, default=COOL_AFTER_STOP,
                       help=f"每次 STOP 后的冷却秒数，默认 {COOL_AFTER_STOP}；"
                            f"高频 RUN/STOP 会把仪器 SCPI 服务打崩，不建议调到 0.2 以下")
    p_csv.add_argument("--observe", type=float, default=6.0,
                       help="连续模式数值观察时长（秒），默认 6；0=跳过长期观察")
    p_csv.add_argument("--observe-interval", type=float, default=1.0,
                       help="连续观察每次数值记录的采集时长（秒），默认 1.0"
                            "（调小会增加 RUN/STOP 次数，仪器更容易掉线）")
    p_csv.add_argument("--screen-periods", type=int, default=10,
                       help="自动调时基后屏幕显示几个信号周期，默认 10")
    p_csv.add_argument("--stable-periods", type=at_least_ten_periods, default=10,
                       help="波形稳定保持几个周期才写 CSV/PNG，至少 10（默认 10）")
    p_csv.add_argument("--period-tol", type=float, default=0.02,
                       help="周期内误差容限：单个周期相对平均周期的最大偏差，默认 0.02（2%%）")
    p_csv.add_argument("--min-spp", type=float, default=10.0,
                       help="每周期最少采样点数，默认 10（低于此值有混叠风险，判为不可用）")
    p_csv.add_argument("--fill-min", type=float, default=0.35,
                       help="垂直填充率下限，默认 0.35（1-2-5 档位量化下填充率只能落在约 40%%~95%%）")
    p_csv.add_argument("--fill-max", type=float, default=0.95,
                       help="垂直填充率上限，默认 0.95（超过视为有削顶风险）")
    p_csv.add_argument("--periods", type=int, default=0,
                       help="只截取 N 个完整周期（自动扫描时基提高采样率），0=不限制")
    p_csv.add_argument("--timebase", default=None,
                       help="直接指定时基（s/div），支持 20us / 100us / 1ms / 2e-5 写法；"
                            "指定后不再自动扫时基；连续模式成功后保留设定值")
    p_csv.add_argument("--preset", choices=["freq", "detail", "edge"], default=None,
                       help="按用途自动选时基：freq≈10 个周期(默认看频率/占空比)；"
                            "detail≈1.5 个周期(看单周期细节)；edge≈1/20 个周期(看边沿，采样率最高)")

    p_stats = sub.add_parser("stats", help="统计波形（不落盘）")
    p_stats.add_argument("--channels", default="", help="通道列表，如 1,2")

    p_watch = sub.add_parser(
        "watch",
        help="常驻轮询等待持续信号，抓边沿并确认稳定后导出",
        description="等待持续信号时使用：先用 NORMal + RUN 武装并等待真实触发，"
                    "触发后读取记录调量程与时基，稳定保持 10 个周期后导出 CSV 和波形图，随后重新武装。"
                    "一次性短脉冲可能在轮询期间漏掉。",
    )
    p_watch.add_argument("--channel", type=int, default=1, help="监测通道，默认 1")
    p_watch.add_argument("--out", help="输出目录，默认 ./scope_out")
    p_watch.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数，默认 1.0")
    p_watch.add_argument("--duration", type=float, default=0.0,
                         help="监测总时长秒数，0=一直跑（Ctrl+C 结束），默认 0")
    p_watch.add_argument("--settle", type=float, default=1.0,
                         help="检测到变化后等待稳定秒数，默认 1.0")
    p_watch.add_argument("--cooldown", type=float, default=COOL_AFTER_STOP,
                         help=f"每次 STOP 后的冷却秒数，默认 {COOL_AFTER_STOP}")
    p_watch.add_argument("--screen-periods", type=int, default=10,
                         help="自动调时基后屏幕上显示几个信号周期，默认 10（0=不改时基）")
    p_watch.add_argument("--stable-periods", type=at_least_ten_periods, default=10,
                         help="波形连续稳定保持几个周期才写 CSV/PNG，至少 10（默认 10）")
    p_watch.add_argument("--period-tol", type=float, default=0.02,
                         help="周期内误差容限：单个周期相对平均周期的最大偏差，默认 0.02（2%%）")
    p_watch.add_argument("--timebase", default=None,
                         help="固定时基（仅在测不到频率时使用），如 5ms")
    p_watch.add_argument("--no-autoscale", action="store_true",
                         help="不做 AUToscale，只按频率改时基")
    p_watch.add_argument("--capture", choices=["shot", "csv", "both", "none"], default="csv",
                         help="事件触发后抓什么，默认 csv（只用数据：CSV + 数据画的波形图，不截图）")
    p_watch.add_argument("--trig-slope", choices=["rise", "fall"], default="rise",
                         help="触发沿，默认 rise（上升沿）")
    p_watch.add_argument("--trig-level", type=float, default=None,
                         help="触发电平（V）；不给则读取面板电平，读不到时回退到 1.0 V")
    p_watch.add_argument("--trig-wait", type=float, default=0.0,
                         help="单次等待信号的最长秒数，默认 0=一直等（触发模式）")
    p_watch.add_argument("--min-vpp", type=float, default=0.005,
                         help="判定“真有波形”的最小 Vpp，默认 0.005 V（防止噪声误触发）")
    p_watch.add_argument("--max-events", type=int, default=20,
                         help="最多处理多少个事件后退出，0=不限，默认 20")
    p_watch.add_argument("--keep-view", action="store_true",
                         help="结束时保留调好的显示；默认恢复监测前的量程/时基/运行状态")

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
    global COOL_AFTER_STOP, DEFAULT_TIMEOUT
    if getattr(args, "cooldown", None):
        COOL_AFTER_STOP = max(0.0, float(args.cooldown))
    if getattr(args, "query_timeout", None):
        DEFAULT_TIMEOUT = max(1.0, float(args.query_timeout))

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
        "watch": cmd_watch,
        "scpi": cmd_scpi,
    }

    inst = None
    try:
        inst = Scpi(ip, args.port, timeout=args.timeout)
        if args.cmd != "scpi":
            idn = inst.query("*IDN?", DEFAULT_TIMEOUT)
            if not is_supported_idn(idn):
                raise ScpiError(f"当前命令集仅验证 RTB2002/RTB2004，实际仪器: {idn}；"
                                "请先适配并验证命令，或用 scpi 直通诊断")
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
