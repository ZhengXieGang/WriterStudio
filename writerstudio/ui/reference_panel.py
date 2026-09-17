"""参考层面板：添加图片/SVG 作为对齐底图。

参考层**不会被书写，也不会导出到 G-code**，只用于把纸上的实际内容与
屏幕上的排版对齐（例如把一张扫描好的表格照片垫在下面，再把文字摆正）。

面板功能：添加图片 / 添加 SVG / 适应页面 / 居中 / 显示隐藏 / 锁定 / 透明度 / 删除。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDockWidget,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.geometry import AffineTransform, BBox
from ..core.reference import KIND_IMAGE, KIND_SVG, REFERENCE_KINDS, ReferenceItem
from .controller import DocumentController
from .style import unify_inputs

_ALIGN_LABELS = {"fit": "适应页边距", "center": "页面居中"}


def current_angle(t: AffineTransform) -> float:
    """从仿射矩阵提取旋转角（度）。"""
    import math
    return math.degrees(math.atan2(t.b, t.a))


class ReferencePanel(QDockWidget):
    """参考层管理面板。"""

    addImageRequested = Signal()
    addSvgRequested = Signal()

    def __init__(self, controller: DocumentController,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("参考层", parent)
        self.controller = controller
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._loading = False
        self._cached_ids: list[str] = []

        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(6, 6, 6, 6)

        self.list = QListWidget()
        self.list.setMinimumHeight(80)
        self.list.itemSelectionChanged.connect(self._on_selection)
        self.list.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list, 1)

        # 透明度
        row = QHBoxLayout()
        row.addWidget(QLabel("透明度"))
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(5, 100)
        self.opacity.setValue(45)
        self.opacity.valueChanged.connect(self._on_opacity)
        row.addWidget(self.opacity, 1)
        self.opacity_label = QLabel("45%")
        row.addWidget(self.opacity_label)
        root.addLayout(row)

        # 旋转角度（精确对齐扫描件）
        arow = QHBoxLayout()
        arow.addWidget(QLabel("旋转"))
        self.angle_spin = QDoubleSpinBox()
        self.angle_spin.setRange(-180.0, 180.0)
        self.angle_spin.setDecimals(2)
        self.angle_spin.setSingleStep(0.5)
        self.angle_spin.setSuffix(" °")
        self.angle_spin.setWrapping(False)
        self.angle_spin.editingFinished.connect(self._on_angle)
        self.angle_spin.valueChanged.connect(self._on_angle)
        arow.addWidget(self.angle_spin, 1)
        reset = QPushButton("归零")
        reset.clicked.connect(lambda: self.angle_spin.setValue(0.0))
        arow.addWidget(reset)
        root.addLayout(arow)

        self.lock_check = QCheckBox("锁定（不可拖动/缩放）")
        self.lock_check.toggled.connect(self._on_lock)
        root.addWidget(self.lock_check)

        self.snap_resize_check = QCheckBox("缩放时吸附纸张边缘")
        self.snap_resize_check.setToolTip(
            "拖动参考图手柄缩放时，被拖动的边/角靠近纸张边缘或页边距线\n"
            "会自动吸附上去；按住 Shift 等比缩放时不吸附。")
        self.snap_resize_check.toggled.connect(self._on_snap_resize)
        root.addWidget(self.snap_resize_check)

        # 操作按钮
        grid = QHBoxLayout()
        add_img = QPushButton("添加图片…")
        add_img.clicked.connect(self.addImageRequested.emit)
        add_svg = QPushButton("添加 SVG…")
        add_svg.clicked.connect(self.addSvgRequested.emit)
        grid.addWidget(add_img)
        grid.addWidget(add_svg)
        root.addLayout(grid)

        grid2 = QHBoxLayout()
        fit = QPushButton("适应页边距")
        fit.clicked.connect(lambda: self._fit("fit"))
        center = QPushButton("页面居中")
        center.clicked.connect(lambda: self._fit("center"))
        rm = QPushButton("删除")
        rm.clicked.connect(self._delete)
        grid2.addWidget(fit)
        grid2.addWidget(center)
        grid2.addWidget(rm)
        root.addLayout(grid2)

        grid3 = QHBoxLayout()
        snap_tl = QPushButton("贴左上角")
        snap_tl.clicked.connect(lambda: self._snap("top-left"))
        snap_c = QPushButton("贴页中心")
        snap_c.clicked.connect(lambda: self._snap("center"))
        grid3.addWidget(snap_tl)
        grid3.addWidget(snap_c)
        root.addLayout(grid3)

        original = QPushButton("原尺寸贴页（模板 1:1）")
        original.setToolTip("恢复到文件原始物理尺寸，并把左上角对齐页面原点，"
                            "用于按实际尺寸绘制的对齐模板")
        original.clicked.connect(self._place_natural)
        root.addWidget(original)

        self.setWidget(body)
        self.refresh()
        unify_inputs(self)

    # --------------------------------------------------------------- 列表
    def refresh(self) -> None:
        """重建列表（用于参考层增删等结构性变化）。"""
        before = self._selected_ids()
        self._rebuild()
        self._restore_selection(before)

    def refresh_if_changed(self) -> None:
        """仅当参考层集合发生变化时重建，否则只做轻量回显。

        画布拖动、透明度调整等都会触发 ``documentChanged``，若每次都重建列表，
        用户的选择会被清空（随后「删除/适应」等按钮对空选择无效）。
        """
        ids = [r.id for r in self.controller.doc.references]
        if self._cached_ids == ids:
            self._sync_rows()
            return
        before = self._selected_ids()
        self._rebuild()
        self._restore_selection(before)

    def _selected_ids(self) -> list[str]:        return [it.data(Qt.UserRole) for it in self.list.selectedItems()]

    def _restore_selection(self, ids) -> None:
        want = set(ids)
        self._loading = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.UserRole) in want:
                it.setSelected(True)
        self._loading = False
        self._on_selection()

    def _rebuild(self) -> None:
        self._loading = True
        self.list.clear()
        for ref in self.controller.doc.references:
            size = f"{ref.width_mm:.0f}×{ref.height_mm:.0f}mm"
            item = QListWidgetItem(
                f"{ref.name}  [{REFERENCE_KINDS.get(ref.kind, ref.kind)}]  {size}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if ref.visible else Qt.Unchecked)
            item.setData(Qt.UserRole, ref.id)
            self.list.addItem(item)
        self._cached_ids = [r.id for r in self.controller.doc.references]
        self._loading = False

    def _sync_rows(self) -> None:
        """集合未变时，只更新勾选状态等显示内容。"""
        self._loading = True
        for i in range(self.list.count()):
            it = self.list.item(i)
            ref = self.controller.doc.find_reference(it.data(Qt.UserRole))
            if ref is None:
                continue
            want = Qt.Checked if ref.visible else Qt.Unchecked
            if it.checkState() != want:
                it.setCheckState(want)
            size = f"{ref.width_mm:.0f}×{ref.height_mm:.0f}mm"
            it.setText(f"{ref.name}  [{REFERENCE_KINDS.get(ref.kind, ref.kind)}]  {size}")
        self._loading = False

    def selected_refs(self) -> list[ReferenceItem]:
        out = []
        for it in self.list.selectedItems():
            ref = self.controller.doc.find_reference(it.data(Qt.UserRole))
            if ref is not None:
                out.append(ref)
        return out

    def _on_selection(self) -> None:
        refs = self.selected_refs()
        has = bool(refs)
        self.opacity.setEnabled(has)
        self.lock_check.setEnabled(has)
        self.angle_spin.setEnabled(has)
        self.snap_resize_check.setEnabled(has)
        if not refs:
            return
        ref = refs[0]
        self._loading = True
        self.opacity.setValue(int(round(ref.opacity * 100)))
        self.opacity_label.setText(f"{int(round(ref.opacity * 100))}%")
        self.lock_check.setChecked(ref.locked)
        self.snap_resize_check.setChecked(ref.snap_to_page)
        self.angle_spin.setValue(current_angle(ref.transform))
        self._loading = False

    def _on_angle(self) -> None:
        """把参考图旋转到指定角度（绝对值，非增量），走撤销栈。"""
        if self._loading:
            return
        want = self.angle_spin.value()
        for ref in self.selected_refs():
            cur = current_angle(ref.transform)
            d = want - cur
            if abs(d) < 1e-9:
                continue
            # 绕图自身中心旋转
            local_c = ref.center_local()
            new_t = ref.transform @ AffineTransform.rotate_about(d, local_c)
            self.controller.edit_reference(ref, "旋转参考图",
                                           {"transform": new_t})

    def _snap(self, kind: str) -> None:
        """把参考图贴到页面角/中心（保持尺寸，仅平移）。"""
        refs = self.selected_refs()
        if not refs:
            return
        page = self.controller.doc.page
        target = page.bbox() if kind == "center" else page.margin_bbox()
        for ref in refs:
            b = ref.world_bbox()
            if b.is_empty:
                continue
            if kind == "center":
                dx = target.center[0] - b.center[0]
                dy = target.center[1] - b.center[1]
                label = "居中参考图"
            else:  # top-left（Y 向上 → 左上角是 x0, y1）
                dx = target.x0 - b.x0
                dy = target.y1 - b.y1
                label = "贴左上角"
            self.controller.edit_reference(
                ref, label,
                {"transform": AffineTransform.translate(dx, dy) @ ref.transform})

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        ref = self.controller.doc.find_reference(item.data(Qt.UserRole))
        if ref is None:
            return
        want = item.checkState() == Qt.Checked
        if want != ref.visible:
            self.controller.edit_reference(
                ref, "显示参考图" if want else "隐藏参考图", {"visible": want})

    def _on_opacity(self, value: int) -> None:
        if self._loading:
            return
        self.opacity_label.setText(f"{value}%")
        for ref in self.selected_refs():
            # 透明度属于显示微调，直接写入模型（不占撤销栈）
            ref.opacity = value / 100.0
        self.controller.notify()

    def _on_lock(self, checked: bool) -> None:
        if self._loading:
            return
        for ref in self.selected_refs():
            self.controller.edit_reference(
                ref, "锁定参考图" if checked else "解锁参考图",
                {"locked": bool(checked)})

    def _on_snap_resize(self, checked: bool) -> None:
        if self._loading:
            return
        for ref in self.selected_refs():
            self.controller.edit_reference(
                ref, "缩放吸附纸张边缘" if checked else "关闭缩放吸附",
                {"snap_to_page": bool(checked)})

    # --------------------------------------------------------------- 操作
    def _fit(self, align: str) -> None:
        refs = self.selected_refs()
        if not refs:
            return
        page = self.controller.doc.page
        box = page.margin_bbox() if align == "fit" else page.bbox()
        for ref in refs:
            changes = _fit_values(ref, box)
            self.controller.edit_reference(ref, _ALIGN_LABELS[align], changes)

    def _delete(self) -> None:
        refs = self.selected_refs()
        if refs:
            self.controller.remove_references(refs)

    def _place_natural(self) -> None:
        """恢复文件原始物理尺寸并对齐到页面左下角原点（模板 1:1 对页）。"""
        refs = self.selected_refs()
        if not refs:
            return
        for ref in refs:
            w, h = ref.natural_size()
            self.controller.edit_reference(
                ref, "原尺寸贴页",
                {"width_mm": w, "height_mm": h,
                 "transform": AffineTransform.identity()})
        self.controller.notify()

    def select_ref(self, ref: ReferenceItem) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.data(Qt.UserRole) == ref.id:
                self.list.blockSignals(True)
                self.list.setCurrentItem(it)
                self.list.blockSignals(False)
                # blockSignals 挡掉了 itemSelectionChanged：手动同步一次
                # 控件值，否则滑条/数值框仍是上一个参考图的值，
                # 下一次拖动会把旧值写进新选中的参考图
                self._on_selection()
                break


def _fit_values(ref: ReferenceItem, box: BBox, margin: float = 2.0) -> dict:
    """计算把参考图等比放入 box 所需的新尺寸与变换（不修改 ref）。"""
    tmp = ref.clone(with_id=True)
    from ..core.reference import fit_reference_to_box
    fit_reference_to_box(tmp, box, align="center", margin=margin)
    return {"width_mm": tmp.width_mm, "height_mm": tmp.height_mm,
            "transform": tmp.transform}


def guess_reference_name(path: str) -> str:
    return f"参考图：{Path(path).name}"


def make_reference_from_file(path: str, *, kind: Optional[str] = None) -> ReferenceItem:
    """根据文件创建参考图（读取原始物理尺寸；失败时给出默认尺寸）。

    原始尺寸同时记录到 ``natural_*``，供「原尺寸贴页」把按实际尺寸绘制的
    模板 1:1 对到页面上。
    """
    p = Path(path)
    kind = kind or (KIND_SVG if p.suffix.lower() in (".svg", ".svgz") else KIND_IMAGE)
    name = guess_reference_name(path)

    if kind == KIND_SVG:
        w, h = _svg_size(p)
    else:
        w, h = _image_size(p)
    return ReferenceItem(name=name, kind=kind, path=str(p.resolve()),
                         width_mm=w, height_mm=h,
                         natural_width_mm=w, natural_height_mm=h)


_UNIT_MM = {
    "mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0, "pc": 25.4 / 6.0,
    "px": 25.4 / 96.0, "": 25.4 / 96.0,
}


def _length_to_mm(text: Optional[str]) -> Optional[float]:
    """把 SVG 的长度字符串（如 ``210mm``/``8.27in``/``800``）换算为 mm。"""
    if not text:
        return None
    t = str(text).strip().lower()
    if t.endswith("%"):
        return None
    m = re.match(r"^([0-9.+\-eE]+)\s*([a-z]*)$", t)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    factor = _UNIT_MM.get(unit)
    if factor is None:
        return None
    return val * factor


def _svg_size(path: Path) -> tuple[float, float]:
    """读取 SVG 的物理尺寸(mm)。

    注意：Qt 的 ``QSvgRenderer`` 对 mm/cm/in 等物理单位按 **90dpi** 换算成像素，
    与 CSS 的 96dpi 不同，直接按 96dpi 反算会小约 6%。因此这里优先解析 SVG 根
    节点的 ``width``/``height``（含单位）得到真实毫米尺寸；缺失时再退回 viewBox
    与 Qt（按 96dpi 视作像素）。
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(str(path)).getroot()
        w = _length_to_mm(root.get("width"))
        h = _length_to_mm(root.get("height"))
        if w and h and w > 0 and h > 0:
            return (w, h)
        # 有 viewBox 但无物理单位 → 按 96dpi 视作像素
        vb = root.get("viewBox")
        if vb:
            parts = re.split(r"[\s,]+", vb.strip())
            if len(parts) == 4:
                vw, vh = float(parts[2]), float(parts[3])
                if vw > 0 and vh > 0:
                    return (vw * 25.4 / 96.0, vh * 25.4 / 96.0)
    except Exception:
        pass
    try:
        from PySide6.QtSvg import QSvgRenderer
        r = QSvgRenderer(str(path))
        size = r.defaultSize()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            return (size.width() * 25.4 / 96.0, size.height() * 25.4 / 96.0)
    except Exception:
        pass
    return (100.0, 100.0)


def _image_size(path: Path) -> tuple[float, float]:
    """返回图片的物理尺寸(mm)。

    优先用图片内嵌 DPI（扫描件常带 300dpi，可还原真实纸张大小）；
    注意 ``QImageReader`` **没有** dotsPerMeter 接口，需读入 ``QImage`` 后查询。
    """
    try:
        from PySide6.QtGui import QImageReader
        r = QImageReader(str(path))
        size = r.size()
        if not size.isValid() or size.width() <= 0 or size.height() <= 0:
            return (100.0, 100.0)
        dpi = 96.0
        try:
            # 缩到极小尺寸再读，既能拿到内嵌 DPI 又不会为超大扫描件吃内存
            probe = QImageReader(str(path))
            probe.setScaledSize(QSize(8, 8))
            img = probe.read()
            dpm = img.dotsPerMeterX() if not img.isNull() else 0
            if dpm and dpm > 1000:
                dpi = dpm * 0.0254
        except Exception:
            pass
        if dpi < 40 or dpi > 2400:
            dpi = 96.0
        return (size.width() * 25.4 / dpi, size.height() * 25.4 / dpi)
    except Exception:
        return (100.0, 100.0)


__all__ = [
    "ReferencePanel",
    "make_reference_from_file",
    "guess_reference_name",
    "KIND_IMAGE",
    "KIND_SVG",
]
