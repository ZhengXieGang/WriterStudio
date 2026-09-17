"""笔序优化：决定笔画的书写先后。

提供两种策略：

* :func:`optimize_order` —— **最短空程**：贪心最近邻 + 允许笔画反向，
  从当前位置出发反复选择最近的笔画端点。空程最短，但会打乱书写顺序
  （一会儿写这儿一会儿写那儿），适合矢量图/签名等无「阅读顺序」的内容。
* :func:`order_reading` —— **阅读顺序**（常规书写顺序）：
  按分组（每个字符/每个对象一组）书写，**写完一组再写下一组**；组内保持
  自然顺序（文字即逐字阅读顺序：从左到右、从上到下），组间按位置先上后下、
  先左后右排序。文字内容用这一种，读起来才自然。

:func:`order_for_writing` 按分组信息自动选择：有分组时用阅读顺序，否则回退
到最短空程。
"""

from __future__ import annotations

import math
from typing import Iterable, Optional, Sequence

from ..core.strokes import Stroke


def _d2(a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


class _EndpointGrid:
    """笔画端点的空间网格索引，供最近邻/邻域查询，替代 O(n²) 逐对扫描。

    千笔级的自由手绘/矢量图在「生成 G-code」时按贪心最近邻排笔序，纯
    Python 逐对比较是 O(n²)（3000 笔约 3 秒，界面冻结）；网格把查询降到
    邻域环扩展，结果与逐对扫描**严格一致**（同样的比较优先级）。
    """

    __slots__ = ("cell", "buckets", "count", "_kmin", "_kmax")

    def __init__(self, cell: float) -> None:
        self.cell = max(float(cell), 1e-6)
        self.buckets: dict[tuple[int, int], list] = {}
        self.count = 0
        # 已占格子的键包围盒（删除不收缩，仅作查询上限的保守估计）
        self._kmin: Optional[tuple[int, int]] = None
        self._kmax: Optional[tuple[int, int]] = None

    @staticmethod
    def for_points(points: Sequence[tuple[float, float]]) -> "_EndpointGrid":
        """按点云密度选格子边长（平均每格约 2~4 个端点）。

        面积公式在退化布局（全部共线/共点）下会把格子算得过小，环扩展
        要跨越海量空格子；与「周长/√n」取较大者，共线时也能保持每格
        少量端点。
        """
        if not points:
            return _EndpointGrid(1.0)
        n = max(1, len(points))
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        cell = max(math.sqrt(max(w * h, 0.0) / n) * 1.5,
                   (w + h) / (2.0 * math.sqrt(n) + 1.0))
        return _EndpointGrid(cell)

    def _key(self, p) -> tuple[int, int]:
        return (int(p[0] // self.cell), int(p[1] // self.cell))

    def add(self, p, item) -> None:
        key = self._key(p)
        self.buckets.setdefault(key, []).append(item)
        self.count += 1
        if self._kmin is None:
            self._kmin = key
            self._kmax = key
        else:
            self._kmin = (min(self._kmin[0], key[0]),
                          min(self._kmin[1], key[1]))
            self._kmax = (max(self._kmax[0], key[0]),
                          max(self._kmax[1], key[1]))

    def remove(self, p, item) -> None:
        b = self.buckets.get(self._key(p))
        if b is not None and item in b:
            b.remove(item)
            self.count -= 1
            if not b:
                del self.buckets[self._key(p)]

    def _ring_keys(self, cx: int, cy: int, k: int):
        """切比雪夫距离恰为 k 的环上的所有格子。"""
        if k == 0:
            yield (cx, cy)
            return
        for dx in range(-k, k + 1):
            yield (cx + dx, cy - k)
            yield (cx + dx, cy + k)
        for dy in range(-k + 1, k):
            yield (cx - k, cy + dy)
            yield (cx + k, cy + dy)

    def nearest(self, p):
        """全局最近条目，返回 ``(item, d2)``；空返回 ``(None, inf)``。

        环扩展：扫完第 k 环后，若当前最优距离 ≤ k·cell（更远的环不可能
        更近），即可停；环越过包围盒后也不可能有格子了。并列距离按
        ``(idx, rev)`` 决胜，与逐对扫描（原始顺序靠前、正向优先）完全一致。
        """
        if self.count == 0:
            return None, math.inf
        cx, cy = self._key(p)
        kmax = max(cx - self._kmin[0], self._kmax[0] - cx,
                   cy - self._kmin[1], self._kmax[1] - cy)
        best_item = None
        best_d2 = math.inf
        k = 0
        while k <= kmax + 1:
            for key in self._ring_keys(cx, cy, k):
                for item in self.buckets.get(key, ()):
                    d2 = _d2(p, (item[0], item[1]))
                    if d2 < best_d2 or (d2 == best_d2
                                        and item[2:] < best_item[2:]):
                        best_d2 = d2
                        best_item = item
            if best_d2 <= (k * self.cell) ** 2:
                break
            k += 1
            if k > 64:
                # 极端不均匀的布局（格子相对点距过密）环扩展代价失控：
                # 退化为全桶线性扫描（与旧 O(n²) 同阶，仅此查询变慢）
                for b in self.buckets.values():
                    for item in b:
                        d2 = _d2(p, (item[0], item[1]))
                        if d2 < best_d2 or (d2 == best_d2
                                            and item[2:] < best_item[2:]):
                            best_d2 = d2
                            best_item = item
                break
        return best_item, best_d2

    def within(self, p, radius: float) -> list:
        """半径内所有条目（方块范围按半径/格长换算；过大时退化为全桶扫描）。"""
        out = []
        cx, cy = self._key(p)
        r = int(radius // self.cell) + 1
        if (2 * r + 1) ** 2 > 9 * max(1, len(self.buckets)):
            for b in self.buckets.values():
                for item in b:
                    if _d2(p, (item[0], item[1])) <= radius * radius:
                        out.append(item)
            return out
        for gx in range(cx - r, cx + r + 1):
            for gy in range(cy - r, cy + r + 1):
                for item in self.buckets.get((gx, gy), ()):
                    if _d2(p, (item[0], item[1])) <= radius * radius:
                        out.append(item)
        return out


def optimize_order(strokes: Sequence[Stroke],
                   start: Optional[tuple[float, float]] = None,
                   allow_reverse: bool = True,
                   tolerance: float = 0.2) -> list[Stroke]:
    """返回重排后的笔画列表（新对象，不修改输入）。

    ``start`` 为起始点（默认用第一笔起点）。``allow_reverse`` 允许反向绘制。
    ``tolerance`` 内首尾相接的笔画会被合并为一条（避免反复抬落笔）。

    贪心最近邻用端点网格加速：选择的笔画与顺序和逐对扫描完全一致
    （并列距离时取原始顺序靠前者、正向优先），只是查询从 O(n) 降到邻域。
    """
    items: list[Stroke] = [s.clone() for s in strokes if len(s.points) >= 2]
    if not items:
        return []

    # 1) 合并首尾相接的笔画（小容差）
    merged = _merge_touching(items, tolerance)

    # 2) 贪心最近邻排序（网格加速，语义与逐对扫描一致）
    pts_all = [pt for s in merged for pt in (s.points[0], s.points[-1])]
    grid = _EndpointGrid.for_points(pts_all)
    alive: dict[int, Stroke] = {}
    for idx, s in enumerate(merged):
        alive[idx] = s
        grid.add(s.points[0], (s.points[0][0], s.points[0][1], idx, 0))
        if allow_reverse:
            grid.add(s.points[-1], (s.points[-1][0], s.points[-1][1], idx, 1))

    def _drop(idx: int, s: Stroke) -> None:
        alive.pop(idx, None)
        grid.remove(s.points[0], (s.points[0][0], s.points[0][1], idx, 0))
        grid.remove(s.points[-1], (s.points[-1][0], s.points[-1][1], idx, 1))

    cur = start if start is not None else merged[0].points[0]
    ordered: list[Stroke] = []
    while alive:
        item, _ = grid.nearest(cur)
        if item is None:
            break
        _, _, idx, rev = item
        s = alive[idx]
        _drop(idx, s)
        if rev:
            s = Stroke(list(reversed(s.points)), s.closed, s.role, s.group)
        ordered.append(s)
        cur = s.points[-1]
    return ordered


def _group_sort_key(items: list[Stroke]) -> tuple[float, float]:
    """分组的阅读顺序键：先上后下（-top，Y 向上故取负），再先左后右。"""
    xs0 = min(s.bbox().x0 for s in items)
    ys1 = max(s.bbox().y1 for s in items)
    # 行内按 top 分组：容差 ~2mm 视为同一行（避免基线细微差异导致重排）
    return (-round(ys1 / 2.0) * 2.0, xs0)


def order_reading(strokes: Sequence[Stroke],
                  groups: Sequence[int],
                  start: Optional[tuple[float, float]] = None,
                  tolerance: float = 0.2) -> list[Stroke]:
    """阅读顺序书写：**写完一组再写下一组**，组内保持自然顺序。

    ``groups`` 与 ``strokes`` 等长，标记每条笔画的所属分组（如一个字符 /
    一个对象）。组内不重排（文字笔画本就是逐字阅读顺序），仅做相接合并；
    组间按页面位置先上后下、先左后右排序。这样文字会被「从左到右、从上到下」
    连贯写出，而不是全局贪心式的到处跳。
    """
    items = [s.clone() for s in strokes if len(s.points) >= 2]
    gs = [g for s, g in zip(strokes, groups) if len(s.points) >= 2]
    if not items:
        return []

    buckets: dict[int, list[Stroke]] = {}
    for s, g in zip(items, gs):
        buckets.setdefault(g, []).append(s)

    ordered_groups = sorted(buckets.values(), key=_group_sort_key)
    out: list[Stroke] = []
    for group in ordered_groups:
        # 组内合并相接笔画（保持顺序），但绝不跨组
        out.extend(_merge_touching(group, tolerance))
    return out


def order_for_writing(strokes: Sequence[Stroke],
                      groups: Optional[Sequence[int]] = None,
                      start: Optional[tuple[float, float]] = None,
                      tolerance: float = 0.2,
                      mode: str = "reading") -> list[Stroke]:
    """按分组信息选择书写顺序。

    ``mode="reading"`` 且有分组时用 :func:`order_reading`（文字自然顺序）；
    否则回退到 :func:`optimize_order`（最短空程）。
    """
    if mode == "reading" and groups is not None and any(g >= 0 for g in groups):
        return order_reading(strokes, groups, start=start, tolerance=tolerance)
    return optimize_order(strokes, start=start, tolerance=tolerance)


def _merge_touching(strokes: list[Stroke], tolerance: float) -> list[Stroke]:
    """把首尾相邻的开放笔画接成更长的笔画（贪心，端点网格加速）。

    合并保留 ``role``（字形/图形）与 ``group``（书写分组），否则后续按
    分组排序会丢失归属；同一链内的笔画本就同组。

    挑选规则与逐个扫描版一致：从链尾出发，取「原始顺序最靠前」的未用
    笔画（同笔先试正向起点、再试反向终点），首/尾落在 ``tolerance`` 内
    即接上，直到接不动为止。
    """
    if tolerance <= 0:
        return strokes
    open_strokes = [s for s in strokes if not s.closed]
    closed = [s for s in strokes if s.closed]
    result: list[Stroke] = []

    pts_all = [pt for s in open_strokes for pt in (s.points[0], s.points[-1])]
    grid = _EndpointGrid.for_points(pts_all) if pts_all else None
    if grid is None:
        result.extend(closed)
        return result
    for idx, t in enumerate(open_strokes):
        grid.add(t.points[0], (t.points[0][0], t.points[0][1], idx, 0))
        grid.add(t.points[-1], (t.points[-1][0], t.points[-1][1], idx, 1))

    used: set[int] = set()

    def _drop(idx: int, t: Stroke) -> None:
        used.add(idx)
        grid.remove(t.points[0], (t.points[0][0], t.points[0][1], idx, 0))
        grid.remove(t.points[-1], (t.points[-1][0], t.points[-1][1], idx, 1))

    for i, s in enumerate(open_strokes):
        if i in used:
            continue
        _drop(i, s)
        chain = list(s.points)
        # 向后接：取原始顺序最靠前的未用笔画，正向（起点）优先
        while True:
            cands = grid.within(chain[-1], tolerance)
            if not cands:
                break
            cands.sort(key=lambda e: (e[2], e[3]))
            _, _, j, is_end = cands[0]
            t = open_strokes[j]
            if is_end:
                chain.extend(list(reversed(t.points))[1:])
            else:
                chain.extend(t.points[1:])
            _drop(j, t)
        result.append(Stroke(chain, False, s.role, s.group))
    result.extend(closed)
    return result


def total_travel(strokes: Sequence[Stroke],
                 start: Optional[tuple[float, float]] = None) -> float:
    """估算空程总长（含落笔前与抬笔后的移动）。"""
    if not strokes:
        return 0.0
    cur = start if start is not None else strokes[0].points[0]
    travel = 0.0
    for s in strokes:
        travel += math.hypot(cur[0] - s.points[0][0], cur[1] - s.points[0][1])
        cur = s.points[-1]
    return travel


def draw_length(strokes: Iterable[Stroke]) -> float:
    return sum(s.length() for s in strokes)
