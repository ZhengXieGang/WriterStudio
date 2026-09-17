"""机器控制面板：连接、Jog、状态、写字起点、G-code 生成与发送。

面板通过信号与主窗口交互（生成/导出由主窗口提供文档访问）。
"""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import QSettings, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

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
from .style import spin, unify_inputs

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

    def __init__(self, link: Optional[GrblLink] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.linkEventReceived.connect(self._on_event)
        self.link = link or GrblLink(on_event=self.linkEventReceived.emit)
        self.config = GCodeConfig()
        self.start_point = StartPoint()
        self._machine_pos: Optional[tuple[float, float, float]] = None
        self._job_started: Optional[float] = None
        self._build_ui()
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._tick_progress)
        self._ui_timer.start(500)
        self._refresh_ports()
        self._on_state(State.DISCONNECTED)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        # 外层只放一个滚动区：面板内容较长（连接/状态/Jog/抬落笔/起点/笔位/作业），
        # 若不滚动，矮窗口下底部控件会被裁掉、无法操作。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        root = QVBoxLayout(body)
        root.setContentsMargins(4, 4, 4, 4)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        # ---- 连接 ----
        conn = QGroupBox("连接")
        cf = QFormLayout(conn)
        row = QHBoxLayout()
        self.port_combo = QComboBox()
        self.port_combo.setMinimumWidth(90)
        self.port_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(6)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_ports)
        row.addWidget(self.port_combo, 1)
        row.addWidget(refresh)
        cf.addRow("串口", row)

        self.baud_combo = QComboBox()
        for b in _BAUD_RATES:
            self.baud_combo.addItem(str(b))
        self.baud_combo.setCurrentText("115200")
        cf.addRow("波特率", self.baud_combo)

        # DTR/RTS：ESP32 板载串口芯片会把它们接到复位/Boot 脚，
        # 打开串口即复位固件；连不上时试着取消勾选。
        lines_row = QHBoxLayout()
        self.dtr_check = QCheckBox("DTR")
        self.dtr_check.setChecked(True)
        self.rts_check = QCheckBox("RTS")
        self.rts_check.setChecked(True)
        self.dtr_check.setToolTip("连接后保持 DTR 电平；ESP32 板连上即复位时取消勾选")
        self.rts_check.setToolTip("连接后保持 RTS 电平；ESP32 板连上即复位时取消勾选")
        lines_row.addWidget(self.dtr_check)
        lines_row.addWidget(self.rts_check)
        lines_row.addStretch(1)
        cf.addRow("串口电平", lines_row)

        btns = QHBoxLayout()
        self.connect_btn = QPushButton("连接")
        self.connect_btn.clicked.connect(self._toggle_connect)
        self.home_btn = QPushButton("回零")
        self.home_btn.clicked.connect(lambda: self._guarded(self.link.home))
        self.unlock_btn = QPushButton("解锁")
        self.unlock_btn.clicked.connect(lambda: self._guarded(self.link.unlock))
        btns.addWidget(self.connect_btn)
        btns.addWidget(self.home_btn)
        btns.addWidget(self.unlock_btn)
        cf.addRow(btns)
        root.addWidget(conn)

        # ---- 状态 ----
        st = QGroupBox("状态")
        sf = QFormLayout(st)
        # 动态文本标签统一可换行：发送中/笔寿命提醒等长文字若不换行，
        # 会把面板最小宽度撑到 350px+（默认停靠宽 300px），内容被挤出
        # 画面外、出现横向滚动条
        self.state_label = QLabel("未连接")
        self.state_label.setWordWrap(True)
        self.pos_label = QLabel("—")
        self.pos_label.setWordWrap(True)
        self.feed_label = QLabel("—")
        self.feed_label.setWordWrap(True)
        self.fw_label = QLabel("—")
        self.fw_label.setWordWrap(True)
        # 布局只有拿到 heightForWidth 标志才会按换行后的实际行数给行高，
        # 否则长文本在表单行里被裁掉下半截
        for w in (self.state_label, self.pos_label,
                  self.feed_label, self.fw_label):
            sp = w.sizePolicy()
            sp.setHeightForWidth(True)
            w.setSizePolicy(sp)
        self.fw_label.setToolTip(
            "连接后自动读取固件设置（$$）得到的参数快照：\n"
            "加速度/速率上限用于书写耗时估算；只读取，不修改。")
        sf.addRow("状态", self.state_label)
        sf.addRow("机械坐标", self.pos_label)
        sf.addRow("进给/转速", self.feed_label)
        sf.addRow("固件参数", self.fw_label)
        root.addWidget(st)

        # ---- Jog ----
        jog = QGroupBox("手动控制 (Jog)")
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
        self.jog_feed.setToolTip("点动速度（mm/min）")
        jf.addRow("步长", self.jog_step)
        jf.addRow("速度", self.jog_feed)
        jogv.addLayout(jf)

        pad = QGridLayout()
        pad.setContentsMargins(0, 0, 0, 0)

        def mk(label, dx, dy):
            b = QPushButton(label)
            b.clicked.connect(lambda: self._jog(dx, dy))
            return b

        pad.addWidget(mk("Y+", 0, 1), 0, 1)
        pad.addWidget(mk("X−", -1, 0), 1, 0)
        pad.addWidget(mk("X+", 1, 0), 1, 2)
        pad.addWidget(mk("Y−", 0, -1), 2, 1)
        for c in range(3):
            pad.setColumnStretch(c, 1)
        jogv.addLayout(pad)
        root.addWidget(jog)

        # ---- 抬落笔设置 ----
        pen = QGroupBox("抬落笔")
        pf = QFormLayout(pen)
        self.pen_mode = QComboBox()
        for key, label in PEN_MODES.items():
            self.pen_mode.addItem(label, key)
        self.pen_mode.currentIndexChanged.connect(self._on_pen_mode)
        pf.addRow("方式", self.pen_mode)
        # 步进 Z 轴（抬/落笔各走一段受控 Z 进给）。默认按弹簧回位笔架标定：
        # 落笔 Z 6mm、抬笔 Z 0mm（弹簧回位笔架释放位）。
        self.pen_down_z = spin(-10.0, 50.0, 6.0, " mm", 0.5)
        self.pen_down_z.setToolTip("落笔 Z 位置：Z 轴笔控写到该高度即下压触纸")
        self.pen_up_z = spin(0.0, 50.0, 0.0, " mm", 0.5)
        self.pen_up_z.setToolTip("抬笔 Z 位置：弹簧回位笔架填 0（释放位）")
        self.pen_z_feed = spin(1.0, 20000.0, 12000.0, " mm/min", 100.0, 0)
        self.pen_z_feed.setToolTip(
            "Z 抬落笔速度：抬笔与落笔均以此进给升降。\n"
            "每笔两趟 Z 全行程是手写作业的最大固定开销——在固件 Z 速率上限"
            "（连接后自动探测显示在「固件参数」行）内尽量调高。")
        pf.addRow("落笔 Z", self.pen_down_z)
        pf.addRow("抬笔 Z", self.pen_up_z)
        pf.addRow("Z 速度", self.pen_z_feed)
        # 收笔斜抬/入笔斜落：笔画两端边走边抬/落（手写的压力渐释动作），
        # 消除末端满压停留的墨点与起点重压墨坨。仅 Z 轴笔控生效。
        self.stroke_taper = spin(0.0, 5.0, 1.5, " mm", 0.25, 2)
        self.stroke_taper.setToolTip(
            "收笔斜抬：笔画最后这段长度内边走边抬笔（0=关闭）。\n"
            "消除「笔在笔画末端满压停留」留下的墨点，收笔更自然。\n"
            "建议 1~2mm；仅 Z 轴笔控生效，斜率受「Z 速度/书写速度」约束。")
        self.entry_taper = spin(0.0, 5.0, 0.5, " mm", 0.25, 2)
        self.entry_taper.setToolTip(
            "入笔斜落：笔画开头这段长度内边走边落笔（0=关闭）。\n"
            "消除起笔重压墨坨，起笔更自然；建议 0.5~1mm。仅 Z 轴笔控生效。")
        pf.addRow("收笔斜抬", self.stroke_taper)
        pf.addRow("入笔斜落", self.entry_taper)
        # 舵机角度（抬/落笔均发 M3 S<角度>）
        self.pen_s = QSpinBox()
        self.pen_s.setRange(0, 30000)
        self.pen_s.setValue(0)
        self.pen_s.setToolTip("落笔角度 / M3 模式落笔 S 值")
        pf.addRow("落笔角度/S", self.pen_s)
        self.pen_up_s = QSpinBox()
        self.pen_up_s.setRange(0, 30000)
        self.pen_up_s.setValue(50)
        self.pen_up_s.setToolTip("抬笔角度")
        pf.addRow("抬笔角度", self.pen_up_s)
        # 激光（M3 S 出光 / M5 关光）
        self.laser_power = QSpinBox()
        self.laser_power.setRange(0, 30000)
        self.laser_power.setValue(1000)
        self.laser_power.setToolTip("出光功率（对应 $30 最大功率的 S 值）")
        pf.addRow("激光功率", self.laser_power)
        self.laser_preview = QSpinBox()
        self.laser_preview.setRange(0, 30000)
        self.laser_preview.setValue(50)
        self.laser_preview.setToolTip("预览/对位低功率")
        pf.addRow("激光预览功率", self.laser_preview)
        self.pen_on_delay = spin(0.0, 5.0, 0.0, " s", 0.05)
        self.pen_off_delay = spin(0.0, 5.0, 0.0, " s", 0.05)
        self.pen_on_delay.setToolTip("落笔后等待（G4），让笔完全落下再写字")
        self.pen_off_delay.setToolTip("抬笔前等待（G4），防止拖墨")
        pf.addRow("落笔后等待", self.pen_on_delay)
        pf.addRow("抬笔前等待", self.pen_off_delay)
        self.draw_feed = spin(1.0, 20000.0, 4400.0, " mm/min", 50.0, 0)
        self.travel_feed = spin(1.0, 30000.0, 9000.0, " mm/min", 100.0, 0)
        self.draw_feed.setToolTip(
            "书写速度：短笔画受加速度限制，峰速有物理上限（连接后按固件\n"
            "加速度自动估算并在预览中提示）；设得再高只会平白增加每笔加减速。")
        pf.addRow("书写速度", self.draw_feed)
        pf.addRow("空程速度", self.travel_feed)
        self.custom_start_edit = QLineEdit()
        self.custom_start_edit.setPlaceholderText("如：M3 S0;G4 P0.5（分号分隔多条）")
        self.custom_start_edit.setToolTip(
            "作业开始前发送的自定义指令。\n"
            "注意：不要包含 G92/G54 等坐标偏移指令——本软件按绝对机械坐标\n"
            "书写，偏移会让整个作业整体错位（发送前的自动清理也会被它作废）。")
        self.custom_end_edit = QLineEdit()
        self.custom_end_edit.setPlaceholderText("如：M5")
        pf.addRow("开始前代码", self.custom_start_edit)
        pf.addRow("结束后代码", self.custom_end_edit)
        root.addWidget(pen)

        # ---- 坐标轴（对调/反转：适配机器原点朝向与导轨方向不同的机型）----
        ax = QGroupBox("坐标轴")
        axv = QVBoxLayout(ax)
        axv.setContentsMargins(4, 4, 4, 4)
        self.swap_xy_check = QCheckBox("对调 X/Y 轴")
        self.swap_xy_check.setToolTip(
            "交换 X 与 Y 轴：适合机器横放在纸左侧、笔沿纸的短边移动的装机方式。\n"
            "机械原点仍在纸张左上角，只是 X 轴改为沿纸边向下、Y 轴沿顶边向右。")
        self.swap_xy_check.toggled.connect(self._on_axis_changed)
        axv.addWidget(self.swap_xy_check)
        irow = QHBoxLayout()
        self.invert_x_check = QCheckBox("反转 X 轴")
        self.invert_x_check.setToolTip("X 轴方向取反：原点在纸的右上角一侧的机型用")
        self.invert_x_check.toggled.connect(self._on_axis_changed)
        self.invert_y_check = QCheckBox("反转 Y 轴")
        self.invert_y_check.setToolTip("Y 轴方向取反：原点在纸的左下角一侧的机型用")
        self.invert_y_check.toggled.connect(self._on_axis_changed)
        irow.addWidget(self.invert_x_check)
        irow.addWidget(self.invert_y_check)
        irow.addStretch(1)
        axv.addLayout(irow)
        root.addWidget(ax)

        # ---- 写字起点 ----
        sp = QGroupBox("写字起点")
        spv = QVBoxLayout(sp)

        self.read_pos_start_btn = QPushButton("读取当前笔位作为起点")
        self.read_pos_start_btn.setMinimumHeight(30)
        self.read_pos_start_btn.setToolTip(
            "读取机器笔头当前坐标作为写字起点（需已连接）。\n"
            "会把笔画标记移动到笔头当前位置。")
        self.read_pos_start_btn.clicked.connect(self.readPenAsStartRequested.emit)
        spv.addWidget(self.read_pos_start_btn)

        self.register_pen_btn = QPushButton("以当前笔位校准标记")
        self.register_pen_btn.setMinimumHeight(30)
        self.register_pen_btn.setToolTip(
            "标记不动，用笔来标定：先把红色标记放到纸上「希望笔开始写」的位置，\n"
            "再把笔手动移到纸上同一点，点此按钮。软件记录「笔现在就在标记处」，\n"
            "据此校正机器原点与纸张的偏差。适合机器原点与纸张角落对不齐时。")
        self.register_pen_btn.clicked.connect(self.registerPenRequested.emit)
        spv.addWidget(self.register_pen_btn)

        self.mark_start_btn = QPushButton("手动标记起点")
        self.mark_start_btn.setMinimumHeight(30)
        self.mark_start_btn.setToolTip(
            "在画布上单击一点作为写字起点；之后也可直接拖动画布上的标记微调。\n"
            "已设置标记后，此按钮变为「移除手动标记点」。")
        self.mark_start_btn.setCheckable(True)
        self.mark_start_btn.toggled.connect(self._on_mark_start_toggled)
        self.mark_start_btn.clicked.connect(self._on_mark_start_clicked)
        spv.addWidget(self.mark_start_btn)

        self.move_pen_btn = QPushButton("移动到标记起点")
        self.move_pen_btn.setToolTip("抬笔后把笔头移动到标记的起点位置")
        self.move_pen_btn.clicked.connect(self.movePenRequested.emit)
        spv.addWidget(self.move_pen_btn)

        self.start_summary = QLabel("当前起点：未设置")
        self.start_summary.setWordWrap(True)
        self.start_summary.setStyleSheet("color:#0a6;")
        spv.addWidget(self.start_summary)
        root.addWidget(sp)

        # 书写坐标：机械原点固定 = 纸张左上角（page_to_machine），上面的
        # 「坐标轴」组提供对调/反转两个方向修正（适配机器朝向不同的装机）；
        # 「坐标系位置」（原点选角）不再提供——原点恒在左上角。
        # （坐标系设置的 pos/revxy/xAxeRev/yAxeRev 在配置导入导出中保留。）

        # ---- 作业 ----
        job = QGroupBox("作业")
        jf = QVBoxLayout(job)
        # 勾选项分两行放，避免三个挤一行把面板最小宽度撑到需要横向滚动
        flow = QHBoxLayout()
        self.single_step_check = QCheckBox("单步发送")
        self.single_step_check.setToolTip(
            "一次只发一条命令，等机器回 ok 再发下一条。\n"
            "速度慢但最稳，适合调试机器或长内容。")
        self.single_step_check.toggled.connect(self._on_single_step)
        self.return_start_check = QCheckBox("完成后回起点")
        self.return_start_check.setChecked(True)
        self.return_start_check.setToolTip("写完后笔头回到 X0 Y0（写字起点）")
        flow.addWidget(self.single_step_check)
        flow.addWidget(self.return_start_check)
        flow.addStretch(1)
        jf.addLayout(flow)
        flow2 = QHBoxLayout()
        self.sleep_check = QCheckBox("完成后休眠")
        self.sleep_check.setToolTip("写完让机器休眠（$SLP，需固件支持）")
        flow2.addWidget(self.sleep_check)
        flow2.addStretch(1)
        jf.addLayout(flow2)
        # 书写顺序：阅读顺序 vs 最短空程
        orow = QHBoxLayout()
        orow.addWidget(QLabel("书写顺序"))
        self.order_combo = QComboBox()
        self.order_combo.addItem("阅读顺序（逐字/从上到下）", "reading")
        self.order_combo.addItem("最短空程（图形/签名）", "shortest")
        self.order_combo.setToolTip(
            "阅读顺序：写完一个字/一组再写下一个，文字从左到右、从上到下，\n"
            "读起来自然（常规书写顺序）。\n"
            "最短空程：贪心最近邻，抬笔移动最少，但会打乱顺序，适合矢量图/签名。")
        orow.addWidget(self.order_combo, 1)
        jf.addLayout(orow)
        # 笔画抽稀：高速书写时微小线段频繁换向会让线条发抖，适度抽稀更顺滑
        srow = QHBoxLayout()
        srow.addWidget(QLabel("笔画抽稀"))
        self.simplify_spin = QDoubleSpinBox()
        self.simplify_spin.setRange(0.0, 1.0)
        self.simplify_spin.setSingleStep(0.05)
        self.simplify_spin.setValue(0.0)
        self.simplify_spin.setSuffix(" mm")
        self.simplify_spin.setToolTip(
            "按此容差合并笔画上的密集采样点（0=关闭）。\n"
            "书写速度快时线条发抖可试 0.1~0.3；0.5 以上会明显失真。")
        srow.addWidget(self.simplify_spin)
        srow.addStretch(1)
        jf.addLayout(srow)
        gen = QPushButton("生成预览")
        gen.clicked.connect(self.generateRequested.emit)
        exp = QPushButton("导出 G-code…")
        exp.clicked.connect(self.exportRequested.emit)
        jf.addWidget(gen)
        jf.addWidget(exp)
        ctrl = QHBoxLayout()
        self.send_btn = QPushButton("发送")
        self.send_btn.clicked.connect(self.sendRequested.emit)
        self.pause_btn = QPushButton("暂停")
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.abort_btn = QPushButton("中止")
        self.abort_btn.clicked.connect(self._abort)
        ctrl.addWidget(self.send_btn)
        ctrl.addWidget(self.pause_btn)
        ctrl.addWidget(self.abort_btn)
        jf.addLayout(ctrl)
        self.progress_label = QLabel("—")
        self.progress_label.setWordWrap(True)
        jf.addWidget(self.progress_label)

        # ---- 笔寿命 ----
        lf = QFormLayout()
        self.penlife_label = QLabel()
        self.penlife_label.setWordWrap(True)
        self.pen_limit = spin(0.0, 10000.0, 0.0, " m", 5.0, 1)
        self.pen_limit.setToolTip("累计书写达到该长度时提醒更换/削笔；0 = 不提醒")
        self.pen_limit.valueChanged.connect(self._save_penlife)
        lrow = QHBoxLayout()
        self.pen_reset_btn = QPushButton("清零")
        self.pen_reset_btn.clicked.connect(self._reset_penlife)
        lrow.addWidget(self.pen_reset_btn)
        lrow.addWidget(QLabel("提醒阈值"))
        lrow.addWidget(self.pen_limit)
        lf.addRow(self.penlife_label)
        lf.addRow(lrow)
        jf.addLayout(lf)
        root.addWidget(job)
        root.addStretch(1)
        self._load_penlife()

        self._on_pen_mode()
        self._update_marker_button()
        self._update_start_summary()
        self._normalize_field_sizes()

    def _normalize_field_sizes(self) -> None:
        """统一输入框/下拉框的宽高（连接/抬落笔/作业各组混排参差）。

        高度取全局 :data:`writerstudio.ui.style.FIELD_HEIGHT`；宽度最小
        88px 并允许横向扩展——表单行里的字段撑满同一列，视觉上等宽对齐。
        """
        unify_inputs(self, min_width=88, button_height=30)

    # --------------------------------------------------------------- 端口
    def _refresh_ports(self) -> None:
        self.port_combo.clear()
        ports = list_ports()
        real = [(d, s) for d, s in ports if not d.startswith("/dev/ttyS")]
        if not real:
            self.port_combo.addItem("（未发现串口）", "")
        for dev, desc in real:
            self.port_combo.addItem(f"{dev} — {desc}", dev)

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
                self.state_label.setText(f"连接失败：{exc}")

    def _on_state(self, st: State) -> None:
        names = {
            State.DISCONNECTED: "未连接",
            State.CONNECTING: "连接中…",
            State.IDLE: "空闲",
            State.RUNNING: "运行中",
            State.PAUSED: "已暂停",
            State.ALARM: "报警",
        }
        self.state_label.setText(names.get(st, st.value))
        self.connect_btn.setText("断开" if st is not State.DISCONNECTED else "连接")
        # 控制器可能在本软件之外进入保持（Hold）——按钮显示「继续」以便恢复
        self.pause_btn.setText("继续" if st is State.PAUSED else "暂停")
        if st is State.DISCONNECTED:
            # 断开后旧坐标不再代表笔头位置，留着会让「读取当前笔位」
            # 拿到上一次连接的过期值
            self._machine_pos = None
            self.pos_label.setText("—")
            self.feed_label.setText("—")

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
            f"发送中：{pct:.0f}%（{acked}/{total} 行）"
            f"  已用 {format_duration(elapsed)}  预计还需 {format_duration(eta)}")

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
        txt = f"累计书写：{draw_m:.1f} m"
        if limit > 0 and draw_m >= limit:
            self.penlife_label.setText(f"{txt}，已超过提醒阈值，建议更换/削笔！")
            self.penlife_label.setStyleSheet("color:#c22;")
        else:
            self.penlife_label.setText(txt)
            self.penlife_label.setStyleSheet("color:#444;")

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
            self.connect_btn.setToolTip(f"固件：{e.text}")
        elif e.kind == "profile" and e.profile is not None:
            self._on_profile(e.profile)
        elif e.kind == "stale":
            self.state_label.setText("无响应？")
            self.progress_label.setText(f"{e.text}（检查 USB/固件）")
        elif e.kind == "job_done":
            self.pause_btn.setText("暂停")
            self.set_progress("作业完成")
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
            self.state_label.setText("未连接")
            return
        try:
            fn(*args)
        except Exception as exc:
            self.progress_label.setText(f"命令失败：{exc}")

    def _on_profile(self, prof) -> None:
        """能力探测完成：显示固件参数摘要（只读快照，不给建议）。"""
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
        self.pen_up_z.setEnabled(is_z)
        self.pen_down_z.setEnabled(is_z)
        self.pen_z_feed.setEnabled(is_z)
        self.pen_s.setEnabled(mode in (PEN_M3M5, PEN_SERVO_ANGLE))
        self.pen_up_s.setEnabled(mode == PEN_SERVO_ANGLE)
        self.laser_power.setEnabled(mode == PEN_LASER)
        self.laser_preview.setEnabled(mode == PEN_LASER)

    def _on_single_step(self, on: bool) -> None:
        self.link.single_step = on

    # --------------------------------------------------------------- 坐标轴
    def _on_axis_changed(self) -> None:
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
        self.mark_start_btn.setText("移除手动标记点" if has else "手动标记起点")
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
        """用一句人话说明当前起点设置。"""
        marker = self.start_point.pen_marker
        if marker is None:
            self.start_summary.setText(
                "当前起点：未设置——发送时会询问是否以机械原点为起点")
        elif self.start_point.mode == MODE_REGISTERED:
            x, y = self.start_point.point
            self.start_summary.setText(
                f"当前起点：标记处 = 机器坐标 ({x:.1f}, {y:.1f}) mm（笔位校准）")
        else:
            self.start_summary.setText(
                f"当前起点：机器坐标 ({marker[0]:.1f}, {marker[1]:.1f}) mm"
                "（可拖动画布标记微调）")

    def _toggle_pause(self) -> None:
        try:
            if self.link.state is State.PAUSED:
                self.link.resume()
                self.pause_btn.setText("暂停")
            elif self.link.state is State.RUNNING:
                # 抬笔暂停：笔尖离纸，避免进给保持期间洇墨
                self.link.pause(pen_cfg=self.current_config())
                self.pause_btn.setText("继续")
        except Exception as exc:
            # 槽里抛未捕获异常会让应用直接退出（PySide6 行为）
            self.progress_label.setText(f"命令失败：{exc}")

    def _abort(self) -> None:
        if not self.link.connected:
            self.state_label.setText("未连接")
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
