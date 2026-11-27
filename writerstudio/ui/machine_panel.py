"""机器控制面板：连接、Jog、状态、写字起点、G-code 生成与发送。

面板通过信号与主窗口交互（生成/导出由主窗口提供文档访问）。
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


from ..i18n import tr, trf
from ..machine.config import (
    PEN_LASER,
    PEN_M3M5,
    PEN_SERVO_ANGLE,
    PEN_Z,
    PEN_MODES,
    GCodeConfig,
)
from ..machine.gcode_gen import format_duration
from ..machine.serial_link import GrblLink, LinkEvent, State, list_ports
from ..machine.start_point import MODE_CANVAS, MODE_REGISTERED, StartPoint
from .icons import action_icon
from .style import FIELD_HEIGHT, MarqueeLabel, set_role, spin, unify_inputs

_BAUD_RATES = [115200, 250000, 57600, 38400, 19200, 9600]


class MachinePanel(QWidget):
    """GRBL 机器控制面板。"""

    generateRequested = Signal()      # 请求生成 G-code 预览
    sendRequested = Signal()          # 请求发送当前文档
    exportRequested = Signal()        # 请求导出 G-code 文件
    statusUpdated = Signal(object)    # 机器状态更新（MachineStatus）
    profileDetected = Signal(object)  # 连接探测到固件能力档案（MachineProfile）
    # -- 写字起点（起点 = 画布标记，三者共享同一位置）--
    readPenAsStartRequested = Signal()     # 读取当前笔位并作为起点
    registerPenRequested = Signal()        # 以当前笔位校准标记（标记不动）
    markStartPickRequested = Signal(bool)  # 手动标记起点（画布拾取开始/取消）
    removeStartMarkerRequested = Signal()  # 移除已设置的手动起点标记
    movePenRequested = Signal()            # 把笔头移动到标记起点
    startPointChanged = Signal()           # 写字起点（标记/校准）发生变化
    axisOptionsChanged = Signal()          # 坐标轴对调/反转发生变化
    # 串口读线程 → GUI 线程的事件桥：link 回调发生在后台线程，
    # Qt 控件只能在本信号（自动排队连接）的槽里安全操作
    linkEventReceived = Signal(object)

    #: 单次「落笔测试」默认下压保持时长（毫秒），到期后自动抬回
    PEN_TEST_HOLD_MS = 1500

    def __init__(self, link: Optional[GrblLink] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.linkEventReceived.connect(self._on_event)
        self.link = link or GrblLink(on_event=self.linkEventReceived.emit)
        self.config = GCodeConfig()
        self.start_point = StartPoint()
        self._machine_pos: Optional[tuple[float, float, float]] = None
        self._job_started: Optional[float] = None
        self._pen_test_cfg: Optional[GCodeConfig] = None
        self._pen_test_timer = QTimer(self)
        self._pen_test_timer.setSingleShot(True)
        self._pen_test_timer.timeout.connect(self._on_pen_test_timeout)
        self._build_ui()
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._tick_progress)
        self._ui_timer.start(500)
        self._refresh_ports()
        self._on_state(State.DISCONNECTED)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # 三段式布局：
        # 1) 顶部固定「控制 / 参数」分段切换，把高频操作与低频硬件配置分离；
        # 2) 中间唯一滚动区，按激活页自适应高度（避免 1600px+ 长卷）；
        # 3) 底部常驻作业栏（预览/导出/发送/暂停/中止），随时可一键紧急中止。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # ---- 顶部分段切换 ----
        seg_wrap = QWidget()
        seg_row = QHBoxLayout(seg_wrap)
        seg_row.setContentsMargins(4, 4, 4, 2)
        seg_row.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_control_btn = QPushButton(tr("控制"))
        self._tab_config_btn = QPushButton(tr("参数"))
        for idx, b in enumerate((self._tab_control_btn, self._tab_config_btn)):
            b.setCheckable(True)
            b.setProperty("wsSegment", True)
            self._tab_group.addButton(b, idx)
            seg_row.addWidget(b, 1)
        self._tab_control_btn.setChecked(True)
        self._tab_group.idClicked.connect(self._switch_tab)
        outer.addWidget(seg_wrap)

        # ---- 中间单滚动区 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(4, 2, 4, 4)
        body_lay.setSpacing(6)

        self._page_control = QWidget()
        ctrl_root = QVBoxLayout(self._page_control)
        ctrl_root.setContentsMargins(0, 0, 0, 0)
        ctrl_root.setSpacing(6)

        self._page_config = QWidget()
        cfg_root = QVBoxLayout(self._page_config)
        cfg_root.setContentsMargins(0, 0, 0, 0)
        cfg_root.setSpacing(6)

        body_lay.addWidget(self._page_control)
        body_lay.addWidget(self._page_config)
        body_lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # ======================== 控制页（高频操作） ========================
        # ---- 连接 ----
        conn = QGroupBox(tr("连接"))
        cf = QFormLayout(conn)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(80)
        self.port_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(6)
        refresh = QPushButton()
        refresh.setToolTip(tr("刷新串口"))
        refresh.clicked.connect(self._refresh_ports)
        self._refresh_btn = refresh
        self._style_refresh_button()
        row.addWidget(self.port_combo, 1)
        row.addWidget(refresh, 0)
        cf.addRow(tr("串口"), row)

        self.baud_combo = QComboBox()
        for b in _BAUD_RATES:
            self.baud_combo.addItem(str(b))
        self.baud_combo.setCurrentText("115200")
        cf.addRow(tr("波特率"), self.baud_combo)

        # DTR/RTS：ESP32 板载串口芯片会把它们接到复位/Boot 脚，
        # 打开串口即复位固件；连不上时试着取消勾选。
        lines_row = QHBoxLayout()
        self.dtr_check = QCheckBox("DTR")
        self.dtr_check.setChecked(True)
        self.rts_check = QCheckBox("RTS")
        self.rts_check.setChecked(True)
        self.dtr_check.setToolTip(tr("连接后保持 DTR 电平；ESP32 板连上即复位时取消勾选"))
        self.rts_check.setToolTip(tr("连接后保持 RTS 电平；ESP32 板连上即复位时取消勾选"))
        lines_row.addWidget(self.dtr_check)
        lines_row.addWidget(self.rts_check)
        lines_row.addStretch(1)
        cf.addRow(tr("串口电平"), lines_row)

        # 连接为主按钮独占一行，回零/解锁两列并排：避免三按钮在英文宽字体下
        # 撑出横向滚动条，同时主操作更醒目。
        self.connect_btn = QPushButton(tr("连接"))
        self.connect_btn.clicked.connect(self._toggle_connect)
        cf.addRow(self.connect_btn)
        btns = QHBoxLayout()
        self.home_btn = QPushButton(tr("回零"))
        self.home_btn.clicked.connect(lambda: self._guarded(self.link.home))
        self.unlock_btn = QPushButton(tr("解锁"))
        self.unlock_btn.clicked.connect(lambda: self._guarded(self.link.unlock))
        btns.addWidget(self.home_btn)
        btns.addWidget(self.unlock_btn)
        cf.addRow(btns)
        ctrl_root.addWidget(conn)

        # ---- 状态 ----
        st = QGroupBox(tr("状态"))
        sf = QFormLayout(st)
        # 状态卡片全部使用单行跑马灯标签：平时单行省略不换行、不撑宽面板；
        # 鼠标移到该项（左侧标题或右侧数值）时自动横向平滑滚动展示全文。
        self.state_label = MarqueeLabel(tr("未连接"))
        self.pos_label = MarqueeLabel(tr("—"))
        self.feed_label = MarqueeLabel(tr("—"))
        self.fw_label = MarqueeLabel(tr("—"))
        self.fw_label.setToolTip(
            tr("连接后自动读取固件设置（$$）得到的参数快照：\n"
            "加速度/速率上限用于书写耗时估算；只读取，不修改。"))
        for title, field in (
            (tr("状态"), self.state_label),
            (tr("机械坐标"), self.pos_label),
            (tr("进给/转速"), self.feed_label),
            (tr("固件参数"), self.fw_label),
        ):
            lbl = QLabel(title)
            field.attach_buddy(lbl)
            sf.addRow(lbl, field)
        ctrl_root.addWidget(st)

        # ---- Jog ----
        jog = QGroupBox(tr("手动控制 (Jog)"))
        jogv = QVBoxLayout(jog)
        # 步长/速度各占一行（竖排），避免和方向键挤在一行而撑宽整个面板
        jf = QFormLayout()
        self.jog_step = QDoubleSpinBox()
        self.jog_step.setRange(0.1, 100.0)
        self.jog_step.setValue(10.0)
        self.jog_step.setSuffix(" mm")
        self.jog_feed = QSpinBox()
        self.jog_feed.setRange(10, 20000)
        self.jog_feed.setValue(2500)        # 默认 2500 mm/min
        self.jog_feed.setSuffix(" mm/min")
        self.jog_feed.setToolTip(tr("点动速度（mm/min）"))
        jf.addRow(tr("步长"), self.jog_step)
        jf.addRow(tr("速度"), self.jog_feed)
        jogv.addLayout(jf)

        pad = QGridLayout()
        pad.setContentsMargins(0, 0, 0, 0)
        self._jog_buttons: list[QPushButton] = []

        def mk(label, dx, dy):
            b = QPushButton(label)
            b.clicked.connect(lambda: self._jog(dx, dy))
            self._jog_buttons.append(b)
            return b

        pad.addWidget(mk("Y+", 0, 1), 0, 1)
        pad.addWidget(mk("X−", -1, 0), 1, 0)
        pad.addWidget(mk("X+", 1, 0), 1, 2)
        pad.addWidget(mk("Y−", 0, -1), 2, 1)
        for c in range(3):
            pad.setColumnStretch(c, 1)
        jogv.addLayout(pad)
        # 单次抬/落笔测试按钮：归入手动控制区，标定落笔深度时直接验证，不必发整页作业
        pen_test_row = QHBoxLayout()
        self.pen_test_btn = QPushButton(tr("落笔测试"))
        self.pen_test_btn.setToolTip(
            tr("按当前参数落笔并停留约 1.5 秒后自动抬回（用于标定落笔深度；\n"
            "激光模式仅以预览功率短时出光）。"))
        self.pen_test_btn.clicked.connect(lambda: self.pen_test())
        self.pen_up_btn = QPushButton(tr("抬笔"))
        self.pen_up_btn.setToolTip(
            tr("按当前抬笔参数立即抬起笔头（并取消未完成的落笔测试）。"))
        self.pen_up_btn.clicked.connect(lambda: self.pen_up_now())
        pen_test_row.addWidget(self.pen_test_btn)
        pen_test_row.addWidget(self.pen_up_btn)
        jogv.addLayout(pen_test_row)
        ctrl_root.addWidget(jog)

        # ---- 写字起点 ----
        sp = QGroupBox(tr("写字起点"))
        spv = QVBoxLayout(sp)

        # 置顶单行状态摘要：一眼看清「未设置 / 已标记 / 已校准」与 X/Y 坐标
        self.start_summary = MarqueeLabel(tr("未设置"))
        spv.addWidget(self.start_summary)

        self.read_pos_start_btn = QPushButton(tr("读取当前笔位作为起点"))
        self.read_pos_start_btn.setMinimumHeight(30)
        self.read_pos_start_btn.setToolTip(
            tr("读取机器笔头当前坐标作为写字起点（需已连接）。\n"
            "会把红色标记移到笔位，并将「内容包围盒左下角」直接对齐到此处；\n"
            "若要把页面上特定参考点（如表格框角）对准笔位，请改用下方「以当前笔位校准标记」。"))
        self.read_pos_start_btn.clicked.connect(self.readPenAsStartRequested.emit)
        spv.addWidget(self.read_pos_start_btn)

        self.register_pen_btn = QPushButton(tr("以当前笔位校准标记"))
        self.register_pen_btn.setMinimumHeight(30)
        self.register_pen_btn.setToolTip(
            tr("标记不动，用笔来标定：先用「手动标记起点」把红色标记放到画布上\n"
            "任意参考点（如表格框角），再把笔移到纸上同一点并点此按钮。\n"
            "整页内容将相对该参考点原样落位，适合套打填表或机器原点与纸角不对齐时。"))
        self.register_pen_btn.clicked.connect(self.registerPenRequested.emit)
        spv.addWidget(self.register_pen_btn)

        self.mark_start_btn = QPushButton(tr("手动标记起点"))
        self.mark_start_btn.setMinimumHeight(30)
        self.mark_start_btn.setToolTip(
            tr("在画布上单击一点作为写字起点；之后也可直接拖动画布上的标记微调。\n"
            "已设置标记后，此按钮变为「移除手动标记点」。"))
        self.mark_start_btn.setCheckable(True)
        self.mark_start_btn.toggled.connect(self._on_mark_start_toggled)
        self.mark_start_btn.clicked.connect(self._on_mark_start_clicked)
        spv.addWidget(self.mark_start_btn)

        self.move_pen_btn = QPushButton(tr("移动到标记起点"))
        self.move_pen_btn.setToolTip(tr("抬笔后把笔头移动到标记的起点位置"))
        self.move_pen_btn.clicked.connect(self.movePenRequested.emit)
        spv.addWidget(self.move_pen_btn)
        ctrl_root.addWidget(sp)

        # ======================== 参数页（低频配置） ========================
        # ---- 抬落笔设置 ----
        pen = QGroupBox(tr("抬落笔"))
        pf = QFormLayout(pen)
        self._pen_form = pf
        self.pen_mode = QComboBox()
        self.pen_mode.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pen_mode.setMinimumContentsLength(6)
        for key, label in PEN_MODES.items():
            self.pen_mode.addItem(tr(label), key)
        self.pen_mode.currentIndexChanged.connect(self._on_pen_mode)
        pf.addRow(tr("方式"), self.pen_mode)
        # 步进 Z 轴（抬/落笔各走一段受控 Z 进给）。默认按弹簧回位笔架标定：
        # 落笔 Z 6mm、抬笔 Z 0mm（弹簧回位笔架释放位）；支持 0.1mm 精细标定。
        self.pen_down_z = spin(-10.0, 50.0, 6.0, " mm", 0.1)
        self.pen_down_z.setToolTip(tr("落笔 Z 位置：Z 轴笔控写到该高度即下压触纸"))
        self.pen_up_z = spin(-10.0, 50.0, 0.0, " mm", 0.1)
        self.pen_up_z.setToolTip(tr("抬笔 Z 位置：弹簧回位笔架填 0（释放位）"))
        self.pen_z_feed = spin(1.0, 20000.0, 12000.0, " mm/min", 100.0, 0)
        self.pen_z_feed.setToolTip(
            tr("Z 抬落笔速度：抬笔与落笔均以此进给升降。\n"
            "每笔两趟 Z 全行程是手写作业的最大固定开销——在固件 Z 速率上限"
            "（连接后自动探测显示在「固件参数」行）内尽量调高。"))
        pf.addRow(tr("落笔 Z"), self.pen_down_z)
        pf.addRow(tr("抬笔 Z"), self.pen_up_z)
        pf.addRow(tr("Z 速度"), self.pen_z_feed)
        # 收笔斜抬/入笔斜落：笔画两端边走边抬/落（手写的压力渐释动作），
        # 消除末端满压停留的墨点与起点重压墨坨。仅 Z 轴笔控生效。
        self.stroke_taper = spin(0.0, 5.0, 1.5, " mm", 0.25, 2)
        self.stroke_taper.setToolTip(
            tr("收笔斜抬：笔画最后这段长度内边走边抬笔（0=关闭）。\n"
            "消除「笔在笔画末端满压停留」留下的墨点，收笔更自然。\n"
            "建议 1~2mm；仅 Z 轴笔控生效，斜率受「Z 速度/书写速度」约束。"))
        self.entry_taper = spin(0.0, 5.0, 0.5, " mm", 0.25, 2)
        self.entry_taper.setToolTip(
            tr("入笔斜落：笔画开头这段长度内边走边落笔（0=关闭）。\n"
            "消除起笔重压墨坨，起笔更自然；建议 0.5~1mm。仅 Z 轴笔控生效。"))
        pf.addRow(tr("收笔斜抬"), self.stroke_taper)
        pf.addRow(tr("入笔斜落"), self.entry_taper)
        # 舵机角度（抬/落笔均发 M3 S<角度>）
        self.pen_s = QSpinBox()
        self.pen_s.setRange(0, 30000)
        self.pen_s.setValue(0)
        self.pen_s.setToolTip(tr("落笔角度 / M3 模式落笔 S 值"))
        pf.addRow(tr("落笔角度/S"), self.pen_s)
        self.pen_up_s = QSpinBox()
        self.pen_up_s.setRange(0, 30000)
        self.pen_up_s.setValue(50)
        self.pen_up_s.setToolTip(tr("抬笔角度"))
        pf.addRow(tr("抬笔角度"), self.pen_up_s)
        # 激光（M3 S 出光 / M5 关光）
        self.laser_power = QSpinBox()
        self.laser_power.setRange(0, 30000)
        self.laser_power.setValue(1000)
        self.laser_power.setToolTip(tr("出光功率（对应 $30 最大功率的 S 值）"))
        pf.addRow(tr("激光功率"), self.laser_power)
        self.laser_preview = QSpinBox()
        self.laser_preview.setRange(0, 30000)
        self.laser_preview.setValue(50)
        self.laser_preview.setToolTip(tr("预览/对位低功率"))
        pf.addRow(tr("激光预览功率"), self.laser_preview)
        self.pen_on_delay = spin(0.0, 5.0, 0.0, " s", 0.05)
        self.pen_off_delay = spin(0.0, 5.0, 0.0, " s", 0.05)
        self.pen_on_delay.setToolTip(tr("落笔后等待（G4），让笔完全落下再写字"))
        self.pen_off_delay.setToolTip(tr("抬笔前等待（G4），防止拖墨"))
        pf.addRow(tr("落笔后等待"), self.pen_on_delay)
        pf.addRow(tr("抬笔前等待"), self.pen_off_delay)
        self.draw_feed = spin(1.0, 20000.0, 4400.0, " mm/min", 50.0, 0)
        self.travel_feed = spin(1.0, 30000.0, 9000.0, " mm/min", 100.0, 0)
        self.draw_feed.setToolTip(
            tr("书写速度：短笔画受加速度限制，峰速有物理上限（连接后按固件\n"
            "加速度自动估算并在预览中提示）；设得再高只会平白增加每笔加减速。"))
        pf.addRow(tr("书写速度"), self.draw_feed)
        pf.addRow(tr("空程速度"), self.travel_feed)
        self.custom_start_edit = QLineEdit()
        self.custom_start_edit.setPlaceholderText(tr("如：M3 S0;G4 P0.5（分号分隔多条）"))
        self.custom_start_edit.setToolTip(
            tr("作业开始前发送的自定义指令。\n"
            "注意：不要包含 G92/G54 等坐标偏移指令——本软件按绝对机械坐标\n"
            "书写，偏移会让整个作业整体错位（发送前的自动清理也会被它作废）。"))
        self.custom_end_edit = QLineEdit()
        self.custom_end_edit.setPlaceholderText(tr("如：M5"))
        pf.addRow(tr("开始前代码"), self.custom_start_edit)
        pf.addRow(tr("结束后代码"), self.custom_end_edit)
        cfg_root.addWidget(pen)

        # ---- 坐标轴（对调/反转：适配机器原点朝向与导轨方向不同的机型）----
        ax = QGroupBox(tr("坐标轴"))
        axv = QVBoxLayout(ax)
        axv.setContentsMargins(6, 6, 6, 6)
        self.swap_xy_check = QCheckBox(tr("对调 X/Y 轴"))
        self.swap_xy_check.setToolTip(
            tr("交换 X 与 Y 轴：适合机器横放在纸左侧、笔沿纸的短边移动的装机方式。\n"
            "机械原点仍在纸张左上角，只是 X 轴改为沿纸边向下、Y 轴沿顶边向右。"))
        self.swap_xy_check.toggled.connect(self._on_axis_changed)
        axv.addWidget(self.swap_xy_check)
        self.invert_x_check = QCheckBox(tr("反转 X 轴"))
        self.invert_x_check.setToolTip(tr("X 轴方向取反：原点在纸的右上角一侧的机型用"))
        self.invert_x_check.toggled.connect(self._on_axis_changed)
        self.invert_y_check = QCheckBox(tr("反转 Y 轴"))
        self.invert_y_check.setToolTip(tr("Y 轴方向取反：原点在纸的左下角一侧的机型用"))
        self.invert_y_check.toggled.connect(self._on_axis_changed)
        axv.addWidget(self.invert_x_check)
        axv.addWidget(self.invert_y_check)
        cfg_root.addWidget(ax)

        # ---- 书写选项 ----
        job_opts = QGroupBox(tr("书写选项"))
        jof = QVBoxLayout(job_opts)
        # 勾选项逐行竖排：避免不同平台字体密度下并排把窄停靠区撑出横向滚动条
        self.single_step_check = QCheckBox(tr("单步发送"))
        self.single_step_check.setToolTip(
            tr("一次只发一条命令，等机器回 ok 再发下一条。\n"
            "速度慢但最稳，适合调试机器或长内容。"))
        self.single_step_check.toggled.connect(self._on_single_step)
        self.return_start_check = QCheckBox(tr("完成后回起点"))
        self.return_start_check.setChecked(True)
        self.return_start_check.setToolTip(tr("写完后笔头回到 X0 Y0（写字起点）"))
        self.sleep_check = QCheckBox(tr("完成后休眠"))
        self.sleep_check.setToolTip(tr("写完让机器休眠（$SLP，需固件支持）"))
        jof.addWidget(self.single_step_check)
        jof.addWidget(self.return_start_check)
        jof.addWidget(self.sleep_check)
        job_form = QFormLayout()
        self.order_combo = QComboBox()
        self.order_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.order_combo.setMinimumContentsLength(6)
        self.order_combo.addItem(tr("阅读顺序（逐字/从上到下）"), "reading")
        self.order_combo.addItem(tr("最短空程（图形/签名）"), "shortest")
        self.order_combo.setToolTip(
            tr("阅读顺序：写完一个字/一组再写下一个，文字从左到右、从上到下，\n"
            "读起来自然（常规书写顺序）。\n"
            "最短空程：贪心最近邻，抬笔移动最少，但会打乱顺序，适合矢量图/签名。"))
        job_form.addRow(tr("书写顺序"), self.order_combo)
        self.simplify_spin = QDoubleSpinBox()
        self.simplify_spin.setRange(0.0, 1.0)
        self.simplify_spin.setSingleStep(0.05)
        self.simplify_spin.setValue(0.0)
        self.simplify_spin.setSuffix(" mm")
        self.simplify_spin.setToolTip(
            tr("按此容差合并笔画上的密集采样点（0=关闭）。\n"
            "书写速度快时线条发抖可试 0.1~0.3；0.5 以上会明显失真。"))
        job_form.addRow(tr("笔画抽稀"), self.simplify_spin)
        jof.addLayout(job_form)
        cfg_root.addWidget(job_opts)

        # ---- 笔寿命 ----
        life_box = QGroupBox(tr("笔寿命"))
        lf = QFormLayout(life_box)
        self.penlife_label = QLabel()
        self.penlife_label.setWordWrap(True)
        self.penlife_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.pen_limit = spin(0.0, 10000.0, 0.0, " m", 5.0, 1)
        self.pen_limit.setToolTip(tr("累计书写达到该长度时提醒更换/削笔；0 = 不提醒"))
        self.pen_limit.valueChanged.connect(self._save_penlife)
        self.pen_reset_btn = QPushButton(tr("清零"))
        self.pen_reset_btn.clicked.connect(self._reset_penlife)
        lf.addRow(self.penlife_label)
        lf.addRow(tr("提醒阈值"), self.pen_limit)
        lf.addRow(self.pen_reset_btn)
        cfg_root.addWidget(life_box)

        # ======================== 底部常驻作业栏 ========================
        footer_sep = QFrame()
        footer_sep.setFrameShape(QFrame.Shape.HLine)
        footer_sep.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(footer_sep)

        footer = QWidget()
        fl = QVBoxLayout(footer)
        fl.setContentsMargins(6, 4, 6, 6)
        fl.setSpacing(5)
        self.progress_label = MarqueeLabel(tr("—"))
        set_role(self.progress_label, "muted")
        fl.addWidget(self.progress_label)

        gen_row = QHBoxLayout()
        gen_row.setSpacing(4)
        self._gen_btn = QPushButton(tr("生成预览"))
        self._gen_btn.clicked.connect(self.generateRequested.emit)
        self._exp_btn = QPushButton(tr("导出 G-code…"))
        self._exp_btn.clicked.connect(self.exportRequested.emit)
        gen_row.addWidget(self._gen_btn)
        gen_row.addWidget(self._exp_btn)
        fl.addLayout(gen_row)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(4)
        self.send_btn = QPushButton(tr("发送"))
        self.send_btn.clicked.connect(self.sendRequested.emit)
        self.pause_btn = QPushButton(tr("暂停"))
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.abort_btn = QPushButton(tr("中止"))
        self.abort_btn.clicked.connect(self._abort)
        set_role(self.abort_btn, "danger")
        ctrl.addWidget(self.send_btn)
        ctrl.addWidget(self.pause_btn)
        ctrl.addWidget(self.abort_btn)
        fl.addLayout(ctrl)
        outer.addWidget(footer)

        # 默认显示控制页，隐藏参数页
        self._switch_tab(0)

        self._load_penlife()
        self._on_pen_mode()
        self._update_marker_button()
        self._update_start_summary()
        self._normalize_field_sizes()
        # 窄停靠区（英文文案 / 不同平台字体更宽）里让表单行整体换行，
        # 而不是把内容撑出横向滚动条；300px 正常宽度下不会触发换行。
        for form in self.findChildren(QFormLayout):
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.WrapLongRows)

    def _switch_tab(self, idx: int) -> None:
        """在「控制」与「参数」两页之间切换，滚动区高度紧贴当前页内容。"""
        is_ctrl = (idx == 0)
        self._page_control.setVisible(is_ctrl)
        self._page_config.setVisible(not is_ctrl)
        self._tab_control_btn.setChecked(is_ctrl)
        self._tab_config_btn.setChecked(not is_ctrl)

    def _style_refresh_button(self) -> None:
        """串口刷新按钮：优先用 30×30 紧凑图标，无图标库时回退紧凑固定宽文字。

        绝对不能设为 QSizePolicy.Ignored，否则在 QHBoxLayout 里会被 stretch=1 的
        port_combo 推出父容器右边界裁切。
        """
        btn = self._refresh_btn
        ic = action_icon("rescan")
        btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if ic is not None:
            btn.setText("")
            btn.setIcon(ic)
            btn.setFixedSize(FIELD_HEIGHT, FIELD_HEIGHT)
        else:
            btn.setIcon(QIcon())
            txt = tr("刷新")
            btn.setText(txt)
            w = max(44, self.fontMetrics().horizontalAdvance(txt) + 18)
            btn.setFixedSize(w, FIELD_HEIGHT)

    def refresh_icons(self) -> None:
        """切换主题时按新前景色重建按钮图标（深色白 / 浅色黑）。"""
        if hasattr(self, "_refresh_btn"):
            self._style_refresh_button()

    def _normalize_field_sizes(self) -> None:
        """统一输入框/下拉框的宽高（连接/抬落笔/书写选项各组混排参差）。

        高度取全局 :data:`writerstudio.ui.style.FIELD_HEIGHT`；宽度最小
        88px 并允许横向扩展——表单行里的字段撑满同一列，视觉上等宽对齐。
        """
        unify_inputs(self, min_width=88, button_height=30)
        # 保持刷新按钮的紧凑固定尺寸，不被 unify_inputs 改宽
        self._style_refresh_button()
        # 同行均分并排按钮：macOS Aqua / Windows / Linux 原生样式默认给每个 QPushButton
        # 强加 75~95px 最小宽度地板，多按钮并排会把 300px 停靠区撑出横向滚动条。
        # 仅对「整行所有成员等宽均分」的按钮设 Ignored + 文案最小宽（绝不用于与
        # stretch=1 输入框混排的按钮，避免被推出右缘）。
        fm = self.fontMetrics()
        compact: list[tuple[QPushButton, tuple[str, ...]]] = [
            (self._tab_control_btn, (self._tab_control_btn.text(),)),
            (self._tab_config_btn, (self._tab_config_btn.text(),)),
            (self.home_btn, (self.home_btn.text(),)),
            (self.unlock_btn, (self.unlock_btn.text(),)),
            (self.pen_test_btn, (self.pen_test_btn.text(),)),
            (self.pen_up_btn, (self.pen_up_btn.text(),)),
            (self._gen_btn, (self._gen_btn.text(),)),
            (self._exp_btn, (self._exp_btn.text(),)),
            (self.send_btn, (self.send_btn.text(),)),
            (self.pause_btn, (tr("暂停"), tr("继续"))),
            (self.abort_btn, (self.abort_btn.text(),)),
            *((b, (b.text(),)) for b in self._jog_buttons),
        ]
        for btn, labels in compact:
            btn.setSizePolicy(QSizePolicy.Policy.Ignored,
                              QSizePolicy.Policy.Fixed)
            min_w = max(fm.horizontalAdvance(t) for t in labels) + 12
            btn.setMinimumWidth(min_w)

    # --------------------------------------------------------------- 端口
    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        ports = list_ports()
        real = [(d, s) for d, s in ports if not d.startswith("/dev/ttyS")]
        if not real:
            self.port_combo.addItem(tr("（未发现串口）"), "")
            self.port_combo.setToolTip(tr("未发现串口；点「刷新」重新扫描"))
        else:
            self.port_combo.setToolTip("")
        for dev, desc in real:
            self.port_combo.addItem(trf('{v0} — {v1}', v0=dev, v1=desc), dev)

    # --------------------------------------------------------------- 连接
    def _toggle_connect(self) -> None:
        if self.link.connected:
            self.link.disconnect()
        else:
            port = self.port_combo.currentData()
            if not port:
                return
            baud = int(self.baud_combo.currentText())
            try:
                self.link.connect(port, baud,
                                  dtr=self.dtr_check.isChecked(),
                                  rts=self.rts_check.isChecked())
            except Exception as exc:
                self.state_label.setText(trf('连接失败：{v0}', v0=exc))

    def _on_state(self, st: State) -> None:
        names = {
            State.DISCONNECTED: tr("未连接"),
            State.CONNECTING: tr("连接中…"),
            State.IDLE: tr("空闲"),
            State.RUNNING: tr("运行中"),
            State.PAUSED: tr("已暂停"),
            State.ALARM: tr("报警"),
        }
        self.state_label.setText(names.get(st, st.value))
        self.connect_btn.setText(tr('断开') if st is not State.DISCONNECTED else tr('连接'))
        # 控制器可能在本软件之外进入保持（Hold）——按钮显示「继续」以便恢复
        self.pause_btn.setText(tr('继续') if st is State.PAUSED else tr('暂停'))
        if st is not State.IDLE:
            # 离开空闲态（断开/运行/报警等）立即取消自动抬回定时器，避免滞后抬笔
            self.cancel_pen_test()
        if st is State.DISCONNECTED:
            # 断开后旧坐标不再代表笔头位置，留着会让「读取当前笔位」
            # 拿到上一次连接的过期值；固件参数同理，未连接时不显示。
            self._machine_pos = None
            self.pos_label.setText(tr("—"))
            self.feed_label.setText(tr("—"))
            self.fw_label.setText(tr("—"))
        elif st is State.CONNECTING:
            self.fw_label.setText(tr("读取中…"))

    # ------------------------------------------------- 进度与笔寿命
    def _tick_progress(self) -> None:
        """发送中每 0.5s 刷新：百分比 + 已用时间 + 预计剩余。"""
        if self.link.state is not State.RUNNING:
            if self.link.state is State.IDLE:
                self._job_started = None
            return
        if self._job_started is None:
            self._job_started = time.monotonic()
            return
        acked, total = self.link.progress()
        if total <= 0:
            return
        elapsed = time.monotonic() - self._job_started
        pct = acked / total * 100.0
        eta = elapsed / pct * (100.0 - pct) if pct > 0.5 else 0.0
        self.set_progress(
            trf('发送中：{v0:.0f}%（{v1}/{v2} 行）  已用 {v3}  预计还需 {v4}', v0=pct, v1=acked, v2=total, v3=format_duration(elapsed), v4=format_duration(eta)))

    # -- 笔寿命（累计落笔书写长度，QSettings 持久化）--
    _PEN_KEY = "machine/pen_draw_mm"
    _PEN_LIMIT_KEY = "machine/pen_limit_mm"

    def _pen_settings(self) -> QSettings:
        return QSettings("writerstudio", "machine")

    def _load_penlife(self) -> None:
        s = self._pen_settings()
        self._pen_draw = float(s.value(self._PEN_KEY, 0.0) or 0.0)
        try:
            self.pen_limit.setValue(float(s.value(self._PEN_LIMIT_KEY, 0.0) or 0.0))
        except (TypeError, ValueError):
            self.pen_limit.setValue(0.0)
        self._update_penlife_label()

    def add_draw_length(self, mm: float) -> None:
        """累加一次作业的落笔书写长度（发送作业时由主窗口调用）。"""
        self._pen_draw = getattr(self, "_pen_draw", 0.0) + max(0.0, mm)
        s = self._pen_settings()
        s.setValue(self._PEN_KEY, self._pen_draw)
        self._update_penlife_label()

    def _save_penlife(self) -> None:
        self._pen_settings().setValue(self._PEN_LIMIT_KEY, self.pen_limit.value())
        self._update_penlife_label()

    def _reset_penlife(self) -> None:
        self._pen_draw = 0.0
        self._pen_settings().setValue(self._PEN_KEY, 0.0)
        self._update_penlife_label()

    def _update_penlife_label(self) -> None:
        draw_m = getattr(self, "_pen_draw", 0.0) / 1000.0
        limit = self.pen_limit.value()
        txt = trf('累计书写：{v0:.1f} m', v0=draw_m)
        if limit > 0 and draw_m >= limit:
            self.penlife_label.setText(trf('{v0}，已超过提醒阈值，建议更换/削笔！', v0=txt))
            set_role(self.penlife_label, "danger")
        else:
            self.penlife_label.setText(txt)
            set_role(self.penlife_label, "muted")

    def _on_event(self, e: LinkEvent) -> None:
        if e.kind == "state":
            try:
                self._on_state(State(e.text))
            except ValueError:
                pass
        elif e.kind == "status" and e.status is not None:
            st = e.status
            self._machine_pos = st.pos
            if st.pos:
                self.pos_label.setText(
                    f"X {st.pos[0]:.2f}  Y {st.pos[1]:.2f}  Z {st.pos[2]:.2f}")
            if st.feed is not None or st.speed is not None:
                self.feed_label.setText(
                    f"F {st.feed or 0:.0f} / S {st.speed or 0:.0f}")
            self.statusUpdated.emit(st)
        elif e.kind == "error":
            self.progress_label.setText(f"{e.text}")
        elif e.kind == "alarm":
            self.progress_label.setText(f"{e.text}")
        elif e.kind == "version":
            self.connect_btn.setToolTip(trf('固件：{v0}', v0=e.text))
        elif e.kind == "profile" and e.profile is not None:
            self._on_profile(e.profile)
        elif e.kind == "stale":
            self.state_label.setText(tr("无响应？"))
            self.progress_label.setText(trf('{v0}（检查 USB/固件）', v0=e.text))
        elif e.kind == "job_done":
            self.pause_btn.setText(tr("暂停"))
            self.set_progress(tr("作业完成"))
            if self.sleep_check.isChecked():
                try:
                    self.link.sleep_now()
                except Exception:
                    pass
        elif e.kind == "line" and "作业" in e.text:
            self.progress_label.setText(e.text)

    def _guarded(self, fn, *args) -> None:
        """未连接时点命令按钮给界面提示，而不是抛未捕获的 ConnectionError。"""
        if not self.link.connected:
            self.state_label.setText(tr("未连接"))
            return
        try:
            fn(*args)
        except Exception as exc:
            self.progress_label.setText(trf('命令失败：{v0}', v0=exc))

    def _on_profile(self, prof) -> None:
        """能力探测完成：显示固件参数摘要（只读快照，不给建议）。

        profile 事件只在真正连上机器并完成 $$ 探测后产生，因此这里直接
        显示；启动时回填的旧档案不写标签（见 MainWindow._restore_machine_profile），
        断开时由 _on_state 清掉——未连接就看不到固件参数。
        """
        self.fw_label.setText(prof.summary())
        self.profileDetected.emit(prof)

    # --------------------------------------------------------------- Jog
    def _jog(self, dx: float, dy: float) -> None:
        step = self.jog_step.value()
        feed = float(self.jog_feed.value())
        self._guarded(self.link.jog, dx * step, dy * step, feed)

    # --------------------------------------------------------------- 抬落笔
    def _on_pen_mode(self) -> None:
        mode = self.pen_mode.currentData()
        is_z = mode == PEN_Z
        # 只显示当前方式真正可调的项（隐藏而不是标灰）：表单里看不到的
        # 控件不会占位，面板也不会被挤出一排无意义的灰框。
        for w in (self.pen_down_z, self.pen_up_z,
                  self.pen_z_feed, self.stroke_taper, self.entry_taper):
            self._set_pen_row_visible(w, is_z)
        self._set_pen_row_visible(
            self.pen_s, mode in (PEN_M3M5, PEN_SERVO_ANGLE))
        self._set_pen_row_visible(self.pen_up_s, mode == PEN_SERVO_ANGLE)
        self._set_pen_row_visible(self.laser_power, mode == PEN_LASER)
        self._set_pen_row_visible(self.laser_preview, mode == PEN_LASER)

    def cancel_pen_test(self) -> None:
        """取消未到期的落笔测试自动抬回定时器。"""
        if self._pen_test_timer.isActive():
            self._pen_test_timer.stop()
        self._pen_test_cfg = None

    def _can_manual_pen(self) -> bool:
        """检查当前是否允许执行手动抬/落笔测试，不允许则给出界面提示。"""
        if not self.link.connected:
            self.state_label.setText(tr("未连接"))
            return False
        if self.link.state in (State.RUNNING, State.PAUSED):
            self.progress_label.setText(tr("作业进行中，无法测试抬落笔"))
            return False
        if self.link.state is State.ALARM:
            self.progress_label.setText(tr("机器处于报警状态，请先回零或解锁"))
            return False
        return True

    def pen_test(self, *, to_z: Optional[float] = None,
                 hold_ms: Optional[int] = None) -> list[str]:
        """单次落笔测试：按当前配置下压，停顿 ``hold_ms`` 毫秒后自动抬回。

        ``hold_ms=0`` 时保持落笔不自动抬回（用于外部工具或手动配对抬笔）。
        返回实际发出的 G-code 指令行；被拦截或失败时返回空列表。
        """
        if not self._can_manual_pen():
            return []
        self.cancel_pen_test()
        cfg = self.current_config()
        try:
            cmds = self.link.pen_down(cfg, to_z=to_z)
        except Exception as exc:
            self.progress_label.setText(trf('命令失败：{v0}', v0=exc))
            return []
        ms = self.PEN_TEST_HOLD_MS if hold_ms is None else int(hold_ms)
        if ms > 0:
            self._pen_test_cfg = cfg
            self._pen_test_timer.start(ms)
            self.progress_label.setText(tr("落笔测试中，稍后自动抬笔…"))
        else:
            self.progress_label.setText(tr("已发送落笔指令"))
        return cmds

    def pen_up_now(self) -> list[str]:
        """立即执行单次抬笔（同时取消任何未到期的落笔测试自动抬回）。"""
        self.cancel_pen_test()
        if not self._can_manual_pen():
            return []
        cfg = self.current_config()
        try:
            cmds = self.link.pen_up(cfg)
        except Exception as exc:
            self.progress_label.setText(trf('命令失败：{v0}', v0=exc))
            return []
        self.progress_label.setText(tr("已发送抬笔指令"))
        return cmds

    def _on_pen_test_timeout(self) -> None:
        """落笔测试保压结束：若仍处于连接空闲态则自动抬笔。"""
        cfg = self._pen_test_cfg
        self._pen_test_cfg = None
        if not self.link.connected or self.link.state is not State.IDLE:
            return
        try:
            self.link.pen_up(cfg or self.current_config())
            self.progress_label.setText(tr("落笔测试完成，已自动抬笔"))
        except Exception as exc:
            self.progress_label.setText(trf('命令失败：{v0}', v0=exc))

    def _set_pen_row_visible(self, widget, visible: bool) -> None:
        """同时隐藏/显示表单行里的控件与它的标签。"""
        widget.setVisible(visible)
        label = self._pen_form.labelForField(widget)
        if label is not None:
            label.setVisible(visible)

    def _on_single_step(self, on: bool) -> None:
        self.link.single_step = on

    # --------------------------------------------------------------- 坐标轴
    def _on_axis_changed(self) -> None:
        # 同步写回底层配置对象，防止 self.config 滞留在启动/加载项目时的旧快照
        self.config.swap_xy = self.swap_xy_check.isChecked()
        self.config.invert_x = self.invert_x_check.isChecked()
        self.config.invert_y = self.invert_y_check.isChecked()
        self.axisOptionsChanged.emit()

    # --------------------------------------------------------------- 起点
    def _on_mark_start_toggled(self, checked: bool) -> None:
        """按钮勾选 = 进入画布拾取（仅在尚未设置标记时可能）。"""
        if self.start_point.pen_marker is not None:
            self.mark_start_btn.blockSignals(True)
            self.mark_start_btn.setChecked(False)
            self.mark_start_btn.blockSignals(False)
            return
        self.markStartPickRequested.emit(checked)

    def _on_mark_start_clicked(self) -> None:
        """按钮双态：已设置标记 → 请求移除；未设置 → 上面的 toggled 进入拾取。"""
        if self.start_point.pen_marker is not None:
            self.removeStartMarkerRequested.emit()

    def _update_marker_button(self) -> None:
        """按是否已有标记切换按钮形态（手动标记起点 ⇄ 移除手动标记点）。"""
        has = self.start_point.pen_marker is not None
        self.mark_start_btn.blockSignals(True)
        self.mark_start_btn.setCheckable(not has)
        self.mark_start_btn.setText(tr('移除手动标记点') if has else tr('手动标记起点'))
        self.mark_start_btn.setChecked(False)
        self.mark_start_btn.blockSignals(False)

    def reset_mark_start(self) -> None:
        """画布拾取完成/取消后复位「手动标记起点」按钮（不触发取消逻辑）。"""
        self._update_marker_button()

    def remove_start_marker(self) -> None:
        """移除起点标记（同时退出笔位校准），并广播起点变化。"""
        self.start_point.pen_marker = None
        if self.start_point.mode == MODE_REGISTERED:
            self.start_point.mode = MODE_CANVAS
            self.start_point.point = (0.0, 0.0)
        self._update_marker_button()
        self._update_start_summary()
        self.startPointChanged.emit()

    def normalize_start_point(self) -> None:
        """兼容旧数据：画布模式记了起点坐标但没落标记 → 补上标记。"""
        sp = self.start_point
        if (sp.mode == MODE_CANVAS and sp.pen_marker is None
                and any(abs(v) > 1e-9 for v in sp.point)):
            sp.pen_marker = (float(sp.point[0]), float(sp.point[1]))
        self._update_marker_button()
        self._update_start_summary()

    def _update_start_summary(self) -> None:
        """简明单行展示当前起点状态与坐标，详细说明放进悬停提示。"""
        marker = self.start_point.pen_marker
        if marker is None:
            self.start_summary.setText(tr("未设置"))
            self.start_summary.setToolTip(tr("尚未设置写字起点"))
        elif self.start_point.mode == MODE_REGISTERED:
            x, y = self.start_point.point
            self.start_summary.setText(
                trf('已校准 · X {v0:.1f}, Y {v1:.1f} mm', v0=x, v1=y))
            self.start_summary.setToolTip(
                tr("画布红点标记已对准该机器笔位，整页内容相对标记原样落位"))
        else:
            self.start_summary.setText(
                trf('已标记 · X {v0:.1f}, Y {v1:.1f} mm', v0=marker[0], v1=marker[1]))
            self.start_summary.setToolTip(
                tr("内容左下角将对齐到该机器坐标起笔"))
        set_role(self.start_summary, "")

    def _toggle_pause(self) -> None:
        try:
            if self.link.state is State.PAUSED:
                self.link.resume()
                self.pause_btn.setText(tr("暂停"))
            elif self.link.state is State.RUNNING:
                # 抬笔暂停：笔尖离纸，避免进给保持期间洇墨
                self.link.pause(pen_cfg=self.current_config())
                self.pause_btn.setText(tr("继续"))
        except Exception as exc:
            # 槽里抛未捕获异常会让应用直接退出（PySide6 行为）
            self.progress_label.setText(trf('命令失败：{v0}', v0=exc))

    def _abort(self) -> None:
        self.cancel_pen_test()
        if not self.link.connected:
            self.state_label.setText(tr("未连接"))
            return
        self.link.abort(pen_cfg=self.current_config(),
                        return_start=self.return_start_check.isChecked())

    # --------------------------------------------------------------- 取值
    def current_config(self) -> GCodeConfig:
        c = self.config.clone()
        c.pen_mode = self.pen_mode.currentData()
        c.pen_up_z = self.pen_up_z.value()
        c.pen_down_z = self.pen_down_z.value()
        c.pen_z_feed = self.pen_z_feed.value()
        c.pen_down_s = self.pen_s.value()
        # 舵机角度模式的落笔角度与「落笔 S 值」共用同一控件（都是 M3 S 值）：
        # 此前 pen_down_angle 没有任何赋值路径，舵机模式落笔恒为 M3 S0，
        # 笔根本压不到纸上
        c.pen_down_angle = self.pen_s.value()
        c.pen_up_s = self.pen_up_s.value()
        c.laser_power = self.laser_power.value()
        c.laser_preview_power = self.laser_preview.value()
        c.pen_on_delay = self.pen_on_delay.value()
        c.pen_off_delay = self.pen_off_delay.value()
        c.draw_feed = self.draw_feed.value()
        c.travel_feed = self.travel_feed.value()
        c.custom_start_gcode = self.custom_start_edit.text().strip()
        c.custom_end_gcode = self.custom_end_edit.text().strip()
        c.return_to_start = self.return_start_check.isChecked()
        c.simplify_mm = self.simplify_spin.value()
        c.stroke_taper_mm = self.stroke_taper.value()
        c.entry_taper_mm = self.entry_taper.value()
        c.order_mode = self.order_combo.currentData() or "reading"
        c.swap_xy = self.swap_xy_check.isChecked()
        c.invert_x = self.invert_x_check.isChecked()
        c.invert_y = self.invert_y_check.isChecked()
        # 能力探测到的加速度只进估算/建议，不写进面板控件（探测一次，
        # 断线后档案仍在 link 上；未探测 = 0 = 估算退化为旧模型）
        prof = getattr(self.link, "profile", None)
        if prof is not None:
            c.accel_xy = float(getattr(prof, "accel_xy", 0.0) or 0.0)
        return c

    def current_start_point(self) -> StartPoint:
        """当前写字起点（内部 StartPoint 即真相，不再经模式/坐标控件）。"""
        return self.start_point.clone()

    def set_start_point(self, mode: str, x: float, y: float) -> None:
        """程序化设置写字起点（模式 + 机器坐标）。

        画布模式：坐标即起点标记（尚未落标记时补落到该坐标）。最后统一发
        一次 startPointChanged，避免拖动标记时以中间状态连续刷新。
        """
        self.start_point.mode = mode
        self.start_point.point = (float(x), float(y))
        if mode == MODE_CANVAS:
            # 画布模式下起点即标记：坐标与标记始终一致
            self.start_point.pen_marker = (float(x), float(y))
        self._update_marker_button()
        self._update_start_summary()
        self.startPointChanged.emit()

    def _apply_config_to_ui(self) -> None:
        """把 ``self.config`` / ``self.start_point`` 的值同步到界面控件。"""
        c = self.config
        idx = self.pen_mode.findData(c.pen_mode)
        if idx >= 0:
            self.pen_mode.setCurrentIndex(idx)
        self.pen_up_z.setValue(c.pen_up_z)
        self.pen_down_z.setValue(c.pen_down_z)
        self.pen_z_feed.setValue(c.pen_z_feed)
        # 舵机角度模式下该控件即「落笔角度」
        self.pen_s.setValue(int(c.pen_down_angle if c.pen_mode == PEN_SERVO_ANGLE
                                else c.pen_down_s))
        self.pen_up_s.setValue(int(c.pen_up_s))
        self.laser_power.setValue(int(c.laser_power))
        self.laser_preview.setValue(int(c.laser_preview_power))
        self.pen_on_delay.setValue(c.pen_on_delay)
        self.pen_off_delay.setValue(c.pen_off_delay)
        self.draw_feed.setValue(c.draw_feed)
        self.travel_feed.setValue(c.travel_feed)
        self.custom_start_edit.setText(c.custom_start_gcode)
        self.custom_end_edit.setText(c.custom_end_gcode)
        self.return_start_check.setChecked(c.return_to_start)
        self.simplify_spin.setValue(float(getattr(c, "simplify_mm", 0.0) or 0.0))
        self.stroke_taper.setValue(float(getattr(c, "stroke_taper_mm", 0.0) or 0.0))
        self.entry_taper.setValue(float(getattr(c, "entry_taper_mm", 0.0) or 0.0))
        idx = self.order_combo.findData(getattr(c, "order_mode", "reading"))
        self.order_combo.setCurrentIndex(max(0, idx))
        self.single_step_check.setChecked(bool(getattr(self.link, "single_step", False)))
        # 坐标轴对调/反转（赋值期间屏蔽信号，避免把「回填」当用户改动）
        for chk, val in ((self.swap_xy_check, c.swap_xy),
                         (self.invert_x_check, c.invert_x),
                         (self.invert_y_check, c.invert_y)):
            chk.blockSignals(True)
            chk.setChecked(bool(val))
            chk.blockSignals(False)
        # 起点设置已无模式/坐标控件：只刷新按钮形态与摘要
        self._update_marker_button()
        self._update_start_summary()

    def set_baud(self, baud: int) -> None:
        i = self.baud_combo.findText(str(baud))
        if i >= 0:
            self.baud_combo.setCurrentIndex(i)

    def baud(self) -> int:
        try:
            return int(self.baud_combo.currentText())
        except ValueError:
            return 115200

    def panel_state(self) -> dict:
        """面板散项快照（点动/连接等，供主窗口持久化）。"""
        return {
            "jog_step": float(self.jog_step.value()),
            "jog_feed": int(self.jog_feed.value()),
            "single_step": self.single_step_check.isChecked(),
            "sleep_after": self.sleep_check.isChecked(),
            "dtr": self.dtr_check.isChecked(),
            "rts": self.rts_check.isChecked(),
        }

    def apply_panel_state(self, state: dict) -> None:
        """恢复面板散项；赋值期间屏蔽信号，避免把「恢复」当成用户改动。"""
        for w in (self.jog_step, self.jog_feed):
            w.blockSignals(True)
        try:
            self.jog_step.setValue(float(state.get("jog_step", 10.0)))
            self.jog_feed.setValue(int(state.get("jog_feed", 2500)))
        finally:
            for w in (self.jog_step, self.jog_feed):
                w.blockSignals(False)
        self.single_step_check.setChecked(bool(state.get("single_step", False)))
        self.sleep_check.setChecked(bool(state.get("sleep_after", False)))
        for chk, key in ((self.dtr_check, "dtr"), (self.rts_check, "rts")):
            chk.blockSignals(True)
            chk.setChecked(bool(state.get(key, True)))
            chk.blockSignals(False)

    def machine_pos(self) -> Optional[tuple[float, float, float]]:
        return self._machine_pos

    def set_progress(self, text: str) -> None:
        self.progress_label.setText(text)
