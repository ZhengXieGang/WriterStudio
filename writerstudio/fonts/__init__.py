"""字体系统：字形模型、多种字库解析、字体管理、文本排版。"""

from .builder import (
    SOURCE_KIND_TEXT,
    TextSpec,
    make_text_object,
    regenerate_text_object,
    render_text,
    render_text_strokes,
    resolve_fonts,
    set_perturb,
    update_text_object,
)
from .gcode_lib import parse_gcode_char, parse_gcode_char_dir
from .gfont import (
    KIND_GFONT,
    font_names_from_gfont,
    parse_gfont,
    read_gfont_records,
)
from .hershey import load_hershey_dir, parse_hershey_jhf
from .layout import (
    ALIGN_CENTER,
    ALIGN_LEFT,
    ALIGN_RIGHT,
    DEFAULT_FONT_SEED,
    CharPlacement,
    LineLayout,
    TextLayout,
    TextStyle,
    layout_text,
)
from .manager import FontEntry, FontManager
from .model import (
    KIND_GCODE,
    KIND_HERSHEY,
    KIND_STROKE_JSON,
    KIND_TRUETYPE,
    FontFamily,
    Glyph,
    glyphs_bbox,
)
from .stroke_json import load_stroke_json_dir, parse_stroke_json
from .truetype import load_truetype_dir, parse_truetype

__all__ = [
    "Glyph",
    "FontFamily",
    "FontEntry",
    "FontManager",
    "glyphs_bbox",
    "KIND_HERSHEY",
    "KIND_GCODE",
    "KIND_STROKE_JSON",
    "KIND_TRUETYPE",
    "KIND_GFONT",
    "TextStyle",
    "TextLayout",
    "LineLayout",
    "CharPlacement",
    "layout_text",
    "ALIGN_LEFT",
    "ALIGN_CENTER",
    "ALIGN_RIGHT",
    "DEFAULT_FONT_SEED",
    "TextSpec",
    "SOURCE_KIND_TEXT",
    "make_text_object",
    "update_text_object",
    "regenerate_text_object",
    "set_perturb",
    "resolve_fonts",
    "render_text",
    "render_text_strokes",
    "parse_hershey_jhf",
    "parse_stroke_json",
    "parse_truetype",
    "parse_gfont",
    "read_gfont_records",
    "font_names_from_gfont",
    "parse_gcode_char",
    "parse_gcode_char_dir",
    "load_hershey_dir",
    "load_stroke_json_dir",
    "load_truetype_dir",
]
