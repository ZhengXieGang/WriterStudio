"""G-code 生成配置。

抬落笔方式按驱动形式区分（覆盖常见写字机的三类笔控硬件，
同类软件的参数命名与之基本一致，便于按同一套思路调机：

    * 步进 Z 轴（本软件 ``z``）        —— 步进 Z 轴升降，参数
      落笔 Z(mm)/抬笔 Z(mm)/Z 速度(mm/min)
    * 舵机角度（本软件 ``servo-angle``）—— 舵机角度，参数
      落笔角度/抬笔角度（``M3 S<角度>``）
    * 激光（本软件 ``laser``）         —— 激光，参数
      ``laserS``(功率)/``laserPreviewS``(预览功率)
    * ``Custom`` （本软件 ``custom``） —— ``customOnCode``/``customOffCode``

另有两种主轴式输出（为兼容不同机型保留）：
    * ``m3m5``   —— ``M3 S<down>`` / ``M5``（Grbl_ESP32 常开主轴笔）
    * ``m7m9``   —— ``M7`` / ``M9``（冷却输出）

坐标约定：文档为 mm、Y 轴向上，与 GRBL 机械坐标一致，因此**无需 Y 翻转**；
实际机器坐标 = 文档坐标 + ``origin_offset``（写字起点）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 抬落笔方式（按驱动形式分类）
PEN_Z = "z"                       # 步进 Z 轴：抬/落笔各走一段受控 Z 进给
PEN_SERVO_ANGLE = "servo-angle"   # 舵机角度：抬/落笔各发 M3 S<角度>
PEN_LASER = "laser"               # 激光：M3 S<功率> 出光 / M5 关光
PEN_CUSTOM = "custom"             # 自定义：用户填抬/落笔指令
PEN_M3M5 = "m3m5"                 # 兼容：M3 S<落笔S>/M5
PEN_M7M9 = "m7m9"                 # 兼容：M7/M9（冷却输出）

PEN_MODES = {
    PEN_Z: "步进 Z 轴（Stepper）",
    PEN_SERVO_ANGLE: "舵机角度（Servo）",
    PEN_LASER: "激光（Laser）",
    PEN_CUSTOM: "自定义指令（Custom）",
    PEN_M3M5: "主轴 M3/M5（S 值）",
    PEN_M7M9: "冷却 M7/M9",
}

# 抬笔延迟（秒）—— 部分机械笔需要等待动作完成
DEFAULT_PEN_DELAY = 0.0


# 坐标系设置（对调 XY / 反转 X / 反转 Y / 原点位置）
# 「坐标系位置」= 内容哪个角对齐机器原点；选项顺序沿用常见固件的 0-3 编号。
ORIGIN_NONE = "none"                # 不用角落对齐（手动起点偏移）
ORIGIN_LEFT_TOP = "left-top"        # 编号 0
ORIGIN_RIGHT_TOP = "right-top"      # 编号 1
ORIGIN_LEFT_BOTTOM = "left-bottom"  # 编号 2
ORIGIN_RIGHT_BOTTOM = "right-bottom"  # 编号 3

ORIGIN_CORNERS = {
    ORIGIN_LEFT_TOP: "左上角 = 原点",
    ORIGIN_RIGHT_TOP: "右上角 = 原点",
    ORIGIN_LEFT_BOTTOM: "左下角 = 原点",
    ORIGIN_RIGHT_BOTTOM: "右下角 = 原点",
    ORIGIN_NONE: "不用（手动起点）",
}

# 「原点位置」的两种常见写法（数字编号与 "LeftUp" 一类英文键名）→ 本软件角落键。
AXE_POS_TO_CORNER = {
    0: ORIGIN_LEFT_TOP, 1: ORIGIN_RIGHT_TOP,
    2: ORIGIN_LEFT_BOTTOM, 3: ORIGIN_RIGHT_BOTTOM,
    "0": ORIGIN_LEFT_TOP, "1": ORIGIN_RIGHT_TOP,
    "2": ORIGIN_LEFT_BOTTOM, "3": ORIGIN_RIGHT_BOTTOM,
    "leftup": ORIGIN_LEFT_TOP, "rightup": ORIGIN_RIGHT_TOP,
    "leftdown": ORIGIN_LEFT_BOTTOM, "rightdown": ORIGIN_RIGHT_BOTTOM,
}
CORNER_TO_AXE_POS = {
    ORIGIN_LEFT_TOP: 0, ORIGIN_RIGHT_TOP: 1,
    ORIGIN_LEFT_BOTTOM: 2, ORIGIN_RIGHT_BOTTOM: 3,
}


def _close(a: tuple[float, float], b: tuple[float, float],
           tol: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _neg(v: float) -> float:
    """取反并把 −0.0 归一为 0.0（避免 G-code 输出 ``X-0``）。"""
    return 0.0 if v == 0 else -v


def page_to_machine(p: tuple[float, float], page_h: float,
                    swap_xy: bool = False, invert_x: bool = False,
                    invert_y: bool = False) -> tuple[float, float]:
    """书写坐标映射：机械原点 = 纸张左上角 + 可选轴方向修正。

    基础映射（默认参数）＝ ``(纸x, 纸高 − 纸y)``——X 沿顶边向右、Y 沿左边
    向下，与写字机从左上角原点出发的行程方向一致；不受「起点偏移」影响，
    也不随纸张旋转变化——内容在页面上的摆法就是书写方向。

    ``swap_xy``/``invert_x``/``invert_y`` 是**轴方向修正**（适配机器原点
    朝向/导轨方向与默认不同的机型）：先做基础映射，再依次对调 X/Y、
    取反 X、取反 Y。修正不改原点——机械零点仍对应纸张左上角，只是轴的
    指向改变。逆变换用 :func:`machine_to_page`（对调与取反不可交换，
    无修正时两函数相同）。
    """
    x, y = (p[0], page_h - p[1])
    if swap_xy:
        x, y = y, x
    if invert_x:
        x = _neg(x)
    if invert_y:
        y = _neg(y)
    return (x, y)


def machine_to_page(m: tuple[float, float], page_h: float,
                    swap_xy: bool = False, invert_x: bool = False,
                    invert_y: bool = False) -> tuple[float, float]:
    """:func:`page_to_machine` 的逆映射（机器坐标 → 页面坐标）。

    按**逆序**撤销正向步骤：先取反 Y、再取反 X、再对调 X/Y，最后做
    基础映射的逆——正向「对调后再取反」不等于「取反后再对调」，不能
    用同一顺序撤销。无轴修正时与正向相同（自逆）。
    机械零点恒对应纸张左上角 ``(0, 纸高)``。
    """
    x, y = m
    if invert_y:
        y = _neg(y)
    if invert_x:
        x = _neg(x)
    if swap_xy:
        x, y = y, x
    return (x, page_h - y)


@dataclass
class GCodeConfig:
    """G-code 输出配置。"""

    pen_mode: str = PEN_Z
    # 抬/落笔 Z 默认值按弹簧回位笔架的常见标定（落笔 6 / 抬笔 0）。
    # 出厂默认即按此标定，换机器时在面板改一次即可（会被持久化）。
    pen_up_z: float = 0.0            # 抬笔 Z（弹簧回位笔架 = 释放位 0）
    pen_down_z: float = 6.0          # 落笔 Z（正向下压触纸）
    pen_down_s: int = 1000           # M3/M7 落笔 S 值
    pen_up_s: int = 0                # 舵机角度模式：抬笔角度
    pen_down_angle: int = 0          # 舵机角度模式：落笔角度
    laser_power: int = 1000          # 激光功率（对应 $30 最大功率的 S 值）
    laser_preview_power: int = 50    # 激光预览/对位低功率
    pen_up_command: str = ""         # 自定义模式：抬笔指令
    pen_down_command: str = ""       # 自定义模式：落笔指令

    # 落笔后/抬笔前的等待（秒）——防墨水未干/笔未落下。
    # 保持 0 为默认：每笔都插入 G4 会明显拖长作业时间，
    # 需要时由用户在面板按机器笔速自行设置（留 0 即不加等待）。
    pen_on_delay: float = 0.0
    pen_off_delay: float = 0.0

    # 绘制/空程/Z 进给（mm/min）。P40 依据实测重定默认值：短笔画受加速度
    # 限制峰速只有 sqrt(a·L)（本机 a=3000、中位笔画 1.73mm → 约 4400），
    # 书写进给默认 1500 保守起步；G0 空程实际走固件
    # $110/$111 最高速，travel_feed 仅用于耗时估算。Z 抬落提进给是缩短
    # 作业时间的最大单项（每笔两趟全行程），12000 在本机固件 $112=15000
    # 上限内；固件会自行钳制到各自上限，低配机器无损。
    draw_feed: float = 4400.0
    travel_feed: float = 9000.0
    pen_z_feed: float = 12000.0

    origin_offset: tuple[float, float] = (0.0, 0.0)  # 写字起点偏移(mm)
    mirror_x: bool = False
    mirror_y: bool = False
    scale: float = 1.0

    # 起止流程
    custom_start_gcode: str = ""     # 开始前自定义代码（; 分隔多条）
    custom_end_gcode: str = ""       # 结束后自定义代码（; 分隔多条）
    return_to_start: bool = True     # 完成后回到起点
    # 完成后回写的坐标：作业的书写起点（映射后机器坐标）。
    # None 时退回 X0 Y0（工件原点）。由 generate_gcode 填入。
    return_xy: tuple[float, float] | None = None

    # 坐标轴映射（对调XY/反转/原点位置）
    swap_xy: bool = False            # 对调 X/Y 轴
    invert_x: bool = False           # 反转 X 轴
    invert_y: bool = False           # 反转 Y 轴
    origin_corner: str = ORIGIN_NONE  # 内容哪个角对齐机器原点
    page_size: tuple[float, float] | None = None  # 纸张尺寸（生成时由文档填入）

    use_arcs: bool = False           # 预留：圆弧拟合（当前输出折线）
    simplify_mm: float = 0.0         # 笔画抽稀容差(mm)：>0 时减少微小线段，
                                     # 高速书写更顺滑
    # 书写顺序：reading=阅读顺序（写完一组再写下组，文字从左到右、从上到下，
    # 即常规书写顺序）；shortest=最短空程（贪心最近邻，适合矢量图/签名）。
    order_mode: str = "reading"

    # 收笔斜抬/入笔斜落（mm，沿笔画方向的过渡长度）。仅 Z 轴笔控
    # （pen_mode == PEN_Z）生效：笔画末端/开头的若干毫米改为
    # ``G1 X Y Z`` 复合移动，Z 按斜率上限（``pen_z_feed / draw_feed``，
    # 保证 Z 分速度不超 Z 抬落笔进给）边走边抬/落。压力渐变更像手写——
    # 笔在末端满压停留留下的墨点、起点重压的墨坨都因此消失；同时 XY↔Z
    # 的直角 junction 被斜坡软化，每笔两端的近全停加减速也随之减轻。
    # 默认开（1.5/0.5，P40 推荐）；设 0 关闭。非 Z 模式自动忽略。
    stroke_taper_mm: float = 1.5
    entry_taper_mm: float = 0.5
    # 机器 XY 加速度（mm/s²）：连接后能力探测（``$$`` 的 $120/$121 取小）。
    # 0 = 未知/未探测。只用于耗时估算与书写速度建议，不改变生成的指令。
    accel_xy: float = 0.0
    decimals: int = 3                # 坐标小数位
    feed_decimals: int = 0

    header: list[str] = field(default_factory=lambda: ["G21", "G90", "G94", "G17"])
    footer: list[str] = field(default_factory=lambda: ["M5"])
    include_comments: bool = True
    line_numbers: bool = False

    def clone(self) -> GCodeConfig:
        d = asdict(self)
        d["origin_offset"] = tuple(self.origin_offset)
        d["header"] = list(self.header)
        d["footer"] = list(self.footer)
        if self.page_size is not None:
            d["page_size"] = tuple(self.page_size)
        if self.return_xy is not None:
            d["return_xy"] = tuple(self.return_xy)
        return GCodeConfig(**d)

    def axis_mapping(self) -> GCodeConfig:
        """仅轴映射的副本：``origin_offset`` 清零，其余全部保留。

        纸面标记（笔位标记、机械原点标注）的显示换算必须用它：这些标记
        在纸上的位置只取决于轴映射（原点对齐角/对调/反转/镜像），与写字
        起点无关。若带着起点偏移换算，偏移又由标记推出的起点决定，形成
        「标记 → 起点 → 偏移 → 标记」的回环——拖动越界时标记被钳制"吸住"，
        机械原点标注被推出纸外不可见。
        """
        c = self.clone()
        c.origin_offset = (0.0, 0.0)
        return c

    def with_page_rotated(self, turns_cw: int, old_size: tuple[float, float],
                          new_size: tuple[float, float]) -> GCodeConfig:
        """纸张坐标系旋转 ``turns_cw`` 个 90° 后的等价轴映射。

        「旋转纸张」改变的是**页面坐标系**：同一台机器、同一张纸（物理
        注册关系不变）在旋转后的页面坐标系里，原点对齐角/对调/反转的
        等价组合会变。不换算的话，旋转后机械原点标注会指错角、工件原点
        模式的书写位置也会错位。

        做法：新旧映射都必须满足 ``map'(R(p)) == map(p)``（R 为页面旋转），
        在全部「原点角 × 对调 × 反转×2」组合里枚举出满足者——同一物理
        注册关系恰有一组等价组合。
        """
        turns_cw %= 4
        c = self.clone()
        c.page_size = (float(new_size[0]), float(new_size[1]))
        if turns_cw == 0:
            return c
        w, h = float(new_size[0]), float(new_size[1])
        # 旋转后页面坐标 → 旋转前页面坐标（含重新归一化）
        to_old = {
            1: lambda x, y: (h - y, x),          # 页面顺时针转 90°
            2: lambda x, y: (w - x, h - y),      # 180°
            3: lambda x, y: (y, w - x),          # 逆时针 90°
        }[turns_cw]
        old = self.clone()
        old.page_size = (float(old_size[0]), float(old_size[1]))
        # 原角优先：none 与 left-bottom 在 map_axis 里等价，保持原选择
        # 不漂移（如 none 不被换成 left-bottom、undo 往返能精确还原）
        corners = [self.origin_corner] + [
            c for c in (ORIGIN_NONE, ORIGIN_LEFT_BOTTOM, ORIGIN_RIGHT_BOTTOM,
                        ORIGIN_LEFT_TOP, ORIGIN_RIGHT_TOP)
            if c != self.origin_corner]
        for corner in corners:
            for swap in (False, True):
                for ix in (False, True):
                    for iy in (False, True):
                        cand = c.clone()
                        cand.origin_corner = corner
                        cand.swap_xy = swap
                        cand.invert_x = ix
                        cand.invert_y = iy
                        if all(_close(cand.map_axis(px, py),
                                      old.map_axis(*to_old(px, py)))
                               for px, py in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))):
                            return cand
        return c                       # 理论不可达；保守返回原映射

    def to_data(self) -> dict[str, Any]:
        d = asdict(self)
        d["origin_offset"] = list(self.origin_offset)
        return d

    # -- 坐标系设置（外部配置导入导出）-------------------------------------
    def apply_axes_settings(self, data: dict[str, Any] | None) -> None:
        """按外部坐标系设置字典填充轴映射。

        接受常见坐标系设置的键名（数字编号与英文键名两种写法）：
        ``revxy``(对调XY) / ``xAxeRev``(反转X) / ``yAxeRev``(反转Y) /
        ``pos``(坐标系位置，数字 0-3 或 "LeftUp" 等名称)。缺省键不动。
        """
        if not data:
            return
        if "revxy" in data:
            self.swap_xy = bool(data["revxy"])
        if "xAxeRev" in data:
            self.invert_x = bool(data["xAxeRev"])
        elif "axe_reverse_x" in data:
            self.invert_x = bool(data["axe_reverse_x"])
        if "yAxeRev" in data:
            self.invert_y = bool(data["yAxeRev"])
        elif "axe_reverse_y" in data:
            self.invert_y = bool(data["axe_reverse_y"])
        pos = data.get("pos", data.get("axe_pos"))
        if pos is not None:
            key = pos.lower() if isinstance(pos, str) else pos
            corner = AXE_POS_TO_CORNER.get(key)
            if corner is not None:
                self.origin_corner = corner

    def axes_settings(self) -> dict[str, Any]:
        """导出为外部坐标系设置字典结构（便于导入导出/回填）。"""
        return {
            "revxy": self.swap_xy,
            "xAxeRev": self.invert_x,
            "yAxeRev": self.invert_y,
            "pos": CORNER_TO_AXE_POS.get(self.origin_corner, 0),
        }

    @classmethod
    def from_data(cls, data: dict[str, Any] | None) -> GCodeConfig:
        if not data:
            return cls()
        allowed = set(cls().__dataclass_fields__)  # type: ignore[attr-defined]
        kw = {k: v for k, v in data.items() if k in allowed}
        if "origin_offset" in kw:
            kw["origin_offset"] = tuple(kw["origin_offset"])
        if kw.get("page_size") is not None:
            kw["page_size"] = tuple(kw["page_size"])
        if kw.get("return_xy") is not None:
            kw["return_xy"] = tuple(kw["return_xy"])
        return cls(**kw)

    # -- 抬落笔指令 ---------------------------------------------------------
    def pen_up_lines(self) -> list[str]:
        """抬笔指令序列（含抬笔前等待）。

        Z 模式抬笔用 ``G1 Z<up> F<zSpeed>`` 而非 ``G0``：把 ``zSpeed``
        注释为「步进电机抬笔速度设置」，抬笔同落笔一样受进给控制——
        弹簧回位笔架若以 G0 最高速弹回，易丢步/抖动，甚至撞限位。
        """
        lines: list[str] = []
        if self.pen_mode == PEN_Z:
            lines.append(f"G1 Z{self._f(self.pen_up_z)} F{self._ff(self.pen_z_feed)}")
        elif self.pen_mode == PEN_M3M5:
            lines.append("M5")
        elif self.pen_mode == PEN_SERVO_ANGLE:
            lines.append(f"M3 S{int(self.pen_up_s)}")
        elif self.pen_mode == PEN_LASER:
            lines.append("M5")
        elif self.pen_mode == PEN_M7M9:
            lines.append("M9")
        else:
            lines.append(self.pen_up_command or "M5")
        if self.pen_off_delay > 0:
            lines.append(f"G4 P{self._f(self.pen_off_delay)}")
        return [ln for ln in lines if ln]

    def pen_down_lines(self, to_z: float | None = None) -> list[str]:
        """落笔指令序列（含落笔后等待）。

        ``to_z`` 仅 Z 模式有效：入笔斜落时先只降到「半压」高度
        （完整下压由笔画开头的复合移动边走边完成），None 落到
        ``pen_down_z``。非 Z 模式忽略（舵机/激光没有可斜落的行程）。
        """
        lines: list[str] = []
        if self.pen_mode == PEN_Z:
            z = self.pen_down_z if to_z is None else to_z
            lines.append(f"G1 Z{self._f(z)} F{self._ff(self.pen_z_feed)}")
        elif self.pen_mode == PEN_M3M5:
            lines.append(f"M3 S{int(self.pen_down_s)}")
        elif self.pen_mode == PEN_SERVO_ANGLE:
            lines.append(f"M3 S{int(self.pen_down_angle)}")
        elif self.pen_mode == PEN_LASER:
            lines.append(f"M3 S{int(self.laser_power)}")
        elif self.pen_mode == PEN_M7M9:
            lines.append("M7")
        else:
            lines.append(self.pen_down_command or "M3")
        if self.pen_on_delay > 0:
            lines.append(f"G4 P{self._f(self.pen_on_delay)}")
        return [ln for ln in lines if ln]

    def split_custom(self, text: str) -> list[str]:
        """把 ``;`` 分隔的自定义 G-code 拆成行。"""
        return [s.strip() for s in text.split(";") if s.strip()]

    def start_lines(self) -> list[str]:
        return self.split_custom(self.custom_start_gcode)

    def end_lines(self) -> list[str]:
        out = self.split_custom(self.custom_end_gcode)
        if self.return_to_start:
            if self.return_xy is not None:
                sx, sy = self.return_xy
                out.append(f"G90 G0 X{self._f(sx)} Y{self._f(sy)}")
            else:
                out.append("G90 G0 X0 Y0")
        return out

    def _f(self, v: float) -> str:
        s = f"{v:.{self.decimals}f}"
        return s.rstrip("0").rstrip(".") if "." in s else s

    def _ff(self, v: float) -> str:
        # 注意：仅在有小数点时才能去尾零，否则 1500 会被截成 15
        s = f"{v:.{self.feed_decimals}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s or "0"

    # -- 坐标变换 -----------------------------------------------------------
    def map_point(self, p: tuple[float, float]) -> tuple[float, float]:
        x, y = p
        if self.scale != 1.0:
            x *= self.scale
            y *= self.scale
        if self.mirror_x:
            x = -x
        if self.mirror_y:
            y = -y
        ox, oy = self.origin_offset
        x += ox
        y += oy
        return self.map_axis(x, y)

    def map_axis(self, x: float, y: float) -> tuple[float, float]:
        """坐标轴映射：原点角落对齐 → 对调 XY → 反转轴。

        角落对齐把**纸张的选定角**平移到机器原点 (0,0)——这样无论机器的
        零点在台面的哪个角（「原点位置」），内容都从零点向可达
        方向展开，不再需要手工计算偏移。
        """
        if self.origin_corner != ORIGIN_NONE and self.page_size:
            w, h = self.page_size
            cx, cy = {
                ORIGIN_LEFT_BOTTOM: (0.0, 0.0),
                ORIGIN_RIGHT_BOTTOM: (w, 0.0),
                ORIGIN_LEFT_TOP: (0.0, h),
                ORIGIN_RIGHT_TOP: (w, h),
            }.get(self.origin_corner, (0.0, 0.0))
            x -= cx
            y -= cy
        if self.swap_xy:
            x, y = y, x
        if self.invert_x:
            x = -x
        if self.invert_y:
            y = -y
        return (x, y)

    def unmap_point(self, p: tuple[float, float]) -> tuple[float, float]:
        """``map_point`` 的逆变换：机器坐标 → 文档(页面)坐标。

        用于把机器当前位置（如笔头位置）换回画布坐标以显示笔位标记。
        """
        x, y = p
        if self.invert_y:
            y = -y
        if self.invert_x:
            x = -x
        if self.swap_xy:
            x, y = y, x
        if self.origin_corner != ORIGIN_NONE and self.page_size:
            w, h = self.page_size
            cx, cy = {
                ORIGIN_LEFT_BOTTOM: (0.0, 0.0),
                ORIGIN_RIGHT_BOTTOM: (w, 0.0),
                ORIGIN_LEFT_TOP: (0.0, h),
                ORIGIN_RIGHT_TOP: (w, h),
            }.get(self.origin_corner, (0.0, 0.0))
            x += cx
            y += cy
        ox, oy = self.origin_offset
        x -= ox
        y -= oy
        if self.mirror_x:
            x = -x
        if self.mirror_y:
            y = -y
        if self.scale not in (0.0, 1.0):
            x /= self.scale
            y /= self.scale
        return (x, y)

    def fmt_xy(self, x: float, y: float) -> str:
        return f"X{self._f(x)} Y{self._f(y)}"
