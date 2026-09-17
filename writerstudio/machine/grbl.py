"""GRBL 协议层：状态解析、命令构造、响应判定。

涵盖 GRBL 1.1 常用交互：
    * 实时状态查询 ``?`` → ``<Idle|MPos:0.000,0.000,0.000|FS:0,0>``
    * 回零 ``$H``、解锁 ``$X``、软复位 ``\\x18``、进给保持 ``!``、循环启动 ``~``
    * Jog：``$J=G91 X10 F1000``
    * 设置查询 ``$$``、单参数 ``$#``/``$G``
    * 归位/报警状态判定与错误码解释

本模块为**纯逻辑**（无串口依赖），便于单元测试；实际收发在 ``serial_link``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# 实时控制字符
CMD_STATUS = b"?"
CMD_HOLD = b"!"
CMD_RESUME = b"~"
CMD_SOFT_RESET = b"\x18"
CMD_JOG_CANCEL = b"\x85"
CMD_SAFETY_DOOR = b"\x84"
CMD_CYCLE_AUTO = b"\x82"      # 循环开始（部分固件）

# 常用系统命令
CMD_HOME = "$H"               # 回原点（需要 $22=1）
CMD_UNLOCK = "$X"             # 解锁（清除报警）
CMD_SLEEP = "$SLP"            # 休眠（grbl 1.1 + $31 休眠功能/ESP32 固件支持）
CMD_SETTINGS = "$$"           # 读全部设置
CMD_BUILD_INFO = "$I"         # 固件/版本信息

# GRBL 状态机
MACHINE_STATES = {
    "Idle", "Run", "Jog", "Hold", "Door", "Home", "Alarm", "Check", "Sleep",
}

# 常见错误码
ERROR_CODES = {
    1: "命令字母未识别",
    2: "缺少数值或格式错误",
    3: "系统未归零或未解锁（需 $H 或 $X）",
    4: "负数值不支持",
    5: "设置项未启用/不匹配",
    6: "起步被安全门/保持状态阻止",
    7: "EEPROM 读取失败",
    8: "命令中未使用该轴",
    9: "G 代码锁定中",
    10: "软限位触发",
    11: "行太长",
    12: "步进速率超限",
    13: "安全门打开",
    14: "启动失败",
    15: "点动目标超出行程",
    16: "点动命令超出机器行程",
    17: "激光模式需开启",
    20: "不支持的命令",
    21: "行程模式冲突",
    22: "未设置进给率",
    23: "进给率必须为正",
    24: "圆弧半径错误",
    25: "重复 G 代码",
    26: "G 代码需与运动同用",
    27: "坐标不在行程内",
    28: "轴未归零",
    29: "G 代码不支持",
    30: "G53 需绝对坐标",
    31: "未设置轴或 G 代码",
    32: "未使用该轴",
    33: "无效的目标",
    34: "圆弧角度错误",
    35: "点动超行程",
    36: "无有效 G 代码",
    37: "G 代码冲突",
    38: "刀具长度偏移未设置",
}

ALARM_CODES = {
    1: "硬限位触发（需解锁）",
    2: "软限位触发",
    3: "复位时正在移动",
    4: "探测失败",
    5: "探测未接触",
    6: "复位未找到开关",
    7: "复位未到位",
    8: "复位未清除开关",
    9: "复位行程不足",
}


class ResponseKind(Enum):
    OK = "ok"
    ERROR = "error"
    ALARM = "alarm"
    STATUS = "status"
    SETTING = "setting"
    MESSAGE = "message"
    VERSION = "version"
    ESP = "esp"            # ESP32 固件扩展响应 [ESPxx]...
    UNKNOWN = "unknown"


@dataclass
class MachineStatus:
    """解析后的 ``?`` 状态报告。"""

    state: str = "Unknown"
    mpos: Optional[tuple[float, float, float]] = None   # 机械坐标
    wpos: Optional[tuple[float, float, float]] = None   # 工件坐标
    wco: Optional[tuple[float, float, float]] = None    # 坐标偏移
    feed: Optional[float] = None
    speed: Optional[float] = None
    spindle: Optional[float] = None
    pins: str = ""
    raw: str = ""

    @property
    def pos(self) -> Optional[tuple[float, float, float]]:
        return self.mpos if self.mpos is not None else self.wpos

    @property
    def is_idle(self) -> bool:
        return self.state == "Idle"

    @property
    def is_running(self) -> bool:
        return self.state in ("Run", "Jog")

    @property
    def is_alarm(self) -> bool:
        return self.state == "Alarm"

    @property
    def is_homed(self) -> bool:
        return self.state in ("Idle", "Run", "Jog", "Hold")


def parse_status(text: str) -> Optional[MachineStatus]:
    """解析 ``<...>`` 状态串。非状态串返回 None。

    兼容两种格式：
        * grbl ≥1.1：``<Idle|MPos:0.000,0.000,0.000|FS:0,0>``
        * 旧版：    ``<Idle,MPos:0.000,0.000,0.000,WPos:...>``（逗号分隔）
    """
    m = re.search(r"<([^>]*)>", text)
    if not m:
        return None
    body = m.group(1)
    # 旧版逗号格式：<Idle,MPos:0.000,0.000,0.000,WPos:...>
    # 坐标值本身也用逗号分隔，需要按 key 分组重组成竖线格式。
    if "|" not in body and re.search(r",(MPos|WPos|WCO):", body):
        fields = body.split(",")
        grouped: list[str] = []
        i = 0
        while i < len(fields):
            f = fields[i]
            key = f.split(":", 1)[0]
            if key in ("MPos", "WPos", "WCO") and ":" in f:
                vals = [f.split(":", 1)[1]]
                while len(vals) < 3 and i + 1 < len(fields) \
                        and ":" not in fields[i + 1]:
                    i += 1
                    vals.append(fields[i])
                grouped.append(f"{key}:{','.join(vals)}")
            else:
                grouped.append(f)
            i += 1
        body = "|".join(grouped)
    parts = body.split("|")
    if not parts:
        return None
    st = MachineStatus(state=parts[0], raw=body)
    for p in parts[1:]:
        if ":" not in p:
            if p.startswith(("Pn", "F", "S")):
                continue
            continue
        key, _, val = p.partition(":")
        try:
            if key == "MPos":
                st.mpos = _parse_axes(val)
            elif key == "WPos":
                st.wpos = _parse_axes(val)
            elif key == "WCO":
                st.wco = _parse_axes(val)
            elif key == "FS":
                vals = [float(v) for v in val.split(",") if v != ""]
                if len(vals) >= 1:
                    st.feed = vals[0]
                if len(vals) >= 2:
                    st.speed = vals[1]
            elif key == "F":
                st.feed = float(val)
            elif key == "S":
                st.spindle = float(val)
            elif key == "Pn":
                st.pins = val
        except (ValueError, TypeError):
            continue
    return st


def _parse_axes(val: str) -> tuple[float, float, float]:
    vals = []
    for v in val.split(","):
        try:
            vals.append(float(v))
        except ValueError:
            vals.append(0.0)
    while len(vals) < 3:
        vals.append(0.0)
    return (vals[0], vals[1], vals[2])


def classify(line: str) -> ResponseKind:
    s = line.strip()
    low = s.lower()
    if s.startswith("<"):
        return ResponseKind.STATUS
    if low == "ok":
        return ResponseKind.OK
    if low.startswith("error"):
        return ResponseKind.ERROR
    if low.startswith("alarm"):
        return ResponseKind.ALARM
    if s.startswith("$"):
        return ResponseKind.SETTING
    if re.match(r"^\[ESP\d+\]", s):
        return ResponseKind.ESP
    # ESP32 定制固件（部分写字机）用 ``[VER:1.1.20220207:]``
    # 报告版本，而非标准 grbl 的 ``Grbl 1.1f [...]`` 欢迎行
    if s.startswith("[VER:"):
        return ResponseKind.VERSION
    if low.startswith("["):
        return ResponseKind.MESSAGE
    if low.startswith("grbl"):
        return ResponseKind.VERSION
    return ResponseKind.UNKNOWN


_VERSION_RE = re.compile(r"([0-9]+\.[0-9]+)")


def parse_version(line: str) -> Optional[float]:
    """解析版本号，兼容两种格式：

    * 标准 grbl 欢迎行：``Grbl 1.1f ['$' for help]``
    * ESP32 定制固件：  ``[VER:1.1.20220207:]``（部分写字机固件）
    """
    s = line.strip()
    if not (re.match(r"^grbl", s, re.IGNORECASE) or s.startswith("[VER:")):
        return None
    v = _VERSION_RE.search(s)
    if v:
        try:
            return float(v.group(1))
        except ValueError:
            return None
    return None


def parse_firmware_info(line: str) -> dict[str, str]:
    """从 ``[VER:...]`` / ``[OPT:...]`` / ``[MSG:...]`` 提取固件信息。

    返回可能为空的字典，键：``version``（原始版本串）、``options``、
    ``machine``（如 ``[MSG:Using machine:...]`` 里的机型名）、
    ``message``（普通 MSG 文本）。
    """
    s = line.strip()
    info: dict[str, str] = {}
    m = re.match(r"^\[VER:([^\]]*)\]$", s)
    if m:
        info["version"] = m.group(1)
        return info
    m = re.match(r"^\[OPT:([^\]]*)\]$", s)
    if m:
        info["options"] = m.group(1)
        return info
    m = re.match(r"^\[MSG:([^\]]*)\]$", s)
    if m:
        body = m.group(1).strip()
        mm = re.match(r"Using machine:(\S+)", body)
        if mm:
            info["machine"] = mm.group(1)
        else:
            info["message"] = body
    return info


def parse_esp_response(line: str) -> Optional[tuple[int, str]]:
    """解析 ESP32 扩展响应 ``[ESP010]payload`` → (编号, 载荷)。"""
    m = re.match(r"^\[ESP(\d+)\](.*)$", line.strip())
    if m:
        return int(m.group(1)), m.group(2)
    return None


def error_message(line: str) -> str:
    m = re.search(r"error:?(\d+)", line, re.IGNORECASE)
    if m:
        code = int(m.group(1))
        return f"错误 {code}：{ERROR_CODES.get(code, '未知错误')}"
    return line.strip()


def alarm_message(line: str) -> str:
    m = re.search(r"alarm:?(\d+)", line, re.IGNORECASE)
    if m:
        code = int(m.group(1))
        return f"报警 {code}：{ALARM_CODES.get(code, '未知报警')}"
    return line.strip()


# ---------------------------------------------------------------------------
# 命令构造
# ---------------------------------------------------------------------------
def jog_command(dx: float = 0.0, dy: float = 0.0, dz: float = 0.0,
                feed: float = 1000.0, absolute: bool = False,
                grbl_version: Optional[float] = None) -> str:
    """构造 Jog 命令。默认相对模式(G91)。

    **版本感知**策略（按固件版本选择命令形式）：
        * grbl ≥ 1.1：``$J=G21G91 X.. F..``（专用 jog 模式，不改变 G 组状态）
        * grbl < 1.1：老固件不支持 ``$J=``，退化为 ``G21G91 X.. F..``
    ``grbl_version`` 传 None 时按 1.1 处理。
    """
    modern = grbl_version is None or grbl_version >= 1.1
    mode = "G90" if absolute else "G91"
    prefix = f"$J=G21{mode}" if modern else f"G21{mode}"
    parts = [prefix]
    if dx:
        parts.append(f"X{dx:g}")
    if dy:
        parts.append(f"Y{dy:g}")
    if dz:
        parts.append(f"Z{dz:g}")
    parts.append(f"F{feed:g}")
    return " ".join(parts)


def goto_command(x: float, y: float, feed: float = 3000.0,
                 absolute: bool = True) -> str:
    mode = "G90" if absolute else "G91"
    return f"G0 {mode} X{x:g} Y{y:g}"


def set_origin_command(axes: str = "XY") -> str:
    """把当前点设为工件零点（G92 临时坐标系）。"""
    return f"G92 {axes}0"


def clear_origin_command() -> str:
    """清除 G92 偏移（``G92.1``）。

    其它控制软件可能发 ``G92 X.. Y.. Z0`` 设临时工件零点；本软件的作业把
    起点烘焙进绝对坐标，残留 G92 会让所有坐标整体错位——Z 向偏移还会把
    落笔/抬笔深度放大数倍冲击舵机。发作业前清一次（本会话手工设零除外）。
    """
    return "G92.1"


def clear_work_offset_command(system: int = 1) -> str:
    """把工件坐标系（默认 G54）偏移清零：``G10 L2 P1 X0 Y0 Z0``。

    部分固件把 G54 偏移持久化在 EEPROM（开机即带着上次标定的偏移），
    ``G92.1`` 清不掉它。GRBL 的 ``G90`` 绝对指令走工件坐标，残留 G54 会让
    烘焙绝对坐标的作业 XY 整体错位、Z 深度错位，且无回零（``$22=0``）时
    MPos 每次连接都从 0 起算，该偏移并无标定意义。发作业前清一次。
    """
    return f"G10 L2 P{int(system)} X0 Y0 Z0"


def home_command() -> str:
    return CMD_HOME


def unlock_command() -> str:
    return CMD_UNLOCK


def sleep_command() -> str:
    """休眠命令 ``$SLP``（grbl 1.1 / ESP32 固件支持；不支持时会报 error）。"""
    return CMD_SLEEP


def build_info_command() -> str:
    """查询固件信息 ``$I``（连接成功后即发送）。"""
    return CMD_BUILD_INFO


def return_to_start_command(feed: float = 3000.0,
                            x: float = 0.0, y: float = 0.0) -> str:
    """回到书写起点：``G90 G0 X<x> Y<y>``。

    作业把起点偏移烘焙进绝对坐标（未发 G92），因此「起点」是作业
    实际开始书写的坐标（默认 0,0 即工件原点）。
    """
    return f"G90 G0 X{x:g} Y{y:g} F{feed:g}"


def set_origin_offset_command(offset_x: float = 0.0, offset_y: float = 0.0) -> str:
    """设置写字起点（G92 偏移）：``G92 X<ox> Y<oy> Z0``。"""
    parts = ["G92"]
    parts.append(f"X{offset_x:g}")
    parts.append(f"Y{offset_y:g}")
    parts.append("Z0")
    return " ".join(parts)


def dwell_command(seconds: float) -> str:
    """暂停 ``G4 P<秒>``——落笔后/抬笔前等待。"""
    return f"G4 P{max(0.0, seconds):g}"


def parse_settings(lines: list[str]) -> dict[int, str]:
    """解析 ``$$`` 输出为 {编号: 值}。"""
    out: dict[int, str] = {}
    for line in lines:
        m = re.match(r"\$(\d+)=([^ (]*)(?:\s*\(([^)]*)\))?", line.strip())
        if m:
            out[int(m.group(1))] = m.group(2).strip()
    return out


def parse_settings_comments(lines: list[str]) -> dict[int, tuple[str, str]]:
    """解析 ``$$`` 输出为 {编号: (值, 注释)}（注释可能为空）。

    形如 ``$110=18000.000 (x-axis rate, mm/min)``。
    """
    out: dict[int, tuple[str, str]] = {}
    for line in lines:
        m = re.match(r"\$(\d+)=([^ (]*)(?:\s*\(([^)]*)\))?", line.strip())
        if m:
            out[int(m.group(1))] = (m.group(2).strip(),
                                    (m.group(3) or "").strip())
    return out


def parse_parser_state(line: str) -> dict[str, str]:
    """解析 ``[GC:...]`` / ``[G28:...]`` 等状态行。"""
    m = re.match(r"\[(\w+):([^\]]*)\]", line.strip())
    if not m:
        return {}
    return {m.group(1): m.group(2)}
