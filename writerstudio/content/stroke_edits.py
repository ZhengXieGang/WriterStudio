"""内容对象的手工笔画编辑层。

富内容对象（Markdown / 公式 / LaTeX / TikZ / SVG / 文本）的笔画由**源内容
重新生成**：调整手写扰动参数、换种子、打开项目时都会按源重渲染一遍。若用户
在画笔编辑工具里删掉或改动了某几条笔画，重渲染会把这些编辑**冲刷掉**
（删除的笔画又冒出来，改过的笔画回退成原样）。

本模块把手工编辑记录成一个**编辑层**，存在 ``source.data["stroke_edits"]``
里，重生成之后再由 :func:`apply_edits` 重新施加：

    * ``deleted``：被删除笔画的**原始序号**（重生成前的下标）；
    * ``modified``：被改动笔画的原始序号 → 新的本地坐标笔画。

序号之所以稳定：一次重渲染只改变笔画的形状，**不改变条数与顺序**
（扰动、抽稀都是逐条映射），所以同一个下标在换参数后仍指向同一条笔画。
用户看到的是「删掉就是删掉、改过就是改过」，与源内容解耦。

一旦用户重新编辑源内容（改文字/换字体/重贴 TikZ 源码），主窗口会整体替换
``source``，编辑层随之清空——这是符合直觉的：源都换了，旧的局部修改自然作废。
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ..core.strokes import Stroke

#: 存在 ``SourceSpec.data`` 里的键名
KEY = "stroke_edits"


def stroke_to_edit_data(s: Stroke) -> dict[str, Any]:
    return {
        "pts": [[float(x), float(y)] for x, y in s.points],
        "closed": bool(s.closed),
        "role": str(s.role or ""),
        "group": int(s.group),
    }


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    """把 ``data[KEY]`` 规整成 ``{"deleted": [...], "modified": {int: {...}}}``。"""
    raw = data.get(KEY)
    if not isinstance(raw, dict):
        return {"deleted": [], "modified": {}}
    deleted: set[int] = set()
    for item in raw.get("deleted") or []:
        i = _as_int(item)
        if i is not None and i >= 0:
            deleted.add(i)
    modified: dict[int, dict[str, Any]] = {}
    for k, v in (raw.get("modified") or {}).items():
        i = _as_int(k)
        if i is None or i < 0 or not isinstance(v, dict):
            continue
        pts = []
        try:
            for p in v.get("pts") or []:
                pts.append((float(p[0]), float(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
        if len(pts) >= 2:
            g = _as_int(v.get("group", -1))
            modified[i] = {"pts": pts, "closed": bool(v.get("closed", False)),
                           "role": str(v.get("role", "") or ""),
                           "group": -1 if g is None else g}
    return {"deleted": sorted(deleted), "modified": modified}


def _store(data: dict[str, Any], state: dict[str, Any]) -> None:
    if not state["deleted"] and not state["modified"]:
        data.pop(KEY, None)
        return
    data[KEY] = {
        "deleted": list(state["deleted"]),
        # JSON 的键只能是字符串，落盘后读回由 _normalized 还原成 int
        "modified": {str(k): v for k, v in state["modified"].items()},
    }


def has_edits(data: dict[str, Any]) -> bool:
    st = _normalized(data)
    return bool(st["deleted"] or st["modified"])


def display_to_raw(deleted: Sequence[int], total_raw: int,
                   display_index: int) -> Optional[int]:
    """显示序号 → 原始序号（跳过已删除项）。越界返回 None。"""
    if display_index < 0:
        return None
    ds = set(deleted)
    seen = -1
    for raw in range(total_raw):
        if raw in ds:
            continue
        seen += 1
        if seen == display_index:
            return raw
    return None


def record_delete(data: dict[str, Any], display_index: int,
                  display_len: int) -> bool:
    """记录「删除第 ``display_index`` 条笔画」（``display_len`` 为删除前条数）。"""
    st = _normalized(data)
    total_raw = display_len + len(st["deleted"])
    raw = display_to_raw(st["deleted"], total_raw, display_index)
    if raw is None:
        return False
    # 该条已被手工改过又删掉：改记录一并移除，避免残留
    st["modified"].pop(raw, None)
    if raw not in st["deleted"]:
        st["deleted"].append(raw)
        st["deleted"].sort()
    _store(data, st)
    return True


def record_modify(data: dict[str, Any], display_index: int, stroke: Stroke,
                  display_len: int) -> bool:
    """记录「把第 ``display_index`` 条笔画改成 ``stroke``」（本地坐标）。"""
    st = _normalized(data)
    total_raw = display_len + len(st["deleted"])
    raw = display_to_raw(st["deleted"], total_raw, display_index)
    if raw is None:
        return False
    st["modified"][raw] = stroke_to_edit_data(stroke)
    _store(data, st)
    return True


def apply_edits(strokes: Sequence[Stroke], data: dict[str, Any]
                ) -> list[Stroke]:
    """对刚重生成的笔画施加编辑层，返回新的笔画列表。

    越界的删除/改动序号直接忽略（源内容变了导致条数减少时不至于删错笔画）。
    """
    st = _normalized(data)
    if not st["deleted"] and not st["modified"]:
        return list(strokes)
    deleted = set(st["deleted"])
    modified = st["modified"]
    out: list[Stroke] = []
    for i, s in enumerate(strokes):
        if i in deleted:
            continue
        m = modified.get(i)
        if m is not None:
            out.append(Stroke(m["pts"], m["closed"], m["role"], m["group"]))
        else:
            out.append(s)
    return out
