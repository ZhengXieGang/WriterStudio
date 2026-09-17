"""TikZ 图形支持。

TikZ 是 LaTeX 里最常用的矢量绘图宏包，适合画精确的几何图形、流程图、电路、
坐标系、函数曲线等。写字机需要的是**可书写的折线**，因此这里走与 LaTeX 文档
相同的管线：``tikzpicture`` 源码 → 套用独立文档模板（``standalone`` + ``tikz``）
→ 引擎编译成 PDF → ``pdftocairo`` 转 SVG → 解析为笔画。

坐标系直接使用 TikZ 自己的 ``(x,y)`` 单位（默认 1cm）；用户在 TikZ 里怎么写，
就按它在纸上的实际物理尺寸渲染，精度由 TeX 保证。

依赖外部命令（与 LaTeX 文档相同）以及 TeX 发行版里的 ``pgf/tikz`` 宏包。
缺 TikZ 宏包时 :func:`tikz_available` 返回 False，界面应提示安装。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from ..core.strokes import Stroke

from .latex_doc import (
    LatexResult,
    compile_tex_source,
    find_engine,
    find_engine_for,
    find_pdf_converter,
    missing_engine_message,
)

#: 自定义字体模式下注入的字体命令名（内部控制序列，不会与用户源码冲突）
_FONT_CMD = r"\wsfont"

_UNSET = object()
_TIKZ_CACHE: object = _UNSET

#: 演示用 TikZ 源码（几何图形 + 坐标轴 + 节点 + 箭头）
TIKZ_SAMPLE = r"""\begin{tikzpicture}[scale=1]
  \draw[thick] (0,0) -- (4,0) -- (4,3) -- (0,3) -- cycle;
  \draw (0,0) -- (4,3);
  \draw[->] (0,-0.6) -- (4.4,-0.6) node[right] {$x$};
  \draw[->] (-0.6,0) -- (-0.6,3.4) node[above] {$y$};
  \fill (4,3) circle (2pt);
  \node at (2,1.5) {A};
\end{tikzpicture}
"""

# 用 standalone 裁到图形本身（最佳）；它属于可选宏包，缺失时退回 article。
# 无论用哪个，import_svg 都会把结果裁到笔画包围盒，所以版面仍然干净。
_TIKZ_BODY_STANDALONE = r"""\documentclass[border=2pt]{{standalone}}
\usepackage{{tikz}}
\IfFileExists{{pgfplots.sty}}{{\usepackage{{pgfplots}}\pgfplotsset{{compat=1.18}}}}{{}}
\begin{{document}}
{body}
\end{{document}}
"""

_TIKZ_BODY_ARTICLE = r"""\documentclass{{article}}
\usepackage[margin=0mm,paperwidth=2000mm,paperheight=2000mm]{{geometry}}
\usepackage{{tikz}}
\IfFileExists{{pgfplots.sty}}{{\usepackage{{pgfplots}}\pgfplotsset{{compat=1.18}}}}{{}}
\pagestyle{{empty}}
\begin{{document}}
{body}
\end{{document}}
"""

# 指定了自定义字体（节点文字/标注用）时用 XeTeX 的系统字体机制：
# \font\wsfont="字体名" at <尺寸> 由 XeTeX 直接向 fontconfig 取字，
# 无需 fontspec/xeCJK 宏包。图形本体（\draw 等）不受影响，仅影响文字。
#
# 注意：只在正文开头写 {cmd} 是**无效**的——TikZ 的 \node 会重置字体，
# 必须经 `every node` 样式注入，节点文字才会真正用上该字体。
_TIKZ_BODY_STANDALONE_FONT = r"""\documentclass[border=2pt]{{standalone}}
\usepackage{{tikz}}
\IfFileExists{{pgfplots.sty}}{{\usepackage{{pgfplots}}\pgfplotsset{{compat=1.18}}}}{{}}
\font{cmd}="{font}" at {size}pt
\tikzset{{every node/.append style={{font={cmd}}}}}
\begin{{document}}
{body}
\end{{document}}
"""

_TIKZ_BODY_ARTICLE_FONT = r"""\documentclass{{article}}
\usepackage[margin=0mm,paperwidth=2000mm,paperheight=2000mm]{{geometry}}
\usepackage{{tikz}}
\IfFileExists{{pgfplots.sty}}{{\usepackage{{pgfplots}}\pgfplotsset{{compat=1.18}}}}{{}}
\pagestyle{{empty}}
\font{cmd}="{font}" at {size}pt
\tikzset{{every node/.append style={{font={cmd}}}}}
\begin{{document}}
{body}
\end{{document}}
"""

#: 自定义字体的默认字号（TikZ 节点字号常由源码里的 \node 大小覆盖，
#: 这里只是给 \font 一个合理基准）
_DEFAULT_FONT_SIZE_PT = 10.0


def _has_package(name: str) -> bool:
    kpse = shutil.which("kpsewhich")
    if not kpse:
        return False
    try:
        r = subprocess.run([kpse, name], capture_output=True, text=True, timeout=8)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _tikz_template(with_font: bool = False) -> str:
    """优先 standalone（最紧凑），缺失则退回 article（导入时会被裁到笔画）。"""
    standalone = _has_package("standalone.cls")
    if with_font:
        return (_TIKZ_BODY_STANDALONE_FONT if standalone
                else _TIKZ_BODY_ARTICLE_FONT)
    return _TIKZ_BODY_STANDALONE if standalone else _TIKZ_BODY_ARTICLE


def system_fonts() -> list[str]:
    """系统字体族名列表（fontconfig 的 ``fc-list : family``），去重排序。

    供 UI 下拉选择 TikZ 自定义字体；系统没有 fc-list 时返回空表。
    """
    exe = shutil.which("fc-list")
    if not exe:
        return []
    try:
        proc = subprocess.run([exe, ":", "family"], capture_output=True,
                              text=True, timeout=10)
    except Exception:
        return []
    names: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        for part in line.split(","):
            n = part.strip()
            if n:
                names.add(n)
    return sorted(names, key=lambda s: s.casefold())


def sanitize_font_name(name: str) -> str:
    """字体名清洗：用于 ``\\font\\x="名字"``，引号/反斜杠/花括号会造成语法错。

    非法字符一律去掉（空串表示不可用，调用方按「未指定字体」处理）。
    """
    bad = set('"\\{}%\n\r')
    return "".join(c for c in (name or "").strip() if c not in bad).strip()


#: 源码含中文（且未指定字体）时按序挑选的兜底 CJK 系统字体
_CJK_FALLBACK_FONTS = (
    "Noto Sans CJK SC", "Noto Serif CJK SC", "Noto Sans CJK TC",
    "Source Han Sans SC", "WenQuanYi Zen Hei", "文泉驿微米黑",
    "WenQuanYi Micro Hei", "Droid Sans Fallback", "SimSun", "宋体",
)


def has_cjk(text: str) -> bool:
    """文本是否含 CJK 汉字（决定 TikZ 是否需要 CJK 字体）。"""
    for ch in text or "":
        cp = ord(ch)
        if (0x3400 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
                or 0x20000 <= cp <= 0x3FFFD):
            return True
    return False


def default_cjk_font() -> Optional[str]:
    """挑一个可用的 CJK 系统字体（随源码含中文且用户未指定字体时使用）。"""
    available = {n.casefold(): n for n in system_fonts()}
    for cand in _CJK_FALLBACK_FONTS:
        hit = available.get(cand.casefold())
        if hit:
            return hit
    # 退一步：任何名字里带 CJK / 中文关键字的字体
    for low, orig in available.items():
        if "cjk" in low or "hei" in low or "song" in low or "ming" in low:
            return orig
    return None


def _font_preamble(fname: str) -> str:
    """测量用字体的导言区片段：定义字体命令 + 注入 all-node 样式。

    只用于让 TeX 把文字**排出来**（好让 ``pdftotext`` 量到位置）；这些文字
    最后会被丢弃、改用本软件字体重排。非 CJK 场景不需要。
    """
    return (f'\n\\font{_FONT_CMD}="{fname}" at {_DEFAULT_FONT_SIZE_PT:g}pt\n'
            f'\\tikzset{{every node/.append style={{font={_FONT_CMD}}}}}\n')


def _inject_font_into_document(doc: str, fname: str) -> str:
    """整份文档（已含 \\documentclass）注入测量字体：在导言区末尾插入。

    注入点在 ``\\begin{document}`` **之前**（导言区）：字体命令与 tikzset
    都必须在文档开始前定义好，且不能放进分组（\\font 赋值是局部的）。
    """
    marker = r"\begin{document}"
    idx = doc.find(marker)
    if idx < 0:
        return doc
    return doc[:idx] + _font_preamble(fname) + doc[idx:]


def _strip_text_from_svg(src: str | Path, dst: str | Path) -> bool:
    """删掉 SVG 里的文字元素（``use``/``text``/``tspan``/``defs``）。

    pdftocairo 把 PDF 文字输出成 ``<use xlink:href="#glyph-…">``，字形轮廓
    定义在 ``<defs>`` 里；svgelements 的 ``elements()`` 会把 defs 里的字形也
    当图形路径返回。因此替换文字时必须先从 XML 树上摘掉这些节点，剩下的才是
    真正的图形（折线/矩形/曲线）。裁剪/遮罩（clipPath/mask）一并去掉：本管线
    的 TikZ 图形不用它们，去掉更安全。返回是否写成功。
    """
    import xml.etree.ElementTree as ET

    ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
    ET.register_namespace("", "http://www.w3.org/2000/svg")

    def local(tag) -> str:
        return str(tag).rsplit("}", 1)[-1]

    def drop(parent) -> None:
        for child in list(parent):
            if local(child.tag) in ("use", "text", "tspan", "defs",
                                     "clipPath", "mask"):
                parent.remove(child)
            else:
                drop(child)

    try:
        tree = ET.parse(str(src))
    except Exception:
        return False
    drop(tree.getroot())
    try:
        tree.write(str(dst), encoding="utf-8", xml_declaration=True)
    except Exception:
        return False
    return True


def _has_tikz_package(engine: str) -> bool:
    """检查 TeX 发行版里是否有 pgf/tikz 宏包（优先 kpsewhich，其次试编译）。"""
    if _has_package("tikz.sty"):
        return True
    # 退化：试编译一个最小 TikZ 文档
    try:
        with tempfile.TemporaryDirectory(prefix="ws_tikzprobe_") as tmp:
            p = Path(tmp) / "probe.tex"
            p.write_text(
                _tikz_template().format(body=r"\begin{tikzpicture}\draw (0,0)--(1,1);"
                                             r"\end{tikzpicture}"),
                encoding="utf-8")
            subprocess.run(
                [engine, "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", tmp, str(p)],
                capture_output=True, text=True, timeout=40)
            return (Path(tmp) / "probe.pdf").exists()
    except Exception:
        return False


def tikz_available() -> bool:
    """TikZ 是否可用（引擎 + 转换器 + pgf/tikz 宏包齐全）。"""
    global _TIKZ_CACHE
    if _TIKZ_CACHE is not _UNSET:
        return bool(_TIKZ_CACHE)
    engine = find_engine()
    _TIKZ_CACHE = (engine is not None
                   and find_pdf_converter() is not None
                   and _has_tikz_package(engine))
    return bool(_TIKZ_CACHE)


def tikz_report() -> dict:
    """诊断信息，供界面提示如何补齐依赖。"""
    engine = find_engine()
    converter = find_pdf_converter()
    notes: list[str] = []
    if engine is None:
        notes.append("未找到可用的 LaTeX 引擎，请安装 texlive（xelatex/pdflatex）")
    if converter is None:
        notes.append("缺少 PDF→SVG 转换器，请安装 poppler（pdftocairo）或 dvisvgm")
    if engine is not None and not _has_tikz_package(engine):
        notes.append("缺少 TikZ 宏包，请安装 texlive-pictures（提供 pgf/tikz）")
    return {"engine": engine, "converter": converter,
            "ok": not notes, "notes": notes}


def _ensure_picture(body: str, measurement_font: Optional[str] = None) -> str:
    """若已是完整文档则原样返回；若只含 ``\\begin{tikzpicture}`` 片段则套模板；
    其余（裸 ``\\draw`` 命令等）整段包进 tikzpicture。

    判定只看环境/文档标记，不能用 "tikz" 子串——正文里出现这个词
    （注释、节点文字）不代表已经是 picture 环境。

    ``measurement_font`` 为系统字体名（如 ``Noto Sans CJK SC``）时，套 XeTeX
    模板把 ``\\font`` 命令与 all-node 样式注入导言区（已是完整文档则注入
    ``\\begin{document}`` 之前）——只为让 TeX 把文字排出来供测量，文字本身
    随后会被丢弃、改用本软件字体重排。
    """
    fname = sanitize_font_name(measurement_font or "")
    if "\\documentclass" in body:
        if not fname:
            return body
        return _inject_font_into_document(body, fname)
    if "\\begin{tikzpicture}" not in body:
        body = "\\begin{tikzpicture}\n" + body + "\n\\end{tikzpicture}"
    if fname:
        return _tikz_template(with_font=True).format(
            cmd=_FONT_CMD, font=fname, size=f"{_DEFAULT_FONT_SIZE_PT:g}",
            body=body)
    return _tikz_template().format(body=body)


def render_tikz(source: str,
                target_width_mm: Optional[float] = None,
                timeout: int = 60,
                font_names: Optional[Sequence[str]] = None,
                manager=None,
                text_scale: float = 1.0) -> LatexResult:
    """编译 TikZ 源码，图形保持 TeX 矢量、文字改用本软件字体重排。

    ``font_names`` 给定时（本软件的字体链，如 ``["手写真迹","futural"]``）：
    节点文字不再取 TeX 的轮廓字形，而是**丢弃后按 TeX 给的位置**用该字体链
    重新排版（见 :mod:`writerstudio.content.tikz_text`）——这样写出来的节点
    文字用的是用户自己的手写字迹。``font_names`` 为空时行为与原先一致
    （文字用 TeX 默认/系统字体轮廓）。

    坐标/尺寸由 TeX 保证；``text_scale`` 可整体微调替换文字的字号。
    """
    replacing = bool(font_names) and manager is not None
    fname = ""
    if has_cjk(source):
        # 源码含中文时一律给 TeX 一个 CJK 系统字体：
        #   * 替换模式：只求把文字排出来供测量（否则 pdflatex 静默丢汉字，
        #     量到的位置就是错的）；
        #   * 原生模式：直接用系统字体画出文字，避免汉字整体丢失。
        fname = default_cjk_font() or ""
    tex_src = _ensure_picture(source, measurement_font=fname or None)
    # 引擎按整份源码选（含 XeTeX 字体语法、或指定了测量字体时须 xe/lua 引擎，
    # pdflatex 解析不了前者，也无法按名字取系统字体）
    engine, need_xetex = find_engine_for(tex_src, force_xetex=bool(fname))
    if engine is None:
        result = LatexResult()
        result.log = missing_engine_message(need_xetex)
        return result
    if not _has_tikz_package(engine):
        result = LatexResult()
        result.log = ("未找到 TikZ 宏包（pgf）。请安装 TeX 发行版的 "
                      "texlive-pictures 包后重试。")
        return result

    if not replacing:
        # 原生路径：文字保留 TeX 画出的轮廓（含中文时用系统 CJK 字体）
        return compile_tex_source(tex_src, target_width_mm=target_width_mm,
                                  engine=engine, timeout=timeout)
    return _render_tikz_with_app_fonts(
        tex_src, target_width_mm, timeout, engine, font_names, manager,
        text_scale)


def _render_tikz_with_app_fonts(tex_src: str,
                                target_width_mm: Optional[float],
                                timeout: int, engine: str,
                                font_names: Sequence[str], manager,
                                text_scale: float) -> LatexResult:
    """文字替换主流程：编译 → 量词框 → 图形转笔画 + 文字用本软件字体重排。"""
    import tempfile
    from pathlib import Path

    from .latex_doc import find_pdf_converter, run_tex
    from .svg_import import parse_svg_geometry, BBox as _BBox
    from .tikz_text import extract_words, place_words

    result = LatexResult()
    converter = find_pdf_converter()
    if converter is None:
        result.log = "未找到 PDF→SVG 转换器（pdftocairo/dvisvgm）"
        return result

    with tempfile.TemporaryDirectory(prefix="ws_tikz_") as tmp:
        pdf, svg, log = run_tex(tex_src, tmp, engine, converter, timeout)
        if svg is None:
            result.log = log
            return result

        # 1) 量取文字位置（失败则退回原生轮廓路径，不丢内容）
        words = extract_words(pdf) if pdf is not None else []

        # 2) 图形：从 SVG 里删掉文字元素后按原管线解析
        clean = Path(tmp) / "graphics.svg"
        src_svg = clean if _strip_text_from_svg(svg, clean) else svg
        try:
            raw, warnings = parse_svg_geometry(src_svg, tolerance=0.1)
        except Exception as exc:
            result.log = f"SVG 解析失败：{exc}"
            return result

        # 归一化基准取**图形 ∪ 文字框**的并集：与未替换时的整体包围盒一致，
        # 这样 target_width 指的始终是整张图的宽度（不论文字是否被替换）。
        # 纯文字图形（如只有 \node 公式）并集就是词框；二者都缺才为空。
        if raw or words:
            from .tikz_text import _PT_TO_PX
            from .svg_import import BBox as _B
            ub = _B.from_points(p for s in raw for p in s.points)
            for w in words:
                ub.expand((w.x0 * _PT_TO_PX, w.y0 * _PT_TO_PX))
                ub.expand((w.x1 * _PT_TO_PX, w.y1 * _PT_TO_PX))
            box = ub
            if target_width_mm and box.width > 1e-9:
                scale = target_width_mm / box.width
            else:
                # 无 target_width 时沿用历史帧：本地单位 = px×96/72。
                # 这不是 px→mm 的物理换算，而是所有既有文档（对象变换、
                # 笔画编辑层的坐标）共同标定的约定——改成 px→mm 会让旧
                # 文档打开时整体缩到约 1/5.04，用户全部尺寸与手绘编辑
                # 全部错位。新对象的观感尺寸由对象变换决定，不受影响。
                scale = _PT_TO_PX
        else:
            result.log = "TikZ 编译成功但未解析出图形笔画；" + "; ".join(warnings)
            return result

        strokes: list[Stroke] = []
        for s in raw:
            pts = [((x - box.x0) * scale, (box.y1 - y) * scale)
                   for x, y in s.points]
            strokes.append(Stroke(pts, s.closed))

        # 3) 文字：丢掉 TeX 轮廓，用本软件字体链重排到原位
        if words:
            missing: set = set()
            strokes.extend(place_words(words, box, scale, manager,
                                       font_names=font_names,
                                       size_scale=text_scale,
                                       missing_out=missing))
            result.missing_chars = sorted(missing)

        nb = _BBox.from_points(p for s in strokes for p in s.points)
        result.strokes = strokes
        result.width = nb.width
        result.height = nb.height
        result.ok = bool(strokes)
        if not result.ok:
            result.log = "未解析出任何笔画"
        return result
