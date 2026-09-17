"""笔画编辑工具：在单个对象内高亮、选中并自由调整单条或多条笔画。

进入方式：工具栏「笔画编辑」（只在显式进入时生效，退出即恢复普通交互，
不影响普通选择/移动/缩放）。

能力：
    * 点选一条笔画（沿线抓取容差随缩放恒定 ≈8px）并高亮；
    * **框选**批量选中（空白处拖出橡皮框；Shift 为追加选择）；
    * 拖动选中笔画平移（多选时一起移动）；四角手柄缩放（Shift 等比）；
      顶部手柄绕中心旋转——手柄仅在**单选**时出现，且只在按到手的
      距离比到墨迹更近（即按在空白处的手柄上）时接管：按在墨迹上
      （哪怕就是选中的这条笔画）永远是选中/拖动，小笔画放大后即可
      精确按到手柄；
    * 「弯折」子模式：拖动笔画上最近点，沿弧长高斯衰减做局部扭曲（单选）；
    * 对选中笔画「重新施加扰动」或删除（多选即批量）。

性能：
    * 命中测试走**均匀网格空间索引**（``_rebuild_index``）：鼠标移动时的
      悬停命中从「遍历全部点」降到只看邻近格子，笔画多的大对象不再卡顿；
    * 重绘用**受影响区域**（``_repaint_rect``）替代整视口 ``update()``：
      悬停/拖动/框选只重画动过的那块，帧成本与对象规模无关。

所有编辑都在**世界坐标**下预览（``preview``），松手时经逆变换写回
``local_strokes``，由主窗口通过既有 ``replace_objects`` 入撤销栈——
一次手势 = 一步撤销。
"""

from __future__ import annotations

import math
from typing import Callable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainterPath, QPen

from ..core.geometry import AffineTransform
from ..core.strokes import Stroke

GRAB_PX = 8.0        # 笔画抓取容差（屏幕像素）
HANDLE_PX = 5.0      # 手柄半径（屏幕像素）
COLOR_SEL = QColor(0x40, 0x86, 0xd8)
COLOR_HOVER = QColor(0x40, 0x86, 0xd8, 160)   # 悬停高亮（比选中细、半透明）
COLOR_HANDLE = QColor(255, 255, 255)
COLOR_HANDLE_EDGE = QColor(0x40, 0x86, 0xd8)
COLOR_BEND = QColor(0xd8, 0x8a, 0x28)
COLOR_BAND = QColor(0x40, 0x86, 0xd8)
COLOR_BAND_FILL = QColor(0x40, 0x86, 0xd8, 40)

_CORNERS = ("nw", "ne", "se", "sw")

#: 空间索引的格子边长（mm）。取略大于典型笔画间距，命中查询只看邻近格子。
_CELL_MM = 4.0
_BAND_START_PX = 3.0     # 空白处按下后移动超过该像素数才视为框选
_HANDLE_MARGIN_PX = 24.0  # 区域重绘时为手柄/高亮预留的像素边距


def _dist_to_polyline(pt, pts) -> float:
    """点到折线的最小距离（先比顶点，再比各段投影）。"""
    if not pts:
        return float("inf")
    if len(pts) == 1:
        return math.hypot(pt[0] - pts[0][0], pt[1] - pts[0][1])
    best = float("inf")
    for a, b in zip(pts, pts[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg2 = dx * dx + dy * dy
        if seg2 <= 1e-18:
            d = math.hypot(pt[0] - a[0], pt[1] - a[1])
        else:
            t = ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / seg2
            t = max(0.0, min(1.0, t))
            d = math.hypot(pt[0] - (a[0] + t * dx), pt[1] - (a[1] + t * dy))
        best = min(best, d)
    return best


def _seg_hits_rect(a, b, rect: QRectF) -> bool:
    """线段是否与矩形相交（含端点在框内）。"""
    if rect.contains(QPointF(a[0], a[1])) or rect.contains(QPointF(b[0], b[1])):
        return True
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    # 与四条边求交（用参数化裁剪的简化版：逐边判交）
    edges = (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
             ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0)))
    for (c, d) in edges:
        if _seg_intersect(a, b, c, d):
            return True
    return False


def _seg_intersect(p1, p2, p3, p4) -> bool:
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 1 if v > 1e-12 else (-1 if v < -1e-12 else 0)

    o1, o2 = orient(p1, p2, p3), orient(p1, p2, p4)
    o3, o4 = orient(p3, p4, p1), orient(p3, p4, p2)
    return o1 != o2 and o3 != o4


class StrokeEditor(QObject):
    """笔画级编辑会话（挂在 CanvasView 上，由主窗口启停）。"""

    requestExit = Signal()

    def __init__(self, canvas) -> None:
        super().__init__(canvas)     # QObject 父子关系：随画布销毁
        self.canvas = canvas
        self.active = False
        self.obj = None                 # 正在编辑的 DocumentObject
        self.world: list[Stroke] = []   # 对象笔画（世界坐标缓存）
        self._selection: set[int] = set()   # 多选集合（见 sel 属性）
        self._primary: Optional[int] = None  # 主轴笔画（手柄/弯折锚定）
        self.hover: Optional[int] = None     # 鼠标下笔画的悬停高亮
        self.mode_bend = False
        self.preview: Optional[Stroke] = None   # 主轴手势中的世界坐标笔画
        self._multi_preview: dict[int, Stroke] = {}  # 多选拖动时的全部预览
        self._drag: Optional[dict] = None
        self._band: Optional[QRectF] = None     # 框选橡皮框（世界坐标）
        self._band_origin: Optional[QPointF] = None
        self._band_additive = False
        # 空间索引：{(ix, iy): [笔画下标]}，附着/刷新世界笔画时重建
        self._grid: dict[tuple[int, int], list[int]] = {}
        self._grid_len = -1          # 建索引时的笔画数（自愈用）
        # 由主窗口注入：
        self.commit_stroke: Optional[Callable] = None    # (obj, index, local_stroke)
        self.delete_stroke: Optional[Callable] = None    # (obj, index)
        self.commit_strokes: Optional[Callable] = None   # (obj, indices, locals)
        self.delete_strokes: Optional[Callable] = None   # (obj, indices)

    # --------------------------------------------------------------- 选择
    @property
    def sel(self) -> Optional[int]:
        """主轴笔画下标（兼容旧接口：单选时即选中项）。"""
        return self._primary

    @sel.setter
    def sel(self, value: Optional[int]) -> None:
        if value is None:
            self._selection = set()
            self._primary = None
        else:
            self._selection = {int(value)}
            self._primary = int(value)

    @property
    def selection(self) -> set[int]:
        return set(self._selection)

    def _set_selection(self, indices) -> None:
        self._selection = {int(i) for i in indices}
        if self._primary not in self._selection:
            self._primary = min(self._selection) if self._selection else None

    # ------------------------------------------------------------- 启停
    def attach(self, obj) -> None:
        self.obj = obj
        self.world = obj.world_strokes()
        self._selection = set()
        self._primary = None
        self.hover = None
        self.preview = None
        self._multi_preview = {}
        self._drag = None
        self._band = None
        self._band_origin = None
        self._rebuild_index()
        self.active = True

    def detach(self) -> None:
        self.active = False
        self.obj = None
        self.world = []
        self._selection = set()
        self._primary = None
        self.hover = None
        self.preview = None
        self._multi_preview = {}
        self._drag = None
        self._band = None
        self._band_origin = None
        self._grid = {}

    def set_bend_mode(self, on: bool) -> None:
        self.mode_bend = on
        self._repaint_all()

    def is_dragging(self) -> bool:
        return self._drag is not None or self._band_origin is not None

    # ------------------------------------------------------------- 空间索引
    def _rebuild_index(self) -> None:
        """重建均匀网格索引。

        不只是把**顶点**放进格子——长直线的两个端点可能相距很远，只登记顶点
        的话，查询线段中段的格子会一无所获（2 点横线尤其明显）。因此把每条
        线段按经过的格子做栅格化登记，命中查询只看邻近格子仍能得到所有可能
        相交的笔画。
        """
        grid: dict[tuple[int, int], list[int]] = {}
        inv = 1.0 / _CELL_MM

        def add(ix: int, iy: int, i: int) -> None:
            bucket = grid.get((ix, iy))
            if bucket is None:
                grid[(ix, iy)] = [i]
            elif bucket[-1] != i:
                bucket.append(i)

        for i, s in enumerate(self.world):
            if s is None:            # 防御：坏槽位不进索引
                continue
            pts = s.points
            for x, y in pts:
                add(int(math.floor(x * inv)), int(math.floor(y * inv)), i)
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                ix0, iy0 = int(math.floor(x0 * inv)), int(math.floor(y0 * inv))
                ix1, iy1 = int(math.floor(x1 * inv)), int(math.floor(y1 * inv))
                if ix0 == ix1 and iy0 == iy1:
                    continue
                # 沿线段按格子边界小步推进（步长 ≤ 半格，保证不漏格）
                dx, dy = x1 - x0, y1 - y0
                length = math.hypot(dx, dy)
                steps = int(length * inv * 2.0) + 1
                for k in range(steps + 1):
                    t = k / steps
                    cx = x0 + dx * t
                    cy = y0 + dy * t
                    add(int(math.floor(cx * inv)),
                        int(math.floor(cy * inv)), i)
        self._grid = grid
        self._grid_len = len(self.world)

    def _candidate_strokes(self, pos: QPointF, tol: float) -> set[int]:
        """邻近格子里的候选笔画下标（tol 为世界坐标容差）。"""
        # 自愈：世界笔画被绕过 attach/_refresh_world 直接替换时（测试、外部
        # 注入）索引会过期，按笔画数变化重建，避免命中结果错误。
        if self._grid_len != len(self.world):
            self._rebuild_index()
        n = int(math.ceil(tol / _CELL_MM))
        cx, cy = int(math.floor(pos.x() / _CELL_MM)), int(math.floor(pos.y() / _CELL_MM))
        out: set[int] = set()
        grid = self._grid
        for ix in range(cx - n, cx + n + 1):
            for iy in range(cy - n, cy + n + 1):
                bucket = grid.get((ix, iy))
                if bucket:
                    out.update(bucket)
        return out

    # ------------------------------------------------------------- 工具
    def _zoom(self) -> float:
        return max(1e-6, self.canvas.current_zoom())

    def _current(self) -> Optional[Stroke]:
        """主轴笔画：手势中取预览，否则取缓存。"""
        if self._primary is None:
            return None
        prev = self._multi_preview.get(self._primary)
        if prev is not None:
            return prev
        if self.preview is not None:
            return self.preview
        if 0 <= self._primary < len(self.world) \
                and self.world[self._primary] is not None:
            return self.world[self._primary]
        return None

    def _handles(self, s: Stroke) -> list[tuple[tuple[float, float], str]]:
        box = s.bbox()
        if box.is_empty:
            return []
        cx = (box.x0 + box.x1) / 2.0
        off = 14.0 / self._zoom()      # 旋转柄在框上方 14px 处
        return [((box.x0, box.y0), "nw"), ((box.x1, box.y0), "ne"),
                ((box.x1, box.y1), "se"), ((box.x0, box.y1), "sw"),
                ((cx, box.y1 + off), "rot")]

    def _refresh_world(self) -> None:
        """按下时重建世界坐标缓存与空间索引。

        缓存可能因撤销/重做、行内编辑、属性面板写回而整体过期
        （笔画数不变但内容变了），索性每次手势开始都重建一次——
        单次 world_strokes() 只是坐标变换，开销可忽略。
        """
        if self.obj is None:
            return
        self.world = self.obj.world_strokes()
        self._rebuild_index()
        if self._primary is not None and self._primary >= len(self.world):
            self._selection = set()
            self._primary = None
        self.preview = None
        self._multi_preview = {}

    def on_document_synced(self) -> None:
        """文档被外部改变（撤销/重做/行内编辑等）后刷新缓存与覆盖层。

        世界缓存过期的话，高亮/手柄会一直画在旧位置形成"撤回后残影"，
        直到某次按下触发重建。手势进行中不会走到这里（UI 单线程，
        撤销不会打断拖动），清空手势状态是安全的。
        """
        if not self.active or self.obj is None:
            return
        self._refresh_world()
        self._selection = {i for i in self._selection
                           if 0 <= i < len(self.world)}
        if self._primary is not None and self._primary not in self._selection:
            self._primary = min(self._selection) if self._selection else None
        self._drag = None
        self._band = None
        self._band_origin = None
        self._repaint_all()

    # ------------------------------------------------------------- 重绘
    def _repaint_all(self) -> None:
        self.canvas.viewport().update()

    def _margin(self) -> float:
        return _HANDLE_MARGIN_PX / self._zoom()

    def _rect_of_indices(self, indices, extra: float = 0.0,
                         override: Optional[dict] = None) -> Optional[QRectF]:
        """一组下标笔画的合并包围盒（世界坐标，QRectF）。

        ``override`` 给定时优先用其中的笔画（手势预览），否则用 ``world``
        缓存；拖动时两处都要覆盖，故调用方把新旧区域并起来。
        """
        rect: Optional[QRectF] = None
        for i in indices:
            st = None
            if override is not None:
                st = override.get(i)
            if st is None:
                if 0 <= i < len(self.world):
                    st = self.world[i]
            if st is None:
                continue
            b = st.bbox()
            if b.is_empty:
                continue
            r = QRectF(b.x0, b.y0, b.width, b.height)
            rect = r if rect is None else rect.united(r)
        if rect is not None and extra:
            rect = rect.adjusted(-extra, -extra, extra, extra)
        return rect

    def _repaint_rect(self, rect: Optional[QRectF]) -> None:
        """只重画给定世界坐标区域（含手柄余量）；rect 为 None 时全画。

        注意不能拿 ``QRectF.isNull()`` 当「全画」判据：水平/垂直笔画的包围盒
        高或宽为 0，``isNull()`` 为真——那样悬停一条横线会退化成整视口重绘。
        这里只在 ``None`` 时全画，其余一概加手柄余量后按区域更新。
        """
        if rect is None:
            self._repaint_all()
            return
        m = self._margin()
        r = rect.adjusted(-m, -m, m, m)
        poly = self.canvas.mapFromScene(r)
        self.canvas.viewport().update(poly.boundingRect())

    def _repaint_for_selection(self) -> None:
        self._repaint_rect(self._rect_of_indices(self._selection))

    # ------------------------------------------------------------- 命中
    def _hit_stroke(self, pos) -> Optional[int]:
        """离光标最近（且在抓取容差内）的笔画。

        用**点到折线**的距离而非只比顶点：长直线（表格线、边框、2 点线段）
        两个端点可能相距很远，只比顶点会导致线中段点不中。候选已由空间索引
        缩小，逐条精确测距代价可控。
        """
        tol = GRAB_PX / self._zoom()
        best, best_d = None, tol
        p = (pos.x(), pos.y())
        for i in self._candidate_strokes(pos, tol):
            if not (0 <= i < len(self.world)) or self.world[i] is None:
                continue
            d = _dist_to_polyline(p, self.world[i].points)
            if d < best_d:
                best, best_d = i, d
        return best

    def update_hover(self, pos) -> None:
        """悬停高亮：鼠标下的笔画自动点亮，方便确认要编辑的是哪一笔。

        ``pos=None``（鼠标移出画布）清除高亮。拖动手势中不更新——
        高亮以正在编辑的笔画为准。
        """
        if not self.active or self.obj is None or self._drag is not None:
            return
        idx = self._hit_stroke(pos) if pos is not None else None
        if idx != self.hover:
            old = self.hover
            self.hover = idx
            # 只重画新旧高亮覆盖的区域
            idxs = [i for i in (old, idx) if i is not None]
            self._repaint_rect(self._rect_of_indices(idxs))

    def _hit_handle(self, pos, handles) -> Optional[str]:
        tol = (GRAB_PX + HANDLE_PX) / self._zoom()
        best, best_d = None, tol
        for (hx, hy), kind in handles:
            d = math.hypot(pos.x() - hx, pos.y() - hy)
            if d <= best_d:
                best, best_d = kind, d
        return best

    def _nearest_stroke_dist(self, pos, radius_mm: float) -> Optional[float]:
        """光标 ``radius_mm`` 邻域内最近笔画的欧氏距离；没有则 None。"""
        p = (pos.x(), pos.y())
        best: Optional[float] = None
        for i in self._candidate_strokes(pos, radius_mm):
            if not (0 <= i < len(self.world)) or self.world[i] is None:
                continue
            d = _dist_to_polyline(p, self.world[i].points)
            if best is None or d < best:
                best = d
        return best

    def _ink_nearer_than_handle(self, pos, s: Stroke, kind: str) -> bool:
        """按点到最近笔画（**含选中这条**）的距离 ≤ 到手柄的距离。

        是则这次按下按笔画处理（选中/拖动），手柄让位——
        「按在墨迹上永远是拖笔画，按在空白处的手柄才是变换」。
        """
        hx, hy = next(p for p, k in self._handles(s) if k == kind)
        d_handle = math.hypot(pos.x() - hx, pos.y() - hy)
        near = self._nearest_stroke_dist(
            pos, d_handle + GRAB_PX / self._zoom())
        if near is None:
            return False            # 周围没有笔画：手柄接管
        return near <= d_handle

    def _strokes_in_band(self, rect: QRectF) -> set[int]:
        """框选：与橡皮框相交（含被完全包含）的所有笔画。

        注意水平/垂直笔画的包围盒宽或高为 0，Qt 视零面积 QRectF 为「空」，
        ``intersects``/``contains`` 会一律返回 False。这里把包围盒按极小量
        膨胀后再判，退化为线段的情况也能正确命中。
        """
        eps = 1e-6
        out: set[int] = set()
        for i, s in enumerate(self.world):
            b = s.bbox()
            if b.is_empty:
                continue
            sb = QRectF(b.x0 - eps, b.y0 - eps,
                        b.width + 2 * eps, b.height + 2 * eps)
            if not sb.intersects(rect):
                continue
            if rect.contains(sb):
                out.add(i)
                continue
            pts = s.points
            hit = any(rect.contains(QPointF(x, y)) for x, y in pts)
            if not hit:
                for a, c in zip(pts, pts[1:]):
                    if _seg_hits_rect(a, c, rect):
                        hit = True
                        break
            if hit:
                out.add(i)
        return out

    # ------------------------------------------------------------- 交互
    def on_press(self, pos, modifiers) -> bool:
        """按下：返回 True 表示事件已消费（画布不再转给场景）。"""
        if not self.active or self.obj is None:
            return False
        self._refresh_world()
        additive = bool(modifiers & Qt.ShiftModifier)

        # 手柄（单选且非弯折模式）。两类误触发都要防：
        #   * 想点选「下一笔」时误中上一笔的手柄 → 选择框乱缩放；
        #   * 小字号手写体笔画包围盒只有几个像素，13px 手柄命中圈罩住
        #     整条笔画 → 第二次拖动被劫持成缩放/旋转，笔画"吸附原位"。
        # 规则：按在墨迹上永远是选中/拖动笔画；只有按到手的距离严格
        # 小于到墨迹的距离（即按在空白处的手柄上）才进入变换。放大后
        # 笔画在屏幕上变大，手柄自然可以精确按到。
        if self._primary is not None and len(self._selection) == 1 \
                and not self.mode_bend:
            s = self._current()
            if s is not None:
                kind = self._hit_handle(pos, self._handles(s))
                if kind is not None and \
                        self._ink_nearer_than_handle(pos, s, kind):
                    kind = None
                if kind == "rot":
                    box = s.bbox()
                    self._drag = {"kind": "rotate", "center": box.center,
                                  "orig": self._orig_points(),
                                  "a0": math.atan2(pos.y() - box.center[1],
                                                   pos.x() - box.center[0])}
                    return True
                if kind in _CORNERS:
                    s_box = s.bbox()
                    anchor = {"nw": (s_box.x1, s_box.y1), "ne": (s_box.x0, s_box.y1),
                              "se": (s_box.x0, s_box.y0), "sw": (s_box.x1, s_box.y0)}[kind]
                    start = {"nw": (s_box.x0, s_box.y0), "ne": (s_box.x1, s_box.y0),
                             "se": (s_box.x1, s_box.y1), "sw": (s_box.x0, s_box.y1)}[kind]
                    self._drag = {"kind": "scale", "anchor": anchor, "start": start,
                                  "orig": self._orig_points(),
                                  "keep_ratio": bool(modifiers & Qt.ShiftModifier)}
                    return True

        idx = self._hit_stroke(pos)
        if idx is None:
            # 空白处按下：开始框选（松开时按是否有拖动决定「点空=清空选择」）
            self._band_origin = QPointF(pos)
            self._band = None
            self._band_additive = additive
            return True
        # 命中笔画：决定选择集
        if additive:
            new_sel = set(self._selection)
            new_sel.add(idx)
            self._set_selection(new_sel)
            self._primary = idx
        elif idx in self._selection and len(self._selection) > 1:
            self._primary = idx          # 已多选：保持集合，仅改主轴
        else:
            self._set_selection({idx})
            self._primary = idx

        s = self.world[idx]
        if self.mode_bend and len(self._selection) == 1:
            pts = s.points
            arc = [0.0]
            for a, b in zip(pts, pts[1:]):
                arc.append(arc[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
            i0 = min(range(len(pts)),
                     key=lambda i: math.hypot(pos.x() - pts[i][0],
                                              pos.y() - pts[i][1]))
            diag = max(1.0, math.hypot(s.bbox().width, s.bbox().height))
            self._drag = {"kind": "bend", "orig": list(pts), "arc": arc,
                          "i0": i0, "s0": arc[i0], "grab": (pos.x(), pos.y()),
                          "sigma": max(2.0, diag * 0.06)}
        else:
            # 多选拖动：记录所有选中笔画的原始点，一起平移。
            # 位移必须相对**按下点**累计——若用相邻事件的增量，真实鼠标
            # 的连续小步会让预览永远只偏移最后一步（笔画"拽回原点、
            # 只能抽搐"）。
            orig = {i: list(self.world[i].points)
                    for i in sorted(self._selection)
                    if 0 <= i < len(self.world)}
            self._drag = {"kind": "move", "start": (pos.x(), pos.y()),
                          "orig": orig}
        self.preview = Stroke(list(s.points), s.closed)
        if self._drag["kind"] == "bend":
            # 弯折手势全程全视口重绘（见 on_move bend 分支的说明）
            self._repaint_all()
        else:
            self._repaint_for_selection()
        return True

    def _orig_points(self) -> list[tuple[float, float]]:
        cur = self._current()
        return list(cur.points) if cur is not None else []

    def on_move(self, pos) -> None:
        if self._band_origin is not None:
            self._update_band(pos)
            return
        if not self._drag or self._primary is None:
            return
        d = self._drag
        kind = d["kind"]
        if kind == "move" and isinstance(d.get("orig"), dict):
            sx, sy = d["start"]
            dx, dy = pos.x() - sx, pos.y() - sy
            new_map: dict[int, Stroke] = {}
            for i, pts in d["orig"].items():
                new_map[i] = Stroke([(x + dx, y + dy) for x, y in pts],
                                    self.world[i].closed)
            self._multi_preview = new_map
            self.preview = new_map.get(self._primary)
            # 只重画受影响区域：原位置 ∪ 预览新位置（都要覆盖）
            old = self._rect_of_indices(list(new_map))
            new = self._rect_of_indices(list(new_map), override=new_map)
            rect = old
            if new is not None:
                rect = new if rect is None else rect.united(new)
            self._repaint_rect(rect)
            return
        pts = list(d["orig"])
        if kind == "move":
            sx, sy = d["start"]
            pts = [(x + pos.x() - sx, y + pos.y() - sy) for x, y in pts]
        elif kind == "rotate":
            cx, cy = d["center"]
            ang = math.degrees(math.atan2(pos.y() - cy, pos.x() - cx) - d["a0"])
            t = AffineTransform.rotate_about(ang, (cx, cy))
            pts = [t.apply(p) for p in pts]
        elif kind == "scale":
            ax, ay = d["anchor"]
            sx0, sy0 = d["start"]
            fx = (pos.x() - ax) / (sx0 - ax) if abs(sx0 - ax) > 1e-9 else 1.0
            fy = (pos.y() - ay) / (sy0 - ay) if abs(sy0 - ay) > 1e-9 else 1.0
            if d["keep_ratio"]:
                f = (abs(fx) + abs(fy)) / 2.0 or 1.0
                fx = fy = math.copysign(f, fx * fy) if fx * fy < 0 else f
            fx = max(-20.0, min(20.0, fx))
            fy = max(-20.0, min(20.0, fy))
            pts = [(ax + (x - ax) * fx, ay + (y - ay) * fy) for x, y in pts]
        elif kind == "bend":
            dx, dy = pos.x() - d["grab"][0], pos.y() - d["grab"][1]
            sigma2 = 2.0 * d["sigma"] * d["sigma"]
            pts = []
            for (x, y), s_a in zip(d["orig"], d["arc"]):
                w = math.exp(-((s_a - d["s0"]) ** 2) / sigma2)
                pts.append((x + dx * w, y + dy * w))
        self.preview = Stroke(pts, self.world[self._primary].closed)
        if kind == "bend":
            # 弯折手势全程全视口失效：黄圈半径 σ 伸出笔画包围盒、只有
            # 1.2px 细环，软件管线下按区域重绘已验证无残留（DPR 1.0/1.5
            # 实测 0 像素），但 Wayland 分数缩放按区域提交 damage 时旧
            # 圈环仍会留在屏上（用户实测拖影）。整视口失效从根上杜绝；
            # 成本与滚动持平（背景缓存 blit，P30 包线内）。
            self._repaint_all()
            return
        # 手势中重画的区域 = 原笔画 ∪ 预览笔画
        old = self._rect_of_indices([self._primary])
        new = QRectF(*self._rect_from_points(pts))
        rect = old.united(new) if old is not None else new
        self._repaint_rect(rect)

    @staticmethod
    def _rect_from_points(pts) -> tuple:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def _update_band(self, pos) -> None:
        origin = self._band_origin
        r = QRectF(origin, QPointF(pos)).normalized()
        if r.width() * self._zoom() < _BAND_START_PX \
                and r.height() * self._zoom() < _BAND_START_PX:
            r = None                 # 还没拖出足够距离：不显示橡皮框
        old = self._band
        self._band = r
        # 重画旧框 ∪ 新框
        repaint = None
        for rr in (old, r):
            if rr is None:
                continue
            repaint = rr if repaint is None else repaint.united(rr)
        self._repaint_rect(repaint)

    def on_release(self) -> None:
        """松手：提交手势（一次手势=一步撤销）；框选则只更新选择集。

        手势没有实际改变笔画（原地点击选笔画）时不提交、不产生撤销项。
        """
        # 框选收尾
        if self._band_origin is not None:
            band = self._band
            self._band_origin = None
            self._band = None
            if band is None:
                # 空白处点击（无拖动）：清空选择（Shift 则保留）
                if not self._band_additive:
                    self._set_selection(set())
            else:
                hits = self._strokes_in_band(band)
                if self._band_additive:
                    self._set_selection(self._selection | hits)
                else:
                    self._set_selection(hits)
            self._repaint_all()
            return
        if not self._drag:
            return
        self._drag = None

        # 多选平移提交
        if self._multi_preview:
            new_map = self._multi_preview
            self._multi_preview = {}
            self.preview = None
            obj = self.obj
            # 位置没变的（原地点击）不提交
            changed = {i: st for i, st in new_map.items()
                       if i < len(self.world)
                       and list(st.points) != list(self.world[i].points)}
            if changed and obj is not None:
                indices, locals_ = [], []
                for i, st in changed.items():
                    inv = obj.transform.invert()
                    xform = Stroke([inv.apply(p) for p in st.points], st.closed)
                    indices.append(i)
                    locals_.append(xform)
                    self.world[i] = st
                if self.commit_strokes is not None:
                    self.commit_strokes(obj, indices, locals_)
                elif self.commit_stroke is not None:
                    for i, lc in zip(indices, locals_):
                        self.commit_stroke(obj, i, lc)
            self._repaint_all()
            return

        if self.preview is None or self.obj is None or self._primary is None:
            self.preview = None
            self._repaint_all()
            return
        if self.preview is not None and self._primary is not None \
                and 0 <= self._primary < len(self.world) \
                and list(self.preview.points) \
                == list(self.world[self._primary].points):
            # 手势没有实际改动（原地点一下选笔画）：原样写回缓存即可，
            # 不提交、不产生撤销项
            self.world[self._primary] = self.preview
            self.preview = None
            self._repaint_for_selection()
            return
        preview = self.preview
        if self.commit_stroke is not None:
            inv = self.obj.transform.invert()
            local = Stroke([inv.apply(p) for p in preview.points],
                           preview.closed)
            # commit 内部会同步文档并经 on_document_synced 重建 world
            # 缓存（顺带清空 self.preview）——绝不能在 commit 之后再把
            # self.preview 写回缓存，否则 None 会进缓存，悬停/重新扰动
            # 全部崩掉
            self.commit_stroke(self.obj, self._primary, local)
        else:
            self.world[self._primary] = preview
        self.preview = None
        self._repaint_all()

    # ------------------------------------------------------------- 操作
    def selected_index(self) -> Optional[int]:
        return self._primary

    def selected_indices(self) -> list[int]:
        return sorted(self._selection)

    def delete_selected(self) -> bool:
        if not self._selection or self.obj is None:
            return False
        indices = sorted(self._selection)
        if self.delete_strokes is not None:
            self.delete_strokes(self.obj, indices)
        elif self.delete_stroke is not None:
            for i in reversed(indices):
                self.delete_stroke(self.obj, i)
        self.world = self.obj.world_strokes()
        self._rebuild_index()
        self._selection = set()
        self._primary = None
        self.hover = None
        self.preview = None
        self._multi_preview = {}
        self._repaint_all()
        return True

    # ------------------------------------------------------------- 绘制
    def paint(self, painter) -> None:
        """在 drawForeground 的**场景坐标**里画高亮/手柄/框选。"""
        if not self.active or self.obj is None:
            return

        def _cos_pen(color, width_px):
            pen = QPen(color)
            pen.setCosmetic(True)      # 场景坐标下保持屏幕像素宽
            pen.setWidthF(width_px)
            return pen

        def _draw_polyline(s: Stroke) -> QPainterPath:
            path = QPainterPath()
            if len(s.points) < 2:
                return path
            path.moveTo(*s.points[0])
            for p in s.points[1:]:
                path.lineTo(*p)
            return path

        painter.save()
        painter.setBrush(Qt.NoBrush)
        # 悬停高亮：鼠标下的笔画（未选中时）点一套弱高亮，方便确认目标
        if (self.hover is not None and self.hover not in self._selection
                and self._drag is None and self.hover < len(self.world)
                and self.world[self.hover] is not None):
            painter.setPen(_cos_pen(COLOR_HOVER, 2.2))
            painter.drawPath(_draw_polyline(self.world[self.hover]))

        # 选中笔画：多选全部点亮
        painter.setPen(_cos_pen(COLOR_SEL, 2.4))
        for i in sorted(self._selection):
            st = self._multi_preview.get(i)
            if st is None and i == self._primary and self.preview is not None:
                st = self.preview
            if st is None:
                if 0 <= i < len(self.world):
                    st = self.world[i]
            if st is not None:
                painter.drawPath(_draw_polyline(st))

        s = self._current()
        # 弯折模式：画抓取半径圈
        if self.mode_bend and self._drag and self._drag["kind"] == "bend" and s:
            painter.setPen(_cos_pen(COLOR_BEND, 1.2))
            cx, cy = s.points[self._drag["i0"]]
            painter.drawEllipse(QPointF(cx, cy), self._drag["sigma"],
                                self._drag["sigma"])

        # 变换手柄只在单选时出现（多选拖动不做整体缩放/旋转）
        if len(self._selection) == 1 and s is not None and len(s.points) >= 2:
            z = self._zoom()
            dragging_bend = self._drag and self._drag["kind"] == "bend"
            if not self.mode_bend or not dragging_bend:
                r = HANDLE_PX / z
                for (hx, hy), kind in self._handles(s):
                    if kind == "rot":
                        painter.setBrush(COLOR_BEND)
                    else:
                        painter.setBrush(COLOR_HANDLE)
                    painter.setPen(_cos_pen(COLOR_HANDLE_EDGE, 1.2))
                    painter.drawRect(QRectF(hx - r, hy - r, 2 * r, 2 * r))

        # 框选橡皮框
        if self._band is not None:
            painter.setPen(_cos_pen(COLOR_BAND, 1.2))
            painter.setBrush(COLOR_BAND_FILL)
            painter.drawRect(self._band)
        painter.restore()
