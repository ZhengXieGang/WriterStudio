"""G-code 生成器：笔画 → GRBL 可执行的 G-code。

流程：
    文档对象 → 世界坐标笔画 → （可选）笔序优化 → 抬落笔 + 直线插补 → G-code 行

坐标：文档 mm、Y 向上，与 GRBL 机械坐标一致，**不做 Y 翻转**；
      机器坐标 = 固定映射 ``page_to_machine``（机械原点 = 纸张左上角）
      ＋ 起点偏移 ``start_offset``（把纸张实际台面位置烘焙进绝对坐标）。

生成结果同时给出统计信息（绘制长度、空程、估算时间），供界面预览。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ..core.document import Document, DocumentObject
from ..core.geometry import rdp_simplify
from ..core.strokes import Stroke
from .config import GCodeConfig, PEN_Z, page_to_machine
from .path_optimizer import order_for_writing
from .profile import motion_time, suggested_feed


@dataclass
class GCodeResult:
    lines: list[str] = field(default_factory=list)
    draw_length: float = 0.0
    travel_length: float = 0.0
    estimated_seconds: float = 0.0
    stroke_count: int = 0
    point_count: int = 0
    suggested_draw_feed: float = 0.0   # 按加速度可达性给出的速度建议，0=无

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def collect_strokes(objects: Iterable[DocumentObject], *,
                    visible_only: bool = True) -> list[Stroke]:
    """把多个文档对象的世界坐标笔画汇总。"""
    out: list[Stroke] = []
    for o in objects:
        if visible_only and not o.visible:
            continue
        out.extend(o.world_strokes())
    return out


def collect_grouped_strokes(objects: Iterable[DocumentObject], *,
                            visible_only: bool = True
                            ) -> tuple[list[Stroke], list[int]]:
    """汇总笔画并给出**分组号**（每个对象一组），供阅读顺序书写使用。

    每个对象（一个文本块/一行字/一张图）算一组：组内保持自然顺序
    （文字本就是逐字阅读顺序），组间按位置排序——这样书写顺序连贯，
    不会一会儿写这儿一会儿写那儿。
    """
    strokes: list[Stroke] = []
    groups: list[int] = []
    for gi, o in enumerate(objects):
        if visible_only and not o.visible:
            continue
        ws = o.world_strokes()
        strokes.extend(ws)
        groups.extend([gi] * len(ws))
    return strokes, groups


def _rdp(points: list, tol: float) -> list:
    """Douglas–Peucker 抽稀（保首尾点）；与扰动「抽稀容差」共用同一实现。"""
    return rdp_simplify(points, tol)


def generate_gcode(strokes: Sequence[Stroke], config: Optional[GCodeConfig] = None,
                   document: Optional[Document] = None,
                   optimize: bool = True,
                   start_point: Optional[tuple[float, float]] = None,
                   groups: Optional[Sequence[int]] = None,
                   start_offset: tuple[float, float] = (0.0, 0.0)) -> GCodeResult:
    """把笔画生成为 G-code。

    ``optimize`` 为 True 时先做笔序规划。``groups`` 为每条笔画的分组号
    （每个字符/对象一组）——有分组且 ``config.order_mode == "reading"`` 时
    按**阅读顺序**书写（写完一组再写下一组、从左到右从上到下）；否则回退为
    最短空程的贪心最近邻。

    坐标映射分两步：先按固定映射 ``page_to_machine``（机械原点 = 纸张
    左上角）把页面坐标换成机器坐标，再整体平移 ``start_offset``（**机器系**
    平移，mm）。``start_offset`` 就是「写字起点/笔位校准」的落地方式——把
    纸张在台面上的实际位置烘焙进绝对坐标，使 ``start_point``（页面坐标）
    映射后恰好落到用户标定的物理笔位。它是对全部笔画与停笔位统一的
    刚体平移，不改变内容自身的形状与相对布局。默认 ``(0,0)`` 即纸张
    左上角对齐机械原点。

    ``start_point`` 为起始点（文档坐标）＝写之前先把笔移动到的位置
    （首条 G0），同样受 ``start_offset`` 平移，因此与实际落笔点一致。
    ``config.simplify_mm`` > 0 时先按该容差（mm）抽稀笔画采样点：
    高速书写时微小线段会导致机器频繁换向而抖动，适度抽稀让运动更顺滑
    （弧长抽稀）。
    """
    cfg = (config or GCodeConfig()).clone()

    idx = [i for i, s in enumerate(strokes) if len(s.points) >= 2]
    use: list[Stroke] = [strokes[i] for i in idx]
    grp: Optional[list[int]] = (
        [int(groups[i]) for i in idx] if groups is not None else None)
    if not use:
        return GCodeResult(lines=list(cfg.header) + list(cfg.start_lines())
                           + list(cfg.end_lines()) + list(cfg.footer))
    if cfg.page_size is None:
        raise ValueError("生成 G-code 需要 config.page_size（纸张尺寸）")
    page_h = float(cfg.page_size[1])
    if cfg.simplify_mm > 0:
        simplified: list[Stroke] = []
        kept_groups: list[int] = []
        for j, s in enumerate(use):
            pts = _rdp(list(s.points), cfg.simplify_mm) \
                if not s.closed else list(s.points)
            if len(pts) >= 2:
                simplified.append(Stroke(pts, s.closed, s.role, s.group))
                if grp is not None:
                    kept_groups.append(grp[j])
        use = simplified
        grp = kept_groups if grp is not None else None
    if not use:
        return GCodeResult(lines=list(cfg.header) + list(cfg.start_lines())
                           + list(cfg.end_lines()) + list(cfg.footer))

    if optimize:
        use = order_for_writing(
            use, groups=grp, start=start_point,
            mode=getattr(cfg, "order_mode", "reading"))

    lines: list[str] = []
    # 预处理：坐标变换（机械原点 = 纸张左上角，外加「坐标轴」面板的
    # 对调/反转修正，见 page_to_machine；机械零点不随修正移动），
    # 再叠加起点偏移（机器系刚体平移，把纸张的实际台面位置烘焙进绝对坐标）
    swap, ix, iy = (bool(cfg.swap_xy), bool(cfg.invert_x), bool(cfg.invert_y))
    ox, oy = float(start_offset[0]), float(start_offset[1])

    def _m(p: tuple[float, float]) -> tuple[float, float]:
        mx, my = page_to_machine(p, page_h, swap, ix, iy)
        return (mx + ox, my + oy)

    mapped: list[list[tuple[float, float]]] = [
        [_m(p) for p in s.points] for s in use
    ]

    # 起始点（映射+偏移后）：写之前的停笔位（首条 G0 / 完成后回位点）
    if start_point is not None:
        sx, sy = _m(start_point)
    else:
        sx, sy = mapped[0][0]
    # 「完成后回起点」回到的是作业实际开始书写的点，而非机器零点
    cfg.return_xy = (sx, sy)

    lines.extend(cfg.header)
    lines.extend(cfg.start_lines())     # 开始前自定义代码

    # 收笔斜抬/入笔斜落的可行性：仅 Z 轴笔控（复合移动里有 Z 词）；斜率
    # 上限 = pen_z_feed/draw_feed（Z 方向 mm / XY 方向 mm），保证斜坡段的
    # Z 分速度不超过 Z 抬落笔进给，GRBL 无需额外钳制
    do_taper = (cfg.pen_mode == PEN_Z
                and (cfg.stroke_taper_mm > 0 or cfg.entry_taper_mm > 0)
                and cfg.draw_feed > 0 and cfg.pen_z_feed > 0
                and cfg.pen_down_z > cfg.pen_up_z)
    slope_cap = (cfg.pen_z_feed / cfg.draw_feed) if do_taper else 0.0

    def _plan_taper(pts: list[tuple[float, float]]):
        """单笔的斜坡计划 ``(入笔长, 入笔ΔZ, 收笔长, 收笔ΔZ, 全长)``。

        入/收笔预算各不超过笔画全长的一半（两坡不重叠）；ΔZ 受满行程
        ``down−up`` 与斜率上限双重封顶。斜坡分不到 Z 行程（极短笔画/极缓
        斜率）时返回 None，该笔按无斜坡发射。
        """
        if not do_taper:
            return None
        total = sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
        if total <= 1e-9:
            return None
        e_len = (min(cfg.entry_taper_mm, total / 2)
                 if cfg.entry_taper_mm > 0 else 0.0)
        x_len = (min(cfg.stroke_taper_mm, total / 2)
                 if cfg.stroke_taper_mm > 0 else 0.0)
        full = cfg.pen_down_z - cfg.pen_up_z
        dz_e = min(full, slope_cap * e_len) if e_len > 0 else 0.0
        dz_x = min(full, slope_cap * x_len) if x_len > 0 else 0.0
        if dz_e <= 1e-9 and dz_x <= 1e-9:
            return None
        return (e_len, dz_e, x_len, dz_x, total)

    # 抬笔（初始）
    lines.extend(cfg.pen_up_lines())
    # 移到起始点
    lines.append(f"G0 {cfg.fmt_xy(sx, sy)}")

    draw_len = 0.0
    travel_len = 0.0
    point_count = 0
    stroke_lens: list[float] = []     # 每笔实际书写长度（加速度感知估算用）
    travel_lens: list[float] = []
    cur = (sx, sy)
    pen_down = False

    for pts in mapped:
        # 移到本笔画起点（抬笔状态）
        if pen_down:
            lines.extend(cfg.pen_up_lines())
            pen_down = False
        s0 = pts[0]
        if s0 != cur:
            d = _dist(cur, s0)
            lines.append(f"G0 {cfg.fmt_xy(s0[0], s0[1])}")
            travel_len += d
            travel_lens.append(d)
            cur = s0
        # 落笔（入笔斜落：先只降到「半压」高度，完整下压在开头斜坡完成）
        plan = _plan_taper(pts)
        e_len, dz_e, x_len, dz_x, total = plan or (0.0, 0.0, 0.0, 0.0, 0.0)
        if plan:
            # 在斜坡边界补点：坡内恒有顶点，与笔画采样密度无关
            pts = _split_taper_boundaries(pts, e_len, x_len)
        lines.extend(cfg.pen_down_lines(
            to_z=(cfg.pen_down_z - dz_e) if dz_e > 0 else None))
        pen_down = True
        # 直线插补（斜坡段带 Z 词：边走边落/边走边抬）
        cum = 0.0
        for p in pts[1:]:
            if p == cur:
                continue
            seg = _dist(cur, p)
            cum += seg
            z_word = None
            if dz_e > 0 and cum <= e_len:
                # 入笔斜落：Z 从半压高度线性降到全压
                z_word = cfg.pen_down_z - dz_e + dz_e * (cum / e_len)
            elif dz_x > 0 and (total - cum) < x_len:
                # 收笔斜抬：Z 从全压线性抬起；末端仍留 dz_x 的抬升行程
                # 交给随后的 pen_up 指令收尾
                frac = (x_len - (total - cum)) / x_len
                z_word = cfg.pen_down_z - dz_x * frac
            if z_word is not None:
                lines.append(f"G1 {cfg.fmt_xy(p[0], p[1])}"
                             f" Z{cfg._f(z_word)} F{cfg._ff(cfg.draw_feed)}")
            else:
                lines.append(f"G1 {cfg.fmt_xy(p[0], p[1])} F{cfg._ff(cfg.draw_feed)}")
            draw_len += seg
            cur = p
            point_count += 1
        stroke_lens.append(cum)
        point_count += 1

    # 收尾抬笔
    if pen_down:
        lines.extend(cfg.pen_up_lines())
    lines.extend(cfg.end_lines())       # 结束自定义代码 + 回起点（可选）
    lines.extend(cfg.footer)

    if cfg.include_comments:
        lines.append(f"(draw {draw_len:.1f}mm, travel {travel_len:.1f}mm)")

    est = _estimate_job(stroke_lens, travel_lens, cfg)
    return GCodeResult(
        lines=lines,
        draw_length=draw_len,
        travel_length=travel_len,
        estimated_seconds=est,
        stroke_count=len(use),
        point_count=point_count,
        suggested_draw_feed=suggested_feed(
            stroke_lens, cfg.accel_xy, cfg.draw_feed),
    )


def generate_from_document(doc: Document, config: Optional[GCodeConfig] = None,
                           optimize: bool = True,
                           start_point: Optional[tuple[float, float]] = None,
                           start_offset: tuple[float, float] = (0.0, 0.0)
                           ) -> GCodeResult:
    strokes, groups = collect_grouped_strokes(doc.objects)
    if config is not None:
        config = config.clone()          # 不改调用方的配置（如 page_size）
        if config.page_size is None:
            # 坐标轴映射（原点角落）需要纸张尺寸
            config.page_size = (doc.page.width, doc.page.height)
    return generate_gcode(strokes, config, doc, optimize, start_point,
                          groups=groups, start_offset=start_offset)


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _split_taper_boundaries(pts: list, e_len: float, x_len: float) -> list:
    """在入笔/收笔斜坡的弧长边界处插入中间点。

    斜坡按「沿笔画方向的弧长」划定，但笔画顶点间距不定——一条 20mm 的
    直线段上斜坡预算只有 1mm 时，不补点的话没有任何顶点落进坡内。在
    边界处插值拆段后，斜坡发射与顶点采样密度无关，几何保持不变。
    """
    total = sum(_dist(a, b) for a, b in zip(pts, pts[1:]))
    bounds = []
    if 1e-9 < e_len < total - 1e-9:
        bounds.append(e_len)
    if x_len > 0:
        b = total - x_len
        if 1e-9 < b < total - 1e-9:
            bounds.append(b)
    if not bounds:
        return pts
    bounds.sort()
    out = [pts[0]]
    cum = 0.0
    bi = 0
    for a, b in zip(pts, pts[1:]):
        seg = _dist(a, b)
        seg_end = cum + seg
        while (bi < len(bounds)
               and bounds[bi] > cum + 1e-9 and bounds[bi] < seg_end - 1e-9):
            t = (bounds[bi] - cum) / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            bi += 1
        out.append(b)
        cum = seg_end
    return out


def _estimate_seconds(draw_len: float, travel_len: float,
                      cfg: GCodeConfig) -> float:
    """旧版粗估（长度/进给，无加速度、无 Z 抬落开销）。

    保留供旧调用方/测试对照；:func:`generate_gcode` 现在用
    :func:`_estimate_job`。
    """
    t = 0.0
    if cfg.draw_feed > 0:
        t += draw_len / cfg.draw_feed * 60.0
    if cfg.travel_feed > 0:
        t += travel_len / cfg.travel_feed * 60.0
    return t


def _estimate_job(stroke_lens: list[float], travel_lens: list[float],
                  cfg: GCodeConfig) -> float:
    """按机器能力估算整份作业耗时（秒）。

    * 绘制/空程：知道 XY 加速度（``cfg.accel_xy``，能力探测所得）时按
      梯形/三角形速度剖面逐笔计算——短笔画永远达不到满进给，旧模型
      「长度/进给」会低估数倍；未知时退化为旧模型。
    * Z 抬落：每笔两趟全行程按 ``pen_z_feed`` 计（机械手写字机的最大
      固定开销，进给设多高都躲不掉）。
    * G4 延迟按笔数累加。
    """
    a = cfg.accel_xy if cfg.accel_xy > 0 else 0.0
    t = sum(motion_time(l, cfg.draw_feed, a) for l in stroke_lens)
    t += sum(motion_time(l, cfg.travel_feed, a) for l in travel_lens)
    n = len(stroke_lens)
    if cfg.pen_mode == PEN_Z and cfg.pen_z_feed > 0:
        dz = abs(cfg.pen_down_z - cfg.pen_up_z)
        t += n * 2.0 * dz / cfg.pen_z_feed * 60.0
    t += n * (max(0.0, cfg.pen_on_delay) + max(0.0, cfg.pen_off_delay))
    return t


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(int(round(seconds)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"
