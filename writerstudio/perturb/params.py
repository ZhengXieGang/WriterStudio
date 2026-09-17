"""手写扰动参数模型。

参数单位约定：
    * 平移类（``*_sigma`` 中带 mm 的、正弦振幅/波长）单位 **毫米(mm)**。
    * 比例类（``size_sigma``、``line_spacing_sigma``）为**无量纲比例**（0.03 = 3%）。
    * 角度类（``char_rot_sigma``、``stroke_theta_sigma``）单位 **度**。

扰动模型分三级（与 Handright 的参数体系对应，但作用在笔画折线上）：

    字符级：字号缩放、水平/垂直位移、整体旋转
    行级  ：行基线抖动、每行首字位置偏移、行尾落差、基线起伏（随机缓弧+游走）
    字距  ：沿行平滑松紧 + AR(1) 逐字节奏、词间距
    笔画级：单笔画绕自身中心的旋转与位移（模拟运笔抖动）

所有参数均以 **σ（标准差）** 表达，实际扰动值取自正态分布 ``N(0, σ)``。
固定 ``seed`` 时结果完全可复现，更换 seed 即可「一键重摇」。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class PerturbParams:
    """手写扰动参数集合（全部有默认值，便于序列化与部分覆盖）。"""

    enabled: bool = False
    seed: int = 20240910

    # -- 字符级 -------------------------------------------------------------
    size_sigma: float = 0.0        # 字号比例抖动 σ（0.03 = ±3%）
    char_x_sigma: float = 0.0      # 字符水平位移 σ (mm)
    char_y_sigma: float = 0.0      # 字符垂直位移 σ (mm)
    char_rot_sigma: float = 0.0    # 字符整体旋转 σ (°)
    char_shear_sigma: float = 0.0  # 字符体态斜切 σ (°)：x 随 y 线性偏移，
                                   # 让同一字的「体态/肩线」各不相同

    # -- 行级 ---------------------------------------------------------------
    line_y_sigma: float = 0.0            # 行基线垂直抖动 σ (mm)
    line_start_sigma: float = 0.0        # 每行首字水平偏移 σ (mm)
    line_tilt_sigma: float = 0.0         # 行尾落差 σ (mm)：行尾相对行首的
                                         # 高度漂移——人写的每行都不水平
    char_spacing_sigma: float = 0.0      # 字距抖动 σ (mm)：沿行平滑松紧 +
                                         # AR(1) 逐字节奏（均值回归，不漂移）
    word_gap_em: float = 0.0             # 词间距（×字号 em）：词/标点之后
                                         # 额外留空、词内节奏聚拢；0 = 关闭

    # -- 基线起伏（取代旧版固定周期的正弦波） ---------------------------------
    # 行基线 = 行尺度的随机缓弧（起讫高度独立，天然带倾斜）+ 中尺度游走。
    # 正弦波有固定周期/固定振幅/必然回到起点，一眼就是机器画的。
    sine_amplitude: float = 0.0          # 基线游走振幅 (mm)
    sine_wavelength: float = 80.0        # 中尺度起伏波长 (mm)，超过行宽时
                                         # 退化为整行一道缓弧
    sine_random_phase: bool = True       # 旧版正弦遗留：仅保留序列化兼容，
    sine_phase_deg: float = 0.0          # 新基线模型不使用

    # -- 笔画级 -------------------------------------------------------------
    stroke_x_sigma: float = 0.0          # 单笔画水平位移 σ (mm)
    stroke_y_sigma: float = 0.0          # 单笔画垂直位移 σ (mm)
    stroke_theta_sigma: float = 0.0      # 单笔画旋转 σ (°)
    stroke_stretch_sigma: float = 0.0    # 单笔画沿自身轴向伸缩 σ（0.08 = ±8%）
    stroke_trim_mm: float = 0.0          # 笔画末端随机修剪上限 (mm)，
                                         # 两端各随机剪掉 0..该值，长短参差

    # -- 线条起伏（矢量/手绘线条的「手抖」效果） -----------------------------
    line_wobble: float = 0.0             # 沿线条法向的平滑起伏振幅 (mm)，0=不抖动
    line_wobble_wavelength: float = 20.0  # 起伏主波长 (mm)
    line_tremor: float = 0.0             # 细微高频颤抖振幅 (mm)，模拟手部微抖

    # -- 笔锋（确定性修饰，非随机扰动） ---------------------------------------
    # 模拟毛笔字「出锋/尖入笔」：收笔沿出笔方向甩出渐细的尖，起笔斜切入笔。
    # 固定宽度笔无法真正变粗变细，靠端部形状让笔画看起来有锋。
    flare_mm: float = 0.0                # 笔锋长度 (mm)，0=不加

    # -- 输出质量 -----------------------------------------------------------
    simplify_mm: float = 0.0             # 扰动输出抽稀容差 (mm)，0=不抽稀。
                                         # Douglas–Peucker 垂直距离容差，与
                                         # 高速书写发抖时
                                         # 可试 0.1~0.3，过大会明显失真

    # -- 全局 ---------------------------------------------------------------
    intensity: float = 1.0               # 总强度系数：统一放大/缩小上面所有量
    smoothing: float = 0.0               # 0..1，对扰动后的笔画做轻微平滑

    def is_active(self) -> bool:
        """是否需要进入扰动管线。

        笔锋（``flare_mm``）是确定性修饰，**不依赖「启用手写扰动」**——
        只设了笔锋也应生效；其余随机扰动仍由 ``enabled`` 总开关控制。
        """
        if self.flare_mm > 1e-12:
            return True
        if not self.enabled:
            return False
        for name in self._numeric_fields():
            if abs(getattr(self, name)) > 1e-12:
                return True
        return False

    @staticmethod
    def _numeric_fields() -> list[str]:
        return [
            "size_sigma", "char_x_sigma", "char_y_sigma", "char_rot_sigma",
            "char_shear_sigma",
            "line_y_sigma", "line_start_sigma", "line_tilt_sigma",
            "char_spacing_sigma",
            "word_gap_em",
            "sine_amplitude",
            "stroke_x_sigma", "stroke_y_sigma", "stroke_theta_sigma",
            "stroke_stretch_sigma", "stroke_trim_mm",
            "line_wobble", "line_tremor",
            "simplify_mm",
            "smoothing", "flare_mm",
        ]

    @property
    def intensity_factor(self) -> float:
        """有效的强度系数（钳制到合理范围，避免 0 或负数让效果消失/翻转）。"""
        try:
            v = float(self.intensity)
        except (TypeError, ValueError):
            return 1.0
        return max(0.0, min(10.0, v))


    def clone(self) -> PerturbParams:
        return PerturbParams(**asdict(self))

    # -- 序列化 -------------------------------------------------------------
    def to_data(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_data(cls, data: dict[str, Any] | None) -> PerturbParams:
        if not data:
            return cls()
        allowed = set(cls().__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in allowed}
        return cls(**kwargs)

    # -- 预设 ---------------------------------------------------------------
    @classmethod
    def natural(cls, size_mm: float = 10.0, seed: int | None = None) -> PerturbParams:
        """「自然手写」预设：各 σ 按字号比例设定，效果接近真人执笔。"""
        p = cls()
        p.enabled = True
        if seed is not None:
            p.seed = seed
        p.size_sigma = 0.035
        p.char_x_sigma = size_mm * 0.05
        p.char_y_sigma = size_mm * 0.04
        p.char_rot_sigma = 0.8
        p.line_y_sigma = size_mm * 0.12
        p.line_start_sigma = size_mm * 0.15
        p.line_tilt_sigma = size_mm * 0.18
        p.char_spacing_sigma = size_mm * 0.025
        p.word_gap_em = 0.10
        p.sine_amplitude = size_mm * 0.08
        p.sine_wavelength = size_mm * 8.0
        p.sine_random_phase = True
        p.stroke_x_sigma = size_mm * 0.006
        p.stroke_y_sigma = size_mm * 0.006
        p.stroke_theta_sigma = 4.0     # ≈ Handright 的 0.07 rad
        p.char_shear_sigma = 2.0       # 体态各异的「歪斜感」
        p.stroke_stretch_sigma = 0.05
        p.stroke_trim_mm = size_mm * 0.03
        p.line_wobble = size_mm * 0.02
        p.line_wobble_wavelength = size_mm * 6.0
        p.line_tremor = size_mm * 0.006
        p.smoothing = 0.15
        return p

    @classmethod
    def subtle(cls, size_mm: float = 10.0) -> PerturbParams:
        """轻微扰动：几乎端正，仅消除「机器感」。"""
        p = cls()
        p.enabled = True
        p.size_sigma = 0.012
        p.char_x_sigma = size_mm * 0.015
        p.char_y_sigma = size_mm * 0.012
        p.char_rot_sigma = 0.25
        p.line_y_sigma = size_mm * 0.02
        p.line_start_sigma = size_mm * 0.04
        p.line_tilt_sigma = size_mm * 0.04
        p.char_spacing_sigma = size_mm * 0.008
        p.word_gap_em = 0.06
        p.sine_amplitude = size_mm * 0.02
        p.sine_wavelength = size_mm * 12.0
        p.stroke_theta_sigma = 1.2
        p.char_shear_sigma = 0.8
        p.stroke_stretch_sigma = 0.02
        p.stroke_trim_mm = size_mm * 0.012
        p.line_wobble = size_mm * 0.008
        p.line_wobble_wavelength = size_mm * 8.0
        p.line_tremor = size_mm * 0.003
        p.smoothing = 0.1
        return p

    @classmethod
    def strong(cls, size_mm: float = 10.0) -> PerturbParams:
        """强烈扰动：潦草、随性。"""
        p = cls()
        p.enabled = True
        p.size_sigma = 0.07
        p.char_x_sigma = size_mm * 0.10
        p.char_y_sigma = size_mm * 0.09
        p.char_rot_sigma = 2.2
        p.line_y_sigma = size_mm * 0.25
        p.line_start_sigma = size_mm * 0.30
        p.line_tilt_sigma = size_mm * 0.35
        p.char_spacing_sigma = size_mm * 0.06
        p.word_gap_em = 0.14
        p.sine_amplitude = size_mm * 0.18
        p.sine_wavelength = size_mm * 5.0
        p.stroke_x_sigma = size_mm * 0.012
        p.stroke_y_sigma = size_mm * 0.012
        p.stroke_theta_sigma = 7.0
        p.char_shear_sigma = 4.5
        p.stroke_stretch_sigma = 0.11
        p.stroke_trim_mm = size_mm * 0.06
        p.line_wobble = size_mm * 0.04
        p.line_wobble_wavelength = size_mm * 4.0
        p.line_tremor = size_mm * 0.012
        p.smoothing = 0.2
        return p

    @classmethod
    def vector_hand_drawn(cls, reference_mm: float = 50.0) -> PerturbParams:
        """「手绘线条」预设：以线条自身起伏为主，适合矢量图/边框/签名。

        ``reference_mm`` 取内容的特征尺寸（如包围盒较短边），用于把振幅换算成
        与图形大小相称的毫米值——小图形抖动小、大图形抖动大，视觉效果一致。
        """
        p = cls()
        p.enabled = True
        ref = max(1.0, reference_mm)
        p.line_wobble = ref * 0.012          # 约为特征尺寸的 1.2%
        p.line_wobble_wavelength = ref * 0.35
        p.line_tremor = ref * 0.004
        p.stroke_x_sigma = ref * 0.002
        p.stroke_y_sigma = ref * 0.002
        p.stroke_theta_sigma = 0.8
        p.smoothing = 0.1
        return p

    def rescaled_for(self, size_mm: float, from_size: float = 10.0) -> PerturbParams:
        """按字号比例缩放平移类参数（切换字号时保持视觉扰动一致）。"""
        ratio = size_mm / from_size if from_size > 0 else 1.0
        p = self.clone()
        for name in ("char_x_sigma", "char_y_sigma", "line_y_sigma",
                     "line_start_sigma", "line_tilt_sigma",
                     "char_spacing_sigma",
                     "sine_amplitude", "sine_wavelength",
                     "stroke_x_sigma", "stroke_y_sigma",
                     "stroke_trim_mm",
                     "line_wobble", "line_wobble_wavelength", "line_tremor"):
            setattr(p, name, getattr(self, name) * ratio)
        return p


PRESETS: dict[str, Any] = {
    "关闭": None,
    "轻微": PerturbParams.subtle,
    "自然": PerturbParams.natural,
    "强烈": PerturbParams.strong,
    "手绘线条": PerturbParams.vector_hand_drawn,
}
