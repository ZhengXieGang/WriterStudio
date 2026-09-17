"""把文本构建为文档对象（连接字体系统、扰动引擎与文档模型）。

对象本地坐标以排版起点 (0,0) 为基准，页面位置由对象的 ``transform`` 决定，
从而「内容编辑」（改文字/字体/扰动）与「自由编辑」（移动/缩放）互不干扰。

渲染管道：``TextSpec`` → 排版(``layout_text``) → 手写扰动(``perturb_layout``) → 笔画。
``TextSpec.perturb`` 为空或未启用时，直接使用排版笔画（即 P2 的纯净输出）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from ..core.document import DocumentObject, SourceSpec
from ..core.strokes import Stroke
from ..perturb.engine import PerturbResult, perturb_layout
from ..perturb.params import PerturbParams
from .layout import (ALIGN_LEFT, DEFAULT_FONT_SEED, DIR_H, TextLayout,
                     TextStyle, frame_box, layout_text)
from .manager import FontManager
from .model import FontFamily

SOURCE_KIND_TEXT = "text"


@dataclass
class TextSpec:
    """文本对象的源参数（可序列化，供撤销/存档/重新生成）。"""

    text: str = ""
    font_names: list[str] = field(default_factory=list)  # 回退链（按优先级）
    size: float = 10.0
    line_spacing: float = 1.4
    char_spacing: float = 0.0
    align: str = ALIGN_LEFT
    perturb: PerturbParams = field(default_factory=PerturbParams)
    # -- 多字体高级选项 --
    font_weights: dict[str, float] = field(default_factory=dict)  # 字体名 → 权重
    random_fonts: bool = False          # 同可绘字体间按权重随机
    fallback_font: str = ""             # 兜底字体（链中全缺该字时使用）
    font_seed: int = DEFAULT_FONT_SEED  # 字体随机分配种子（可重摇）
    # -- 书写方向与词距 --
    direction: str = DIR_H              # h=横向 / v-rl=竖排右→左 / v-lr=竖排左→右
    word_space: float = 100.0           # 空格宽度百分比（100=字体步距）
    word_space_random: float = 0.0      # 空格宽度随机幅度（±%）
    # 文本框宽度(mm)：>0 时按宽度自动换行；0 = 不限宽。画布上的文本框
    # 右缘手柄与属性面板都会改这个值。
    frame_width: float = 0.0
    # 逐字符字体覆盖：字符 → {"font": 字体名, "scale": 可选缩放}。
    # 为指定字符设置另一套字体（如个别符号用符号字体），排版时最优先。
    char_overrides: dict[str, dict] = field(default_factory=dict)

    def to_style(self) -> TextStyle:
        return TextStyle(size=self.size, line_spacing=self.line_spacing,
                         char_spacing=self.char_spacing, align=self.align,
                         random_fonts=self.random_fonts,
                         font_weights=dict(self.font_weights),
                         direction=self.direction,
                         word_space=self.word_space,
                         word_space_random=self.word_space_random,
                         frame_width=self.frame_width,
                         char_overrides={k: dict(v)
                                         for k, v in self.char_overrides.items()})

    def clone(self) -> TextSpec:
        # 关键字构造：字段众多且含列表/字典，位置传参一旦与 dataclass
        # 字段顺序错位就会静默串位
        return TextSpec(
            text=self.text,
            font_names=list(self.font_names),
            size=self.size,
            line_spacing=self.line_spacing,
            char_spacing=self.char_spacing,
            align=self.align,
            perturb=self.perturb.clone(),
            font_weights=dict(self.font_weights),
            random_fonts=self.random_fonts,
            fallback_font=self.fallback_font,
            font_seed=self.font_seed,
            direction=self.direction,
            word_space=self.word_space,
            word_space_random=self.word_space_random,
            frame_width=self.frame_width,
            char_overrides={k: dict(v) for k, v in self.char_overrides.items()},
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "text": self.text, "font_names": list(self.font_names),
            "size": self.size, "line_spacing": self.line_spacing,
            "char_spacing": self.char_spacing, "align": self.align,
            "perturb": self.perturb.to_data(),
            "font_weights": dict(self.font_weights),
            "random_fonts": bool(self.random_fonts),
            "fallback_font": self.fallback_font,
            "font_seed": int(self.font_seed),
            "direction": self.direction,
            "word_space": float(self.word_space),
            "word_space_random": float(self.word_space_random),
            "frame_width": float(self.frame_width),
            "char_overrides": {k: dict(v)
                               for k, v in self.char_overrides.items()},
        }

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> TextSpec:
        raw_weights = data.get("font_weights") or {}
        weights = {}
        if isinstance(raw_weights, dict):
            for k, v in raw_weights.items():
                try:
                    weights[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        raw_overrides = data.get("char_overrides") or {}
        overrides: dict[str, dict] = {}
        if isinstance(raw_overrides, dict):
            for k, v in raw_overrides.items():
                if isinstance(v, dict) and v.get("font"):
                    overrides[str(k)] = dict(v)
        return cls(
            text=data.get("text", ""),
            font_names=list(data.get("font_names", [])),
            size=float(data.get("size", 10.0)),
            line_spacing=float(data.get("line_spacing", 1.4)),
            char_spacing=float(data.get("char_spacing", 0.0)),
            align=data.get("align", ALIGN_LEFT),
            perturb=PerturbParams.from_data(data.get("perturb")),
            font_weights=weights,
            random_fonts=bool(data.get("random_fonts", False)),
            fallback_font=str(data.get("fallback_font", "") or ""),
            font_seed=int(data.get("font_seed", DEFAULT_FONT_SEED)),
            direction=str(data.get("direction", DIR_H) or DIR_H),
            word_space=float(data.get("word_space", 100.0)),
            word_space_random=float(data.get("word_space_random", 0.0)),
            frame_width=float(data.get("frame_width", 0.0) or 0.0),
            char_overrides=overrides,
        )


def resolve_fonts(spec: TextSpec, manager: FontManager) -> list[FontFamily]:
    """按回退链顺序解析字体；忽略加载失败的项。

    兜底字体（``fallback_font``）若不在链中，会追加到末尾作为最后候选——
    这样「链内互相兜底 + 兜底字体保底」两者同时生效。
    逐字符覆盖（``char_overrides``）用到的字体若不在链中，也追加到
    最末尾：``_pick_font`` 对覆盖字符按名字直取，追加不影响其他字符
    的正常回退顺序。
    """
    names = list(spec.font_names)
    if spec.fallback_font and spec.fallback_font not in names:
        names.append(spec.fallback_font)
    for ov in spec.char_overrides.values():
        n = (ov or {}).get("font")
        if n and n not in names:
            names.append(n)
    fonts: list[FontFamily] = []
    for name in names:
        fam = manager.get(name)
        if fam is not None:
            fonts.append(fam)
    return fonts


def render_text(spec: TextSpec, fonts: Sequence[FontFamily]) -> TextLayout:
    """仅排版（不含扰动）——用于预览与度量。"""
    return layout_text(spec.text, fonts, spec.to_style(), origin=(0.0, 0.0),
                       seed=spec.font_seed)


def render_text_strokes(spec: TextSpec, fonts: Sequence[FontFamily]
                        ) -> tuple[list[Stroke], TextLayout, Optional[PerturbResult]]:
    """排版 + 扰动，返回 (笔画, 排版结果, 扰动结果)。"""
    lay = render_text(spec, fonts)
    if spec.perturb.is_active():
        pr = perturb_layout(lay, spec.perturb)
        return pr.strokes, lay, pr
    return lay.strokes(), lay, None


def _apply_render(obj: DocumentObject, spec: TextSpec,
                  fonts: Sequence[FontFamily]) -> None:
    strokes, lay, pr = render_text_strokes(spec, fonts)
    # 手工删除/改动的笔画记录在 source.data 的编辑层里，重排版后需重新施加，
    # 否则一改扰动参数被删的笔画又冒出来（详见 content/stroke_edits.py）
    from ..content.stroke_edits import apply_edits
    obj.local_strokes = apply_edits(strokes, obj.source.data)
    obj.meta["layout"] = lay
    obj.meta["perturb"] = pr
    # 文本框几何持久化进 source.data（参与序列化）：meta 不入库，重开文档
    # 时若重新生成失败（缺字体等），画布的文本框/调整框仍能恢复原大小，
    # 不会塌到「无排版结果」的兜底框。渲染失败产出空笔画时保留旧值。
    if obj.local_strokes:
        f = frame_box(lay)
        if f is not None:
            obj.source.data["layout_box"] = [float(v) for v in f]


def make_text_object(spec: TextSpec, manager: FontManager,
                     name: Optional[str] = None) -> DocumentObject:
    """用字体管理器构建文本对象（源内容已记录，可重复重新生成）。"""
    fonts = resolve_fonts(spec, manager)
    obj = DocumentObject(
        name=name or _default_name(spec),
        source=SourceSpec(SOURCE_KIND_TEXT, spec.to_data()),
    )
    _apply_render(obj, spec, fonts)
    return obj


def regenerate_text_object(obj: DocumentObject, manager: FontManager) -> bool:
    """按对象当前的源内容重新生成笔画，保留其 transform。返回是否成功。"""
    if obj.source.kind != SOURCE_KIND_TEXT:
        return False
    spec = TextSpec.from_data(obj.source.data)
    fonts = resolve_fonts(spec, manager)
    _apply_render(obj, spec, fonts)
    return True


def update_text_object(obj: DocumentObject, spec: TextSpec,
                       manager: FontManager) -> None:
    """更新源内容并重新生成（源与笔画同时刷新）。

    文字内容没变（只改字体/字号/字距等）时保留手工笔画编辑层——否则用户
    删掉的笔画会在改字号后又冒出来。文字本身变了则丢弃编辑层（下标已错位，
    沿用会删错笔画）。
    """
    from ..content.stroke_edits import KEY as _EDITS_KEY
    old_data = obj.source.data
    keep_edits = (old_data.get(_EDITS_KEY)
                  if old_data.get("text", "") == spec.text else None)
    obj.source = SourceSpec(SOURCE_KIND_TEXT, spec.to_data())
    if keep_edits:
        obj.source.data[_EDITS_KEY] = keep_edits
    obj.name = _default_name(spec)
    fonts = resolve_fonts(spec, manager)
    _apply_render(obj, spec, fonts)


def set_perturb(obj: DocumentObject, params: PerturbParams,
                manager: FontManager) -> bool:
    """仅更新扰动参数并重新渲染（保留文字/字体/transform）。"""
    if obj.source.kind != SOURCE_KIND_TEXT:
        return False
    spec = TextSpec.from_data(obj.source.data)
    spec.perturb = params.clone()
    obj.source.data["perturb"] = spec.perturb.to_data()
    fonts = resolve_fonts(spec, manager)
    _apply_render(obj, spec, fonts)
    return True


def _default_name(spec: TextSpec) -> str:
    first = spec.text.strip().split("\n", 1)[0]
    if len(first) > 12:
        first = first[:12] + "…"
    return f"文本：{first}" if first else "文本"
