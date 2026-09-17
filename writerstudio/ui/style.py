"""板块内输入控件的尺寸统一。

把 ui-ux-pro-max skill 的 ``consistency``（同一板块同一风格）、
``spacing-scale``（4px 栅格）与 ``touch-friendly-input``（输入目标
不要太小）准则落到桌面表单：同一板块内所有输入框/下拉框等高、同列
等宽对齐。只调几何、不写死颜色——控件配色继续取自调色板并随主题
热切换（``system-controls`` 与语义色令牌原则）。

各板块在构造末尾调用一次::

    unify_inputs(self)                  # 常规面板
    unify_inputs(self, min_width=88, button_height=30)   # 机器面板等

高度统一取模块级常量 :data:`FIELD_HEIGHT`（30px），各板块不设各自
高度——见下方「为什么高度必须写进 QSS」。

**为什么高度必须写进 QSS，而不是 setFixedHeight**：主题库
（qdarktheme）自带一份样式表，其中已给输入类控件设了高度规则。一旦
再设置任意控件样式表，控件改由 ``QStyleSheetStyle`` 绘制，而
``setFixedHeight`` 只改外壳、压不过样式表的盒模型——实测同一个对话框
里 QSpinBox 塌到 20px、QComboBox 仍是 28px，行高参差（用户反馈
「数值文本框和下拉框改得很烂」）。正确做法是用 QSS 同时给
``min-height`` **与** ``max-height``：两者一起才把内容框钉死，
只给 min-height 会被内容撑高（历史教训）。
"""

from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QWidget,
)

# 全局统一的输入框/下拉框高度（逻辑像素）。取 30：机器控制面板与手写
# 扰动面板（用户此前指定按机器面板对齐）都已用 30，索性全项目统一一个
# 值——对话框里嵌入手写扰动的子面板时，内外层高度规则才不会互相打架。
FIELD_HEIGHT = 30

#: 样式表里标记本模块注入规则块的注释（**置于块首**）。重复调用时据它
#: 剥掉上一次注入的块再追加，避免多层嵌套/重复调用把规则叠加成一堆。
_MARK = "/* ws-field-height */"

#: 已应用 unify_inputs 的控件对象名集合（动态属性），用于让嵌套子面板
#: 保留自己的高度、不被外层再次改写。
_OWNER_PROP = "_ws_field_owner"


def spin(lo: float, hi: float, val: float, suffix: str = "",
         step: float = 0.5, decimals: int = 2) -> QDoubleSpinBox:
    """统一构造 QDoubleSpinBox（原先五个板块各有一份相同实现）。"""
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setValue(val)
    s.setSingleStep(step)
    s.setSuffix(suffix)
    s.setDecimals(decimals)
    return s


def _field_qss(height: int, pad: int) -> str:
    """把总高 ``height`` 钉死的输入控件规则（含焦点环）。

    QSS 的 ``min/max-height`` 是**内容**高度（不含边框），故内容高 = 总高
    − 2。另加 ``border: 1px solid transparent``：一是让 spinbox 在原生
    Fusion 下也多算这 1px 边框、与 combo/line 同为 ``height``（否则
    QSpinBox 会高出 4px）；二是透明边框不改变外观，保留主题库自身的
    底色观感。透明边框会一并盖掉主题的焦点高亮，故必须自己补一条
    ``:focus`` 规则——用 ``palette(highlight)`` 取调色板高亮色，不写死
    颜色，深浅主题都清晰可见（无障碍：能看出焦点在哪）。
    """
    content = max(1, height - 2)
    classes = ("QSpinBox", "QDoubleSpinBox", "QComboBox", "QLineEdit")
    fields = ", ".join(classes)
    focus = ", ".join(f"{c}:focus" for c in classes)
    return (f"{_MARK}\n"
            f"{fields} {{"
            f" padding: 0 {pad}px;"
            f" border: 1px solid transparent; border-radius: 4px;"
            f" min-height: {content}px; max-height: {content}px; }}"
            f"{focus} {{"
            f" border: 1px solid palette(highlight); }}")


def _is_input(w: QWidget) -> bool:
    """是否是纳入统一的「外层」输入控件。

    QAbstractSpinBox 内部自带一个 ``qt_spinbox_lineedit``：它会被
    ``findChildren(QLineEdit)`` 命中，但高度由外层 spinbox 决定，
    不能单独设尺寸，否则箭头/文本错位。
    """
    return w.objectName() != "qt_spinbox_lineedit"


def _has_unified_owner(w: QWidget) -> bool:
    """``w`` 是否处在某个已统一过的子面板内（用于跳过二次改写）。"""
    p = w.parentWidget()
    while p is not None:
        if p.property(_OWNER_PROP):
            return True
        p = p.parentWidget()
    return False


def unify_inputs(root: QWidget, height: int = FIELD_HEIGHT,
                 min_width: int = 0, button_height: int = 0) -> None:
    """递归统一 root 下输入控件的几何（含子类，如 QFontComboBox）。

    * 等高 ``height``：经 QSS 的 min/max-height 钉死（见模块文档）；
    * 等宽：横向 Expanding，QFormLayout/QGridLayout 同列控件自动
      对齐到列宽；个别明确设了固定宽度的控件不受影响；
    * 嵌套子面板（自身调用过 :func:`unify_inputs`）内的控件跳过，
      保留其板块规格——否则外层对话框会把内层大按钮面板压成普通高度；
    * ``button_height`` > 0 时按钮最小高度同步，行内混排不参差。
    """
    for cls in (QAbstractSpinBox, QComboBox, QLineEdit):
        for w in root.findChildren(cls):
            if not _is_input(w) or _has_unified_owner(w):
                continue
            # 清掉历史遗留的 setFixedHeight 外壳约束（改由 QSS 统一接管）
            w.setMinimumHeight(0)
            w.setMaximumHeight(16777215)
            if min_width:
                w.setMinimumWidth(min_width)
            w.setSizePolicy(QSizePolicy.Policy.Expanding,
                            QSizePolicy.Policy.Fixed)

    qss_block = _field_qss(height, max(6, height // 5))
    old = root.styleSheet()
    # 标记在块首：剥掉上一次注入的块（连同标记行），再追加——重复调用
    # 幂等，且不碰用户/其它模块写下的样式（它们位于标记之前）。
    head = old.split(_MARK)[0].rstrip("\n") if _MARK in old else old
    root.setStyleSheet((head + "\n" if head else "") + qss_block)
    root.setProperty(_OWNER_PROP, True)

    if button_height:
        for b in root.findChildren(QPushButton):
            b.setMinimumHeight(button_height)
