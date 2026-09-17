"""完整 LaTeX 文档渲染（外部 TeX 工具链）。

流程：``.tex`` → ``xelatex`` 生成 PDF → ``pdftocairo -svg`` 转 SVG →
用 :mod:`svg_import` 解析为笔画。

相比 :mod:`equation`（mathtext，免装 TeX，适合单条公式），本模块用于
**整段 LaTeX 文档**：复杂宏包、自定义命令、化学式、表格环境等。

依赖外部命令：``xelatex``（或 ``pdflatex``）与 ``pdftocairo``/``dvisvgm``。
工具缺失时 :func:`render_latex_document` 自动回退到 :mod:`latex_lite`
的内置渲染（常用子集，零依赖）；TikZ 无法回退，仍要求 TeX。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from ..core.geometry import BBox
from ..core.strokes import Stroke
from .svg_import import import_svg
from .svg_import import _PT_TO_PX

ENGINES = ("xelatex", "pdflatex", "lualatex")
XETEX_ENGINES = ("xelatex", "lualatex")
PDF_TO_SVG = ("pdftocairo", "dvisvgm")

# 仅 XeTeX/LuaTeX 支持的字体用法。用 pdfLaTeX 编译这些写法会去找 TFM 字体
# 并报「Metric (TFM) file not found」，非常费解——须改派 xe/lua 引擎。
_XETEX_PATTERNS = (
    re.compile(r"\\font\s*\\[A-Za-z@]+\s*=\s*[\"']"),   # \font\zhA="Noto Sans CJK HK"
    re.compile(r"\\usepackage(?:\[[^\]]*\])?\{fontspec\}"),
    re.compile(r"\\usepackage(?:\[[^\]]*\])?\{xeCJK\}"),
    re.compile(r"\\usepackage(?:\[[^\]]*\])?\{unicode-math\}"),
    re.compile(r"\\usepackage(?:\[[^\]]*\])?\{polyglossia\}"),
    re.compile(r"\\set(?:main|sans|mono)font\b"),
    re.compile(r"\\newfontfamily\b"),
    re.compile(r"\\setCJK(?:main|sans|mono)font\b"),
    # TeX 魔法注释（编辑器据此选引擎，我们也照做）
    re.compile(r"%\s*!TeX\s+program\s*=\s*(xelatex|lualatex)", re.I),
    re.compile(r"%\s*!TEX\s+(?:TS-)?program\s*=\s*(xelatex|lualatex)", re.I),
)


def source_needs_xetex(source: str) -> bool:
    """源码是否含仅 XeTeX/LuaTeX 支持的字体用法（须用 xe/lua 引擎编译）。"""
    return any(p.search(source) for p in _XETEX_PATTERNS)


@dataclass
class LatexResult:
    strokes: list[Stroke] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    log: str = ""
    ok: bool = False
    #: 文字替换模式下字体链画不出的字符（TikZ 用），供界面提示
    missing_chars: list[str] = field(default_factory=list)
    #: True = 无 TeX 工具链时由内置排版引擎（latex_lite）渲染的常用子集
    native: bool = False

    def bbox(self) -> BBox:
        return BBox.from_points(p for s in self.strokes for p in s.points)


_PROBE_CACHE: dict[str, bool] = {}


def _engine_works(engine: str) -> bool:
    """某引擎能否实际编译最小文档（结果缓存）。"""
    if engine not in _PROBE_CACHE:
        _PROBE_CACHE[engine] = bool(shutil.which(engine)) and _probe_engine(engine)
    return _PROBE_CACHE[engine]


def find_engine(require_xetex: bool = False) -> Optional[str]:
    """返回**可实际编译**的 LaTeX 引擎（会做一次试编译探测并缓存）。

    仅检查二进制存在是不够的：常见情况是装了 texlive-bin 却缺 latex 格式文件
    （``latex.ltx`` 缺失，需 ``texlive-latex``）或基础字体（Latin Modern，
    需 ``texlive-fontsrecommended``），此时引擎会立刻报错。因此这里做一次
    真实的最小文档试编译。

    ``require_xetex=True`` 时只在 XeTeX/LuaTeX 引擎中挑选（供含 XeTeX 专属
    字体语法的文档使用；pdflatex 无法解析这类写法）。
    """
    candidates = XETEX_ENGINES if require_xetex else ENGINES
    for e in candidates:
        if _engine_works(e):
            return e
    return None


def find_engine_for(source: str, force_xetex: bool = False
                    ) -> tuple[Optional[str], bool]:
    """按源码选择引擎。返回 ``(引擎 或 None, 是否要求 XeTeX)``。

    含 ``\\font\\xx="字体名"``、fontspec/xeCJK 等写法的文档必需 xe/lua 引擎；
    若这类引擎不可用，返回 ``(None, True)``，由调用方给出可操作的提示，
    而不是退到 pdflatex 抛「TFM 字体找不到」。

    ``force_xetex=True`` 用于调用方已确定必须用系统字体（如 TikZ 指定了
    自定义字体）的场合——即便源码里没有字体语法也走 xe/lua 引擎。
    """
    need = force_xetex or source_needs_xetex(source)
    engine = find_engine(require_xetex=need)
    if engine is None and not need:
        engine = find_engine()
    return engine, need


def _probe_engine(engine: str) -> bool:
    """试编译一个最小文档，确认引擎与格式文件均可用。"""
    try:
        with tempfile.TemporaryDirectory(prefix="ws_texprobe_") as tmp:
            p = Path(tmp) / "probe.tex"
            p.write_text("\\documentclass{article}\\begin{document}x\\end{document}",
                         encoding="utf-8")
            subprocess.run(
                [engine, "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", tmp, str(p)],
                capture_output=True, text=True, timeout=30,
            )
            return (Path(tmp) / "probe.pdf").exists()
    except Exception:
        return False


def find_pdf_converter() -> Optional[str]:
    for c in PDF_TO_SVG:
        if shutil.which(c):
            return c
    return None


def latex_toolchain_available() -> bool:
    return find_engine() is not None and find_pdf_converter() is not None


def missing_engine_message(needs_xetex: bool) -> str:
    """引擎不可用时的可操作提示（区分 pdf 与 xe/lua 需求）。"""
    if not needs_xetex:
        return ("未找到可用的 LaTeX 引擎。若已安装 xelatex/pdflatex，"
                "通常是缺少格式文件 latex.ltx 或基础字体 —— 请安装 "
                "texlive-latex 与 texlive-fontsrecommended。"
                "也可改用内置 mathtext 公式模式（无需 TeX）。")
    return ("该文档使用了 XeTeX 专属字体语法（如 \\font\\xx=\"字体名\"、"
            "fontspec/xeCJK），须用 xelatex 或 lualatex 编译，但当前没有"
            "可用者：系统 xelatex 多半因缺少基础字体（Latin Modern）无法启动。"
            "请安装 texlive-fontsrecommended（中文还需 texlive-langchinese、"
            "fontspec/xeCJK），再重试。")


def toolchain_report() -> dict:
    """诊断信息，供界面提示用户如何补齐工具链。"""
    engine = find_engine()
    xetex = find_engine(require_xetex=True)
    converter = find_pdf_converter()
    notes: list[str] = []
    if engine is None:
        if any(shutil.which(e) for e in ENGINES):
            notes.append("已安装 LaTeX 引擎但缺少格式文件或基础字体，"
                         "请安装 texlive-latex 与 texlive-fontsrecommended")
        else:
            notes.append("未安装 LaTeX 引擎，请安装 texlive（xelatex/pdflatex）")
    elif xetex is None:
        notes.append("缺少可用的 xelatex/lualatex（含 XeTeX 字体语法的文档"
                     "无法编译），请安装 texlive-fontsrecommended")
    if converter is None:
        notes.append("缺少 PDF→SVG 转换器，请安装 poppler（pdftocairo）或 dvisvgm")
    return {"engine": engine, "xetex_engine": xetex, "converter": converter,
            "ok": not notes, "notes": notes}


DOCUMENT_TEMPLATE = r"""\documentclass[12pt]{{article}}
\usepackage[margin=1cm,paperwidth={pw}mm,paperheight={ph}mm]{{geometry}}
\usepackage{{amsmath,amssymb}}
\usepackage{{graphicx}}
% xeCJK 用于中文，未安装时也不致命（纯英文/公式文档照常编译）
\IfFileExists{{xeCJK.sty}}{{\usepackage{{xeCJK}}}}{{}}
\pagestyle{{empty}}
\begin{{document}}
{body}
\end{{document}}
"""


def _ensure_document(body: str, paper_w: float, paper_h: float) -> str:
    """若已是完整文档则原样返回，否则套用模板。"""
    if "\\documentclass" in body:
        return body
    return DOCUMENT_TEMPLATE.format(pw=f"{paper_w:.0f}", ph=f"{paper_h:.0f}", body=body)


def render_latex_document(source: str,
                          target_width_mm: Optional[float] = None,
                          paper_w: float = 200.0,
                          paper_h: float = 280.0,
                          timeout: int = 60,
                          fonts: Optional[Sequence] = None) -> LatexResult:
    """编译 LaTeX 文档并转成笔画。

    无可用 TeX 引擎时自动回退到内置排版引擎（:mod:`latex_lite`）渲染
    常用子集——标题/章节/段落/列表/公式/简单表格，零依赖即开即用；
    ``fonts`` 是回退路径用的本软件字体链。装好 TeX 后同一份源码自动
    改走忠实编译。
    """
    tex_src = _ensure_document(source, paper_w, paper_h)
    engine, need_xetex = find_engine_for(tex_src)
    if engine is None:
        # 无 TeX：内置引擎渲染常用子集（探测失败但二进制存在也走回退，
        # 提示语里保留诊断信息供想装 TeX 的用户参考）
        from .latex_lite import render_latex_native
        r = render_latex_native(source, fonts or [])
        if r.ok:
            hint = ("未检测到可用 TeX 引擎：已用内置排版引擎渲染常用子集"
                    "（标题/章节/列表/公式/简单表格），复杂宏包与版式被忽略。"
                    "安装 texlive + poppler 后重新生成即自动改为忠实编译。")
            r.log = f"{hint}{('；' + r.log) if r.log else ''}"
            return r
        return _fail(missing_engine_message(need_xetex))
    converter = find_pdf_converter()
    if converter is None:
        return _fail("未找到 PDF→SVG 转换器（pdftocairo/dvisvgm）")
    return compile_tex_source(tex_src, target_width_mm=target_width_mm,
                              engine=engine, converter=converter, timeout=timeout)


def _fail(msg: str) -> LatexResult:
    result = LatexResult()
    result.log = msg
    return result


def run_tex(tex_src: str, tmpdir: str, engine: str, converter: str,
            timeout: int = 60) -> tuple[Optional[Path], Optional[Path], str]:
    """在 ``tmpdir`` 里编译 ``tex_src`` 并转 SVG。

    返回 ``(pdf 路径, svg 路径, 日志)``；任一步失败时对应路径为 ``None``，
    日志含可读原因。供 LaTeX 文档与 TikZ（含文字位置提取）共用。
    """
    tmp_path = Path(tmpdir)
    tex_file = tmp_path / "doc.tex"
    tex_file.write_text(tex_src, encoding="utf-8")

    cmd = [engine, "-interaction=nonstopmode", "-halt-on-error",
           "-output-directory", str(tmp_path), str(tex_file)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, cwd=str(tmp_path))
    except subprocess.TimeoutExpired:
        return None, None, f"{engine} 编译超时（>{timeout}s）"

    pdf = tmp_path / "doc.pdf"
    if not pdf.exists():
        return None, None, (proc.stdout or "")[-2000:] + "\n" + (proc.stderr or "")[-1000:]

    svg_out = tmp_path / "doc.svg"
    if converter == "pdftocairo":
        cmd2 = ["pdftocairo", "-svg", str(pdf), str(svg_out)]
    else:  # dvisvgm
        cmd2 = ["dvisvgm", "--pdf", str(pdf), "-o", str(svg_out)]
    try:
        proc2 = subprocess.run(cmd2, capture_output=True, text=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        return pdf, None, "PDF→SVG 转换超时"

    candidates = sorted(tmp_path.glob("doc*.svg"))
    if not candidates:
        return pdf, None, (proc2.stderr or proc2.stdout or "未生成 SVG")[-1500:]
    return pdf, candidates[0], ""


def compile_tex_source(tex_src: str, target_width_mm: Optional[float] = None,
                       engine: Optional[str] = None,
                       converter: Optional[str] = None,
                       timeout: int = 60) -> LatexResult:
    """把一份完整 ``.tex`` 源码编译成笔画（LaTeX 与 TikZ 共用）。"""
    result = LatexResult()
    if engine is None:
        engine, need_xetex = find_engine_for(tex_src)
        if engine is None:
            result.log = missing_engine_message(need_xetex)
            return result
    converter = converter or find_pdf_converter()
    if converter is None:
        result.log = "未找到 PDF→SVG 转换器（pdftocairo/dvisvgm）"
        return result

    with tempfile.TemporaryDirectory(prefix="ws_latex_") as tmp:
        _pdf, svg, log = run_tex(tex_src, tmp, engine, converter, timeout)
        if svg is None:
            result.log = log
            return result
        try:
            # TeX 编译产物走历史帧 px×96/72（全部既有文档的对象变换与
            # 笔画编辑层都按它标定；TikZ 字体替换路径同帧），不是 SVG
            # 文件导入的物理尺寸语义。
            imported = import_svg(svg, target_width_mm=target_width_mm,
                                  natural_scale=_PT_TO_PX)
        except Exception as exc:
            result.log = f"SVG 解析失败：{exc}"
            return result

        result.strokes = imported.strokes
        result.width = imported.width
        result.height = imported.height
        result.ok = bool(result.strokes)
        if not result.ok:
            result.log = "编译成功但未解析出笔画；" + "; ".join(imported.warnings)
        return result
