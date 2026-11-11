"""文本编辑对话框：内容 + 字体回退链 + 排版参数，带实时预览。

「字体回退链」是单文档多字体的操作方式：把中文字体与英文字体都加入列表，
排版时自动按字选取首个含该字的字体，从而实现中英混排。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


from ..i18n import tr, trf
from ..fonts.builder import TextSpec
from ..fonts.manager import FontManager
from .perturb_panel import PerturbPanel
from .style import (fit_dialog_to_screen, localize_dialog_buttons,
                    set_role, spin, unify_inputs)


class TextEditDialog(QDialog):
    """编辑一个文本对象的源参数。"""

    def __init__(self, spec: TextSpec, manager: FontManager,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("文本编辑"))
        # 布局是「上：文本内容；中：字体链 + 排版参数（横向双栏）」。
        # 最小宽 780：英文双栏内容实测最小约 722px，加上滚动条/边距仍不会
        # 横向挤压；高度下限 560，更矮的屏幕由内容页自身滚动兜底。
        self.setMinimumSize(780, 560)
        self.manager = manager
        self._spec = spec.clone()
        # 在 _build_ui 之前初始化：构建控件时会触发预览刷新，需要这些字段存在
        self._weights: dict[str, float] = {}
        self._font_seed = int(spec.font_seed)
        self._overrides: dict[str, dict] = {}

        self._build_ui()
        self._load_spec(spec)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        # ===== 页签 1：内容与排版 =====
        content = QWidget()
        # 内容页整体可滚动：窗口被屏幕高度限制时，文字框仍至少保留 10 行，
        # 其余参数往下滚，而不是把文字框压成两三行。
        content_scroll = QScrollArea()
        content_scroll.setWidgetResizable(True)
        content_scroll.setFrameShape(QFrame.NoFrame)
        # 内容页最小宽约 510px，而对话框最小宽 760：横向永远不会溢出，
        # 关掉横向滚动条避免首次布局时闪出一条无用的横条。
        content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content_scroll.setWidget(content)
        self.tabs.addTab(content_scroll, tr("内容 / 排版"))
        croot = QVBoxLayout(content)

        # 文本内容
        croot.addWidget(QLabel(tr("文本内容：")))
        self.text_edit = QTextEdit()
        self.text_edit.setAcceptRichText(False)
        f = QFont("monospace")
        f.setStyleHint(QFont.Monospace)
        self.text_edit.setFont(f)
        # 至少 12 行：编辑长文本时不必在一个小窗口里来回滚
        self.text_edit.setMinimumHeight(240)
        self.text_edit.textChanged.connect(self._update_preview)
        croot.addWidget(self.text_edit, 3)

        # 中间：字体链 + 排版参数。给 mid 拉伸权重：否则额外高度全被
        # 上面的文本框吃掉，字体链列表被压到只剩一两项可显示
        mid = QHBoxLayout()
        croot.addLayout(mid, 2)

        # 字体回退链
        chain_box = QVBoxLayout()
        chain_box.addWidget(QLabel(tr("字体（按顺序回退）：")))
        self.chain_list = QListWidget()
        self.chain_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.chain_list.setMinimumHeight(100)   # 至少可见 4~5 项
        self.chain_list.currentRowChanged.connect(self._on_chain_row_changed)
        chain_box.addWidget(self.chain_list, 1)

        ch_btns = QHBoxLayout()
        self.font_combo = QComboBox()
        for e in self.manager.visible_entries():
            self.font_combo.addItem(f"{e.display_name}  [{e.kind}]", e.name)
        ch_btns.addWidget(self.font_combo, 1)
        add_btn = QPushButton(tr("添加"))
        add_btn.clicked.connect(self._add_font)
        ch_btns.addWidget(add_btn)
        chain_box.addLayout(ch_btns)

        up_btn = QPushButton(tr("上移"))
        up_btn.clicked.connect(lambda: self._move_font(-1))
        down_btn = QPushButton(tr("下移"))
        down_btn.clicked.connect(lambda: self._move_font(1))
        del_btn = QPushButton(tr("移除"))
        del_btn.clicked.connect(self._remove_font)
        row = QHBoxLayout()
        row.addWidget(up_btn)
        row.addWidget(down_btn)
        row.addWidget(del_btn)
        chain_box.addLayout(row)

        # 选中字体的权重（仅在「按权重随机」时生效）
        wrow = QHBoxLayout()
        wrow.addWidget(QLabel(tr("选中字体权重")))
        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 100.0)
        self.weight_spin.setSingleStep(0.1)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setValue(1.0)
        self.weight_spin.setToolTip(tr("越大越常被选中（0 = 不参与随机）"))
        self.weight_spin.valueChanged.connect(self._on_weight_changed)
        wrow.addWidget(self.weight_spin, 1)
        reset_btn = QPushButton(tr("恢复 1.0"))
        reset_btn.clicked.connect(lambda: self.weight_spin.setValue(1.0))
        wrow.addWidget(reset_btn)
        chain_box.addLayout(wrow)

        self.random_check = QCheckBox(tr("按权重随机选用字体"))
        self.random_check.setToolTip(
            tr("在字体链中按权重随机选用（模拟不同字体混写）"))
        self.random_check.toggled.connect(self._update_preview)
        chain_box.addWidget(self.random_check)

        fbrow = QHBoxLayout()
        fbrow.addWidget(QLabel(tr("兜底字体")))
        self.fallback_combo = QComboBox()
        self.fallback_combo.setToolTip(
            tr("链中所有字体都缺某个字时，用它补上；\n"
            "（链内字体本身已互相兜底，这里只是最后一道保险）"))
        self.fallback_combo.addItem(tr("（无）"), "")
        for e in self.manager.visible_entries():
            self.fallback_combo.addItem(f"{e.display_name}  [{e.kind}]", e.name)
        self.fallback_combo.currentIndexChanged.connect(self._update_preview)
        fbrow.addWidget(self.fallback_combo, 1)
        self.reseed_font_btn = QPushButton(tr("重摇字体"))
        self.reseed_font_btn.setToolTip(tr("换一个种子，重新随机分配各字使用哪款字体"))
        from .icons import action_icon
        _shf = action_icon("reshuffle_font")
        if _shf is not None:
            self.reseed_font_btn.setIcon(_shf)
        self.reseed_font_btn.clicked.connect(self._reseed_fonts)
        fbrow.addWidget(self.reseed_font_btn)
        chain_box.addLayout(fbrow)

        # 逐字符字体覆盖：为指定字符单独设置另一套字体（如个别符号）
        ov_label = QLabel(tr("字符字体覆盖："))
        ov_label.setToolTip(tr("在文本内容中选中字符，再设为上方下拉的当前字体；"
                            "每行一条「字符 → 字体」覆盖。"))
        chain_box.addWidget(ov_label)
        self.ov_list = QListWidget()
        # 固定高度：只设 setMaximumHeight 会与 QListWidget 自带的
        # minimumSizeHint(~87px) 冲突（min>max，布局未定义行为）
        self.ov_list.setFixedHeight(56)
        self.ov_list.setToolTip(tr("每行一条「字符 → 字体」覆盖；选中后可清除"))
        chain_box.addWidget(self.ov_list)
        ovrow = QHBoxLayout()
        ovrow.addWidget(QLabel(tr("缩放")))
        self.ov_scale_spin = spin(0.1, 3.0, 1.0, " ×", step=0.05)
        ovrow.addWidget(self.ov_scale_spin)
        set_ov_btn = QPushButton(tr("设为所选字符字体"))
        set_ov_btn.setToolTip(tr("先在「文本内容」里选中字符，再点此把它们设为"
                              "上方下拉当前字体（覆盖回退链与随机挑选）"))
        set_ov_btn.clicked.connect(self._apply_char_override)
        ovrow.addWidget(set_ov_btn, 1)
        clr_ov_btn = QPushButton(tr("清除覆盖"))
        clr_ov_btn.clicked.connect(self._clear_selected_override)
        ovrow.addWidget(clr_ov_btn)
        chain_box.addLayout(ovrow)
        mid.addLayout(chain_box, 3)

        # 排版参数
        param_box = QVBoxLayout()
        param_box.addWidget(QLabel(tr("排版参数：")))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.size_spin = spin(0.5, 500.0, 10.0, " mm")
        self.size_spin.valueChanged.connect(self._update_preview)
        form.addRow(tr("字号"), self.size_spin)

        self.ls_spin = spin(0.5, 5.0, 1.4, " ×", step=0.05)
        self.ls_spin.valueChanged.connect(self._update_preview)
        form.addRow(tr("行距"), self.ls_spin)

        self.cs_spin = spin(-20.0, 50.0, 0.0, " mm", step=0.1)
        self.cs_spin.valueChanged.connect(self._update_preview)
        form.addRow(tr("字距"), self.cs_spin)

        self.fw_spin = spin(0.0, 2000.0, 0.0, " mm", step=5.0)
        self.fw_spin.setToolTip(
            tr("文本框宽度：大于 0 时按该宽度自动换行（0 = 不限宽）。\n"
            "也可以之后在画布上直接拖动文本框右缘的手柄调整。"))
        self.fw_spin.valueChanged.connect(self._update_preview)
        form.addRow(tr("文本框宽度"), self.fw_spin)

        self.align_combo = QComboBox()
        self.align_combo.addItem(tr("左对齐"), "left")
        self.align_combo.addItem(tr("居中"), "center")
        self.align_combo.addItem(tr("右对齐"), "right")
        self.align_combo.currentIndexChanged.connect(self._update_preview)
        form.addRow(tr("对齐"), self.align_combo)

        self.dir_combo = QComboBox()
        self.dir_combo.addItem(tr("横排"), "h")
        self.dir_combo.addItem(tr("竖排·右到左"), "v-rl")
        self.dir_combo.addItem(tr("竖排·左到右"), "v-lr")
        self.dir_combo.currentIndexChanged.connect(self._update_preview)
        form.addRow(tr("书写方向"), self.dir_combo)

        self.ws_spin = spin(0.0, 400.0, 100.0, " %", step=5.0)
        self.ws_spin.setDecimals(0)
        self.ws_spin.setToolTip(tr("空格宽度百分比（100=字体默认步距）"))
        self.ws_spin.valueChanged.connect(self._update_preview)
        form.addRow(tr("词距"), self.ws_spin)

        self.wsr_spin = spin(0.0, 100.0, 0.0, " %", step=5.0)
        self.wsr_spin.setDecimals(0)
        self.wsr_spin.setToolTip(tr("空格宽度随机幅度（±%），模拟手写时词距忽大忽小"))
        self.wsr_spin.valueChanged.connect(self._update_preview)
        form.addRow(tr("词距随机"), self.wsr_spin)

        param_box.addLayout(form)
        self.preview_label = QLabel(tr("—"))
        self.preview_label.setWordWrap(True)
        set_role(self.preview_label, "muted")
        param_box.addWidget(self.preview_label)
        param_box.addStretch(1)
        mid.addLayout(param_box, 2)

        # ===== 页签 2：手写扰动 =====
        self.perturb_panel = PerturbPanel(params=self._spec.perturb, size_hint=10.0)
        self.perturb_panel.paramsChanged.connect(self._update_preview)
        self.perturb_panel.set_size_hint(self.size_spin.value())
        self.tabs.addTab(self.perturb_panel, tr("手写扰动"))

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        localize_dialog_buttons(self)
        unify_inputs(self)
        # 默认给文字框更宽裕的高度；小屏由屏幕可用区域收敛，内容页可滚动。
        fit_dialog_to_screen(self, 800, 820)

    # ------------------------------------------------------------- 数据装载
    def _load_spec(self, spec: TextSpec) -> None:
        self.text_edit.setPlainText(spec.text)
        self._weights = dict(spec.font_weights)
        self._font_seed = int(spec.font_seed)
        self._overrides = {k: dict(v)
                           for k, v in getattr(spec, "char_overrides", {}).items()}
        self._refresh_override_list()
        for name in spec.font_names:
            entry = self.manager.get_entry(name)
            label = f"{entry.display_name}  [{entry.kind}]" if entry else name
            self.chain_list.addItem(label)
            item = self.chain_list.item(self.chain_list.count() - 1)
            item.setData(Qt.UserRole, name)
            self._set_item_weight(item, self._weights.get(name, 1.0))
        if self.chain_list.count() == 0:
            # 默认给一个英文字体
            for cand in ("futural", "scripts", "cursive"):
                if self.manager.get_entry(cand):
                    self._append_font(cand)
                    break
        self.random_check.setChecked(bool(spec.random_fonts))
        idx = self.fallback_combo.findData(spec.fallback_font or "")
        self.fallback_combo.setCurrentIndex(max(0, idx))
        self.size_spin.setValue(spec.size)
        self.ls_spin.setValue(spec.line_spacing)
        self.cs_spin.setValue(spec.char_spacing)
        self.fw_spin.setValue(float(getattr(spec, "frame_width", 0.0) or 0.0))
        ai = self.align_combo.findData(spec.align)
        if ai >= 0:
            self.align_combo.setCurrentIndex(ai)
        di = self.dir_combo.findData(getattr(spec, "direction", "h") or "h")
        if di >= 0:
            self.dir_combo.setCurrentIndex(di)
        self.ws_spin.setValue(float(getattr(spec, "word_space", 100.0)))
        self.wsr_spin.setValue(float(getattr(spec, "word_space_random", 0.0)))
        if self.chain_list.count():
            self.chain_list.setCurrentRow(0)
        self._update_preview()

    @staticmethod
    def _set_item_weight(item, weight: float) -> None:
        base = item.text().split("  ×", 1)[0]
        item.setText(f"{base}  ×{weight:g}")

    def _on_chain_row_changed(self, row: int) -> None:
        if row >= 0:
            name = self.chain_list.item(row).data(Qt.UserRole)
            self.weight_spin.blockSignals(True)
            self.weight_spin.setValue(self._weights.get(name, 1.0))
            self.weight_spin.blockSignals(False)
        self._update_preview()

    def _on_weight_changed(self, value: float) -> None:
        row = self.chain_list.currentRow()
        if row < 0:
            return
        item = self.chain_list.item(row)
        name = item.data(Qt.UserRole)
        self._weights[name] = float(value)
        self._set_item_weight(item, value)
        self._update_preview()

    def _reseed_fonts(self) -> None:
        self._font_seed = (self._font_seed * 1103515245 + 12345) % (2 ** 31)
        self._update_preview()

    # ----------------------------------------------------- 字符字体覆盖
    def _apply_char_override(self) -> None:
        name = self.font_combo.currentData()
        if not name:
            return
        sel = self.text_edit.textCursor().selectedText()
        if not sel:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, tr("提示"),
                                    tr("请先在「文本内容」中选中要设置字体的字符。"))
            return
        scale = self.ov_scale_spin.value()
        for ch in set(sel.replace("\u2029", "")):
            if ch.strip():
                self._overrides[ch] = {"font": name, "scale": scale}
        self._refresh_override_list()
        self._update_preview()

    def _clear_selected_override(self) -> None:
        row = self.ov_list.currentRow()
        if row < 0:
            return
        self._overrides.pop(self.ov_list.item(row).data(Qt.UserRole), None)
        self._refresh_override_list()
        self._update_preview()

    def _refresh_override_list(self) -> None:
        self.ov_list.clear()
        for ch, ov in self._overrides.items():
            entry = self.manager.get_entry(str(ov.get("font", "")))
            label = entry.display_name if entry else str(ov.get("font", "?"))
            shown = ch if ch.strip() else repr(ch)
            item = QListWidgetItem(trf('「{v0}」→ {v1} ×{v2:g}', v0=shown, v1=label, v2=ov.get('scale', 1.0)))
            item.setData(Qt.UserRole, ch)
            self.ov_list.addItem(item)

    def _append_font(self, name: str) -> None:
        entry = self.manager.get_entry(name)
        label = f"{entry.display_name}  [{entry.kind}]" if entry else name
        self.chain_list.addItem(label)
        item = self.chain_list.item(self.chain_list.count() - 1)
        item.setData(Qt.UserRole, name)
        self._set_item_weight(item, self._weights.get(name, 1.0))

    def _add_font(self) -> None:
        name = self.font_combo.currentData()
        if name:
            self._append_font(name)
            self._update_preview()

    def _remove_font(self) -> None:
        row = self.chain_list.currentRow()
        if row >= 0:
            self.chain_list.takeItem(row)
            self._update_preview()

    def _move_font(self, delta: int) -> None:
        row = self.chain_list.currentRow()
        new = row + delta
        if row < 0 or new < 0 or new >= self.chain_list.count():
            return
        item = self.chain_list.takeItem(row)
        self.chain_list.insertItem(new, item)
        self.chain_list.setCurrentRow(new)
        self._update_preview()

    # --------------------------------------------------------------- 预览
    def _current_spec(self) -> TextSpec:
        names = [self.chain_list.item(i).data(Qt.UserRole)
                 for i in range(self.chain_list.count())]
        return TextSpec(
            text=self.text_edit.toPlainText(),
            font_names=names,
            size=self.size_spin.value(),
            line_spacing=self.ls_spin.value(),
            char_spacing=self.cs_spin.value(),
            frame_width=self.fw_spin.value(),
            direction=self.dir_combo.currentData(),
            word_space=self.ws_spin.value(),
            word_space_random=self.wsr_spin.value(),
            align=self.align_combo.currentData(),
            perturb=self.perturb_panel.params(),
            font_weights={n: self._weights.get(n, 1.0) for n in names},
            random_fonts=self.random_check.isChecked(),
            fallback_font=self.fallback_combo.currentData() or "",
            font_seed=self._font_seed,
            char_overrides=dict(self._overrides),
        )

    def _update_preview(self) -> None:
        spec = self._current_spec()
        from ..fonts import resolve_fonts, render_text_strokes
        fonts = resolve_fonts(spec, self.manager)
        if not fonts:
            self.preview_label.setText(tr("未选择有效字体"))
            return
        if self.perturb_panel.isVisible() or spec.perturb.is_active():
            # 尺寸提示随字号更新
            self.perturb_panel.set_size_hint(spec.size)
        strokes, lay, pr = render_text_strokes(spec, fonts)
        from ..core.geometry import BBox
        box = BBox.from_points(p for s in strokes for p in s.points)
        if box.is_empty:
            self.preview_label.setText(tr("（无内容）"))
            return
        # 统计各字体承担的字数
        usage: dict[str, int] = {}
        for c in lay.chars:
            usage[c.font_name] = usage.get(c.font_name, 0) + 1
        parts = tr("，").join(trf('{v0} {v1}字', v0=k, v1=v) for k, v in usage.items())
        notes = []
        if pr is not None:
            notes.append(tr("含手写扰动"))
        if spec.random_fonts:
            notes.append(tr("字体按权重随机"))
        if spec.char_overrides:
            notes.append(trf('字符覆盖 {v0} 项', v0=len(spec.char_overrides)))
        if spec.fallback_font:
            notes.append(trf('兜底：{v0}', v0=spec.fallback_font))
        missing = list(getattr(lay, "missing", []) or [])
        miss_line = ""
        if missing:
            miss_line = (tr("\n缺字未绘制：") + "".join(missing[:20])
                         + (trf(' 等 {v0} 个', v0=len(missing)) if len(missing) > 20 else ""))
        note_txt = trf('（{v0}）', v0=tr('，').join(notes)) if notes else ""
        self.preview_label.setText(
            trf('尺寸：{v0:.1f} × {v1:.1f} mm {v2}\n{v3} 行，{v4} 字\n字体使用：{v5}{v6}', v0=box.width, v1=box.height, v2=note_txt, v3=len(lay.lines), v4=len(lay.chars), v5=parts or '—', v6=miss_line)
        )

    def result_spec(self) -> TextSpec:
        return self._current_spec()
