"""把扰动参数应用到任意文档对象（统一入口）。

不同来源的对象，扰动参数存放位置不同：

    * ``text``         → ``TextSpec.perturb``（排版时逐字扰动）
    * ``markdown``     → ``source.data["perturb"]``
    * ``equation``     → ``source.data["perturb"]``（重建时施加）
    * ``latex``        → ``source.data["perturb"]``
    * ``svg``          → ``source.data["perturb"]``（另有独立的线条抖动参数）
    * 静态/自由矢量对象 → ``source.data["base_strokes"]`` + ``data["perturb"]``

统一只对外暴露 :func:`apply_perturb` 与 :func:`resolve_perturb`：前者写入参数
并立即重算笔画，后者只读取当前参数（用于面板回填）。这样「矢量线条也能被随机
扰动、并设置扰动强度」就和文本走同一条路径。
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.document import DocumentObject
from ..core.strokes import Stroke
from .engine import perturb_strokes
from .params import PerturbParams

# 这些来源的扰动直接由各自的构建器（按 source 重建）负责
_CONTENT_KINDS = {"markdown", "equation", "latex", "svg", "tikz"}
_TEXT_KIND = "text"
# 原始笔画缓存的键名（仅静态/矢量对象使用）
BASE_STROKES_KEY = "base_strokes"


# ---------------------------------------------------------------------------
# 笔画 ↔ 可序列化数据
# ---------------------------------------------------------------------------
def _strokes_to_data(strokes: list[Stroke]) -> list[dict[str, Any]]:
    return [{"pts": [[float(x), float(y)] for x, y in s.points],
             "closed": bool(s.closed)} for s in strokes]


def _strokes_from_data(data: list) -> list[Stroke]:
    out: list[Stroke] = []
    for d in data or []:
        try:
            pts = [(float(p[0]), float(p[1])) for p in d.get("pts", [])]
        except (TypeError, ValueError, AttributeError):
            continue
        out.append(Stroke(pts, bool(d.get("closed", False))))
    return out


# ---------------------------------------------------------------------------
# 支持性判断
# ---------------------------------------------------------------------------
def supports_perturb(obj: DocumentObject) -> bool:
    """该对象是否支持扰动（参考层不是对象，不在此列）。"""
    return obj.source.kind in (_CONTENT_KINDS | {_TEXT_KIND, "static", "freehand"})


def resolve_perturb(obj: DocumentObject) -> Optional[PerturbParams]:
    """读取对象当前的扰动参数；不支持则返回 None。"""
    kind = obj.source.kind
    if kind == _TEXT_KIND:
        from ..fonts.builder import TextSpec
        try:
            return TextSpec.from_data(obj.source.data).perturb
        except Exception:
            return None
    if kind in _CONTENT_KINDS or kind in ("static", "freehand"):
        return PerturbParams.from_data(obj.source.data.get("perturb"))
    return None


def object_reference_size(obj: DocumentObject) -> float:
    """对象特征尺寸(mm)，用于把「手绘线条」预设换算成合适的振幅。"""
    box = obj.local_bbox()
    if box.is_empty:
        return 10.0
    return max(1.0, min(box.width, box.height) or max(box.width, box.height))


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
def apply_perturb(obj: DocumentObject, params: PerturbParams, manager=None) -> bool:
    """把扰动参数写入对象并立即重算笔画；返回是否成功。

    * 文本走排版扰动（三级扰动，逐字处理）。
    * 富内容（Markdown/公式/LaTeX/SVG）写回 source 后按源重建。
    * 静态/矢量对象：首次调用把当前笔画存为「原始笔画」，之后每次都基于原始
      笔画重算，因此可反复调强度、换种子而**不累积失真**。
    """
    kind = obj.source.kind
    if kind == _TEXT_KIND:
        from ..content.stroke_edits import KEY as _EDITS_KEY, apply_edits
        from ..fonts.builder import TextSpec, update_text_object
        # 只调扰动时源内容（文字/字体）没变，画笔编辑层必须原样保留：
        # update_text_object 会整体重建 source，先把编辑层取出来再放回
        edits = obj.source.data.get(_EDITS_KEY)
        spec = TextSpec.from_data(obj.source.data)
        spec.perturb = params.clone()
        if obj.source.data.get("font_seed") is not None:
            spec.font_seed = int(obj.source.data["font_seed"])
        update_text_object(obj, spec, manager)
        if edits:
            obj.source.data[_EDITS_KEY] = edits
            obj.local_strokes = apply_edits(obj.local_strokes, obj.source.data)
        return True

    if kind in _CONTENT_KINDS:
        from ..content.builder import regenerate_content_object
        old = obj.source.data.get("perturb")
        obj.source.data["perturb"] = params.to_data()
        try:
            regenerate_content_object(obj, manager)
        except Exception:
            # 重建失败：回滚参数，保持「参数 ↔ 笔画」一致
            if old is None:
                obj.source.data.pop("perturb", None)
            else:
                obj.source.data["perturb"] = old
            return False
        return True

    if kind in ("static", "freehand"):
        data = obj.source.data
        if BASE_STROKES_KEY not in data:
            data[BASE_STROKES_KEY] = _strokes_to_data(obj.local_strokes)
        base = _strokes_from_data(data[BASE_STROKES_KEY])
        data["perturb"] = params.to_data()
        obj.local_strokes = (perturb_strokes(base, params)
                             if params.is_active() else [s.clone() for s in base])
        return True

    return False


def reseed_one(obj: DocumentObject, manager=None) -> bool:
    """就地为对象换一个随机种子（保留其它扰动参数）并立即重算笔画。

    ``resolve_perturb`` 返回的是参数副本，必须经 :func:`apply_perturb`
    写回 source 才会生效（否则只是改了副本，对象毫无变化）。
    """
    p = resolve_perturb(obj)
    if p is None:
        return False
    p.seed = (p.seed * 1103515245 + 12345) % (2 ** 31)
    return apply_perturb(obj, p, manager)
