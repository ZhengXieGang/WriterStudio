"""P30 画布/面板修复回归测试：

    * 滚动后背景缓存失效（中键平移网格不再错位）
    * 笔画编辑浮条锚定在视口内（滚动 blit 不再把它挪出画布）
    * 笔画编辑拖动经真实画布事件链路可正常提交
    * 机器面板输入框/下拉框统一宽高
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QPushButton,
    QSpinBox,
)

from writerstudio.core.document import Document, make_static_object  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.settings import Settings, _MemoryBackend  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _win(qapp, size=(1200, 800)):
    win = MainWindow(settings=Settings(_MemoryBackend()))
    win.confirm_on_close = False
    win.resize(*size)
    win.show()
    for _ in range(6):
        qapp.processEvents()
    obj = make_static_object([make_rect(60, 60, 40, 30)])
    doc = Document()
    doc.add(obj)
    win.controller.set_document(doc)
    win.canvas.zoom_100()
    for _ in range(4):
        qapp.processEvents()
    return win, obj


def _diff(a, b, step=7) -> int:
    n = 0
    for y in range(0, a.height(), step):
        for x in range(0, a.width(), step):
            ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
            if (ca.red(), ca.green(), ca.blue()) != \
                    (cb.red(), cb.green(), cb.blue()):
                n += 1
    return n


# ==================================================== 滚动后背景不错位
def test_scroll_rerenders_background(qapp):
    """中键平移（滚动条滚动）后画面必须与全量重渲染一致。

    回归：CacheBackground 的缓存在滚动时不重建，网格/纸张"冻"在旧位置，
    与场景物件错位（用户可见为拖动时网格游动、笔画拖不动）。
    """
    win, _ = _win(qapp)
    cv = win.canvas
    h, v = cv.horizontalScrollBar(), cv.verticalScrollBar()
    if h.maximum() <= h.minimum() or v.maximum() <= v.minimum():
        pytest.skip("无滚动行程")
    h.setValue(h.value() - 150)
    v.setValue(v.value() + 90)
    for _ in range(6):
        qapp.processEvents()
    img2 = cv.viewport().grab().toImage()
    cv.resetCachedContent()
    for _ in range(6):
        qapp.processEvents()
    img3 = cv.viewport().grab().toImage()
    # scrollContentsBy 里已失效缓存：滚动后的画面应与重新渲染完全一致
    assert _diff(img2, img3) == 0
    win.close()


# ==================================================== 浮条锚定
def test_stroke_bar_stays_anchored(qapp):
    """笔画编辑浮条在平移/缩放/窗口缩放后仍在视口左上角。

    回归：QAbstractScrollArea 滚动 blit 会把 viewport 子控件一起挪走，
    浮条被"挤出画布外"（平移几次后飘到 (103,-22) 一类位置）。
    """
    win, obj = _win(qapp)
    win.canvas.select_object(obj)
    win._toggle_stroke_edit(True)
    for _ in range(3):
        qapp.processEvents()
    cv = win.canvas
    h, v = cv.horizontalScrollBar(), cv.verticalScrollBar()
    for _ in range(3):
        h.setValue(max(h.minimum(), h.value() - 100))
        v.setValue(min(v.maximum(), v.value() + 60))
        cv.zoom_in()
        qapp.processEvents()
        assert win._stroke_bar.pos() == QPoint(10, 10)
    win.resize(900, 600)
    for _ in range(4):
        qapp.processEvents()
    assert win._stroke_bar.pos() == QPoint(10, 10)
    # 仍在视口可见范围内
    vp = cv.viewport().rect()
    assert vp.contains(win._stroke_bar.geometry().topLeft())
    win.close()


# ==================================================== 笔画拖动链路
def test_stroke_drag_via_canvas_events(qapp):
    """笔画编辑：真实画布事件链路（press→move→release）能正常拖动提交。"""
    win, obj = _win(qapp)
    win.canvas.select_object(obj)
    win._toggle_stroke_edit(True)
    for _ in range(3):
        qapp.processEvents()
    se = win.stroke_editor
    cv = win.canvas
    p0 = se.world[0].points[0]
    vp0 = cv.mapFromScene(QPointF(p0[0], p0[1]))

    def me(kind, vp):
        return QMouseEvent(kind, QPointF(vp), QPointF(vp),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    before = [(x, y) for x, y in obj.local_strokes[0].points]
    cv.mousePressEvent(me(QEvent.MouseButtonPress, vp0))
    assert se.is_dragging()
    vp1 = vp0 + QPoint(60, 0)
    cv.mouseMoveEvent(me(QEvent.MouseMove, vp1))
    assert se.preview is not None                 # 拖动预览跟随
    cv.mouseReleaseEvent(me(QEvent.MouseButtonRelease, vp1))
    for _ in range(4):
        qapp.processEvents()
    after = [(x, y) for x, y in obj.local_strokes[0].points]
    assert after != before                        # 已写回对象
    dx = after[0][0] - before[0][0]
    assert dx == pytest.approx(60 / cv.current_zoom(), abs=0.5)
    win.close()


# ==================================================== 机器面板控件统一
def test_machine_panel_uniform_field_sizes(qapp):
    """输入框/下拉框高度统一 30px、最小宽 88px；按钮最小高 30px。"""
    win, _ = _win(qapp)
    mp = win.machine_panel
    for cls in (QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox):
        ws = mp.findChildren(cls)
        assert ws, cls.__name__
        for w in ws:
            # QAbstractSpinBox 内部自带的 qt_spinbox_lineedit 高度随外层
            # spinbox 走，不单独参与统一（由 ui/style.py 明确排除）
            if w.objectName() == "qt_spinbox_lineedit":
                continue
            assert w.height() == 30, (cls.__name__, w.height())
            assert w.minimumWidth() >= 88
            assert w.sizePolicy().horizontalPolicy() == \
                __import__("PySide6.QtWidgets",
                           fromlist=["QSizePolicy"]).QSizePolicy.Expanding
    for b in mp.findChildren(QPushButton):
        assert b.minimumHeight() >= 30
    win.close()
