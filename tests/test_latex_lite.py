"""LaTeX 文档零依赖回退（latex_lite 转译 + 内置渲染）测试。"""

from __future__ import annotations

from writerstudio.content.latex_lite import render_latex_native, transpile_latex
from writerstudio.fonts.model import FontFamily, Glyph

_DOC = r"""\documentclass[12pt]{article}
\usepackage{amsmath}
\title{Test Report}
\author{Alice}
\begin{document}
\maketitle
\section{Principle}
Given sides $a, b$ and hypotenuse $c$:
\[ a^2 + b^2 = c^2 \]
\subsection{Data} % comment
\begin{itemize}
  \item voltage 3.3V
  \item current \textbf{1.2A}
\end{itemize}
\begin{tabular}{|l|c|}
\hline
item & value \\
\hline
resistor & 100 \\
\hline
\end{tabular}
\begin{equation}
E = mc^2 \label{eq:mass}
\end{equation}
20\% escaped and 100\% fine.
\end{document}
"""


def _stub_fonts(text: str = "abcdefghijklmnopqrstuvwxyz"):
    def glyph(ch):
        return Glyph(ch, [[(0, 0), (0, 6), (4, 6), (4, 0)]], advance=5)
    return [FontFamily(name="t", kind="hershey", units_per_em=10.0,
                       glyphs={c: glyph(c) for c in text})]


class _StubManager:
    """最小 FontManager 桩：_resolve 只用到 get/entries/get_entry。"""

    def __init__(self, fonts):
        self._fonts = {f.name: f for f in fonts}

    def entries(self):
        return list(self._fonts.values())

    def get_entry(self, name):
        return self._fonts.get(name)

    def get(self, name):
        return self._fonts.get(name)


# --------------------------------------------------------------- 转译
def test_transpile_headings_and_title():
    md, warns = transpile_latex(_DOC)
    assert "# Test Report" in md
    assert "Alice" in md
    assert "# Principle" in md
    assert "## Data" in md
    assert "\\section" not in md
    assert "\\documentclass" not in md
    assert warns == []


def test_transpile_inline_and_display_math():
    md, _ = transpile_latex(_DOC)
    assert "$a, b$" in md
    assert "$a^2 + b^2 = c^2$" in md
    assert "$E = mc^2$" in md          # equation 环境，\label 被丢弃
    assert "\\[" not in md and "\\]" not in md


def test_transpile_lists_and_tabular():
    md, warns = transpile_latex(_DOC)
    assert "- voltage 3.3V" in md
    assert "- current 1.2A" in md      # \textbf 去壳
    assert "| item | value |" in md
    assert "| resistor | 100 |" in md
    assert "\\hline" not in md and "&" not in md
    assert warns == []


def test_transpile_escapes_and_unknown_commands():
    md, warns = transpile_latex(_DOC)
    assert "20% escaped and 100% fine" in md
    assert warns == []
    md2, warns2 = transpile_latex(r"Unknown \unknowncmd{kept} here")
    assert "kept" in md2
    assert any("unknowncmd" in w for w in warns2)


def test_transpile_without_document_env():
    md, warns = transpile_latex(r"纯文本 \textbf{加粗} $x^2$ 结束")
    assert "纯文本 加粗 $x^2$ 结束" in md
    assert warns == []


def test_transpile_linebreaks_split_paragraphs():
    md, _ = transpile_latex("第一行\\\\第二行")
    assert "第一行" in md and "第二行" in md


# --------------------------------------------------------------- 渲染
def test_render_native_produces_strokes():
    r = render_latex_native(r"\section{Hello} world end", _stub_fonts())
    assert r.ok and r.native
    assert r.strokes
    assert r.width > 0 and r.height > 0


def test_render_native_bad_document_still_ok_or_fails_gracefully():
    r = render_latex_native(r"\begin{document}\end{document}", _stub_fonts())
    assert not r.ok                     # 空文档：不产出笔画，log 说明原因


# ------------------------------------------------------- 文档渲染回退
def test_render_latex_document_falls_back_without_tex(monkeypatch):
    from writerstudio.content import latex_doc
    monkeypatch.setattr(latex_doc, "find_engine_for", lambda s, force=False:
                        (None, False))
    monkeypatch.setattr(latex_doc, "find_pdf_converter", lambda: None)
    r = latex_doc.render_latex_document(
        r"\section{Hello} world", fonts=_stub_fonts())
    assert r.ok and r.native
    assert "内置排版引擎" in r.log
    assert r.strokes


def test_render_latex_document_fallback_failure_keeps_tex_hints(monkeypatch):
    from writerstudio.content import latex_doc
    monkeypatch.setattr(latex_doc, "find_engine_for", lambda s, force=False:
                        (None, False))
    monkeypatch.setattr(latex_doc, "find_pdf_converter", lambda: None)
    r = latex_doc.render_latex_document(
        "\\begin{document}\\end{document}", fonts=_stub_fonts())
    assert not r.ok
    assert "texlive" in r.log           # 保留 TeX 安装指引


def test_make_latex_object_native_path(monkeypatch):
    """无 TeX 时 make_latex_object 走回退并把回退标记/字体链写进 data。"""
    from writerstudio.content.builder import (
        SOURCE_LATEX,
        _build_content,
        make_latex_object,
    )
    from writerstudio.content import latex_doc
    monkeypatch.setattr(latex_doc, "find_engine_for", lambda s, force=False:
                        (None, False))
    monkeypatch.setattr(latex_doc, "find_pdf_converter", lambda: None)
    manager = _StubManager(_stub_fonts())

    obj = make_latex_object(r"\section{Hello} world",
                            manager=manager, font_names=["t"])
    assert obj.local_strokes
    d = obj.source.data
    assert d["tex_native"] is True
    assert d["font_names"] == ["t"]

    # 全量重建路径：字体链/回退标记透传
    obj2 = _build_content(SOURCE_LATEX, dict(d), "r", manager=manager)
    assert obj2 is not None and obj2.local_strokes
    assert obj2.source.data["font_names"] == ["t"]
