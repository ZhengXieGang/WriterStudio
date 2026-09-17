"""手写扰动参数控件：三级扰动的滑杆组 + 预设 + 一键重摇。

可嵌入文本编辑对话框，也可独立用于矢量图/手绘对象。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..perturb.params import PRESETS, PerturbParams
from .style import unify_inputs

_PRESET_ORDER = ["关闭", "轻微", "自然", "强烈", "手绘线条", "自定义"]


class PerturbPanel(QWidget):
    """扰动参数编辑面板。参数变化时发出 :attr:`paramsChanged`。"""

    paramsChanged = Signal()
    reseedRequested = Signal()

    #: 用户连续调整控件时的防抖间隔(ms)：停顿后统一应用一次。
    #: 每个刻度都实时应用会让大文档整篇重排（选中越多越卡）。
    DEBOUNCE_MS = 80

    def __init__(self, params: Optional[PerturbParams] = None,
                 size_hint: float = 10.0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._params = (params or PerturbParams()).clone()
        self._size_hint = size_hint
        self._loading = False
        self._build_ui()
        # 参数改动防抖：spinbox 按住连点/拖动时每个刻度都会触发一次
        # 「快照 + 整篇重排 + 全场景同步」，大文档明显卡顿。停顿
        # DEBOUNCE_MS 后统一发一次 paramsChanged；flush() 可立即落盘。
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(self.DEBOUNCE_MS)
        self._debounce.timeout.connect(self.paramsChanged.emit)
        self._load(self._params)

    def flush(self) -> None:
        """立即发出挂起的参数改动（关闭窗口/导出前调用，避免丢最后一次调整）。"""
        if self._debounce.isActive():
            self._debounce.stop()
            self.paramsChanged.emit()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # 顶部：启用一行；预设 + 重摇一行。挤一行时最小宽 ~324px，
        # 超过默认停靠宽 280px，整列会被强制撑宽、画布被挤出画面外
        top = QHBoxLayout()
        self.enable_check = QCheckBox("启用手写扰动")
        self.enable_check.toggled.connect(self._on_changed)
        top.addWidget(self.enable_check)
        top.addStretch(1)
        outer.addLayout(top)

        top2 = QHBoxLayout()
        top2.addWidget(QLabel("预设:"))
        self.preset_combo = QComboBox()
        for name in _PRESET_ORDER:
            self.preset_combo.addItem(name)
        self.preset_combo.currentTextChanged.connect(self._apply_preset)
        top2.addWidget(self.preset_combo, 1)

        self.reseed_btn = QPushButton("重新随机")
        self.reseed_btn.setToolTip("更换随机种子，在保持参数不变的情况下换个手写效果")
        from .icons import icon as _qicon
        _dice = _qicon("reseed")
        if _dice is not None:
            self.reseed_btn.setIcon(_dice)
        self.reseed_btn.clicked.connect(self._on_reseed)
        top2.addWidget(self.reseed_btn)
        outer.addLayout(top2)

        self.seed_label = QLabel()
        self.seed_label.setStyleSheet("color:#666;")
        outer.addWidget(self.seed_label)

        # 总强度（一条滑杆统一放大/缩小所有扰动）——矢量线条也靠它调强弱
        inten_row = QHBoxLayout()
        inten_row.addWidget(QLabel("总强度"))
        self.intensity_slider = QSlider(Qt.Horizontal)
        self.intensity_slider.setRange(0, 300)      # 0.00 ~ 3.00
        self.intensity_slider.setToolTip(
            "统一放大/缩小下面所有扰动量的总强度系数（1.0 = 标准）")
        self.intensity_slider.valueChanged.connect(self._on_intensity_slider)
        self.intensity_spin = QDoubleSpinBox()
        self.intensity_spin.setRange(0.0, 10.0)
        self.intensity_spin.setSingleStep(0.05)
        self.intensity_spin.setDecimals(2)
        self.intensity_spin.setSuffix(" ×")
        self.intensity_spin.valueChanged.connect(self._on_intensity_spin)
        inten_row.addWidget(self.intensity_slider, 1)
        inten_row.addWidget(self.intensity_spin)
        outer.addLayout(inten_row)

        # 参数区（可滚动）
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        vbox = QVBoxLayout(body)
        vbox.setContentsMargins(0, 0, 0, 0)

        self._spin: dict[str, QDoubleSpinBox] = {}

        g_char = QGroupBox("字符级扰动")
        f1 = QFormLayout(g_char)
        self._add(f1, "size_sigma", "字号抖动", 0.0, 0.30, 0.005, " 比例", 3)
        self._add(f1, "char_x_sigma", "水平位移", 0.0, 100.0, 0.1, " mm")
        self._add(f1, "char_y_sigma", "垂直位移", 0.0, 100.0, 0.1, " mm")
        self._add(f1, "char_rot_sigma", "字符旋转", 0.0, 20.0, 0.1, " °")
        self._add(f1, "char_shear_sigma", "体态斜切", 0.0, 15.0, 0.1, " °")
        self._spin["char_shear_sigma"].setToolTip(
            "字符整体斜切（x 随 y 线性偏移），让同一字的体态/肩线各不相同；\n"
            "旋转保持正交，产生不了这种形态变化。")
        vbox.addWidget(g_char)

        g_line = QGroupBox("行级扰动")
        f2 = QFormLayout(g_line)
        self._add(f2, "line_y_sigma", "行基线抖动", 0.0, 200.0, 0.1, " mm")
        self._add(f2, "line_start_sigma", "每行首字偏移", 0.0, 200.0, 0.1, " mm")
        self._add(f2, "line_tilt_sigma", "行尾落差", 0.0, 200.0, 0.1, " mm")
        self._spin["line_tilt_sigma"].setToolTip(
            "行尾相对行首的高度漂移——人写的每行都不水平，\n"
            "写一行就会往上飘或往下沉一点。")
        self._add(f2, "char_spacing_sigma", "字距抖动", 0.0, 100.0, 0.05, " mm")
        self._spin["char_spacing_sigma"].setToolTip(
            "字距沿行平滑起伏 + 逐字节奏（均值回归，不会越排越散）。\n"
            "逐字独立的小抖动由「字符级 → 水平位移」提供。")
        self._add(f2, "word_gap_em", "词间距", 0.0, 2.0, 0.02, " em", 2)
        self._spin["word_gap_em"].setToolTip(
            "词与词、标点之后额外留出的空隙（字号的倍数），\n"
            "词内字距自动聚拢——词内紧、词间松。0 = 关闭分组。")
        vbox.addWidget(g_line)

        # 基线起伏：行尺度的随机缓弧 + 中尺度游走（每行形状都不同、
        # 行尾不回到行首高度）。旧的固定周期正弦波一眼就是机器画的。
        g_sine = QGroupBox("基线起伏")
        f3 = QFormLayout(g_sine)
        self._add(f3, "sine_amplitude", "游走振幅", 0.0, 100.0, 0.1, " mm")
        self._spin["sine_amplitude"].setToolTip(
            "基线游走的幅度：行整体在垂直方向的漂移量。\n"
            "每行独立随机生成，不是固定周期的波浪。")
        self._add(f3, "sine_wavelength", "起伏尺度", 1.0, 4000.0, 1.0, " mm")
        self._spin["sine_wavelength"].setToolTip(
            "行内起伏的空间尺度：越大越接近整行一道缓弧，\n"
            "越小则行内多几个缓和的起伏（仍非周期性波浪）。")
        vbox.addWidget(g_sine)

        g_stroke = QGroupBox("笔画级扰动")
        f4 = QFormLayout(g_stroke)
        self._add(f4, "stroke_x_sigma", "笔画水平位移", 0.0, 50.0, 0.01, " mm", 3)
        self._add(f4, "stroke_y_sigma", "笔画垂直位移", 0.0, 50.0, 0.01, " mm", 3)
        self._add(f4, "stroke_theta_sigma", "笔画旋转", 0.0, 30.0, 0.1, " °")
        self._add(f4, "stroke_stretch_sigma", "笔画伸缩", 0.0, 0.50, 0.01, "", 3)
        self._add(f4, "stroke_trim_mm", "末端修剪", 0.0, 10.0, 0.05, " mm")
        self._spin["stroke_stretch_sigma"].setToolTip(
            "单笔画沿自身走向随机伸缩，让笔画长短/舒展程度各不相同。")
        self._spin["stroke_trim_mm"].setToolTip(
            "每条笔画两端各随机剪掉 0~该值，起收笔长短参差、更接近手写。")
        vbox.addWidget(g_stroke)

        # 线条起伏：只作用于图形线条（矢量图/表格线/边框/签名等），
        # 不作用于文字笔画——弯折文字会让字形变形
        g_wobble = QGroupBox("线条起伏（仅图形线条）")
        f6 = QFormLayout(g_wobble)
        self._add(f6, "line_wobble", "起伏振幅", 0.0, 30.0, 0.01, " mm", 3)
        self._add(f6, "line_wobble_wavelength", "起伏波长", 0.5, 2000.0, 0.5, " mm")
        self._add(f6, "line_tremor", "细微颤抖", 0.0, 10.0, 0.01, " mm", 3)
        self._spin["line_wobble"].setToolTip(
            "沿图形线条法向的平滑抖动，模拟人手画线的轻微摆动；\n"
            "作用于 SVG/表格线/边框等图形线条，不影响文字笔画。")
        self._spin["line_tremor"].setToolTip(
            "在起伏之上叠加一层高频微抖，模拟手部肌肉的细微颤抖；\n"
            "同样只作用于图形线条，不影响文字笔画。")
        vbox.addWidget(g_wobble)

        g_misc = QGroupBox("笔锋与平滑")
        f5 = QFormLayout(g_misc)
        self._add(f5, "flare_mm", "笔锋长度", 0.0, 5.0, 0.1, " mm")
        self._add(f5, "smoothing", "笔画平滑", 0.0, 1.0, 0.05, "", 2)
        self._spin["flare_mm"].setToolTip(
            "收笔沿出笔方向甩出渐细的尖、起笔斜切入笔，模拟毛笔出锋；\n"
            "0.8~1.5mm 适合 15~20mm 字。只需笔锋也生效（无需启用整体扰动）。")
        self._spin["smoothing"].setToolTip(
            "对扰动后的笔画做邻点平均的轻微平滑，削弱折线毛刺。")
        vbox.addWidget(g_misc)

        g_out = QGroupBox("输出质量")
        f7 = QFormLayout(g_out)
        self._add(f7, "simplify_mm", "抽稀容差", 0.0, 2.0, 0.05, " mm")
        self._spin["simplify_mm"].setToolTip(
            "对扰动输出做 Douglas–Peucker 抽稀（与导出抽稀同类），\n"
            "减少微小线段让高速书写更顺滑；0=不抽稀。0.1~0.3 一般够用，\n"
            "过大会明显失真。")
        vbox.addWidget(g_out)

        vbox.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        # 与机器控制面板同规格（用户要求各板块观感一致）：
        # 高度取全局 FIELD_HEIGHT、最小宽 88px、按钮最小高 30px
        unify_inputs(self, min_width=88, button_height=30)

    def _add(self, form: QFormLayout, field: str, label: str,
             lo: float, hi: float, step: float, suffix: str, decimals: int = 2) -> None:
        # 面板变宽时输入框跟着伸展，而不是挤在左侧一小块
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setSuffix(suffix)
        sp.setDecimals(decimals)
        sp.valueChanged.connect(self._on_changed)
        form.addRow(label, sp)
        self._spin[field] = sp

    # ------------------------------------------------------------- 数据同步
    def _load(self, p: PerturbParams) -> None:
        self._loading = True
        self.enable_check.setChecked(p.enabled)
        for field, sp in self._spin.items():
            sp.setValue(getattr(p, field))
        self.intensity_spin.setValue(p.intensity)
        self.intensity_slider.setValue(int(round(p.intensity * 100)))
        self.preset_combo.setCurrentText(self._detect_preset(p))
        self._loading = False
        self._update_seed_label()
        self._update_enabled_state()

    def _on_intensity_slider(self, value: int) -> None:
        if self._loading:
            return
        self._loading = True
        self.intensity_spin.setValue(value / 100.0)
        self._loading = False
        self._on_changed()

    def _on_intensity_spin(self, value: float) -> None:
        if self._loading:
            return
        self._loading = True
        self.intensity_slider.setValue(int(round(value * 100)))
        self._loading = False
        self._on_changed()

    def _detect_preset(self, p: PerturbParams) -> str:
        """根据当前参数值反查匹配的预设名，找不到则返回「自定义」。"""
        if not p.enabled:
            return "关闭"
        size = self._size_hint
        for name in ("轻微", "自然", "强烈", "手绘线条"):
            factory = PRESETS.get(name)
            if factory is None:
                continue
            ref = factory(size)
            if all(abs(getattr(p, f) - getattr(ref, f)) < 1e-6
                   for f in self._spin):
                return name
        return "自定义"

    def _collect(self) -> PerturbParams:
        p = self._params.clone()
        p.enabled = self.enable_check.isChecked()
        for field, sp in self._spin.items():
            setattr(p, field, sp.value())
        p.intensity = self.intensity_spin.value()
        return p

    def _on_changed(self, *args) -> None:
        if self._loading:
            return
        self._params = self._collect()
        # 手动改动参数后切换到「自定义」
        if self.preset_combo.currentText() not in ("自定义",):
            self._loading = True
            self.preset_combo.setCurrentText("自定义")
            self._loading = False
        self._update_enabled_state()
        self._debounce.start()      # 防抖：停顿后统一应用（flush 可立即落盘）

    def _apply_preset(self, name: str) -> None:
        if self._loading or name == "自定义":
            return
        factory = PRESETS.get(name)
        old_seed = self._params.seed
        if factory is None:
            self._params = PerturbParams(seed=old_seed)
            self._params.enabled = False
        else:
            self._params = factory(self._size_hint)
            self._params.seed = old_seed   # 保留当前种子，避免预设切换后效果跳变
        self._load(self._params)
        self.paramsChanged.emit()

    def _on_reseed(self) -> None:
        self._params = self._collect()
        self._params.seed = (self._params.seed * 1103515245 + 12345) % (2 ** 31)
        self._load(self._params)
        self._update_seed_label()
        self.reseedRequested.emit()
        self.paramsChanged.emit()

    def _update_enabled_state(self) -> None:
        active = self.enable_check.isChecked()
        for field, sp in self._spin.items():
            # 笔锋是独立修饰，不依赖「启用手写扰动」总开关
            sp.setEnabled(active or field == "flare_mm")
        self.intensity_slider.setEnabled(active)
        self.intensity_spin.setEnabled(active)

    def _update_seed_label(self) -> None:
        self.seed_label.setText(f"随机种子：{self._params.seed}")

    # --------------------------------------------------------------- 接口
    def params(self) -> PerturbParams:
        return self._collect()

    def set_size_hint(self, size_mm: float) -> None:
        self._size_hint = size_mm

    def set_params(self, p: PerturbParams) -> None:
        self._params = p.clone()
        self._load(self._params)


class PerturbDialog(QDialog):
    """独立的扰动参数对话框（用于矢量/静态对象，非文本）。"""

    def __init__(self, params: PerturbParams, title: str = "手写扰动",
                 size_hint: float = 10.0, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(430, 620)
        root = QVBoxLayout(self)
        self.panel = PerturbPanel(params=params, size_hint=size_hint)
        root.addWidget(self.panel, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def result_params(self) -> PerturbParams:
        return self.panel.params()
