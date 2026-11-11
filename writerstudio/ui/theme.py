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
        "success": "#7ee787",
        "bar_bg": "rgba(45,45,48,235)",
        "bar_border": "#5a5a5e",
        "highlight": "#3d6fa8",
        "highlight_text": "#ffffff",
    },
    THEME_LIGHT: {
        "text": "#1f1f1f",
        "muted": "#5f6368",
        "danger": "#b3261e",
        "success": "#1a7f37",
        "bar_bg": "rgba(250,250,252,235)",
        "bar_border": "#b8c4d0",
        "highlight": "#2f6fbf",
        "highlight_text": "#ffffff",
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


def _role_qss(theme: str) -> str:
    """让内联 ``wsRole`` 属性跟随主题的全局规则。

    qdarktheme 的 QSS 里 QLabel 默认文字色是浅色，但硬编码
    ``color:#666`` 的说明文字在深色背景上几乎看不见（对比度约 2:1）。
    改用属性选择器后，切主题时整条 QSS 重建，所有标签自动换色。
    """
    c = _ROLE_COLORS[normalize_theme(theme)]
    return (
        f'QLabel[wsRole="muted"] {{ color: {c["muted"]}; }}'
        f'QLabel[wsRole="danger"] {{ color: {c["danger"]}; }}'
        f'QLabel[wsRole="success"] {{ color: {c["success"]}; }}'
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
    global _CURRENT_THEME
    name = normalize_theme(theme)
    try:
        import qdarktheme
        qdarktheme.setup_theme(name, additional_qss=_role_qss(name))
        _CURRENT_THEME = name
        return True
    except Exception:
        return False
