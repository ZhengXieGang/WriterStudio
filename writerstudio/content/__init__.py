"""富内容：Markdown（含表格）、LaTeX 公式/文档、SVG 矢量图导入。"""

from .builder import (
    SOURCE_EQUATION,
    SOURCE_LATEX,
    SOURCE_MARKDOWN,
    SOURCE_SVG,
    SOURCE_TIKZ,
    make_equation_object,
    make_latex_object,
    make_markdown_object,
    make_svg_object,
    make_tikz_object,
    regenerate_content_object,
)
from .equation import (
    equation_bbox,
    mathtext_available,
    render_equation,
    wrap_math,
)
from .latex_doc import (
    LatexResult,
    compile_tex_source,
    find_engine,
    find_pdf_converter,
    latex_toolchain_available,
    render_latex_document,
    toolchain_report,
)
from .markdown import MarkdownResult, MarkdownStyle, render_markdown
from .svg_import import SVGImportResult, import_svg, svg_available
from .tikz import TIKZ_SAMPLE, render_tikz, tikz_available, tikz_report

__all__ = [
    "render_markdown",
    "MarkdownStyle",
    "MarkdownResult",
    "render_equation",
    "equation_bbox",
    "wrap_math",
    "mathtext_available",
    "import_svg",
    "SVGImportResult",
    "svg_available",
    "render_latex_document",
    "compile_tex_source",
    "LatexResult",
    "latex_toolchain_available",
    "find_engine",
    "find_pdf_converter",
    "toolchain_report",
    "render_tikz",
    "tikz_available",
    "tikz_report",
    "TIKZ_SAMPLE",
    "SOURCE_MARKDOWN",
    "SOURCE_EQUATION",
    "SOURCE_LATEX",
    "SOURCE_SVG",
    "SOURCE_TIKZ",
    "make_markdown_object",
    "make_equation_object",
    "make_latex_object",
    "make_svg_object",
    "make_tikz_object",
    "regenerate_content_object",
]
