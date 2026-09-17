"""参考层对象模型。

参考层用于「对齐纸张」：用户可以放入扫描图 / 照片 / SVG 图纸作为底图，
**参考层永不参与 G-code 输出**，只用于视觉对齐（写字机不会画它）。

与普通对象一样，参考物也遵循「本地坐标 + 变换矩阵」的约定：

    * 本地坐标 = 图像自身的毫米坐标，范围 ``(0, 0, width_mm, height_mm)``；
    * ``transform`` 把本地矩形映射到页面坐标（mm，Y 向上）。

因此缩放/旋转/移动参考图与普通对象使用同一套交互逻辑。

本模块为纯 Python，不依赖 Qt；像素尺寸探测等 Qt 相关工作由界面层完成。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .geometry import AffineTransform, BBox, Vec2

_id_counter = itertools.count(1)

KIND_IMAGE = "image"
KIND_SVG = "svg"

REFERENCE_KINDS = {KIND_IMAGE: "图片", KIND_SVG: "SVG 矢量图"}


def new_reference_id() -> str:
    return f"ref-{next(_id_counter):04d}"


@dataclass
class ReferenceItem:
    """一个参考层对象（图片或 SVG），不参与书写。"""

    name: str = "参考图"
    kind: str = KIND_IMAGE
    path: str = ""
    width_mm: float = 100.0
    height_mm: float = 100.0
    transform: AffineTransform = field(default_factory=AffineTransform.identity)
    opacity: float = 0.45
    visible: bool = True
    locked: bool = False
    # 缩放时把被拖动的边/角自动吸附到纸张边缘（含页边距内框）
    snap_to_page: bool = True
    id: str = field(default_factory=new_reference_id)
    # 文件原始物理尺寸(mm)：0 表示与当前 width_mm/height_mm 相同。
    # 用于「原尺寸贴页」把一个按实际尺寸绘制的模板 1:1 对到页面上。
    natural_width_mm: float = 0.0
    natural_height_mm: float = 0.0

    # -- 几何 ---------------------------------------------------------------
    def local_bbox(self) -> BBox:
        return BBox(0.0, 0.0, max(0.0, self.width_mm), max(0.0, self.height_mm))

    def world_bbox(self) -> BBox:
        box = self.local_bbox()
        corners = [
            self.transform.apply((box.x0, box.y0)),
            self.transform.apply((box.x1, box.y0)),
            self.transform.apply((box.x1, box.y1)),
            self.transform.apply((box.x0, box.y1)),
        ]
        return BBox.from_points(corners)

    def center_local(self) -> Vec2:
        return (self.width_mm / 2.0, self.height_mm / 2.0)

    def world_center(self) -> Vec2:
        return self.transform.apply(self.center_local())

    def contains_local(self, p: Vec2) -> bool:
        x, y = p
        return 0.0 <= x <= self.width_mm and 0.0 <= y <= self.height_mm

    def aspect(self) -> float:
        if self.width_mm <= 1e-9:
            return 1.0
        return self.height_mm / self.width_mm

    def natural_size(self) -> tuple[float, float]:
        """文件原始物理尺寸(mm)；未记录时退回当前显示尺寸。"""
        return (self.natural_width_mm or self.width_mm,
                self.natural_height_mm or self.height_mm)

    def place_natural(self, origin: Vec2 = (0.0, 0.0)) -> None:
        """恢复文件原始尺寸并平移到 ``origin``（用于模板 1:1 贴页对齐）。"""
        w, h = self.natural_size()
        self.width_mm, self.height_mm = w, h
        self.transform = AffineTransform.translate(origin[0], origin[1])

    def set_width_mm(self, width: float, *, keep_aspect: bool = True) -> None:
        width = max(1e-6, float(width))
        ratio = self.aspect()
        self.width_mm = width
        if keep_aspect and ratio > 0:
            self.height_mm = width * ratio

    # -- 变换操作（就地，撤销由上层负责） -----------------------------------
    def translate(self, dx: float, dy: float) -> None:
        self.transform = AffineTransform.translate(dx, dy) @ self.transform

    def scale_about_local(self, sx: float, sy: float, origin: Vec2) -> None:
        self.transform = self.transform @ AffineTransform.scale_about(sx, sy, origin)

    def rotate_about_local(self, degrees: float, origin: Vec2) -> None:
        self.transform = self.transform @ AffineTransform.rotate_about(degrees, origin)

    def clone(self, *, with_id: bool = False) -> ReferenceItem:
        return ReferenceItem(
            name=self.name,
            kind=self.kind,
            path=self.path,
            width_mm=self.width_mm,
            height_mm=self.height_mm,
            transform=self.transform,
            opacity=self.opacity,
            visible=self.visible,
            locked=self.locked,
            snap_to_page=self.snap_to_page,
            id=self.id if with_id else new_reference_id(),
            natural_width_mm=self.natural_width_mm,
            natural_height_mm=self.natural_height_mm,
        )

    # -- 序列化 -------------------------------------------------------------
    def to_data(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "natural_width_mm": self.natural_width_mm,
            "natural_height_mm": self.natural_height_mm,
            "transform": list(self.transform.as_tuple()),
            "opacity": self.opacity,
            "visible": bool(self.visible),
            "locked": bool(self.locked),
            "snap_to_page": bool(self.snap_to_page),
        }

    @classmethod
    def from_data(cls, d: dict) -> ReferenceItem:
        t = d.get("transform", [1, 0, 0, 1, 0, 0])
        return cls(
            name=d.get("name", "参考图"),
            kind=d.get("kind", KIND_IMAGE),
            path=d.get("path", ""),
            width_mm=float(d.get("width_mm", 100.0)),
            height_mm=float(d.get("height_mm", 100.0)),
            transform=AffineTransform.from_sequence(t),
            opacity=float(d.get("opacity", 0.45)),
            visible=bool(d.get("visible", True)),
            locked=bool(d.get("locked", False)),
            snap_to_page=bool(d.get("snap_to_page", True)),
            natural_width_mm=float(d.get("natural_width_mm", 0.0)),
            natural_height_mm=float(d.get("natural_height_mm", 0.0)),
        )


def fit_reference_to_box(ref: ReferenceItem, box: BBox, *,
                         align: str = "center", margin: float = 0.0) -> None:
    """把参考图等比缩放并平移到 ``box`` 内（默认居中）。

    会同时更新 ``width_mm`` / ``height_mm``（保持比例）与 ``transform``。
    """
    if box.is_empty or ref.width_mm <= 0 or ref.height_mm <= 0:
        return
    avail_w = max(1e-6, box.width - 2 * margin)
    avail_h = max(1e-6, box.height - 2 * margin)
    scale = min(avail_w / ref.width_mm, avail_h / ref.height_mm)
    new_w = ref.width_mm * scale
    new_h = ref.height_mm * scale
    ref.width_mm, ref.height_mm = new_w, new_h
    if align == "center":
        cx = (box.x0 + box.x1) / 2.0
        cy = (box.y0 + box.y1) / 2.0
        ref.transform = AffineTransform.translate(cx - new_w / 2.0, cy - new_h / 2.0)
    elif align == "top-left":
        ref.transform = AffineTransform.translate(box.x0 + margin,
                                                  box.y1 - margin - new_h)
    else:  # bottom-left
        ref.transform = AffineTransform.translate(box.x0 + margin, box.y0 + margin)
