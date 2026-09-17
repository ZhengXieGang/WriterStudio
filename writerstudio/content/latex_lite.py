"""LaTeX 常用子集的内置渲染（无 TeX 工具链时的自动回退）。

把日常文档用到的 LaTeX 结构——标题/章节/段落/列表/公式/简单表格——
转译成 Markdown，复用 :func:`render_markdown` 的整条排版管线（含行内
公式与表格手绘随机），**无需安装任何 TeX 环境**。

不在子集内的结构（浮动体、自定义命令、多栏、交叉引用等）不会让渲染
失败：已知无害命令被丢弃、未知命令保留内容并记入警告，由调用方展示。
装有 TeX 时 :func:`render_latex_document` 走忠实编译路径，本模块只是
零依赖的兜底。
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

from ..fonts.model import FontFamily
from .latex_doc import LatexResult
from .markdown import MarkdownStyle, render_markdown

# ---------------------------------------------------------------------------
# 命令表
# ---------------------------------------------------------------------------
# 带花括号参数、整体丢弃（版式/索引/参考文献等，无正文价值）
_DROP_WITH_ARG = (
    "pagestyle", "thispagestyle", "setlength", "addtolength", "label",
    "index", "hspace", "hspace*", "vspace", "vspace*", "bibliography",
    "bibliographystyle", "usepackage", "documentclass", "graphicspath",
    "definecolor", "color", "textcolor", "pagecolor", "arraystretch",
    "renewcommand", "newcommand", "setcounter", "addtocounter",
    "pagenumbering", "markboth", "markright", "input", "include",
)
# 去壳保内容（文本形状类；单线笔迹无粗细/斜体之分）
_UNWRAP_TEXT = (
    "textbf", "textit", "emph", "texttt", "textsc", "textsf", "textrm",
    "textup", "textsl", "textnormal", "underline", "uline", "mbox",
    "text", "hl", "textsuperscript", "textsubscript", "textmd", "textlf",
)
# 无参数直接丢弃
_DROP_BARE = (
    "centering", "newpage", "clearpage", "cleardoublepage", "noindent",
    "indent", "raggedright", "raggedleft", "normalsize", "small",
    "footnotesize", "scriptsize", "tiny", "large", "Large", "LARGE",
    "huge", "Huge", "maketitle", "tableofcontents", "protect", "relax",
    "par", "appendix", "frontmatter", "mainmatter", "backmatter",
    "sloppy", "fussy", "displaybreak", "allowdisplaybreaks", "nonumber",
    "notag", "cr",
)
# 引用类：整块丢弃（编译路径才有意义的编号）
_DROP_REF = ("ref", "eqref", "cite", "pageref", "autoref", "citep",
             "citet", "footnotemark")
# 命令 → 文本替换
_TEXT_FOR = {
    "ldots": "…", "dots": "…", "dotsb": "…", "dotsc": "…", "dotsi": "…",
    "TeX": "TeX", "LaTeX": "LaTeX",
    "textasciitilde": "~", "textasciicircum": "^", "textbackslash": "\\",
    "textbar": "|", "textless": "<", "textgreater": ">",
    "textquotedblleft": "“", "textquotedblright": "”",
    "textendash": "–", "textemdash": "—",
    "checkmark": "✓", "degree": "°",
}

_ENV_RE = re.compile(
    r"\\begin\{([a-zA-Z*]+)\}(?:\[[^\]]*\])?(.*?)\\end\{\1\}",
    re.S)

_SECTION_RE = re.compile(r"\\(section|subsection|subsubsection"
                         r"|paragraph|subparagraph)\*?\s*\{")

_CMD_ARG_RE = re.compile(r"\\([a-zA-Z]+)\*?(?:\[[^\]]*\])?\{([^{}]*)\}")
_CMD_BARE_RE = re.compile(r"\\([a-zA-Z]+)\*?")
_LINEBREAK = "\x00"                    # ``\\`` 换行占位符


class _Transpiler:
    def __init__(self) -> None:
        self.warns: list[str] = []
        self._warned: set[str] = set()

    def warn(self, msg: str) -> None:
        if msg not in self.warns:
            self.warns.append(msg)

    def _warn_cmd_once(self, cmd: str) -> None:
        if cmd not in self._warned:
            self._warned.add(cmd)
            self.warn(f"不支持的命令 \\{cmd}（内容保留，排版效果忽略）")

    # ------------------------------------------------------------- 主入口
    def transpile(self, source: str) -> str:
        src = _strip_comments(source)
        title, author, date, body = _split_document(src)
        body = self._sections(body)

        out: list[str] = []
        if title:
            out.append(f"# {self.inline(title)}")
        if author:
            out.append(self.inline(author))
        if date:
            out.append(self.inline(date))
        out.append(self._blocks(body))
        md = "\n\n".join(b for b in out if b.strip())
        return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"

    def _sections(self, text: str) -> str:
        """``\\section{X}`` 一类标题命令 → Markdown 标题（``# X``）。"""
        out: list[str] = []
        pos = 0
        for m in _SECTION_RE.finditer(text):
            close = _match_brace(text, m.end() - 1)
            if close is None:
                continue
            out.append(text[pos:m.start()])
            level = (1 if m.group(1) == "section" else
                     2 if m.group(1) == "subsection" else
                     3 if m.group(1) == "subsubsection" else 4)
            out.append("\n\n" + "#" * level + " "
                       + text[m.end():close] + "\n\n")
            pos = close + 1
        out.append(text[pos:])
        return "".join(out)

    # ------------------------------------------------------------- 行内
    def inline(self, s: str) -> str:
        """行内清理：公式定界符归一、命令展开/丢弃、转义还原。"""
        s = s.replace(r"\(", "$").replace(r"\)", "$")
        s = re.sub(r"\$\$", "$", s)            # ``$$..$$`` → ``$..$``
        prev = None
        while prev != s:                       # 嵌套命令逐层剥壳
            prev = s
            s = _inline_pass(self, s)
        s = re.sub(r"\\([&%$#_{}])", r"\1", s)  # 转义字符还原
        s = s.replace("~", " ")                # 不换行空格
        s = re.sub(r"\\/", " ", s)             # 斜体纠正
        return s

    # ------------------------------------------------------------- 块级
    def _blocks(self, text: str) -> str:
        """按环境切分顶层文本；环境外是普通文本。"""
        parts: list[str] = []
        pos = 0
        for m in _ENV_RE.finditer(text):
            if m.start() > pos:
                parts.append(self._plain(text[pos:m.start()]))
            parts.append(self._environment(m.group(1), m.group(2)))
            pos = m.end()
        if pos < len(text):
            parts.append(self._plain(text[pos:]))
        return "\n\n".join(p for p in parts if p.strip())

    def _plain(self, text: str) -> str:
        """普通文本块：显示公式 ``\\[..]``/``$$..$$`` 提为独立段。"""
        text = text.replace(r"\[", " \x01 ").replace(r"\]", " \x01 ")
        text = text.replace("$$", " \x01 ")
        # ``\\``（源码里两个反斜杠）是换行：先换成占位符，避免被当命令吃掉
        text = text.replace("\\\\", f" {_LINEBREAK} ")
        pieces = text.split(" \x01 ")
        out: list[str] = []
        for i, piece in enumerate(pieces):
            piece = piece.strip()
            if not piece:
                continue
            if i % 2 == 1:                     # 奇数段 = 显示公式
                out.append("$" + self.inline(piece).strip() + "$")
            else:
                out.append(self._paragraphs(piece))
        return "\n\n".join(o for o in out if o.strip())

    def _paragraphs(self, text: str) -> str:
        """普通文本：命令清理后按换行占位符/换行符分行，每行一个段落。"""
        text = self.inline(text)
        lines = [ln.strip() for ln in re.split(_LINEBREAK + r"|\n", text)]
        return "\n\n".join(ln for ln in lines if ln)

    # ------------------------------------------------------------- 环境
    def _environment(self, name: str, content: str) -> str:
        if name in ("itemize", "enumerate", "description"):
            return self._list(name, content, depth=0)
        if name in ("quote", "quotation"):
            inner = self._blocks(content).strip()
            return "\n".join("> " + ln if ln else ">"
                             for ln in inner.splitlines())
        if name in ("center", "flushleft", "flushright", "abstract",
                    "adjustbox", "spacing"):
            return self._blocks(content)
        if name in ("equation", "equation*", "align", "align*", "gather",
                    "gather*", "displaymath", "multline", "multline*",
                    "eqnarray", "eqnarray*"):
            return self._display_math(content)
        if name in ("tabular", "tabular*", "tabularx", "longtable"):
            return self._tabular(content)
        if name in ("figure", "figure*", "table", "table*"):
            return self._float(name, content)
        if name in ("verbatim", "verbatim*", "lstlisting", "minted"):
            return self._plain(content)
        self.warn(f"不支持的环境 {name}（内容按普通文本处理）")
        return self._plain(content)

    def _list(self, kind: str, content: str, depth: int) -> str:
        out: list[str] = []
        counter = 0
        pad = "  " * depth
        for item in _split_items(content):
            counter += 1
            m = _ENV_RE.search(item)
            prefix, extra = item, []
            if m:                              # item 内嵌套环境
                prefix = item[:m.start()]
                extra.append(self._environment(m.group(1), m.group(2)))
            marker = f"{counter}." if kind == "enumerate" else "-"
            text = self.inline(prefix.strip())
            if text:
                out.append(f"{pad}{marker} {text}")
            out.extend(extra)
        return "\n".join(out)

    def _display_math(self, content: str) -> str:
        body = content.replace("&", "")        # 对齐点在单行渲染里无意义
        body = re.sub(r"\\\\", "  ", body)     # 公式内换行 → 空格
        body = self.inline(body).strip()
        return f"${body}$" if body else ""

    def _tabular(self, content: str) -> str:
        m = re.match(r"\s*(?:\{[^{}]*\})?\s*\{([^{}]*)\}", content)
        if not m:
            self.warn("表格列格式无法解析，按普通文本处理")
            return self._paragraphs(content)
        ncols = len(re.findall(r"[lcrpmb]", m.group(1)))
        rows: list[list[str]] = []
        for row in re.split(r"\\\\(?:\*|\[[^\]]*\])?", content[m.end():]):
            cells = [self._cell(c) for c in _split_top_level(row, "&")]
            if not any(cells):
                continue                       # 纯 \hline/空行
            rows.append(cells[:ncols] if ncols else cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = ["| " + " | ".join(rows[0]) + " |",
              "|" + "|".join([" --- "] * width) + "|"]
        for r in rows[1:]:
            md.append("| " + " | ".join(r) + " |")
        return "\n".join(md)

    def _cell(self, text: str) -> str:
        text = re.sub(r"\\hline\b", "", text)
        text = re.sub(r"\\cline\{[^}]*\}", "", text)
        m = re.match(r"\s*\\multicolumn\{\d+\}\{[^{}]*\}\{(.*)\}\s*", text,
                     re.S)
        if m:
            self.warn("表格 \\multicolumn 合并格按普通格处理（跨列丢失）")
            text = m.group(1)
        return self.inline(text).replace("\n", " ").strip()

    def _float(self, name: str, content: str) -> str:
        for m in re.finditer(r"\\includegraphics(?:\[[^\]]*\])?\{([^{}]*)\}",
                             content):
            self.warn(f"插图 {m.group(1)} 无法在无 TeX 模式渲染，已跳过")
        cap = re.search(r"\\caption(?:\[[^\]]*\])?\{([^{}]*)\}", content,
                        re.S)
        if cap:
            return self._paragraphs("图：" + cap.group(1))
        if name.startswith("table"):
            inner = re.sub(r"\\caption(?:\[[^\]]*\])?\{[^{}]*\}", "", content)
            return self._blocks(inner)
        return ""


# ---------------------------------------------------------------------------
# 行内清理
# ---------------------------------------------------------------------------
def _inline_pass(t: _Transpiler, s: str) -> str:
    """一层命令展开（嵌套由调用方循环到不动点）。"""
    def arg_sub(m: re.Match) -> str:
        cmd, arg = m.group(1), m.group(2)
        if cmd in _DROP_WITH_ARG or cmd in _DROP_REF:
            return ""
        if cmd in _UNWRAP_TEXT:
            return arg
        if cmd in _TEXT_FOR:
            return _TEXT_FOR[cmd]
        if cmd == "footnote":
            return f"（{arg}）"
        t._warn_cmd_once(cmd)
        return arg

    def bare_sub(m: re.Match) -> str:
        cmd = m.group(1)
        if cmd in _DROP_BARE or cmd in _DROP_WITH_ARG or cmd in _DROP_REF:
            return ""
        if cmd in _TEXT_FOR:
            return _TEXT_FOR[cmd]
        t._warn_cmd_once(cmd)
        return ""

    s = _CMD_ARG_RE.sub(arg_sub, s)
    return _CMD_BARE_RE.sub(bare_sub, s)


# ---------------------------------------------------------------------------
# 底层工具
# ---------------------------------------------------------------------------
def _strip_comments(src: str) -> str:
    """去掉 ``%`` 注释（``\\%`` 转义除外），保留行结构。"""
    out = []
    for line in src.splitlines():
        i = 0
        while i < len(line):
            if line[i] == "\\" and i + 1 < len(line):
                i += 2
                continue
            if line[i] == "%":
                break
            i += 1
        out.append(line[:i])
    return "\n".join(out)


def _split_document(src: str) -> tuple[str, str, str, str]:
    """返回 ``(title, author, date, 正文)``；无 document 环境时整体为正文。"""
    title = author = date = ""
    m = re.search(r"\\begin\{document\}", src)
    if not m:
        return title, author, date, src
    pre, body = src[:m.start()], src[m.end():]
    end = re.search(r"\\end\{document\}", body)
    if end:
        body = body[:end.start()]
    for cmd, slot in (("title", "t"), ("author", "a"), ("date", "d")):
        dm = re.search(r"\\" + cmd + r"(?:\[[^\]]*\])?\{(.*?)\}", pre, re.S)
        if dm:
            if slot == "t":
                title = dm.group(1)
            elif slot == "a":
                author = dm.group(1)
            else:
                date = dm.group(1)
    return title, author, date, body


def _match_brace(s: str, open_idx: int) -> Optional[int]:
    """``s[open_idx]`` 是 ``{``，返回配对 ``}`` 的下标（含转义/嵌套）。"""
    depth = 0
    i = open_idx
    n = len(s)
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_items(content: str) -> list[str]:
    """按顶层 ``\\item`` 切分列表环境内容（嵌套环境内的 item 不切）。"""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    n = len(content)
    while i < n:
        if content.startswith("\\begin{", i):
            depth += 1
        elif content.startswith("\\end{", i):
            depth -= 1
        elif content.startswith("\\item", i) and depth == 0 \
                and not content[i + 5:i + 6].isalpha():
            parts.append("".join(cur))
            cur = []
            i += 5
            if content[i:i + 1] == "[":        # description 的 \item[词]
                j = content.find("]", i)
                if j != -1:
                    cur.append(content[i + 1:j] + "：")
                    i = j + 1
            continue
        cur.append(content[i])
        i += 1
    parts.append("".join(cur))
    return [p for p in (x.strip() for x in parts) if p]


def _split_top_level(text: str, sep: str) -> list[str]:
    """按不在花括号内的 ``sep`` 切分（``\\&`` 之类转义不切）。"""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            cur.append(text[i:i + 2])
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def transpile_latex(source: str) -> tuple[str, list[str]]:
    """LaTeX 常用子集 → Markdown。返回 ``(markdown, 警告列表)``。"""
    t = _Transpiler()
    md = t.transpile(source)
    return md, t.warns


def render_latex_native(source: str, fonts: Sequence[FontFamily],
                        style: Optional[MarkdownStyle] = None) -> LatexResult:
    """无 TeX 时用内置排版引擎渲染 LaTeX 文档（常用子集）。"""
    md, warns = transpile_latex(source)
    result = LatexResult()
    result.native = True
    try:
        res = render_markdown(md, fonts, style or MarkdownStyle())
    except Exception as exc:
        result.log = f"内置排版引擎渲染失败：{exc}"
        return result
    result.strokes = list(res.strokes)
    result.width = res.width
    result.height = res.height
    result.ok = bool(res.strokes)
    result.log = "；".join(warns)
    if not result.ok and not result.log:
        result.log = "文档内容为空"
    return result
