"""应用主题：qdarktheme（PyQtDarkTheme，MIT）的扁平深浅色主题。

选型（对应「更现代、更美观、较为成熟的 PySide6 UI 库」）：
    * **扁平、克制的桌面级设计**（类 macOS/VS Code 观感），对比 Material
      风格更符合桌面工具的审美；深浅两套主题开箱即用；
    * **QSS 层**的主题库——所有控件仍是标准 QWidget 子类，
      不替换控件类，业务代码与既有测试完全不受影响；
    * 自绘区域（画布 :meth:`CanvasView.drawBackground`、G-code 预览、
      字体缩略图）自己填色，不受样式表背景影响；
    * 图标走 qtawesome、统一用两套主题下都清晰的中性灰；
    * 库缺失（极端环境）时 :func:`apply_theme` 返回 False，界面退化为
      系统原生样式，绝不因主题炸掉应用。

主题经 ``Settings``（``ui/theme``）持久化：``dark``（默认）/ ``light``，
视图菜单 → 主题 可切换，切换即重设 QApplication 样式表
（全局生效，所有对话框因共享 QApplication 一并换肤）。

注：PyPI 包名为 ``pyqtdarktheme-fork``（原 PyQtDarkTheme 的持续维护
分支，适配 Qt 6.11+），导入名仍是 ``qdarktheme``。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QApplication

THEME_DARK = "dark"
THEME_LIGHT = "light"
THEMES = (THEME_DARK, THEME_LIGHT)
DEFAULT_THEME = THEME_DARK

#: 当前已应用的主题。qdarktheme 虽然设置 QPalette，但其中 WindowText 仍是
#: 黑色（文字颜色实际由 QSS 负责），自绘/内联颜色不能读 palette——统一从
#: 这里取语义色。
_CURRENT_THEME = DEFAULT_THEME

#: 语义色令牌。深/浅两套都按对比度挑选：次要文字 ≥ 5:1，错误/成功 ≥ 5:1。
_ROLE_COLORS = {
    THEME_DARK: {
        "text": "#ffffff",
        "muted": "#9a9a9a",
        "danger": "#ff7b72",
        "danger_hover": "rgba(255,123,114,34)",
        "success": "#7ee787",
        "bar_bg": "rgba(45,45,48,235)",
        "bar_border": "#5a5a5e",
        "highlight": "#3d6fa8",
        "highlight_text": "#ffffff",
        "field_border": "#4e5258",
        "field_bg": "#2b2d30",
        "field_disabled_border": "#3a3d42",
        "canvas_bg": "#3f434a",
        "separator": "#4a4e56",
        "chip_hover": "rgba(255,255,255,26)",
        "segment_bg": "#2b2d30",
        "segment_border": "#464a50",
        "preview_border": "#4e5258",
    },
    THEME_LIGHT: {
        "text": "#1f1f1f",
        "muted": "#5f6368",
        "danger": "#b3261e",
        "danger_hover": "rgba(179,38,30,22)",
        "success": "#1a7f37",
        "bar_bg": "rgba(250,250,252,235)",
        "bar_border": "#b8c4d0",
        "highlight": "#2f6fbf",
        "highlight_text": "#ffffff",
        "field_border": "#bfc6d0",
        "field_bg": "#ffffff",
        "field_disabled_border": "#dce0e6",
        "canvas_bg": "#d8dde5",
        "separator": "#c5ccd6",
        "chip_hover": "rgba(0,0,0,16)",
        "segment_bg": "#eef1f5",
        "segment_border": "#cbd2dc",
        "preview_border": "#c4c9d0",
    },
}

_THEME_LABELS = {
    THEME_DARK: "深色",
    THEME_LIGHT: "浅色",
}


def current_theme() -> str:
    """最近一次成功应用的主题名（主题库缺失时为默认深色）。"""
    return _CURRENT_THEME


def semantic_color(role: str) -> str:
    """取当前主题的语义色（text / muted / danger / success …）。"""
    return _ROLE_COLORS[normalize_theme(_CURRENT_THEME)].get(
        role, _ROLE_COLORS[DEFAULT_THEME].get(role, "#808080"))


#: 最近一次 apply_theme 是否成功挂载 qdarktheme（用于原生降级样式判断）。
_THEME_ACTIVE = False


def theme_active() -> bool:
    """当前应用是否已通过 qdarktheme 套用主题样式表。"""
    app = QApplication.instance()
    return _THEME_ACTIVE and app is not None and bool(app.styleSheet())


def _role_qss(theme: str) -> str:
    """让内联 ``wsRole`` / ``wsField`` / ``wsSegment`` 等语义属性跟随主题。

    qdarktheme 的 QSS 里 QLabel 默认文字色是浅色，但硬编码
    ``color:#666`` 的说明文字在深色背景上几乎看不见（对比度约 2:1）。
    改用属性选择器后，切主题时整条 QSS 重建，所有标签、输入控件边框、
    分段按钮与分隔线自动换色。
    """
    c = _ROLE_COLORS[normalize_theme(theme)]
    field_classes = ("QSpinBox", "QDoubleSpinBox", "QComboBox", "QLineEdit")
    fields = ", ".join(f'{cls}[wsField="true"]' for cls in field_classes)
    fields_focus = ", ".join(f'{cls}[wsField="true"]:focus' for cls in field_classes)
    fields_disabled = ", ".join(f'{cls}[wsField="true"]:disabled' for cls in field_classes)
    return (
        f'QLabel[wsRole="muted"] {{ color: {c["muted"]}; }}'
        f'QLabel[wsRole="danger"] {{ color: {c["danger"]}; }}'
        f'QLabel[wsRole="success"] {{ color: {c["success"]}; }}'
        f'QPushButton[wsRole="danger"] {{'
        f' color: {c["danger"]}; border: 1px solid {c["danger"]}; }}'
        f'QPushButton[wsRole="danger"]:hover {{ background: {c["danger_hover"]}; }}'
        f'QPushButton[wsRole="danger"]:disabled {{'
        f' color: {c["muted"]}; border-color: {c["field_disabled_border"]};'
        f' background: transparent; }}'
        f'QPushButton[wsSegment="true"] {{'
        f' background: {c["segment_bg"]};'
        f' border: 1px solid {c["segment_border"]};'
        f' border-radius: 4px; padding: 3px 8px; }}'
        f'QPushButton[wsSegment="true"]:checked {{'
        f' background: {c["highlight"]};'
        f' border-color: {c["highlight"]};'
        f' color: {c["highlight_text"]}; font-weight: bold; }}'
        f'QLabel[wsClickable="true"] {{'
        f' padding: 1px 6px; border-radius: 4px; }}'
        f'QLabel[wsClickable="true"]:hover {{'
        f' background: {c["chip_hover"]}; color: {c["highlight"]}; }}'
        f'QToolBar::separator {{'
        f' background: {c["separator"]}; width: 1px; margin: 5px 5px; }}'
        f'QFrame[wsStatusSep="true"] {{'
        f' color: {c["separator"]}; max-width: 1px; margin: 3px 4px; }}'
        f'{fields} {{'
        f' border: 1px solid {c["field_border"]};'
        f' background-color: {c["field_bg"]};'
        f' border-radius: 4px; }}'
        f'QComboBox[wsField="true"] QLineEdit {{'
        f' border: none; background: transparent; padding: 0;'
        f' min-height: 0px; max-height: 16777215px; }}'
        f'{fields_disabled} {{'
        f' border: 1px solid {c["field_disabled_border"]}; }}'
        f'{fields_focus} {{'
        f' border: 1px solid palette(highlight); }}'
    )


def stroke_bar_qss() -> str:
    """笔画编辑浮条配色（画布浮动控件自绘，不能靠主题 QSS 继承）。"""
    c = _ROLE_COLORS[normalize_theme(_CURRENT_THEME)]
    return (
        f"QWidget {{ background: {c['bar_bg']};"
        f" border: 1px solid {c['bar_border']}; border-radius: 6px; }}"
        f"QToolButton {{ border: none; padding: 3px 8px;"
        f" color: {c['text']}; border-radius: 4px; }}"
        f"QToolButton:checked {{ background: {c['highlight']};"
        f" color: {c['highlight_text']}; }}"
    )


def theme_label(theme: str) -> str:
    """主题的人类可读名（菜单项用）。"""
    return _THEME_LABELS.get(theme, theme)


def normalize_theme(theme: Optional[str]) -> str:
    """非法/缺失的主题名归一为默认深色。"""
    return theme if theme in THEMES else DEFAULT_THEME


def available() -> bool:
    """qdarktheme 是否可用（依赖缺失时应用整体退化为原生样式）。

    必须走 ``builtins.__import__``（测试用 fake import 模拟库缺失），
    ``importlib.find_spec`` 会绕过导入钩子破坏降级契约。
    """
    try:
        __import__("qdarktheme")
        return True
    except Exception:
        return False


def apply_theme(app: QApplication, theme: Optional[str]) -> bool:
    """把主题应用到整个应用（样式表 + 语义色规则）。

    返回是否成功；失败（库缺失）时保持原样式不变。重复调用即换主题
    （热切换）。``theme`` 非法时归一为默认深色。
    """
    if app is None:
        return False
    global _CURRENT_THEME, _THEME_ACTIVE
    name = normalize_theme(theme)
    try:
        import qdarktheme
        qdarktheme.setup_theme(name, additional_qss=_role_qss(name))
        _CURRENT_THEME = name
        _THEME_ACTIVE = True
        try:
            from .style import refresh_field_styles
            refresh_field_styles()
        except Exception:
            pass
        return True
    except Exception:
        return False
