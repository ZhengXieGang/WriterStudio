"""画布显示缓存回归：缩放/平移不再每帧重画整条笔画路径。

背景：缩放/平移卡顿的根因是每个对象每帧都重新光栅化 QPainterPath
（抗锯齿 + 圆头圆角，一页文字 10ms+，密集图形上百 ms）。现在对象按当前
视图分辨率缓存一张位图：静态/平移时 1:1 贴图；缩放期间直接缩放旧图；
停手后由视图逐个按新分辨率重建（超密对象走后台线程）。
"""

from __future__ import annotations

import math
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.core.document import (  # noqa: E402
    Document,
    PageSpec,
    make_static_object,
)
from writerstudio.core.geometry import AffineTransform  # noqa: E402
from writerstudio.core.reference import ReferenceItem  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.core.strokes import Stroke  # noqa: E402
from writerstudio.ui import items as items_mod  # noqa: E402
from writerstudio.ui.canvas import CanvasView  # noqa: E402
from writerstudio.ui.controller import DocumentController  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _view(doc, w=800, h=600):
    v = CanvasView(DocumentController(doc))
    v.resize(w, h)
    v.show()
    QApplication.processEvents()
    v.fit_page()
    QApplication.processEvents()
    return v


def _item(v):
    return next(it for it in v.scene().items()
                if hasattr(it, "display_cache_ppm"))


def _simple_doc():
    doc = Document(page=PageSpec(width=297.0, height=210.0, margin=10.0))
    doc.add(make_static_object([make_rect(0.0, 0.0, 80.0, 40.0)],
                               name="rect"))
    return doc


def _dense_doc(n_strokes=120, n_points=120):
    doc = Document(page=PageSpec(width=297.0, height=210.0, margin=10.0))
    strokes = []
    for s in range(n_strokes):
        cx = 20.0 + (s % 30) * 8.0
        cy = 20.0 + (s // 30) * 20.0
        strokes.append(Stroke([(cx + 3.0 * math.sin(i * 0.2 + s), cy + i * 0.05)
                               for i in range(n_points)]))
    doc.add(make_static_object(strokes, name="dense"))
    return doc


def test_cache_is_built_at_view_resolution(qapp):
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        assert it._disp_pix is not None
        target = it.display_cache_target_ppm(
            v.current_zoom() * max(1.0, v.devicePixelRatioF()))
        # 缓存与视图 1:1：贴图时只缩不放，边缘始终清晰
        assert it.display_cache_ppm() == pytest.approx(target)
        assert it.display_cache_ppm() >= v.current_zoom() * 0.99
    finally:
        v.close()


def test_cached_item_is_not_rebuilt_every_frame(qapp):
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        calls = []
        orig = it._build_display_cache

        def counting(bbox, ppm):
            calls.append(ppm)
            return orig(bbox, ppm)

        it._build_display_cache = counting
        for _ in range(5):
            v.viewport().repaint()
        assert not calls, "已缓存的对象不应每帧重建位图"
    finally:
        v.close()


def test_zoom_reuses_old_bitmap_then_refreshes(qapp):
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        old_pix = it._disp_pix
        old_ppm = it.display_cache_ppm()
        assert old_pix is not None
        center = v.viewport().rect().center()
        v._zoom_at(center, 1.5)
        v.viewport().repaint()
        # 缩放过程中直接缩放旧位图（不重建）
        assert it._disp_pix is old_pix
        assert it.display_cache_ppm() == old_ppm
        # 停手后按新分辨率重建
        v._refresh_item_caches()
        v.viewport().repaint()
        target = it.display_cache_target_ppm(
            v.current_zoom() * max(1.0, v.devicePixelRatioF()))
        assert it.display_cache_ppm() == pytest.approx(target)
        assert it._disp_pix is not old_pix
    finally:
        v.close()


def test_content_change_invalidates_cache(qapp):
    doc = _simple_doc()
    v = _view(doc)
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        assert it._disp_pix is not None
        obj = doc.objects[0]
        obj.local_strokes = [make_rect(0.0, 0.0, 20.0, 10.0)]
        it.sync()
        assert it._disp_pix is None
        v.viewport().repaint()
        assert it._disp_pix is not None
    finally:
        v.close()


def test_cached_pixels_match_direct_rendering(qapp):
    """位图缓存不能翻转/错位：与直接矢量绘制逐像素比较（允许 AA 差异）。"""
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        cached = v.grab().toImage()
        it = _item(v)
        orig = it._build_display_cache
        it._disp_pix = None
        it._build_display_cache = lambda bbox, ppm: None
        v.viewport().repaint()
        direct = v.grab().toImage()
        it._build_display_cache = orig
        w, h = cached.width(), cached.height()
        total = 0
        n = 0
        for y in range(0, h, 5):
            for x in range(0, w, 5):
                a = cached.pixelColor(x, y)
                b = direct.pixelColor(x, y)
                total += (abs(a.red() - b.red()) + abs(a.green() - b.green())
                          + abs(a.blue() - b.blue()))
                n += 1
        assert total / n < 4.0, "缓存画面与矢量画面差异过大（可能翻转/错位）"
    finally:
        v.close()


def test_dense_object_renders_cache_off_main_thread(qapp):
    v = _view(_dense_doc())
    try:
        it = _item(v)
        assert it.display_cache_async_threshold() > 0
        assert it.display_element_count() >= it.display_cache_async_threshold()
        token = it.request_display_cache_async(v.current_zoom())
        result = None
        end = time.monotonic() + 5.0
        while time.monotonic() < end and result is None:
            QApplication.processEvents()
            result = items_mod.take_display_cache(token)
            time.sleep(0.005)
        assert result is not None
        image, ppm = result
        assert isinstance(image, QImage) and not image.isNull()
        it.apply_display_cache(image, ppm)
        assert it._disp_pix is not None
        assert it.display_cache_ppm() == pytest.approx(ppm)
    finally:
        v.close()


def test_reference_image_is_cached(qapp, tmp_path):
    img = QImage(200, 150, QImage.Format_RGB32)
    img.fill(QColor(200, 210, 220))
    path = tmp_path / "ref.png"
    assert img.save(str(path))
    doc = _simple_doc()
    doc.references.append(ReferenceItem(
        kind="image", path=str(path), width_mm=100.0, height_mm=75.0))
    v = _view(doc)
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        ref = next(it for it in v.scene().items()
                   if hasattr(it, "display_cache_ppm")
                   and it.model in doc.references)
        assert ref._disp_pix is not None
        assert ref.display_cache_ppm() > 0
    finally:
        v.close()


def _pump(ms: float = 1.5) -> None:
    end = time.monotonic() + ms
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def _visible_items(v):
    rect = v.mapToScene(v.viewport().rect()).boundingRect()
    return [it for it in v.scene().items()
            if hasattr(it, "display_cache_ppm") and it.isVisible()
            and it.sceneBoundingRect().intersects(rect)]


def test_zoom_out_reuses_larger_cache(qapp):
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        old_pix, old_ppm = it._disp_pix, it.display_cache_ppm()
        v._zoom_at(v.viewport().rect().center(), 1.0 / 1.8)
        v._refresh_item_caches()
        # 缩小后旧位图分辨率更高：直接缩小贴图即可，不重建
        assert it._disp_pix is old_pix
        assert it.display_cache_ppm() == old_ppm
    finally:
        v.close()


def test_undersampled_cache_is_drawn_without_smoothing(qapp):
    class FakePainter:
        def __init__(self):
            self.smooth = None
            self.drawn = False

        def save(self):
            pass

        def restore(self):
            pass

        def setRenderHint(self, hint, on):
            if hint == QPainter.SmoothPixmapTransform:
                self.smooth = on

        def drawPixmap(self, *args):
            self.drawn = True

    pix = QPixmap(10, 10)
    rect = QRectF(0, 0, 10, 10)
    # 缓存分辨率不足 → 关平滑（最近邻），保持硬边不发虚
    p = FakePainter()
    items_mod.TransformGraphicsItem._draw_pixmap_sharp(p, rect, pix, 1.0, 2.0)
    assert p.drawn and p.smooth is False
    # 缓存足够大（缩小贴图）→ 不动平滑设置
    p = FakePainter()
    items_mod.TransformGraphicsItem._draw_pixmap_sharp(p, rect, pix, 2.0, 1.0)
    assert p.drawn and p.smooth is None


def test_translation_keeps_display_cache(qapp):
    doc = _simple_doc()
    v = _view(doc)
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        assert it._disp_pix is not None
        obj = doc.objects[0]
        obj.transform = obj.transform @ AffineTransform.translate(18.0, 7.0)
        it.sync()
        # 纯平移不改位图内容，缓存应保留（避免每次移动后同步重光栅化）
        assert it._disp_pix is not None
    finally:
        v.close()


def test_move_drag_reuses_display_cache(qapp):
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        it._drag_kind = "move"
        it._drag_paint_hint_changed()
        it._ensure_fast_preview()
        assert it._fast_pix is None, "平移应直接复用显示缓存，不另建预览位图"
        assert it._disp_pix is not None
    finally:
        v.close()


def test_handle_drag_preview_supersamples(qapp):
    v = _view(_simple_doc())
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        it._drag_kind = "handle"
        it._drag_paint_hint_changed()
        it._ensure_fast_preview()
        assert it._fast_pix is not None
        ppm = it._fast_pix.width() / it._fast_rect.width()
        view_ppm = v.current_zoom() * max(1.0, v.devicePixelRatioF())
        # 预览位图超采样，放大对象拖动时先靠它顶住
        assert ppm >= view_ppm * 1.1
    finally:
        v.close()


def test_refresh_queue_converges_with_tiny_budget(qapp, monkeypatch):
    doc = Document(page=PageSpec(width=297.0, height=210.0, margin=10.0))
    for k in range(5):
        doc.add(make_static_object([make_rect(0.0, k * 20.0, 80.0, 10.0)],
                                   name=f"r{k}"))
    v = _view(doc)
    try:
        from writerstudio.ui import canvas as canvas_mod
        monkeypatch.setattr(canvas_mod, "CACHE_REFRESH_BUDGET_PX", 1)
        for it in _visible_items(v):
            it._disp_ppm = 0.0        # 全部标记为过期
        v._refresh_item_caches()
        _pump(2.5)
        view_ppm = v.current_zoom() * max(1.0, v.devicePixelRatioF())
        for it in _visible_items(v):
            assert it.display_cache_ppm() >= \
                it.display_cache_target_ppm(view_ppm) * 0.999
    finally:
        v.close()


def test_pan_schedules_cache_refresh(qapp):
    v = _view(_simple_doc())
    try:
        v._zoom_at(v.viewport().rect().center(), 2.0)
        _pump(0.6)
        v._cache_timer.stop()
        bar = v.horizontalScrollBar()
        bar.setValue(bar.value() + 20)
        assert v._cache_timer.isActive(), "平移后应安排可见对象的缓存补刷"
    finally:
        v.close()


def test_anisotropic_scale_uses_max_axis_resolution(qapp):
    doc = _simple_doc()
    obj = doc.objects[0]
    obj.transform = AffineTransform.scale_about(2.0, 0.35, (0.0, 0.0))
    v = _view(doc)
    try:
        v._refresh_item_caches()
        v.viewport().repaint()
        it = _item(v)
        view_ppm = v.current_zoom() * max(1.0, v.devicePixelRatioF())
        target = it.display_cache_target_ppm(view_ppm)
        # 非等比缩放：必须按被拉长的那一轴（2×）给分辨率，而不是
        # sqrt(|det|)≈0.84——否则长轴贴图时会被放大发虚
        assert target >= view_ppm * 2.0 * 0.99
        assert it.display_cache_ppm() == pytest.approx(target)
    finally:
        v.close()


def test_rotated_cache_keeps_smoothing_at_one_to_one(qapp):
    class FakePainter:
        def __init__(self):
            self.smooth = None
            self.drawn = False

        def save(self):
            pass

        def restore(self):
            pass

        def setRenderHint(self, hint, on):
            if hint == QPainter.SmoothPixmapTransform:
                self.smooth = on

        def drawPixmap(self, *args):
            self.drawn = True

    p = FakePainter()
    items_mod.TransformGraphicsItem._draw_pixmap_sharp(
        p, QRectF(0, 0, 10, 10), QPixmap(10, 10), 2.0, 2.0, rotated=True)
    # 旋转时最近邻会让字形边缘锯齿化，1:1 仍保留双线性
    assert p.drawn and p.smooth is None
