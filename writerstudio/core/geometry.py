"""几何基础：点、包围盒、仿射变换。

坐标约定（全项目统一，**非常重要**）：
    * 文档坐标 = 页面坐标，单位 **毫米(mm)**，**Y 轴向上**（与机械/G-code 一致）。
    * 旋转正方向为逆时针（数学惯例）。
    * 画布视图负责把 Y 翻转到屏幕方向，文档层不做任何翻转。
这样导出 G-code 时无需再做 Y 轴换运算，用户设置写字起点也更直观。

本模块为纯 Python，不依赖 Qt，便于单元测试。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

Vec2 = tuple[float, float]
"""二维点/向量，用元组表示 (x, y)。"""

EPS = 1e-9


# ---------------------------------------------------------------------------
# 包围盒
# ---------------------------------------------------------------------------
@dataclass
class BBox:
    """轴对齐包围盒。默认空盒（x0>x1）便于逐步 union。"""

    x0: float = math.inf
    y0: float = math.inf
    x1: float = -math.inf
    y1: float = -math.inf

    @property
    def is_empty(self) -> bool:
        return self.x0 > self.x1 or self.y0 > self.y1

    @property
    def width(self) -> float:
        return 0.0 if self.is_empty else self.x1 - self.x0

    @property
    def height(self) -> float:
        return 0.0 if self.is_empty else self.y1 - self.y0

    @property
    def center(self) -> Vec2:
        if self.is_empty:
            return (0.0, 0.0)   # 空盒避免 (inf + -inf)/2 = nan 污染下游
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    def expand(self, p: Vec2) -> None:
        """把点纳入盒内（原地）。"""
        x, y = p
        if x < self.x0:
            self.x0 = x
        if y < self.y0:
            self.y0 = y
        if x > self.x1:
            self.x1 = x
        if y > self.y1:
            self.y1 = y

    @classmethod
    def from_points(cls, points: Iterable[Vec2]) -> BBox:
        # 热路径（字库加载/SVG 归一化逐字形、逐笔画调用）：内联展开，
        # 不走 expand() 方法派发
        x0 = y0 = math.inf
        x1 = y1 = -math.inf
        for p in points:
            x = p[0]
            y = p[1]
            if x < x0:
                x0 = x
            if y < y0:
                y0 = y
            if x > x1:
                x1 = x
            if y > y1:
                y1 = y
        return cls(x0, y0, x1, y1)

    def union(self, other: BBox) -> BBox:
        if other.is_empty:
            return self
        if self.is_empty:
            return other
        return BBox(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


# ---------------------------------------------------------------------------
# 仿射变换
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AffineTransform:
    """2D 仿射变换，用 6 参数表示（SVG/PostScript 约定）：

        | a  c  e |
        | b  d  f |
        | 0  0  1 |

    点变换：x' = a*x + c*y + e ,  y' = b*x + d*y + f
    该约定与 Qt 的 QTransform(m11,m12,m21,m22,dx,dy) 完全一致，
    即 a=m11, b=m12, c=m21, d=m22, e=dx, f=dy，可直接互转。
    """

    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    e: float = 0.0
    f: float = 0.0

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def identity(cls) -> AffineTransform:
        return cls()

    @classmethod
    def from_sequence(cls, values) -> "AffineTransform":
        """从 6 分量序列构造；长度不对直接报错（避免静默错位填充）。"""
        vs = [float(v) for v in values]
        if len(vs) != 6:
            raise ValueError(f"transform 需要 6 个分量，实际 {len(vs)} 个")
        return cls(*vs)

    @classmethod
    def translate(cls, tx: float, ty: float) -> AffineTransform:
        return cls(1.0, 0.0, 0.0, 1.0, tx, ty)

    @classmethod
    def scale(cls, sx: float, sy: float | None = None) -> AffineTransform:
        if sy is None:
            sy = sx
        return cls(sx, 0.0, 0.0, sy, 0.0, 0.0)

    @classmethod
    def rotate(cls, degrees: float) -> AffineTransform:
        r = math.radians(degrees)
        cos_r, sin_r = math.cos(r), math.sin(r)
        return cls(cos_r, sin_r, -sin_r, cos_r, 0.0, 0.0)

    # -- 应用 ---------------------------------------------------------------
    def apply(self, p: Vec2) -> Vec2:
        x, y = p
        return (self.a * x + self.c * y + self.e, self.b * x + self.d * y + self.f)

    def apply_many(self, points: Iterable[Vec2]) -> list[Vec2]:
        a, b, c, d, e, f = self.a, self.b, self.c, self.d, self.e, self.f
        return [(a * x + c * y + e, b * x + d * y + f) for x, y in points]

    def apply_vector(self, v: Vec2) -> Vec2:
        """只应用线性部分（忽略平移），用于方向/法向量。"""
        x, y = v
        return (self.a * x + self.c * y, self.b * x + self.d * y)

    # -- 组合/求逆 ----------------------------------------------------------
    def __matmul__(self, other: AffineTransform) -> AffineTransform:
        """self @ other ：先应用 other，再应用 self（矩阵乘法 self·other）。"""
        return AffineTransform(
            a=self.a * other.a + self.c * other.b,
            b=self.b * other.a + self.d * other.b,
            c=self.a * other.c + self.c * other.d,
            d=self.b * other.c + self.d * other.d,
            e=self.a * other.e + self.c * other.f + self.e,
            f=self.b * other.e + self.d * other.f + self.f,
        )

    def then(self, other: AffineTransform) -> AffineTransform:
        """先应用 self，再应用 other（= other @ self，更符合阅读顺序）。"""
        return other @ self

    def determinant(self) -> float:
        return self.a * self.d - self.b * self.c

    def invert(self) -> AffineTransform:
        det = self.determinant()
        if abs(det) < EPS:
            raise ValueError("矩阵不可逆（行列式接近 0）")
        inv_det = 1.0 / det
        a, b, c, d, e, f = self.a, self.b, self.c, self.d, self.e, self.f
        return AffineTransform(
            a=d * inv_det,
            b=-b * inv_det,
            c=-c * inv_det,
            d=a * inv_det,
            e=(c * f - d * e) * inv_det,
            f=(b * e - a * f) * inv_det,
        )

    def as_tuple(self) -> tuple[float, float, float, float, float, float]:
        return (self.a, self.b, self.c, self.d, self.e, self.f)

    # -- 便捷构造（围绕某点操作） -------------------------------------------
    @classmethod
    def rotate_about(cls, degrees: float, origin: Vec2) -> AffineTransform:
        """绕 origin 旋转 degrees 度（逆时针）。"""
        ox, oy = origin
        return cls.translate(ox, oy) @ cls.rotate(degrees) @ cls.translate(-ox, -oy)

    @classmethod
    def scale_about(cls, sx: float, sy: float, origin: Vec2) -> AffineTransform:
        """以 origin 为不动点缩放。"""
        ox, oy = origin
        return cls.translate(ox, oy) @ cls.scale(sx, sy) @ cls.translate(-ox, -oy)

    @classmethod
    def shear_x_about(cls, k: float, origin: Vec2) -> AffineTransform:
        """以 origin 为不动点做水平斜切：x' = x + k·y（k=tan(倾角)）。

        用于字体的「体态斜切」——整体倾斜度随纵向位置线性变化，
        旋转做不到这一点（旋转保持正交，斜切才是体态变化）。
        """
        ox, oy = origin
        return cls.translate(ox, oy) @ cls(1.0, 0.0, k, 1.0, 0.0, 0.0) \
            @ cls.translate(-ox, -oy)


def rdp_simplify(points: list[Vec2], tol: float) -> list[Vec2]:
    """Douglas–Peucker 抽稀（迭代实现，保首尾点）。

    ``tol <= 0`` 或点数 < 3 时原样返回。G-code 导出抽稀（垂直距离
    容差抽稀）与扰动输出的「抽稀容差」参数共用。
    """
    if tol <= 0 or len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = points[i]
        bx, by = points[j]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        dmax = -1.0
        imax = -1
        for m in range(i + 1, j):
            px, py = points[m]
            if seg2 <= 1e-18:
                d = math.hypot(px - ax, py - ay)
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                t = max(0.0, min(1.0, t))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > dmax:
                dmax = d
                imax = m
        if dmax > tol:
            keep[imax] = True
            stack.append((i, imax))
            stack.append((imax, j))
    return [p for p, k in zip(points, keep) if k]
