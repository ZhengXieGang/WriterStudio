"""页面导出 PNG / SVG：尺寸、坐标、内容与菜单动作回归。"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.core.document import (  # noqa: E402
    Document,
    DocumentObject,
    PageSpec,
    SourceSpec,
)
from writerstudio.core.strokes import Stroke  # noqa: E402
from writerstudio.export import (  # noqa: E402
    DEFAULT_DPI,
    _f,
    page_to_svg,
    render_page_image,
    save_page_png,
    save_page_svg,
)

SVG_NS = "{http://www.w3.org/2000/svg}"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _doc_two_lines(*, visible: bool = True) -> Document:
    """A4 横向页面上：一条 (10,10)-(60,10) 横线 + 一条 (10,20)-(10,50) 竖线。"""
    doc = Document(page=PageSpec(297.0, 210.0, 10.0, "A4 横向"))
    obj = DocumentObject(name="线", source=SourceSpec("text", {}))
    obj.local_strokes = [Stroke([(10.0, 10.0), (60.0, 10.0)]),
                         Stroke([(10.0, 20.0), (10.0, 50.0)])]
    obj.visible = visible
    doc.add(obj)
    return doc


# --------------------------------------------------------------------- PNG
def test_png_pixel_size_matches_dpi(qapp):
    doc = _doc_two_lines()
    img = render_page_image(doc, 300.0)
    assert img.width() == round(297.0 * 300.0 / 25.4)      # 3508
    assert img.height() == round(210.0 * 300.0 / 25.4)     # 2480


def test_png_has_ink_at_expected_position(qapp):
    """(10,10) 的横线应画在 y=(210-10)mm 处（内部 y 向上、图像 y 向下）。"""
    doc = _doc_two_lines()
    img = render_page_image(doc, DEFAULT_DPI)
    scale = DEFAULT_DPI / 25.4
    assert img.pixelColor(round(35 * scale), round(200 * scale)).lightness() \
        < 128                                               # 线中点：有色
    assert img.pixelColor(round(150 * scale), round(100 * scale)).lightness() \
        > 200                                               # 空白处：白


def test_png_skips_invisible_objects(qapp):
    doc = _doc_two_lines(visible=False)
    img = render_page_image(doc)
    assert img.pixelColor(round(35 * DEFAULT_DPI / 25.4),
                          round(200 * DEFAULT_DPI / 25.4)).lightness() > 200


def test_save_png_writes_file(qapp, tmp_path):
    doc = _doc_two_lines()
    out = tmp_path / "page.png"
    w, h = save_page_png(doc, str(out))
    assert out.exists() and out.stat().st_size > 0
    from PySide6.QtGui import QImage
    loaded = QImage(str(out))
    assert (loaded.width(), loaded.height()) == (w, h)


# --------------------------------------------------------------------- SVG
def test_svg_is_valid_xml_with_physical_size(qapp):
    root = ET.fromstring(page_to_svg(_doc_two_lines()))
    assert root.get("width") == "297mm"
    assert root.get("height") == "210mm"
    assert root.get("viewBox") == "0 0 297 210"


def test_svg_paths_flip_y_axis(qapp):
    """内部 (10,10) → SVG (10,200)：y 轴从向上翻成向下。"""
    root = ET.fromstring(page_to_svg(_doc_two_lines()))
    paths = root.findall(f".//{SVG_NS}path")
    assert len(paths) == 1                        # 一个对象 → 一个 path
    d = paths[0].get("d")
    assert d.startswith("M10 200L60 200")         # 横线
    assert "M10 190L10 160" in d                  # 竖线 (20→190, 50→160)


def test_svg_skips_invisible_objects(qapp):
    root = ET.fromstring(page_to_svg(_doc_two_lines(visible=False)))
    assert root.findall(f".//{SVG_NS}path") == []


def test_save_svg_counts_objects(qapp, tmp_path):
    doc = _doc_two_lines()
    out = tmp_path / "page.svg"
    n = save_page_svg(doc, str(out))
    assert n == 1
    ET.parse(str(out))                            # 落盘内容是合法 XML


def test_number_formatting_is_compact():
    assert _f(10.0) == "10"
    assert _f(10.5) == "10.5"
    assert _f(0.35) == "0.35"
    assert _f(-0.0) == "0"


# --------------------------------------------------------------- 菜单动作
def _patch_export_ui(monkeypatch, path: str, dpi: int | None = None):
    """打桩：DPI 弹窗（可返回值/取消）与保存对话框、完成提示。"""
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from writerstudio.ui.export_dialog import PngExportDialog

    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (path, "")))
    monkeypatch.setattr(PngExportDialog, "ask",
                        classmethod(lambda cls, *a, **k: dpi))
    return shown


def test_menu_actions_export_files(qapp, tmp_path, monkeypatch):
    """走真实 UI 路径：菜单动作 → DPI 弹窗 → 文件对话框（打桩）→ 落盘。"""
    from writerstudio.fonts.builder import TextSpec, make_text_object
    from writerstudio.settings import K_AI_ENABLED, Settings
    from writerstudio.ui.main_window import MainWindow

    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    try:
        win.confirm_on_close = False
        obj = make_text_object(TextSpec(text="AB", font_names=["futural"],
                                        size=10.0), win.font_manager)
        win.controller.add_object(obj)

        png = tmp_path / "out.png"
        shown = _patch_export_ui(monkeypatch, str(png), dpi=150)
        win.act_export_png.trigger()
        assert png.exists(), shown
        assert "150 DPI" in shown[-1]
        from PySide6.QtGui import QImage
        img = QImage(str(png))
        assert img.width() == round(297.0 / 25.4 * 150)     # 按所选 DPI 输出

        svg = tmp_path / "out.svg"
        _patch_export_ui(monkeypatch, str(svg))
        win.act_export_svg.trigger()
        assert svg.exists()
        ET.parse(str(svg))
    finally:
        win.confirm_on_close = False
        win.close()


def test_export_png_dialog_cancel_aborts(qapp, tmp_path, monkeypatch):
    """在 DPI 弹窗点取消：不弹保存对话框、不写文件。"""
    from PySide6.QtWidgets import QFileDialog

    from writerstudio.fonts.builder import TextSpec, make_text_object
    from writerstudio.settings import K_AI_ENABLED, Settings
    from writerstudio.ui.main_window import MainWindow

    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    try:
        win.confirm_on_close = False
        win.controller.add_object(make_text_object(
            TextSpec(text="A", font_names=["futural"]), win.font_manager))
        called = _patch_export_ui(
            monkeypatch, str(tmp_path / "never.png"), dpi=None)
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName",
            staticmethod(lambda *a, **k: called.append("file-dialog") or ("", "")))
        win.act_export_png.trigger()
        assert "file-dialog" not in called
        assert not (tmp_path / "never.png").exists()
    finally:
        win.confirm_on_close = False
        win.close()


def test_png_dpi_is_remembered(qapp, tmp_path, monkeypatch):
    """所选 DPI 记忆进设置，下次弹窗以其为默认值。"""
    from writerstudio.fonts.builder import TextSpec, make_text_object
    from writerstudio.settings import K_AI_ENABLED, Settings
    from writerstudio.ui.main_window import MainWindow

    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    try:
        win.confirm_on_close = False
        win.controller.add_object(make_text_object(
            TextSpec(text="A", font_names=["futural"]), win.font_manager))
        _patch_export_ui(monkeypatch, str(tmp_path / "a.png"), dpi=600)
        win.act_export_png.trigger()
        assert st.png_export_dpi() == 600
        assert win.settings.png_export_dpi() == 600
    finally:
        win.confirm_on_close = False
        win.close()


def test_export_png_extends_missing_suffix(qapp, tmp_path, monkeypatch):
    """用户把文件名写成 output（无扩展名）时补上 .png，内容与扩展名一致。"""
    from writerstudio.fonts.builder import TextSpec, make_text_object
    from writerstudio.settings import K_AI_ENABLED, Settings
    from writerstudio.ui.main_window import MainWindow

    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    try:
        win.confirm_on_close = False
        win.controller.add_object(make_text_object(
            TextSpec(text="A", font_names=["futural"]), win.font_manager))
        _patch_export_ui(monkeypatch, str(tmp_path / "out"), dpi=300)
        win.act_export_png.trigger()
        assert (tmp_path / "out.png").exists()
    finally:
        win.confirm_on_close = False
        win.close()


def test_export_on_empty_document_is_harmless(qapp, monkeypatch):
    """空文档导出只提示、不弹任何对话框、不崩溃。"""
    from PySide6.QtWidgets import QFileDialog

    from writerstudio.settings import K_AI_ENABLED, Settings
    from writerstudio.ui.export_dialog import PngExportDialog
    from writerstudio.ui.main_window import MainWindow

    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    try:
        win.confirm_on_close = False
        called: list[str] = []
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: called.append("file")))
        monkeypatch.setattr(PngExportDialog, "ask",
                            classmethod(lambda cls, *a, **k: called.append("dpi")))
        win.act_export_png.trigger()
        win.act_export_svg.trigger()
        assert called == []
        assert "空" in win.statusBar().currentMessage()
    finally:
        win.confirm_on_close = False
        win.close()


# ----------------------------------------------------------- DPI 选择弹窗
def test_dpi_dialog_shows_pixel_size(qapp):
    from writerstudio.core.document import PageSpec
    from writerstudio.ui.export_dialog import PngExportDialog, pixel_size

    page = PageSpec(297.0, 210.0, 10.0, "A4 横向")
    dlg = PngExportDialog(page, 300)
    try:
        assert dlg.dpi() == 300
        assert "3508 × 2480 像素" in dlg._size_label.text()
        dlg.dpi_spin.setValue(600)
        assert "7016 × 4961 像素" in dlg._size_label.text()
        assert pixel_size(page, 600) == (7016, 4961)
    finally:
        dlg.deleteLater()


def test_dpi_dialog_warns_on_huge_output(qapp):
    from writerstudio.core.document import PageSpec
    from writerstudio.ui.export_dialog import PngExportDialog

    dlg = PngExportDialog(PageSpec(297.0, 210.0, 10.0, "A4 横向"), 1200)
    try:
        assert "很大" in dlg._size_label.text()        # 1200 DPI 提示内存
        dlg.dpi_spin.setValue(300)
        assert "很大" not in dlg._size_label.text()
    finally:
        dlg.deleteLater()


def test_dpi_setting_clamps_to_valid_range(qapp):
    from writerstudio.settings import Settings

    st = Settings()
    st.set_png_export_dpi(99999)
    assert st.png_export_dpi() == Settings.PNG_DPI_MAX
    st.set_png_export_dpi(1)
    assert st.png_export_dpi() == Settings.PNG_DPI_MIN
