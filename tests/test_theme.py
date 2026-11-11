"""P29 主题迁移测试：qdarktheme 全局主题的应用、切换与持久化。

约定：
    * 主题是 **QSS + QPalette 层**的（qdarktheme），控件类不变，
      业务测试不受影响；
    * 主题应用失败（库缺失）必须优雅降级为原生样式，不允许抛异常；
    * 视图菜单 → 主题 可热切换并持久化到 Settings。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.settings import _MemoryBackend  # noqa: E402
from writerstudio.settings import Settings  # noqa: E402
from writerstudio.ui.theme import (  # noqa: E402
    DEFAULT_THEME,
    THEME_DARK,
    THEME_LIGHT,
    apply_theme,
    available,
    normalize_theme,
    theme_label,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _purge_windows(qapp):
    """主题测试会整应用重设样式；清掉前面用例遗留的窗口，避免重绘上千控件。"""
    def purge():
        from PySide6.QtCore import QCoreApplication, QEvent

        for w in qapp.topLevelWidgets():
            try:
                # 不调用 close()：未保存文档会弹模态框；直接销毁即可
                if hasattr(w, "confirm_on_close"):
                    w.confirm_on_close = False
                w.deleteLater()
            except RuntimeError:
                pass
        # processEvents 默认不处理 DeferredDelete，要显式派发，否则窗口仍在
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()

    purge()
    yield
    purge()


def test_normalize_and_labels():
    assert normalize_theme(None) == DEFAULT_THEME
    assert normalize_theme("") == DEFAULT_THEME
    assert normalize_theme("nonsense") == DEFAULT_THEME
    assert normalize_theme("light") == THEME_LIGHT
    assert theme_label("dark") == "深色"
    assert theme_label("light") == "浅色"


def test_settings_theme_roundtrip():
    s = Settings(_MemoryBackend())
    assert s.ui_theme() == DEFAULT_THEME      # 缺省深色
    s.set_ui_theme("light")
    assert s.ui_theme() == THEME_LIGHT
    s.set_ui_theme("bogus")
    assert s.ui_theme() == DEFAULT_THEME      # 非法值回退


def test_apply_theme_returns_bool(qapp):
    assert isinstance(available(), bool)
    if not available():
        pytest.skip("qdarktheme 未安装")
    assert apply_theme(qapp, THEME_DARK) is True
    assert len(qapp.styleSheet()) > 1000      # QSS 真正生效
    assert apply_theme(qapp, THEME_LIGHT) is True
    assert apply_theme(qapp, "bogus") is True  # 非法名归一为默认，仍成功
    qapp.setStyleSheet("")                     # 清理，不污染后续测试


def test_semantic_roles_are_injected_into_qss(qapp):
    """说明文字/错误/成功色走属性选择器，切主题时自动换色。"""
    if not available():
        pytest.skip("qdarktheme 未安装")
    assert apply_theme(qapp, THEME_DARK) is True
    qss = qapp.styleSheet()
    for role in ("muted", "danger", "success"):
        assert f'QLabel[wsRole="{role}"]' in qss
    from writerstudio.ui.theme import _ROLE_COLORS, semantic_color
    assert semantic_color("muted") != semantic_color("text")
    # 浅色令牌单独配置，不需要再跑一次全应用重绘（测试进程里窗口很多，很慢）
    assert _ROLE_COLORS[THEME_LIGHT]["muted"] != _ROLE_COLORS[THEME_DARK]["muted"]
    qapp.setStyleSheet("")


def _icon_luma(icon) -> int:
    """图标不透明像素的最大亮度（白图 ≈255，黑图 ≈0）。"""
    img = icon.pixmap(32, 32).toImage()
    values = []
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 200:
                values.append((c.red() + c.green() + c.blue()) // 3)
    return max(values) if values else -1


def test_icon_color_follows_theme(qapp):
    """图标前景色：深色主题白、浅色主题黑。"""
    if not available():
        pytest.skip("qdarktheme 未安装")
    from writerstudio.ui import icons

    apply_theme(qapp, THEME_DARK)
    icons.clear_cache()
    assert icons._foreground_color() == "#ffffff"
    apply_theme(qapp, THEME_LIGHT)
    icons.clear_cache()
    assert icons._foreground_color() == "#000000"
    qapp.setStyleSheet("")


def test_theme_switch_repaints_icons(qapp):
    """切主题后工具栏/面板 QIcon 必须按新前景色重建，而不是保留旧颜色。"""
    if not available():
        pytest.skip("qdarktheme 未安装")
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win._apply_theme_setting(THEME_DARK)
        qapp.processEvents()
        if win.act_new.icon().isNull():
            pytest.skip("qtawesome 不可用")
        assert _icon_luma(win.act_new.icon()) >= 200
        win._apply_theme_setting(THEME_LIGHT)
        qapp.processEvents()
        assert _icon_luma(win.act_new.icon()) <= 60
        # 面板按钮图标也一起重建（曾把动作键当 fa 图标名，导致根本没有图标）
        assert not win.font_panel._icon_buttons[0][0].icon().isNull()
    finally:
        win.close()
        qapp.setStyleSheet("")


def test_apply_theme_degrades_gracefully(qapp, monkeypatch):
    """qdarktheme 缺失时返回 False 且不抛异常（保持原样式）。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "qdarktheme":
            raise ImportError("no qdarktheme")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    before = qapp.styleSheet()
    assert apply_theme(qapp, THEME_DARK) is False
    assert qapp.styleSheet() == before
    assert available() is False


def test_theme_switch_persists_and_applies(qapp):
    """视图菜单 → 主题：切换即应用并写回设置。"""
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(_MemoryBackend()))
    win.confirm_on_close = False
    if not available():
        pytest.skip("qdarktheme 未安装")
    win._apply_theme_setting(THEME_LIGHT)
    assert win.settings.ui_theme() == THEME_LIGHT
    assert len(QApplication.instance().styleSheet()) > 1000
    win._apply_theme_setting(THEME_DARK)
    assert win.settings.ui_theme() == THEME_DARK
    qapp.setStyleSheet("")
    win.close()


def test_theme_menu_built(qapp):
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(_MemoryBackend()))
    menu = win._build_theme_menu()
    assert menu.title() == "主题"
    actions = menu.actions()
    assert len(actions) == 2
    assert [a.text() for a in actions] == ["深色", "浅色"]
    checked = [a for a in actions if a.isChecked()]
    assert len(checked) == 1                    # 互斥且恰有一项选中
    qapp.setStyleSheet("")
    win.close()
