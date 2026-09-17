"""画布视图：网格/页面渲染 + 平移、缩放、框选、参考层与笔位标记。

关键点：scene 坐标 = 页面坐标（mm，Y 向上）；视图变换里对 Y 取负，
从而把「Y 向上」翻转为屏幕方向。所有交互（滚轮缩放、中键平移、框选）
由 QGraphicsView 原生处理，文档层无需关心屏幕方向。

参考层（图片/SVG）绘制在书写对象**下方**，仅用于对齐纸张，不参与导出。
笔位标记（当前笔头位置）绘制在最上层，可用鼠标直接拖动。
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView

from .controller import DocumentController
from .items import (
    COLOR_MARKER,
    ObjectGraphicsItem,
    ReferenceGraphicsItem,
)

COLOR_BG = QColor(96, 99, 106)
COLOR_PAPER = QColor(255, 255, 255)
COLOR_PAGE_BORDER = QColor(70, 70, 78)
COLOR_GRID_MINOR = QColor(0, 0, 0, 16)
COLOR_GRID_MAJOR = QColor(0, 0, 0, 40)
COLOR_GUIDE = QColor(0, 150, 170, 150)
COLOR_ORIGIN = QColor(30, 90, 220)     # 机械原点圆圈
COLOR_AXIS_X = QColor(214, 48, 49)     # X 轴（红）
COLOR_AXIS_Y = QColor(0, 153, 68)      # Y 轴（绿）

MIN_ZOOM = 0.2
MAX_ZOOM = 200.0

MARKER_HIT_PX = 10.0     # 笔位标记点击命中半径(px)
MARKER_RADIUS_PX = 7.0   # 笔位标记绘制半径(px)
ORIGIN_AXIS_PX = 34.0    # 坐标轴箭头长度(px)
ORIGIN_RADIUS_PX = 4.0   # 原点圆圈半径(px)


class CanvasView(QGraphicsView):
    """写字机内容画布。"""

    mouseMoved = Signal(QPointF)   # scene 坐标(mm)
    zoomChanged = Signal(float)
    penMarkerMoved = Signal(float, float)   # 笔位标记拖动到页面坐标(mm)
    interactionStarted = Signal()  # 用户开始交互（按下/滚轮/拖动中），供
                                   # 后台任务让路（如字体缩略图解析暂停）

    def __init__(self, controller: DocumentController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setRubberBandSelectionMode(Qt.IntersectsItemShape)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        # 局部重绘：拖动/旋转对象时只重画受影响的区域（全视口重绘在带
        # 参考图+网格的画面上每帧成本太高，旋转手感发卡）。标记/原点
        # 标注变化处都显式调用 viewport().update()，不受影响。
        self.setViewportUpdateMode(QGraphicsView.MinimalViewportUpdate)
        # 背景缓存：网格/纸面/边框/参考线只画一次，之后每帧直接贴缓存。
        # 拖动参考图/对象时曝光区要连同背景一起重画，A4 网格逐线重画在
        # 实机（HiDPI 大窗口）上是拖动卡顿的大头——实测移动 6.3ms/帧 →
        # 0.54ms/帧，平移也更快。视图变形/尺寸变化时 Qt 会自动失效缓存；
        # 页面几何变化在 _update_scene_rect 里、参考线开关在下面的属性
        # setter 里显式失效。**滚动条滚动 Qt 不会重建缓存**（网格会"冻"
        # 在旧位置、与场景错位）——由下面的 scrollContentsBy 失效。
        self.setCacheMode(QGraphicsView.CacheBackground)
        self.setMouseTracking(True)
        self.setBackgroundBrush(COLOR_BG)

        self._items: dict[str, ObjectGraphicsItem] = {}
        self._ref_items: dict[str, ReferenceGraphicsItem] = {}
        self._panning = False
        self._pan_start = QPoint()
        self._pick_mode = False
        self._pick_callback: Optional[Callable] = None
        self._pick_hint = ""        # 笔位标记
        self._marker: Optional[tuple[float, float]] = None
        self._marker_dragging = False
        # 笔画编辑工具（由主窗口在进入模式时注入 StrokeEditor；None=未启用）
        self.stroke_editor = None
        # 文字就地编辑会话（由主窗口在开始编辑时注入 TextEditSession）
        self.text_session = None
        self.setFocusPolicy(Qt.StrongFocus)   # 接收 Esc 退出笔画编辑
        # 机械原点标注：(原点页面坐标, X 轴单位方向, Y 轴单位方向)，页面 mm
        self._origin: Optional[tuple[tuple[float, float],
                                     tuple[float, float],
                                     tuple[float, float]]] = None
        self._show_page_guide = True    # 是否绘制页边距参考线
        # 背景缓存对应的页面几何（变化时失效缓存）
        self._bg_page_key: Optional[tuple] = None
        # 视口浮动控件（笔画编辑操作条）：(widget, 锚定位置)，滚动后被
        # blit 挪走时复位
        self._overlays: list[tuple, QPoint] = []

        controller.documentChanged.connect(self.sync_scene)
        controller.documentReplaced.connect(self.rebuild_scene)
        self._scene.selectionChanged.connect(self._on_scene_selection)

        self.rebuild_scene()

    # --------------------------------------------------------------- 场景同步
    def rebuild_scene(self) -> None:
        self._scene.clear()
        self._items.clear()
        self._ref_items.clear()
        # 参考层在底层（负 z），书写对象在上层
        for i, ref in enumerate(self.controller.doc.references):
            item = ReferenceGraphicsItem(ref, self.controller)
            item.setZValue(-1000 + i)
            self._scene.addItem(item)
            self._ref_items[ref.id] = item
        for i, obj in enumerate(self.controller.doc.objects):
            item = ObjectGraphicsItem(obj, self.controller)
            item.setZValue(i)
            self._scene.addItem(item)
            self._items[obj.id] = item
        self._update_scene_rect()
        self.viewport().update()

    def sync_scene(self) -> None:
        doc = self.controller.doc

        alive_refs: set[str] = set()
        for i, ref in enumerate(doc.references):
            alive_refs.add(ref.id)
            item = self._ref_items.get(ref.id)
            if item is None:
                item = ReferenceGraphicsItem(ref, self.controller)
                self._scene.addItem(item)
                self._ref_items[ref.id] = item
            item.ref = ref
            item.setZValue(-1000 + i)
            item.sync()
        for rid in list(self._ref_items):
            if rid not in alive_refs:
                item = self._ref_items.pop(rid)
                self._scene.removeItem(item)

        alive: set[str] = set()
        for i, obj in enumerate(doc.objects):
            alive.add(obj.id)
            item = self._items.get(obj.id)
            if item is None:
                item = ObjectGraphicsItem(obj, self.controller)
                self._scene.addItem(item)
                self._items[obj.id] = item
            item.obj = obj
            item.setZValue(i)
            item.sync()
        for oid in list(self._items):
            if oid not in alive:
                item = self._items.pop(oid)
                self._scene.removeItem(item)

        self._update_scene_rect()

        # 笔画编辑会话的世界缓存随文档刷新（撤销/重做后高亮/手柄否则
        # 会拿旧缓存画在旧位置，留下残影，直到点击画布才消失）
        se = self.stroke_editor
        if se is not None:
            se.on_document_synced()

    def sync_object(self, obj) -> None:
        """只同步单个书写对象（轻量；供拖动重排等高频路径使用）。

        :meth:`sync_scene` 会遍历全部对象/参考图并对每个 item 调
        ``update()``——高频调用时（拖宽度手柄）无关的参考图也被反复标脏，
        整视口跟着重绘。对象还没有 item 时（刚添加未同步过）退化为全量。
        """
        item = self._items.get(obj.id)
        if item is None:
            self.sync_scene()
            return
        item.sync()

    def _update_scene_rect(self) -> None:
        page = self.controller.doc.page
        pad = 40.0
        self.setSceneRect(QRectF(-pad, -pad, page.width + 2 * pad, page.height + 2 * pad))
        # 页面几何（尺寸/边距）变了 → 画进背景缓存的纸框/参考线已过期。
        # setSceneRect 相同值时 Qt 不会失效缓存，边距单独改必须这里兜底。
        key = (page.width, page.height, page.margin)
        if key != self._bg_page_key:
            self._bg_page_key = key
            self.resetCachedContent()

    @property
    def show_page_guide(self) -> bool:
        return self._show_page_guide

    @show_page_guide.setter
    def show_page_guide(self, on: bool) -> None:
        if bool(on) == self._show_page_guide:
            return
        self._show_page_guide = bool(on)
        self.resetCachedContent()

    def _on_scene_selection(self) -> None:
        self.controller.notify_selection()

    # --------------------------------------------------------------- 选择
    def selected_objects(self):
        return [it.obj for it in self._scene.selectedItems()
                if isinstance(it, ObjectGraphicsItem)]

    def selected_references(self):
        return [it.ref for it in self._scene.selectedItems()
                if isinstance(it, ReferenceGraphicsItem)]

    def selected_models(self) -> list:
        """被选中的书写对象 + 参考图（统一下标）。"""
        return self.selected_objects() + self.selected_references()

    def select_object(self, obj) -> None:
        self._scene.clearSelection()
        item = self._items.get(obj.id)
        if item is not None:
            item.setSelected(True)
            self.ensureVisible(item, 40, 40)

    def select_reference(self, ref) -> None:
        item = self._ref_items.get(ref.id)
        if item is not None:
            self._scene.clearSelection()
            item.setSelected(True)
            self.ensureVisible(item, 40, 40)

    def clear_selection(self) -> None:
        self._scene.clearSelection()

    def refresh_reference(self, ref) -> None:
        """参考图路径/尺寸变化后强制重载位图。"""
        item = self._ref_items.get(ref.id)
        if item is not None:
            item.invalidate_cache()

    # --------------------------------------------------------------- 笔位标记
    def set_pen_marker(self, pos: Optional[tuple[float, float]]) -> None:
        if self._marker_dragging:
            return          # 拖动中标记以鼠标为准，忽略程序回写（松手后统一刷新）
        self._marker = pos
        self.viewport().update()

    def pen_marker(self) -> Optional[tuple[float, float]]:
        return self._marker

    def set_machine_origin(self, pos: Optional[tuple[float, float]],
                           x_dir: tuple[float, float] = (1.0, 0.0),
                           y_dir: tuple[float, float] = (0.0, 1.0)) -> None:
        """设置机械原点标注（页面坐标 + X/Y 轴单位方向）；pos=None 隐藏。

        机械原点恒定在纸张左上角（由主窗口传入），X 沿顶边向右、Y 沿
        左边向下，不随起点拖动/纸张旋转变化。
        """
        self._origin = None if pos is None else (pos, x_dir, y_dir)
        self.viewport().update()

    def machine_origin(self):
        """当前机械原点标注 (原点页面坐标, X 方向, Y 方向)；未设置返回 None。"""
        return self._origin

    def _marker_view_pos(self) -> Optional[QPointF]:
        if self._marker is None:
            return None
        return QPointF(self.mapFromScene(QPointF(self._marker[0], self._marker[1])))

    def _hit_marker(self, view_pos: QPoint) -> bool:
        p = self._marker_view_pos()
        if p is None:
            return False
        return (abs(view_pos.x() - p.x()) <= MARKER_HIT_PX
                and abs(view_pos.y() - p.y()) <= MARKER_HIT_PX)

    # ----------------------------------------------------------------- 背景
    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, COLOR_BG)

        page = self.controller.doc.page
        page_rect = QRectF(0.0, 0.0, page.width, page.height)
        painter.fillRect(page_rect, COLOR_PAPER)

        # 仅在页面内绘制网格
        painter.save()
        painter.setClipRect(page_rect)
        minor = QPen(COLOR_GRID_MINOR)
        minor.setCosmetic(True)
        minor.setWidthF(1.0)
        major = QPen(COLOR_GRID_MAJOR)
        major.setCosmetic(True)
        major.setWidthF(1.0)

        # 纸张较大时（如 A2/A1）网格过密，自动放大间距
        step = 10.0
        while max(page.width, page.height) / step > 120.0:
            step *= 2.0
        # 同级线合成一条 path 一次画完（逐线 setPen+drawLine 在整页重绘时
        # 开销明显，拖动/旋转会卡）
        minor_path = QPainterPath()
        major_path = QPainterPath()

        def _add_line(path: QPainterPath, x1: float, y1: float,
                      x2: float, y2: float) -> None:
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)

        x = 0.0
        while x <= page.width + 1e-6:
            path = major_path if abs(x % (step * 5)) < 1e-6 else minor_path
            _add_line(path, x, 0.0, x, page.height)
            x += step
        y = 0.0
        while y <= page.height + 1e-6:
            path = major_path if abs(y % (step * 5)) < 1e-6 else minor_path
            _add_line(path, 0.0, y, page.width, y)
            y += step
        painter.setPen(minor)
        painter.drawPath(minor_path)
        painter.setPen(major)
        painter.drawPath(major_path)
        painter.restore()

        # 页边距参考线（帮助对齐纸内区域）
        if self.show_page_guide and page.margin > 0:
            mb = page.margin_bbox()
            if mb.width > 0 and mb.height > 0:
                gp = QPen(COLOR_GUIDE)
                gp.setCosmetic(True)
                gp.setWidthF(1.0)
                gp.setStyle(Qt.DashLine)
                painter.setPen(gp)
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(mb.x0, mb.y0, mb.width, mb.height))

        border = QPen(COLOR_PAGE_BORDER)
        border.setCosmetic(True)
        border.setWidthF(1.4)
        painter.setPen(border)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(page_rect)

    # --------------------------------------------------------------- 前景
    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        """在最上层绘制笔位标记与机械原点/坐标轴标注。

        鼠标坐标不再画在画布上（状态栏右下角已有显示），也就不需要
        鼠标移动时全视口重绘——大参考图下拖动明显更流畅。
        """
        # 文字编辑会话：光标/选区/文本框（场景坐标 mm 绘制）
        ts = self.text_session
        if ts is not None:
            painter.save()
            ts.paint(painter)
            painter.restore()
        # 笔画编辑的高亮/手柄：场景坐标（mm）绘制，线宽用 cosmetic 保持像素宽
        se = self.stroke_editor
        if se is not None and (se.active or se.is_dragging()):
            painter.save()
            se.paint(painter)
            painter.restore()
        painter.save()
        painter.resetTransform()
        if self._origin is not None:
            self._draw_machine_origin(painter)
        if self._marker is not None:
            self._draw_pen_marker(painter)
        painter.restore()

    def _draw_machine_origin(self, painter: QPainter) -> None:
        """机械原点：圆圈十字 + 两根不同颜色的轴箭头（X 红、Y 绿，屏幕像素尺寸）。"""
        pos, x_dir, y_dir = self._origin
        # mapFromScene 返回 QPoint，与 QPointF 混算在 PySide6 里会炸，先包一层
        vp = QPointF(self.mapFromScene(QPointF(pos[0], pos[1])))

        def _screen_dir(d: tuple[float, float]) -> QPointF:
            # 页面 Y 向上、视图变换里翻了 Y，方向一律用两点换算，别手推符号
            p2 = QPointF(pos[0] + d[0], pos[1] + d[1])
            s2 = QPointF(self.mapFromScene(p2))
            vec = s2 - vp
            n = vec.manhattanLength()
            return vec / n if n > 1e-9 else QPointF(1, 0)

        pen = QPen(COLOR_ORIGIN)
        pen.setWidthF(1.6)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(vp, ORIGIN_RADIUS_PX, ORIGIN_RADIUS_PX)
        painter.drawLine(QPointF(vp.x() - 8, vp.y()), QPointF(vp.x() - 4, vp.y()))
        painter.drawLine(QPointF(vp.x() + 4, vp.y()), QPointF(vp.x() + 8, vp.y()))
        painter.drawLine(QPointF(vp.x(), vp.y() - 8), QPointF(vp.x(), vp.y() - 4))
        painter.drawLine(QPointF(vp.x(), vp.y() + 4), QPointF(vp.x(), vp.y() + 8))

        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        fm = painter.fontMetrics()
        for d, label, color in ((x_dir, "X", COLOR_AXIS_X),
                                (y_dir, "Y", COLOR_AXIS_Y)):
            sdir = _screen_dir(d)
            tip = vp + sdir * ORIGIN_AXIS_PX
            axpen = QPen(color)
            axpen.setWidthF(1.6)
            painter.setPen(axpen)
            painter.drawLine(vp, tip)
            # 箭头头部
            back = vp + sdir * (ORIGIN_AXIS_PX - 7.0)
            side = QPointF(-sdir.y(), sdir.x()) * 3.0
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([tip, back + side, back - side]))
            painter.setBrush(Qt.NoBrush)
            # 轴标签放在箭头稍外，颜色与轴一致
            lp = vp + sdir * (ORIGIN_AXIS_PX + 8.0)
            painter.drawText(QPointF(lp.x() - fm.horizontalAdvance(label) / 2,
                                     lp.y() + fm.ascent() / 2), label)

    def _draw_pen_marker(self, painter: QPainter) -> None:
        vp = QPointF(self.mapFromScene(QPointF(self._marker[0], self._marker[1])))
        pen = QPen(COLOR_MARKER)
        pen.setWidthF(1.8)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        r = MARKER_RADIUS_PX
        painter.drawEllipse(vp, r, r)
        painter.drawLine(QPointF(vp.x() - r - 5, vp.y()), QPointF(vp.x() + r + 5, vp.y()))
        painter.drawLine(QPointF(vp.x(), vp.y() - r - 5), QPointF(vp.x(), vp.y() + r + 5))
        tri = QPolygonF([QPointF(vp.x() - 4, vp.y() - r - 12),
                         QPointF(vp.x() + 4, vp.y() - r - 12),
                         QPointF(vp.x(), vp.y() - r - 4)])
        painter.setBrush(COLOR_MARKER)
        painter.drawPolygon(tri)

    def leaveEvent(self, event) -> None:
        se = self.stroke_editor
        if se is not None and se.active:
            se.update_hover(None)        # 鼠标移出画布 → 清除悬停高亮
        super().leaveEvent(event)

    # ------------------------------------------------------------ 文字编辑
    def _session_wants_key(self, event) -> bool:
        """编辑会话是否要接管该键（用于覆盖窗口级快捷键）。"""
        ts = self.text_session
        if ts is None:
            return False
        key = event.key()
        mods = event.modifiers()
        if mods & (Qt.ControlModifier | Qt.MetaModifier):
            return key in (Qt.Key_Z, Qt.Key_Y, Qt.Key_A,
                           Qt.Key_C, Qt.Key_X, Qt.Key_V)
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Meta, Qt.Key_Alt,
                   Qt.Key_AltGr, Qt.Key_CapsLock, Qt.Key_NumLock):
            return False
        return True

    def event(self, e) -> bool:
        # 编辑会话活动期间，把 Delete/字符/导航键从窗口快捷键（如「删除
        # 对象」）手里抢回来，否则 Delete 会直接删掉整个对象
        if (e.type() == QEvent.ShortcutOverride
                and self.text_session is not None
                and self._session_wants_key(e)):
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, event) -> None:
        ts = self.text_session
        if ts is not None:
            if event.key() == Qt.Key_Escape:
                ts.finish()
                event.accept()
                return
            if ts.handle_key(event):
                event.accept()
                return
        se = self.stroke_editor
        if (se is not None and se.active
                and event.key() == Qt.Key_Escape):
            se.requestExit.emit()      # 主窗口负责退出笔画编辑模式
            event.accept()
            return
        super().keyPressEvent(event)

    def inputMethodEvent(self, event) -> None:
        ts = self.text_session
        if ts is None:
            super().inputMethodEvent(event)
            return
        commit = event.commitString()
        if commit:
            ts.insert_text(commit)
        pre = event.preeditString()
        ts.set_preedit(pre)
        event.accept()

    def inputMethodQuery(self, query):
        ts = self.text_session
        if ts is not None:
            if query == Qt.ImEnabled:
                return True
            if query == Qt.ImCursorRectangle:
                r = ts.caret_view_rect()
                return r if r is not None else QRectF()
            if query == Qt.ImCursorPosition:
                return ts.caret
            if query == Qt.ImAnchorPosition:
                return ts.anchor
            if query == Qt.ImSurroundingText:
                return ts.text
            if query == Qt.ImCurrentSelection:
                return ts.selected_chars()
            if query == Qt.ImHints:
                return Qt.InputMethodHint.ImhNone
        return super().inputMethodQuery(query)

    # ----------------------------------------------------------------- 缩放
    def current_zoom(self) -> float:
        return abs(self.transform().m11()) or 1.0

    def wheelEvent(self, event) -> None:
        self.interactionStarted.emit()
        factor = 1.0015 ** event.angleDelta().y()
        self._zoom_at(event.position().toPoint(), factor)
        event.accept()

    def _zoom_at(self, view_pos: QPoint, factor: float) -> None:
        old_scene = self.mapToScene(view_pos)
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.current_zoom() * factor))
        factor = new_zoom / self.current_zoom()
        if abs(factor - 1.0) < 1e-9:
            return
        # 以鼠标位置为锚：缩放后修正平移，使锚点 scene 坐标不变
        self.scale(factor, factor)
        new_scene = self.mapToScene(view_pos)
        delta = new_scene - old_scene
        self.translate(delta.x(), delta.y())
        self.zoomChanged.emit(self.current_zoom())

    def zoom_in(self) -> None:
        self._zoom_at(self.viewport().rect().center(), 1.25)

    def zoom_out(self) -> None:
        self._zoom_at(self.viewport().rect().center(), 1.0 / 1.25)

    def fit_page(self) -> None:
        self._update_scene_rect()
        page = self.controller.doc.page
        rect = self.sceneRect()
        vw = max(1, self.viewport().width())
        vh = max(1, self.viewport().height())
        z = min(vw / rect.width(), vh / rect.height())
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        self.setTransform(QTransform().scale(z, -z))
        self.centerOn(page.width / 2.0, page.height / 2.0)
        self.zoomChanged.emit(self.current_zoom())

    def zoom_100(self) -> None:
        # 1mm = 3.78px（约 96dpi），便于直观预览实际尺寸
        z = 3.7795
        self.setTransform(QTransform().scale(z, -z))
        page = self.controller.doc.page
        self.centerOn(page.width / 2.0, page.height / 2.0)
        self.zoomChanged.emit(self.current_zoom())

    def selection_bbox(self) -> Optional[QRectF]:
        """当前选中项（书写对象 + 参考图）在页面坐标下的包围矩形。"""
        rect: Optional[QRectF] = None
        items = [it for it in self._scene.selectedItems()
                 if isinstance(it, (ObjectGraphicsItem, ReferenceGraphicsItem))]
        for it in items:
            r = it.sceneBoundingRect()
            rect = r if rect is None else rect.united(r)
        return rect

    def zoom_to_selection(self, margin: float = 10.0) -> bool:
        """缩放到选中内容；无选择返回 False。"""
        rect = self.selection_bbox()
        if rect is None or rect.isEmpty():
            return False
        rect = rect.adjusted(-margin, -margin, margin, margin)
        vw = max(1, self.viewport().width())
        vh = max(1, self.viewport().height())
        z = min(vw / rect.width(), vh / rect.height())
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        self.setTransform(QTransform().scale(z, -z))
        self.centerOn(rect.center())
        self.zoomChanged.emit(self.current_zoom())
        return True

    # ------------------------------------------------------------- 起点拾取
    def set_pick_mode(self, enabled: bool,
                      callback: Optional[Callable[[float, float], None]] = None,
                      hint: str = "") -> None:
        """开启后，下一次左键点击会回调页面坐标(mm) 并自动退出。

        ``hint`` 用于区分拾取目标（起点 / 笔位标记），仅作提示。
        """
        self._pick_mode = enabled
        self._pick_callback = callback
        self._pick_hint = hint
        self.setCursor(Qt.CrossCursor if enabled else Qt.ArrowCursor)
        self.setDragMode(QGraphicsView.NoDrag if enabled
                         else QGraphicsView.RubberBandDrag)

    # ----------------------------------------------------------------- 平移
    def scrollContentsBy(self, dx: int, dy: int) -> None:
        super().scrollContentsBy(dx, dy)
        if dx or dy:
            # CacheBackground 的缓存在滚动条滚动时不会重建：平移后网格/
            # 纸张会"冻"在旧位置、与场景错位（物件正常移动，笔画编辑的
            # 局部重绘预览也会被错位背景糊住——表现为"笔画拖不动"）。
            # 滚动本来就是全视口变化，立即作废缓存让下一帧全量重绘背景
            # （开销与无缓存模式相同）；对象/参考图拖动不触发滚动，仍享
            # 受缓存加速。
            self.resetCachedContent()
        # viewport 上的浮动控件（笔画编辑操作条）会被滚动的 blit 一起
        # 挪走——复位到注册的锚定位置
        for w, pos in self._overlays:
            if not w.isHidden():
                w.move(pos)

    def register_overlay(self, w, pos) -> None:
        """把视口上的浮动控件注册到锚定位置（滚动后自动复位）。"""
        self._overlays.append((w, pos))
        w.move(pos)

    def mousePressEvent(self, event) -> None:
        self.interactionStarted.emit()
        scene_pos = self.mapToScene(event.position().toPoint())
        if self._pick_mode and event.button() == Qt.LeftButton:
            p = scene_pos
            cb = self._pick_callback
            self.set_pick_mode(False, None)
            if cb is not None:
                cb(float(p.x()), float(p.y()))
            event.accept()
            return
        # 笔位标记优先：直接拖动标记（不改动选择）
        if (event.button() == Qt.LeftButton and self._marker is not None
                and self._hit_marker(event.position().toPoint())):
            self._marker_dragging = True
            self.setCursor(Qt.SizeAllCursor)
            event.accept()
            return
        # 文字编辑会话：框内接管（定位光标/框选）；框外则提交并结束编辑
        ts = self.text_session
        if (ts is not None and event.button() == Qt.LeftButton
                and not (event.modifiers() & Qt.AltModifier)):
            if ts.on_press(scene_pos, event):
                event.accept()
                return
            ts.finish()
            event.accept()
            return
        # 笔画编辑工具：模式内完全接管左键（不移动对象、不框选）
        se = self.stroke_editor
        if (se is not None and se.active and event.button() == Qt.LeftButton
                and not (event.modifiers() & Qt.AltModifier)):
            if se.on_press(scene_pos, event.modifiers()):
                event.accept()
                return
        if event.button() == Qt.MiddleButton or (
                event.button() == Qt.LeftButton
                and event.modifiers() & Qt.AltModifier):
            self._panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        # 任何按键按下的移动都属于拖动交互（对象/标记/平移/框选）——
        # 让后台任务持续让路。scene 会把事件发给 item，这里只是旁听。
        if event.buttons() != Qt.NoButton:
            self.interactionStarted.emit()
        scene_pos = self.mapToScene(event.position().toPoint())
        self.mouseMoved.emit(scene_pos)
        ts = self.text_session
        if ts is not None:
            if ts.is_dragging():
                ts.on_move(scene_pos)
                event.accept()
                return
        se = self.stroke_editor
        if se is not None and se.active:
            if se.is_dragging():
                se.on_move(scene_pos)
                event.accept()
                return
            if not self._marker_dragging and not self._panning:
                se.update_hover(scene_pos)   # 悬停笔画高亮
        if self._marker_dragging:
            self._marker = (float(scene_pos.x()), float(scene_pos.y()))
            self.penMarkerMoved.emit(self._marker[0], self._marker[1])
            self.viewport().update()      # 只为移动笔位标记重绘
            event.accept()
            return
        if self._panning:
            pos = event.position().toPoint()
            delta = pos - self._pan_start
            self._pan_start = pos
            h = self.horizontalScrollBar()
            v = self.verticalScrollBar()
            # 视图 Y 已翻转：垂直方向符号取反
            h.setValue(h.value() - delta.x())
            v.setValue(v.value() - delta.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # 笔画编辑/文字编辑会话期间，双击不许穿透到场景（否则会误开
        # 对象的属性对话框）；按普通按下处理
        scene_pos = self.mapToScene(event.position().toPoint())
        ts = self.text_session
        se = self.stroke_editor
        if (se is not None and se.active
                and event.button() == Qt.LeftButton
                and not (event.modifiers() & Qt.AltModifier)):
            se.on_press(scene_pos, event.modifiers())
            event.accept()
            return
        if ts is not None and event.button() == Qt.LeftButton:
            ts.on_press(scene_pos, event)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        ts = self.text_session
        if (ts is not None and ts.is_dragging()
                and event.button() == Qt.LeftButton):
            ts.on_release()
            event.accept()
            return
        se = self.stroke_editor
        if (se is not None and se.active and se.is_dragging()
                and event.button() == Qt.LeftButton):
            se.on_release()
            event.accept()
            return
        if self._marker_dragging and event.button() == Qt.LeftButton:
            self._marker_dragging = False
            self.setCursor(Qt.CrossCursor if self._pick_mode else Qt.ArrowCursor)
            if self._marker is not None:
                self.penMarkerMoved.emit(self._marker[0], self._marker[1])
            event.accept()
            return
        if self._panning and event.button() in (Qt.MiddleButton, Qt.LeftButton):
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)
