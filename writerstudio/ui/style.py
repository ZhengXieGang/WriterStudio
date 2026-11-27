"""板块内输入控件的尺寸统一。

把 ui-ux-pro-max skill 的 ``consistency``（同一板块同一风格）、
``spacing-scale``（4px 栅格）与 ``touch-friendly-input``（输入目标
不要太小）准则落到桌面表单：同一板块内所有输入框/下拉框等高、同列
等宽对齐。只调几何、不写死颜色——控件配色继续取自主题样式并随主题
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

from weakref import WeakKeyDictionary

from PySide6.QtCore import QEvent, QObject, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QWidget,
)

from ..i18n import is_english
from . import theme as _theme

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

#: 已安装聚焦滚轮守卫的动态属性标记（保证幂等）
_WHEEL_GUARD_PROP = "_ws_wheel_guard"


class _FocusWheelGuard(QObject):
    """未获得焦点时拦截滚轮事件，防止滚动面板时误调数值/下拉/滑杆。

    Qt 的 SpinBox / ComboBox / Slider 默认接受未聚焦时的滚轮事件（WheelFocus），
    在带滚动条的侧边栏与对话框中滑动滚轮极易误改参数。拦截后调用
    ``event.ignore()`` 并返回 True：控件本身不消费该事件，Qt 会继续向上冒泡给
    外层 QScrollArea 完成正常滚动；用户显式点击选中控件获得焦点后，滚轮照常
    调整数值。
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and isinstance(obj, QWidget):
            if not _widget_has_focus(obj):
                event.ignore()
                return True
        return False


def _widget_has_focus(w: QWidget) -> bool:
    """判断输入控件（含其内部组合的 QLineEdit）当前是否处于选中聚焦态。"""
    if w.hasFocus():
        return True
    if isinstance(w, QAbstractSpinBox):
        le = w.lineEdit()
        if le is not None and le.hasFocus():
            return True
    if isinstance(w, QComboBox):
        le = w.lineEdit()
        if le is not None and le.hasFocus():
            return True
    return False


#: 进程级无状态事件过滤器单例（不持有任何控件引用，避免生命周期问题）
_WHEEL_GUARD: _FocusWheelGuard | None = None


def _get_wheel_guard() -> _FocusWheelGuard:
    global _WHEEL_GUARD
    if _WHEEL_GUARD is None:
        _WHEEL_GUARD = _FocusWheelGuard()
    return _WHEEL_GUARD


def guard_wheel_until_focused(root: QWidget) -> None:
    """递归为 ``root`` 下的旋钮/下拉/滑杆启用「仅聚焦时响应滚轮」保护。"""
    guard = _get_wheel_guard()
    targets: list[QWidget] = []
    if isinstance(root, (QAbstractSpinBox, QComboBox, QSlider)):
        targets.append(root)
    for cls in (QAbstractSpinBox, QComboBox, QSlider):
        targets.extend(root.findChildren(cls))
    for w in targets:
        if w.property(_WHEEL_GUARD_PROP):
            continue
        w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.installEventFilter(guard)
        w.setProperty(_WHEEL_GUARD_PROP, True)
        # 内部组合的 QLineEdit 同样设 StrongFocus 防止滚轮抢焦点
        if isinstance(w, (QAbstractSpinBox, QComboBox)):
            le = w.lineEdit()
            if le is not None and not le.property(_WHEEL_GUARD_PROP):
                le.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                le.installEventFilter(guard)
                le.setProperty(_WHEEL_GUARD_PROP, True)


class MarqueeLabel(QLabel):
    """单行标签：超出宽度时省略显示，鼠标悬停后平滑横向滚动展示全文。

    专为窄停靠区状态卡片设计：
    * 永不换行，保持卡片高度恒定不抖动；
    * 最小宽度极小且水平策略为 Ignored，永不撑宽父面板或触发横向滚动条；
    * 鼠标移入且文本超出可见宽时自动启动平滑跑马灯滚动，移出立即复位；
    * 完全继承 ``QLabel`` 接口（``text()`` / ``setText()`` / ``setToolTip()``）。
    """

    #: 跑马灯每帧刷新间隔（毫秒）与单帧步进像素（约 48px/s）
    _TICK_MS = 30
    _STEP_PX = 1.4
    #: 悬停后开始滚动前的起停顿帧数（约 0.36 秒）
    _PAUSE_TICKS = 12
    #: 循环衔接时两段文本之间的空白间距（像素）
    _GAP_PX = 36

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(False)
        # Expanding 保证在 QFormLayout(ExpandingFieldsGrow) 里撑满整行剩余宽度，
        # 同时极小的 minimumSizeHint 避免撑宽窄停靠栏。
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._hover = False
        self._offset = 0.0
        self._pause = 0
        self._base_tooltip = ""
        self._timer = QTimer(self)
        self._timer.setInterval(self._TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._sync_tooltip()

    def attach_buddy(self, buddy: QWidget) -> None:
        """把同行左侧标题标签关联到本跑马灯：鼠标移到该项整行均可触发滚动。"""
        buddy.installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        t = event.type()
        if t == QEvent.Type.Enter:
            self._set_hover(True)
        elif t == QEvent.Type.Leave:
            self._set_hover(False)
        return super().eventFilter(obj, event)

    def _set_hover(self, on: bool) -> None:
        if on == self._hover:
            return
        self._hover = on
        if on:
            self._maybe_start_scroll()
        else:
            self._reset_scroll()
            self.update()

    def _display_text(self) -> str:
        """把含换行的原始文本压成单行展示（保留 text() 原值不变）。"""
        raw = self.text()
        if "\n" not in raw:
            return raw
        return " · ".join(part.strip() for part in raw.splitlines() if part.strip())

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(min(140, max(48, fm.horizontalAdvance(self._display_text()) + 4)), fm.height() + 4)

    def minimumSizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(24, fm.height() + 4)

    def setText(self, text: str) -> None:
        if text == self.text():
            return
        super().setText(text)
        self._reset_scroll()
        self._sync_tooltip()
        if self._hover:
            self._maybe_start_scroll()
        self.update()

    def setToolTip(self, tip: str) -> None:
        # 记住业务侧设置的静态说明；当文本超长时自动把完整值拼到提示首行
        self._base_tooltip = tip
        self._sync_tooltip()

    def _sync_tooltip(self) -> None:
        txt = self.text()
        if self._base_tooltip:
            merged = f"{txt}\n\n{self._base_tooltip}" if txt and txt != "—" else self._base_tooltip
            super().setToolTip(merged)
        else:
            super().setToolTip(txt)

    def _text_width(self) -> int:
        return QFontMetrics(self.font()).horizontalAdvance(self._display_text())

    def _avail_width(self) -> int:
        return max(0, self.contentsRect().width())

    def _needs_scroll(self) -> bool:
        return self._text_width() > self._avail_width() and self._avail_width() > 4

    def _reset_scroll(self) -> None:
        self._timer.stop()
        self._offset = 0.0
        self._pause = 0

    def _maybe_start_scroll(self) -> None:
        if self._hover and self._needs_scroll():
            if not self._timer.isActive():
                self._offset = 0.0
                self._pause = self._PAUSE_TICKS
                self._timer.start()
        else:
            self._reset_scroll()

    def _on_tick(self) -> None:
        if not self._hover or not self._needs_scroll():
            self._reset_scroll()
            self.update()
            return
        if self._pause > 0:
            self._pause -= 1
            return
        span = self._text_width() + self._GAP_PX
        self._offset += self._STEP_PX
        if self._offset >= span:
            self._offset = 0.0
            self._pause = self._PAUSE_TICKS
        self.update()

    def enterEvent(self, event) -> None:
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hover(False)
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self._needs_scroll():
            self._reset_scroll()

    def hideEvent(self, event) -> None:
        self._hover = False
        self._reset_scroll()
        super().hideEvent(event)

    def _text_color(self) -> QColor:
        role = str(self.property("wsRole") or "")
        if _theme.theme_active():
            return QColor(_theme.semantic_color(role if role else "text"))
        if role:
            return QColor(_theme.semantic_color(role))
        return self.palette().color(self.foregroundRole())

    def paintEvent(self, event) -> None:
        rect = self.contentsRect()
        txt = self._display_text()
        if not txt or rect.width() <= 0:
            return
        painter = QPainter(self)
        painter.setClipRect(rect)
        painter.setPen(self._text_color())
        fm = painter.fontMetrics()
        y = rect.y() + (rect.height() + fm.ascent() - fm.descent()) // 2
        if self._hover and self._needs_scroll():
            tw = fm.horizontalAdvance(txt)
            x1 = int(round(rect.x() - self._offset))
            painter.drawText(x1, y, txt)
            x2 = x1 + tw + self._GAP_PX
            if x2 < rect.right():
                painter.drawText(x2, y, txt)
        else:
            shown = fm.elidedText(txt, Qt.TextElideMode.ElideRight, rect.width())
            painter.drawText(QRect(rect), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, shown)


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


#: 已应用 unify_inputs 的容器 → (height, pad)，切主题时自动热刷边框色
_UNIFIED_ROOTS: WeakKeyDictionary[QWidget, tuple[int, int]] = WeakKeyDictionary()


def _field_qss(height: int, pad: int) -> str:
    """把总高 ``height`` 钉死的输入控件规则（带语义边框与焦点环）。

    QSS 的 ``min/max-height`` 是**内容**高度（不含边框），故内容高 = 总高
    − 2。边框色取当前主题的 ``field_border`` 令牌（而非透明）：深/浅主题
    与原生 Fusion 降级环境下总高恒为 ``height`` 且边界清晰；切换主题时由
    :func:`refresh_field_styles` 自动重刷所有现存容器。
    """
    content = max(1, height - 2)
    border = _theme.semantic_color("field_border")
    classes = ("QSpinBox", "QDoubleSpinBox", "QComboBox", "QLineEdit")
    fields = ", ".join(f'{c}[wsField="true"]' for c in classes)
    focus = ", ".join(f'{c}[wsField="true"]:focus' for c in classes)
    return (
        f"{_MARK}\n"
        f"{fields} {{"
        f" padding: 0 {pad}px;"
        f" border: 1px solid {border}; border-radius: 4px;"
        f" min-height: {content}px; max-height: {content}px; }}"
        f"{focus} {{"
        f" border: 1px solid palette(highlight); }}"
    )


def _apply_field_qss(root: QWidget, height: int, pad: int) -> None:
    qss_block = _field_qss(height, pad)
    old = root.styleSheet()
    # 标记在块首：剥掉上一次注入的块（连同标记行），再追加——重复调用
    # 幂等，且不碰用户/其它模块写下的样式（它们位于标记之前）。
    head = old.split(_MARK)[0].rstrip("\n") if _MARK in old else old
    root.setStyleSheet((head + "\n" if head else "") + qss_block)


def refresh_field_styles() -> None:
    """主题热切换时，用新主题的语义边框色重刷所有现存已统一面板。"""
    for root, (height, pad) in list(_UNIFIED_ROOTS.items()):
        try:
            _apply_field_qss(root, height, pad)
        except RuntimeError:
            pass


def _is_input(w: QWidget) -> bool:
    """是否是纳入统一的「外层」输入控件。

    排除两类内部组合子控件：
    * ``QAbstractSpinBox`` 内置的 ``qt_spinbox_lineedit``；
    * 可编辑 ``QComboBox`` 内嵌的 ``QLineEdit``。
    两者高度与边框都应由外层控件决定，单独设几何会导致箭头/文本错位或高度差 2px。
    """
    if w.objectName() == "qt_spinbox_lineedit":
        return False
    if isinstance(w, QLineEdit) and isinstance(w.parentWidget(), QComboBox):
        return False
    return True


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
    """递归统一 root 下输入控件的几何与滚轮保护（含子类，如 QFontComboBox）。

    * 等高 ``height``：经 QSS 的 min/max-height 钉死，边框跟随全局主题；
    * 等宽：横向 Expanding，QFormLayout/QGridLayout 同列控件自动
      对齐到列宽；个别明确设了固定宽度的控件不受影响；
    * 滚轮保护：旋钮/下拉/滑杆默认改为仅在选中聚焦时响应滚轮，避免滚动页面误调；
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
            w.setProperty("wsField", True)

    pad = max(6, height // 5)
    _apply_field_qss(root, height, pad)
    _UNIFIED_ROOTS[root] = (height, pad)
    if button_height:
        for b in root.findChildren(QPushButton):
            if _has_unified_owner(b):
                continue
            b.setMinimumHeight(button_height)

    root.setProperty(_OWNER_PROP, True)
    # 所有旋钮/下拉/滑杆启用「仅聚焦时响应滚轮」
    guard_wheel_until_focused(root)


def set_role(widget: QWidget, role: str) -> None:
    """给标签设置语义色（muted/danger/success），跟随当前主题。

    不用内联 ``color:#666``：qdarktheme 调色板里的正文色仍是黑色（文字
    颜色由 QSS 负责），深色主题下硬编码的深灰说明文字对比度只有约 2:1。
    语义色规则由 :func:`writerstudio.ui.theme.apply_theme` 注入，切换主题
    时整体重建。
    """
    widget.setProperty("wsRole", role or "")
    target = widget.parentWidget() or widget
    style = target.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


_BUTTON_TEXTS = {
    QDialogButtonBox.StandardButton.Ok: ("确定", "OK"),
    QDialogButtonBox.StandardButton.Cancel: ("取消", "Cancel"),
    QDialogButtonBox.StandardButton.Close: ("关闭", "Close"),
    QDialogButtonBox.StandardButton.Yes: ("是", "Yes"),
    QDialogButtonBox.StandardButton.No: ("否", "No"),
    QDialogButtonBox.StandardButton.Save: ("保存", "Save"),
    QDialogButtonBox.StandardButton.Discard: ("不保存", "Discard"),
    QDialogButtonBox.StandardButton.Apply: ("应用", "Apply"),
    QDialogButtonBox.StandardButton.Reset: ("重置", "Reset"),
    QDialogButtonBox.StandardButton.Help: ("帮助", "Help"),
    QDialogButtonBox.StandardButton.Open: ("打开", "Open"),
}


def localize_dialog_buttons(root: QWidget) -> None:
    """把标准按钮改成与界面一致的语言（Qt 自带翻译未必随包分发）。"""
    english = is_english()
    for box in root.findChildren(QDialogButtonBox):
        for role, (zh, en) in _BUTTON_TEXTS.items():
            button = box.button(role)
            if button is not None:
                button.setText(en if english else zh)


def fit_dialog_to_screen(widget: QWidget, width: int, height: int,
                         margin_h: int = 48, margin_v: int = 72) -> None:
    """按屏幕可用区域收敛初始尺寸，避免窗口/对话框顶出屏幕。

    1366×768 这类常见笔记本屏上，1280×840 的主窗口或 800×760 的编辑
    对话框加标题栏后会超出屏幕；这里只缩小、不放大，也不会小于布局的
    真实最小需求。
    """
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:
        widget.resize(width, height)
        return
    avail = screen.availableGeometry()
    min_hint = widget.minimumSizeHint()
    widget.resize(
        max(min_hint.width(), min(width, avail.width() - margin_h)),
        max(min_hint.height(), min(height, avail.height() - margin_v)),
    )


def ask_yes_no(parent: QWidget, title: str, text: str,
               default_yes: bool = False, warning: bool = False) -> bool:
    """是/否询问框（按钮文字跟随界面语言，不吃 Qt 翻译包是否安装）。

    ``QMessageBox.question`` 的 Yes/No 在中文界面里会显示英文，除非系统
    恰好装了 Qt 中文翻译；显式建按钮才能保证两种语言下都一致。
    返回用户是否选了「是」（关窗/Esc 视为否）。
    """
    english = is_english()
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning if warning
               else QMessageBox.Icon.Question)
    yes = box.addButton("Yes" if english else "是", QMessageBox.ButtonRole.YesRole)
    no = box.addButton("No" if english else "否", QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(yes if default_yes else no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


def ask_save_discard_cancel(parent: QWidget, title: str, text: str) -> str:
    """未保存修改的「保存 / 不保存 / 取消」三选一；返回 save/discard/cancel。"""
    english = is_english()
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning)
    save = box.addButton("Save" if english else "保存",
                         QMessageBox.ButtonRole.AcceptRole)
    discard = box.addButton("Discard" if english else "不保存",
                            QMessageBox.ButtonRole.DestructiveRole)
    cancel = box.addButton("Cancel" if english else "取消",
                           QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(save)
    box.setEscapeButton(cancel)
    box.exec()
    clicked = box.clickedButton()
    if clicked is save:
        return "save"
    if clicked is discard:
        return "discard"
    return "cancel"
