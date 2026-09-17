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
