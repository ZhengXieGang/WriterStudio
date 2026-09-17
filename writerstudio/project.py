"""项目文件（``.wsproj``）序列化：文档 + 字体引用 + 机器配置。

设计原则：
    * **源内容优先**：每个对象的 ``source`` 都会保存，打开后可继续编辑/重新生成
      （换字体、调扰动）。
    * **笔画兜底**：同时保存 ``local_strokes`` 坐标，即使字体缺失也能正确显示；
      ``source`` 可再生成的对象在打开时会按源重新渲染（保持可编辑）。
    * 文件为 UTF-8 JSON，坐标保留 4 位小数以控制体积。

格式概览::

    {
      "format": "writerstudio-project",
      "version": 1,
      "page": {"width": ..., "height": ..., "margin": ..., "preset_name": ...},
      "objects": [
        {"name": ..., "source": {"kind": ..., "data": {...}},
         "transform": [a,b,c,d,e,f], "visible": true, "locked": false,
         "strokes": [[[x,y],...], ...], "closed": [bool, ...]}
      ],
      "references": [
        {"name": ..., "kind": "image"|"svg", "path": ...,
         "width_mm": ..., "height_mm": ..., "transform": [...],
         "opacity": ..., "visible": true, "locked": false}
      ],
      "machine": {"config": {...}, "start_point": {...}},
      "fonts": {"search_dirs": [...], "preferred": [...]},
      "history": [{"text": "移动对象：…", "doc": {…快照…}}, ...],
      "metadata": {...}
    }

``history`` 是**撤销历史日志**：第 k 项是第 k 条撤销命令执行**前**的文档
快照（``doc`` = page/objects/references）。打开项目时按日志重放到当前状态，
关闭程序前的操作仍可逐步回撤。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .core.document import Document, DocumentObject, PageSpec, SourceSpec
from .core.geometry import AffineTransform
from .core.reference import ReferenceItem
from .core.strokes import Stroke
from .machine.config import GCodeConfig
from .machine.start_point import StartPoint

FORMAT_ID = "writerstudio-project"
FORMAT_VERSION = 1
FILE_SUFFIX = ".wsproj"

# 可按 source 重新生成的对象类型（打开时会尝试重渲染）
REGENERABLE_KINDS = {"text", "markdown", "equation", "latex", "svg", "tikz"}

_COORD_DECIMALS = 4


@dataclass
class ProjectData:
    """一个项目文件承载的全部内容。"""

    document: Document = field(default_factory=Document)
    machine_config: GCodeConfig = field(default_factory=GCodeConfig)
    start_point: StartPoint = field(default_factory=StartPoint)
    search_dirs: list[str] = field(default_factory=list)
    preferred_fonts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # 撤销历史日志（每项 = 一条命令执行前的文档快照），见模块 docstring
    history: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
def _round(v: float) -> float:
    r = round(float(v), _COORD_DECIMALS)
    return 0.0 if r == 0 else r


def stroke_to_data(s: Stroke) -> dict:
    return {
        "pts": [[_round(x), _round(y)] for x, y in s.points],
        "closed": bool(s.closed),
    }


def stroke_from_data(d: dict) -> Stroke:
    pts = [(float(p[0]), float(p[1])) for p in d.get("pts", [])]
    return Stroke(pts, bool(d.get("closed", False)))


def object_to_data(obj: DocumentObject) -> dict:
    return {
        "name": obj.name,
        "source": {"kind": obj.source.kind, "data": obj.source.data},
        "transform": list(obj.transform.as_tuple()),
        "visible": bool(obj.visible),
        "locked": bool(obj.locked),
        "strokes": [stroke_to_data(s) for s in obj.local_strokes],
    }


def object_from_data(d: dict) -> DocumentObject:
    t = d.get("transform", [1, 0, 0, 1, 0, 0])
    src = d.get("source", {}) or {}
    obj = DocumentObject(
        name=d.get("name", "对象"),
        source=SourceSpec(src.get("kind", "static"), dict(src.get("data", {}))),
        local_strokes=[stroke_from_data(s) for s in d.get("strokes", [])],
        transform=AffineTransform.from_sequence(t),
        visible=bool(d.get("visible", True)),
        locked=bool(d.get("locked", False)),
    )
    return obj


def project_to_data(project: ProjectData) -> dict:
    doc = project.document
    data = {
        "format": FORMAT_ID,
        "version": FORMAT_VERSION,
        "doc": doc_to_data(doc),
        "machine": {
            "config": project.machine_config.to_data(),
            "start_point": project.start_point.to_data(),
        },
        "fonts": {
            "search_dirs": list(project.search_dirs),
            "preferred": list(project.preferred_fonts),
        },
        "metadata": dict(project.metadata),
    }
    if project.history:
        data["history"] = [
            {"text": str(e.get("text", "")), "doc": e["doc"]}
            for e in project.history if isinstance(e, dict) and e.get("doc")
        ]
    return data


def doc_to_data(doc: Document) -> dict:
    """文档（页面 + 对象 + 参考层 + 元数据）的快照——也是项目文件的主体。"""
    return {
        "page": {
            "width": doc.page.width,
            "height": doc.page.height,
            "margin": doc.page.margin,
            "preset_name": doc.page.preset_name,
        },
        "objects": [object_to_data(o) for o in doc.objects],
        "references": [r.to_data() for r in doc.references],
        "metadata": dict(doc.metadata),
    }


def doc_from_data(data: dict) -> Document:
    page_d = data.get("page", {})
    doc = Document(page=PageSpec(
        width=float(page_d.get("width", 297.0)),
        height=float(page_d.get("height", 210.0)),
        margin=float(page_d.get("margin", 10.0)),
        preset_name=str(page_d.get("preset_name", "") or ""),
    ))
    doc.metadata = dict(data.get("metadata", {}))
    # 单个对象损坏（坏点坐标/transform 长度不对等）只跳过该对象，
    # 不让整个项目打不开——与 references 的容错策略一致
    for od in data.get("objects", []):
        try:
            doc.add(object_from_data(od))
        except Exception:
            continue
    for rd in data.get("references", []):
        try:
            doc.add_reference(ReferenceItem.from_data(rd))
        except Exception:
            continue
    return doc


def apply_document_data(doc: Document, data: dict) -> None:
    """把快照**就地**应用到一个已有 Document（撤销历史重建用）。

    对象/参考图整体替换（新实例）；页面参数逐字段写入。异常逐项吞掉，
    保证重建过程不会半途抛出。
    """
    page_d = data.get("page", {})
    try:
        doc.page.width = float(page_d.get("width", doc.page.width))
        doc.page.height = float(page_d.get("height", doc.page.height))
        doc.page.margin = float(page_d.get("margin", doc.page.margin))
        doc.page.preset_name = str(page_d.get("preset_name", "") or "")
    except Exception:
        pass
    objects: list[DocumentObject] = []
    for od in data.get("objects", []):
        try:
            objects.append(object_from_data(od))
        except Exception:
            continue
    doc.objects = objects
    references: list[ReferenceItem] = []
    for rd in data.get("references", []):
        try:
            references.append(ReferenceItem.from_data(rd))
        except Exception:
            continue
    doc.references = references
    doc.metadata = dict(data.get("metadata", {}))


def project_from_data(data: dict) -> ProjectData:
    if data.get("format") != FORMAT_ID:
        raise ValueError("不是 WriterStudio 项目文件")
    ver = int(data.get("version", 0))
    if ver > FORMAT_VERSION:
        raise ValueError(f"项目版本 {ver} 高于本程序支持的 {FORMAT_VERSION}")

    # 新格式文档在 "doc" 键下；旧格式 page/objects/references 在顶层
    doc_d = data.get("doc")
    if not isinstance(doc_d, dict):
        doc_d = {"page": data.get("page", {}),
                 "objects": data.get("objects", []),
                 "references": data.get("references", []),
                 "metadata": data.get("metadata", {})}
    doc = doc_from_data(doc_d)

    mach = data.get("machine", {})
    fonts = data.get("fonts", {})
    return ProjectData(
        document=doc,
        machine_config=GCodeConfig.from_data(mach.get("config")),
        start_point=StartPoint.from_data(mach.get("start_point")),
        search_dirs=list(fonts.get("search_dirs", [])),
        preferred_fonts=list(fonts.get("preferred", [])),
        metadata=dict(data.get("metadata", {})),
        history=[e for e in data.get("history", [])
                 if isinstance(e, dict) and e.get("doc")],
    )


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
def save_project(path: str | Path, project: ProjectData,
                 *, indent: Optional[int] = 1) -> None:
    path = Path(path)
    if path.suffix.lower() != FILE_SUFFIX:
        path = path.with_suffix(FILE_SUFFIX)
    data = project_to_data(project)
    text = json.dumps(data, ensure_ascii=False, indent=indent,
                      separators=(",", ":") if indent is None else None)
    path.write_text(text, encoding="utf-8")


def load_project(path: str | Path) -> ProjectData:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return project_from_data(raw)


# ---------------------------------------------------------------------------
# 打开后按源重新生成（字体可用时让对象保持可编辑）
# ---------------------------------------------------------------------------
def _restore_render_state(obj, meta: dict, boxes: dict) -> None:
    """回滚重生成时一并恢复 meta 与持久化的框几何。

    重新生成可能已把 ``meta["layout"]`` 等覆盖成本次（失败）尝试的结果，
    只回滚笔画会让画布拿到「空排版」去画文本框——塌成兜底大小。
    """
    obj.meta.clear()
    obj.meta.update(meta)
    for key, value in boxes.items():
        if value is None:
            obj.source.data.pop(key, None)
        else:
            obj.source.data[key] = value


def regenerate_all(project: ProjectData, font_manager) -> int:
    """对可再生成的对象按源重渲染；返回成功重新生成的数量。

    失败（缺字体/工具链等）时**保留已存储的笔画**，不破坏内容。
    注意：某些重生成会「成功但产出空笔画」（例如字体链完全无法解析），
    此时也必须保留原笔画，否则会静默清空内容。
    """
    from .content.builder import regenerate_content_object
    from .fonts.builder import regenerate_text_object

    count = 0
    for obj in project.document.objects:
        kind = obj.source.kind
        before = obj.local_strokes
        before_meta = dict(obj.meta)
        before_boxes = {k: obj.source.data.get(k)
                        for k in ("layout_box", "table_box")}
        try:
            if kind == "text":
                ok = regenerate_text_object(obj, font_manager)
            elif kind in ("markdown", "equation", "latex", "svg", "tikz"):
                ok = regenerate_content_object(obj, font_manager)
            else:
                continue
        except Exception:
            obj.local_strokes = before
            _restore_render_state(obj, before_meta, before_boxes)
            continue
        if not ok or not obj.local_strokes:
            obj.local_strokes = before      # 回滚，避免清空内容
            _restore_render_state(obj, before_meta, before_boxes)
            continue
        count += 1
    return count
