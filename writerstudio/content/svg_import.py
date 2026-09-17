"""SVG 矢量图导入。

用 ``svgelements`` 解析 SVG，把其中的路径/图元转成折线笔画（单位 mm）。
支持 path/line/polyline/polygon/rect/circle/ellipse，圆与贝塞尔曲线按容差离散。

坐标：SVG 为 **Y 向下**，本模块统一翻转为 **Y 向上**（项目约定），
并把图形平移到左下方为 (0,0)（即包围盒左下角对齐原点）。

导入后可经 :mod:`~writerstudio.perturb.engine` 的 ``wobble_strokes`` /
``perturb_strokes`` 施加手绘抖动，得到「手绘感」矢量图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from ..core.geometry import BBox
from ..core.strokes import Stroke

# 常用单位 → mm
_UNIT_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "in": 25.4,
    "pt": 25.4 / 72.0,
    "pc": 25.4 / 6.0,
    "px": 25.4 / 96.0,
    "": 25.4 / 96.0,   # 缺省按 96dpi 像素
}

# pt → px（96dpi）。与 tikz_text._PT_TO_PX 同值；历史帧（无 target_width
# 的归一化兜底）以此为单位约定，见 normalize_params。
_PT_TO_PX = 96.0 / 72.0


@dataclass
class SVGImportResult:
    strokes: list[Stroke] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def bbox(self) -> BBox:
        return BBox.from_points(p for s in self.strokes for p in s.points)


def svg_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("svgelements") is not None
    except Exception:
        return False


def _svg_length_to_mm(value) -> float:
    """把 svgelements 的 Length 转为 mm。

    svgelements 的 ``float(Length)`` 已把所有单位（含 pt/pc/mm）归一到
    96dpi 像素，这里只需再做 **一次** px→mm；若再按原单位乘系数会把
    pt/pc 双重换算（6pc 应为 25.4mm，错乘后成 406mm）。
    """
    try:
        from svgelements import Length
    except Exception:
        return float(value) * _UNIT_TO_MM["px"]
    if isinstance(value, Length):
        try:
            v = float(value)
        except Exception:
            return 0.0
        return v * _UNIT_TO_MM["px"]
    return float(value) * _UNIT_TO_MM["px"]


def parse_svg_geometry(path: str | Path, tolerance: float = 0.1
                       ) -> tuple[list[Stroke], list[str]]:
    """解析 SVG 得到**原始笔画**（svgelements 用户单位，Y 仍向下）。

    不做 mm 换算与 Y 翻转，保留原始坐标——供需要与外部坐标（如 pdftotext
    的文字框）对齐的调用方复用同一套归一化参数。
    """
    from svgelements import SVG, Path as SVGPath, Shape

    raw: list[Stroke] = []
    warnings: list[str] = []
    try:
        svg = SVG.parse(str(Path(path)))
    except Exception as exc:
        raise ValueError(f"无法解析 SVG：{exc}") from exc

    for element in svg.elements():
        if isinstance(element, (SVGPath, Shape)):
            for el in _as_subpaths(element):
                try:
                    segs = _element_to_points(el, tolerance)
                except Exception as exc:
                    warnings.append(f"{type(element).__name__}: {exc}")
                    continue
                for pts, closed in segs:
                    if len(pts) >= 2:
                        raw.append(Stroke(pts, closed))
    return raw, warnings


def normalize_params(raw: Sequence[Stroke],
                     target_width_mm: Optional[float] = None,
                     natural_scale: Optional[float] = None
                     ) -> tuple[BBox, float]:
    """由原始笔画求归一化参数 ``(包围盒, 缩放系数)``。

    与 :func:`import_svg` 用的是同一套换算，供文字位置等外部坐标同步对齐。
    ``natural_scale``：无 target_width 时的兜底缩放。缺省为 px→mm（SVG
    文件导入的物理尺寸语义）；TeX 编译产物走历史帧 px×96/72，由调用方
    （compile_tex_source）传入。
    """
    box = BBox.from_points(p for s in raw for p in s.points)
    if target_width_mm and box.width > 1e-9:
        scale = target_width_mm / box.width
    else:
        scale = natural_scale if natural_scale is not None \
            else _UNIT_TO_MM["px"]
    return box, scale


def normalize_point(x: float, y: float, box: BBox, scale: float
                    ) -> tuple[float, float]:
    """把 SVG 用户坐标点按 ``(box, scale)`` 归一化：Y 翻转 + 缩放到 mm。

    与 :func:`import_svg` 对笔画的换算完全一致（包围盒左下角 → 原点）。
    """
    return ((x - box.x0) * scale, (box.y1 - y) * scale)


def import_svg(path: str | Path,
               target_width_mm: Optional[float] = None,
               tolerance: float = 0.1,
               natural_scale: Optional[float] = None) -> SVGImportResult:
    """导入 SVG 文件为笔画（mm，Y 向上，包围盒左下角在原点）。

    ``target_width_mm`` 给定时按宽度等比缩放；否则按 ``natural_scale``
    （缺省 px→mm，即文件物理尺寸）。``tolerance`` 为曲线离散容差（mm）。
    """
    result = SVGImportResult()
    raw, warnings = parse_svg_geometry(path, tolerance)
    result.warnings.extend(warnings)
    if not raw:
        result.warnings.append("SVG 中未找到可绘制的矢量元素")
        return result

    box, scale = normalize_params(raw, target_width_mm, natural_scale)
    for s in raw:
        pts = [normalize_point(x, y, box, scale) for x, y in s.points]
        result.strokes.append(Stroke(pts, s.closed))

    nb = BBox.from_points(p for s in result.strokes for p in s.points)
    result.width = nb.width
    result.height = nb.height
    return result


def _as_subpaths(element):
    """把任意形状统一成可迭代的路径列表。

    svgelements 的 Rect/Circle/Ellipse 等既不可迭代也没有 ``as_subpaths``，
    需经 ``Path(shape)`` 转换；已有 ``as_subpaths`` 的（Path）直接用。
    """
    from svgelements import Path as SVGPath
    if hasattr(element, "as_subpaths"):
        try:
            return element.as_subpaths()
        except Exception:
            pass
    try:
        return [SVGPath(element)]
    except Exception:
        return [element]


def _element_to_points(el, tolerance: float):
    """把一个 svgelements 元素转为 [(点列表, 是否闭合)]。"""
    from svgelements import Move, Close, Line, QuadraticBezier, CubicBezier, Arc

    # Polygon/Polyline/Rect/SimpleLine 等本身带 points 的快速路径
    pts_attr = getattr(el, "points", None)
    if pts_attr:
        pts = [(float(p[0]), float(p[1])) for p in pts_attr if p is not None]
        if len(pts) >= 2:
            closed = bool(getattr(el, "closed", False))
            return [(pts, closed)]

    segs: list[tuple[list[tuple[float, float]], bool]] = []
    cur: list[tuple[float, float]] = []
    closed = False

    for seg in el:
        if isinstance(seg, Move):
            if len(cur) >= 2:
                segs.append((cur, closed))
            cur = [(float(seg.end[0]), float(seg.end[1]))]
            closed = False
        elif isinstance(seg, Close):
            if cur:
                cur.append((float(seg.end[0]), float(seg.end[1])))
                closed = True
                segs.append((cur, True))
                cur = []
        elif isinstance(seg, Line):
            cur.append((float(seg.end[0]), float(seg.end[1])))
        elif isinstance(seg, (QuadraticBezier, CubicBezier, Arc)):
            pts = _flatten_curve(seg, tolerance)
            cur.extend(pts)
        else:
            end = getattr(seg, "end", None)
            if end is not None:
                cur.append((float(end[0]), float(end[1])))
    if len(cur) >= 2:
        segs.append((cur, closed))
    return segs


def _bezier_point(t: float, p0, p1, p2, p3=None) -> tuple[float, float]:
    """Bernstein 多项式直接求值（与 svgelements 逐点调用结果一致，
    单次 ~0.5µs vs ~27µs）。``p3=None`` 为二次曲线。"""
    mt = 1.0 - t
    if p3 is None:
        a = mt * mt
        b = 2.0 * mt * t
        c = t * t
        return (a * p0[0] + b * p1[0] + c * p2[0],
                a * p0[1] + b * p1[1] + c * p2[1])
    a = mt * mt * mt
    b = 3.0 * mt * mt * t
    c = 3.0 * mt * t * t
    d = t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _flatten_curve(seg, tolerance: float, samples: int = 32):
    """把曲线段离散为折线。

    自适应采样：控制点贴弦的「伪曲线」直接退化成直线（最快）；其余按
    控制多边形链长 ≈2px 一步取点（上限 ``samples`` 步/段，弧按扫角
    加密）。平坦处点数比旧的固定 32 采样少一个量级，形状肉眼无差。
    贝塞尔走 Bernstein 直接求值，不逐点调 svgelements。
    """
    from svgelements import Arc, CubicBezier, QuadraticBezier

    if isinstance(seg, Arc):
        n = max(12, int(64 * (abs(getattr(seg, "sweep", 0)) or 1.0)))
        n = min(n, 256)
        pts = []
        for i in range(1, n + 1):
            try:
                p = seg.point(i / n)
                pts.append((float(p[0]), float(p[1])))
            except Exception:
                break
        return pts

    start = seg.start
    end = seg.end
    if isinstance(seg, QuadraticBezier):
        c1 = seg.control                # 二次曲线只有一个控制点
        c2 = None
    else:
        c1 = getattr(seg, "control1", None)
        c2 = getattr(seg, "control2", None)
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    chord2 = dx * dx + dy * dy
    if chord2 <= 1e-18:
        return [(end[0], end[1])]              # 零长曲线段
    # 贴弦判定：控制点到弦线的偏距 ≤ 弦长×1e-3 → 直线，直接出端点
    dev = 0.0
    poly_len = chord2 ** 0.5                   # 控制多边形链长（长度上界）
    prev = start
    for c in (c1, c2, end):
        if c is None:
            continue
        cross = (c[0] - prev[0]) * dy - (c[1] - prev[1]) * dx
        if abs(cross) > dev:
            dev = abs(cross)
        poly_len += ((c[0] - prev[0]) ** 2 + (c[1] - prev[1]) ** 2) ** 0.5
        prev = c
    if dev * dev <= chord2 * 1e-6:
        return [(end[0], end[1])]
    n = min(samples, max(1, int(poly_len / 2.0) + 1))
    if isinstance(seg, QuadraticBezier):
        return [_bezier_point(i / n, start, c1, end) for i in range(1, n + 1)]
    if isinstance(seg, CubicBezier):
        return [_bezier_point(i / n, start, c1, c2, end)
                for i in range(1, n + 1)]
    # 其它类型（少见）：回退 svgelements 通用采样
    pts = []
    for i in range(1, n + 1):
        try:
            p = seg.point(i / n)
            pts.append((float(p[0]), float(p[1])))
        except Exception:
            break
    return pts
