"""文档对象模型。

设计要点（对应需求「写绘内容的自由编辑：自由移动、缩放」）：
    每个对象 = **源内容(source) + 生成笔画(local_strokes) + 变换矩阵(transform)**

    * ``source`` 记录「这段内容怎么来的」（文本/字体/字号/Markdown/LaTeX/SVG/自由笔画…），
      修改源内容后调用重新生成即可刷新笔画，而用户对对象做的移动/缩放/旋转不丢失。
    * ``local_strokes`` 是源内容生成的本地坐标笔画（mm）。
    * ``transform`` 把本地笔画映射到页面坐标（mm, Y 向上）。

本模块为纯 Python，不依赖 Qt。
"""

from __future__ import annotations

import copy
import itertools
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .geometry import AffineTransform, BBox, Vec2
from .reference import ReferenceItem
from .strokes import Stroke, strokes_bbox, total_length

_id_counter = itertools.count(1)


def _new_id(prefix: str = "obj") -> str:
    return f"{prefix}-{next(_id_counter):04d}"


# ---------------------------------------------------------------------------
# 源内容描述
# ---------------------------------------------------------------------------
@dataclass
class SourceSpec:
    """对象「源内容」的描述，用于之后重新生成笔画。

    kind 取值（后续阶段逐步实现）：
        ``text``     普通文本（data: text, font_id, size, ...）
        ``markdown`` Markdown 片段（含表格）
        ``latex``     LaTeX 公式/文档
        ``svg``       矢量图导入
        ``freehand``  自由手绘笔画
        ``static``    直接给定的静态笔画（P1 画布演示用）
    """

    kind: str = "static"
    data: dict[str, Any] = field(default_factory=dict)

    def clone(self) -> SourceSpec:
        # 深拷贝：data 可能含嵌套 dict/list（如 perturb 参数），浅拷贝会让
        # 复制出来的对象与原对象共享同一份参数（改一个影响另一个）。
        return SourceSpec(self.kind, copy.deepcopy(self.data))


# ---------------------------------------------------------------------------
# 文档对象
# ---------------------------------------------------------------------------
@dataclass
class DocumentObject:
    """页面上的一个可编辑对象。"""

    name: str = "对象"
    source: SourceSpec = field(default_factory=SourceSpec)
    local_strokes: list[Stroke] = field(default_factory=list)
    transform: AffineTransform = field(default_factory=AffineTransform.identity)
    visible: bool = True
    locked: bool = False
    id: str = field(default_factory=_new_id)
    # 框架无关的附加元数据（如最近一次排版的字符信息），不参与序列化
    meta: dict[str, Any] = field(default_factory=dict)

    def __setattr__(self, name, value) -> None:
        if name == "local_strokes":
            # 笔画内容版本号：视图层据此跳过未变更对象的路径重建
            # （每次编辑只重建受影响对象，全场景同步从 O(文档) 降到 O(改动)）
            object.__setattr__(self, "_strokes_rev",
                               getattr(self, "_strokes_rev", 0) + 1)
        super().__setattr__(name, value)

    @property
    def strokes_rev(self) -> int:
        """笔画内容版本号，``local_strokes`` 每次赋值 +1（就地改动用 :meth:`touch`）。"""
        return getattr(self, "_strokes_rev", 0)

    def touch(self) -> None:
        """就地修改笔画内容（如下标赋值）后手动递增版本号。"""
        object.__setattr__(self, "_strokes_rev",
                           getattr(self, "_strokes_rev", 0) + 1)

    # -- 几何 ---------------------------------------------------------------
    def world_strokes(self) -> list[Stroke]:
        """应用变换后的页面坐标笔画（新对象，不修改本地数据）。"""
        t = self.transform
        return [Stroke(t.apply_many(s.points), s.closed, s.role)
                for s in self.local_strokes]

    def local_bbox(self) -> BBox:
        return strokes_bbox(self.local_strokes)

    def world_bbox(self) -> BBox:
        return strokes_bbox(self.world_strokes())

    def center_local(self) -> Vec2:
        box = self.local_bbox()
        return box.center if not box.is_empty else (0.0, 0.0)

    def length(self) -> float:
        return total_length(self.local_strokes)

    # -- 变换操作（就地，撤销由上层负责） -----------------------------------
    def translate(self, dx: float, dy: float) -> None:
        self.transform = AffineTransform.translate(dx, dy) @ self.transform

    def scale_about_local(self, sx: float, sy: float, origin: Vec2) -> None:
        """以本地坐标中的 origin 为不动点缩放。"""
        self.transform = self.transform @ AffineTransform.scale_about(sx, sy, origin)

    def rotate_about_local(self, degrees: float, origin: Vec2) -> None:
        self.transform = self.transform @ AffineTransform.rotate_about(degrees, origin)

    def set_transform(self, transform: AffineTransform) -> None:
        self.transform = transform

    # -- 复制 ---------------------------------------------------------------
    def clone(self, *, with_id: bool = False) -> DocumentObject:
        new = DocumentObject(
            name=self.name,
            source=self.source.clone(),
            local_strokes=[s.clone() for s in self.local_strokes],
            transform=self.transform,
            visible=self.visible,
            locked=self.locked,
            id=self.id if with_id else _new_id(),
        )
        return new


# ---------------------------------------------------------------------------
# 页面 & 文档
# ---------------------------------------------------------------------------
@dataclass
class PageSpec:
    """纸张/工作区域（mm）。默认 A4 横向，Y 向上。"""

    width: float = 297.0
    height: float = 210.0
    margin: float = 10.0
    preset_name: str = ""       # 最近应用的纸张预设名（仅作显示/回填用）

    def bbox(self) -> BBox:
        return BBox(0.0, 0.0, self.width, self.height)

    def margin_bbox(self) -> BBox:
        """页边距内框（用于对齐参考线）。"""
        m = max(0.0, min(self.margin, self.width / 2.0, self.height / 2.0))
        return BBox(m, m, self.width - m, self.height - m)

    def rotated(self, direction: str) -> PageSpec:
        """返回旋转后的页面尺寸（90° 会交换宽高，180° 不变）。"""
        _, w, h = page_rotation(direction, self.width, self.height)
        return PageSpec(w, h, self.margin, self.preset_name)


# -- 纸张旋转 ---------------------------------------------------------------
ROTATE_CW = "cw"        # 顺时针 90°
ROTATE_CCW = "ccw"      # 逆时针 90°
ROTATE_180 = "180"

ROTATE_LABELS = {
    ROTATE_CW: "顺时针 90°",
    ROTATE_CCW: "逆时针 90°",
    ROTATE_180: "180°",
}


def page_rotation(direction: str, width: float, height: float
                  ) -> tuple[AffineTransform, float, float]:
    """纸张旋转的页面坐标变换与新的页面尺寸。

    把整张纸连同内容一起绕纸面中心旋转，并平移到新页面的左下角原点，因此
    纸上的相对版面保持不变、不会旋转出界。返回 ``(变换, 新宽, 新高)``：

    * 顺时针 90°：``(x, y) → (y, width - x)``，新尺寸 ``(height, width)``
    * 逆时针 90°：``(x, y) → (height - y, x)``，新尺寸 ``(height, width)``
    * 180°：``(x, y) → (width - x, height - y)``，尺寸不变
    """
    if direction == ROTATE_CW:
        return AffineTransform(0.0, -1.0, 1.0, 0.0, 0.0, width), height, width
    if direction == ROTATE_CCW:
        return AffineTransform(0.0, 1.0, -1.0, 0.0, height, 0.0), height, width
    if direction == ROTATE_180:
        return AffineTransform(-1.0, 0.0, 0.0, -1.0, width, height), width, height
    raise ValueError(f"未知旋转方向：{direction}")


@dataclass
class Document:
    """一个写字机作业文档：页面 + 若干对象 + 参考层。"""

    page: PageSpec = field(default_factory=PageSpec)
    objects: list[DocumentObject] = field(default_factory=list)
    references: list[ReferenceItem] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- 对象管理 -----------------------------------------------------------
    def add(self, obj: DocumentObject, index: Optional[int] = None) -> DocumentObject:
        if index is None:
            self.objects.append(obj)
        else:
            self.objects.insert(index, obj)
        return obj

    def remove(self, obj: DocumentObject) -> int:
        """移除并返回其原索引（供撤销恢复位置）。"""
        idx = self.objects.index(obj)
        self.objects.pop(idx)
        return idx

    def find(self, obj_id: str) -> Optional[DocumentObject]:
        for o in self.objects:
            if o.id == obj_id:
                return o
        return None

    def index_of(self, obj: DocumentObject) -> int:
        return self.objects.index(obj)

    def move_z(self, obj: DocumentObject, delta: int) -> bool:
        """图层顺序调整：delta>0 上移(更靠后绘制)。"""
        idx = self.objects.index(obj)
        new_idx = max(0, min(len(self.objects) - 1, idx + delta))
        if new_idx == idx:
            return False
        self.objects.pop(idx)
        self.objects.insert(new_idx, obj)
        return True

    # -- 参考层管理（不参与书写/导出） --------------------------------------
    def add_reference(self, ref: ReferenceItem,
                      index: Optional[int] = None) -> ReferenceItem:
        if index is None:
            self.references.append(ref)
        else:
            self.references.insert(index, ref)
        return ref

    def remove_reference(self, ref: ReferenceItem) -> int:
        idx = self.references.index(ref)
        self.references.pop(idx)
        return idx

    def find_reference(self, ref_id: str) -> Optional[ReferenceItem]:
        for r in self.references:
            if r.id == ref_id:
                return r
        return None

    def references_bbox(self, visible_only: bool = True) -> BBox:
        box = BBox()
        for r in self.references:
            if visible_only and not r.visible:
                continue
            box = box.union(r.world_bbox())
        return box

    # -- 查询 ---------------------------------------------------------------
    def bbox(self) -> BBox:
        box = self.page.bbox()
        for o in self.objects:
            if o.visible:
                box = box.union(o.world_bbox())
        return box

    def content_bbox(self) -> BBox:
        box = BBox()
        for o in self.objects:
            if o.visible:
                box = box.union(o.world_bbox())
        return box

    def total_length(self) -> float:
        return sum(o.length() for o in self.objects)

    def __iter__(self) -> Iterator[DocumentObject]:
        return iter(self.objects)

    def __len__(self) -> int:
        return len(self.objects)


# ---------------------------------------------------------------------------
# 笔画生成：把若干页面坐标笔画包成静态对象（P1 用；P2+ 由字体/公式模块替换）
# ---------------------------------------------------------------------------
def make_static_object(
    strokes: list[Stroke],
    *,
    name: str = "图形",
    transform: Optional[AffineTransform] = None,
    source_kind: str = "static",
    source_data: Optional[dict[str, Any]] = None,
) -> DocumentObject:
    """直接以页面坐标笔画构造对象（本地笔画即页面笔画，变换为单位阵）。"""
    return DocumentObject(
        name=name,
        source=SourceSpec(source_kind, dict(source_data or {})),
        local_strokes=[s.clone() for s in strokes],
        transform=transform or AffineTransform.identity(),
    )
