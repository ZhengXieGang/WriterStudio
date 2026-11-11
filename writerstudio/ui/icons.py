"""应用图标：统一走 qtawesome 的 FontAwesome 6（solid）单一家族。

规矩（设计约定）：
    * 只用 **一个** 图标家族（fa6 solid），禁止混用多套风格；
    * 所有图标经 :func:`icon` 取用——名字写错或库缺失时返回 None，
      控件退化为纯文字，绝不因图标炸掉界面；
    * 图标只做视觉锚点，动作含义仍以菜单/工具提示文字为准。

颜色随主题：深色主题用白色、浅色主题用黑色；切换主题时调用方清缓存并重建
QIcon（见 :func:`clear_cache`）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtGui import QIcon, QPixmap

try:
    import qtawesome as qta
except Exception:            # 依赖缺失（极端环境）：整体退化为纯文字
    qta = None

# 动作 → 图标名（fa6 solid）。集中一处，工具栏/菜单/按钮共用。
ACTION_ICONS: dict[str, str] = {
    "undo": "arrow-rotate-left",
    "redo": "arrow-rotate-right",
    "new": "file",
    "open": "folder-open",
    "save": "floppy-disk",
    "new_text": "font",
    "edit_text": "pen-to-square",
    "insert_md": "table",
    "insert_eq": "square-root-variable",
    "add_ref_image": "image",
    "duplicate": "copy",
    "delete": "trash-can",
    "stroke_edit": "pen-nib",
    "raise": "arrow-up",
    "lower": "arrow-down",
    "page_setup": "gear",
    "rotate_cw": "rotate-right",
    "rotate_ccw": "rotate-left",
    "zoom_out": "magnifying-glass-minus",
    "zoom_in": "magnifying-glass-plus",
    "fit": "up-right-and-down-left-from-center",
    "zoom_100": "magnifying-glass",
    # 面板/按钮用
    "reseed": "dice",
    "reshuffle_font": "shuffle",
    "warning": "triangle-exclamation",
    "check": "check",
    "pin": "thumbtack",
    "hide": "eye-slash",
    "import_font": "file-import",
    "add_dir": "folder-plus",
    "rescan": "arrows-rotate",
    "chevron_right": "chevron-right",
    "chevron_down": "chevron-down",
    "rotate_page_cw": "arrows-rotate",
}


def _foreground_color() -> str:
    """图标前景色：深色主题白、浅色主题黑。

    不能读 QPalette：qdarktheme 调色板里的 ``WindowText`` 在深色主题下
    仍是黑色（文字颜色由 QSS 负责），图标会几乎看不见。颜色取自当前
    主题；切换主题后由 :func:`clear_cache` + 调用方重建 QIcon。
    """
    try:
        from . import theme
        if theme.current_theme() == theme.THEME_LIGHT:
            return "#000000"
    except Exception:
        pass
    return "#ffffff"


#: 图标渲染档位：QIcon 里塞两档位图，工具栏/菜单/HiDPI 由 Qt 平滑缩放取用
_ICON_SIZES = (32, 64)
#: (名字, 颜色, 尺寸) → QPixmap（同一图标被多处取用，只渲染一次）
_PIXMAP_CACHE: dict[tuple[str, str, int], QPixmap] = {}


def clear_cache() -> None:
    """清空位图缓存；切换主题后调用，让图标按新前景色重新渲染。"""
    _PIXMAP_CACHE.clear()


def _drop_qta_cache() -> None:
    """清掉 qtawesome 自己的图标缓存。

    那里存的是**引擎是 Python 对象**的 QIcon（CharIconEngine）：留着它们，
    Qt 侧就还持有 Python 实现的对象，解释器退出时会在 Python 拆了一半之后
    回头调用（实测直接踩坏堆）。位图已经拿到手，缓存没必要留。
    """
    try:
        qta._instance().icon_cache.clear()      # noqa: SLF001（第三方私有 API）
    except Exception:
        pass


def _pixmap(name: str, color: str, size: int) -> Optional[QPixmap]:
    key = (name, color, size)
    pix = _PIXMAP_CACHE.get(key)
    if pix is not None:
        return pix
    try:
        pix = qta.icon(f"fa6s.{name}", color=color).pixmap(size, size)
        _drop_qta_cache()
    except Exception:
        return None
    if pix.isNull():
        return None
    _PIXMAP_CACHE[key] = pix
    return pix


def icon(name: str) -> Optional[QIcon]:
    """按 fa6s（FontAwesome 6 solid）名字取图标；库缺失/名字无效时返回
    None（控件退化为文字）。

    **不把 qtawesome 的 QIcon 直接交给控件**：它的引擎是 Python 对象，控件
    销毁/解释器退出时 Qt 会回头调用那个引擎——那一刻 Python 侧可能已经拆了
    一半，0.2.0 的退出段错误与 CI 门禁的 ``malloc(): unaligned fastbin chunk
    detected`` 都出在这条路径上。这里先渲染成位图，再包成 Qt 侧不含任何
    Python 对象的 QIcon。
    """
    if qta is None:
        return None
    color = _foreground_color()
    out = QIcon()
    for size in _ICON_SIZES:
        pix = _pixmap(name, color, size)
        if pix is not None:
            out.addPixmap(pix)
    return None if out.isNull() else out


def action_icon(key: str) -> Optional[QIcon]:
    """按动作键取图标（见 :data:`ACTION_ICONS`）。"""
    name = ACTION_ICONS.get(key)
    return icon(name) if name else None
