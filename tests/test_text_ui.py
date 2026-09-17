"""P2 UI 冒烟测试：文本对话框、字体面板、文本对象创建/编辑闭环。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.fonts.builder import SOURCE_KIND_TEXT, TextSpec  # noqa: E402
from writerstudio.fonts.manager import FontManager  # noqa: E402
from writerstudio.ui.main_window import MainWindow  # noqa: E402
from writerstudio.ui.text_dialog import TextEditDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_text_dialog_builds_and_reports_spec(qapp):
    win = MainWindow()
    spec = TextSpec(text="Hello 世界", font_names=["STRK-Kaiti", "futural"],
                    size=12.0, line_spacing=1.4)
    dlg = TextEditDialog(spec, win.font_manager)
    dlg.resize(700, 500)
    dlg.show()
    qapp.processEvents()
    out = dlg.result_spec()
    assert out.text == "Hello 世界"
    assert out.font_names == ["STRK-Kaiti", "futural"]
    assert out.size == pytest.approx(12.0)
    # 预览不应报错
    assert "字体使用" in dlg.preview_label.text()
    dlg.close()
    win.close()


def test_text_dialog_font_chain_reorder(qapp):
    win = MainWindow()
    spec = TextSpec(text="AB", font_names=["futural", "scripts"], size=10)
    dlg = TextEditDialog(spec, win.font_manager)
    dlg.chain_list.setCurrentRow(0)
    dlg._move_font(1)  # futural 下移
    assert dlg.result_spec().font_names == ["scripts", "futural"]
    dlg.chain_list.setCurrentRow(0)
    dlg._remove_font()
    assert dlg.result_spec().font_names == ["futural"]
    dlg.close()
    win.close()


def test_font_panel_lists_and_selects(qapp):
    win = MainWindow()
    panel = win.font_panel
    assert panel.list.count() == len(win.font_manager.entries())
    # 选中一个英文字体，信息应更新
    for i in range(panel.list.count()):
        if panel.list.item(i).data(Qt.UserRole) == "futural":
            panel.list.setCurrentRow(i)
            break
    assert "futural" in panel.info.text() or "Hershey" in panel.info.text()
    win.close()


def test_main_window_new_text_flow(qapp, monkeypatch):
    """模拟「插入文本」：对话框直接返回一个 spec，验证对象被加入文档。"""
    win = MainWindow()
    n0 = len(win.controller.doc)

    def fake_exec(self):
        self.text_edit.setPlainText("Grbl 测试")
        return TextEditDialog.Accepted

    monkeypatch.setattr(TextEditDialog, "exec", fake_exec)
    win._new_text()
    assert len(win.controller.doc) == n0 + 1
    obj = win.controller.doc.objects[-1]
    assert obj.source.kind == SOURCE_KIND_TEXT
    assert obj.local_strokes
    win.close()


def test_main_window_remembers_last_font(qapp, monkeypatch):
    """新建文本使用的字体链会被记住，下一次新建自动默认选中。"""
    win = MainWindow()
    entries = [e.name for e in win.font_manager.visible_entries()]
    if not entries:
        win.close()
        pytest.skip("无可用字体")
    chosen = [entries[0]]

    def fake_exec(self):
        self.chain_list.clear()
        self._append_font(chosen[0])
        self.text_edit.setPlainText("A")
        return TextEditDialog.Accepted

    # 防止所选字体画不出该字符时弹出模态警告框（自动化下会一直等）
    monkeypatch.setattr("writerstudio.ui.main_window.QMessageBox.warning",
                        lambda *a, **k: None)
    monkeypatch.setattr(TextEditDialog, "exec", fake_exec)
    win._new_text()
    assert win.settings.last_font_chain() == chosen
    # 第二次：默认 spec 应带上记住的字体链
    spec = win._default_text_spec()
    assert spec.font_names == chosen
    win.close()


def test_default_font_chain_falls_back_when_saved_missing(qapp):
    """记住的字体已不存在时回退到内置启发式链，而不是给出无效字体。"""
    win = MainWindow()
    win.settings.set_last_font_chain(["__不存在的字体__"])
    chain = win._default_font_chain()
    assert "__不存在的字体__" not in chain
    assert chain                          # 至少有内置回退
    win.close()


def test_main_window_edit_text_flow(qapp, monkeypatch):
    win = MainWindow()
    # 先插入一个文本对象
    def fake_exec(self):
        self.text_edit.setPlainText("ABC")
        return TextEditDialog.Accepted

    monkeypatch.setattr(TextEditDialog, "exec", fake_exec)
    win._new_text()
    obj = win.controller.doc.objects[-1]
    w0 = obj.local_bbox().width

    def fake_exec2(self):
        self.text_edit.setPlainText("ABCDEFGH")
        return TextEditDialog.Accepted

    monkeypatch.setattr(TextEditDialog, "exec", fake_exec2)
    win.canvas.select_object(obj)
    win._edit_text()
    assert obj.local_bbox().width > w0
    assert obj.source.data["text"] == "ABCDEFGH"
    win.close()


# ===================================================== 字体面板增强
def test_font_panel_shows_glyph_count_and_thumbnail(qapp):
    """字体面板应展示每款字体的缩略图与字形数（后台加载后回填）。"""
    import time
    win = MainWindow()
    win.show()
    panel = win.font_panel
    # 面板可见时才触发后台加载（标签组里非当前标签则不加载）；
    # 加载有 250ms 去抖延迟，这里显式立即触发。
    panel.show()
    panel.raise_()
    qapp.processEvents()
    panel.ensure_loaded()
    qapp.processEvents()
    # 等后台线程把 futural 的字形数填上
    deadline = time.time() + 10.0
    while time.time() < deadline:
        qapp.processEvents()
        if panel._counts.get("futural") is not None:
            break
        time.sleep(0.02)
    idx = None
    for i in range(panel.list.count()):
        if panel.list.item(i).data(Qt.UserRole) == "futural":
            idx = i
            break
    assert idx is not None
    item = panel.list.item(idx)
    assert "字" in item.text()                 # 形如 "96 字"
    assert not item.icon().isNull()            # 有缩略图
    panel.list.setCurrentRow(idx)
    assert "字形" in panel.info.text()
    win.close()


def test_font_thumbnail_renders_glyphs(qapp):
    from writerstudio.ui.font_panel import font_thumbnail
    m = FontManager(user_font_dir="/nonexistent-xyz")
    fam = m.get("futural")
    assert fam is not None
    pm = font_thumbnail(fam)
    assert pm is not None and not pm.isNull()
    assert pm.width() > 10 and pm.height() > 5
    # 缩略图应有非透明像素（确实画了字）
    img = pm.toImage()
    opaque = sum(1 for y in range(img.height()) for x in range(img.width())
                 if img.pixelColor(x, y).alpha() > 0)
    assert opaque > 20


def test_font_thumbnail_white_background(qapp):
    """缩略图背景为白色，字迹为深色。"""
    from writerstudio.ui.font_panel import font_thumbnail
    m = FontManager(user_font_dir="/nonexistent-xyz")
    fam = m.get("futural")
    pm = font_thumbnail(fam)
    assert pm is not None
    img = pm.toImage()
    corner = img.pixelColor(0, 0)
    assert corner.red() > 240 and corner.green() > 240 and corner.blue() > 240
    dark = sum(1 for y in range(img.height()) for x in range(img.width())
               if img.pixelColor(x, y).lightness() < 128)
    assert dark > 10


def test_sample_chars_prefers_zh_latin_digits(qapp):
    """示例文字固定为「中文Aa123」（缺字跳过，全缺才退回任意字形）。"""
    from writerstudio.fonts.model import FontFamily, Glyph
    from writerstudio.ui.font_panel import _sample_chars
    glyphs = {ch: Glyph(ch, [], advance=50.0) for ch in "中文Aa123"}
    fam = FontFamily(name="t", kind="gfont", units_per_em=100.0, glyphs=glyphs)
    assert _sample_chars(fam) == list("中文Aa123")
    # 全缺时回退到任意字形
    glyphs2 = {ch: Glyph(ch, [], advance=50.0) for ch in "xyz"}
    fam2 = FontFamily(name="t2", kind="gfont", units_per_em=100.0, glyphs=glyphs2)
    picked = _sample_chars(fam2)
    assert picked and all(c in "xyz" for c in picked)


def test_load_preview_gfont_light(tmp_path):
    """gfont 轻量预览：只读示例字符条目、Y 翻转、统一字间距。"""
    import struct
    import zipfile

    from writerstudio.fonts.preview import load_preview

    def stroke(*pts: tuple[float, float]) -> bytes:
        # 单笔画：抬笔起点 + 落笔续画；坐标为原始 Y 向下
        body = struct.pack(f">{len(pts) * 2}f",
                           *[v for p in pts for v in p])
        return (struct.pack(">HI", 65, len(pts) * 2) + body
                + struct.pack(">I", len(pts)) + b"\x00" + b"\x01" * (len(pts) - 1))

    p = tmp_path / "preview.gfont"
    with zipfile.ZipFile(p, "w") as zf:
        # 「A」的墨迹只在原始坐标 y∈[-10,-5]（Y 向下=字形的上半部）：
        # 翻转后应落在 y∈[5,10]（Y 向上的上半部），颠倒即会落到下半部
        zf.writestr("65", stroke((0.0, -5.0), (10.0, -10.0)))       # A
        zf.writestr("49", stroke((0.0, -10.0), (2.0, 0.0)))         # 1
        zf.writestr("99999999999", b"\x00" * 16)                    # 非法码点：跳过
    pv = load_preview(p, "gfont")
    assert pv is not None
    assert pv.has("A") and pv.has("1")
    assert not pv.has("中")                # 字体里没有的字 → 不在预览里
    ys = [py for s in pv.glyph("A").strokes for _, py in s]
    assert min(ys) == pytest.approx(5.0)   # 翻转后墨迹在上半部（Y 向上）
    assert max(ys) == pytest.approx(10.0)
    # 字间距：统一间隙 = 最高字墨高 × 0.28，「1」与「A」的步距差 = 墨宽差
    adv_a, adv_1 = pv.glyph("A").advance, pv.glyph("1").advance
    assert adv_a - adv_1 == pytest.approx(10.0 - 2.0)
    assert adv_1 > 2.0                     # 窄字也有呼吸空间，不会挤在一起
    # 缺全部示例字符时返回 None（调用方退回全量加载）
    assert load_preview(p, "gfont", sample="字xyz") is None


def test_load_preview_truetype_lazy():
    """TrueType 轻量预览：惰性只 draw 示例字形（DejaVu 无中文则取拉丁部分）。"""
    pytest.importorskip("fontTools")
    import pathlib

    import matplotlib

    from writerstudio.fonts.preview import load_preview
    ttf = (pathlib.Path(matplotlib.get_data_path())
           / "fonts" / "ttf" / "DejaVuSans.ttf")
    if not ttf.exists():
        pytest.skip("无 matplotlib 自带字体")
    pv = load_preview(ttf, "truetype")
    assert pv is not None
    assert pv.has("A") and pv.has("1")
    assert pv.glyph("A").strokes
    assert pv.units_per_em > 0


def test_font_panel_search_and_filter(qapp):
    win = MainWindow()
    panel = win.font_panel
    total = panel.list.count()
    panel.search_edit.setText("futural")
    qapp.processEvents()
    assert panel.list.count() >= 1
    assert panel.list.count() < total
    found = [panel.list.item(i).data(Qt.UserRole) for i in range(panel.list.count())]
    assert "futural" in found
    panel.search_edit.clear()
    qapp.processEvents()
    # 类型过滤：只留 Hershey
    panel.kind_combo.setCurrentIndex(panel.kind_combo.findData("hershey"))
    qapp.processEvents()
    assert panel.list.count() > 0
    win.close()


# ===================================================== 画布坐标上报
def test_canvas_cursor_coordinate_tracks_mouse(qapp):
    """鼠标移动经 mouseMoved 信号上报页面坐标（状态栏右下角显示用）。"""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    win = MainWindow()
    win.resize(1000, 700)
    win.show()
    qapp.processEvents()
    c = win.canvas
    c.fit_page()
    qapp.processEvents()

    def ev(p):
        return QMouseEvent(QEvent.MouseMove, QPointF(p), QPointF(p),
                           Qt.NoButton, Qt.NoButton, Qt.NoModifier)

    page = win.controller.doc.page
    target = c.mapFromScene(QPointF(page.width / 2, page.height / 2))
    seen: list = []
    c.mouseMoved.connect(seen.append)
    c.mouseMoveEvent(ev(target))
    assert seen, "mouseMoved 信号未发出"
    pos = seen[-1]
    assert abs(pos.x() - page.width / 2) < 1.0
    assert abs(pos.y() - page.height / 2) < 1.0
    win.close()


def test_text_frame_survives_reopen_with_missing_font(qapp, tmp_path):
    """保存→重开（字体不可解析）：内容保留，文本框/调整框大小不变。

    回归：框几何只存于不序列化的 meta 时，重生成失败后画布会把调整框
    画成 10×4mm 兜底框——画布上看内容正常、框却完全对不上。
    """
    from writerstudio.fonts.builder import make_text_object
    from writerstudio.project import save_project

    win = MainWindow()
    doc = win.controller.doc
    t = make_text_object(TextSpec(text="Hello World", font_names=["futural"],
                                  size=12, frame_width=50.0), win.font_manager)
    doc.add(t)
    win.controller.set_document(doc)
    it0 = win.canvas._items[t.id]
    f0 = it0._frame_rect_or_extent()
    assert (f0.width(), f0.height()) == pytest.approx((50.0, 29.4))

    path = str(tmp_path / "t.wsproj")
    save_project(path, win._collect_project())
    orig_get = win.font_manager.get
    win.font_manager.get = lambda name, *a, **k: None   # 模拟字体丢失
    try:
        assert win._load_project_path(path)
    finally:
        win.font_manager.get = orig_get
    it1 = next(iter(win.canvas._items.values()))
    f1 = it1._frame_rect_or_extent()
    assert len(it1.model.local_strokes) > 0, "内容笔画应保留"
    assert (f1.left(), f1.top(), f1.width(), f1.height()) == \
        pytest.approx((f0.left(), f0.top(), f0.width(), f0.height()))
    win.close()
