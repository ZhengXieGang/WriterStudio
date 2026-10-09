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
膨胀（见 ``_history_pool_pack``）。

**内存里也保持这个池化形态**（:class:`SnapshotPool`），池对象以紧凑 JSON
文本存放：旧实现打开项目时把池展开成逐条完整快照，5.58 MB 的项目文件
光历史就常驻 113 MB，编辑时每步再追加一份全量快照。展开是**用的时候才做**
的（:func:`history_entry_doc`），撤销/重放时会得到全新对象，绝无共享可变
结构之虞。旧版的 ``"history": [{"text", "doc"}]`` 全量快照格式仍可读取。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

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
    # 撤销历史日志（每项 = 一条命令执行前的文档快照），见模块 docstring。
    # 条目有两种形态：池化条目（``objects``/``references`` 存池下标）与旧式
    # 全量快照（``doc``）——后者只出现在旧文件与历史重建路径里。
    history: list[dict[str, Any]] = field(default_factory=list)
    #: 池化历史的对象池（紧凑 JSON 文本，按内容去重），与 ``history`` 配套
    history_pool: list[str] = field(default_factory=list)


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
    entries = [e for e in project.history if isinstance(e, dict)]
    if entries:
        if any("doc" in e for e in entries):
            # 旧式全量快照（历史重建/外部构造）：打包成池化条目
            packed, pool = _history_pool_pack(
                [{"text": str(e.get("text", "")), "doc": e["doc"]}
                 for e in entries if e.get("doc")])
            data["history_refs"] = packed
            data["history_pool"] = pool
        else:
            # 池化条目（运行期日志/刚读入的文件）：池里是 JSON 文本，
            # 写出时才解析成对象——内存里始终只留紧凑文本。
            # 池可能有已释放的空洞（日志裁掉旧条目后留下的），写出前压实并
            # 同步重编条目下标。
            texts, remap = _compact_pool(project.history_pool)
            data["history_refs"] = [_remap_entry(e, remap) for e in entries]
            data["history_pool"] = history_pool_to_data(texts)
    return data


def _compact_pool(pool: Sequence[Optional[str]]
                  ) -> tuple[list[str], dict[int, int]]:
    """压实带空洞的对象池，返回 ``(紧凑文本列表, 旧下标→新下标)``。"""
    texts: list[str] = []
    remap: dict[int, int] = {}
    for i, text in enumerate(pool):
        if text is not None:
            remap[i] = len(texts)
            texts.append(text)
    return texts, remap


def _remap_entry(entry: dict[str, Any], remap: dict[int, int]) -> dict[str, Any]:
    """按下标映射复制一条历史条目（不改动原条目——撤销栈还在用它）。"""
    out = dict(entry)
    for key in ("objects", "references"):
        out[key] = [remap[i] for i in entry.get(key) or [] if i in remap]
    return out


# ---------------------------------------------------------------------------
# 撤销历史：对象池（内容去重）+ 池下标引用
#
# 相邻两步快照之间绝大多数对象完全没变。逐条存整份文档会让内存与文件都随
# 编辑步数线性膨胀（实测 59 步 × 5.1 MB ≈ 300 MB 常驻），因此历史一律是
# 「条目 + 池」两段式：
#
#     entry = {"text", "page", "objects": [池下标...],
#              "references": [池下标...], "metadata"}
#     pool  = 每个对象内容一份，按内容去重
#
# 池里存的是**紧凑 JSON 文本**而不是可变 dict：体积比 Python 对象小一个
# 数量级（同一份 0.72 MB 文档：对象图 ≈ 5 MB，文本 ≈ 0.7 MB），而且天然
# 只读——取用时 ``json.loads`` 得到全新对象，绝不会与文档里的活对象共享
# 可变结构（旧实现逐条深拷贝整份快照，正是内存暴增的来源）。
# ---------------------------------------------------------------------------
def compact_dumps(value: Any) -> str:
    """紧凑 JSON 文本（去重键与池内存储都用它）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class SnapshotPool:
    """按内容去重的快照对象池（内存形态 = 文件里的 ``history_pool``）。"""

    __slots__ = ("_items", "_index")

    def __init__(self, items: Optional[Sequence[str]] = None) -> None:
        self._items: list[Optional[str]] = list(items or ())
        self._index: dict[str, int] = {t: i for i, t in enumerate(self._items)
                                       if t is not None}

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[Optional[str]]:
        return self._items

    def ref(self, data: dict[str, Any]) -> int:
        """把一个对象/参考图数据登记进池（内容相同只存一份），返回池下标。"""
        text = compact_dumps(data)
        i = self._index.get(text)
        if i is None:
            i = len(self._items)
            self._index[text] = i
            self._items.append(text)
        return i

    def release(self, entries: list[dict[str, Any]]) -> int:
        """释放不再被任何历史条目引用的池对象（日志裁掉旧条目后调用）。

        只把槽位清成 ``None``，**不重编号**：历史条目里存的是池下标，重编号
        就得把所有持有者（日志条目 + 撤销栈里的重建命令，它们可能共享同一个
        条目对象）统统改写一遍，改漏或改两遍都会让撤销恢复到错误状态。
        留洞只多几个空指针，返回释放的条目数。
        """
        keep = {i for e in entries
                for i in (e.get("objects") or []) + (e.get("references") or [])
                if isinstance(i, int)}
        freed = 0
        for i, text in enumerate(self._items):
            if text is not None and i not in keep:
                self._items[i] = None
                self._index.pop(text, None)
                freed += 1
        return freed


def page_to_data(page: PageSpec) -> dict[str, Any]:
    return {
        "width": page.width,
        "height": page.height,
        "margin": page.margin,
        "preset_name": page.preset_name,
    }


def snapshot_entry(doc: Document, pool: SnapshotPool) -> dict[str, Any]:
    """当前文档的一条**池化**快照（撤销日志用；不复制整份文档）。"""
    return {
        "page": page_to_data(doc.page),
        "objects": [pool.ref(object_to_data(o)) for o in doc.objects],
        "references": [pool.ref(r.to_data()) for r in doc.references],
        "metadata": dict(doc.metadata),
    }


def history_entry_doc(entry: dict[str, Any],
                      pool: Sequence[Optional[str]]) -> dict[str, Any]:
    """把一条历史条目展开成完整文档快照（每次调用都产出全新对象）。

    旧版 ``{"text", "doc"}`` 全量快照条目直接返回其中的 ``doc``（老文件与
    历史重建路径仍在用），池化条目按池下标取对象；下标越界或已释放的槽位
    跳过（损坏/异常时尽量少丢内容，与读文件时的容错一致）。
    """
    doc = entry.get("doc")
    if isinstance(doc, dict):
        return doc
    out: dict[str, Any] = {
        "page": entry.get("page") or {},
        "objects": [],
        "references": [],
        "metadata": dict(entry.get("metadata") or {}),
    }
    for key, target in (("objects", out["objects"]), ("references", out["references"])):
        for i in entry.get(key) or []:
            if isinstance(i, int) and 0 <= i < len(pool) and pool[i] is not None:
                target.append(json.loads(pool[i]))
    return out


def history_pool_to_data(pool: Sequence[str]) -> list[dict[str, Any]]:
    """池（JSON 文本）→ 文件里的 ``history_pool``（对象列表）。"""
    return [json.loads(t) for t in pool]


def history_pool_from_data(pool_data: Sequence[Any]) -> list[str]:
    """文件里的 ``history_pool``（对象列表）→ 池（JSON 文本）。"""
    return [compact_dumps(o) for o in pool_data if isinstance(o, dict)]


# ---------------------------------------------------------------------------
# 旧式全量快照 → 池化条目（历史重建路径与旧调用方用）
# ---------------------------------------------------------------------------
def _history_pool_pack(entries: list[dict[str, Any]]
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把逐条全量快照的历史打包成 ``(history_refs, history_pool)``。

    撤销日志的相邻快照之间只有一两个对象不同（每次命令只动一步），
    全量存储会让文件随操作次数线性膨胀。这里把所有出现过的对象/参考图
    数据收进池（按内容去重，相同数据只存一份），每条快照只留池下标序列：

        history_refs[k] = {"text", "page", "objects": [池下标...],
                           "references": [池下标...], "metadata"}

    页面参数与元数据每条单独存（几十字节，不值得进池）。
    """
    pool = SnapshotPool()
    packed: list[dict[str, Any]] = []
    for e in entries:
        doc = e.get("doc") or {}
        packed.append({
            "text": str(e.get("text", "")),
            "page": doc.get("page", {}),
            "objects": [pool.ref(o) for o in doc.get("objects", [])],
            "references": [pool.ref(r) for r in doc.get("references", [])],
            "metadata": doc.get("metadata", {}),
        })
    return packed, history_pool_to_data(pool.items)


def doc_to_data(doc: Document) -> dict:
    """文档（页面 + 对象 + 参考层 + 元数据）的快照——也是项目文件的主体。"""
    return {
        "page": page_to_data(doc.page),
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
    # 撤销历史：池化格式（history_refs + history_pool）保持池化读入——展开成
    # 逐条全量快照会让内存随编辑步数线性膨胀（实测 5.58 MB 的项目文件展开后
    # 常驻 113 MB）。旧版逐条全量快照（history）也照读，语义由 project_to_data
    # 与历史重建统一处理。
    # 池缺失/损坏时整体放弃历史——半份池会让撤销重放出空文档，
    # 比没有历史危险得多；文档本体不受影响。
    history_pool: list[str] = []
    if isinstance(data.get("history_refs"), list):
        pool = data.get("history_pool")
        if isinstance(pool, list):
            try:
                history_pool = history_pool_from_data(pool)
                history = [dict(e) for e in data["history_refs"]
                           if isinstance(e, dict)]
            except Exception:
                history, history_pool = [], []
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
        history_pool=history_pool,
    )


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FileStamp:
    """文件指纹（大小 + mtime + 内容摘要），用于识别外部改动。

    摘要取 blake2b 前 16 字节：8 MB 的文件约 10 ms，只在打开/保存时算一次。
    mtime 单独不可靠（有些文件系统/同步工具会保留时间戳），所以内容对不上
    就算改过。
    """

    size: int
    mtime_ns: int
    digest: str

    @classmethod
    def of(cls, path: str | Path) -> Optional["FileStamp"]:
        """读取文件指纹；文件不存在或读不了返回 None。"""
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return None
        h = hashlib.blake2b(digest_size=16)
        try:
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        except OSError:
            return None
        return cls(st.st_size, st.st_mtime_ns, h.hexdigest())

    def same_file_as(self, other: Optional["FileStamp"]) -> bool:
        return other is not None and self.digest == other.digest

    def mtime_text(self) -> str:
        """本地时间的 mtime（报给用户看的「磁盘上那份是什么时候的」）。"""
        import time

        try:
            return time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(self.mtime_ns / 1e9))
        except (OSError, OverflowError, ValueError):
            return "未知时间"


class ExternalChangeError(RuntimeError):
    """保存前发现目标文件已被外部修改（内容与打开时记下的指纹不符）。"""

    def __init__(self, path: str | Path, expected: "FileStamp",
                 actual: "FileStamp") -> None:
        super().__init__(f"{path} 已被外部修改")
        self.path = Path(path)
        self.expected = expected
        self.actual = actual


def save_project(path: str | Path, project: ProjectData,
                 *, indent: Optional[int] = None,
                 expect: Optional[FileStamp] = None) -> None:
    """写项目文件。默认紧凑 JSON：坐标数组缩进后每行一个数字，缩进空白能占
    文件的三分之二（实测 9.86 MB 的项目紧凑写只有 3.21 MB），白白拖慢读写、
    抬高打开时的文本/解析峰值。``indent`` 仅调试用。

    ``expect`` 为打开/上次保存时记下的磁盘指纹：文件在别处被改过（另一
    实例或外部编辑器）就抛 :class:`ExternalChangeError`，由调用方决定
    覆盖/另存/重载——整份覆盖别人的成果是静默丢数据的头号来源。
    """
    path = Path(path)
    if path.suffix.lower() != FILE_SUFFIX:
        path = path.with_suffix(FILE_SUFFIX)
    if expect is not None:
        actual = FileStamp.of(path)
        # 文件被外部删除不算冲突（重新写出来即可）；内容对不上才是冲突
        if actual is not None and not actual.same_file_as(expect):
            raise ExternalChangeError(path, expect, actual)
    data = project_to_data(project)
    text = json.dumps(data, ensure_ascii=False, indent=indent,
                      separators=(",", ":") if indent is None else None)
    # 先写同目录临时文件再替换：写到一半被杀（断电/崩溃）也不会留下
    # 半份 JSON 把原文件毁掉
    tmp = path.with_name(path.name + ".part")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
