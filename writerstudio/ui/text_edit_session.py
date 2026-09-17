"""画布文字直接编辑会话（Photoshop 文本工具式）。

进入编辑后画布上出现**真实光标与字符选区**：单击定位、拖动框选、
直接敲键输入（含中文输入法），改动经 :meth:`MainWindow.on_text_session_changed`
写回对象并即时重排——文字始终以手写笔画显示，**没有任何叠加的
文本框控件**，所见即所得。

索引/几何约定：

* ``caret`` / ``anchor`` 为源文本（``spec.text``，含 ``\\n``）的绝对下标；
  选区 = ``[min(anchor, caret), max(anchor, caret))``。
* 字符命中与光标几何基于 ``obj.meta["layout"]``（:class:`TextLayout`，
  对象本地坐标 mm）；scene → 本地由 ``obj.transform`` 逆变换完成。
* 光标/选区/预编辑串由 :class:`CanvasView` 的 ``drawForeground`` 调
  :meth:`paint` 绘制；输入法事件与查询由 CanvasView 转发进来。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import (QObject, QPointF, QRect, QRectF, Qt,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication

from ..fonts.builder import TextSpec
from ..fonts.layout import CharPlacement, TextLayout, frame_box
from .items import qtransform_from

COLOR_CURSOR = QColor(0, 102, 204, 230)
COLOR_SELECTION = QColor(0, 120, 215, 66)
COLOR_FRAME = QColor(0, 122, 204, 130)
COLOR_PREEDIT = QColor(20, 20, 30)

_FRAME_MARGIN_MM = 3.0     # 点击命中范围在文本框外扩这么多


class TextEditSession(QObject):
    """一个文本对象的就地编辑会话（生命周期 = 一次编辑）。"""

    #: 文本被修改（主窗口负责写回对象、撤销合并与 sync_scene）
    textChanged = Signal(str)
    #: 字符选区变化 [start, end)，end==start 表示无选区
    charSelectionChanged = Signal(int, int)
    finished = Signal()

    BLINK_MS = 530

    def __init__(self, canvas, obj, main) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.obj = obj
        self.main = main                     # MainWindow：font_manager/controller
        self.text: str = TextSpec.from_data(obj.source.data).text
        self.caret: int = len(self.text)
        self.anchor: int = self.caret
        self.preedit: str = ""               # 输入法预编辑串（尚未上屏）
        self.preedit_at: int = 0             # 预编辑开始时的光标位置
        self._drag = False
        self._column_x: Optional[float] = None   # 上下导航锁定的目标列 x（本地 mm）
        self._fonts: dict[str, object] = {}
        self._last_view_rect: Optional[QRectF] = None
        self.caret_visible = True
        self._blink = QTimer(self)
        self._blink.setInterval(self.BLINK_MS)
        self._blink.timeout.connect(self._toggle_blink)

    # ------------------------------------------------------------ 生命周期
    def sync_after_commit(self) -> None:
        """主窗口写回并重排之后调用：钳制光标位置并刷新光标区域。"""
        n = len(self.text)
        self.caret = max(0, min(self.caret, n))
        self.anchor = max(0, min(self.anchor, n))
        self._emit_selection()
        self._restart_blink()
        self.update_caret_region()

    def start(self, scene_pos: Optional[QPointF] = None) -> None:
        """进入编辑；``scene_pos`` 给出时把光标放到点击处。"""
        if scene_pos is not None:
            idx = self.hit_test(scene_pos)
            self.caret = self.anchor = idx
        self._blink.start()
        self.update_caret_region()

    def finish(self) -> None:
        """结束编辑（文本已逐键提交，这里只做收尾）。"""
        if self._blink.isActive():
            self._blink.stop()
        if self.preedit:
            self.preedit = ""
            QApplication.inputMethod().reset()
        self._repaint_last()
        self.finished.emit()

    # ------------------------------------------------------------ 字体/布局
    def _layout(self) -> Optional[TextLayout]:
        return self.obj.meta.get("layout")

    def _font(self, name: str):
        fam = self._fonts.get(name)
        if fam is None:
            fam = self.main.font_manager.get(name)
            self._fonts[name] = fam
        return fam

    def _char_band(self, c: CharPlacement, lay: TextLayout) -> tuple[float, float]:
        """字符的上伸/下延（mm，相对基线）。"""
        fam = self._font(c.font_name)
        if fam is not None:
            return fam.ascent * c.scale, -fam.descent * c.scale
        size = lay.style.size if lay.style is not None else 10.0
        return size * 0.8, size * 0.25

    def _local_from_scene(self, p: QPointF) -> tuple[float, float]:
        x, y = self.obj.transform.invert().apply((p.x(), p.y()))
        return x, y

    def frame(self) -> Optional[tuple[float, float, float, float]]:
        """当前文本框 (x0, y_top, x1, y_bottom)，本地 mm。"""
        lay = self._layout()
        if lay is None:
            return None
        return frame_box(lay)

    # ------------------------------------------------------------ 命中测试
    def hit_test(self, scene_pos: QPointF) -> int:
        """scene 坐标 → 最近的文本下标（光标可落位置 0..len）。"""
        lay = self._layout()
        if lay is None or not lay.lines:
            return 0
        lx, ly = self._local_from_scene(scene_pos)

        best = None
        best_d = None
        for ln in lay.lines:
            f0 = self._font(ln.chars[0].font_name) if ln.chars else None
            if f0 is not None:
                asc = f0.ascent * ln.chars[0].scale
                desc = -f0.descent * ln.chars[0].scale
            else:
                size = lay.style.size if lay.style else 10.0
                asc, desc = size * 0.8, size * 0.25
            if ln.baseline_y - desc <= ly <= ln.baseline_y + asc:
                best = ln
                break
            d = min(abs(ly - (ln.baseline_y + asc)),
                    abs(ly - (ln.baseline_y - desc)))
            if best_d is None or d < best_d:
                best_d = d
                best = ln
        ln = best
        if ln is None:
            return 0
        if not ln.chars:
            return max(0, min(len(self.text), ln.text_start))
        if lx <= ln.chars[0].origin[0]:
            return ln.chars[0].text_index
        for c in ln.chars:
            if lx < c.origin[0] + c.advance / 2.0:
                return c.text_index
        return ln.chars[-1].text_index + 1

    def caret_geom(self, index: Optional[int] = None):
        """文本下标 → (x, baseline, asc, desc)（本地 mm）；找不到返回 None。"""
        if index is None:
            index = self._visual_caret()
        lay = self._layout()
        if lay is None:
            return None
        for ln in lay.lines:
            for c in ln.chars:
                if c.text_index == index:
                    asc, desc = self._char_band(c, lay)
                    return (c.origin[0], ln.baseline_y, asc, desc)
            # 空行上的光标
            if not ln.chars and ln.text_start == index:
                size = lay.style.size if lay.style else 10.0
                return (ln.x_start, ln.baseline_y, size * 0.8, size * 0.25)
        # index 是行尾（或落在缺字/空白上）：回退到它之前最近的字符
        best = None
        for ln in lay.lines:
            for c in ln.chars:
                if c.text_index < index:
                    if best is None or c.text_index > best[0].text_index:
                        best = (c, ln)
        if best is not None:
            c, ln = best
            asc, desc = self._char_band(c, lay)
            return (c.origin[0] + c.advance, ln.baseline_y, asc, desc)
        for ln in lay.lines:
            if ln.chars:
                asc, desc = self._char_band(ln.chars[0], lay)
                return (ln.chars[0].origin[0], ln.baseline_y, asc, desc)
        return None

    def _visual_caret(self) -> int:
        """光标绘制位置（有预编辑串时停在预编辑串之前）。"""
        return self.preedit_at if self.preedit else self.caret

    # ------------------------------------------------------------ 选区/编辑
    def _sel_range(self) -> tuple[int, int]:
        a = max(0, min(self.anchor, len(self.text)))
        b = max(0, min(self.caret, len(self.text)))
        return (min(a, b), max(a, b))

    def has_selection(self) -> bool:
        a, b = self._sel_range()
        return b > a

    def selected_chars(self) -> str:
        a, b = self._sel_range()
        return self.text[a:b]

    def _emit_selection(self) -> None:
        a, b = self._sel_range()
        self.charSelectionChanged.emit(a, b)

    def _commit_text(self) -> None:
        self._column_x = None
        self.textChanged.emit(self.text)

    def insert_text(self, s: str) -> None:
        if not s:
            return
        a, b = self._sel_range()
        self.text = self.text[:a] + s + self.text[b:]
        self.caret = self.anchor = a + len(s)
        self._commit_text()

    def _delete(self, a: int, b: int) -> None:
        if a >= b:
            return
        self.text = self.text[:a] + self.text[b:]
        self.caret = self.anchor = a
        self._commit_text()

    def delete_backward(self) -> None:
        a, b = self._sel_range()
        self._delete(a - 1 if a == b else a, b)

    def delete_forward(self) -> None:
        a, b = self._sel_range()
        self._delete(a, b + 1 if a == b else b)

    def delete_word_backward(self) -> None:
        if self.has_selection():
            self.delete_backward()
            return
        self._delete(_word_start(self.text, self.caret), self.caret)

    def delete_word_forward(self) -> None:
        if self.has_selection():
            self.delete_forward()
            return
        self._delete(self.caret, _word_end(self.text, self.caret))

    # ------------------------------------------------------------ 光标移动
    def set_caret(self, index: int, extend: bool = False,
                  remember_column: bool = False) -> None:
        index = max(0, min(index, len(self.text)))
        if not extend:
            self.anchor = index
        self.caret = index
        if not remember_column:
            self._column_x = None
        self._emit_selection()
        self._restart_blink()
        self.update_caret_region()

    def move_horizontal(self, step: int, extend: bool = False) -> None:
        self.set_caret(self.caret + step, extend)

    def move_word(self, step: int, extend: bool = False) -> None:
        if step < 0:
            self.set_caret(_word_start(self.text, self.caret), extend)
        else:
            self.set_caret(_word_end(self.text, self.caret), extend)

    def move_to(self, index: int, extend: bool = False) -> None:
        self.set_caret(index, extend)

    def move_vertical(self, lines: int, extend: bool = False) -> None:
        lay = self._layout()
        if lay is None or not lay.lines:
            return
        if self._column_x is None:
            g = self.caret_geom()
            if g is None:
                return
            self._column_x = g[0]
        cur = self._line_of_index(self._visual_caret())
        target = max(0, min(len(lay.lines) - 1, cur + lines))
        if target == cur:
            # 已在顶/底行：跳到文首/文末
            self.set_caret(0 if lines < 0 else len(self.text), extend,
                           remember_column=True)
            return
        ln = lay.lines[target]
        idx = self._index_at_x(ln, self._column_x)
        self.set_caret(idx, extend, remember_column=True)

    def _line_of_index(self, index: int) -> int:
        """文本下标 → 视觉行号（按光标几何最近的基线）。"""
        g = self.caret_geom(index)
        lay = self._layout()
        if g is None or lay is None:
            return 0
        gy = g[1]
        best_i = 0
        best_d = None
        for i, ln in enumerate(lay.lines):
            d = abs(ln.baseline_y - gy)
            if best_d is None or d < best_d:
                best_d = d
                best_i = i
        return best_i

    def _index_at_x(self, ln, x: float) -> int:
        if not ln.chars:
            return max(0, min(len(self.text), ln.text_start))
        if x <= ln.chars[0].origin[0]:
            return ln.chars[0].text_index
        for c in ln.chars:
            if x < c.origin[0] + c.advance / 2.0:
                return c.text_index
        return ln.chars[-1].text_index + 1

    def select_all(self) -> None:
        self.anchor = 0
        self.caret = len(self.text)
        self._emit_selection()
        self.update_caret_region()

    def clear_selection(self) -> None:
        self.anchor = self.caret
        self._emit_selection()

    # ------------------------------------------------------------ 预编辑串
    def set_preedit(self, s: str) -> None:
        if s == self.preedit:
            return
        if s and not self.preedit:
            self.preedit_at = self.caret
        self.preedit = s
        self.update_caret_region()

    # ------------------------------------------------------------ 键盘
    def handle_key(self, event) -> bool:
        """处理按键；返回是否消费。由 CanvasView.keyPressEvent 调用。"""
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & (Qt.ControlModifier | Qt.MetaModifier))
        shift = bool(mods & Qt.ShiftModifier)

        if ctrl:
            if key == Qt.Key_Z:
                self._finish_then(lambda: self.main.controller.undo())
                return True
            if key in (Qt.Key_Y,):
                self._finish_then(lambda: self.main.controller.redo())
                return True
            if key == Qt.Key_A:
                self.select_all()
                return True
            if key == Qt.Key_C:
                self._copy()
                return True
            if key == Qt.Key_X:
                self._copy()
                self.delete_backward()
                return True
            if key == Qt.Key_V:
                cb = QApplication.clipboard()
                if cb is not None:
                    self.insert_text(cb.text())
                return True
            return False

        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.insert_text("\n")
            return True
        if key == Qt.Key_Backspace:
            if mods & Qt.AltModifier:
                return False
            self.delete_backward()
            return True
        if key == Qt.Key_Delete:
            self.delete_forward()
            return True
        if key == Qt.Key_Left:
            if mods & Qt.AltModifier:
                return False
            if mods & Qt.ControlModifier:
                self.move_word(-1, shift)
            else:
                self.move_horizontal(-1, shift)
            return True
        if key == Qt.Key_Right:
            if mods & Qt.AltModifier:
                return False
            if mods & Qt.ControlModifier:
                self.move_word(1, shift)
            else:
                self.move_horizontal(1, shift)
            return True
        if key == Qt.Key_Up:
            self.move_vertical(-1, shift)
            return True
        if key == Qt.Key_Down:
            self.move_vertical(1, shift)
            return True
        if key == Qt.Key_Home:
            lay = self._layout()
            if mods & Qt.ControlModifier:
                self.set_caret(0, shift)
            elif lay is not None and lay.lines:
                ln = lay.lines[self._line_of_index(self._visual_caret())]
                self.set_caret(self._index_at_x(ln, -1e18), shift)
            return True
        if key == Qt.Key_End:
            lay = self._layout()
            if mods & Qt.ControlModifier:
                self.set_caret(len(self.text), shift)
            elif lay is not None and lay.lines:
                ln = lay.lines[self._line_of_index(self._visual_caret())]
                self.set_caret(self._index_at_x(ln, 1e18), shift)
            return True
        if key == Qt.Key_Escape:
            self.finish()
            return True
        if key == Qt.Key_Tab:
            self.insert_text("\t")
            return True

        txt = event.text()
        if txt and txt.isprintable() and not self.preedit:
            self.insert_text(txt)
            return True
        if self.preedit:
            return True        # 预编辑进行中：字符键交给输入法
        return False

    def _finish_then(self, action) -> None:
        self.finish()
        action()

    def _copy(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self.selected_chars())

    # ------------------------------------------------------------ 鼠标
    def hit_frame(self, scene_pos: QPointF, margin: float = _FRAME_MARGIN_MM
                  ) -> bool:
        """点是否落在文本框（外扩 margin）内。"""
        f = self.frame()
        if f is None:
            return False
        x0, y_top, x1, y_bottom = f
        lx, ly = self._local_from_scene(scene_pos)
        return (x0 - margin <= lx <= x1 + margin
                and y_bottom - margin <= ly <= y_top + margin)

    def on_press(self, scene_pos: QPointF, event) -> bool:
        """左键按下：框内则定位光标并开始框选；返回是否接管。"""
        if not self.hit_frame(scene_pos):
            if self.preedit:
                QApplication.inputMethod().reset()
                self.set_preedit("")
            return False
        idx = self.hit_test(scene_pos)
        self._drag = True
        if event.modifiers() & Qt.ShiftModifier:
            self.caret = idx
        else:
            self.caret = self.anchor = idx
            if self.preedit:
                QApplication.inputMethod().reset()
                self.set_preedit("")
        self._emit_selection()
        self._restart_blink()
        self.update_caret_region()
        return True

    def on_move(self, scene_pos: QPointF) -> None:
        if self._drag:
            self.set_caret(self.hit_test(scene_pos), extend=True)

    def on_release(self) -> None:
        self._drag = False

    def is_dragging(self) -> bool:
        return self._drag

    # ------------------------------------------------------------ 重绘
    def _toggle_blink(self) -> None:
        self.caret_visible = not self.caret_visible
        self.update_caret_region()

    def _restart_blink(self) -> None:
        self.caret_visible = True
        self._blink.start()

    def caret_view_rect(self) -> Optional[QRectF]:
        g = self.caret_geom()
        if g is None:
            return None
        x, baseline, asc, desc = g
        px, py = self.obj.transform.apply((x, baseline))
        p1 = self.canvas.mapFromScene(QPointF(px, py - desc))
        p2 = self.canvas.mapFromScene(QPointF(px, py + asc))
        x0, x1 = sorted((float(p1.x()), float(p2.x())))
        y0, y1 = sorted((float(p1.y()), float(p2.y())))
        return QRectF(x0 - 2.0, y0 - 2.0, x1 - x0 + 4.0, y1 - y0 + 4.0)

    def update_caret_region(self) -> None:
        """只重画光标附近区域（闪烁/移动都不惊动整幅画面）。"""
        vp = self.canvas.viewport()
        new = self.caret_view_rect()
        if self._last_view_rect is not None:
            vp.update(_to_qrect(self._last_view_rect.adjusted(-30, -30, 30, 30)))
        if new is not None:
            vp.update(_to_qrect(new.adjusted(-30, -30, 30, 30)))
        self._last_view_rect = new

    def _repaint_last(self) -> None:
        if self._last_view_rect is not None:
            self.canvas.viewport().update(
                _to_qrect(self._last_view_rect.adjusted(-30, -30, 30, 30)))

    # ------------------------------------------------------------ 绘制
    def paint(self, painter: QPainter) -> None:
        """在 scene（页面 mm）坐标系绘制选区/光标/文本框/预编辑串。"""
        lay = self._layout()
        if lay is None:
            return
        t = qtransform_from(self.obj.transform)
        painter.save()
        painter.setTransform(t, True)      # 之后按对象本地 mm 绘制

        # 字符选区
        a, b = self._sel_range()
        if b > a:
            painter.setPen(Qt.NoPen)
            painter.setBrush(COLOR_SELECTION)
            for ln in lay.lines:
                for c in ln.chars:
                    if a <= c.text_index < b:
                        asc, desc = self._char_band(c, lay)
                        painter.drawRect(QRectF(c.origin[0],
                                                ln.baseline_y - desc,
                                                max(c.advance, 0.2),
                                                asc + desc))

        # 文本框（虚线）
        f = self.frame()
        if f is not None:
            pen = QPen(COLOR_FRAME)
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            x0, y_top, x1, y_bottom = f
            painter.drawRect(QRectF(x0, y_bottom, x1 - x0, y_top - y_bottom))

        # 光标（本地竖线，cosmetic 保持屏幕宽）
        if self.caret_visible:
            g = self.caret_geom()
            if g is not None:
                x, baseline, asc, desc = g
                pen = QPen(COLOR_CURSOR)
                pen.setCosmetic(True)
                pen.setWidthF(1.6)
                painter.setPen(pen)
                painter.drawLine(QPointF(x, baseline - desc),
                                 QPointF(x, baseline + asc))
        painter.restore()

        # 预编辑串（组字中的拼音）：切到设备坐标绘制（QPainter 文字恒按
        # 「+y 向下」排版，在场景坐标系里会被视图的 Y 翻转画倒）
        if self.preedit:
            g = self.caret_geom()
            if g is not None:
                x, baseline, _asc, _desc = g
                px, py = self.obj.transform.apply((x, baseline))
                vp = self.canvas.mapFromScene(QPointF(px, py))
                size = lay.style.size if lay.style else 10.0
                font = QFont()
                font.setPixelSize(max(6, int(round(size * self.canvas.current_zoom()))))
                painter.save()
                painter.resetTransform()
                painter.setFont(font)
                fm = painter.fontMetrics()
                # 文字坐在基线上（字形向上），整体略抬离笔画避免重叠
                y_base = vp.y() - int(size * self.canvas.current_zoom() * 0.15)
                painter.setPen(QPen(COLOR_PREEDIT))
                painter.drawText(QPointF(vp.x(), y_base), self.preedit)
                pw = fm.horizontalAdvance(self.preedit)
                pen = QPen(COLOR_PREEDIT)
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.drawLine(QPointF(vp.x(), y_base + 2),
                                 QPointF(vp.x() + pw, y_base + 2))
                # 预编辑期间光标停在串后
                pen2 = QPen(COLOR_CURSOR)
                pen2.setWidthF(1.6)
                painter.setPen(pen2)
                painter.drawLine(QPointF(vp.x() + pw + 2.0, y_base - fm.ascent()),
                                 QPointF(vp.x() + pw + 2.0, y_base + 2))
                painter.restore()


def _to_qrect(r: QRectF) -> QRect:
    """QRectF → QRect（QWidget.update 只收整数矩形）。"""
    return QRect(int(r.left()) - 1, int(r.top()) - 1,
                 int(r.width()) + 3, int(r.height()) + 3)


def _word_start(text: str, i: int) -> int:
    """向左跳一个「词」（连续字母数字或连续空白）。"""
    if i <= 0:
        return 0
    i -= 1
    al = text[i].isalnum()
    while i > 0 and text[i - 1].isalnum() == al and (al or text[i - 1].isspace()):
        i -= 1
    return i


def _word_end(text: str, i: int) -> int:
    n = len(text)
    if i >= n:
        return n
    al = text[i].isalnum()
    while i < n and text[i].isalnum() == al and (al or text[i].isspace()):
        i += 1
    return i
