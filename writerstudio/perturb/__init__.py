"""手写扰动引擎：让机器书写/绘制更接近真人手笔。

    * :mod:`params`  —— 参数模型与预设
    * :mod:`engine`  —— 字符级/行级/正弦基线/笔画级扰动 + 通用线抖动
"""

from .apply import apply_perturb, resolve_perturb, supports_perturb
from .engine import (
    CharPerturb,
    PerturbResult,
    perturb_layout,
    perturb_strokes,
    smooth_stroke,
    wobble_strokes,
)
from .params import PRESETS, PerturbParams

__all__ = [
    "PerturbParams",
    "PRESETS",
    "PerturbResult",
    "CharPerturb",
    "perturb_layout",
    "perturb_strokes",
    "smooth_stroke",
    "wobble_strokes",
    "apply_perturb",
    "resolve_perturb",
    "supports_perturb",
]
