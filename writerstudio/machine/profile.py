"""机器能力档案：从 GRBL ``$$``/``$I`` 应答解析出的固件参数快照。

不同写字机的固件能力差异很大（加速度、轴数、Z 速率上限、甚至有无 Z 轴），
任何按「本机标定」调出来的优化（进给建议、收笔斜抬的 Z 斜率上限、耗时
估算）都不能硬编码。这里把连接时探测到的参数存成一份档案，供：

    * ``gcode_gen`` —— 加速度感知的耗时估算、进给可达性判断；
    * 机器面板 —— 显示固件信息与设置建议（只建议、不代改 EEPROM）；
    * 收笔斜抬 —— 判断机器是否具备 Z 轴（纯舵机/激光机器不生成 Z 词）。

GRBL 设置编号（1.1，按轴序 X/Y/Z/A/B... 扩展）：
    ``$100+`` 步进/mm、``$110+`` 最大速率 mm/min、``$120+`` 加速度 mm/s²、
    ``$130+`` 最大行程 mm；``$11`` junction deviation。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def motion_time(length_mm: float, feed_mm_min: float, accel_mm: float) -> float:
    """一段长 ``length_mm`` 的运动在最短时间（梯形/三角形速度剖面，秒）。

    加速度未知（``accel_mm <= 0``）时退化为 ``length / feed``——即认为
    机器瞬间达速，这是旧版估算的行为。加速度已知时：行程不足以达到满速
    （``L < v²/a``）按三角形剖面 ``2·sqrt(L/a)``,否则按梯形剖面
    ``L/v + v/a``。短笔画占多数的书写作业里，这个模型才接近真实耗时。
    """
    if length_mm <= 0:
        return 0.0
    v = feed_mm_min / 60.0
    if v <= 0:
        return 0.0
    if accel_mm <= 0:
        return length_mm / v
    critical = v * v / accel_mm       # 加速到满速再减速回零所需的总长
    if length_mm >= critical:
        return length_mm / v + v / accel_mm
    return 2.0 * math.sqrt(length_mm / accel_mm)


def peak_speed(length_mm: float, accel_mm: float, feed_mm_min: float) -> float:
    """长 ``length_mm`` 的一段运动实际可达的峰速（mm/min）。

    受加速度限制 ``v_peak = sqrt(a·L)``（三角剖面）；满进给达不到时取
    两者较小值。用于回答「设 10100 mm/min 到底能不能跑出来」。
    """
    if length_mm <= 0 or accel_mm <= 0:
        return 0.0
    v = feed_mm_min / 60.0
    reachable = math.sqrt(accel_mm * length_mm) * 60.0
    return min(feed_mm_min, reachable) if v > 0 else reachable


def suggested_feed(stroke_lengths: list[float], accel_mm: float,
                   current_feed: float) -> float:
    """按笔画长度分布给出「实际可达」的书写速度建议（mm/min）。

    取**中位笔画长度**的可达峰速——一半笔画比它短，设得再高也跑不出；
    建议值同时不超过当前设置（只降不升）。无加速度数据、没有笔画、或
    当前设置本就可达（建议 == 设置）时返回 0（无建议，避免界面噪音）。
    """
    if not stroke_lengths or accel_mm <= 0 or current_feed <= 0:
        return 0.0
    lens = sorted(stroke_lengths)
    median = lens[len(lens) // 2]
    reachable = peak_speed(median, accel_mm, current_feed)
    if reachable >= current_feed * 0.95:
        return 0.0
    return max(0.0, reachable)


@dataclass
class MachineProfile:
    """一台机器的固件参数快照（轴序 = GRBL 轴序 X/Y/Z/A/B...）。"""

    version: str = ""                              # $I [VER:...] 原文
    grbl_version: float | None = None              # 1.1 / 0.9
    steps_per_mm: list[float] = field(default_factory=list)   # $100+
    max_rate: list[float] = field(default_factory=list)       # $110+ mm/min
    accel: list[float] = field(default_factory=list)          # $120+ mm/s²
    max_travel: list[float] = field(default_factory=list)     # $130+ mm
    junction_deviation: float = 0.0                # $11

    # ------------------------------------------------------------ 轴访问
    def _axis(self, values: list[float], idx: int) -> float:
        return values[idx] if 0 <= idx < len(values) else 0.0

    @property
    def n_axes(self) -> int:
        """报告了最大速率的轴数（0 视为未报告）。"""
        return sum(1 for v in self.max_rate if v > 0)

    @property
    def has_z(self) -> bool:
        """是否具备可控 Z 轴（决定收笔斜抬能否生成 Z 词）。"""
        return self.n_axes >= 3 and self._axis(self.max_rate, 2) > 0

    @property
    def accel_xy(self) -> float:
        """XY 加速度取两轴较小值（保守），未报告返回 0。"""
        ax, ay = self._axis(self.accel, 0), self._axis(self.accel, 1)
        if ax > 0 and ay > 0:
            return min(ax, ay)
        return max(ax, ay)

    @property
    def max_rate_xy(self) -> float:
        ax, ay = self._axis(self.max_rate, 0), self._axis(self.max_rate, 1)
        if ax > 0 and ay > 0:
            return min(ax, ay)
        return max(ax, ay)

    @property
    def z_max_rate(self) -> float:
        return self._axis(self.max_rate, 2)

    @property
    def z_accel(self) -> float:
        return self._axis(self.accel, 2)

    # ------------------------------------------------------------ 派生
    def suggested_draw_feed(self, stroke_lengths: list[float],
                            current_feed: float) -> float:
        """:func:`suggested_feed` 的档案方法版（用本机加速度）。"""
        return suggested_feed(stroke_lengths, self.accel_xy, current_feed)

    def summary(self) -> str:
        """一行人话描述（面板固件信息标签用）。"""
        name = self.version.strip().strip(":") or (
            f"GRBL {self.grbl_version:g}" if self.grbl_version else "未知固件")
        if self.n_axes == 0:
            return f"固件：{name}（未读到轴参数）"
        parts = [f"固件：{name}", f"{self.n_axes} 轴"]
        if self.accel_xy > 0:
            parts.append(f"XY 加速度 {self.accel_xy:g} mm/s²")
        if self.max_rate_xy > 0:
            parts.append(f"XY 速率上限 {self.max_rate_xy:g} mm/min")
        if self.has_z and self.z_max_rate > 0:
            parts.append(f"Z 上限 {self.z_max_rate:g} mm/min")
        return " · ".join(parts)

    # ------------------------------------------------------------ 序列化
    def to_data(self) -> dict:
        return {
            "version": self.version,
            "grbl_version": self.grbl_version,
            "steps_per_mm": list(self.steps_per_mm),
            "max_rate": list(self.max_rate),
            "accel": list(self.accel),
            "max_travel": list(self.max_travel),
            "junction_deviation": self.junction_deviation,
        }

    @classmethod
    def from_data(cls, data: dict | None) -> "MachineProfile":
        if not data:
            return cls()
        d = cls()
        d.version = str(data.get("version", ""))
        gv = data.get("grbl_version")
        d.grbl_version = float(gv) if gv else None
        d.steps_per_mm = [float(v) for v in data.get("steps_per_mm", [])]
        d.max_rate = [float(v) for v in data.get("max_rate", [])]
        d.accel = [float(v) for v in data.get("accel", [])]
        d.max_travel = [float(v) for v in data.get("max_travel", [])]
        d.junction_deviation = float(data.get("junction_deviation", 0.0) or 0.0)
        return d

    @classmethod
    def from_settings(cls, settings: dict, version: str = "",
                      grbl_version: float | None = None) -> "MachineProfile":
        """从 ``grbl.parse_settings`` 的 ``{编号: 值}`` 构建档案。

        缺哪个键就空着哪项——能力判断一律按「未报告 = 不具备」，调用方
        据此降级，绝不拿默认值冒充探测结果。
        """

        def num(code: int) -> float:
            try:
                return float(settings.get(code))
            except (TypeError, ValueError):
                return 0.0

        def axis_list(base: int, count: int = 5) -> list[float]:
            return [num(base + i) for i in range(count)]

        p = cls(version=version, grbl_version=grbl_version)
        p.steps_per_mm = axis_list(100)
        p.max_rate = axis_list(110)
        p.accel = axis_list(120)
        p.max_travel = axis_list(130)
        p.junction_deviation = num(11)
        return p
