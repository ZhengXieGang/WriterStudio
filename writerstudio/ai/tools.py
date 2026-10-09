"""AI 排版工具层：MCP 工具的具体实现。

设计要点：

* **坐标语义**：对 AI 一律是「页面左上原点、x 右、y 下、mm」；本模块负责
  与内部「左下原点、y 向上」互转（:func:`_ai_to_page` / :func:`_bbox_to_ai`）。
  对 AI 而言「定位」永远是**对象包围盒左上角**落到 (x, y)——所见即所得，
  不需要理解排版基线等内部概念。
* **可撤销**：所有修改都通过既有 ``DocumentController`` 命令入撤销栈，
  命令文本带「AI」前缀，用户可在历史面板逐步撤销 AI 的任何操作。
* **失败即说明**：参数错误抛 :class:`ToolError`，错误文案面向 AI（附上
  可用字体清单、缺字列表等纠错线索），服务层原样返回给客户端。
* 本模块不依赖网络：:class:`AiTools` 可直接在进程内调用（单测/端到端
  演示即走此路径），也可被 ``ai.server`` 经 TCP 调用。
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..content.builder import (
    make_equation_object,
    make_markdown_object,
    make_tikz_object,
)
from ..content.tikz import tikz_available
from ..core.document import PageSpec
from ..core.geometry import AffineTransform, BBox
from ..fonts.builder import TextSpec, update_text_object
from ..fonts.layout import ALIGN_CENTER, ALIGN_LEFT, ALIGN_RIGHT
from ..perturb.params import PerturbParams
from ..project import ExternalChangeError, save_project

#: 越界判定容差 (mm)：包围盒超出页面该距离才报 out_of_page
_EPS = 0.5
#: 重叠判定：交叠面积超过该值 (mm²) 才报 overlap
_OVERLAP_AREA = 1.0

#: add_* 的落点锚点：包围盒在页面上的哪一点落到 (x, y)。
# 分数含义 (fx, fy)：fx 沿 x 向右 0→1，fy 沿 y **向下** 0→1。
_ANCHOR_FRACTIONS: dict[str, tuple[float, float]] = {
    "top-left": (0.0, 0.0), "top-center": (0.5, 0.0),
    "top-right": (1.0, 0.0),
    "center-left": (0.0, 0.5), "center": (0.5, 0.5),
    "center-right": (1.0, 0.5),
    "bottom-left": (0.0, 1.0), "bottom-center": (0.5, 1.0),
    "bottom-right": (1.0, 1.0),
    # baseline-left：首行基线左端——往表格格子里写字时「下沿贴线」用它
    "baseline-left": (0.0, -1.0),
}
_ANCHOR_HELP = ("top-left/top-center/top-right、center-left/center/"
                "center-right、bottom-left/bottom-center/bottom-right、"
                "baseline-left（首行基线左端）")


def _bbox_intersection(a: BBox, b: BBox) -> Optional[BBox]:
    """两包围盒的交集；不相交返回 None（BBox 自身无 intersect 方法）。"""
    x0, y0 = max(a.x0, b.x0), max(a.y0, b.y0)
    x1, y1 = min(a.x1, b.x1), min(a.y1, b.y1)
    if x0 >= x1 or y0 >= y1:
        return None
    return BBox(x0, y0, x1, y1)


class ToolError(Exception):
    """工具执行失败；消息面向 AI，应包含可据此修正的线索。"""


@dataclass
class _Pos:
    """AI 语义下的放置位置（页面左上原点，mm）。"""

    x: float
    y: float

    @classmethod
    def parse(cls, args: dict, *, required: bool) -> Optional["_Pos"]:
        x, y = args.get("x"), args.get("y")
        if x is None and y is None:
            if required:
                raise ToolError("缺少位置参数 x/y（mm，页面左上角为原点，y 向下）")
            return None
        if x is None or y is None:
            raise ToolError("x 与 y 必须成对提供（页面左上角为原点，y 向下，单位 mm）")
        return cls(_num(x, "x"), _num(y, "y"))


def _num(v: Any, name: str, *, lo: Optional[float] = None,
         hi: Optional[float] = None) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ToolError(f"参数 {name} 必须是数字，收到：{v!r}")
    if not math.isfinite(f):
        raise ToolError(f"参数 {name} 必须是有限数字")
    if lo is not None and f < lo:
        raise ToolError(f"参数 {name} 不能小于 {lo}")
    if hi is not None and f > hi:
        raise ToolError(f"参数 {name} 不能大于 {hi}")
    return f


def _page_top_left(page: PageSpec, pos: _Pos) -> tuple[float, float]:
    """AI 坐标（左上原点、y 向下）→ 内部页面坐标（左下原点、y 向上）。"""
    return pos.x, page.height - pos.y


def _bbox_to_ai(page: PageSpec, box: BBox) -> dict:
    """内部包围盒 → AI 语义 dict（左上原点、y 向下）。"""
    if box.is_empty:
        return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
    return {
        "x": round(box.x0, 3),
        "y": round(page.height - box.y1, 3),
        "w": round(box.width, 3),
        "h": round(box.height, 3),
    }


class AiTools:
    """AI 排版工具集。``window`` 是主窗口（提供文档控制器/字体/机器配置）。"""

    def __init__(self, window) -> None:
        self.win = window

    # ------------------------------------------------------------ 分发
    def call(self, name: str, arguments: Optional[dict]) -> dict:
        args = dict(arguments or {})
        handler = getattr(self, "tool_" + name, None)
        if handler is None or not name:
            raise ToolError(
                f"未知工具：{name}。可用工具：{', '.join(self.names())}")
        try:
            result = handler(args)
        except ToolError:
            raise
        except Exception as exc:            # 任何内部异常都转成 AI 可读消息
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        return result if isinstance(result, dict) else {"ok": True}

    def names(self) -> list[str]:
        return sorted(k[5:] for k in dir(self) if k.startswith("tool_"))

    # ------------------------------------------------------------ 内部
    @property
    def _doc(self):
        return self.win.controller.doc

    @property
    def _page(self) -> PageSpec:
        return self._doc.page

    def _fonts_by_names(self, names) -> Optional[list[str]]:
        """校验字体名；None 返回默认链。未知字体直接报错并列出可用项。"""
        if names is None:
            return None
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ToolError("font_names 必须是字体名字符串数组")
        if not names:
            return None
        available = {e.name: e for e in self.win.font_manager.visible_entries()}
        bad = [n for n in names if n not in available]
        if bad:
            raise ToolError(
                f"字体不存在：{bad}。可用字体：{sorted(available)}")
        return names

    # ============================================================ 参考图层
    def tool_list_references(self, args: dict) -> dict:
        """列出参考图层（底图）：图片/SVG 对齐辅助，**永不参与书写与 G-code**。"""
        page = self._page
        refs = []
        for r in self._doc.references:
            refs.append({
                "id": r.id, "name": r.name, "kind": r.kind, "path": r.path,
                "visible": r.visible, "locked": r.locked,
                "opacity": round(float(r.opacity), 3),
                "bbox": _bbox_to_ai(page, r.world_bbox()),
            })
        return {
            "references": refs, "count": len(refs),
            "note": "参考图只用于视觉对齐（表单套打等），不会写进 G-code；"
                    "render_preview 传 include_references=true 可把它们"
                    "按透明度画进预览图，用来核对内容与底图是否对齐。",
        }

    def tool_add_reference_image(self, args: dict) -> dict:
        """添加参考图层（底图）：把图片或 SVG 放到页面上用于对齐。

        path: 文件绝对路径（.png/.jpg/.jpeg/.bmp/.tif/.tiff/.webp/.gif/.svg）。
        x/y + anchor: 落点（默认 anchor=center）；省略 x/y 时按页边距内
        等比适配并居中（与软件界面「添加参考图片」一致）。
        width_mm: 指定显示宽度(mm)，省略按原尺寸；height_mm 同理（只给一个
        时按原比例算另一个）。opacity: 0~1，默认 0.45（淡一点不干扰书写）。
        """
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ToolError("path 必须是参考图文件的绝对路径")
        p = Path(path).expanduser()
        if not p.exists():
            raise ToolError(f"文件不存在：{p}")
        from ..ui.reference_panel import make_reference_from_file
        kind = args.get("kind") or None
        if kind is not None and kind not in ("image", "svg"):
            raise ToolError("kind 只能是 image 或 svg（省略时按扩展名判断）")
        try:
            ref = make_reference_from_file(str(p), kind=kind)
        except Exception as exc:
            raise ToolError(f"读取参考图失败：{exc}") from exc
        if args.get("opacity") is not None:
            ref.opacity = max(0.0, min(1.0, _num(args["opacity"], "opacity")))
        self._apply_reference_size(ref, args)
        pos = self._common_pos(args)
        if pos is None:
            from ..core.reference import fit_reference_to_box
            fit_reference_to_box(ref, self._page.margin_bbox(),
                                 align="center", margin=2.0)
        else:
            self._place_reference(ref, pos, self._common_anchor(args))
        self.win.controller.add_reference(ref)
        self._refresh_references(ref)
        page = self._page
        return {"ok": True, "id": ref.id, "name": ref.name, "kind": ref.kind,
                "bbox": _bbox_to_ai(page, ref.world_bbox()),
                "opacity": round(float(ref.opacity), 3),
                **self._fits_note(ref.world_bbox())}

    def tool_update_reference(self, args: dict) -> dict:
        """修改参考图：透明度/可见/锁定、位置与大小（不改文件本身）。

        x/y + anchor 重新落点；width_mm/height_mm 改显示尺寸（只给一个时
        按原比例）；opacity 0~1；visible/locked 布尔。返回新 bbox。
        """
        ref = self._find_reference(args.get("reference_id"))
        changed: dict = {}
        if args.get("opacity") is not None:
            changed["opacity"] = max(0.0, min(1.0,
                                               _num(args["opacity"], "opacity")))
        if args.get("visible") is not None:
            changed["visible"] = bool(args["visible"])
        if args.get("locked") is not None:
            changed["locked"] = bool(args["locked"])
        size_args = {k: v for k, v in args.items()
                     if k in ("width_mm", "height_mm") and v is not None}
        pos = self._common_pos(args)
        if changed:
            self.win.controller.edit_reference(ref, "AI：修改参考图", changed)
        if size_args or pos is not None:
            old_w, old_h = ref.width_mm, ref.height_mm
            box_before = ref.world_bbox()
            self._apply_reference_size(ref, args)
            if pos is not None:
                self._place_reference(ref, pos, self._common_anchor(args))
            elif size_args:
                # 只改尺寸：保持左上角不动（缩放手柄的直觉）
                b = ref.world_bbox()
                self._translate_reference(
                    ref, box_before.x0 - b.x0, box_before.y1 - b.y1)
            self._refresh_references(ref)
            changed.update({"width_mm": round(ref.width_mm, 3),
                            "height_mm": round(ref.height_mm, 3),
                            "was": [round(old_w, 3), round(old_h, 3)]})
        return {"ok": True, "id": ref.id, "changed": changed,
                "bbox": _bbox_to_ai(self._page, ref.world_bbox())}

    def tool_remove_reference(self, args: dict) -> dict:
        """删除参考图（可撤销）。不传 reference_id 时删除全部参考图。"""
        rid = args.get("reference_id")
        if rid:
            ref = self._find_reference(rid)
            self.win.controller.remove_reference(ref)
            self._refresh_references(None)
            return {"ok": True, "removed": 1, "id": ref.id}
        refs = list(self._doc.references)
        if not refs:
            return {"ok": True, "removed": 0}
        self.win.controller.remove_references(refs)
        self._refresh_references(None)
        return {"ok": True, "removed": len(refs)}

    def _find_reference(self, rid):
        rid = str(rid or "")
        for r in self._doc.references:
            if r.id == rid:
                return r
        ids = [r.id for r in self._doc.references]
        raise ToolError(f"参考图不存在：{rid}。当前参考图：{ids or '（无）'}")

    def _refresh_references(self, ref) -> None:
        panel = getattr(self.win, "reference_panel", None)
        if panel is not None:
            panel.refresh()
        self.win.canvas.sync_scene()
        if ref is not None:
            self.win.statusBar().showMessage(f"AI：参考图 {ref.name}", 4000)

    def _apply_reference_size(self, ref, args: dict) -> None:
        """按 args 里的 width_mm/height_mm 改显示尺寸（只给一个按原比例）。"""
        w = args.get("width_mm")
        h = args.get("height_mm")
        if w is None and h is None:
            return
        ratio = (ref.height_mm / ref.width_mm) if ref.width_mm > 1e-9 else 1.0
        if w is not None:
            ref.width_mm = _num(w, "width_mm", lo=1.0, hi=5000.0)
            ref.height_mm = ref.width_mm * ratio
        else:
            ref.height_mm = _num(h, "height_mm", lo=1.0, hi=5000.0)
            ref.width_mm = (ref.height_mm / ratio) if ratio > 1e-9 else \
                ref.height_mm

    def _place_reference(self, ref, pos: _Pos, anchor: str) -> None:
        box = ref.world_bbox()
        if box.is_empty:
            return
        fx, fy = _ANCHOR_FRACTIONS.get(anchor, (0.5, 0.5))
        tx, ty = _page_top_left(self._page, pos)
        want_x = box.x0 + fx * box.width
        want_y = box.y1 - fy * box.height
        self._translate_reference(ref, tx - want_x, ty - want_y)

    @staticmethod
    def _translate_reference(ref, dx: float, dy: float) -> None:
        t = ref.transform
        ref.transform = AffineTransform(t.a, t.b, t.c, t.d, t.e + dx, t.f + dy)

    def _find(self, object_id: str):
        obj = self._doc.find(str(object_id))
        if obj is None:
            ids = [o.id for o in self._doc.objects]
            raise ToolError(f"对象不存在：{object_id}。当前对象：{ids}")
        return obj

    def _place_and_add(self, obj, pos: Optional[_Pos], label: str,
                       anchor: str = "top-left") -> None:
        """放置新对象并加入文档（与 GUI 相同的入栈/刷新路径）。

        ``anchor`` 决定包围盒的哪一点落到 (x, y)——「往格子里写字」用
        ``bottom-left``/``baseline-left`` 可以直接贴住行格底线，不必先
        add 再 move（少一次调用，也不额外污染撤销历史）。
        """
        if pos is not None:
            page = self._page
            box = obj.local_bbox()
            if not box.is_empty:
                tx, ty = _page_top_left(page, pos)
                ax, ay = self._anchor_local(obj, box, anchor)
                obj.transform = AffineTransform.translate(tx - ax, ty - ay)
        else:
            self.win._place_object(obj)      # 自动错开放置，避免叠在一起
        self.win.controller.add_object(obj)
        self.win.canvas.sync_scene()

    def _anchor_local(self, obj, box: BBox, anchor: str) -> tuple[float, float]:
        """锚点在对象**本地坐标**（左下原点、y 向上）里的位置。"""
        fx, fy = _ANCHOR_FRACTIONS.get(anchor, (0.0, 0.0))
        ax = box.x0 + fx * box.width
        if fy < 0.0:                          # baseline-left：首行基线
            lay = obj.meta.get("layout")
            lines = getattr(lay, "lines", None)
            if lines:
                return box.x0, float(lines[0].baseline_y)
            fy = 1.0                          # 非文本对象退回底边
        ay = box.y1 - fy * box.height
        return ax, ay

    def _common_anchor(self, args: dict) -> str:
        anchor = args.get("anchor") or "top-left"
        if anchor not in _ANCHOR_FRACTIONS:
            raise ToolError(f"anchor 只能是 {_ANCHOR_HELP}，收到：{anchor!r}")
        return str(anchor)

    def _world_bbox(self, obj) -> BBox:
        return obj.world_bbox()

    def _moved_transform(self, obj, dx: float, dy: float) -> AffineTransform:
        t = obj.transform
        return AffineTransform(t.a, t.b, t.c, t.d, t.e + dx, t.f + dy)

    def _missing_of(self, obj) -> list[str]:
        """缺字列表：文本/markdown 记在 meta["layout"].missing 或
        meta["missing"]，TikZ 记在 meta["missing"]。

        Markdown 等对象的缺字此前从未上报（表格表头里的 kΩ 会静默变成
        k），现在渲染层统一汇总到 ``obj.meta["missing"]``。
        """
        lay = obj.meta.get("layout")
        missing = list(getattr(lay, "missing", []) or [])
        for c in (obj.meta.get("missing") or []):
            if c not in missing:
                missing.append(c)
        return missing

    def _obj_summary(self, obj) -> str:
        d = obj.source.data
        k = obj.source.kind
        if k in ("text", "markdown"):
            t = str(d.get("text", "")).strip().replace("\n", " ⏎ ")
            return t[:80]
        if k == "tikz":
            return str(d.get("source", "")).strip()[:80]
        if k == "equation":
            return str(d.get("latex", "")).strip()[:80]
        if k == "svg":
            return str(d.get("path", ""))
        return ""

    def _fits_note(self, box: BBox) -> dict:
        page = self._page
        out = (box.x0 < -_EPS or box.y0 < -_EPS
               or box.x1 > page.width + _EPS or box.y1 > page.height + _EPS)
        return {"fits_page": not out}

    # ================================================================ 查询
    def tool_get_page_info(self, args: dict) -> dict:
        p = self._page
        return {
            "width_mm": p.width, "height_mm": p.height,
            "margin_mm": p.margin, "preset_name": p.preset_name,
            "unit": "mm",
            "coords": "原点在页面左上角；x 向右、y 向下",
            "object_count": len(self._doc.objects),
            "current_file": self.win.project_path,
        }

    def tool_set_page(self, args: dict) -> dict:
        p = self._page
        old = PageSpec(p.width, p.height, p.margin, p.preset_name)
        w = _num(args.get("width"), "width", lo=20, hi=2000)
        h = _num(args.get("height"), "height", lo=20, hi=2000)
        m = args.get("margin")
        margin = _num(m, "margin", lo=0, hi=min(w, h) / 2) if m is not None else p.margin
        new = PageSpec(w, h, margin, "")
        self.win.controller.set_page(old, new, "AI：页面设置")
        return {"ok": True, "width_mm": w, "height_mm": h, "margin_mm": margin}

    def tool_list_fonts(self, args: dict) -> dict:
        """列出可用字体；``filter`` 按名称子串过滤，``kind`` 按类型过滤。

        字体多时（用户可能装着上百款）一次性返回大 JSON 很浪费上下文，
        因此支持过滤；不传参数则全量返回（带 total 计数）。
        """
        needle = args.get("filter")
        needle = str(needle).strip().lower() if needle else ""
        kind = args.get("kind")
        kind = str(kind) if kind else ""
        entries = self.win.font_manager.visible_entries()
        total = len(entries)
        fonts = []
        for e in entries:
            if kind and e.kind != kind:
                continue
            if needle and needle not in e.name.lower() \
                    and needle not in (e.display_name or "").lower():
                continue
            fonts.append({"name": e.name, "kind": e.kind,
                          "display_name": e.display_name})
        out = {"fonts": fonts, "total": total, "matched": len(fonts),
               "kinds": sorted({e.kind for e in entries}),
               "note": "font_names 是按优先级排列的回退链；一款中文字体"
                       " + 一款英文字体即可中英混排"}
        if len(fonts) < total:
            out["hint"] = "结果已过滤；去掉 filter/kind 可看全部字体"
        return out

    def tool_measure_text(self, args: dict) -> dict:
        """干跑测量：只算文字排版的尺寸，**不往文档里加东西**。

        在决定「这段文字放得下吗 / 该放哪儿」时用它先量一下，比先 add
        再 remove 省事，也不污染撤销历史（返回值与 add_text 同语义：
        页面左上原点、y 向下、mm）。
        """
        text = args.get("text")
        if not isinstance(text, str) or not text:
            raise ToolError("text 必须是非空字符串")
        font_names = self._fonts_by_names(args.get("font_names"))
        spec = TextSpec(
            text=text,
            font_names=font_names if font_names is not None
            else self.win._default_font_chain(),
            size=_num(args.get("size", 10.0), "size", lo=1.0, hi=100.0),
            line_spacing=_num(args.get("line_spacing", 1.4), "line_spacing",
                              lo=0.5, hi=5.0),
            char_spacing=_num(args.get("char_spacing", 0.0), "char_spacing",
                              lo=-10.0, hi=50.0),
            align=self._parse_align(args.get("align")),
            frame_width=_num(args.get("frame_width", 0.0), "frame_width",
                             lo=0.0, hi=2000.0),
            direction=self._parse_direction(args.get("direction")),
        )
        from ..fonts.builder import render_text, resolve_fonts
        fonts = resolve_fonts(spec, self.win.font_manager)
        if not fonts:
            raise ToolError("字体链里没有任何可用字体，请先用 list_fonts 查看")
        lay = render_text(spec, fonts)
        box = lay.bbox()
        lines = [{"text": ln.text if hasattr(ln, "text") else "",
                  "width_mm": round(ln.width, 3)} for ln in lay.lines]
        out = {
            "ok": True,
            "width_mm": round(box.width, 3),
            "height_mm": round(box.height, 3),
            "line_count": len(lay.lines),
            "lines": lines,
            # 内部坐标（左下原点）→ AI 语义（左上原点）：仅给相对量
            "first_baseline_from_top_mm": round(
                (box.y1 - lay.lines[0].baseline_y) if lay.lines else 0.0, 3),
        }
        if lay.missing:
            out["missing"] = list(lay.missing)
            out["warning"] = ("以下字符当前字体链画不出（会被留空）："
                              + "".join(lay.missing))
        return out

    def tool_list_objects(self, args: dict) -> dict:
        page = self._page
        objs = []
        for o in self._doc.objects:
            objs.append({
                "id": o.id, "name": o.name, "kind": o.source.kind,
                "visible": o.visible, "locked": o.locked,
                "bbox": _bbox_to_ai(page, self._world_bbox(o)),
                "summary": self._obj_summary(o),
            })
        return {"objects": objs}

    # ================================================================ 内容
    def _common_pos(self, args) -> Optional[_Pos]:
        return _Pos.parse(args, required=False)

    def tool_add_text(self, args: dict) -> dict:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("text 必须是非空字符串")
        font_names = self._fonts_by_names(args.get("font_names"))
        seed = args.get("seed")
        perturb = self._parse_perturb(args.get("perturb"), seed)
        spec = TextSpec(
            text=text,
            font_names=font_names if font_names is not None
            else self.win._default_font_chain(),
            size=_num(args.get("size", 10.0), "size", lo=1.0, hi=100.0),
            line_spacing=_num(args.get("line_spacing", 1.4), "line_spacing",
                              lo=0.5, hi=5.0),
            char_spacing=_num(args.get("char_spacing", 0.0), "char_spacing",
                              lo=-10.0, hi=50.0),
            align=self._parse_align(args.get("align")),
            frame_width=_num(args.get("frame_width", 0.0), "frame_width",
                             lo=0.0, hi=2000.0),
            perturb=perturb,
            direction=self._parse_direction(args.get("direction")),
        )
        if seed is not None:
            spec.font_seed = int(seed)
        obj = self._make_text(spec)
        if not obj.local_strokes:
            fonts = self.win.font_manager.visible_entries()
            raise ToolError(
                f"所选字体无法绘制该文本的任何字符（文本含 {len(spec.text)} 字符）。"
                f"可用字体：{[e.name for e in fonts]}")
        self._place_and_add(obj, self._common_pos(args), "添加文本",
                               anchor=self._common_anchor(args))
        return self._added_result(obj, missing=self._missing_of(obj))

    def _make_text(self, spec: TextSpec):
        from ..fonts.builder import make_text_object
        return make_text_object(spec, self.win.font_manager)

    def tool_add_markdown(self, args: dict) -> dict:
        from ..content.markdown import MarkdownStyle
        text = args.get("markdown")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("markdown 必须是非空字符串（支持 # 标题、列表、"
                            "**粗体**、| 表格 |、$公式$）")
        font_names = self._fonts_by_names(args.get("font_names"))
        style = MarkdownStyle(
            size=_num(args.get("size", 4.0), "size", lo=1.0, hi=50.0),
            wrap_width=_num(args.get("wrap_width", 120.0), "wrap_width",
                            lo=0.0, hi=2000.0),
            table_width=_num(args.get("table_width", 0.0), "table_width",
                             lo=0.0, hi=2000.0),
        )
        obj = make_markdown_object(
            text, self.win.font_manager, font_names=font_names, style=style,
            perturb=self._parse_perturb(args.get("perturb"),
                                        args.get("seed")))
        if not obj.local_strokes:
            raise ToolError("未能生成任何笔画：字体无法绘制该内容")
        self._place_and_add(obj, self._common_pos(args), "添加Markdown",
                               anchor=self._common_anchor(args))
        return self._added_result(obj, missing=self._missing_of(obj))

    def tool_add_tikz(self, args: dict) -> dict:
        code = args.get("code") or args.get("tikz")
        if not isinstance(code, str) or not code.strip():
            raise ToolError("code 必须是非空 TikZ 代码字符串（tikzpicture "
                            "环境可省略，直接写 \\draw 等命令）")
        if not tikz_available():
            from ..content.tikz import tikz_report
            rep = tikz_report()
            detail = "；".join(rep["notes"]) or "依赖探测未通过"
            tm = rep.get("texmf", {})
            extra = "".join(
                f" {k}={v['path']}({'存在' if v['exists'] else '不存在'})"
                for k, v in tm.items() if isinstance(v, dict))
            raise ToolError(
                f"TikZ 不可用：{detail}。环境自检（check_tex 工具可随时查看）："
                f"engine={rep['engine']}，converter={rep['converter']}，{extra}。"
                "隔离 HOME/自定义 TEXMFHOME 时个人 texmf 树与 xelatex.fmt 会"
                "查不到，需把 TEXMFHOME/TEXMFVAR/TEXMFCONFIG 指回真实 HOME。"
                "也可以改用 add_text/add_markdown/add_equation 完成排版")
        width = args.get("width_mm")
        width = _num(width, "width_mm", lo=5.0, hi=2000.0) if width is not None else None
        font_names = self._fonts_by_names(args.get("font_names"))
        try:
            obj = make_tikz_object(
                code, width, name=args.get("name") or None,
                font_names=font_names if font_names is not None
                else self.win._default_font_chain(),
                manager=self.win.font_manager,
                text_scale=_num(args.get("text_scale", 1.0), "text_scale",
                                lo=0.2, hi=5.0),
                perturb=self._parse_perturb(args.get("perturb"),
                                            args.get("seed")),
            )
        except ValueError as exc:
            from ..content.tikz import tikz_report
            notes = tikz_report()["notes"]
            hint = ("；环境提示：" + "；".join(notes)) if notes else ""
            raise ToolError(f"TikZ 编译失败：{exc}{hint}") from exc
        if not obj.local_strokes:
            raise ToolError("TikZ 编译成功但未产生任何笔画，请检查代码")
        self._place_and_add(obj, self._common_pos(args), "添加TikZ",
                               anchor=self._common_anchor(args))
        return self._added_result(obj, missing=self._missing_of(obj))

    def tool_add_equation(self, args: dict) -> dict:
        latex = args.get("latex")
        if not isinstance(latex, str) or not latex.strip():
            raise ToolError("latex 必须是非空 mathtext 公式字符串，如 "
                            r"$F = m a$")
        font_names = self._fonts_by_names(args.get("font_names"))
        try:
            obj = make_equation_object(
                latex, _num(args.get("size_mm", 6.0), "size_mm",
                            lo=1.0, hi=50.0),
                font_names=font_names if font_names is not None
                else self.win._default_font_chain(),
                manager=self.win.font_manager,
                perturb=self._parse_perturb(args.get("perturb"),
                                            args.get("seed")),
            )
        except Exception as exc:
            raise ToolError(f"公式渲染失败（需 matplotlib mathtext 语法）：{exc}")
        if not obj.local_strokes:
            raise ToolError("公式渲染成功但未产生任何笔画")
        self._place_and_add(obj, self._common_pos(args), "添加公式",
                               anchor=self._common_anchor(args))
        return self._added_result(obj, missing=self._missing_of(obj))

    def tool_update_text(self, args: dict) -> dict:
        obj = self._find(args.get("object_id"))
        if obj.source.kind != "text":
            raise ToolError(
                f"对象 {obj.id} 不是文本对象（kind={obj.source.kind}）；"
                "请删除后用对应的 add_* 工具重建")
        spec = TextSpec.from_data(obj.source.data)
        if "text" in args:
            if not isinstance(args["text"], str) or not args["text"].strip():
                raise ToolError("text 必须是非空字符串")
            spec.text = args["text"]
        if "font_names" in args:
            names = self._fonts_by_names(args["font_names"])
            if names:
                spec.font_names = names
        if "size" in args:
            spec.size = _num(args["size"], "size", lo=1.0, hi=100.0)
        if "line_spacing" in args:
            spec.line_spacing = _num(args["line_spacing"], "line_spacing",
                                     lo=0.5, hi=5.0)
        if "char_spacing" in args:
            spec.char_spacing = _num(args["char_spacing"], "char_spacing",
                                     lo=-10.0, hi=50.0)
        if "align" in args:
            spec.align = self._parse_align(args["align"])
        if "frame_width" in args:
            spec.frame_width = _num(args["frame_width"], "frame_width",
                                    lo=0.0, hi=2000.0)
        if "perturb" in args or "seed" in args:
            spec.perturb = self._parse_perturb(
                args.get("perturb", spec.perturb.to_data()), args.get("seed"))
        from ..ui.controller import snapshot_object
        old = snapshot_object(obj)
        update_text_object(obj, spec, self.win.font_manager)
        self.win.controller.replace_objects(
            [(obj, old, snapshot_object(obj))], f"AI：修改文本（{obj.name}）")
        self.win.canvas.sync_scene()
        return {"ok": True, "id": obj.id,
                "bbox": _bbox_to_ai(self._page, self._world_bbox(obj)),
                "missing": self._missing_of(obj)}

    def tool_move_object(self, args: dict) -> dict:
        obj = self._find(args.get("object_id"))
        if obj.locked:
            raise ToolError(f"对象 {obj.id} 已锁定，请先解锁")
        dx_arg, dy_arg = args.get("dx"), args.get("dy")
        if dx_arg is not None or dy_arg is not None:
            dx = _num(dx_arg, "dx") if dx_arg is not None else 0.0
            dy = _num(dy_arg, "dy") if dy_arg is not None else 0.0
            # AI 语义 y 向下、内部 y 向上：相对平移的 dy 取反
            dx_int, dy_int = dx, -dy
        else:
            pos = _Pos.parse(args, required=True)
            box = self._world_bbox(obj)
            if box.is_empty:
                raise ToolError(f"对象 {obj.id} 没有可见内容，无法定位")
            tx, ty = _page_top_left(self._page, pos)
            fx, fy = _ANCHOR_FRACTIONS.get(self._common_anchor(args), (0.0, 0.0))
            ax = box.x0 + fx * box.width
            ay = box.y1 - fy * box.height if fy >= 0.0 else box.y0
            dx_int, dy_int = tx - ax, ty - ay
        self.win.controller.set_transform(
            obj, self._moved_transform(obj, dx_int, dy_int),
            f"AI：移动（{obj.name}）")
        return {"ok": True, "id": obj.id,
                "bbox": _bbox_to_ai(self._page, self._world_bbox(obj))}

    def tool_transform_object(self, args: dict) -> dict:
        obj = self._find(args.get("object_id"))
        if obj.locked:
            raise ToolError(f"对象 {obj.id} 已锁定，请先解锁")
        scale = args.get("scale")
        rotate = args.get("rotate_deg")
        if scale is None and rotate is None:
            raise ToolError("请提供 scale（缩放倍数）和/或 rotate_deg（逆时针°）")
        box = self._world_bbox(obj)
        if box.is_empty:
            raise ToolError(f"对象 {obj.id} 没有可见内容，无法变换")
        c = box.center
        t = obj.transform
        if scale is not None:
            s = _num(scale, "scale", lo=0.01, hi=100.0)
            t = AffineTransform.scale_about(s, s, c) @ t
        if rotate is not None:
            t = AffineTransform.rotate_about(_num(rotate, "rotate_deg"), c) @ t
        self.win.controller.set_transform(
            obj, t, f"AI：变换（{obj.name}）")
        return {"ok": True, "id": obj.id,
                "bbox": _bbox_to_ai(self._page, self._world_bbox(obj))}

    def tool_remove_object(self, args: dict) -> dict:
        obj = self._find(args.get("object_id"))
        self.win.controller.remove_object(obj)
        return {"ok": True}

    def tool_clear_page(self, args: dict) -> dict:
        objs = list(self._doc.objects)
        if not objs:
            return {"ok": True, "removed": 0}
        self.win.controller.remove_objects(objs)
        return {"ok": True, "removed": len(objs)}

    # ============================================================ 文件
    def tool_open_project(self, args: dict) -> dict:
        path = str(args.get("path") or "")
        if not path:
            raise ToolError("path 不能为空（.wsproj 文件绝对路径）")
        p = Path(path).expanduser()
        if not p.exists():
            raise ToolError(f"文件不存在：{p}")
        if not self.win._load_project_path(str(p.resolve())):
            raise ToolError(f"打开失败：{p}（详见软件状态栏/日志）")
        return {"ok": True, "path": str(p),
                "object_count": len(self._doc.objects)}

    def tool_save_project(self, args: dict) -> dict:
        path = args.get("path") or self.win.project_path
        if not path:
            raise ToolError("当前文档尚未保存过，请提供 path（.wsproj 绝对路径）")
        p = Path(str(path)).expanduser().resolve()
        if p.suffix.lower() != ".wsproj":
            p = p.with_suffix(".wsproj")
        p.parent.mkdir(parents=True, exist_ok=True)
        # 与手动保存同一套保护：文件在别处被改过就拒绝——AI 不能替用户
        # 决定要不要覆盖别人写进去的内容，让用户在界面里选
        expect = None
        if self.win.project_path and Path(self.win.project_path) == p:
            expect = self.win._file_stamp
        try:
            save_project(str(p), self.win._collect_project(), expect=expect)
        except ExternalChangeError as exc:
            raise ToolError(
                f"文件已被外部修改：{p}（磁盘上那份 {exc.actual.mtime_text()}）。"
                "请在 WriterStudio 里保存并选择「覆盖保存 / 另存为 / 重新载入」。") from None
        self.win._set_current_path(str(p))    # 登记 + 刷新磁盘指纹
        self.win._mark_clean()
        return {"ok": True, "path": str(p),
                "object_count": len(self._doc.objects)}

    def tool_export_gcode(self, args: dict) -> dict:
        path = str(args.get("path") or "")
        if not path:
            raise ToolError("path 不能为空（.gcode 文件绝对路径）")
        if not self._doc.objects:
            raise ToolError("文档为空，没有可导出的内容")
        result = self.win._build_gcode()
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(result.text(), encoding="utf-8")
        return {
            "ok": True, "path": str(p),
            "lines": len(result.lines),
            "strokes": result.stroke_count,
            "draw_length_mm": round(result.draw_length, 1),
            "travel_length_mm": round(result.travel_length, 1),
            "estimated_seconds": round(result.estimated_seconds),
            "note": "G-code 已生成。发送到机器请在软件「机器」面板连接后"
                    "点「发送到机器」，发送前建议先用界面预览确认",
        }

    # ============================================================ 检查
    def tool_check_layout(self, args: dict) -> dict:
        page = self._page
        boxes = {}
        for o in self._doc.objects:
            if o.visible:
                b = self._world_bbox(o)
                if not b.is_empty:
                    boxes[o.id] = b
        issues = []
        margin_box = page.margin_bbox()
        for oid, b in boxes.items():
            out = (b.x0 < -_EPS or b.y0 < -_EPS
                   or b.x1 > page.width + _EPS or b.y1 > page.height + _EPS)
            if out:
                issues.append({
                    "type": "out_of_page", "severity": "error",
                    "object_ids": [oid],
                    "message": f"对象 {oid} 超出页面范围",
                    "bbox": _bbox_to_ai(page, b),
                })
                continue
            if (b.x0 < margin_box.x0 - _EPS or b.y0 < margin_box.y0 - _EPS
                    or b.x1 > margin_box.x1 + _EPS
                    or b.y1 > margin_box.y1 + _EPS):
                issues.append({
                    "type": "crosses_margin", "severity": "warning",
                    "object_ids": [oid],
                    "message": f"对象 {oid} 越过页边距（边距 "
                               f"{page.margin}mm），打印机物理边界可能裁切",
                    "bbox": _bbox_to_ai(page, b),
                })
        ids = sorted(boxes)
        for i, a in enumerate(ids):
            for b_id in ids[i + 1:]:
                inter = _bbox_intersection(boxes[a], boxes[b_id])
                if inter is None:
                    continue
                area = inter.width * inter.height
                if area > _OVERLAP_AREA:
                    objs = {o.id: o for o in self._doc.objects}
                    issues.append({
                        "type": "overlap", "severity": "warning",
                        "object_ids": [a, b_id],
                        "message": (
                            f"对象 {a}（{self._obj_summary(objs[a])[:24]}）"
                            f"与 {b_id}（{self._obj_summary(objs[b_id])[:24]}）"
                            f"重叠约 {area:.0f} mm²"),
                        # 相交矩形：程序化修版（挪谁、挪多少）直接用它算，
                        # 不必自己再解析两个 bbox
                        "overlap_bbox": _bbox_to_ai(page, inter),
                        "overlap_area_mm2": round(area, 1),
                        "bbox": {a: _bbox_to_ai(page, boxes[a]),
                                 b_id: _bbox_to_ai(page, boxes[b_id])},
                    })
        return {
            "page": {"width_mm": page.width, "height_mm": page.height,
                     "margin_mm": page.margin},
            "objects": {oid: _bbox_to_ai(page, b) for oid, b in boxes.items()},
            "issues": issues,
            "ok": not any(i["severity"] == "error" for i in issues),
        }

    def tool_render_preview(self, args: dict) -> dict:
        """整页渲染为 PNG。

        ``include_references=true`` 时把参考图层（底图）按各自透明度一并
        画出——套打表单时用它核对内容与底图是否对齐（不开启时只画书写
        内容，保持与输出一致）。
        """
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPainterPath

        page = self._page
        width_px = int(_num(args.get("width_px", 1024), "width_px",
                            lo=200, hi=4000))
        scale = width_px / page.width
        height_px = max(1, int(round(page.height * scale)))
        img = QImage(width_px, height_px, QImage.Format_RGB32)
        img.fill(QColor("#ffffff"))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        # 页面边框 + 页边距参考线（AI 坐标系一致：左上原点、y 向下）
        painter.setPen(QPen(QColor("#999999"), 1.0))
        painter.drawRect(QRectF(0.5, 0.5, width_px - 1, height_px - 1))
        m = page.margin * scale
        if m > 2:
            painter.setPen(QPen(QColor("#dddddd"), 1.0, Qt.DashLine))
            painter.drawRect(QRectF(m, m, width_px - 2 * m, height_px - 2 * m))
        refs_drawn = 0
        if args.get("include_references"):
            self._paint_references(painter, page, scale)
            refs_drawn = sum(1 for r in self._doc.references
                             if r.visible and r.path)
        painter.setPen(QPen(QColor("#111111"), 0.35 * scale))
        painter.setBrush(Qt.NoBrush)
        path = QPainterPath()
        for o in self._doc.objects:
            if not o.visible:
                continue
            for s in o.world_strokes():
                pts = s.points
                if not pts:
                    continue
                path.moveTo(pts[0][0] * scale,
                            (page.height - pts[0][1]) * scale)
                for x, y in pts[1:]:
                    path.lineTo(x * scale, (page.height - y) * scale)
                if s.closed:
                    path.closeSubpath()
        painter.drawPath(path)
        painter.end()
        png = _qimage_to_png(img)
        return {
            "ok": True,
            "format": "png",
            "width_px": width_px, "height_px": height_px,
            "png_base64": base64.b64encode(png).decode("ascii"),
            "page": {"width_mm": page.width, "height_mm": page.height},
            "references_drawn": refs_drawn,
        }

    def _paint_references(self, painter, page, scale: float) -> None:
        """把可见参考图按各自透明度画到预览图上（变换与画布一致）。"""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import (
            QImage,
            QImageReader,
            QPainter,
            QTransform,
        )

        for ref in self._doc.references:
            if not ref.visible or not ref.path:
                continue
            img = None
            try:
                if ref.kind == "svg":
                    from PySide6.QtSvg import QSvgRenderer
                    r = QSvgRenderer(ref.path)
                    if r.isValid():
                        w = max(1, int(ref.width_mm * scale))
                        h = max(1, int(ref.height_mm * scale))
                        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
                        img.fill(0)
                        p = QPainter(img)
                        r.render(p)
                        p.end()
                else:
                    reader = QImageReader(ref.path)
                    reader.setAutoTransform(True)
                    img = reader.read()
            except Exception:
                img = None
            if img is None or img.isNull():
                continue
            t = ref.transform
            # 本地(mm, y 向上) → 页面(mm) → 图像像素(y 向下)，两步合成
            tr = QTransform(scale * t.a, -scale * t.b,
                            scale * t.c, -scale * t.d,
                            scale * t.e,
                            scale * (page.height - t.f))
            painter.save()
            painter.setOpacity(max(0.0, min(1.0, float(ref.opacity))))
            painter.setTransform(tr)
            painter.drawImage(QRectF(0.0, 0.0, ref.width_mm, ref.height_mm), img)
            painter.restore()

    def tool_check_tex(self, args: dict) -> dict:
        """TeX/TikZ 环境自检：引擎、转换器、宏包与 texmf 路径。

        add_tikz 报错时先跑这个——它会指出到底是没装、还是隔离 HOME
        导致个人 texmf 树 / xelatex 格式文件找不到。
        """
        from ..content.tikz import tikz_report
        rep = tikz_report()
        out = {"ok": rep["ok"], "engine": rep["engine"],
               "converter": rep["converter"], "notes": rep["notes"],
               "texmf": rep["texmf"],
               "hint": "ok=true 时 add_tikz 可用；notes 里逐条给出缺什么/该设哪个变量"}
        return out

    # ============================================================ 解析
    def _parse_align(self, v) -> str:
        if v is None:
            return ALIGN_LEFT
        if v in (ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT):
            return v
        raise ToolError(f"align 只能是 left/center/right，收到：{v!r}")

    def _parse_direction(self, v) -> str:
        if v is None:
            from ..fonts.layout import DIR_H
            return DIR_H
        if v in ("h", "v-rl", "v-lr"):
            return v
        raise ToolError("direction 只能是 h（横排）/ v-rl（竖排右→左）/ "
                        "v-lr（竖排左→右），收到：" + repr(v))

    def _parse_perturb(self, raw, seed) -> PerturbParams:
        if raw is None:
            return PerturbParams()
        if not isinstance(raw, dict):
            raise ToolError("perturb 必须是参数字典，如 "
                            '{"enabled": true, "size_sigma": 0.03, '
                            '"baseline_amplitude": 0.4}')
        p = PerturbParams.from_data(raw)
        if seed is not None:
            try:
                p.seed = int(seed)
            except (TypeError, ValueError):
                raise ToolError("seed 必须是整数")
        return p

    def _added_result(self, obj, *, missing: Optional[list[str]] = None) -> dict:
        page = self._page
        box = self._world_bbox(obj)
        out = {
            "ok": True, "id": obj.id, "name": obj.name,
            "kind": obj.source.kind,
            "bbox": _bbox_to_ai(page, box),
            **self._fits_note(box),
        }
        if missing:
            out["missing"] = missing
            out["warning"] = (
                f"以下字符当前字体链无法绘制，已留空：{''.join(missing)}。"
                "请换用包含这些字符的字体（见 list_fonts）")
        return out


def _qimage_to_png(img) -> bytes:
    """QImage → PNG 字节流（QBuffer 写入）。"""
    from PySide6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())
