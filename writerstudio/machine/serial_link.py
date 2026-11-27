"""串口链接：与 GRBL 控制器通信（流式发送 + 状态轮询）。

设计要点：
    * 使用 GRBL 的**字符计数流控**：在途字节数（已发送 − 已确认）不超过
      ``RX_BUFFER_SIZE``（默认 128），避免控制器缓冲溢出丢步。
    * **后台线程**读串口，主线程通过回调接收事件（便于 Qt 使用）。
    * 支持暂停/继续/中止、实时状态轮询、Jog、回零、设零点。
    * 通过 :class:`SerialBackend` 协议隔离 pyserial，便于用虚拟串口或
      :class:`FakeSerial` 做单元测试。

线程模型：``connect()`` 启动读线程；写操作加锁，可跨线程调用。
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol

from ..i18n import tr, trf
from . import grbl
from .config import (
    PEN_CUSTOM,
    PEN_LASER,
    PEN_M3M5,
    PEN_M7M9,
    PEN_SERVO_ANGLE,
    PEN_Z,
)
from .profile import MachineProfile

RX_BUFFER_SIZE = 128          # GRBL 默认接收缓冲字节数
STATUS_POLL_INTERVAL = 0.2    # 状态轮询间隔（秒）
PROBE_MIN_SETTINGS = 4        # 收到这么多条 $N=… 才认定是一份 $$ 应答

# 注入队列来源标签：区分清偏移、暂停抬笔与继续恢复，支持自由反复暂停/继续
_INJ_OFFSET = "offset"
_INJ_PAUSE = "pause"
_INJ_RESUME_MOVE = "resume_move"
_INJ_RESUME_DOWN = "resume_down"
_INJ_RESUME_MODAL = "resume_modal"

_GCODE_COMMENT_RE = re.compile(r"\([^)]*\)")
_GCODE_WORD_RE = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))")


def _strip_gcode_comments(line: str) -> str:
    """去除 G-code 行内的括号注释与分号尾注，返回大写规范文本。"""
    s = _GCODE_COMMENT_RE.sub("", line)
    if ";" in s:
        s = s.split(";", 1)[0]
    return " ".join(s.strip().upper().split())


def _line_has_word(line: str, letter: str) -> bool:
    """判断单行 G-code 是否显式携带指定词字母（如 ``F``）。"""
    clean = _strip_gcode_comments(line)
    target = letter.upper()
    return any(w == target for w, _ in _GCODE_WORD_RE.findall(clean))


class _JobMotionTracker:
    """实时追踪已发送作业行的模态位置、进给与抬落笔状态。

    用于暂停时精确锁定「最后暂停的位置与笔状态」，确保恢复继续时：
    1. 暂停期间若手动移动过笔头，可先抬笔精准返回最后暂停的 ``(X, Y)``；
    2. 仅在暂停前确实处于落笔态时才落笔（精确还原斜坡 Z 高度），空程暂停绝不误落笔划纸；
    3. 被 Z 抬落笔改写的模态进给率 ``F`` 可按需恢复，避免后续省略 ``F`` 的行失速。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.abs_mode: bool = True
        self.motion: str = ""
        self.last_xy_motion: str = ""
        self.x: Optional[float] = None
        self.y: Optional[float] = None
        self.z: Optional[float] = None
        self.draw_feed: Optional[float] = None
        self.last_feed: Optional[float] = None
        self.spindle_on: Optional[bool] = None
        self.spindle_s: Optional[float] = None
        self.coolant_on: Optional[bool] = None

    @property
    def xy(self) -> Optional[tuple[float, float]]:
        if self.x is not None and self.y is not None:
            return (self.x, self.y)
        return None

    def feed(self, line: str) -> None:
        """解析一条已发出的作业 G-code 行，更新当前模态快照。"""
        clean = _strip_gcode_comments(line)
        if not clean or clean.startswith("$"):
            return
        words = [(ch, float(val)) for ch, val in _GCODE_WORD_RE.findall(clean)]
        if not words:
            return
        has_xy = False
        line_motion: Optional[str] = None
        line_f: Optional[float] = None
        for ch, val in words:
            if ch == "G":
                code = int(round(val))
                if abs(val - code) < 1e-6:
                    if code == 90:
                        self.abs_mode = True
                    elif code == 91:
                        self.abs_mode = False
                    elif code == 0:
                        line_motion = "G0"
                        self.motion = "G0"
                    elif code == 1:
                        line_motion = "G1"
                        self.motion = "G1"
                    elif code in (2, 3):
                        line_motion = f"G{code}"
                        self.motion = line_motion
            elif ch == "M":
                code = int(round(val))
                if abs(val - code) < 1e-6:
                    if code in (3, 4):
                        self.spindle_on = True
                    elif code == 5:
                        self.spindle_on = False
                    elif code in (7, 8):
                        self.coolant_on = True
                    elif code == 9:
                        self.coolant_on = False
            elif ch == "X":
                self.x = val if self.abs_mode else (self.x or 0.0) + val
                has_xy = True
            elif ch == "Y":
                self.y = val if self.abs_mode else (self.y or 0.0) + val
                has_xy = True
            elif ch == "Z":
                self.z = val if self.abs_mode else (self.z or 0.0) + val
            elif ch == "F":
                line_f = val
                self.last_feed = val
            elif ch == "S":
                self.spindle_s = val
        if has_xy:
            eff_motion = line_motion or self.motion
            if eff_motion:
                self.last_xy_motion = eff_motion
            if line_f is not None and eff_motion != "G0":
                self.draw_feed = line_f

    def pen_state(self, pen_cfg, sent_lines: list[str]) -> Optional[bool]:
        """依据笔控配置判定当前笔的显式状态。

        返回 ``True``（明确落笔）、``False``（明确抬笔）或 ``None``（作业中
        尚未出现该模式的显式抬落笔指令）。
        """
        if pen_cfg is None:
            return None
        mode = getattr(pen_cfg, "pen_mode", PEN_Z)
        if mode == PEN_Z:
            if self.z is None:
                return None
            return abs(self.z - float(pen_cfg.pen_up_z)) > 1e-3
        if mode == PEN_SERVO_ANGLE:
            if self.spindle_on is False:
                return False
            if self.spindle_on is True and self.spindle_s is not None:
                return abs(self.spindle_s - float(pen_cfg.pen_up_s)) > 1e-3
            return None
        if mode in (PEN_M3M5, PEN_LASER):
            if self.spindle_on is False:
                return False
            if self.spindle_on is True:
                return self.spindle_s is None or self.spindle_s > 1e-6
            return None
        if mode == PEN_M7M9:
            return self.coolant_on
        if mode == PEN_CUSTOM:
            up_cmds = {_strip_gcode_comments(x) for x in pen_cfg.pen_up_lines()
                       if _strip_gcode_comments(x) and not _strip_gcode_comments(x).startswith("G4")}
            down_cmds = {_strip_gcode_comments(x) for x in pen_cfg.pen_down_lines()
                         if _strip_gcode_comments(x) and not _strip_gcode_comments(x).startswith("G4")}
            for raw in reversed(sent_lines):
                norm = _strip_gcode_comments(raw)
                if norm in down_cmds and norm not in up_cmds:
                    return True
                if norm in up_cmds and norm not in down_cmds:
                    return False
            return None
        return None

    def is_pen_down(self, pen_cfg, sent_lines: list[str]) -> bool:
        """判定暂停瞬间笔是否压在纸上（无显式抬落笔时按最后 XY 运动兜底）。"""
        explicit = self.pen_state(pen_cfg, sent_lines)
        if explicit is not None:
            return explicit
        # 作业未含显式抬落笔指令（如纯 G1 线段流）：正在执行直线插补视为落笔态
        return self.last_xy_motion in ("G1", "G2", "G3")


@dataclass
class _PauseSnapshot:
    """暂停瞬间的精确断点状态快照，用于自由恢复并从最后暂停处续写。"""

    line_index: int
    xy: Optional[tuple[float, float]]
    z: Optional[float]
    abs_mode: bool
    pen_down: bool
    feed: Optional[float]
    pen_cfg: object
    used_hold: bool
    moved_while_paused: bool = False


class State(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ALARM = "alarm"


# 控制器状态串 → 软件状态。GRBL 可能在软件之外进入 Hold/Alarm
# （上一会话残留、物理暂停、限位/报警），必须同步显示，否则界面会误报
# 「空闲」并把作业发给一台保持中的机器（收到 ok 但不执行）。
_CONTROLLER_STATE = {
    "idle": State.IDLE,
    "run": State.RUNNING,
    "jog": State.RUNNING,
    "hold": State.PAUSED,
    "door": State.PAUSED,
    "check": State.PAUSED,
    "alarm": State.ALARM,
    "sleep": State.IDLE,
}


@dataclass
class LinkEvent:
    """从控制器收到/发出的事件。"""

    kind: str                 # line / status / ok / error / alarm / state / sent / profile
    text: str = ""
    status: Optional[grbl.MachineStatus] = None
    profile: Optional[MachineProfile] = None


class SerialBackend(Protocol):
    """pyserial 的最小接口（便于测试替换）。"""

    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    @property
    def in_waiting(self) -> int: ...
    def close(self) -> None: ...


def apply_serial_params(backend: object, dtr: Optional[bool] = None,
                        rts: Optional[bool] = None) -> None:
    """连接后设置 DTR/RTS 电平（若后端支持）。

    ESP32 等 dev-board 串口芯片会把 DTR/RTS 接到 EN/IO0 上，
    电平不对会导致打开串口即复位固件（开发板通病，故提供开关）。
    （dataBits/stopBits/parity/setRTS/setDTR）。
    """
    try:
        if dtr is not None and hasattr(backend, "setDTR"):
            backend.setDTR(dtr)
        if rts is not None and hasattr(backend, "setRTS"):
            backend.setRTS(rts)
    except Exception:
        pass


def list_ports() -> list[tuple[str, str]]:
    """列出可用串口 [(设备, 描述)]。"""
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return []
    out = []
    for p in lp.comports():
        desc = p.description or ""
        if desc in ("n/a", "N/A"):
            desc = p.device
        out.append((p.device, desc))
    return out


class GrblLink:
    """GRBL 串口链接与流式发送器。"""

    def __init__(self, on_event: Optional[Callable[[LinkEvent], None]] = None) -> None:
        self._serial: Optional[SerialBackend] = None
        self._on_event = on_event or (lambda e: None)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.RLock()      # 保护队列与流控状态

        self._line_buffer = bytearray()     # 未完成的行
        self._tag_buffer = bytearray()      # 未完成的状态串

        # 流控队列：在途行 (字节数, 是否注入行)；注入行不参与作业计数
        self._pending: deque[tuple[int, bool]] = deque()
        # 优先发送的注入行：(来源标签, 指令文本)，区分清偏移/暂停抬笔/继续恢复
        self._injected: deque[tuple[str, str]] = deque()
        self._extra_inflight = 0
        self._job_lines_cache: list[str] = []
        # 流控计数在 __init__ 即就位：connect 之前调用 progress()/load_job
        # 不会撞 AttributeError
        self._job_total = 0
        self._job_acked = 0
        self._job_lines = 0
        self._all_acked_at = 0.0     # 全部 ack 的时刻（等机械停稳用）

        self.state = State.DISCONNECTED
        self.status: Optional[grbl.MachineStatus] = None
        self.version: str = ""
        self.grbl_version: Optional[float] = None   # 1.1 / 0.9 ...
        self._last_poll = 0.0
        self._paused = False
        self._finish_announced = False
        # 作业模态与暂停断点追踪：支持自由多次暂停/继续，并从最后暂停位置精准续写
        self._tracker = _JobMotionTracker()
        self._pause_snap: Optional[_PauseSnapshot] = None
        self._resume_pending_snap: Optional[_PauseSnapshot] = None
        self._pause_lift_sent = False
        self._resume_move_needed = False
        # --- 扩展能力 ---
        self.single_step = False        # 单步模式：在途行确认后才发下一行
        self.stale_seconds = 5.0        # 状态看门狗：N 秒无状态报告 → stale 事件
        self._last_status_time = 0.0
        self._stale_emitted = False
        self._pen_cfg = None            # 抬笔暂停用的笔控配置
        self._greeting = True           # 连接后发 $I 查询固件信息
        self._job_home: Optional[tuple[float, float]] = None  # 作业书写起点
        # 能力探测：连接后发 $$ 收集固件设置，凑齐一份应答后产出 profile
        self._settings_buf: dict[float, float] = {}
        self.profile: Optional[MachineProfile] = None

    # ------------------------------------------------------------ 连接
    def connect(self, port: str, baudrate: int = 115200,
                backend: Optional[SerialBackend] = None,
                dtr: Optional[bool] = None, rts: Optional[bool] = None) -> None:
        if self._serial is not None:
            self.disconnect()
        self._set_state(State.CONNECTING)
        try:
            if backend is None:
                # write_timeout 必须设：USB 串口假死时 write 会无限阻塞，
                # 把界面线程一起卡成「未响应」
                import serial
                backend = serial.Serial(port, baudrate,
                                        timeout=0.05, write_timeout=1.0)
            apply_serial_params(backend, dtr=dtr, rts=rts)
        except Exception:
            # 打开失败（占用/权限等）：不能把状态留在「连接中」
            self._set_state(State.DISCONNECTED)
            raise
        self._serial = backend
        self._stop.clear()
        self._reset_flow()
        self._thread = threading.Thread(target=self._reader_loop, daemon=True,
                                        name="grbl-reader")
        self._thread.start()
        self._set_state(State.IDLE)
        self._emit("line", f"已连接 {port} @ {baudrate}")
        if self._greeting:
            # 连接成功后即发 ``$I`` 触发固件回版本信息（Grbl x.xx）
            try:
                self.send_line(grbl.build_info_command())
            except Exception:
                pass
            # 能力探测：读全部设置（加速度/速率上限/轴数）。不回 $$ 的固件
            # 只是没有档案而已，一切功能按「未报告 = 不具备」降级
            try:
                self.query_settings()
            except Exception:
                pass

    def connect_backend(self, backend: SerialBackend) -> None:
        """直接注入后端（测试/虚拟串口用）。"""
        self.connect("", backend=backend)

    @property
    def connected(self) -> bool:
        return self._serial is not None

    @property
    def is_paused(self) -> bool:
        """当前是否处于暂停态（软件主动暂停或控制器外部保持 Hold/Door）。"""
        with self._lock:
            return self._paused or self.state is State.PAUSED

    @property
    def job_active(self) -> bool:
        """当前是否存在尚未完成的已装载作业（运行中或暂停中均为 True）。"""
        with self._lock:
            return (self._job_lines > 0
                    and not self._finish_announced
                    and self.state is not State.ALARM)

    @property
    def pause_point(self) -> Optional[tuple[int, Optional[tuple[float, float]]]]:
        """暂停时的断点信息 ``(已发作业行数, 最后暂停 XY 坐标)``；未暂停返回 None。"""
        with self._lock:
            if not self._paused or self._pause_snap is None:
                return None
            return (self._pause_snap.line_index, self._pause_snap.xy)

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # 可能从读线程自身调用（如 pump 写失败）：join 自己会抛 RuntimeError
            if self._thread is not threading.current_thread():
                self._thread.join(timeout=1.0)
            self._thread = None
        ser, self._serial = self._serial, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        self._set_state(State.DISCONNECTED)

    # ------------------------------------------------------------ 读线程
    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            try:
                ser = self._serial
                if ser is None:
                    break
                n = ser.in_waiting
                if n:
                    data = ser.read(n)
                    if data:
                        self._on_bytes(data)
                else:
                    time.sleep(0.005)
                self._maybe_poll()
                self._pump()
            except Exception as exc:
                if not self._stop.is_set():
                    self._emit("line", trf("串口错误：{v0}", v0=exc))
                    # 读线程死亡后必须完整收尾（关串口、复位状态），
                    # 否则 connected 仍为 True 而链接已不可用
                    self.disconnect()
                break

    def _on_bytes(self, data: bytes) -> None:
        for byte in data:
            ch = bytes([byte])
            if ch == b"\n":
                if self._tag_buffer:
                    # 状态串没等到 '>' 就来了换行：残片作废，回到行模式，
                    # 否则后续正常行会被继续堆进 tag 缓冲而丢失
                    self._tag_buffer.clear()
                line = bytes(self._line_buffer).decode("utf-8", "replace").strip()
                self._line_buffer.clear()
                if line:
                    self._handle_line(line)
            elif ch == b">":
                # 状态串结束
                body = bytes(self._tag_buffer) + b">"
                self._tag_buffer.clear()
                st = grbl.parse_status(body.decode("utf-8", "replace"))
                if st:
                    self.status = st
                    self._last_status_time = time.monotonic()
                    self._stale_emitted = False
                    # 以控制器上报状态为准（外部 Hold/Alarm 可见）。
                    # 例外：
                    # 1. 软件主动暂停中：控制器排空在途缓冲报 Run、停稳报 Idle、
                    #    或暂停期间手动点动报 Jog 都不得把 PAUSED 冲回 RUNNING/IDLE
                    #    （否则界面「继续」按钮会被覆写成「暂停」，作业死锁）；
                    #    仅 ALARM 具有最高优先级，可打断暂停。
                    # 2. 作业进行中的行间瞬时 Idle 不得把 RUNNING 打回空闲。
                    name = (st.state or "").strip().lower().split(":")[0]
                    mapped = _CONTROLLER_STATE.get(name)
                    with self._lock:
                        paused = self._paused
                        job_active = (self._job_lines > 0
                                      and not self._finish_announced)
                    if mapped is not None:
                        if mapped is State.ALARM:
                            with self._lock:
                                self._paused = False
                                self._pause_snap = None
                                self._resume_pending_snap = None
                                self._pause_lift_sent = False
                                self._resume_move_needed = False
                                self._pen_cfg = None
                            self._set_state(State.ALARM)
                        elif paused:
                            pass
                        elif mapped is State.IDLE and job_active:
                            pass
                        else:
                            self._set_state(mapped)
                    self._emit("status", body.decode("utf-8", "replace"), st)
            else:
                if self._tag_buffer or ch == b"<":
                    if ch == b"<":
                        self._tag_buffer.clear()
                    self._tag_buffer.extend(ch)
                else:
                    self._line_buffer.extend(ch)

    def _handle_line(self, line: str) -> None:
        kind = grbl.classify(line)
        if kind is grbl.ResponseKind.OK:
            self._on_ack()
            self._finish_probe()
            self._emit("ok", "ok")
        elif kind is grbl.ResponseKind.ERROR:
            self._emit("error", grbl.error_message(line))
            self._on_ack()
        elif kind is grbl.ResponseKind.SETTING:
            # $$ 应答体：$N=value 行收集进探测缓冲，凑齐一份（以 ok 结尾）
            # 就产出 MachineProfile。其余 $ 开头的行（如未知的 $X）落不到
            # 解析里就留在缓冲，无碍——下一条 ok 会把它们一并清掉
            try:
                parsed = grbl.parse_settings([line])
                for code, val in parsed.items():
                    self._settings_buf[float(code)] = float(val)
            except ValueError:
                pass
            self._emit("line", line)
        elif kind is grbl.ResponseKind.ALARM:
            self._emit("alarm", grbl.alarm_message(line))
            # 报警：丢弃剩余作业，保持 ALARM 状态直到用户解锁($X)/回零
            with self._lock:
                self._job_total = self._job_lines
                self._pending.clear()
                self._injected.clear()
                self._paused = False
                self._pause_snap = None
                self._resume_pending_snap = None
                self._pause_lift_sent = False
                self._resume_move_needed = False
                self._pen_cfg = None
            self._set_state(State.ALARM)
            self._on_ack()
        elif kind is grbl.ResponseKind.VERSION:
            self.version = line
            self.grbl_version = grbl.parse_version(line)
            self._emit("version", line)
            self._emit("line", line)
        elif kind is grbl.ResponseKind.ESP:
            self._emit("esp", line)
            self._emit("line", line)
        else:
            self._emit("line", line)

    def _on_ack(self) -> None:
        """收到 ok：释放在途字节配额。"""
        with self._lock:
            if self._pending:
                _, is_extra = self._pending.popleft()
                if not is_extra:
                    self._job_acked += 1

    def _finish_probe(self) -> None:
        """ok 结束了一份应答：若缓冲里攒够了设置行则产出能力档案。

        ``$I`` 的应答同样以 ok 结尾但不带设置行——此时缓冲不足，直接清空，
        不会误产出。探测结果同时留在 :attr:`profile` 供同步读取。
        """
        with self._lock:
            buf = dict(self._settings_buf)
            self._settings_buf.clear()
        if len(buf) < PROBE_MIN_SETTINGS:
            return
        prof = MachineProfile.from_settings(
            buf, version=self.version, grbl_version=self.grbl_version)
        self.profile = prof
        self._emit("profile", prof.summary(), profile=prof)

    # ------------------------------------------------------------ 写/发送
    def _reset_flow(self) -> None:
        with self._lock:
            self._pending.clear()
            self._injected.clear()
            self._job_total = 0
            self._job_acked = 0
            self._job_lines = 0
            self._finish_announced = False
            self._last_status_time = 0.0
            self._stale_emitted = False
            self._settings_buf.clear()
            self._paused = False
            self._tracker.reset()
            self._pause_snap = None
            self._resume_pending_snap = None
            self._pause_lift_sent = False
            self._resume_move_needed = False
            self._pen_cfg = None
        self._job_home = None
        self.profile = None

    def _write_raw(self, data: bytes) -> None:
        ser = self._serial
        if ser is None:
            raise ConnectionError(tr("未连接"))
        ser.write(data)

    def send_line(self, line: str) -> None:
        """立即发送一行（用于查询/设置类命令，不参与流控）。

        持锁写出，避免与 _pump 的「记账后写出」交错导致顺序颠倒；
        实时字节（:meth:`realtime`）刻意不加锁——GRBL 实时字符可在
        任意字节间隙插入，且报警/保持不能被普通写阻塞。
        """
        line = line.strip()
        if not line:
            return
        data = (line + "\n").encode("ascii", "replace")
        with self._lock:
            self._write_raw(data)
        self._emit("sent", line)

    def realtime(self, cmd: bytes) -> None:
        self._write_raw(cmd)

    def zero_offset_lines(self) -> list[str]:
        """清零工件坐标偏移的指令（无条件，作业/绝对移动前都注入）。

        本软件把写字起点烘焙进**绝对**坐标，要求「工件坐标 ≡ 机械坐标」。
        固件 EEPROM 里持久化的 G54 偏移、残留或手工设置的 G92 会让每个绝对
        坐标整体偏移——笔头
        跑到错误点位，而校准读的是 MPos（机械坐标，不受 G92 影响），重新
        校准也救不回来，只能表现为「画布正常、写出来整体错位」。任何绝对
        移动前都先清一次；手工 G92 对本软件的作业模型
        没有合法用途，不例外。
        """
        return [grbl.clear_work_offset_command(), grbl.clear_origin_command()]

    # -- 作业发送（带字符计数流控） -----------------------------------------
    def load_job(self, lines: list[str]) -> None:
        """装载 G-code 作业并开始发送（过滤空行与纯注释行）。

        发送前注入清零指令（见 :meth:`zero_offset_lines`），避免固件里
        残留的 G54/G92 偏移让烘焙绝对坐标的作业整体错位。
        """
        cleaned = []
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("("):
                continue
            cleaned.append(s)
        self._job_lines_cache = cleaned
        self._job_home = _first_positioning(cleaned)
        with self._lock:
            self._injected.clear()
            self._injected.extend(
                (_INJ_OFFSET, ln) for ln in self.zero_offset_lines()
            )
            self._job_lines = len(cleaned)
            self._job_total = 0
            self._job_acked = 0
            self._pending.clear()
            self._finish_announced = False
            self._all_acked_at = 0.0
            self._paused = False
            self._tracker.reset()
            self._pause_snap = None
            self._resume_pending_snap = None
            self._pause_lift_sent = False
            self._resume_move_needed = False
            self._pen_cfg = None
        self._emit("line", f"已装载作业：{len(cleaned)} 行")
        self._pump()

    def _pump(self) -> None:
        """在缓冲允许范围内发送队列中的行。

        暂停时**注入行**（抬笔等）仍会按流控继续发送：
        暂停 = 停止补充作业行 + 把抬笔指令排在在途缓冲之后执行。

        「选行 → 记账 → 写串口」整体持锁：读线程与界面线程都会调用 _pump，
        不持锁的话两边可能交错「记账」与「写出」，导致机器收到的时间序与
        _pending 记录的顺序不一致，流控映射错乱。
        """
        if self._serial is None:
            return
        if self.state is State.ALARM:
            return
        while True:
            line: Optional[str] = None
            with self._lock:
                # 注入行优先（不受暂停限制）
                if self._injected:
                    if self.single_step and self._pending:
                        return
                    kind, line = self._injected[0]
                    need = len(line) + 1
                    if self._in_flight() + need > RX_BUFFER_SIZE:
                        return
                    self._injected.popleft()
                    self._pending.append((need, True))
                    if kind == _INJ_PAUSE:
                        self._pause_lift_sent = True
                    elif kind == _INJ_RESUME_DOWN:
                        self._pause_lift_sent = False
                    elif kind == _INJ_RESUME_MOVE:
                        if not any(k == _INJ_RESUME_MOVE
                                   for k, _ in self._injected):
                            self._resume_move_needed = False
                else:
                    if self._paused:
                        return
                    if self._job_total >= self._job_lines:
                        if (not self._finish_announced
                                and self._job_lines > 0
                                and self._job_acked >= self._job_lines
                                and not self._pending
                                and self.state is not State.ALARM):
                            # ack 完 ≠ 机械停了：GRBL 收到行就回 ok，最后几笔
                            # 可能还在走。等状态离开 Run/Hold 再宣布完成，
                            # 否则「完成后自动休眠」会打断收尾动作；最多等 30s。
                            # 状态未知（尚未收到首个状态串）也先等。
                            st = self.status
                            if (st is None
                                    or st.state in ("Run", "Hold", "Jog")):
                                now = time.monotonic()
                                if not self._all_acked_at:
                                    self._all_acked_at = now
                                if now - self._all_acked_at < 30.0:
                                    return
                            self._finish_announced = True
                            self._all_acked_at = 0.0
                            self._resume_pending_snap = None
                            self._pause_lift_sent = False
                            self._resume_move_needed = False
                            self._set_state(State.IDLE)
                            self._emit("line", "作业发送完成")
                            self._emit("job_done", "")
                        return
                    # 单步模式：在途行全部确认后才发下一行
                    if self.single_step and self._pending:
                        return
                    line = self._get_job_line(self._job_total)
                    if line is None:
                        return
                    need = len(line) + 1
                    if self._in_flight() + need > RX_BUFFER_SIZE:
                        return
                    self._pending.append((need, False))
                    self._job_total += 1
                    self._tracker.feed(line)
                    self._pause_lift_sent = False
                    self._resume_move_needed = False
                    self._resume_pending_snap = None
                    self._set_state(State.RUNNING)
                try:
                    self._write_raw((line + "\n").encode("ascii", "replace"))
                except Exception as exc:
                    self._emit("line", f"发送失败：{exc}")
                    self.disconnect()
                    return
            self._emit("sent", line)

    def _in_flight(self) -> int:
        return sum(size for size, _ in self._pending)

    def _get_job_line(self, index: int) -> Optional[str]:
        cache = self._job_lines_cache
        if 0 <= index < len(cache):
            return cache[index]
        return None

    def load_job_lines(self, lines: list[str]) -> None:
        """装载并缓存作业（推荐入口）。"""
        cleaned = [ln.strip() for ln in lines
                   if ln.strip() and not ln.strip().startswith("(")]
        self._job_lines_cache = cleaned
        self.load_job(cleaned)

    # ------------------------------------------------------------ 控制
    def _drop_injected_kinds_locked(self, kinds: set[str]) -> list[tuple[str, str]]:
        """在持锁状态下移除指定来源标签的尚未发出注入行，并返回被移除列表。"""
        kept: deque[tuple[str, str]] = deque()
        removed: list[tuple[str, str]] = []
        for item in self._injected:
            if item[0] in kinds:
                removed.append(item)
            else:
                kept.append(item)
        self._injected = kept
        return removed

    def pause(self, pen_cfg=None, use_hold: bool = True) -> None:
        """暂停作业，记录精确断点位置与笔状态，供后续自由恢复继续。

        ``pen_cfg`` 传入笔控配置时，停止补充后续作业行，并在必要时把**抬笔指令**
        排到在途缓冲之后执行——笔尖离开纸面，避免暂停期间墨水洇纸；
        否则仅发送实时进给保持 ``!``。

        空闲/未连接时是**误操作**：直接忽略（否则继续时会把落笔指令
        注入一台没在写字的机器，激光模式等于点亮光束且状态卡在运行中）。
        """
        used_hold = False
        need_pump = False
        with self._lock:
            if self._serial is None or self._paused:
                return
            job_active = (self._job_lines > 0 and not self._finish_announced)
            if self.state is not State.RUNNING and not (
                    job_active and self.state is not State.ALARM):
                return
            self._paused = True
            self._all_acked_at = 0.0

            # 极速 resume -> pause：先撤销尚未发出的旧恢复指令，避免落笔滞后发出
            self._drop_injected_kinds_locked(
                {_INJ_RESUME_MOVE, _INJ_RESUME_DOWN, _INJ_RESUME_MODAL}
            )
            prev_snap = self._resume_pending_snap
            re_paused_before_job = (
                prev_snap is not None
                and self._job_total == prev_snap.line_index
            )
            self._resume_pending_snap = None

            cfg_copy = (pen_cfg.clone()
                        if (pen_cfg is not None and hasattr(pen_cfg, "clone"))
                        else pen_cfg)

            if re_paused_before_job and prev_snap is not None:
                # 刚恢复但一行新作业还没发出就再次暂停：完整保留原始断点与笔状态
                snap_xy = prev_snap.xy
                snap_z = prev_snap.z
                snap_abs = prev_snap.abs_mode
                snap_feed = prev_snap.feed
                pen_down = prev_snap.pen_down
                moved_flag = bool(self._resume_move_needed)
                if pen_cfg is not None:
                    self._pen_cfg = cfg_copy
                    # 若上次恢复已经把笔落回纸上（或笔未抬起），再次注入抬笔
                    if not self._pause_lift_sent and pen_down:
                        for ln in pen_cfg.pen_up_lines():
                            self._injected.append((_INJ_PAUSE, ln))
                    need_pump = True
                elif use_hold:
                    used_hold = True
            else:
                sent_lines = self._job_lines_cache[:self._job_total]
                snap_xy = self._tracker.xy
                snap_z = self._tracker.z
                snap_abs = self._tracker.abs_mode
                snap_feed = self._tracker.draw_feed or self._tracker.last_feed
                explicit_pen = (
                    self._tracker.pen_state(pen_cfg, sent_lines)
                    if pen_cfg is not None else None
                )
                pen_down = (
                    self._tracker.is_pen_down(pen_cfg, sent_lines)
                    if pen_cfg is not None else False
                )
                moved_flag = False
                self._pause_lift_sent = False
                self._resume_move_needed = False
                if pen_cfg is not None:
                    self._pen_cfg = cfg_copy
                    # 除非已明确处于抬笔状态（如刚抬笔或在空程 G0 处），一律抬笔确保安全
                    if explicit_pen is not False:
                        for ln in pen_cfg.pen_up_lines():
                            self._injected.append((_INJ_PAUSE, ln))
                    need_pump = True
                elif use_hold:
                    used_hold = True

            self._pause_snap = _PauseSnapshot(
                line_index=self._job_total,
                xy=snap_xy,
                z=snap_z,
                abs_mode=snap_abs,
                pen_down=pen_down,
                feed=snap_feed,
                pen_cfg=cfg_copy,
                used_hold=used_hold,
                moved_while_paused=moved_flag,
            )
        if need_pump:
            self._pump()
        elif used_hold:
            self.realtime(grbl.CMD_HOLD)
        self._set_state(State.PAUSED)

    def resume(self, pen_cfg=None) -> None:
        """恢复作业：从最后暂停的位置与笔状态无缝继续。

        * 若暂停期间手动移动过笔头（Jog/Goto/Home），自动先确保抬笔、清偏移、
          快速精准移回最后暂停坐标；
        * 仅当暂停前笔处于落笔态时才落笔（精确还原斜坡 Z 高度，也支持暂停期间
          微调落笔深度）；空程暂停绝不误落笔划纸；
        * 若 Z 抬落笔改写了模态进给率 ``F`` 且下一行未显式带 ``F``，自动恢复原速度。
        """
        if self._serial is None:
            return
        with self._lock:
            if not self._paused:
                # 外部保持（Hold/Door）：软件没主动暂停过，仍允许发 ~ 恢复运行
                name = ""
                if self.status is not None:
                    name = (self.status.state or "").strip().lower().split(":")[0]
                if name in ("hold", "door"):
                    self.realtime(grbl.CMD_RESUME)
                return

            snap = self._pause_snap
            self._pause_snap = None
            cfg = (pen_cfg if pen_cfg is not None
                   else (self._pen_cfg or (snap.pen_cfg if snap else None)))
            self._pen_cfg = None

            # 清理尚未发出的暂停抬笔行
            removed_pause = self._drop_injected_kinds_locked({_INJ_PAUSE})
            has_remaining = (self._job_total < self._job_lines)

            # 情况 1：刚点暂停、抬笔一行都未发出且未移动笔头 → 直接无痕取消暂停
            no_lift_and_still = (
                bool(removed_pause)
                and not self._pause_lift_sent
                and (snap is None or not snap.moved_while_paused)
            )

            resume_items: list[tuple[str, str]] = []
            if not no_lift_and_still and cfg is not None and snap is not None and has_remaining:
                moved = snap.moved_while_paused
                # 1) 暂停期间挪动过笔头：确保抬笔 → 清偏移 → 精准回到最后暂停的 XY
                if moved:
                    for ln in cfg.pen_up_lines():
                        resume_items.append((_INJ_RESUME_MOVE, ln))
                    if snap.xy is not None:
                        for ln in self.zero_offset_lines():
                            resume_items.append((_INJ_RESUME_MOVE, ln))
                        resume_items.append(
                            (_INJ_RESUME_MOVE,
                             f"G90 G0 {cfg.fmt_xy(snap.xy[0], snap.xy[1])}")
                        )
                    self._resume_move_needed = True

                # 2) 仅在暂停前笔处于落笔态时才落笔（空程暂停绝不误落笔划纸）
                need_pen_down = snap.pen_down and (self._pause_lift_sent or moved)
                if need_pen_down:
                    to_z: Optional[float] = None
                    if (getattr(cfg, "pen_mode", None) == PEN_Z
                            and snap.z is not None):
                        old_cfg = snap.pen_cfg
                        # 若暂停在入/收笔斜坡过渡高度，精准落回该高度；
                        # 若暂停在满压深度，使用当前配置的 pen_down_z（支持暂停中微调压深）
                        if (old_cfg is not None
                                and getattr(old_cfg, "pen_mode", None) == PEN_Z
                                and abs(snap.z - float(old_cfg.pen_down_z)) > 1e-3):
                            to_z = snap.z
                    for ln in cfg.pen_down_lines(to_z=to_z):
                        resume_items.append((_INJ_RESUME_DOWN, ln))

                # 3) 恢复坐标模式（若原作业是 G91 且回位用了 G90）
                if moved and snap.xy is not None and not snap.abs_mode:
                    resume_items.append((_INJ_RESUME_MODAL, "G91"))

                # 4) 恢复被 Z 抬落笔覆盖的模态进给速度 F（下一行未显式带 F 时）
                if (getattr(cfg, "pen_mode", None) == PEN_Z
                        and snap.feed is not None and snap.feed > 0
                        and abs(snap.feed - float(cfg.pen_z_feed)) > 1e-3
                        and (self._pause_lift_sent or need_pen_down or moved)):
                    next_line = self._get_job_line(self._job_total)
                    if next_line and not _line_has_word(next_line, "F"):
                        resume_items.append(
                            (_INJ_RESUME_MODAL, f"F{cfg._ff(snap.feed)}")
                        )

            if resume_items:
                self._injected.extend(resume_items)
                self._resume_pending_snap = snap
            else:
                self._resume_pending_snap = None
                if not has_remaining:
                    self._pause_lift_sent = False
                    self._resume_move_needed = False

            self._paused = False

        self.realtime(grbl.CMD_RESUME)
        self._set_state(State.RUNNING)
        self._pump()

    def abort(self, pen_cfg=None, return_start: bool = False,
              feed: float = 3000.0) -> None:
        """中止作业：软复位清空控制器缓冲，再抬笔（可选回起点）。

        先发实时软复位 ``\\x18`` 清空缓冲与动作，
        再补抬笔；``return_start`` 为 True 时回到**本次作业的书写起点**。
        """
        with self._lock:
            self._job_total = self._job_lines  # 丢弃剩余
            self._finish_announced = True
            self._pending.clear()
            self._injected.clear()
            self._paused = False
            self._pause_snap = None
            self._resume_pending_snap = None
            self._pause_lift_sent = False
            self._resume_move_needed = False
            self._tracker.reset()
            self._pen_cfg = None
        self.realtime(grbl.CMD_SOFT_RESET)
        self._set_state(State.IDLE)
        if pen_cfg is not None:
            for ln in pen_cfg.pen_up_lines():
                self.send_line(ln)
        if return_start:
            # 软复位已从 EEPROM 还原 G54/G92，回起点（绝对坐标）前须再清一次
            for ln in self.zero_offset_lines():
                self.send_line(ln)
            hx, hy = self._job_home or (0.0, 0.0)
            self.send_line(grbl.return_to_start_command(feed, hx, hy))
        self._job_home = None
        self._emit("line", "已中止")

    def finish(self, pen_cfg=None, return_start: bool = False,
               sleep_after: bool = False, feed: float = 3000.0) -> None:
        """作业完成后收尾：抬笔 → 回起点 → （可选）休眠。

        对应「完成后自动回零」「完成后自动休眠」开关。
        """
        if pen_cfg is not None:
            for ln in pen_cfg.pen_up_lines():
                self.send_line(ln)
        if return_start:
            for ln in self.zero_offset_lines():
                self.send_line(ln)
            hx, hy = self._job_home or (0.0, 0.0)
            self.send_line(grbl.return_to_start_command(feed, hx, hy))
        if sleep_after:
            # 给回起点动作留出执行时间后再休眠
            self.send_line(grbl.sleep_command())
        self._job_home = None
        self._emit("job_done", "")

    def stop_jog(self) -> None:
        """取消进行中的 Jog（实时 ``0x85``）。"""
        self.realtime(grbl.CMD_JOG_CANCEL)

    def request_status(self) -> None:
        self.realtime(grbl.CMD_STATUS)

    def _mark_moved_while_paused(self) -> None:
        """暂停期间发生手动移动/笔控操作时标记：恢复继续前自动抬笔回最后暂停点。"""
        with self._lock:
            if self._paused and self._pause_snap is not None:
                self._pause_snap.moved_while_paused = True

    def jog(self, dx: float = 0.0, dy: float = 0.0, feed: float = 1000.0) -> None:
        # 版本感知：grbl <1.1 不支持 $J=
        self._mark_moved_while_paused()
        self.send_line(grbl.jog_command(dx, dy, feed=feed,
                                        grbl_version=self.grbl_version))

    def goto(self, x: float, y: float, feed: float = 3000.0) -> None:
        """抬笔快速定位到绝对坐标（G90 G0）。

        发送前先清固件里的工件偏移：绝对坐标只有在「工件坐标 ≡ 机械坐标」
        时才等于用户设定的机器点位，否则会整体偏移到错误位置。
        """
        self._mark_moved_while_paused()
        for ln in self.zero_offset_lines():
            self.send_line(ln)
        self.send_line(grbl.goto_command(x, y, feed=feed))

    def pen_down(self, pen_cfg, to_z: Optional[float] = None) -> list[str]:
        """立即执行一次单次落笔测试（不经过作业流控队列），返回发出的指令行。"""
        self._mark_moved_while_paused()
        lines = list(pen_cfg.pen_test_down_lines(to_z=to_z))
        for ln in lines:
            self.send_line(ln)
        return lines

    def pen_up(self, pen_cfg) -> list[str]:
        """立即执行一次单次抬笔（不经过作业流控队列），返回发出的指令行。"""
        lines = list(pen_cfg.pen_test_up_lines())
        for ln in lines:
            self.send_line(ln)
        return lines

    def home(self) -> None:
        self._mark_moved_while_paused()
        self.send_line(grbl.home_command())

    def unlock(self) -> None:
        self.send_line(grbl.unlock_command())
        if self.state is State.ALARM:
            with self._lock:
                if self._job_total >= self._job_lines:
                    self._finish_announced = True
            self._set_state(State.IDLE)

    def set_origin(self, axes: str = "XY") -> None:
        """手工设工件零点（``G92 X0 Y0``）。

        仅用于互操作/调试：本软件的作业烘焙绝对坐标且发送前**无条件**清
        工件偏移（:meth:`zero_offset_lines`），手工零点不影响书写。
        """
        self.send_line(grbl.set_origin_command(axes))

    def set_start_point(self, offset_x: float = 0.0, offset_y: float = 0.0) -> None:
        """设置写字起点：``G92 X<ox> Y<oy> Z0``。

        同 :meth:`set_origin`：协议入口保留，作业发送前一律清掉。
        """
        self.send_line(grbl.set_origin_offset_command(offset_x, offset_y))

    def sleep_now(self) -> None:
        self.send_line(grbl.sleep_command())

    def query_settings(self) -> None:
        self.send_line(grbl.CMD_SETTINGS)

    def query_info(self) -> None:
        self.send_line(grbl.CMD_BUILD_INFO)

    def _maybe_poll(self) -> None:
        now = time.monotonic()
        if self._serial is not None and now - self._last_poll >= STATUS_POLL_INTERVAL:
            self._last_poll = now
            try:
                self.realtime(grbl.CMD_STATUS)
            except Exception:
                pass
        # 状态看门狗：连续 N 次轮询无响应则报 stale：
        # 这里仅发事件，由界面决定提示/重连。
        if (self._serial is not None and self._last_status_time > 0
                and now - self._last_status_time >= self.stale_seconds
                and not self._stale_emitted):
            self._stale_emitted = True
            self._emit("stale", f"{self.stale_seconds:g} 秒未收到状态报告")

    # ------------------------------------------------------------ 事件
    def _set_state(self, st: State) -> None:
        if st is not self.state:
            self.state = st
            self._emit("state", st.value)

    def _emit(self, kind: str, text: str = "",
              status: Optional[grbl.MachineStatus] = None,
              profile: Optional[MachineProfile] = None) -> None:
        try:
            self._on_event(LinkEvent(kind=kind, text=text, status=status,
                                     profile=profile))
        except Exception:
            pass

    def set_event_handler(self, handler: Callable[[LinkEvent], None]) -> None:
        self._on_event = handler

    # -- 进度 -----------------------------------------------------------
    def progress(self) -> tuple[int, int]:
        with self._lock:
            return (self._job_acked, self._job_lines)

    def in_flight_bytes(self) -> int:
        with self._lock:
            return self._in_flight()


# ---------------------------------------------------------------------------
# 测试用假串口：模拟 GRBL 行为
# ---------------------------------------------------------------------------
def _first_positioning(lines: list[str]) -> Optional[tuple[float, float]]:
    """取作业里第一条 ``G0 X.. Y..`` 定位行作为书写起点（供中止后回归）。"""
    for ln in lines:
        words = ln.split()
        if not words or words[0] not in ("G0", "G00"):
            continue
        x = y = None
        for w in words[1:]:
            if w[:1] == "X" and x is None:
                try:
                    x = float(w[1:])
                except ValueError:
                    pass
            elif w[:1] == "Y" and y is None:
                try:
                    y = float(w[1:])
                except ValueError:
                    pass
        if x is not None and y is not None:
            return (x, y)
    return None


class FakeSerial:
    """内存串口，模拟 GRBL：每行命令回 ``ok``，``?`` 回状态串。

    * ``responses``：按行内容注入自定义响应（如 ``{"G1 ...": "error:9"}``）
    * ``out_lines``：记录已写入的完整行
    * :meth:`feed` 注入下行数据
    """

    def __init__(self, responses: Optional[dict[str, str]] = None,
                 initial_pos: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 simulate_full: bool = False) -> None:
        self.responses = responses or {}
        self.out_lines: list[str] = []
        self._in = bytearray()
        self.pos = list(initial_pos)
        self.state = "Idle"
        self.simulate_full = simulate_full      # 模拟缓冲满（不回 ok）
        self.closed = False
        self._partial = ""

    def write(self, data: bytes) -> int:
        text = data.decode("ascii", "replace")
        # 拆分实时字符与普通行
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\n":
                line, self._partial = self._partial, ""
                if line.strip():
                    self._on_command(line.strip())
            elif ch in "?~!\x18\x85\x84":
                self._on_realtime(ch)
            else:
                self._partial += ch
            i += 1
        return len(data)

    def _on_realtime(self, ch: str) -> None:
        if ch == "?":
            self._in.extend(self._status_bytes())

    def _on_command(self, line: str) -> None:
        self.out_lines.append(line)
        if self.simulate_full:
            return
        if line in self.responses:
            self._in.extend((self.responses[line] + "\n").encode())
            return
        self._in.extend(b"ok\n")

    def _status_bytes(self) -> bytes:
        p = self.pos
        return (f"<{self.state}|MPos:{p[0]:.3f},{p[1]:.3f},{p[2]:.3f}"
                f"|FS:0,0>").encode()

    def read(self, size: int = 1) -> bytes:
        out = bytes(self._in[:size])
        self._in = self._in[size:]
        return out

    @property
    def in_waiting(self) -> int:
        return len(self._in)

    def close(self) -> None:
        self.closed = True

    def feed(self, data: str) -> None:
        self._in.extend(data.encode())
