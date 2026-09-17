"""应用主题：qdarktheme（PyQtDarkTheme，MIT）的扁平深浅色主题。

选型（对应「更现代、更美观、较为成熟的 PySide6 UI 库」）：
    * **扁平、克制的桌面级设计**（类 macOS/VS Code 观感），对比 Material
      风格更符合桌面工具的审美；深浅两套主题开箱即用；
    * **QSS + QPalette 层**的主题库——所有控件仍是标准 QWidget 子类，
      不替换控件类，业务代码与既有测试完全不受影响；
    * 自绘区域（画布 :meth:`CanvasView.drawBackground`、G-code 预览、
      字体缩略图）自己填色，不受样式表背景影响；
    * 图标走 qtawesome、颜色跟随调色板，深浅主题都清晰；
    * 库缺失（极端环境）时 :func:`apply_theme` 返回 False，界面退化为
      系统原生样式，绝不因主题炸掉应用。

主题经 ``Settings``（``ui/theme``）持久化：``dark``（默认）/ ``light``，
视图菜单 → 主题 可切换，切换即重设 QApplication 样式表与调色板
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

_THEME_LABELS = {
    THEME_DARK: "深色",
    THEME_LIGHT: "浅色",
}


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
    """把主题应用到整个应用（样式表 + 调色板）。

    返回是否成功；失败（库缺失）时保持原样式不变。重复调用即换主题
    （热切换）。``theme`` 非法时归一为默认深色。
    """
    if app is None:
        return False
    try:
        import qdarktheme
        qdarktheme.setup_theme(normalize_theme(theme))
        return True
    except Exception:
        return False
