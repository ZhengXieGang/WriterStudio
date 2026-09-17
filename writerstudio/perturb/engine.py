"""手写扰动引擎。

对 :class:`~writerstudio.fonts.layout.TextLayout` 施加三级扰动，输出新的页面坐标笔画。
本模块采用 **duck typing**，不导入字体模块，从而可同时服务于：
    * 文本排版结果（字符级 + 行级 + 笔画级）
    * 任意矢量笔画 / 自由手绘（:func:`perturb_strokes`、:func:`wobble_strokes`）

确定性：固定 ``params.seed`` 时，随机数的取用顺序固定（行 → 字符 → 笔画），
结果完全可复现；更换 seed 即「一键重摇」。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ..core.geometry import AffineTransform, Vec2, rdp_simplify
from ..core.strokes import Stroke
from .params import PerturbParams


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------
@dataclass
class CharPerturb:
    """单个字符的扰动量（便于预览/调试/后续逐字处理）。"""

    char: str
    font_name: str
    size_factor: float = 1.0
    dx: float = 0.0
    dy: float = 0.0
    rotation: float = 0.0
    shear: float = 0.0
    sine_dy: float = 0.0   # 基线起伏分量（行尾落差+游走，不含逐字抖动）


@dataclass
class PerturbResult:
    strokes: list[Stroke] = field(default_factory=list)
    chars: list[CharPerturb] = field(default_factory=list)

    def bbox(self):
        from ..core.geometry import BBox
        box = BBox()
        for s in self.strokes:
            for p in s.points:
                box.expand(p)
        return box


# ---------------------------------------------------------------------------
# 基础随机工具
# ---------------------------------------------------------------------------
def _gauss(rng: random.Random, sigma: float) -> float:
    if sigma <= 0.0:
        return 0.0
    return rng.gauss(0.0, sigma)


def _scaled(value: float, params: PerturbParams) -> float:
    """把单个扰动幅值乘以总强度系数。"""
    return value * params.intensity_factor


def _transform_points(strokes: Sequence[Stroke],
                      origin: Vec2,
                      size_factor: float,
                      rot_deg: float,
                      dx: float,
                      dy: float,
                      shear_deg: float = 0.0) -> list[Stroke]:
    """对一组笔画依次做：绕 origin 缩放 → 绕整体中心旋转 → 体态斜切 → 平移。"""
    # 1) 缩放（绕字符排版原点）
    if size_factor != 1.0:
        scale_t = AffineTransform.scale_about(size_factor, size_factor, origin)
        scaled = [s.transformed(scale_t) for s in strokes]
    else:
        scaled = [s.clone() for s in strokes]

    # 2) 旋转（绕缩放后整体中心）
    if rot_deg != 0.0:
        from ..core.geometry import BBox
        box = BBox.from_points(p for s in scaled for p in s.points)
        if not box.is_empty:
            center = box.center
            rot_t = AffineTransform.rotate_about(rot_deg, center)
            scaled = [s.transformed(rot_t) for s in scaled]

    # 2.5) 体态斜切（绕整体中心）：x 随 y 线性偏移——「肩线/体态」的倾斜
    # 是非正形变换，旋转再大也产生不了这种形态，只能用 shear
    if shear_deg != 0.0:
        from ..core.geometry import BBox
        box = BBox.from_points(p for s in scaled for p in s.points)
        if not box.is_empty:
            k = math.tan(math.radians(max(-60.0, min(60.0, shear_deg))))
            shear_t = AffineTransform.shear_x_about(k, box.center)
            scaled = [s.transformed(shear_t) for s in scaled]

    # 3) 平移
    if dx != 0.0 or dy != 0.0:
        trans_t = AffineTransform.translate(dx, dy)
        scaled = [s.transformed(trans_t) for s in scaled]

    return scaled


def _perturb_stroke_once(s: Stroke, rng: random.Random,
                         params: PerturbParams,
                         apply_rigid: bool = True) -> Stroke:
    """单笔画：绕自身中心旋转 + 位移。

    ``apply_rigid=False`` 时仍按同样顺序取用随机数（保持确定性），但不施加
    变换——结构长线（表格线/边框）若整体旋转会从接缝处被撕开，交由起伏处理。
    """
    theta = _gauss(rng, _scaled(params.stroke_theta_sigma, params))
    dx = _gauss(rng, _scaled(params.stroke_x_sigma, params))
    dy = _gauss(rng, _scaled(params.stroke_y_sigma, params))
    if not apply_rigid or (theta == 0.0 and dx == 0.0 and dy == 0.0):
        return s.clone()
    box = s.bbox()
    t = AffineTransform.identity()
    if theta != 0.0 and not box.is_empty:
        t = AffineTransform.rotate_about(theta, box.center)
    if dx != 0.0 or dy != 0.0:
        t = AffineTransform.translate(dx, dy) @ t
    return s.transformed(t)


def _stretch_stroke(s: Stroke, factor: float) -> Stroke:
    """沿笔画主方向（首末点连线）绕自身中心伸缩，垂直方向保持。

    让同一笔画的长短/舒展程度各不相同——旋转与位移都是刚性的，
    只有伸缩能改变笔画的「伸展体态」。
    """
    if abs(factor - 1.0) < 1e-9 or len(s.points) < 2:
        return s.clone()
    pts = s.points
    dx, dy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
    dl = math.hypot(dx, dy)
    if dl < 1e-9:
        return s.clone()
    ux, uy = dx / dl, dy / dl
    box = s.bbox()
    if box.is_empty:
        return s.clone()
    q = factor - 1.0
    # M = I + q·ûûᵀ：沿 û 方向伸缩 q，垂直向不变（û ûᵀ 是方向投影矩阵）
    m = AffineTransform(
        1.0 + q * ux * ux, q * ux * uy, q * ux * uy, 1.0 + q * uy * uy, 0.0, 0.0)
    cx, cy = box.center
    t = (AffineTransform.translate(cx, cy) @ m
         @ AffineTransform.translate(-cx, -cy))
    return s.transformed(t)


def _trim_stroke(s: Stroke, trim_start: float, trim_end: float) -> Stroke:
    """按弧长裁掉首尾各一段，模拟起笔/收笔的参差长短。

    闭合笔画与裁后所剩无几的笔画不裁；裁剪端点在原折线上插值，
    保持走向自然。
    """
    if trim_start <= 0.0 and trim_end <= 0.0:
        return s
    if s.closed or len(s.points) < 2:
        return s
    pts = s.points
    arc = _arc_lengths(pts)
    total = arc[-1]
    if total <= trim_start + trim_end + 1e-9:
        return s.clone()
    lo, hi = trim_start, total - trim_end

    def _point_at(t: float) -> Vec2:
        for i in range(1, len(arc)):
            if arc[i] >= t:
                seg = arc[i] - arc[i - 1]
                u = 0.0 if seg < 1e-12 else (t - arc[i - 1]) / seg
                a, b = pts[i - 1], pts[i]
                return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)
        return pts[-1]

    out = [p for p, a in zip(pts, arc) if lo + 1e-9 <= a <= hi - 1e-9]
    out.insert(0, _point_at(lo))
    out.append(_point_at(hi))
    return Stroke(out, False, s.role, s.group)


def _maybe_simplify(strokes: list[Stroke], params: PerturbParams) -> list[Stroke]:
    """按「抽稀容差」精简扰动输出的采样点（闭合笔画保持原样）。

    与 G-code 导出抽稀（``GCodeConfig.simplify_mm``）同一算法
    （Douglas–Peucker）；这里在对象生成阶段
    就把微小线段合并掉，画布所见即所得。
    """
    tol = params.simplify_mm
    if tol <= 0.0:
        return strokes
    out: list[Stroke] = []
    for s in strokes:
        if s.closed:
            out.append(s)
            continue
        pts = rdp_simplify(list(s.points), tol)
        out.append(Stroke(pts, False) if len(pts) >= 2 else s)
    return out


def smooth_stroke(s: Stroke, amount: float) -> Stroke:
    """轻度平滑：把每个内部点向相邻点均值靠拢，削弱折线毛刺。"""
    if amount <= 0.0 or len(s.points) < 3:
        return s
    a = max(0.0, min(1.0, amount))
    pts = s.points
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        px, py = pts[i]
        nx = (pts[i - 1][0] + pts[i + 1][0]) * 0.5
        ny = (pts[i - 1][1] + pts[i + 1][1]) * 0.5
        out.append((px + (nx - px) * a, py + (ny - py) * a))
    out.append(pts[-1])
    return Stroke(out, s.closed, s.role, s.group)


def _smoothstep(t: float) -> float:
    """淡入淡出插值，令噪声在控制点之间是 C1 平滑的（人手不会画折角）。"""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _value_noise(rng: random.Random, xs: list[float], wavelength: float,
                 octaves: int) -> list[float]:
    """沿弧长 ``xs`` 生成**平滑**的伪随机噪声序列（值域约 [-1, 1]）。

    做法是「值噪声」：每隔 ``wavelength`` 放一个随机控制点，控制点之间用
    smoothstep 插值；再叠加若干倍频（频率翻倍、振幅减半）增加细节。相比直接
    给每个点加独立随机数（会得到锯齿状的毛刺），这样得到的是舒展、连贯的
    起伏——正是人手画线时的那种轻微摆动。
    """
    n = len(xs)
    if n == 0:
        return []
    wl = wavelength if wavelength > 1e-6 else 1.0
    out = [0.0] * n
    norm_sq = 0.0
    for o in range(max(1, octaves)):
        k = o + 1
        lam = wl / k
        amp = 1.0 / k
        span = xs[-1] / lam if xs[-1] > 0 else 0.0
        n_knots = max(2, int(math.ceil(span)) + 2)
        knots = [rng.uniform(-1.0, 1.0) for _ in range(n_knots)]
        for i, x in enumerate(xs):
            u = x / lam
            i0 = int(u)
            if i0 < 0:
                i0 = 0
            if i0 >= n_knots - 1:
                out[i] += amp * knots[-1]
                continue
            t = u - i0
            a, b = knots[i0], knots[i0 + 1]
            out[i] += amp * (a + (b - a) * _smoothstep(t))
        norm_sq += amp * amp
    # 用 L2 范数归一：多个倍频叠加时峰值仍能接近 ±1（若按算术和归一会被压得过低）；
    # 再硬限幅到 ±1，保证位移不超过用户设定的「起伏振幅」。
    norm = math.sqrt(norm_sq)
    if norm > 0.0:
        out = [max(-1.0, min(1.0, v / norm)) for v in out]
    return out


def _arc_lengths(pts: list[Vec2]) -> list[float]:
    arc = [0.0] * len(pts)
    for i in range(1, len(pts)):
        arc[i] = arc[i - 1] + math.hypot(pts[i][0] - pts[i - 1][0],
                                        pts[i][1] - pts[i - 1][1])
    return arc


def _resample_long_segments(pts: list[Vec2], max_seg: float) -> list[Vec2]:
    """把折线里超过 ``max_seg`` 的长直段按弧长细分（端点保持原位）。

    表格线/下划线等来自渲染器的 2 点线段没有内部顶点，起伏对它们原本是
    空操作（顶点位移只见于内部点）；先细分出中间采样点，法向噪声才有
    施加对象，直线才能被「画弯」。已经足够密的笔画不受影响。
    """
    if max_seg <= 0.0 or len(pts) < 2:
        return pts
    out: list[Vec2] = [pts[0]]
    for a, b in zip(pts, pts[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n_ins = int(d / max_seg)
        for k in range(1, n_ins + 1):
            t = k / (n_ins + 1)
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        out.append(b)
    return out


# 结构长线识别：表格线/边框/下划线/坐标轴这类互相连接的线条，整条线的
# 形状由结构决定（端点即接缝），不能像字形笔画那样整体旋转/伸缩/修剪——
# 否则会从接缝处被「撕开」。判据 = 非字形 + 够长 + 与他线相接。
_STRUCTURAL_MIN_MM = 4.0        # 结构长线的绝对长度下限
_STRUCTURAL_MIN_WAVES = 2.0     # 起伏至少铺满几个波，避免退化成单向倾斜
_JUNCTION_TOL_MM = 0.6          # 端点相接/落在对方上的判定容差


def _point_near_segment(p, a, b, tol: float) -> bool:
    """点 ``p`` 是否落在（或贴着）线段 ``a-b`` 上。"""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay) <= tol
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(p[0] - cx, p[1] - cy) <= tol


def _hash_junctions(strokes: Sequence[Stroke],
                    candidates: list[int], tol: float) -> list[tuple[int, int]]:
    """找出候选笔画之间「端点相接」的配对（空间哈希，O(线段数) 量级）。

    朴素的逐对判定（每对笔画互查 4 个端点是否落在对方线段上）是
    O(k²×段数)：千条相连的表格线/网格每次调扰动要 1 秒以上，界面明显
    发卡。这里把所有候选线段按格子（边长 ``4×tol``）登记，端点只查自己
    周围 3×3 格子里的线段，命中（端点落在对方线段 ``tol`` 内）即并入
    同一连通分量；返回按分量展开的配对表，判定条件与逐对版本完全一致。
    """
    cell = max(4.0 * tol, 1e-6)
    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def _cells_along(a, b):
        """线段扫过的格子：按 ~半格步长沿线采样（连续采样不会跳格）。"""
        step = cell * 0.5
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(length / step) + 1)
        seen = set()
        for k in range(n + 1):
            t = k / n
            cx = int((a[0] + (b[0] - a[0]) * t) // cell)
            cy = int((a[1] + (b[1] - a[1]) * t) // cell)
            seen.add((cx, cy))
        return seen

    for i in candidates:
        s = strokes[i]
        for si, (u, v) in enumerate(zip(s.points, s.points[1:])):
            for c in _cells_along(u, v):
                grid.setdefault(c, []).append((i, si))

    parent = {i: i for i in candidates}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        ra, rb = find(x), find(y)
        if ra != rb:
            parent[ra] = rb

    for i in candidates:
        s = strokes[i]
        for p in (s.points[0], s.points[-1]):
            cx, cy = int(p[0] // cell), int(p[1] // cell)
            for gx in (cx - 1, cx, cx + 1):
                for gy in (cy - 1, cy, cy + 1):
                    for j, si in grid.get((gx, gy), ()):
                        if j == i:
                            continue
                        other = strokes[j]
                        u, v = other.points[si], other.points[si + 1]
                        if _point_near_segment(p, u, v, tol):
                            union(i, j)
    # 按连通分量输出配对（≥2 条的分量才算结构，与逐对版本语义一致）
    groups: dict[int, list[int]] = {}
    for i in candidates:
        groups.setdefault(find(i), []).append(i)
    pairs: list[tuple[int, int]] = []
    for members in groups.values():
        if len(members) >= 2:
            first = members[0]
            for m in members[1:]:
                pairs.append((first, m))
    return pairs


def _is_structural_strokes(strokes: Sequence[Stroke]) -> list[bool]:
    """逐笔判断是否为「结构长线」（表格线/边框/下划线等）。

    判据：非字形、长度 ≥ :data:`_STRUCTURAL_MIN_MM`，且通过端点相接
    与另一条同类线连通。单独一条长线没有可被撕开的接缝，整体旋转反而
    能带来自然的手写变化，应照常处理（返回 False）。

    实测：markdown 表格开启扰动后，网格线被各自整体旋转/平移，端点从
    交叉处错开，看起来像「沿正弦波斜切」而非手绘——正是这类互相连接的
    长线被当作字形笔画做了刚性变换。
    """
    n = len(strokes)
    flags = [False] * n
    candidate = [
        i for i, s in enumerate(strokes)
        if s.role != "glyph" and len(s.points) >= 2
        and s.length() >= _STRUCTURAL_MIN_MM
    ]
    if len(candidate) < 2:
        return flags
    for i, j in _hash_junctions(strokes, candidate, _JUNCTION_TOL_MM):
        flags[i] = flags[j] = True
    return flags


def _fit_wavelength(wavelength: float, length: float,
                    min_waves: float = _STRUCTURAL_MIN_WAVES) -> float:
    """把起伏波长收敛到「一条线里至少铺 ``min_waves`` 个波」。

    预设波长按字号设定（可达数十毫米），用在十几毫米的表格线/边框上连
    一个波都铺不满，起伏退化成整条线单向倾斜——看上去像被斜切。按线长
    限制波长后，短线上也能出现完整的手绘起伏。
    """
    wl = wavelength if wavelength > 1e-6 else 1.0
    if length <= 1e-9:
        return wl
    return min(wl, length / max(1.0, min_waves))


def _wobble_stroke(s: Stroke, rng: random.Random, amplitude: float,
                   wavelength: float, octaves: int = 4,
                   tremor: float = 0.0) -> Stroke:
    """让一条笔画产生「人手画线」的平滑抖动。

    与旧实现的关键区别：

    * 位移沿笔画的**法向**（垂直于走向）施加，而不是在 x/y 轴向上各自加正弦。
      法向抖动才像人手「画歪了一点」，轴向上的偏移更像整条线在平移。
    * 抖动来自**平滑噪声**（见 :func:`_value_noise`），低频、连贯，不会出现
      逐点独立的毛刺。
    * 端点用 taper 渐隐到原位，保证相邻笔画/闭合图形接缝处不错开；
      taper 按弧长参数计算，长短段相间的笔画渐隐节奏一致。
    * 长直段（如表格线、2 点线段）先按弧长细分再施加噪声，直线也能画弯。
    * 可选 ``tremor``：叠加一层高频、小振幅的细微颤抖，模拟手部肌肉的微抖。
    """
    pts = s.points
    n = len(pts)
    if n < 2 or (amplitude <= 0.0 and tremor <= 0.0):
        return s.clone()
    wavelength = wavelength if wavelength > 1e-6 else 1.0
    # 采样步长取波长的 ~1/7：足够表现波形，又不显著膨胀点数
    pts = _resample_long_segments(pts, max(1.0, wavelength * 0.15))
    n = len(pts)
    arc = _arc_lengths(pts)
    total = arc[-1]
    if total <= 1e-9:
        return s.clone()

    main = _value_noise(rng, arc, wavelength, octaves) if amplitude > 0.0 else None
    fine = _value_noise(rng, arc, max(0.6, wavelength * 0.08), 2) \
        if tremor > 0.0 else None

    new_pts: list[Vec2] = []
    for i, (x, y) in enumerate(pts):
        # 局部切线（中心差分；端点用单侧差分）
        if i == 0:
            tx, ty = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        elif i == n - 1:
            tx, ty = pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1]
        else:
            tx, ty = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        tl = math.hypot(tx, ty)
        if tl < 1e-9:
            tx, ty = 1.0, 0.0
        else:
            tx, ty = tx / tl, ty / tl
        nx, ny = -ty, tx          # 法向
        t = arc[i] / total        # 弧长参数（端点 0/1，taper 渐隐到原位）
        taper = min(1.0, 4.0 * t * (1.0 - t)) if n > 2 else 1.0
        off = 0.0
        if main is not None:
            off += amplitude * main[i]
        if fine is not None:
            off += tremor * fine[i]
        off *= taper
        new_pts.append((x + nx * off, y + ny * off))
    return Stroke(new_pts, s.closed, s.role, s.group)


def _add_flare(s: Stroke, flare: float) -> Stroke:
    """给笔画端部加「笔锋」：起笔斜切入笔、收笔甩出渐细的尖。

    固定宽度笔写不出真正的粗细渐变，靠端部形状模拟：
    * 收笔（出锋）：沿出笔方向甩出长约 flare 的微曲尖——单线自由末端
      在纸上自然显尖，像毛笔收笔的提锋；
    * 起笔（尖入）：入笔点沿反方向退 flare 并沿法向偏移 1/4 flare，
      斜着切入笔画，像毛笔的尖锋入纸。

    短笔画（总长 < 3×flare）与闭合笔画不加，避免糊成团。
    """
    pts = s.points
    if s.closed or flare <= 0.0 or len(pts) < 2:
        return s
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1])
                for a, b in zip(pts, pts[1:]))
    if total < flare * 3.0:
        return s.clone()

    def _dir(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        dl = math.hypot(dx, dy)
        return (dx / dl, dy / dl) if dl > 1e-9 else (1.0, 0.0)

    # 起笔：斜切入笔
    d_in = _dir(pts[0], pts[1])
    nx, ny = -d_in[1], d_in[0]
    entry = (pts[0][0] - d_in[0] * flare + nx * flare * 0.25,
             pts[0][1] - d_in[1] * flare + ny * flare * 0.25)
    # 收笔：甩出微曲的尖（中段带一点法向偏移，末端再偏一点，像提锋）
    d_out = _dir(pts[-2], pts[-1])
    tx, ty = -d_out[1], d_out[0]
    mid = (pts[-1][0] + d_out[0] * flare * 0.55 + tx * flare * 0.12,
           pts[-1][1] + d_out[1] * flare * 0.55 + ty * flare * 0.12)
    tip = (pts[-1][0] + d_out[0] * flare + tx * flare * 0.30,
           pts[-1][1] + d_out[1] * flare + ty * flare * 0.30)
    return Stroke([entry] + list(pts) + [mid, tip], False, s.role, s.group)


def _finish_stroke(s: Stroke, rng: random.Random, params: PerturbParams,
                   structural: bool = False) -> Stroke:
    """单笔画的非刚性修饰链：伸缩 → 末端修剪 → 起伏 → 平滑 → 笔锋。

    供文本与通用两条扰动路径共用；新随机量的取用都带「参数为 0 就不
    消耗 RNG」守卫，未启用新参数的老参数集输出与旧版完全一致。

    线条起伏/细微颤抖（弯折）只作用于**图形线条**：字形笔画
    （``role="glyph"``，来自字体排版）跳过起伏——文字的笔画形态由字体
    本身和其它扰动参数决定，弯折会让文字变形。起伏内部的「长直段细分」
    也随之跳过，字形笔画不做无谓的重采样。

    ``structural=True``（结构长线：表格线/边框/下划线）时，跳过伸缩、
    末端修剪与笔锋——这些都会改变端点，等于把线条从结构接缝处撕开；
    只保留平滑起伏，且起伏波长收敛到线长以内，短线上也有完整波形。
    随机数仍照常取用，保证序列不受分类影响。
    """
    is_glyph = s.role == "glyph"
    f = params.intensity_factor
    stretch = _gauss(rng, params.stroke_stretch_sigma * f)
    trim = _scaled(params.stroke_trim_mm, params)
    trim_u1 = trim_u2 = 0.0
    if trim > 0.0:
        trim_u1 = rng.uniform(0.0, trim)
        trim_u2 = rng.uniform(0.0, trim)
    if structural:
        ps = s.clone()
    else:
        ps = _stretch_stroke(s, 1.0 + stretch)
        if trim > 0.0:
            ps = _trim_stroke(ps, trim_u1, trim_u2)
    if not is_glyph:
        wl = params.line_wobble_wavelength
        if structural:
            wl = _fit_wavelength(wl, ps.length())
        ps = _wobble_stroke(ps, rng, params.line_wobble * f, wl,
                            tremor=params.line_tremor * f)
    if params.smoothing > 0.0:
        ps = smooth_stroke(ps, params.smoothing)
    if not structural and params.flare_mm > 0.0:
        ps = _add_flare(ps, params.flare_mm)
    return ps


# ---------------------------------------------------------------------------
# 文本排版扰动
# ---------------------------------------------------------------------------
# 词边界字符：空白 + 中西文常用标点。词/标点之后是人手自然的「顿笔」位置：
# 字距节奏在此重置（词内聚拢），并可选留出一点词间空隙。
_WORD_BREAK_CHARS = frozenset(
    " \t\u3000"
    "，。、；：！？…—·“”‘’（）《》〈〉【】〔〕"
    ",.;:!?)]}>\"'"
)

# 字距节奏模型（取代旧版的无界随机游走——游走会让长行越排越散/越挤）：
#   * 沿行平滑低频起伏（主分量）：整行前后呼应的松紧「呼吸」；
#   * AR(1) 均值回归节奏（逐字呼应，方差有界，不会漂移）；
#   * 逐字独立的高频分量仍由 char_x_sigma 提供（小幅）。
_SPACING_RHO = 0.65        # AR(1) 回归系数：越大节奏越「黏」，0 退化为白噪声
_SPACING_AR_SIGMA = 0.7    # AR(1) 驱动 σ（× char_spacing_sigma）
_SPACING_LF_AMP = 1.5      # 低频噪声振幅（× char_spacing_sigma）
_SPACING_LF_WL_EM = 4.0    # 低频波长（×字号 em），实际取 max(15mm, 该值)


def _is_word_break(ch: str) -> bool:
    return ch in _WORD_BREAK_CHARS


def _along_pos(line, c, horizontal: bool) -> float:
    """字符沿书写方向的弧长坐标（供沿行的低频字距噪声取值）。"""
    if horizontal:
        return c.origin[0] - getattr(line, "x_start", 0.0)
    return getattr(line, "baseline_y", 0.0) - c.origin[1]


def perturb_layout(layout, params: PerturbParams,
                   seed: Optional[int] = None) -> PerturbResult:
    """对排版结果施加扰动，返回新的页面坐标笔画。

    ``layout`` 需提供 ``.lines``，每行含 ``.chars``（``.origin`` / ``.strokes`` /
    ``.advance`` / ``.font_name`` / ``.char``）与 ``.x_start``。
    """
    result = PerturbResult()
    if not params.is_active():
        for line in layout.lines:
            for c in line.chars:
                result.strokes.extend(s.clone() for s in c.strokes)
                result.chars.append(CharPerturb(c.char, c.font_name))
        return result

    rng = random.Random(params.seed if seed is None else seed)
    f = params.intensity_factor
    style_size = float(getattr(layout.style, "size", 10.0) or 10.0)
    horizontal = getattr(layout.style, "direction", "h") == "h"

    for line in layout.lines:
        line_y_off = _gauss(rng, params.line_y_sigma * f)
        line_start_dx = _gauss(rng, params.line_start_sigma * f)
        # 行尾落差：行尾相对行首的高度漂移（人写的每行都不水平）
        tilt = _gauss(rng, params.line_tilt_sigma * f)
        # 基线游走：行尺度的随机缓弧（起讫高度独立、每行形状全新）+
        # 用户设定的中尺度起伏。取代固定周期/固定振幅的正弦——正弦的
        # 周期性一眼就能看出来，且必然回到起点，没有「行在漂」的感觉。
        base_vals: Optional[list[float]] = None
        mid_vals: Optional[list[float]] = None
        line_len = 0.0
        if ((params.sine_amplitude > 1e-12 or params.line_tilt_sigma > 1e-12)
                and line.chars):
            xs = [_along_pos(line, c, horizontal) for c in line.chars]
            line_len = max(getattr(line, "width", 0.0),
                           xs[-1] if xs else 0.0, 1e-6)
            if params.sine_amplitude > 1e-12:
                base_vals = _value_noise(rng, xs, line_len, 1)
                mid_wl = min(max(params.sine_wavelength, 15.0), line_len)
                mid_vals = _value_noise(rng, xs, mid_wl, 1)
        prev_ink_right: Optional[float] = None   # 已排前字的墨迹右缘（含抖动）
        # 字距节奏的均值回归状态（词边界处重置）
        spacing_state = 0.0
        # 沿行的平滑低频松紧（主分量）：整行一次性生成，前后自然呼应
        lf_vals: Optional[list[float]] = None
        if params.char_spacing_sigma > 1e-12 and line.chars:
            wl = max(15.0, _SPACING_LF_WL_EM * style_size)
            lf_vals = _value_noise(
                rng, [_along_pos(line, c, horizontal) for c in line.chars],
                wl, 1)

        for ci, c in enumerate(line.chars):
            size_factor = 1.0 + _gauss(rng, params.size_sigma * f)
            size_factor = max(0.2, min(5.0, size_factor))

            # 字符墨迹相对基线原点的横向范围（用于防重叠钳制）
            min_rel = max_rel = 0.0
            ink_h = 0.0
            if c.strokes:
                xs_all = [p[0] for st_ in c.strokes for p in st_.points]
                ys_all = [p[1] for st_ in c.strokes for p in st_.points]
                min_rel = min(xs_all) - c.origin[0]
                max_rel = max(xs_all) - c.origin[0]
                ink_h = max(ys_all) - min(ys_all)

            # 词/标点之后：节奏重置（词内紧，不跨词携带松紧）；
            # 可选再留一点词间空隙（词间松）
            word_start = (ci > 0 and _is_word_break(line.chars[ci - 1].char)
                          and not _is_word_break(c.char))
            if word_start:
                spacing_state = 0.0
            rhythm = spacing_state
            if lf_vals is not None:
                rhythm += (_SPACING_LF_AMP * params.char_spacing_sigma * f
                           * lf_vals[ci])
            if word_start and params.word_gap_em > 1e-12:
                rhythm += (params.word_gap_em * style_size * f
                           * rng.uniform(0.75, 1.25))

            dx_white = _gauss(rng, params.char_x_sigma * f)
            if horizontal:
                dx = dx_white + rhythm
            else:
                dx = dx_white            # 竖排：字距节奏沿列（dy）方向
            if ci == 0:
                dx += line_start_dx
            # 防重叠钳制：随机字距/字抖动只准把字推开、不准拉近——
            # 本字墨迹左缘不得越过前字墨迹右缘 + 呼吸间隙
            if horizontal and c.strokes and prev_ink_right is not None:
                gap = max(0.15, 0.03 * ink_h)
                left = c.origin[0] + dx + min_rel * size_factor
                if left < prev_ink_right + gap:
                    dx = prev_ink_right + gap - c.origin[0] \
                        - min_rel * size_factor

            # 基线：行尾落差（随沿行位置线性变化）+ 行尺度缓弧 + 中尺度游走
            base_dy = 0.0
            if line_len > 0.0:
                base_dy += tilt * (_along_pos(line, c, horizontal) / line_len)
            if base_vals is not None:
                base_dy += params.sine_amplitude * f * (
                    base_vals[ci] + 0.5 * mid_vals[ci])

            dy = _gauss(rng, params.char_y_sigma * f) + line_y_off
            if horizontal:
                dy += base_dy
            else:
                dx += base_dy            # 竖排：基线游走垂直于列（横向摆）
                dy -= rhythm             # 竖排：字距沿列推进方向（-Y）
            rot = _gauss(rng, params.char_rot_sigma * f)
            shear = _gauss(rng, params.char_shear_sigma * f)

            new_strokes = _transform_points(
                c.strokes, c.origin, size_factor, rot, dx, dy, shear)

            for s in new_strokes:
                ps = _perturb_stroke_once(s, rng, params)
                ps = _finish_stroke(ps, rng, params)
                result.strokes.append(ps)

            result.chars.append(CharPerturb(
                char=c.char, font_name=c.font_name, size_factor=size_factor,
                dx=dx, dy=dy, rotation=rot, shear=shear, sine_dy=base_dy,
            ))
            if horizontal and c.strokes:
                prev_ink_right = c.origin[0] + dx + max_rel * size_factor
            # AR(1) 均值回归：保留逐字节奏，方差有界、不随行长漂移
            spacing_state = (_SPACING_RHO * spacing_state
                             + _gauss(rng, _SPACING_AR_SIGMA
                                      * params.char_spacing_sigma * f))

    result.strokes = _maybe_simplify(result.strokes, params)
    return result


# ---------------------------------------------------------------------------
# 通用笔画扰动（矢量图 / 自由手绘）
# ---------------------------------------------------------------------------
def perturb_strokes(strokes: Iterable[Stroke], params: PerturbParams,
                    seed: Optional[int] = None) -> list[Stroke]:
    """对任意笔画集合施加手写扰动（整笔刚性位移/旋转 + 平滑手绘抖动）。

    这是「让任意对象都有人手笔迹感」的统一入口：无论笔画来自字体、公式、
    SVG 还是自由手绘，都会逐笔叠加上一次平滑抖动。
    """
    strokes = list(strokes)
    if not params.is_active():
        return [s.clone() for s in strokes]
    rng = random.Random(params.seed if seed is None else seed)
    structural = _is_structural_strokes(strokes)
    out: list[Stroke] = []
    for s, is_struct in zip(strokes, structural):
        ps = _perturb_stroke_once(s, rng, params, apply_rigid=not is_struct)
        ps = _finish_stroke(ps, rng, params, structural=is_struct)
        out.append(ps)
    return _maybe_simplify(out, params)


def wobble_strokes(strokes: Iterable[Stroke], amplitude: float,
                   wavelength: float = 20.0, seed: Optional[int] = None,
                   octaves: int = 3, tremor: float = 0.0) -> list[Stroke]:
    """「手绘抖动」：沿笔画法向叠加平滑多频噪声，模拟手画的线条。

    :func:`perturb_strokes` 是更完整的入口（含整笔位移/旋转）；本函数只做
    线条本身的平滑起伏，适合矢量图/签名/边框的纯手绘感。
    字形笔画（``role="glyph"``）不受影响——弯折只作用于图形线条，
    不作用于文字。
    """
    strokes = list(strokes)
    if amplitude <= 0.0 and tremor <= 0.0:
        return [s.clone() for s in strokes]
    rng = random.Random(seed if seed is not None else 0)
    return [s.clone() if s.role == "glyph"
            else _wobble_stroke(s, rng, amplitude, wavelength, octaves, tremor)
            for s in strokes]
