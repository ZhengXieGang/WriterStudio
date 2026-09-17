"""画布上的图形项（QGraphicsObject）。

坐标关系（全项目统一）：
    * item 本地坐标 = 对象本地坐标（mm）
    * item.transform = 对象的仿射变换（本地 → 页面）
    * scene 坐标 = 页面坐标（mm，Y 向上）
    * 视图层（CanvasView）负责把 Y 翻转成屏幕方向

因此鼠标事件的 ``event.pos()`` 直接就是本地坐标，缩放/旋转计算无需额外换算。

本模块提供两个图形项，共享同一套移动/缩放/旋转交互：

    * :class:`ObjectGraphicsItem` —— 可书写对象（笔画）
    * :class:`ReferenceGraphicsItem` —— 参考层图片/SVG（**不参与书写**，仅对齐用）
"""

from __future__ import annotations

import math
from enum import Enum, auto
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
)

from ..core.document import DocumentObject
from ..core.geometry import AffineTransform
from ..core.reference import KIND_SVG, ReferenceItem
from ..content.builder import SOURCE_MARKDOWN
from ..fonts.builder import TextSpec, SOURCE_KIND_TEXT
from ..fonts.layout import frame_box
from .controller import DocumentController, snapshot_object

# 颜色
COLOR_INK = QColor(30, 30, 40)
COLOR_SELECT = QColor(0, 122, 204)
COLOR_HANDLE_FILL = QColor(255, 255, 255)
COLOR_HOVER = QColor(0, 160, 255)
COLOR_REF_BORDER = QColor(0, 130, 150)
COLOR_MARKER = QColor(220, 40, 40)

HANDLE_PX = 8.0        # 手柄目标屏幕边长(px)
ROTATE_OFFSET_PX = 22  # 旋转手柄距包围盒顶部的屏幕距离(px)
MARGIN_MM = 6.0        # 包围盒外扩(mm)，用于容纳手柄/旋转柄
HIT_GRAB_MM = 1.6      # 线条命中区沿线的抓取宽度(mm)
MAX_IMAGE_PX = 3000    # 参考图最大解码边长(px)，避免超大图拖慢界面
SNAP_PX = 8.0          # 缩放吸附的屏幕容差(px)，换算成页面 mm 后比较


def _snap_coord(targets, value: float, tol: float) -> Optional[float]:
    """在 ``tol`` 内取离 ``value`` 最近的吸附目标；没有则返回 None。"""
    best: Optional[float] = None
    best_d = tol
    for t in targets:
        d = abs(t - value)
        if d <= best_d:
            best_d = d
            best = t
    return best


class Handle(Enum):
    NONE = auto()
    TL = auto()
    T = auto()
    TR = auto()
    R = auto()
    BR = auto()
    B = auto()
    BL = auto()
    L = auto()
    ROTATE = auto()


# 手柄 → 本地单位坐标系数（相对包围盒）；y 越大越靠上（Y 向上）
_HANDLE_UV = {
    Handle.TL: (0.0, 1.0),
    Handle.T: (0.5, 1.0),
    Handle.TR: (1.0, 1.0),
    Handle.R: (1.0, 0.5),
    Handle.BR: (1.0, 0.0),
    Handle.B: (0.5, 0.0),
    Handle.BL: (0.0, 0.0),
    Handle.L: (0.0, 0.5),
}
_HANDLE_OPPOSITE = {
    Handle.TL: Handle.BR,
    Handle.T: Handle.B,
    Handle.TR: Handle.BL,
    Handle.R: Handle.L,
    Handle.BR: Handle.TL,
    Handle.B: Handle.T,
    Handle.BL: Handle.TR,
    Handle.L: Handle.R,
}
_SCALE_HANDLES = list(_HANDLE_UV.keys())


def qtransform_from(affine: AffineTransform) -> QTransform:
    return QTransform(*affine.as_tuple())


class TransformGraphicsItem(QGraphicsObject):
    """可移动/缩放/旋转的图形项基类，持有 ``model.transform``。"""

    #: 提交变换时的操作名（用于撤销文本）
    ACTION_NOUN = "对象"

    def __init__(self, model, controller: DocumentController) -> None:
        super().__init__()
        self.model = model
        self.controller = controller

        self._mode: Handle | None = None
        self._drag_kind: str | None = None
        self._pending: Optional[AffineTransform] = None
        self._start_transform = model.transform
        self._start_scene = QPointF()
        self._start_local = QPointF()
        self._hover_handle = Handle.NONE

        self.setFlags(
            QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.sync()

    # --------------------------------------------------------- 子类需要实现
    def _local_bbox(self) -> QRectF:
        raise NotImplementedError

    def _rebuild(self) -> None:
        """从 model 重建缓存（路径/位图）。"""

    def _paint_content(self, painter: QPainter) -> None:
        """绘制本地内容（已应用 item 变换）。"""

    def _hit_content(self, local_pos: QPointF) -> bool:
        box = self._local_bbox()
        return box.contains(local_pos)

    @property
    def _transform(self) -> AffineTransform:
        return self.model.transform

    def _display_name(self) -> str:
        return getattr(self.model, "name", self.ACTION_NOUN)

    # ------------------------------------------------------------------ 同步
    def sync(self) -> None:
        self.prepareGeometryChange()
        self._rebuild()
        self.setTransform(qtransform_from(self.model.transform))
        self.setVisible(bool(getattr(self.model, "visible", True)))
        self.update()

    @property
    def locked(self) -> bool:
        return bool(getattr(self.model, "locked", False))

    # ------------------------------------------------------------- 几何工具
    def _scene_per_px(self) -> float:
        """1 屏幕像素对应多少页面单位(mm)。"""
        view_scale = 1.0
        sc = self.scene()
        if sc is not None:
            views = sc.views()
            if views:
                view_scale = abs(views[0].transform().m11()) or 1.0
        return 1.0 / view_scale if view_scale > 1e-9 else 1.0

    def _local_per_px(self) -> float:
        """1 屏幕像素对应多少本地单位(mm)。"""
        # 用 item 的**当前**变换（拖拽预览时即 pending 变换），这样手柄/旋转
        # 柄的屏幕大小恒定，不会随对象一起被放大或缩小。
        det = abs(self.transform().determinant())
        item_scale = math.sqrt(det) if det > 1e-12 else 1.0
        denom = item_scale / self._scene_per_px()
        return 1.0 / denom if denom > 1e-9 else 1.0

    def _handle_size_local(self) -> float:
        return HANDLE_PX * self._local_per_px()

    def _visible_handles(self) -> list[Handle]:
        """参与绘制/命中的缩放手柄（子类可收窄，如文本对象隐藏 T/B）。"""
        return _SCALE_HANDLES

    def _handle_positions(self) -> dict[Handle, QPointF]:
        box = self._local_bbox()
        # 注意：数据坐标为 Y 向上，而 QRectF.top() 是最小 y、bottom() 是最大 y。
        # 因此本地「下边缘」= box.top()，「上边缘」= box.bottom()。
        pts: dict[Handle, QPointF] = {}
        for h in self._visible_handles():
            u, v = _HANDLE_UV[h]
            pts[h] = QPointF(box.left() + u * box.width(),
                             box.top() + v * box.height())
        # 旋转柄放在数据意义上的「上方」（最大 y）之外
        data_top = QPointF(box.center().x(), box.bottom())
        off = ROTATE_OFFSET_PX * self._local_per_px()
        pts[Handle.ROTATE] = QPointF(data_top.x(), data_top.y() + off)
        return pts

    def _hit_handle(self, local_pos: QPointF) -> Handle:
        if not self.isSelected() or self.locked:
            return Handle.NONE
        # 在**屏幕空间**比较：把鼠标与手柄都映射到页面坐标，再用「像素→页面单位」
        # 的容差。若在本地坐标比较，非等比缩放会让命中区也跟着变扁，与屏幕上画出的
        # 方形手柄对不上。多个手柄都在容差内时取**最近**的，避免对象在屏幕上较小、
        # 手柄彼此靠得很近时误命中相邻手柄。
        t = self.transform()
        p_scene = t.map(local_pos)
        tol = HANDLE_PX * 0.7 * self._scene_per_px()
        best = Handle.NONE
        best_d = None
        for h, p in self._handle_positions().items():
            hp = t.map(p)
            dx = p_scene.x() - hp.x()
            dy = p_scene.y() - hp.y()
            if abs(dx) <= tol and abs(dy) <= tol:
                d = dx * dx + dy * dy
                if best_d is None or d < best_d:
                    best_d = d
                    best = h
        return best

    # --------------------------------------------------------------- 绘制
    def boundingRect(self) -> QRectF:
        # 本地坐标下要留出足够余量容纳手柄/旋转柄（它们按屏幕像素恒定大小，
        # 换算成本地单位会随缩放变化，故取屏幕余量与固定余量的较大者）。
        margin = max(MARGIN_MM, (ROTATE_OFFSET_PX + HANDLE_PX) * self._local_per_px())
        return self._local_bbox().adjusted(-margin, -margin, margin, margin)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._paint_content(painter)
        if not self.isSelected():
            return
        self._paint_selection(painter)

    def _selection_box(self) -> QRectF:
        """选中态虚线框的范围（默认 = 本地包围盒）。"""
        return self._local_bbox()

    def _paint_selection(self, painter: QPainter) -> None:
        sel_pen = QPen(COLOR_SELECT)
        sel_pen.setCosmetic(True)
        sel_pen.setWidthF(1.0)
        sel_pen.setStyle(Qt.DashLine)
        painter.setPen(sel_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(self._selection_box())

        if self.locked:
            return      # 锁定对象只画虚线框，不提供手柄

        # 手柄必须**恒定屏幕尺寸且保持方正**：若在本地坐标里画，非等比缩放会把
        # 方块拉成长方形、圆拉成椭圆（对象越扁手柄越扁）。这里把手柄位置映射到
        # 设备坐标后再绘制，于是无论对象如何缩放/旋转，手柄始终是同样大小的方块。
        positions = self._handle_positions()
        wt = painter.worldTransform()
        hs = HANDLE_PX
        painter.save()
        painter.setWorldTransform(QTransform())     # 之后按屏幕（设备）像素绘制
        hpen = QPen(COLOR_SELECT)
        hpen.setWidthF(1.0)
        painter.setPen(hpen)
        for h in self._visible_handles():
            p = wt.map(positions[h])
            fill = COLOR_HOVER if h == self._hover_handle else COLOR_HANDLE_FILL
            painter.setBrush(QBrush(fill))
            painter.drawRect(QRectF(p.x() - hs / 2, p.y() - hs / 2, hs, hs))

        # 旋转手柄（圆）：同样按屏幕尺寸绘制，保持正圆
        rp = wt.map(positions[Handle.ROTATE])
        box = self._local_bbox()
        data_top = wt.map(QPointF(box.center().x(), box.bottom()))
        painter.setPen(sel_pen)
        painter.drawLine(data_top, rp)
        painter.setBrush(QBrush(COLOR_HOVER if self._hover_handle == Handle.ROTATE
                                else COLOR_HANDLE_FILL))
        painter.drawEllipse(rp, hs * 0.6, hs * 0.6)
        painter.restore()

    # ------------------------------------------------------------ 事件处理
    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        h = self._hit_handle(event.pos())
        if h != self._hover_handle:
            self._hover_handle = h
            self._update_cursor(h)
            self.update()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hover_handle = Handle.NONE
        self.setCursor(Qt.ArrowCursor)
        self.update()
        super().hoverLeaveEvent(event)

    def _update_cursor(self, h: Handle) -> None:
        if h == Handle.NONE or self.locked:
            self.setCursor(Qt.ArrowCursor)
        elif h == Handle.ROTATE:
            self.setCursor(Qt.CrossCursor)
        elif h in (Handle.TL, Handle.BR):
            self.setCursor(Qt.SizeFDiagCursor)
        elif h in (Handle.TR, Handle.BL):
            self.setCursor(Qt.SizeBDiagCursor)
        elif h in (Handle.T, Handle.B):
            self.setCursor(Qt.SizeVerCursor)
        else:
            self.setCursor(Qt.SizeHorCursor)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return

        handle = self._hit_handle(event.pos())
        if handle != Handle.NONE:
            self._begin_drag('handle', handle, event)
            event.accept()
            return

        # 命中对象内容 → 移动；否则放行给视图做框选
        if self._hit_content(event.pos()):
            if not self.isSelected():
                if self.scene() is not None:
                    self.scene().clearSelection()
                self.setSelected(True)
            if not self.locked:
                self._begin_drag('move', None, event)
            event.accept()
            return

        event.ignore()

    def _begin_drag(self, mode: str, handle: Optional[Handle], event) -> None:
        if self.locked:
            return
        self._mode = handle if mode == 'handle' else Handle.NONE
        self._drag_kind = mode
        self._start_transform = self.model.transform
        self._start_scene = event.scenePos()
        self._start_local = self._start_frame_local(event.scenePos())
        self._pending = None
        self._drag_paint_hint_changed()

    def _drag_paint_hint_changed(self) -> None:
        """拖动开始/结束时的绘制质量切换钩子（默认无操作）。"""

    def _set_views_smooth_pixmap(self, on: bool) -> None:
        """切换观察本场景的视图的 SmoothPixmapTransform（拖动快速预览用）。"""
        sc = self.scene()
        if sc is None:
            return
        for v in sc.views():
            v.setRenderHint(QPainter.SmoothPixmapTransform, on)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_kind == 'move':
            delta = event.scenePos() - self._start_scene
            new_t = AffineTransform.translate(delta.x(), delta.y()) @ self._start_transform
            self._preview(new_t)
            event.accept()
            return
        if self._drag_kind == 'handle':
            new_t = self._compute_handle_transform(event)
            if new_t is not None:
                self._preview(new_t)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def _start_frame_local(self, scene_pos: QPointF) -> QPointF:
        """把 scene(页面)坐标换算到「拖拽开始时的本地坐标系」。

        拖拽过程中 item 自身的变换一直在变，``event.pos()`` 是在**当前**变换下
        的本地坐标，用它计算缩放比例会形成反馈回路（拖得越大、反算出的鼠标
        位置越靠近锚点，对象便被弹回，反复抖动）。这里统一用固定的起始变换
        反算，得到与当前变换无关的稳定本地坐标。
        """
        x, y = self._start_transform.invert().apply((scene_pos.x(), scene_pos.y()))
        return QPointF(x, y)

    def _compute_handle_transform(self, event) -> Optional[AffineTransform]:
        handle: Handle = self._mode
        box = self._local_bbox()
        positions = self._handle_positions()

        if handle == Handle.ROTATE:
            # 旋转必须在**页面空间**里绕对象中心做刚体旋转（前乘），角度也在页面
            # 空间度量。旧写法把它写成本地空间的「先转后乘 start_transform」，一旦
            # 对象被非等比缩放（改过宽高比），旋转后又被那个非等比缩放作用，等价于
            # 「旋转 + 非等比缩放」→ 产生剪切，宽高比被严重拉伸。
            cx, cy = self._start_transform.apply((box.center().x(),
                                                  box.center().y()))
            center = QPointF(cx, cy)
            v0 = self._start_scene - center
            v1 = event.scenePos() - center
            a0 = math.atan2(v0.y(), v0.x())
            a1 = math.atan2(v1.y(), v1.x())
            deg = math.degrees(a1 - a0)
            return AffineTransform.rotate_about(deg, (cx, cy)) @ self._start_transform

        # 缩放：鼠标位置用起始变换反算成本地坐标，避免拖拽反馈回路（见上）
        cur = self._start_frame_local(event.scenePos())
        anchor_handle = _HANDLE_OPPOSITE.get(handle)
        if anchor_handle is None:
            return None
        anchor = positions[anchor_handle]
        start = self._start_local

        sx = sy = 1.0
        if abs(start.x() - anchor.x()) > 1e-6:
            sx = (cur.x() - anchor.x()) / (start.x() - anchor.x())
        if abs(start.y() - anchor.y()) > 1e-6:
            sy = (cur.y() - anchor.y()) / (start.y() - anchor.y())

        # 边手柄只缩放单轴
        if handle in (Handle.T, Handle.B):
            sx = 1.0
        elif handle in (Handle.L, Handle.R):
            sy = 1.0

        # Shift 保持比例
        mods = Qt.KeyboardModifiers(event.modifiers())
        if (mods & Qt.ShiftModifier) and handle in (
                Handle.TL, Handle.TR, Handle.BL, Handle.BR):
            s = max(abs(sx), abs(sy))
            sx = math.copysign(s, sx)
            sy = math.copysign(s, sy)

        # 吸附微调（如参考图缩放吸附纸张边缘）：Shift 锁比例时跳过，
        # 否则改一个轴会破坏比例、取舍不稳定。
        if not (mods & Qt.ShiftModifier):
            sx, sy = self._resize_snap(handle, anchor, sx, sy)

        # 防止归零/翻转抖动
        if abs(sx) < 1e-3:
            sx = math.copysign(1e-3, sx or 1.0)
        if abs(sy) < 1e-3:
            sy = math.copysign(1e-3, sy or 1.0)

        return self._start_transform @ AffineTransform.scale_about(
            sx, sy, (anchor.x(), anchor.y()))

    def _resize_snap(self, handle: Handle, anchor: QPointF,
                     sx: float, sy: float) -> tuple[float, float]:
        """缩放吸附钩子：返回微调后的 ``(sx, sy)``。默认不吸附。"""
        return sx, sy

    def _preview(self, transform: AffineTransform) -> None:
        self._pending = transform
        self.setTransform(qtransform_from(transform))
        self.update()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        kind = getattr(self, '_drag_kind', None)
        if kind == 'move':
            self._commit_transform(self._pending, "移动")
        elif kind == 'handle' and self._pending is not None:
            text = "旋转" if self._mode == Handle.ROTATE else "缩放"
            self._commit_transform(self._pending, text)
        elif kind in ('move', 'handle') and self._pending is None:
            self.sync()   # 未产生有效变换 → 还原视图
        self._mode = None
        self._drag_kind = None
        self._pending = None
        if kind is not None:
            self._drag_paint_hint_changed()
        event.accept()

    def _commit_transform(self, transform: Optional[AffineTransform], verb: str) -> None:
        # 锁定对象不接受任何变换；无实际变化（原地点击/拖回原位）也不产生撤销项
        if self.locked:
            return
        if transform is None or transform == self.model.transform:
            return
        self.controller.set_model_transform(
            self.model, transform, f"{verb}{self.ACTION_NOUN}")

    # ------------------------------------------------------------- 选择反馈
    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedHasChanged:
            self.update()
            sc = self.scene()
            if sc is not None:
                self.controller.notify_selection()
        return super().itemChange(change, value)


class ObjectGraphicsItem(TransformGraphicsItem):
    """显示并交互编辑一个 :class:`DocumentObject`（可书写笔画）。"""

    ACTION_NOUN = "对象"

    def __init__(self, obj: DocumentObject, controller: DocumentController) -> None:
        self._path = QPainterPath()
        self._hit_path = QPainterPath()
        self._path_rev = -1          # 上次重建路径时的笔画版本号
        self._hit_deferred = False   # 拖宽中跳过了命中区描边，待松手补建
        super().__init__(obj, controller)

    @property
    def obj(self) -> DocumentObject:
        return self.model

    @obj.setter
    def obj(self, value: DocumentObject) -> None:
        self.model = value

    # ------------------------------------------------------------------ 同步
    def sync(self) -> None:
        # 笔画版本号没变就跳过路径重建（QPainterPath + 命中区重建是
        # 全场景同步的主要开销）：一次编辑只重排真正改动的对象
        rev = self.model.strokes_rev
        if rev != self._path_rev:
            self._path_rev = rev
            self.prepareGeometryChange()
            self._rebuild()
        self.setTransform(qtransform_from(self.model.transform))
        self.setVisible(bool(self.model.visible))
        self.update()

    def _rebuild(self) -> None:
        self._rebuild_path()

    def _rebuild_path(self) -> None:
        path = QPainterPath()
        for s in self.model.local_strokes:
            if not s.points:
                continue
            path.moveTo(*s.points[0])
            for p in s.points[1:]:
                path.lineTo(*p)
            if s.closed:
                path.closeSubpath()
        self._path = path
        # 命中区 = 笔迹沿线一个小抓取宽度，而不是 boundingRect——
        # boundingRect 带 6mm+ 的手柄余量，会让线条旁的空白也能抓起对象、
        # 且橡皮框选在那里起不了作用。
        # 拖宽度手柄期间跳过描边（大文档上万点的 createStroke 很贵，且每次
        # 重排都会走到）：拖动中对象必处于选中态，``_hit_content`` 走「选中
        # 包围盒」分支用不到命中区，松手时再补建（见 mouseReleaseEvent）。
        if path.isEmpty() or self._drag_kind == 'frame':
            self._hit_path = QPainterPath()
            self._hit_deferred = not path.isEmpty()
            return
        self._hit_deferred = False
        self._hit_path = self._stroke_hit_path(path)

    def _stroke_hit_path(self, path: QPainterPath) -> QPainterPath:
        stroker = QPainterPathStroker()
        stroker.setWidth(HIT_GRAB_MM)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(path)

    def _local_bbox(self) -> QRectF:
        return self._path.boundingRect()

    def _hit_content(self, local_pos: QPointF) -> bool:
        # 文本对象：整个文本框都可点（像 PS 文本框，点空白处也能选中/进入
        # 编辑）；其余对象仍按墨迹命中，避免线条旁的空白也能抓起对象
        if self._is_text:
            r = self._frame_rect_or_extent()
            if r.adjusted(-0.5, -0.5, 0.5, 0.5).contains(local_pos):
                return True
        # 已选中的对象：虚线框内的空白处也能直接拖动（PS 变换框语义）。
        # 未选中时不放宽——否则大对象的框会挡住橡皮框选和点击下层对象。
        if self.isSelected() and self._local_bbox().contains(local_pos):
            return True
        if not self._path.isEmpty():
            return self._hit_path.contains(local_pos)
        return self.boundingRect().contains(local_pos)

    def _paint_content(self, painter: QPainter) -> None:
        pen = QPen(COLOR_INK)
        pen.setCosmetic(True)
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self._path)

    def boundingRect(self) -> QRectF:
        # 文本框手柄钉在框缘/框角上，可能落在墨迹包围盒之外
        # （框宽 > 当前墨迹宽时），否则手柄会被裁剪、命中失效
        rect = super().boundingRect()
        if self._is_text:
            r = self._frame_rect_or_extent()
            if r.isValid():
                m = (HANDLE_PX + 2.0) * self._local_per_px()
                rect = rect.united(r.adjusted(-m, -m, m, m))
        return rect

    # ------------------------------------------------------------- 文本框
    @property
    def _is_text(self) -> bool:
        return self.model.source.kind == SOURCE_KIND_TEXT

    @property
    def _is_markdown(self) -> bool:
        return self.model.source.kind == SOURCE_MARKDOWN

    def _spec(self) -> TextSpec:
        return TextSpec.from_data(self.model.source.data)

    def _table_rect(self) -> Optional[QRectF]:
        """Markdown 表格外框（本地 mm）；无表格返回 None。

        渲染时记录在 ``meta["md_table_box"]``（x0,y_top,x1,y_bottom），
        画布据此给 Markdown 对象显示宽度手柄——与文字文本框同一套交互。
        meta 不参与序列化，重开文档时回退到持久化的
        ``source.data["table_box"]``（重新生成失败时它仍在前次渲染的值）。
        """
        if not self._is_markdown:
            return None
        box = self.model.meta.get("md_table_box")
        if not box:
            box = self.model.source.data.get("table_box")
        if not box:
            return None
        x0, y_top, x1, y_bottom = box
        if x1 <= x0 or y_top <= y_bottom:
            return None
        return QRectF(x0, y_bottom, x1 - x0, y_top - y_bottom)

    def _layout_box(self):
        """文本排版框 (x0, y_top, x1, y_bottom)；无排版结果返回 None。

        优先取 meta 里本次排版结果；重开文档后 meta 为空（不序列化），
        回退到渲染时持久化的 ``source.data["layout_box"]``——重新生成
        失败（缺字体等）时它仍是前次渲染的正确框，调整框不会塌到兜底。
        """
        lay = self.model.meta.get("layout")
        f = frame_box(lay) if lay is not None else None
        if f is None:
            f = self.model.source.data.get("layout_box")
        if f is not None:
            try:
                x0, y_top, x1, y_bottom = (float(v) for v in f)
            except (TypeError, ValueError):
                return None
            if x1 > x0 and y_top > y_bottom:
                return (x0, y_top, x1, y_bottom)
        return None

    def _frame_rect(self) -> Optional[QRectF]:
        """文本框范围（本地 mm）；非文本对象/无排版结果返回 None。

        设了 ``frame_width``（自动换行宽）时框右缘就是换行边界；
        否则框贴当前排版宽度，供拖动「从无到有」设定宽度。
        """
        if not self._is_text:
            return None
        f = self._layout_box()
        if f is None:
            return None
        _x0, y_top, _x1, y_bottom = f
        fw = self._spec().frame_width
        if fw > 0:
            return QRectF(0.0, y_bottom, fw, y_top - y_bottom)
        return None

    def _frame_rect_or_extent(self) -> QRectF:
        """带宽度的框；未设宽度时框到排版墨迹宽度（仍可拖动手柄）。"""
        r = self._frame_rect()
        if r is not None:
            return r
        f = self._layout_box()
        if f is None:
            return QRectF(0.0, -2.0, 10.0, 4.0)
        _x0, y_top, x1, y_bottom = f
        return QRectF(0.0, y_bottom, max(x1, 5.0), y_top - y_bottom)

    def _frame_handles(self) -> tuple[Handle, ...]:
        """本对象支持「宽度框」交互的手柄集合；不支持则空元组。

        文本：6 个手柄，文字按新宽度重排（字形不缩放）。
        Markdown（含表格）：左右缘 2 个手柄，拖动改表格宽度并重排单元格。
        """
        if self._is_text:
            return self._TEXT_BOX_HANDLES
        if self._is_markdown and self._table_rect() is not None:
            return (Handle.L, Handle.R)
        return ()

    def _frame_geometry(self) -> Optional[QRectF]:
        """宽度框矩形（文本=文本框，Markdown=表格外框）；不支持返回 None。"""
        if self._is_text:
            return self._frame_rect_or_extent()
        if self._is_markdown:
            return self._table_rect()
        return None

    # 文本对象的手柄语义（Photoshop 段落文本框式）：
    # * 普通拖动 6 个手柄（四角 + 左右缘中点）→ 改文本框宽度，文字按新
    #   宽度**重新换行**，字形绝不缩放/拉伸；左右缘手柄分别以对侧缘为锚；
    # * Ctrl + 拖动 → 旧的「缩放变换」行为（整体放大缩小笔画）；
    # * T/B 手柄对文本无意义（高度随内容自动），直接不显示。
    _TEXT_BOX_HANDLES = (Handle.R, Handle.L,
                         Handle.TR, Handle.BR, Handle.TL, Handle.BL)
    _LEFT_HANDLES = (Handle.L, Handle.TL, Handle.BL)
    _TEXT_MIN_W = 5.0
    _TEXT_MAX_W = 2000.0

    def _visible_handles(self) -> list[Handle]:
        fh = self._frame_handles()
        if fh:
            return list(fh)
        return super()._visible_handles()

    def _handle_positions(self) -> dict[Handle, QPointF]:
        pts = super()._handle_positions()
        r = self._frame_geometry() if self._frame_handles() else None
        if r is not None:
            # 手柄钉到框（换行边界 / 表格外框）的缘/角上，而不是墨迹包围盒
            pts[Handle.R] = QPointF(r.right(), r.center().y())
            pts[Handle.L] = QPointF(r.left(), r.center().y())
            pts[Handle.TR] = QPointF(r.right(), r.top())
            pts[Handle.BR] = QPointF(r.right(), r.bottom())
            pts[Handle.TL] = QPointF(r.left(), r.top())
            pts[Handle.BL] = QPointF(r.left(), r.bottom())
        return pts

    def _selection_box(self) -> QRectF:
        # 文本对象只画「文本框」不画墨迹选框：拖缘改宽时框就是操作对象，
        # 两层框叠在一起反而看不出哪条是换行边界；Markdown 表格同理
        r = self._frame_geometry() if self._frame_handles() else None
        if r is not None:
            return r
        return super()._selection_box()

    # ------------------------------------------------------------- 鼠标
    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        # 先取宽度手柄集：无宽度手柄（非文本/无表格）直接走普通拖动，
        # 不触碰 event.modifiers()——合成事件的 modifiers 可能是普通 int，
        # 而 `int & Qt.ControlModifier` 会抛 TypeError。
        handles = self._frame_handles()
        if (handles and event.button() == Qt.LeftButton
                and not self.locked
                and not (event.modifiers() & Qt.ControlModifier)):
            handle = self._hit_handle(event.pos())
            if handle in handles:
                self._begin_frame_drag(event, handle)
                event.accept()
                return
        self._press_was_selected = self.isSelected()
        super().mousePressEvent(event)

    def _begin_frame_drag(self, event, handle: Handle) -> None:
        self._drag_kind = 'frame'
        self._mode = handle
        self._frame_handle = handle
        self._start_transform = self.model.transform
        self._start_scene = event.scenePos()
        self._start_local = self._start_frame_local(event.scenePos())
        geom = self._frame_geometry()
        if geom is None:
            geom = self._frame_rect_or_extent()
        self._frame_start_w = float(geom.width())
        self._frame_last_w = self._frame_start_w
        self._frame_snap = snapshot_object(self.model)
        self._pending = None

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_kind == 'frame':
            # 宽度用起始变换反算的本地坐标（与缩放手柄同一思路，避免反馈回路）
            local = self._start_frame_local(event.scenePos())
            dx = float(local.x()) - self._start_local.x()
            if self._frame_handle in self._LEFT_HANDLES:
                # 左缘手柄：右缘固定 → 宽 = 起始宽 - dx，且对象平移 dx
                new_w = self._frame_start_w - dx
            else:
                new_w = self._frame_start_w + dx
            new_w = max(self._TEXT_MIN_W, min(self._TEXT_MAX_W, new_w))
            if abs(new_w - self._frame_last_w) >= 0.25:
                self._frame_last_w = new_w
                cb = getattr(self.controller,
                             "table_frame_resizing" if self._is_markdown
                             else "text_frame_resizing", None)
                if callable(cb):
                    cb(self.model, new_w)
            # 变换预览（仅左缘手柄）——必须在重排之后套用：重排钩子里
            # 的 sync_scene 会用模型变换重置 item 变换
            if self._frame_handle in self._LEFT_HANDLES:
                shift = self._frame_start_w - self._frame_last_w
                new_t = AffineTransform.translate(shift, 0.0) @ self._start_transform
                self._pending = new_t
                self.setTransform(qtransform_from(new_t))
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_kind == 'frame':
            self._drag_kind = None
            self._mode = None
            # 松手先让主窗口把限流期间最后一次宽度同步套用（否则下面的
            # 撤销快照会记到陈旧的 frame_width/table_width）
            fin = getattr(self.controller, "frame_resize_finish", None)
            if callable(fin):
                fin()
            # 补建拖动期间跳过的命中区描边（最后一次重排在 _drag_kind 已
            # 清空后发生的话，_rebuild_path 已自行补过，此处幂等）
            if getattr(self, "_hit_deferred", False):
                self._hit_deferred = False
                self.prepareGeometryChange()
                self._rebuild()
            width_changed = abs(self._frame_last_w - self._frame_start_w) >= 0.25
            transform_changed = (self._pending is not None
                                 and self._pending != self.model.transform)
            if width_changed or transform_changed:
                # 左缘手柄同时改宽度与位移：两个命令打进同一个宏，
                # 一次撤销整步还原
                stack = self.controller.undo_stack
                stack.beginMacro("调整表格宽度" if self._is_markdown
                                 else "调整文本框宽度")
                if transform_changed:
                    self.controller.set_model_transform(
                        self.model, self._pending, "调整文本框")
                if width_changed:
                    self.controller.replace_objects(
                        [(self.model, self._frame_snap,
                          snapshot_object(self.model))],
                        "调整表格宽度" if self._is_markdown else "调整文本框宽度")
                stack.endMacro()
            self._pending = None
            self._frame_snap = None
            event.accept()
            return
        # 单击已选中的文本对象且没有拖动 → 进入就地编辑（Photoshop 文本工具式）
        if (self._drag_kind == 'move' and self._pending is None
                and getattr(self, "_press_was_selected", False)
                and self._is_text and not self.locked
                and (event.scenePos() - self._start_scene).manhattanLength() < 1.5):
            cb = getattr(self.controller, "on_text_click_edit", None)
            if callable(cb):
                self._drag_kind = None
                self._pending = None
                cb(self.model)
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """双击对象请求打开编辑器（由视图转发给主窗口）。"""
        if event.button() == Qt.LeftButton:
            cb = getattr(self.controller, "on_object_activated", None)
            if callable(cb):
                cb(self.model)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


class ReferenceGraphicsItem(TransformGraphicsItem):
    """参考层图片 / SVG：只对齐用，**不参与书写**。"""

    ACTION_NOUN = "参考图"

    def __init__(self, ref: ReferenceItem, controller: DocumentController) -> None:
        self._pix: Optional[QPixmap] = None
        self._cache_key: Optional[tuple] = None
        self._load_error = False
        super().__init__(ref, controller)

    @property
    def ref(self) -> ReferenceItem:
        return self.model

    @ref.setter
    def ref(self, value: ReferenceItem) -> None:
        self.model = value

    def sync(self) -> None:
        self.prepareGeometryChange()
        self._rebuild()
        self.setTransform(qtransform_from(self.model.transform))
        self.setOpacity(max(0.0, min(1.0, float(self.model.opacity))))
        self.setVisible(bool(self.model.visible))
        self.update()

    # ------------------------------------------------------------- 位图缓存
    def _load_pixmap(self) -> None:
        ref = self.model
        key = (ref.path, ref.kind, ref.width_mm, ref.height_mm)
        if key == self._cache_key:
            return
        self._cache_key = key
        self._pix = None
        self._load_error = False
        if not ref.path:
            self._load_error = True
            return
        try:
            if ref.kind == KIND_SVG:
                self._pix = self._render_svg(ref)
            else:
                self._pix = self._read_image(ref)
        except Exception:
            self._pix = None
        if self._pix is None or self._pix.isNull():
            self._pix = None
            self._load_error = True

    def _render_svg(self, ref: ReferenceItem) -> Optional[QPixmap]:
        from PySide6.QtSvg import QSvgRenderer
        r = QSvgRenderer(ref.path)
        if not r.isValid():
            return None
        w = max(1.0, ref.width_mm)
        h = max(1.0, ref.height_mm)
        scale = MAX_IMAGE_PX / max(w, h)
        iw, ih = max(1, int(w * scale)), max(1, int(h * scale))
        img = QImage(iw, ih, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        r.render(p)
        p.end()
        return QPixmap.fromImage(img)

    def _read_image(self, ref: ReferenceItem) -> Optional[QPixmap]:
        reader = QImageReader(ref.path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and max(size.width(), size.height()) > MAX_IMAGE_PX:
            k = MAX_IMAGE_PX / max(size.width(), size.height())
            reader.setScaledSize(size.scaled(int(size.width() * k),
                                             int(size.height() * k),
                                             Qt.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            return None
        return QPixmap.fromImage(img)

    def invalidate_cache(self) -> None:
        self._cache_key = None
        self.sync()

    def _drag_paint_hint_changed(self) -> None:
        # 拖动/缩放参考图期间关掉位图平滑插值（PS 变换时的快速预览）：
        # 大扫描件（上限 3000px、25MB+）每帧双线性重采样是拖动卡顿的
        # 大头，关掉后近似整块搬移；松手立即恢复平滑。只影响拖拽体验，
        # 静止时画质不变。
        self._set_views_smooth_pixmap(not self._drag_kind)

    def _rebuild(self) -> None:
        self._load_pixmap()

    # --------------------------------------------------------------- 绘制
    def _local_bbox(self) -> QRectF:
        return QRectF(0.0, 0.0, max(0.0, self.model.width_mm),
                      max(0.0, self.model.height_mm))

    def _hit_content(self, local_pos: QPointF) -> bool:
        return self._local_bbox().contains(local_pos)

    def _resize_snap(self, handle: Handle, anchor: QPointF,
                     sx: float, sy: float) -> tuple[float, float]:
        """缩放时把被拖动的边/角吸附到纸张边缘。

        吸附目标 = 页面外框 + 页边距内框（贴板/居中模板都按这两条线对齐）。
        在页面空间求解：锚点（对侧手柄）固定不动，被拖手柄的页面位置是
        缩放系数的仿射函数，令其落在吸附目标上即可反解出 ``sx``/``sy``——
        因此旋转过的参考图也能正确吸附。
        """
        if not bool(getattr(self.model, "snap_to_page", False)):
            return sx, sy
        page = getattr(self.controller.doc, "page", None)
        if page is None:
            return sx, sy
        m = max(0.0, min(page.margin, page.width / 2.0, page.height / 2.0))
        xs = sorted({0.0, page.width, m, page.width - m})
        ys = sorted({0.0, page.height, m, page.height - m})

        pos = self._handle_positions()
        P = pos[handle]
        ax, ay = anchor.x(), anchor.y()
        t = self._start_transform
        bx, by = t.apply((ax, ay))
        # 手柄页面位置 = b + sx·ux + sy·uy。ux/uy 是缩放系数在页面空间的
        # 位移导数（由锚点与手柄本地位置决定）。参考图旋转后，某个手柄在
        # 页面里可能沿 x 或沿 y 移动，导数向量天然指明了该解哪个系数——
        # 不按「手柄本地轴」判断，否则旋转 90° 后 R 手柄沿 y 移动会漏吸附。
        ux = t.apply((ax + (P.x() - ax), ay))
        uy = t.apply((ax, ay + (P.y() - ay)))
        ux = (ux[0] - bx, ux[1] - by)
        uy = (uy[0] - bx, uy[1] - by)
        tol = SNAP_PX * self._scene_per_px()

        # x 方向：优先用对 x 影响大的那个系数（|ux[0]| vs |uy[0]|）
        if max(abs(ux[0]), abs(uy[0])) > 1e-9:
            hx = bx + sx * ux[0] + sy * uy[0]
            tx = _snap_coord(xs, hx, tol)
            if tx is not None:
                if abs(ux[0]) >= abs(uy[0]):
                    sx = (tx - bx - sy * uy[0]) / ux[0]
                else:
                    sy = (tx - bx - sx * ux[0]) / uy[0]
        # y 方向（用已更新的 sx/sy，避免两轴抢同一个系数时互相抵消）
        if max(abs(ux[1]), abs(uy[1])) > 1e-9:
            hy = by + sx * ux[1] + sy * uy[1]
            ty = _snap_coord(ys, hy, tol)
            if ty is not None:
                if abs(uy[1]) >= abs(ux[1]):
                    sy = (ty - by - sx * ux[1]) / uy[1]
                else:
                    sx = (ty - by - sy * uy[1]) / ux[1]
        return sx, sy

    def _paint_content(self, painter: QPainter) -> None:
        box = self._local_bbox()
        if self._pix is not None:
            # item 本地坐标为 Y 向上，而位图是 Y 向下；直接 drawPixmap 会把图
            # 上下颠倒。这里先把画笔翻到「位图坐标系」再绘制，使图片顶部落在
            # 本地 y=height（视觉上方）。
            painter.save()
            painter.translate(box.left(), box.top() + box.height())
            painter.scale(1.0, -1.0)
            painter.drawPixmap(QRectF(0.0, 0.0, box.width(), box.height()),
                               self._pix, QRectF(self._pix.rect()))
            painter.restore()
        else:
            painter.fillRect(box, QColor(235, 235, 240))
        # 细边框，方便看出参考图边界
        pen = QPen(COLOR_REF_BORDER)
        pen.setCosmetic(True)
        pen.setWidthF(1.0)
        pen.setStyle(Qt.DotLine)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(box)
        if self._pix is None:
            painter.setPen(QPen(COLOR_REF_BORDER))
            painter.drawText(box.adjusted(2, 2, -2, -2),
                             Qt.AlignCenter | Qt.TextWordWrap,
                             f"参考图无法显示\n{self.model.path or '(无路径)'}")

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            cb = getattr(self.controller, "on_reference_activated", None)
            if callable(cb):
                cb(self.model)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


__all__ = [
    "Handle",
    "TransformGraphicsItem",
    "ObjectGraphicsItem",
    "ReferenceGraphicsItem",
    "qtransform_from",
    "COLOR_MARKER",
    "HANDLE_PX",
    "MARGIN_MM",
]
