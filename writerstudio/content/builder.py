"""把富内容（Markdown / 公式 / LaTeX 文档 / SVG）构建为文档对象。

与文本对象一致，源内容记录在 ``source`` 中、笔画存于 ``local_strokes``，
因此对象可自由移动/缩放/旋转而不影响内容，也可按源重新生成。

源类型（``SourceSpec.kind``）：
    ``markdown``   Markdown 片段（含表格）
    ``equation``   LaTeX 公式（mathtext）
    ``latex``      完整 LaTeX 文档
    ``svg``        矢量化图形（含可选手绘扰动参数）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Sequence

from ..core.document import DocumentObject, SourceSpec
from ..core.geometry import BBox
from ..core.strokes import Stroke
from ..fonts.manager import FontManager
from ..fonts.model import FontFamily
from ..perturb.params import PerturbParams
from ..perturb.engine import perturb_strokes, wobble_strokes
from .equation import render_equation
from .stroke_edits import apply_edits as _apply_stroke_edits
from .latex_doc import render_latex_document
from .markdown import MarkdownStyle, render_markdown
from .svg_import import import_svg
from .tikz import render_tikz

SOURCE_MARKDOWN = "markdown"
SOURCE_EQUATION = "equation"
SOURCE_LATEX = "latex"
SOURCE_SVG = "svg"
SOURCE_TIKZ = "tikz"

# ---------------------------------------------------------------------------
# 原始渲染缓存
# ---------------------------------------------------------------------------
#: ``obj.meta`` 里的键（meta 不参与序列化，缓存只在会话内有效）。
#: 值形如 ``{"fp": 渲染输入指纹, "strokes": [未施加扰动的原始笔画]}``。
_RENDER_CACHE_KEY = "render_cache"
#: 缓存点数上限：超大文档（整页 LaTeX）不再翻倍内存，退回全量重建
_CACHE_MAX_POINTS = 1_000_000


def _render_fingerprint(kind: str, data: dict[str, Any], manager) -> str:
    """渲染输入指纹：除「扰动参数/笔画编辑」外一切影响渲染结果的输入。

    扰动只变换已渲染的笔画，不改变它们；指纹一致即可直接复用原始笔画，
    跳过昂贵的重渲染（TeX 重编译等）。SVG 文件的磁盘内容（mtime/大小）
    与字体集版本（FontManager.version）也计入，外部变化不会拿到旧图。
    """
    core = {k: v for k, v in (data or {}).items()
            if k not in ("perturb", "stroke_edits",
                         "layout_box", "table_box")}
    if kind == SOURCE_SVG and core.get("path"):
        try:
            st = os.stat(core["path"])
            core["\x00mtime_ns"] = st.st_mtime_ns
            core["\x00size"] = st.st_size
        except OSError:
            core["\x00gone"] = True
    if manager is not None:
        core["\x00fonts"] = getattr(manager, "version", 0)
    try:
        return json.dumps([kind, core], sort_keys=True, default=repr)
    except Exception:
        return repr([kind, core])


def _cache_put(meta: dict[str, Any], fp: str, strokes: list[Stroke],
               tables: Optional[list] = None) -> None:
    if not strokes:
        return
    total = sum(len(s.points) for s in strokes)
    if total > _CACHE_MAX_POINTS:
        return
    entry: dict[str, Any] = {"fp": fp, "strokes": strokes}
    if tables:
        entry["tables"] = list(tables)     # Markdown 表格外框（供画布手柄）
    meta[_RENDER_CACHE_KEY] = entry


def _apply_object_perturb(raw: list[Stroke],
                          p: PerturbParams) -> list[Stroke]:
    """对原始笔画施加整笔扰动；未启用时返回副本（缓存与结果绝不共享）。"""
    if p.is_active():
        return perturb_strokes(raw, p)
    return [s.clone() for s in raw]


def transfer_render_cache(src: DocumentObject, dst: DocumentObject) -> None:
    """把 ``src``（刚按新源构建的对象）的渲染缓存与缺字元数据搬给 ``dst``。

    内容对话框重编辑时主窗口只把 source/笔画/名称换到原对象上，meta 里
    留着的是旧源的缓存；不搬移的话，改源后的第一次调扰动会多付一次
    全量重渲染（TikZ/LaTeX 重编译）。
    """
    dst.meta.pop(_RENDER_CACHE_KEY, None)
    entry = src.meta.get(_RENDER_CACHE_KEY)
    if isinstance(entry, dict):
        dst.meta[_RENDER_CACHE_KEY] = entry
    if src.meta.get("missing"):
        dst.meta["missing"] = src.meta["missing"]
    else:
        dst.meta.pop("missing", None)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def make_markdown_object(source: str, manager: FontManager,
                         font_names: Optional[Sequence[str]] = None,
                         style: Optional[MarkdownStyle] = None,
                         name: Optional[str] = None,
                         perturb: Optional[PerturbParams] = None) -> DocumentObject:
    fonts = _resolve(manager, font_names)
    st = style or MarkdownStyle()
    res = render_markdown(source, fonts, st)
    raw = list(res.strokes)
    data: dict[str, Any] = {
        "text": source,
        "font_names": list(font_names or []),
        "style": _style_to_data(st),
    }
    fp = _render_fingerprint(SOURCE_MARKDOWN, data, manager)
    strokes = _apply_object_perturb(raw, perturb or PerturbParams())
    box = BBox.from_points(pts for s in strokes for pts in s.points)
    _normalize_to_origin(strokes, box)
    obj = DocumentObject(
        name=name or _md_name(source),
        source=SourceSpec(SOURCE_MARKDOWN, {**data, "perturb":
                                            (perturb or PerturbParams()).to_data()}),
        local_strokes=strokes,
    )
    _store_table_box(obj, res.tables, box)
    _cache_put(obj.meta, fp, raw, res.tables)
    return obj


def _store_table_box(obj: DocumentObject, tables, box: BBox) -> None:
    """记录表格外框（平移至与 ``_normalize_to_origin`` 后笔画同一坐标）。

    画布据此给 Markdown 对象显示「表格宽度」手柄；无表格则清除。宽度取
    所有表格中最宽者（多表文档改宽度会同时作用于全部表格）。框几何同时
    持久化到 ``source.data["table_box"]``——``meta`` 不参与序列化，重开
    文档时若重新生成失败（缺字体等），画布仍能从源数据恢复手柄位置。
    """
    obj.meta.pop("md_table_box", None)
    obj.source.data.pop("table_box", None)
    if not tables or box.is_empty:
        return
    dx, dy = -box.x0, -box.y1
    x0 = min(t[0] for t in tables)
    y_top = max(t[1] for t in tables)
    x1 = max(t[2] for t in tables)
    y_bottom = min(t[3] for t in tables)
    box_tuple = (x0 + dx, y_top + dy, x1 + dx, y_bottom + dy)
    obj.meta["md_table_box"] = box_tuple
    obj.source.data["table_box"] = [float(v) for v in box_tuple]


def _style_to_data(st: MarkdownStyle) -> dict[str, Any]:
    return {
        "size": st.size, "line_spacing": st.line_spacing,
        "char_spacing": st.char_spacing,
        "wrap_width": st.wrap_width, "table_width": st.table_width,
        "list_indent": st.list_indent,
        "quote_indent": st.quote_indent, "math_size_scale": st.math_size_scale,
        "table_end_jitter": st.table_end_jitter,
        "table_overshoot": st.table_overshoot,
        "table_row_jitter": st.table_row_jitter,
        "table_col_jitter": st.table_col_jitter,
        "table_seed": st.table_seed,
    }


def _style_from_data(d: dict[str, Any]) -> MarkdownStyle:
    st = MarkdownStyle()
    for k in ("size", "line_spacing", "char_spacing", "wrap_width",
              "table_width", "list_indent", "quote_indent", "math_size_scale",
              "table_end_jitter", "table_overshoot", "table_row_jitter",
              "table_col_jitter", "table_seed"):
        if k in d:
            setattr(st, k, d[k])
    return st


# ---------------------------------------------------------------------------
# 公式
# ---------------------------------------------------------------------------
def make_equation_object(latex: str, size_mm: float = 5.0,
                         name: Optional[str] = None,
                         perturb: Optional[PerturbParams] = None,
                         font_names: Optional[Sequence[str]] = None,
                         manager: Optional[FontManager] = None,
                         text_scale: float = 1.0) -> DocumentObject:
    """构建公式对象。

    ``font_names`` + ``manager`` 给定时，公式中链上画得出的字符（字母/数字
    等）改用字体链重排（手写字迹），数学符号保留 mathtext 轮廓。
    """
    raw = render_equation(latex, size_mm=size_mm, font_names=font_names,
                          manager=manager, text_scale=text_scale)
    p = perturb or PerturbParams()
    data: dict[str, Any] = {"latex": latex, "size": size_mm,
                            "font_names": list(font_names or []),
                            "text_scale": float(text_scale)}
    fp = _render_fingerprint(SOURCE_EQUATION, data, manager)
    strokes = _apply_object_perturb(raw, p)
    name = name or _eq_name(latex)
    obj = DocumentObject(
        name=name,
        source=SourceSpec(SOURCE_EQUATION, {**data, "perturb": p.to_data()}),
        local_strokes=strokes,
    )
    _cache_put(obj.meta, fp, raw)
    return obj


# ---------------------------------------------------------------------------
# 完整 LaTeX 文档
# ---------------------------------------------------------------------------
def make_latex_object(source: str, target_width_mm: Optional[float] = None,
                      name: Optional[str] = None,
                      perturb: Optional[PerturbParams] = None,
                      manager: Optional[FontManager] = None,
                      font_names: Optional[Sequence[str]] = None
                      ) -> DocumentObject:
    fonts = _resolve(manager, font_names)
    r = render_latex_document(source, target_width_mm=target_width_mm,
                              fonts=fonts)
    if not r.ok:
        raise ValueError(r.log or "LaTeX 编译失败")
    p = perturb or PerturbParams()
    raw = list(r.strokes)
    data: dict[str, Any] = {"source": source, "target_width": target_width_mm,
                            "font_names": list(font_names or []),
                            # 回退/忠实编译产出不同笔画：进指纹——装上 TeX
                            # 后「重新生成」会自动切换到编译路径
                            "tex_native": bool(r.native)}
    fp = _render_fingerprint(SOURCE_LATEX, data, None)
    strokes = _apply_object_perturb(raw, p)
    obj = DocumentObject(
        name=name or "LaTeX 文档",
        source=SourceSpec(SOURCE_LATEX, {**data, "perturb": p.to_data()}),
        local_strokes=strokes,
    )
    _cache_put(obj.meta, fp, raw)
    return obj


# ---------------------------------------------------------------------------
# TikZ 图形
# ---------------------------------------------------------------------------
def make_tikz_object(source: str, target_width_mm: Optional[float] = None,
                     name: Optional[str] = None,
                     perturb: Optional[PerturbParams] = None,
                     font_names: Optional[Sequence[str]] = None,
                     manager: Optional[FontManager] = None,
                     text_scale: float = 1.0) -> DocumentObject:
    """构建 TikZ 对象。

    ``font_names``（本软件字体链）+ ``manager`` 给定时，TikZ 节点文字改用该
    字体链重排（用用户自己的手写字迹），图形仍由 TeX 保证位置精度。缺省时
    文字保留 TeX 的字体轮廓。
    """
    r = render_tikz(source, target_width_mm=target_width_mm,
                    font_names=font_names, manager=manager,
                    text_scale=text_scale)
    if not r.ok:
        raise ValueError(r.log or "TikZ 编译失败")
    p = perturb or PerturbParams()
    raw = list(r.strokes)
    data: dict[str, Any] = {
        "source": source, "target_width": target_width_mm,
        "font_names": list(font_names or []),
        "text_scale": float(text_scale),
    }
    fp = _render_fingerprint(SOURCE_TIKZ, data, manager)
    strokes = _apply_object_perturb(raw, p)
    obj = DocumentObject(
        name=name or _tikz_name(source),
        source=SourceSpec(SOURCE_TIKZ, {**data, "perturb": p.to_data()}),
        local_strokes=strokes,
    )
    _cache_put(obj.meta, fp, raw)
    # 文字替换时字体链画不出的字符，供界面提示（与文本对象同一套提示路径）
    if getattr(r, "missing_chars", None):
        obj.meta["missing"] = list(r.missing_chars)
    return obj


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------
def make_svg_object(path: str | Path, target_width_mm: Optional[float] = None,
                    name: Optional[str] = None,
                    wobble_amplitude: float = 0.0,
                    wobble_wavelength: float = 20.0,
                    perturb: Optional[PerturbParams] = None,
                    seed: Optional[int] = None) -> DocumentObject:
    r = import_svg(path, target_width_mm=target_width_mm)
    strokes = list(r.strokes)
    # 手绘抖动（线条起伏）属于「原始渲染」：起伏参数进了渲染指纹，
    # 调起伏参数会重新导入+起伏；调扰动参数则直接复用下面的原始笔画
    if wobble_amplitude > 0.0:
        strokes = wobble_strokes(strokes, wobble_amplitude,
                                 wobble_wavelength, seed=seed)
    raw = [s.clone() for s in strokes]
    p = perturb or PerturbParams()
    data: dict[str, Any] = {
        "path": str(path), "target_width": target_width_mm,
        "wobble_amplitude": wobble_amplitude,
        "wobble_wavelength": wobble_wavelength,
    }
    fp = _render_fingerprint(SOURCE_SVG, data, None)
    strokes = _apply_object_perturb(raw, p)
    box = BBox.from_points(pts for s in strokes for pts in s.points)
    _normalize_to_origin(strokes, box)
    obj = DocumentObject(
        name=name or f"SVG：{Path(path).stem}",
        source=SourceSpec(SOURCE_SVG, {**data, "perturb": p.to_data()}),
        local_strokes=strokes,
    )
    _cache_put(obj.meta, fp, raw)
    return obj


# ---------------------------------------------------------------------------
# 重新生成（按 source 刷新笔画，保留 transform）
# ---------------------------------------------------------------------------
def regenerate_content_object(obj: DocumentObject, manager: FontManager) -> bool:
    kind = obj.source.kind
    data = obj.source.data
    fp = _render_fingerprint(kind, data, manager)
    p = PerturbParams.from_data(data.get("perturb"))

    # 缓存命中：渲染输入（源内容/字体集/SVG 文件）没变，只调了扰动参数
    # ——直接复用原始笔画，跳过昂贵的重渲染（TikZ/LaTeX 重编译等）
    cached = obj.meta.get(_RENDER_CACHE_KEY)
    if (isinstance(cached, dict) and cached.get("fp") == fp
            and isinstance(cached.get("strokes"), list) and cached["strokes"]):
        strokes = _apply_object_perturb(cached["strokes"], p)
        if kind in (SOURCE_MARKDOWN, SOURCE_SVG):
            box = BBox.from_points(pts for s in strokes for pts in s.points)
            _normalize_to_origin(strokes, box)
            if kind == SOURCE_MARKDOWN:
                _store_table_box(obj, cached.get("tables"), box)
        obj.local_strokes = _apply_stroke_edits(strokes, data)
        return True

    new = _build_content(kind, data, obj.name, manager)
    if new is None:
        return False
    obj.name = new.name
    transfer_render_cache(new, obj)
    # 表格外框随新渲染更新（画布宽度手柄据此定位；漏搬会让手柄停在旧宽度）
    if kind == SOURCE_MARKDOWN:
        box = new.meta.get("md_table_box")
        if box is None:
            obj.meta.pop("md_table_box", None)
        else:
            obj.meta["md_table_box"] = box
    # 重生成会冲刷掉画笔编辑的删除/改动，这里把编辑层重新施加一遍
    obj.local_strokes = _apply_stroke_edits(new.local_strokes, data)
    return True


def _build_content(kind: str, data: dict[str, Any], name: str,
                   manager: FontManager) -> Optional[DocumentObject]:
    """按源数据全量重建一个内容对象（含扰动）；失败返回 None。"""
    if kind == SOURCE_MARKDOWN:
        return make_markdown_object(
            data.get("text", ""), manager,
            font_names=data.get("font_names"),
            style=_style_from_data(data.get("style", {})),
            name=name,
            perturb=PerturbParams.from_data(data.get("perturb")),
        )
    if kind == SOURCE_EQUATION:
        return make_equation_object(data.get("latex", ""),
                                    float(data.get("size", 5.0)), name,
                                    perturb=PerturbParams.from_data(
                                        data.get("perturb")),
                                    font_names=data.get("font_names") or None,
                                    manager=manager,
                                    text_scale=float(
                                        data.get("text_scale", 1.0) or 1.0))
    if kind == SOURCE_LATEX:
        try:
            return make_latex_object(data.get("source", ""),
                                     data.get("target_width"), name,
                                     perturb=PerturbParams.from_data(
                                         data.get("perturb")),
                                     manager=manager,
                                     font_names=data.get("font_names")
                                     or None)
        except ValueError:
            return None
    if kind == SOURCE_TIKZ:
        try:
            return make_tikz_object(data.get("source", ""),
                                    data.get("target_width"), name,
                                    perturb=PerturbParams.from_data(
                                        data.get("perturb")),
                                    font_names=data.get("font_names") or None,
                                    manager=manager,
                                    text_scale=float(
                                        data.get("text_scale", 1.0) or 1.0))
        except Exception:
            return None
    if kind == SOURCE_SVG:
        try:
            return make_svg_object(
                data.get("path", ""), data.get("target_width"), name,
                float(data.get("wobble_amplitude", 0.0)),
                float(data.get("wobble_wavelength", 20.0)),
                perturb=PerturbParams.from_data(data.get("perturb")),
            )
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _resolve(manager: FontManager,
             font_names: Optional[Sequence[str]]) -> list[FontFamily]:
    if manager is None:                    # headless/测试：无管理器即无字体
        return []
    names = list(font_names or [])
    if not names:
        # 默认：中文单线 + 英文 Hershey
        for e in manager.entries():
            if e.kind == "stroke-json":
                names.append(e.name)
                break
        for cand in ("futural", "futuram", "scripts"):
            if manager.get_entry(cand):
                names.append(cand)
                break
    fonts: list[FontFamily] = []
    for n in names:
        f = manager.get(n)
        if f is not None:
            fonts.append(f)
    return fonts


def _normalize_to_origin(strokes: list[Stroke], box: BBox) -> None:
    """把内容平移，使包围盒左上角对齐原点（Y 向上 → 最高点 y=0）。"""
    if box.is_empty:
        return
    dx, dy = -box.x0, -box.y1
    for s in strokes:
        s.points = [(x + dx, y + dy) for x, y in s.points]


def _md_name(source: str) -> str:
    for line in source.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return f"Markdown：{line[:12]}"
    return "Markdown"


def _eq_name(latex: str) -> str:
    t = latex.strip()
    return f"公式：{t[:16]}" if t else "公式"


def _tikz_name(source: str) -> str:
    for line in source.splitlines():
        s = line.strip()
        if s and not s.startswith("%"):
            return f"TikZ：{s[:12]}"
    return "TikZ 图形"
