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
      "history_pool": [<对象/参考图数据>, ...],
      "history_refs": [{"text": "移动对象：…", "page": {...},
                        "objects": [0, 1, ...], "references": [...],
                        "metadata": {...}}, ...],
      "metadata": {...}
    }

``history_refs`` + ``history_pool`` 是**撤销历史日志**：第 k 项是第 k 条
撤销命令执行**前**的文档状态。相邻快照间绝大多数对象完全相同，因此把
对象数据抽进 ``history_pool`` 去重（相同内容只存一份），``history_refs``
按序引用池下标——否则每次小移动都会存一整份文档，文件随操作次数线性
膨胀（见 ``_history_pool_pack``）。打开项目时还原成逐条完整快照重放到
当前状态，撤销语义与旧版完全一致。
旧版的 ``"history": [{"text", "doc"}]`` 全量快照格式仍可读取（见
``project_from_data``）。
"""

from __future__ import annotations

import copy
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
    entries = [
        {"text": str(e.get("text", "")), "doc": e["doc"]}
        for e in project.history if isinstance(e, dict) and e.get("doc")
    ]
    if entries:
        packed, pool = _history_pool_pack(entries)
        data["history_refs"] = packed
        data["history_pool"] = pool
    return data


# ---------------------------------------------------------------------------
# 撤销历史的体积去重：对象池 + 池下标引用
# ---------------------------------------------------------------------------
def _history_pool_pack(entries: list[dict[str, Any]]
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把逐条全量快照的历史打包成 ``(history_refs, history_pool)``。

    撤销日志的相邻快照之间只有一两个对象不同（每次命令只动一步），
    全量存储会让文件随操作次数线性膨胀。这里把所有出现过的对象/参考图
    数据收进池（按内容去重，相同数据只存一份），每条快照只留池下标序列：

        history_refs[k] = {"text", "page", "objects": [池下标...],
                           "references": [池下标...], "metadata"}

    页面参数与元数据每条单独存（几十字节，不值得进池）。还原见
    :func:`_history_pool_unpack`。
    """
    pool: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def _ref(data: dict[str, Any]) -> int:
        key = json.dumps(data, ensure_ascii=False, sort_keys=True)
        i = index.get(key)
        if i is None:
            i = len(pool)
            index[key] = i
            pool.append(data)
        return i

    packed: list[dict[str, Any]] = []
    for e in entries:
        doc = e.get("doc") or {}
        packed.append({
            "text": str(e.get("text", "")),
            "page": doc.get("page", {}),
            "objects": [_ref(o) for o in doc.get("objects", [])],
            "references": [_ref(r) for r in doc.get("references", [])],
            "metadata": doc.get("metadata", {}),
        })
    return packed, pool


def _history_pool_unpack(packed: list[dict[str, Any]],
                         pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把池化历史还原成旧式全量快照（``[{"text", "doc"}, ...]``）。

    还原出的对象数据逐份深拷贝——历史各条目会长期驻留在撤销栈日志里，
    不能共享同一份可变字典（一条命令的重建若原地改动会串染其它条目）。
    池下标越界按缺失对象跳过（文件损坏时尽量少丢内容）。
    """
    n = len(pool)
    entries: list[dict[str, Any]] = []
    for e in packed:
        entries.append({
            "text": str(e.get("text", "")),
            "doc": {
                "page": copy.deepcopy(e.get("page", {})),
                "objects": [copy.deepcopy(pool[i])
                            for i in e.get("objects", [])
                            if isinstance(i, int) and 0 <= i < n],
                "references": [copy.deepcopy(pool[i])
                               for i in e.get("references", [])
                               if isinstance(i, int) and 0 <= i < n],
                "metadata": dict(e.get("metadata", {})),
            },
        })
    return entries


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
    # 撤销历史：新版为池化格式（history_refs + history_pool），
    # 旧版为逐条全量快照（history），两者都能读。
    # 池缺失/损坏时整体放弃历史——半份池会让撤销重放出空文档，
    # 比没有历史危险得多；文档本体不受影响。
    if isinstance(data.get("history_refs"), list):
        pool = data.get("history_pool")
        if isinstance(pool, list):
            try:
                history = _history_pool_unpack(
                    [e for e in data["history_refs"] if isinstance(e, dict)],
                    [o for o in pool if isinstance(o, dict)])
            except Exception:
                history = []
        else:
            history = []
    else:
        history = [e for e in data.get("history", [])
                   if isinstance(e, dict) and e.get("doc")]
    return ProjectData(
        document=doc,
        machine_config=GCodeConfig.from_data(mach.get("config")),
        start_point=StartPoint.from_data(mach.get("start_point")),
        search_dirs=list(fonts.get("search_dirs", [])),
        preferred_fonts=list(fonts.get("preferred", [])),
        metadata=dict(data.get("metadata", {})),
        history=history,
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
