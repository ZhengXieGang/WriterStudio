"""富内容插入对话框：Markdown 表格 / LaTeX 公式 / LaTeX 文档 / SVG 导入。

一个对话框按「类型」切换编辑区，统一返回 (kind, payload)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..content import (
    render_equation,
    render_markdown,
    svg_available,
    toolchain_report,
)
from ..content.tikz import TIKZ_SAMPLE, tikz_report
from ..fonts import resolve_fonts
from ..fonts.builder import TextSpec
from ..fonts.manager import FontManager
from . import filedialog
from .perturb_panel import PerturbPanel
from .style import spin, unify_inputs

# 类型标识
KIND_MARKDOWN = "markdown"
KIND_EQUATION = "equation"
KIND_LATEX = "latex"
KIND_TIKZ = "tikz"
KIND_SVG = "svg"

_MD_SAMPLE = """# 标题

段落文字，含公式 $a^2+b^2=c^2$ 与 **强调**。

| 列一 | 列二 | 列三 |
|:-----|:----:|-----:|
| 甲 | 1 | 3.14 |
| 乙 | 2 | 2.72 |
"""

_EQ_SAMPLE = r"\frac{-b \pm \sqrt{b^2-4ac}}{2a}"

_LATEX_SAMPLE = r"""\section*{勾股定理}
设直角三角形两直角边为 $a, b$，斜边为 $c$，则
\[ a^2 + b^2 = c^2 \]
"""


@dataclass
class ContentPayload:
    kind: str
    data: dict[str, Any]


class ContentDialog(QDialog):
    """富内容编辑对话框。"""

    def __init__(self, manager: FontManager, initial_kind: str = KIND_MARKDOWN,
                 parent: Optional[QWidget] = None,
                 default_font_chain: Optional[list[str]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("插入富内容")
        self.resize(780, 620)
        self.manager = manager
        self._default_font_chain = list(default_font_chain or [])
        self._build_ui()
        self._select_kind(initial_kind)
        self._update_preview()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("类型:"))
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Markdown（含表格）", KIND_MARKDOWN)
        self.kind_combo.addItem("LaTeX 公式", KIND_EQUATION)
        self.kind_combo.addItem("LaTeX 文档", KIND_LATEX)
        self.kind_combo.addItem("TikZ 图形", KIND_TIKZ)
        self.kind_combo.addItem("SVG 矢量图", KIND_SVG)
        self.kind_combo.currentIndexChanged.connect(
            lambda: self._select_kind(self.kind_combo.currentData()))
        top.addWidget(self.kind_combo)
        top.addStretch(1)
        root.addLayout(top)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self._build_markdown_page()
        self._build_equation_page()
        self._build_latex_page()
        self._build_tikz_page()
        self._build_svg_page()

        # 预览信息
        self.preview_label = QLabel("—")
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet("color:#666;")
        root.addWidget(self.preview_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        unify_inputs(self)

    # -- Markdown 页 -------------------------------------------------------
    def _build_markdown_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(QLabel("Markdown 内容（支持标题、列表、引用、代码块、表格、$公式$）："))
        self.md_edit = QTextEdit()
        self.md_edit.setPlainText(_MD_SAMPLE)
        self.md_edit.setFont(self._mono())
        self.md_edit.textChanged.connect(self._update_preview)
        lay.addWidget(self.md_edit, 1)

        form = QFormLayout()
        self.md_size = spin(1.0, 30.0, 4.0, " mm", 0.5)
        self.md_size.valueChanged.connect(self._update_preview)
        form.addRow("正文字号", self.md_size)
        self.md_cs = spin(-5.0, 20.0, 0.0, " mm", 0.1)
        self.md_cs.setToolTip("额外字距：均匀加在每个字符的步距上")
        self.md_cs.valueChanged.connect(self._update_preview)
        form.addRow("字距", self.md_cs)
        self.md_wrap = spin(20.0, 400.0, 120.0, " mm", 5.0)
        self.md_wrap.valueChanged.connect(self._update_preview)
        form.addRow("换行宽度", self.md_wrap)
        self.md_table_w = spin(0.0, 1000.0, 0.0, " mm", 5.0)
        self.md_table_w.setToolTip(
            "表格整体宽度：0 = 按内容自适应。\n"
            "设了宽度后各列按该总宽缩放、过宽的单元格自动折行——\n"
            "也可在画布上直接拖动表格左右缘手柄调整（与文本框同手感）。")
        self.md_table_w.valueChanged.connect(self._update_preview)
        form.addRow("表格宽度", self.md_table_w)
        # 表格随机化（手绘感）：默认线端轻偏移+出头（网格仍对齐），
        # 行/列界抖动默认关——需要「画出来」的表格再开
        jrow1 = QHBoxLayout()
        self.md_tab_ej = spin(0.0, 3.0, 0.5, " mm", 0.1, 2)
        self.md_tab_ej.setToolTip(
            "线端随机偏移 σ：每条表格线的两端沿轴向随机平移，并带垂直分量\n"
            "（线条微倾）——线不再横平竖直、精确交在角上。0 = 规整。")
        self.md_tab_os = spin(0.0, 3.0, 0.6, " mm", 0.1, 2)
        self.md_tab_os.setToolTip(
            "线端出头上限：线端随机越过相交线一点，手绘表格的「过线」笔感。\n"
            "0 = 不出头。")
        jrow1.addWidget(QLabel("偏移"))
        jrow1.addWidget(self.md_tab_ej)
        jrow1.addWidget(QLabel("出头"))
        jrow1.addWidget(self.md_tab_os)
        jrow1.addStretch(1)
        form.addRow("线端随机", jrow1)
        jrow2 = QHBoxLayout()
        self.md_tab_rj = spin(0.0, 3.0, 0.0, " mm", 0.1, 2)
        self.md_tab_rj.setToolTip(
            "行界随机偏移 σ：各行高度不再一致（顶/底界与外框不动，\n"
            "单元格文字随行界走）。0 = 行高一致。")
        self.md_tab_cj = spin(0.0, 3.0, 0.0, " mm", 0.1, 2)
        self.md_tab_cj.setToolTip(
            "列界随机偏移 σ：各列宽度不再一致（外框不动；折行走抖动后的\n"
            "列宽，文字不会越过列线）。0 = 列宽规整。")
        jrow2.addWidget(QLabel("行界"))
        jrow2.addWidget(self.md_tab_rj)
        jrow2.addWidget(QLabel("列界"))
        jrow2.addWidget(self.md_tab_cj)
        jrow2.addStretch(1)
        form.addRow("网格抖动", jrow2)
        for w in (self.md_tab_ej, self.md_tab_os, self.md_tab_rj,
                  self.md_tab_cj):
            w.valueChanged.connect(self._update_preview)
        self.md_perturb = PerturbPanel()
        self.md_perturb.paramsChanged.connect(self._update_preview)
        lay.addLayout(form)

        self.md_fonts = _FontChainWidget(self.manager)
        if self._default_font_chain:
            self.md_fonts.set_names(self._default_font_chain)
        self.md_fonts.changed.connect(self._update_preview)
        lay.addWidget(self.md_fonts)
        lay.addWidget(self.md_perturb)
        self.stack.addWidget(page)

    # -- 公式页 ------------------------------------------------------------
    def _build_equation_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addWidget(QLabel("LaTeX 公式（无需安装 TeX，使用内置 mathtext 渲染）："))
        self.eq_edit = QTextEdit()
        self.eq_edit.setPlainText(_EQ_SAMPLE)
        self.eq_edit.setFont(self._mono())
        self.eq_edit.textChanged.connect(self._update_preview)
        lay.addWidget(self.eq_edit, 2)

        form = QFormLayout()
        self.eq_size = spin(1.0, 60.0, 6.0, " mm", 0.5)
        self.eq_size.valueChanged.connect(self._update_preview)
        form.addRow("公式字号", self.eq_size)
        self.eq_text_scale = spin(0.2, 5.0, 1.0, " ×", 0.05)
        self.eq_text_scale.setToolTip(
            "替换文字的字号微调（相对 mathtext 排版得到的字号）")
        form.addRow("文字字号", self.eq_text_scale)
        self.eq_text_scale.valueChanged.connect(self._update_preview)
        lay.addLayout(form)

        # 公式字体链：字母/数字等链上画得出的字符改用本软件字体（手写
        # 字迹）重排，位置与字号由 mathtext 保证；链画不出的数学符号
        # （∑ ∫ ≤ 等）与分式线保留 mathtext 轮廓。清空 = 全部用轮廓。
        self.eq_fonts = _FontChainWidget(self.manager)
        if self._default_font_chain:
            self.eq_fonts.set_names(self._default_font_chain)
        self.eq_fonts.changed.connect(self._update_preview)
        lay.addWidget(self.eq_fonts)

        tips = QLabel("示例：\\frac{a}{b}　\\sqrt{x^2+1}　\\sum_{i=1}^{n}　"
                      "\\int_0^\\infty　\\alpha \\beta \\gamma　x^{2}　a_{i}")
        tips.setWordWrap(True)
        tips.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(tips)
        lay.addStretch(1)
        self.stack.addWidget(page)

    # -- LaTeX 文档页 ------------------------------------------------------
    def _build_latex_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        report = toolchain_report()
        if report["ok"]:
            note = "使用外部 TeX 工具链编译（xelatex/pdflatex → PDF → SVG）。"
            color = "#2a7"
        else:
            note = ("未检测到 TeX 工具链：将用内置排版引擎渲染常用子集"
                    "（标题/章节/列表/公式/简单表格），零依赖可用；"
                    "安装 texlive + poppler 后自动改为忠实编译。")
            color = "#c33"
        lab = QLabel(note)
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{color};")
        lay.addWidget(lab)

        lay.addWidget(QLabel("LaTeX 源码（可含标题、公式、宏包）："))
        self.tex_edit = QTextEdit()
        self.tex_edit.setPlainText(_LATEX_SAMPLE)
        self.tex_edit.setFont(self._mono())
        lay.addWidget(self.tex_edit, 1)

        form = QFormLayout()
        self.tex_width = spin(10.0, 500.0, 150.0, " mm", 5.0)
        form.addRow("目标宽度", self.tex_width)
        lay.addLayout(form)
        # 内置引擎回退路径用字体链渲染文字（TeX 编译路径忽略此项）
        self.tex_fonts = _FontChainWidget(self.manager)
        if self._default_font_chain:
            self.tex_fonts.set_names(self._default_font_chain)
        self.tex_fonts.changed.connect(self._update_preview)
        lay.addWidget(self.tex_fonts)
        self.stack.addWidget(page)

    # -- TikZ 页 -----------------------------------------------------------
    def _build_tikz_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        rep = tikz_report()
        if rep["ok"]:
            note = ("使用 TeX 工具链编译 TikZ（standalone 裁切 → PDF → SVG）。"
                    "坐标即 TikZ 的 (x,y) 单位（默认 1cm）。")
            color = "#2a7"
        else:
            note = "TikZ 不可用：" + "；".join(rep["notes"])
            color = "#c33"
        lab = QLabel(note)
        lab.setWordWrap(True)
        lab.setStyleSheet(f"color:{color};")
        lay.addWidget(lab)

        lay.addWidget(QLabel("TikZ 源码（tikzpicture 内容；可含坐标轴、节点、函数曲线）："))
        self.tikz_edit = QTextEdit()
        self.tikz_edit.setPlainText(TIKZ_SAMPLE)
        self.tikz_edit.setFont(self._mono())
        lay.addWidget(self.tikz_edit, 1)

        form = QFormLayout()
        self.tikz_width = spin(0.0, 500.0, 0.0, " mm (0=原始尺寸)", 5.0)
        form.addRow("目标宽度", self.tikz_width)
        self.tikz_text_scale = spin(0.2, 5.0, 1.0, " ×", 0.05)
        self.tikz_text_scale.setToolTip(
            "替换文字的字号微调（相对 TeX 排版得到的字号）")
        form.addRow("文字字号", self.tikz_text_scale)
        lay.addLayout(form)

        # 文字字体：TikZ 节点/标注文字改用**本软件的字体**（如自己的手写真迹）
        # 重排——TeX 只负责把图形画准，文字笔画由本软件生成。
        # 图形线条与该设置无关。清空列表 = 保留 TeX 原生文字轮廓。
        self.tikz_fonts = _FontChainWidget(self.manager)
        if self._default_font_chain:
            self.tikz_fonts.set_names(self._default_font_chain)
        self.tikz_fonts.changed.connect(self._update_preview)
        lay.addWidget(self.tikz_fonts)
        self.stack.addWidget(page)

    # -- SVG 页 ------------------------------------------------------------
    def _build_svg_page(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        if not svg_available():
            lab = QLabel("未安装 svgelements，无法导入 SVG：pip install svgelements")
            lab.setStyleSheet("color:#c33;")
            lay.addWidget(lab)

        row = QHBoxLayout()
        self.svg_path = QLineEdit()
        self.svg_path.setPlaceholderText("选择 SVG 文件…")
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_svg)
        row.addWidget(self.svg_path, 1)
        row.addWidget(browse)
        lay.addLayout(row)

        form = QFormLayout()
        self.svg_width = QDoubleSpinBox()
        self.svg_width.setRange(0.0, 500.0)
        self.svg_width.setValue(0.0)
        self.svg_width.setSuffix(" mm (0=原始尺寸)")
        self.svg_width.setDecimals(1)
        form.addRow("目标宽度", self.svg_width)
        self.svg_wobble = spin(0.0, 5.0, 0.0, " mm", 0.05, 2)
        self.svg_wobble.setToolTip("给线条叠加多频正弦抖动，产生手绘感")
        self.svg_wobble.valueChanged.connect(self._update_preview)
        form.addRow("手绘抖动幅度", self.svg_wobble)
        self.svg_wl = spin(2.0, 200.0, 20.0, " mm", 2.0)
        form.addRow("抖动波长", self.svg_wl)
        lay.addLayout(form)

        lay.addWidget(QLabel("整笔扰动（可选）："))
        self.svg_perturb = PerturbPanel()
        lay.addWidget(self.svg_perturb)
        lay.addStretch(1)
        self.stack.addWidget(page)

    # --------------------------------------------------------------- 工具
    def _mono(self) -> QFont:
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        return f

    def _browse_svg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 SVG", filedialog.start_dir(), "SVG (*.svg)")
        if path:
            self.svg_path.setText(path)
            filedialog.remember(path)
            self._update_preview()

    def _select_kind(self, kind: str) -> None:
        idx = {KIND_MARKDOWN: 0, KIND_EQUATION: 1,
               KIND_LATEX: 2, KIND_TIKZ: 3, KIND_SVG: 4}.get(kind, 0)
        self.stack.setCurrentIndex(idx)
        self.kind_combo.setCurrentIndex(idx)
        self._update_preview()

    # ------------------------------------------------------------- 预览
    def _update_preview(self) -> None:
        kind = self.kind_combo.currentData()
        try:
            if kind == KIND_MARKDOWN:
                self._preview_markdown()
            elif kind == KIND_EQUATION:
                self._preview_equation()
            elif kind == KIND_LATEX:
                rep = toolchain_report()
                if rep["ok"]:
                    self.preview_label.setText(
                        "工具链就绪，点击确定开始编译（可能需要数秒）。")
                else:
                    nfonts = len(self.tex_fonts.names())
                    self.preview_label.setText(
                        "未检测到 TeX：将用内置排版引擎渲染常用子集"
                        "（标题/章节/列表/公式/简单表格），点击确定即插入。"
                        + (f"文字将用所选 {nfonts} 款本软件字体重排。"
                           if nfonts else ""))
            elif kind == KIND_TIKZ:
                rep = tikz_report()
                if not rep["ok"]:
                    self.preview_label.setText(
                        "TikZ 不可用，无法编译：" + "；".join(rep["notes"]))
                else:
                    nfonts = len(self.tikz_fonts.names())
                    self.preview_label.setText(
                        "TikZ 就绪，点击确定开始编译（可能需要数秒）。"
                        + (f"节点文字将用所选 {nfonts} 款本软件字体重排。"
                           if nfonts else
                           "未选字体：节点文字保留 TeX 轮廓。"))
            elif kind == KIND_SVG:
                self._preview_svg()
        except Exception as exc:
            self.preview_label.setText(f"预览失败：{exc}")

    def _preview_markdown(self) -> None:
        fonts = self.md_fonts.resolve()
        if not fonts:
            self.preview_label.setText("未选择有效字体")
            return
        from ..content.markdown import MarkdownStyle
        st = MarkdownStyle(size=self.md_size.value(),
                           char_spacing=self.md_cs.value(),
                           wrap_width=self.md_wrap.value(),
                           table_width=self.md_table_w.value(),
                           table_end_jitter=self.md_tab_ej.value(),
                           table_overshoot=self.md_tab_os.value(),
                           table_row_jitter=self.md_tab_rj.value(),
                           table_col_jitter=self.md_tab_cj.value())
        r = render_markdown(self.md_edit.toPlainText(), fonts, st)
        self.preview_label.setText(
            f"块类型：{', '.join(r.blocks) or '—'}\n"
            f"笔画 {len(r.strokes)} 条，尺寸 {r.width:.1f} × {r.height:.1f} mm")

    def _preview_equation(self) -> None:
        strokes = render_equation(
            self.eq_edit.toPlainText(), self.eq_size.value(),
            font_names=self.eq_fonts.names() or None,
            manager=self.manager,
            text_scale=self.eq_text_scale.value())
        from ..core.geometry import BBox
        b = BBox.from_points(p for s in strokes for p in s.points)
        if b.is_empty:
            self.preview_label.setText("（无内容）")
        else:
            replaced = sum(1 for s in strokes if s.role == "glyph")
            self.preview_label.setText(
                f"笔画 {len(strokes)} 条（字体替换 {replaced} 条），"
                f"尺寸 {b.width:.1f} × {b.height:.1f} mm")

    def _preview_svg(self) -> None:
        path = self.svg_path.text().strip()
        if not path or not Path(path).exists():
            self.preview_label.setText("请选择 SVG 文件")
            return
        from ..content import import_svg
        w = self.svg_width.value() or None
        r = import_svg(path, target_width_mm=w)
        extra = f"；{len(r.warnings)} 条警告" if r.warnings else ""
        self.preview_label.setText(
            f"矢量元素 {len(r.strokes)} 条，尺寸 {r.width:.1f} × {r.height:.1f} mm"
            + (f"（目标宽 {w:.0f} mm）" if w else "") + extra)

    # ------------------------------------------------------------- 结果
    def load_from_object(self, obj) -> None:
        """用已有对象的源内容填充对话框（用于「编辑」而非新建）。"""
        kind = obj.source.kind
        data = obj.source.data
        if kind == "markdown":
            self._select_kind(KIND_MARKDOWN)
            self.md_edit.setPlainText(data.get("text", ""))
            # 样式存在嵌套的 "style" 里（make_markdown_object 写入）；
            # 兼容历史版本的扁平键
            st = data.get("style") or {}
            if "size" in st:
                self.md_size.setValue(float(st["size"]))
            elif "size" in data:
                self.md_size.setValue(float(data["size"]))
            if "char_spacing" in st:
                self.md_cs.setValue(float(st["char_spacing"]))
            elif "char_spacing" in data:
                self.md_cs.setValue(float(data["char_spacing"]))
            if "wrap_width" in st:
                self.md_wrap.setValue(float(st["wrap_width"]))
            elif "wrap_width" in data:
                self.md_wrap.setValue(float(data["wrap_width"]))
            if "table_width" in st:
                self.md_table_w.setValue(float(st["table_width"]))
            elif "table_width" in data:
                self.md_table_w.setValue(float(data["table_width"]))
            for key, w in (("table_end_jitter", self.md_tab_ej),
                           ("table_overshoot", self.md_tab_os),
                           ("table_row_jitter", self.md_tab_rj),
                           ("table_col_jitter", self.md_tab_cj)):
                if key in st:
                    w.setValue(float(st[key]))
            names = data.get("font_names") or []
            if names:
                self.md_fonts.set_names(names)
            if data.get("perturb"):
                from ..perturb.params import PerturbParams
                self.md_perturb.set_params(PerturbParams.from_data(data["perturb"]))
        elif kind == "equation":
            self._select_kind(KIND_EQUATION)
            self.eq_edit.setPlainText(data.get("latex", ""))
            if "size" in data:
                self.eq_size.setValue(float(data["size"]))
            self.eq_text_scale.setValue(
                float(data.get("text_scale", 1.0) or 1.0))
            names = data.get("font_names") or []
            if names:
                self.eq_fonts.set_names(names)
        elif kind == "svg":
            self._select_kind(KIND_SVG)
            self.svg_path.setText(data.get("path", ""))
            if data.get("target_width"):
                self.svg_width.setValue(float(data["target_width"]))
            self.svg_wobble.setValue(float(data.get("wobble_amplitude", 0.0)))
            self.svg_wl.setValue(float(data.get("wobble_wavelength", 20.0)))
            if data.get("perturb"):
                from ..perturb.params import PerturbParams
                self.svg_perturb.set_params(PerturbParams.from_data(data["perturb"]))
        elif kind == "latex":
            self._select_kind(KIND_LATEX)
            self.tex_edit.setPlainText(data.get("source", ""))
            if data.get("target_width"):
                self.tex_width.setValue(float(data["target_width"]))
            names = data.get("font_names") or []
            if names:
                self.tex_fonts.set_names(names)
        elif kind == "tikz":
            self._select_kind(KIND_TIKZ)
            self.tikz_edit.setPlainText(data.get("source", ""))
            if data.get("target_width"):
                self.tikz_width.setValue(float(data["target_width"]))
            self.tikz_text_scale.setValue(
                float(data.get("text_scale", 1.0) or 1.0))
            names = data.get("font_names") or []
            if names:
                self.tikz_fonts.set_names(names)
        self._update_preview()

    def payload(self) -> ContentPayload:
        kind = self.kind_combo.currentData()
        if kind == KIND_MARKDOWN:
            return ContentPayload(kind, {
                "text": self.md_edit.toPlainText(),
                "size": self.md_size.value(),
                "char_spacing": self.md_cs.value(),
                "wrap_width": self.md_wrap.value(),
                "table_width": self.md_table_w.value(),
                "table_end_jitter": self.md_tab_ej.value(),
                "table_overshoot": self.md_tab_os.value(),
                "table_row_jitter": self.md_tab_rj.value(),
                "table_col_jitter": self.md_tab_cj.value(),
                "font_names": self.md_fonts.names(),
                "perturb": self.md_perturb.params().to_data(),
            })
        if kind == KIND_EQUATION:
            return ContentPayload(kind, {
                "latex": self.eq_edit.toPlainText(),
                "size": self.eq_size.value(),
                "font_names": self.eq_fonts.names(),
                "text_scale": self.eq_text_scale.value(),
            })
        if kind == KIND_LATEX:
            return ContentPayload(kind, {
                "source": self.tex_edit.toPlainText(),
                "target_width": self.tex_width.value() or None,
                "font_names": self.tex_fonts.names(),
            })
        if kind == KIND_TIKZ:
            return ContentPayload(kind, {
                "source": self.tikz_edit.toPlainText(),
                "target_width": self.tikz_width.value() or None,
                "font_names": self.tikz_fonts.names(),
                "text_scale": self.tikz_text_scale.value(),
            })
        return ContentPayload(kind, {
            "path": self.svg_path.text().strip(),
            "target_width": self.svg_width.value() or None,
            "wobble_amplitude": self.svg_wobble.value(),
            "wobble_wavelength": self.svg_wl.value(),
            "perturb": self.svg_perturb.params().to_data(),
        })


class _FontChainWidget(QWidget):
    """简化的字体回退链选择控件（下拉 + 顺序列表）。"""

    changed = Signal()

    def __init__(self, manager: FontManager, parent=None) -> None:
        super().__init__(parent)
        self.manager = manager
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("字体（回退链，先匹配者优先）："))

        self.list = QListWidget()
        # 固定高度：只设 setMaximumHeight 会与 QListWidget 自带的
        # minimumSizeHint(~87px) 冲突（min>max，布局未定义行为）
        self.list.setFixedHeight(88)
        lay.addWidget(self.list)

        row = QHBoxLayout()
        self.combo = QComboBox()
        for e in manager.visible_entries():
            self.combo.addItem(f"{e.display_name}  [{e.kind}]", e.name)
        row.addWidget(self.combo, 1)
        add = QPushButton("添加")
        add.clicked.connect(self._add)
        rm = QPushButton("移除")
        rm.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(rm)
        lay.addLayout(row)

        # 默认链
        for e in manager.entries():
            if e.kind == "stroke-json":
                self._append(e.name)
                break
        for cand in ("futural", "futuram", "scripts"):
            if manager.get_entry(cand):
                self._append(cand)
                break

    def _append(self, name: str) -> None:
        e = self.manager.get_entry(name)
        label = f"{e.display_name}  [{e.kind}]" if e else name
        item = QListWidgetItem(label)
        item.setData(Qt.UserRole, name)
        self.list.addItem(item)

    def _add(self) -> None:
        name = self.combo.currentData()
        if name:
            self._append(name)
            self.changed.emit()

    def _remove(self) -> None:
        r = self.list.currentRow()
        if r >= 0:
            self.list.takeItem(r)
            self.changed.emit()

    def names(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def set_names(self, names: list[str]) -> None:
        self.list.clear()
        for n in names:
            self._append(n)
        self.changed.emit()

    def resolve(self):
        spec = TextSpec(font_names=self.names())
        return resolve_fonts(spec, self.manager)
