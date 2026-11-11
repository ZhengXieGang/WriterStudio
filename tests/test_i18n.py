"""界面语言：系统 locale 检测、手动切换与英文翻译完整性。"""

from __future__ import annotations

import ast
import glob
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio import i18n  # noqa: E402
from writerstudio.i18n_en import TRANSLATIONS  # noqa: E402
from writerstudio.settings import Settings, _MemoryBackend  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _use(monkeypatch, lang: str) -> None:
    monkeypatch.setenv("WRITERSTUDIO_LANG", lang)
    i18n.update()


def test_system_language_follows_locale(monkeypatch):
    for key in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(key, raising=False)
    # 测试机本身可能是中文桌面；locale 探测结果单独测
    monkeypatch.setattr(i18n, "_qt_locale_name", lambda: "")
    monkeypatch.setattr(i18n, "_python_locale_name", lambda: "")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert i18n.system_language() == i18n.LANG_ZH
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    assert i18n.system_language() == i18n.LANG_EN
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert i18n.system_language() == i18n.LANG_EN
    # 没有任何 locale 信息时按英文（AppImage 目录的收录要求）
    monkeypatch.delenv("LANG", raising=False)
    assert i18n.system_language() == i18n.LANG_EN
    # 空字符串视为未设置
    monkeypatch.setenv("LANG", "")
    assert i18n.system_language() == i18n.LANG_EN


def test_system_language_uses_qt_locale(monkeypatch):
    """Windows/macOS 从桌面启动没有 LANG，回退到 Qt 的系统 locale。"""
    for key in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(i18n, "_python_locale_name", lambda: "")
    monkeypatch.setattr(i18n, "_qt_locale_name", lambda: "zh_CN")
    assert i18n.system_language() == i18n.LANG_ZH
    monkeypatch.setattr(i18n, "_qt_locale_name", lambda: "de_DE")
    assert i18n.system_language() == i18n.LANG_EN
    # Qt 不可用时再用 Python locale
    monkeypatch.setattr(i18n, "_qt_locale_name", lambda: "")
    monkeypatch.setattr(i18n, "_python_locale_name",
                        lambda: "Chinese (Simplified)_China")
    assert i18n.system_language() == i18n.LANG_ZH


def test_resolve_preference():
    assert i18n.resolve("zh") == i18n.LANG_ZH
    assert i18n.resolve("en") == i18n.LANG_EN
    assert i18n.resolve("") == i18n.system_language()
    assert i18n.resolve("bogus") == i18n.system_language()


def test_tr_and_trf_switch(monkeypatch):
    _use(monkeypatch, "en")
    assert i18n.tr("保存") == "Save"
    assert i18n.tr("不存在的词条") == "不存在的词条"    # 缺词回退原文
    assert i18n.trf("已保存 {v0}", v0="a.wsproj") == "Saved a.wsproj"
    _use(monkeypatch, "zh")
    assert i18n.tr("保存") == "保存"
    assert i18n.trf("已保存 {v0}", v0="a.wsproj") == "已保存 a.wsproj"


def test_every_wrapped_string_has_translation():
    """源码里每个 tr()/trf() 字面量都必须有英文词条（否则英文界面会露中文）。"""
    missing = []
    for path in glob.glob("writerstudio/**/*.py", recursive=True):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("tr", "trf") and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value not in TRANSLATIONS:
                missing.append((path, node.lineno, arg.value))
    assert not missing, f"缺少英文词条：{missing[:10]}"


def test_placeholders_are_preserved():
    """英文译文必须保留与中文模板相同的一组 {vN} 占位符。"""
    import re

    ph = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)")
    bad = []
    for zh, en in TRANSLATIONS.items():
        if "{" not in zh:
            continue
        a = set(ph.findall(zh))
        b = set(ph.findall(en))
        if a != b:
            bad.append((zh, en, a, b))
    assert not bad, f"占位符不一致：{bad[:5]}"


def test_settings_roundtrip_language():
    s = Settings(backend=_MemoryBackend())
    assert s.ui_language() == "auto"
    s.set_ui_language("en")
    assert s.ui_language() == "en"
    s.set_ui_language("bogus")
    assert s.ui_language() == "auto"


def test_language_menu_persists_choice(qapp, monkeypatch):
    from writerstudio.ui import main_window as mw

    # 触发真实切换时会弹「重启后生效」提示；测试里吞掉它
    monkeypatch.setattr(mw.QMessageBox, "information",
                        lambda *a, **k: None)
    settings = Settings(backend=_MemoryBackend())
    win = mw.MainWindow(settings=settings)
    try:
        menu = win._build_language_menu()
        actions = {a.data(): a for a in menu.actions()}
        assert set(actions) == {"auto", "zh", "en"}
        actions["en"].trigger()
        assert settings.ui_language() == "en"
        assert actions["en"].isChecked()
    finally:
        win.close()


def test_english_main_window_chrome(qapp, monkeypatch):
    _use(monkeypatch, "en")
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    try:
        assert "写字机" not in win.windowTitle()
        assert "WriterStudio" in win.windowTitle()
        menus = [a.text().replace("&", "") for a in win.menuBar().actions()]
        for want in ("File(F)", "View(V)", "Help(H)"):
            assert want in menus, menus
        # 「语言」是视图菜单的子菜单
        view = next(a.menu() for a in win.menuBar().actions()
                    if a.text().replace("&", "") == "View(V)")
        assert any(sub.text() == "Language" for sub in view.actions()
                   if sub.menu() is not None)
        # 状态栏固定文案
        texts = [lab.text() for lab in win.statusBar().findChildren(type(
            win._status_page))]
        assert "Paper:" in texts and "Position (mm):" in texts
    finally:
        win.close()


def test_english_default_names_and_duration(monkeypatch):
    """英文界面下：默认对象名与时长单位也必须是英文（不能只翻标签）。"""
    _use(monkeypatch, "en")
    try:
        from writerstudio.fonts.builder import TextSpec, _default_name
        from writerstudio.machine.gcode_gen import format_duration

        assert _default_name(TextSpec(text="Hello world")) == "Text: Hello world"
        assert _default_name(TextSpec(text="")) == "Text"
        assert format_duration(61) == "1 min 1 s"
        assert format_duration(3661) == "1 h 1 min"
        assert format_duration(9) == "9 s"
    finally:
        _use(monkeypatch, "zh")


def test_dialog_buttons_follow_language(qapp, monkeypatch):
    """标准按钮不走 Qt 翻译包：中文界面必须是确定/取消，英文界面是 OK/Cancel。"""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

    from writerstudio.ui.style import localize_dialog_buttons

    dlg = QDialog()
    box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    QVBoxLayout(dlg).addWidget(box)
    localize_dialog_buttons(dlg)
    assert box.button(QDialogButtonBox.Ok).text() == "确定"
    assert box.button(QDialogButtonBox.Cancel).text() == "取消"
    _use(monkeypatch, "en")
    localize_dialog_buttons(dlg)
    assert box.button(QDialogButtonBox.Ok).text() == "OK"
    assert box.button(QDialogButtonBox.Cancel).text() == "Cancel"
    _use(monkeypatch, "zh")


def test_english_perturb_panel_fits_default_dock(qapp, monkeypatch):
    """英文手写扰动面板在 280px 左停靠区内不得出现横向滚动条。"""
    from PySide6.QtWidgets import QScrollArea

    from writerstudio.ui.theme import apply_theme, available

    if available():
        apply_theme(qapp, "dark")
    _use(monkeypatch, "en")
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win.show()
        qapp.processEvents()
        win._apply_default_dock_widths()
        win.perturb_dock.raise_()
        qapp.processEvents()
        qapp.processEvents()
        areas = win.perturb_dock.findChildren(QScrollArea)
        assert areas, "扰动面板应有滚动区"
        for area in areas:
            assert not area.horizontalScrollBar().isVisible()
    finally:
        win.close()
        _use(monkeypatch, "zh")
        qapp.setStyleSheet("")


def test_english_machine_panel_fits_default_dock(qapp, monkeypatch):
    """英文机器面板不得因长文案撑出横向滚动条（默认 300px 停靠宽内完整显示）。"""
    from PySide6.QtWidgets import QScrollArea

    from writerstudio.ui.theme import apply_theme, available

    if available():
        apply_theme(qapp, "dark")     # 与真实启动一致（原生样式按钮更宽）
    _use(monkeypatch, "en")
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow(settings=Settings(backend=_MemoryBackend()))
    win.confirm_on_close = False
    try:
        win.show()
        qapp.processEvents()
        win._apply_default_dock_widths()
        win.machine_dock.raise_()
        qapp.processEvents()
        qapp.processEvents()
        body = win.machine_panel.findChild(QScrollArea).widget()
        assert body.minimumSizeHint().width() <= 300
        assert win.machine_dock.width() >= 280
        assert not win.machine_panel.findChild(
            QScrollArea).horizontalScrollBar().isVisible()
    finally:
        win.close()
        _use(monkeypatch, "zh")
        qapp.setStyleSheet("")
