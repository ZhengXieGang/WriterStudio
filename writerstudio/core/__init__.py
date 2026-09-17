"""核心数据层：几何、笔画、文档对象模型（纯 Python，无 Qt 依赖）。"""

from .document import (
    ROTATE_180,
    ROTATE_CCW,
    ROTATE_CW,
    ROTATE_LABELS,
    Document,
    DocumentObject,
    PageSpec,
    SourceSpec,
    make_static_object,
    page_rotation,
)
from .geometry import AffineTransform, BBox, Vec2
from .reference import (
    KIND_IMAGE,
    KIND_SVG,
    REFERENCE_KINDS,
    ReferenceItem,
    fit_reference_to_box,
)
from .sample import demo_document, make_polyline, make_rect, make_star
from .strokes import Stroke, strokes_bbox, total_length

__all__ = [
    "Vec2",
    "BBox",
    "AffineTransform",
    "Stroke",
    "strokes_bbox",
    "total_length",
    "Document",
    "DocumentObject",
    "PageSpec",
    "SourceSpec",
    "make_static_object",
    "page_rotation",
    "ROTATE_CW",
    "ROTATE_CCW",
    "ROTATE_180",
    "ROTATE_LABELS",
    "ReferenceItem",
    "KIND_IMAGE",
    "KIND_SVG",
    "REFERENCE_KINDS",
    "fit_reference_to_box",
    "make_polyline",
    "make_rect",
    "make_star",
    "demo_document",
]
