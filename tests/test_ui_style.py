"""ui/style.py ``unify_inputs``：板块内输入控件等高 + 文本竖直居中。

用户反馈驱动（「数值文本框和下拉框被改得很烂」）：加了主题库
（qdarktheme）后，同一个表单里 QSpinBox 塌到 20/24px、QComboBox 仍
28px、QLineEdit 21px，行高参差。根因是**用 setFixedHeight 设高 +
只用 padding 的 QSS**——控件改由 QStyleSheetStyle 绘制后
setFixedHeight 压不过主题库自带样式表的盒模型，各控件按自身度量算出
不同高度（此前的旧测试跑在无主题的裸 Fusion 样式下，恰好没暴露）。

正确做法：高度写进 QSS 的 **min-height 与 max-height**（两者一起才
把内容框钉死），只给 min-height 会被内容撑高。本模块据此实现。测试
在**生产所用的 qdarktheme** 下校验，避免再次「测试通过、真机翻车」。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QWidget,
)

from writerstudio.ui.main_window import MainWindow  # noqa: E402
from writerstudio.ui.style import FIELD_HEIGHT, unify_inputs  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    # 与生产一致：应用主题库（其自带样式表正是旧实现翻车的上下文）。
    from writerstudio.ui import theme
    theme.apply_theme(app, theme.DEFAULT_THEME)
    yield app


def _is_field(w):
    """排除 QAbstractSpinBox 内部的 qt_spinbox_lineedit（随外层定高）。"""
    return w.objectName() != "qt_spinbox_lineedit"


def _inputs(root):
    from PySide6.QtWidgets import QAbstractSpinBox
    for cls in (QAbstractSpinBox, QComboBox, QLineEdit):
        yield from (w for w in root.findChildren(cls) if _is_field(w))


def test_panel_inputs_uniform_height(qapp):
    """各大板块内所有输入框/下拉框高度唯一，且等于全局 FIELD_HEIGHT。"""
    win = MainWindow()
    try:
        cases = (
            ("机器", win.machine_panel),
            ("扰动", win.global_perturb),
            ("文字属性", win.text_props),
            ("字体库", win.font_panel),
            ("参考层", win.reference_panel),
        )
        for name, panel in cases:
            heights = {w.height() for w in _inputs(panel)}
            assert heights == {FIELD_HEIGHT}, \
                f"{name}板块输入控件高度不统一: {heights}"
    finally:
        win.close()


def test_dialog_field_types_have_identical_height(qapp):
    """回归：同一表单里 spin / combo / line 必须等高（旧实现是 24/28/21）。"""
    from PySide6.QtWidgets import QFormLayout
    w = QWidget()
    sp = QDoubleSpinBox()
    cb = QComboBox()
    cb.addItem("x")
    le = QLineEdit()
    f = QFormLayout(w)
    for lbl, x in (("字号", sp), ("对齐", cb), ("备注", le)):
        f.addRow(lbl, x)
    unify_inputs(w)
    w.resize(320, 180)
    w.show()
    qapp.processEvents()
    try:
        assert {sp.height(), cb.height(), le.height()} == {FIELD_HEIGHT}, \
            f"三类控件高度不一致: {sp.height()}/{cb.height()}/{le.height()}"
    finally:
        w.close()


def test_field_qss_pins_min_and_max_height(qapp):
    """高度靠 QSS 的 min+max-height 钉死；只给 min-height 会被内容撑高。"""
    w = QWidget()
    unify_inputs(w)
    qss = w.styleSheet()
    assert "min-height" in qss and "max-height" in qss
    assert "padding" in qss
    assert ":focus" in qss                    # 透明边框会盖掉焦点环，须自补
    assert "palette(highlight)" in qss        # 焦点色取自调色板，不写死
    assert "color" not in qss                 # 配色一律留给调色板


def test_focused_field_shows_focus_ring(qapp):
    """回归：透明边框不得让焦点不可见（无障碍）。

    有焦点时上边框应变成调色板高亮色，而不是与无焦点时一样。
    """
    from PySide6.QtGui import QImage
    w = QWidget()
    le = QLineEdit("x")
    from PySide6.QtWidgets import QFormLayout
    QFormLayout(w).addRow("a", le)
    unify_inputs(w)
    w.resize(240, 80)
    w.show()
    qapp.processEvents()

    def top_edge():
        img = le.grab().toImage().convertToFormat(QImage.Format_RGB32)
        return img.pixelColor(img.width() // 2, 0).name()

    try:
        le.clearFocus()          # 先确保无焦点（否则显示时默认已聚焦）
        qapp.processEvents()
        le.repaint()
        qapp.processEvents()
        before = top_edge()
        le.setFocus()
        qapp.processEvents()
        le.repaint()
        qapp.processEvents()
        after = top_edge()
        assert before != after, f"聚焦前后上边框色相同（{after}），焦点不可见"
    finally:
        w.close()


def test_unify_inputs_is_idempotent_and_keeps_foreign_rules(qapp):
    """重复调用幂等（不叠加高度块），且保留其它模块写下的颜色规则。"""
    panel = QWidget()
    QComboBox(panel)
    panel.setStyleSheet("QComboBox { color: #123456; }")
    unify_inputs(panel)
    unify_inputs(panel)
    qss = panel.styleSheet()
    assert "color: #123456" in qss
    assert qss.count("min-height") == 1, f"高度块被叠加: {qss!r}"


def test_nested_panel_keeps_its_spec(qapp):
    """内层子面板（自身统一过）不被外层对话框改写高度。"""
    from PySide6.QtWidgets import QVBoxLayout
    from writerstudio.ui.perturb_panel import PerturbPanel
    outer = QWidget()
    lay = QVBoxLayout(outer)
    inner = PerturbPanel()
    lay.addWidget(inner)
    unify_inputs(outer)                  # 外层再统一一次（模拟对话框）
    unify_inputs(outer)
    got = {w.height() for w in _inputs(inner)}
    assert got == {FIELD_HEIGHT}, f"内层面板被外层改写: {got}"
