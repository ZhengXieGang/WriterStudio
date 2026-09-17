"""P5 写字起点与机器 UI 测试。"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from writerstudio.core.document import Document, make_static_object  # noqa: E402
from writerstudio.core.geometry import BBox  # noqa: E402
from writerstudio.core.sample import make_rect  # noqa: E402
from writerstudio.machine.config import machine_to_page, page_to_machine  # noqa: E402
from writerstudio.machine.serial_link import FakeSerial  # noqa: E402
from writerstudio.machine.start_point import (  # noqa: E402
    MODE_CANVAS,
    MODE_CURRENT,
    MODE_ORIGIN,
    MODE_REGISTERED,
    StartPoint,
    compute_offset,
    content_bbox,
    registered_offset,
)
from writerstudio.ui.main_window import MainWindow  # noqa: E402


# ============================================================ 起点逻辑
def test_offset_canvas_mode_places_left_bottom():
    box = BBox(5.0, 10.0, 25.0, 40.0)
    off = compute_offset(StartPoint(MODE_CANVAS, (100.0, 50.0)), box)
    # 包围盒左下角 (5,10) → (100,50)
    assert off == (95.0, 40.0)


def test_offset_origin_mode_is_zero():
    assert compute_offset(StartPoint(MODE_ORIGIN, (99, 99)), BBox(1, 2, 3, 4)) == (0.0, 0.0)


def test_offset_current_mode_uses_machine_pos():
    box = BBox(0.0, 0.0, 10.0, 10.0)
    off = compute_offset(StartPoint(MODE_CURRENT), box, machine_pos=(50.0, 60.0, 0.0))
    assert off == (50.0, 60.0)


def test_offset_current_without_pos_is_zero():
    box = BBox(0.0, 0.0, 10.0, 10.0)
    assert compute_offset(StartPoint(MODE_CURRENT), box, machine_pos=None) == (0.0, 0.0)


def test_start_point_roundtrip():
    sp = StartPoint(MODE_CANVAS, (1.5, 2.5))
    back = StartPoint.from_data(sp.to_data())
    assert back.mode == MODE_CANVAS
    assert back.point == (1.5, 2.5)


def test_content_bbox_ignores_invisible():
    doc = Document()
    a = make_static_object([make_rect(0, 0, 10, 10)])
    b = make_static_object([make_rect(100, 100, 10, 10)])
    b.visible = False
    doc.add(a)
    doc.add(b)
    box = content_bbox(doc)
    assert box.as_tuple() == (0.0, 0.0, 10.0, 10.0)


# ============================================================ UI
@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_machine_panel_builds(qapp):
    win = MainWindow()
    p = win.machine_panel
    assert p.pen_mode.count() == 6          # z/舵机角度/激光/custom/m3m5/m7m9
    # 起点系统：无「高级选项」，手动标记按钮默认「手动标记起点」
    assert not hasattr(p, "advanced_box")
    assert not hasattr(p, "start_mode")
    assert p.mark_start_btn.text() == "手动标记起点"
    assert p.state_label.text() in ("未连接", "空闲")
    win.close()


def test_speed_defaults(qapp):
    """速度/斜落笔默认值合理（面板与配置一致）。"""
    from writerstudio.machine.config import GCodeConfig
    c = GCodeConfig()
    assert c.draw_feed == 4400.0            # 中位笔画可达峰速（a=3000）
    assert c.pen_z_feed == 12000.0          # 固件 Z 上限内
    assert c.stroke_taper_mm == 1.5         # 收笔斜抬默认开
    assert c.entry_taper_mm == 0.5          # 入笔斜落默认开
    win = MainWindow()
    assert float(win.machine_panel.jog_feed.value()) == 2500.0   # 默认 2500
    assert float(win.machine_panel.draw_feed.value()) == 4400.0
    assert float(win.machine_panel.pen_z_feed.value()) == 12000.0
    win.close()


def test_order_mode_combo_wired_and_persisted(qapp):
    """书写顺序下拉接入配置并持久化（默认阅读顺序）。"""
    win = MainWindow()
    p = win.machine_panel
    assert p.order_combo.count() == 2
    assert p.current_config().order_mode == "reading"
    p.order_combo.setCurrentIndex(p.order_combo.findData("shortest"))
    assert p.current_config().order_mode == "shortest"
    # 回填
    p.config.order_mode = "shortest"
    p._apply_config_to_ui()
    assert p.order_combo.currentData() == "shortest"
    win.close()


def test_machine_panel_config_from_ui(qapp):
    win = MainWindow()
    p = win.machine_panel
    p.pen_mode.setCurrentIndex(p.pen_mode.findData("m3m5"))
    p.draw_feed.setValue(2000.0)
    cfg = p.current_config()
    assert cfg.pen_mode == "m3m5"
    assert cfg.draw_feed == 2000.0
    win.close()


def test_machine_panel_start_point_from_ui(qapp):
    """起点即标记：set_start_point 落标记，current_start_point 原样返回。"""
    win = MainWindow()
    p = win.machine_panel
    p.set_start_point(MODE_CANVAS, 12.0, 34.0)
    sp = p.current_start_point()
    assert sp.mode == MODE_CANVAS
    assert sp.point == (12.0, 34.0)
    assert sp.pen_marker == pytest.approx((12.0, 34.0))
    win.close()


def test_machine_panel_connect_via_fake(qapp):
    win = MainWindow()
    fake = FakeSerial()
    win.machine_panel.link.connect_backend(fake)
    qapp.processEvents()
    assert win.machine_panel.link.connected
    assert win.machine_panel.state_label.text() == "空闲"
    win.machine_panel.link.disconnect()
    win.close()


def test_main_window_build_gcode_anchors_content_at_start(qapp):
    """起点即内容锚点：内容包围盒左下角被放到起点的机器坐标。

    「写字起点」的落地方式是把纸张的实际台面位置烘焙进绝对坐标——起点
    偏移 = 标定笔位 − page_to_machine(内容左下角)。回归：此前 P26/P27
    重构丢掉了偏移，内容恒从纸张左上角写起，机器必然偏离标定位置。
    """
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    win.machine_panel.set_start_point(MODE_CANVAS, 100.0, 50.0)
    r = win._build_gcode()
    text = "\n".join(r.lines)
    # 页面左下角 (0,0) → 标定起点机器 (100,50)，矩形沿 +x/−y 展开 10mm
    assert "G0 X100 Y50" in text          # 首条 G0 停在起点（=内容锚点）
    assert "G1 X110 Y50" in text
    assert "G1 X110 Y40" in text
    assert "G1 X100 Y40" in text
    # 未经标定的纸张左上角一带（x 0..10 / y 200..210）不应再出现
    assert "Y200" not in text and "Y210" not in text
    win.close()


def test_main_window_build_gcode_without_start_starts_at_content(qapp):
    """未设起点：首条 G0 直接落在第一笔起点（内容左下角→机器坐标）。"""
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    assert win._start_machine_xy() is None
    r = win._build_gcode()
    # 内容 (0,0) → 机器 (0, 210)；先于此处书写
    assert "G0 X0 Y210" in "\n".join(r.lines)
    win.close()


def test_main_window_send_requires_connection(qapp, monkeypatch):
    win = MainWindow()
    warned = []
    monkeypatch.setattr("writerstudio.ui.main_window.QMessageBox.warning",
                        lambda *a, **k: warned.append(a))
    win._send_job()
    assert warned            # 未连接应告警
    win.close()


def test_main_window_send_job_to_fake(qapp, monkeypatch):
    """未设起点时弹窗询问；选「否」→ 直接发送（无起点停笔位）。"""
    monkeypatch.setattr(
        "writerstudio.ui.main_window.QMessageBox.question",
        lambda *a, **k: QMessageBox.No)
    win = MainWindow()
    fake = FakeSerial()
    win.machine_panel.link.connect_backend(fake)
    win._send_job()
    link = win.machine_panel.link
    end = time.time() + 4
    while time.time() < end and link.progress()[0] < link.progress()[1]:
        qapp.processEvents()
        time.sleep(0.01)
    acked, total = link.progress()
    assert total > 0 and acked == total
    assert len(fake.out_lines) >= total - 1
    link.disconnect()
    win.close()


def test_main_window_send_job_yes_moves_to_origin_first(qapp, monkeypatch):
    """未设起点选「是」：先读笔位 → 抬笔 → G0 到机械原点 → 再发作业。"""
    monkeypatch.setattr(
        "writerstudio.ui.main_window.QMessageBox.question",
        lambda *a, **k: QMessageBox.Yes)
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    win.machine_panel.pen_mode.setCurrentIndex(
        win.machine_panel.pen_mode.findData("z"))
    fake = FakeSerial(initial_pos=(50.0, 60.0, 0.0))
    win.machine_panel.link.connect_backend(fake)
    qapp.processEvents()
    win._send_job()
    link = win.machine_panel.link
    end = time.time() + 4
    # 起点→原点的移动是异步的：先等作业入队（total>0），再等全部 ack
    while time.time() < end and (link.progress()[1] == 0
                                 or link.progress()[0] < link.progress()[1]):
        qapp.processEvents()
        time.sleep(0.01)
    acked, total = link.progress()
    assert total > 0 and acked == total
    # 抬笔 + 移动到机械原点，且发生在作业书写指令之前（F 值随配置，不写死）
    assert any(l.startswith("G1 Z0 F") for l in fake.out_lines)
    idx_origin = fake.out_lines.index("G0 G90 X0 Y0")
    idx_job = next(i for i, l in enumerate(fake.out_lines)
                   if l.startswith("G1 X"))
    assert idx_origin < idx_job
    link.disconnect()
    win.close()


def test_canvas_pick_mode_callback(qapp):
    win = MainWindow()
    seen = {}

    def cb(x, y):
        seen["p"] = (x, y)

    win.canvas.set_pick_mode(True, cb)
    assert win.canvas._pick_mode
    # 直接触发回调路径（模拟点击）
    win.canvas.set_pick_mode(False, None)
    cb(5.0, 6.0)
    assert seen["p"] == (5.0, 6.0)
    win.close()


# ==================================================== 标记"吸住"/坐标轴回归
def test_axis_mapping_zeroes_offset_keeps_axes():
    from writerstudio.machine.config import GCodeConfig
    c = GCodeConfig()
    c.origin_offset = (12.0, 34.0)
    c.swap_xy = True
    am = c.axis_mapping()
    assert am.origin_offset == (0.0, 0.0)
    assert am.swap_xy
    # 轴映射自身仍可逆
    assert am.unmap_point(am.map_point((3.0, 4.0))) == (3.0, 4.0)


def test_marker_display_follows_beyond_old_clamp(qapp):
    """拖动标记（含机器坐标超出旧 ±500 钳制区）显示必须严格跟随，不再"吸住"。"""
    win = MainWindow()
    for page_pt in [(10.0, 10.0), (300.0, 400.0), (632.7, 611.0)]:
        win._on_pen_marker_moved(*page_pt)                 # 模拟拖动的 mouseMove
        marker_m = win.machine_panel.start_point.pen_marker
        assert win._pen_marker_page(marker_m) == pytest.approx(page_pt)
        # 画布上的标记也被同步到同一位置（拖动结束后）
        win.canvas._marker_dragging = False
        win._on_machine_config_changed()
        assert win.canvas.pen_marker() == pytest.approx(page_pt)
    win.close()


def test_origin_annotation_near_page_regardless_of_start(qapp):
    """机械原点标注=轴映射零点的纸面位置，与起点/内容无关且就在纸边可见。"""
    win = MainWindow()
    win._on_pen_marker_moved(632.7, 611.0)                 # 旧版会把标注推出纸外
    win._on_machine_config_changed()
    got = win.canvas.machine_origin()
    assert got is not None
    pos = got[0]
    page = win.controller.doc.page
    assert -60.0 <= pos[0] <= page.width + 60.0
    assert -60.0 <= pos[1] <= page.height + 60.0
    win.close()


def test_set_start_point_emits_once(qapp):
    win = MainWindow()
    p = win.machine_panel
    n = []
    p.startPointChanged.connect(lambda: n.append(1))
    p.set_start_point(MODE_CANVAS, 12.345, 67.891)
    assert len(n) == 1
    # 起点 = 标记：程序化设置后标记与按钮形态同步
    assert p.start_point.pen_marker == pytest.approx((12.345, 67.891))
    assert p.mark_start_btn.text() == "移除手动标记点"
    win.close()


def test_remove_start_marker_restores_button(qapp):
    """「移除手动标记点」：清标记、退出校准模式，按钮回到「手动标记起点」。"""
    win = MainWindow()
    p = win.machine_panel
    fired = []
    p.removeStartMarkerRequested.connect(lambda: fired.append(1))
    p.set_start_point(MODE_CANVAS, 40.0, 30.0)
    p._on_mark_start_clicked()               # 已有标记 → 请求移除
    assert fired
    assert p.start_point.pen_marker is None
    assert p.start_point.mode == MODE_CANVAS
    assert p.mark_start_btn.text() == "手动标记起点"
    win.close()


def test_registered_calibration_removed_by_remove_marker(qapp):
    """笔位校准后移除标记：模式退回画布、校准记录清零。"""
    win = MainWindow()
    p = win.machine_panel
    p.start_point.pen_marker = (100.0, 80.0)
    win._register_pen_at((150.0, 100.0))
    p.remove_start_marker()
    assert p.start_point.pen_marker is None
    assert p.start_point.mode == MODE_CANVAS
    assert p.start_point.point == (0.0, 0.0)
    win.close()


def test_normalize_start_point_backfills_marker(qapp):
    """旧数据：画布模式记了起点坐标但没落标记 → 自动补落标记。"""
    from writerstudio.machine.start_point import StartPoint as SP
    win = MainWindow()
    p = win.machine_panel
    p.start_point = SP(MODE_CANVAS, (60.0, 80.0))
    p.start_point.pen_marker = None
    p.normalize_start_point()
    assert p.start_point.pen_marker == pytest.approx((60.0, 80.0))
    assert p.mark_start_btn.text() == "移除手动标记点"
    # 全零起点视为未设置（新文档默认）
    p.start_point = SP(MODE_CANVAS, (0.0, 0.0))
    p.normalize_start_point()
    assert p.start_point.pen_marker is None
    win.close()


def test_canvas_marker_ignores_programmatic_set_while_dragging(qapp):
    win = MainWindow()
    c = win.canvas
    c.set_pen_marker((5.0, 5.0))
    c._marker_dragging = True
    c.set_pen_marker((99.0, 99.0))
    assert c.pen_marker() == (5.0, 5.0)
    c._marker_dragging = False
    c.set_pen_marker((99.0, 99.0))
    assert c.pen_marker() == (99.0, 99.0)
    win.close()


# ==================================================== 轴映射坐标域（P13 审计）
def test_servo_mode_pen_down_angle_wired(qapp):
    """舵机角度模式：落笔角度来自「落笔 S 值」控件（此前恒为 S0，笔压不下）。"""
    win = MainWindow()
    p = win.machine_panel
    p.pen_mode.setCurrentIndex(p.pen_mode.findData("servo-angle"))
    p.pen_s.setValue(90)
    cfg = p.current_config()
    assert cfg.pen_down_angle == 90
    assert cfg.pen_down_lines() == ["M3 S90"]
    # 回填：舵机模式下该控件显示落笔角度
    p.config.pen_mode = "servo-angle"
    p.config.pen_down_angle = 75
    p._apply_config_to_ui()
    assert p.pen_s.value() == 75
    win.close()


def test_panel_z_speed_and_laser_preview_wired(qapp):
    """面板暴露 Z 抬落笔速度与激光预览功率两个旋钮。"""
    win = MainWindow()
    p = win.machine_panel
    # zSpeed：Z 模式上升降通用
    p.pen_mode.setCurrentIndex(p.pen_mode.findData("z"))
    p.pen_z_feed.setValue(12000.0)
    cfg = p.current_config()
    assert cfg.pen_z_feed == 12000.0
    assert cfg.pen_up_lines() == ["G1 Z0 F12000"]
    # 非 Z 模式下 Z 速度控件应禁用
    p.pen_mode.setCurrentIndex(p.pen_mode.findData("laser"))
    assert not p.pen_z_feed.isEnabled()
    assert p.laser_preview.isEnabled()
    # laserPreviewS 往返
    p.laser_preview.setValue(100)
    assert p.current_config().laser_preview_power == 100
    p.config.laser_preview_power = 33
    p._apply_config_to_ui()
    assert p.laser_preview.value() == 33
    win.close()


def test_read_pen_position_uses_machine_pos(qapp):
    """「读取当前笔位」把机器当前坐标存为起点标记（机器坐标语义）。"""
    win = MainWindow()
    fake = FakeSerial(initial_pos=(100.0, 80.0, 0.0))
    win.machine_panel.link.connect_backend(fake)
    qapp.processEvents()
    win.machine_panel.link.request_status()
    end = time.time() + 2
    while time.time() < end and win.machine_panel.machine_pos() is None:
        qapp.processEvents()
        time.sleep(0.01)
    win._on_read_pen_as_start()
    qapp.processEvents()
    assert win.machine_panel.start_point.pen_marker == pytest.approx((100.0, 80.0))
    assert win.machine_panel.start_point.mode == MODE_CANVAS
    win.machine_panel.link.disconnect()
    win.close()


def test_move_pen_to_marker_issues_absolute_g0(qapp):
    """「移动到标记起点」应抬笔并 G0 到标记的机器坐标，且先清偏移。"""
    win = MainWindow()
    # 前序测试可能把笔控方式持久化成别的模式，这里显式用 Z 轴
    win.machine_panel.pen_mode.setCurrentIndex(
        win.machine_panel.pen_mode.findData("z"))
    fake = FakeSerial()
    win.machine_panel.link.connect_backend(fake)
    qapp.processEvents()
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win._on_move_pen_to_marker()
    # send_line 直写串口，但读线程/连接问候是异步的；轮询至命令到齐
    end = time.time() + 2
    while time.time() < end and not any(
            l.startswith("G0 G90 X100 Y80") for l in fake.out_lines):
        qapp.processEvents()
        time.sleep(0.01)
    assert any(l.startswith("G0 G90 X100 Y80") for l in fake.out_lines)
    # 抬笔指令先发出（避免划伤纸面；F 值随配置，不写死）
    assert any(l.startswith("G1 Z0 F") for l in fake.out_lines)
    # 绝对移动前清固件工件偏移，否则会跑到错误物理点位
    assert "G10 L2 P1 X0 Y0 Z0" in fake.out_lines
    assert "G92.1" in fake.out_lines
    win.machine_panel.link.disconnect()
    win.close()


def test_machine_config_ignores_start_point(qapp):
    """固定映射：起点不再产生书写偏移，内容恒从左上角原点铺开。"""
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win.machine_panel.set_start_point(MODE_CANVAS, 100.0, 80.0)
    cfg = win._machine_config()
    assert cfg.origin_offset == (0.0, 0.0)
    win.close()


# ==================================================== 笔位校准（标记不动）
def test_registered_offset_maps_marker_page_to_machine():
    """笔位校准：标记纸面点应映射到记录的机器坐标；标记本身位置不变。"""
    from writerstudio.machine.config import GCodeConfig
    axis = GCodeConfig()                       # 恒等轴映射，origin_offset=0
    sp = StartPoint(MODE_REGISTERED, (150.0, 100.0))
    sp.pen_marker = (100.0, 80.0)
    off = registered_offset(sp, axis)
    assert off == pytest.approx((50.0, 20.0))
    # 标记所在纸面点经含偏移映射后 = 记录的机器坐标
    assert axis.map_point((100.0, 80.0)) != pytest.approx((150.0, 100.0))
    assert (100.0 + off[0], 80.0 + off[1]) == pytest.approx((150.0, 100.0))
    # 无标记 → None（调用方退化为零偏移）
    sp2 = StartPoint(MODE_REGISTERED, (150.0, 100.0))
    assert registered_offset(sp2, axis) is None


def test_register_pen_keeps_marker_and_switches_mode(qapp):
    """「以当前笔位校准标记」不得移动标记，只切换模式并记录机器坐标。"""
    win = MainWindow()
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win.machine_panel.set_start_point(MODE_CANVAS, 100.0, 80.0)
    win._register_pen_at((150.0, 100.0))
    # 标记原地不动
    assert win.machine_panel.start_point.pen_marker == pytest.approx((100.0, 80.0))
    # 模式切到笔位校准，记录笔位
    assert win.machine_panel.start_point.mode == MODE_REGISTERED
    assert win.machine_panel.start_point.point == pytest.approx((150.0, 100.0))
    win.close()


def test_register_pen_anchors_content_at_calibrated_pen(qapp):
    """校准后「标记纸面点 → 校准笔位」的对应被烘焙进绝对坐标。"""
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win._register_pen_at((150.0, 100.0))
    # origin_offset 恒为 0（起点偏移在生成端以 start_offset 施加，不写回配置）
    assert win._machine_config().origin_offset == (0.0, 0.0)
    anchor = win._start_anchor()
    assert anchor is not None
    s_page, off = anchor
    # 校准不变量：标记纸面点经固定映射 + 偏移后 = 校准记录的笔位
    swap, ix, iy = win._axis_opts()
    page = win.controller.doc.page
    mx, my = page_to_machine(s_page, page.height, swap, ix, iy)
    assert (mx + off[0], my + off[1]) == pytest.approx((150.0, 100.0))
    # 首条 G0 停在校准笔位；内容沿同一偏移整体平移
    r = win._build_gcode()
    text = "\n".join(r.lines)
    assert "G0 X150 Y100" in text
    win.close()


def test_read_pen_after_calibration_keeps_marker(qapp):
    """校准模式下再读取笔位 = 重做校准：标记不动、标定刷新。

    回归：此前「读取当前笔位」会退回画布模式并把标记移到笔位，校准被
    静默丢弃，起点位置随之改变。
    """
    win = MainWindow()
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win._register_pen_at((150.0, 100.0))
    win._apply_read_pen((160.0, 110.0))        # 模拟读到新笔位
    sp = win.machine_panel.start_point
    assert sp.mode == MODE_REGISTERED         # 仍是校准模式
    assert sp.pen_marker == pytest.approx((100.0, 80.0))   # 标记未动
    assert sp.point == pytest.approx((160.0, 110.0))       # 标定刷新
    win.close()


def test_marker_drag_preserves_registered_mode(qapp):
    """校准后拖动标记：不退回画布模式、校准平移保持（内容不动），
    且机械原点标注纹丝不动（回归：旧版标注会跟着拖动乱跳）。"""
    win = MainWindow()
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win._register_pen_at((150.0, 100.0))       # 平移 = (150,100) − (100,80)
    origin_before = win.canvas.machine_origin()
    win._on_pen_marker_moved(120.0, 90.0)      # 页面坐标 → 机器 (120, 210−90)
    sp = win.machine_panel.start_point
    assert sp.mode == MODE_REGISTERED
    assert sp.pen_marker == pytest.approx((120.0, 120.0))
    # 读数 = 新标记位置 + 原校准平移 (50, 20)：内容落点不变
    assert sp.point == pytest.approx((170.0, 140.0))
    assert win._machine_config().origin_offset == (0.0, 0.0)
    # 机械原点标注恒在左上角
    assert win.canvas.machine_origin() == origin_before
    page = win.controller.doc.page
    assert win.canvas.machine_origin()[0] == pytest.approx((0.0, page.height))
    win.close()


def test_register_pen_without_marker_warns(qapp):
    """未放标记时点校准只提示、不崩溃。"""
    win = MainWindow()
    win.machine_panel.start_point.pen_marker = None
    win.machine_panel.start_point.mode = MODE_CANVAS   # 前序测试可能残留校准模式
    win._on_register_pen()
    assert win.machine_panel.start_point.mode != MODE_REGISTERED
    win.close()


def test_move_pen_uses_registered_machine_pos(qapp):
    """校准后「移动到标记起点」应发往校准记录的笔位，而非标记拖放坐标。"""
    win = MainWindow()
    win.machine_panel.pen_mode.setCurrentIndex(
        win.machine_panel.pen_mode.findData("z"))
    win.machine_panel.start_point.pen_marker = (100.0, 80.0)
    win._register_pen_at((150.0, 100.0))
    fake = FakeSerial()
    win.machine_panel.link.connect_backend(fake)
    qapp.processEvents()
    win._on_move_pen_to_marker()
    end = time.time() + 2
    while time.time() < end and not any(
            l.startswith("G0 G90 X150 Y100") for l in fake.out_lines):
        qapp.processEvents()
        time.sleep(0.01)
    assert any(l.startswith("G0 G90 X150 Y100") for l in fake.out_lines)
    win.machine_panel.link.disconnect()
    win.close()


# ==================================================== 原点标注与纸张旋转（P26）
def test_origin_annotation_pinned_top_left(qapp):
    """机械原点标注恒在纸张左上角：X 向右、Y 沿左边向下（页面 Y 向上）。"""
    win = MainWindow()
    pos, xd, yd = win.canvas.machine_origin()
    page = win.controller.doc.page
    assert pos == pytest.approx((0.0, page.height))
    assert (round(xd[0]), round(xd[1])) == (1.0, 0.0)
    assert (round(yd[0]), round(yd[1])) == (0.0, -1.0)
    # 与起点设置无关
    win.machine_panel.set_start_point(MODE_CANVAS, 632.7, 611.0)
    win._on_machine_config_changed()
    pos2, xd2, yd2 = win.canvas.machine_origin()
    assert pos2 == pytest.approx((0.0, page.height))
    assert (xd2, yd2) == (xd, yd)
    win.close()


def test_rotate_page_keeps_origin_pinned(qapp):
    """旋转纸张：内容转向，机械原点标注仍在新页面左上角，可撤销。"""
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(20, 30, 10, 10)]))
    win.controller.set_document(doc)
    page0 = (win.controller.doc.page.width, win.controller.doc.page.height)

    win._rotate_page("cw")
    page = win.controller.doc.page
    assert (page.width, page.height) == (page0[1], page0[0])
    pos, xd, yd = win.canvas.machine_origin()
    assert pos == pytest.approx((0.0, page.height))
    assert (round(xd[0]), round(xd[1])) == (1.0, 0.0)
    assert (round(yd[0]), round(yd[1])) == (0.0, -1.0)

    # 撤销旋转：页面尺寸还原，原点标注仍在新（旧）页面左上角
    win.controller.undo_stack.undo()
    page = win.controller.doc.page
    assert (page.width, page.height) == page0
    pos, _, _ = win.canvas.machine_origin()
    assert pos == pytest.approx((0.0, page.height))
    win.close()


def test_canvas_minimal_viewport_update(qapp):
    """画布用局部重绘：旋转/拖动不再每帧全视口重绘（卡顿根因）。"""
    from PySide6.QtWidgets import QGraphicsView
    win = MainWindow()
    assert win.canvas.viewportUpdateMode() == QGraphicsView.MinimalViewportUpdate
    win.close()


# ============================================================ 标记钉在纸面（P42）
def test_axis_change_reglues_start_marker(qapp):
    """切换对调/反转：标记机器坐标重算，画布（纸面）位置不变。"""
    win = MainWindow()
    sp = win.machine_panel.start_point
    sp.pen_marker = win._page_to_machine((50.0, 60.0))
    win._on_machine_config_changed()          # 建立轴快照
    q0 = win._machine_to_page(sp.pen_marker)
    assert q0 == pytest.approx((50.0, 60.0), abs=1e-9)
    old_machine = sp.pen_marker
    win.machine_panel.swap_xy_check.setChecked(True)   # 触发 axisOptionsChanged
    q1 = win._machine_to_page(sp.pen_marker)
    assert q1 == pytest.approx((50.0, 60.0), abs=1e-9)
    assert sp.pen_marker != old_machine       # 机器坐标确实随映射换算
    win.close()


def test_axis_change_keeps_calibration_anchor(qapp):
    """校准模式：轴变化后锚定纸面点不动，读数换算到新轴描述。

    校准平移 = 读数 − 标记映射，两者都必须换算；读数若保持旧描述，
    平移被破坏，内容整体错位而画布看不出。
    """
    win = MainWindow()
    page = win.controller.doc.page
    h = page.height
    old = win._axis_opts()
    sp = win.machine_panel.start_point
    q = (50.0, 60.0)
    sp.pen_marker = win._page_to_machine(q)
    M = (123.4, -56.7)                        # 校准时笔的机器读数（任意）
    win.machine_panel.set_start_point(MODE_REGISTERED, M[0], M[1])
    win._on_machine_config_changed()
    win.machine_panel.swap_xy_check.setChecked(not old[0])
    win._on_machine_config_changed()
    new = win._axis_opts()
    s_page, offset = win._start_anchor()
    assert s_page == pytest.approx(q, abs=1e-9)      # 锚定纸面点不动
    # 读数已换算到新描述；锚点在新描述下的落点 = 换算后的读数
    M_new = page_to_machine(machine_to_page(M, h, *old), h, *new)
    assert sp.point == pytest.approx(M_new, abs=1e-9)
    cfg = win._machine_config()
    mx, my = page_to_machine(s_page, h,
                             cfg.swap_xy, cfg.invert_x, cfg.invert_y)
    assert (mx + offset[0], my + offset[1]) == pytest.approx(M_new, abs=1e-9)
    win.close()


def test_rotate_page_rotates_start_marker(qapp):
    """旋转纸张：起点标记随内容同转（仍钉在同一物理纸面点）。"""
    from writerstudio.core.document import ROTATE_CW
    win = MainWindow()
    sp = win.machine_panel.start_point
    w0 = win.controller.doc.page.width
    sp.pen_marker = win._page_to_machine((50.0, 60.0))
    win._rotate_page(ROTATE_CW)
    # 顺时针 90°：(x, y) → (y, width − x)
    q = win._machine_to_page(sp.pen_marker)
    assert q == pytest.approx((60.0, w0 - 50.0), abs=1e-6)
    win.close()


def test_send_warns_when_marker_off_page(qapp, monkeypatch):
    """标记显示在页面外（陈旧校准）：发送前拦截，默认取消。"""
    win = MainWindow()
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 10, 10)]))
    win.controller.set_document(doc)
    sp = win.machine_panel.start_point
    sp.pen_marker = (50.0, 400.0)        # 恒等映射 → 页面 (50, -103)：纸外
    sp.mode = MODE_REGISTERED
    warned = []

    def fake_warning(*a, **k):
        warned.append(a)
        return QMessageBox.No

    monkeypatch.setattr(
        "writerstudio.ui.main_window.QMessageBox.warning",
        staticmethod(fake_warning))
    assert win._warn_stale_marker() is False
    assert warned
    # 页面内的标记不拦截
    sp.pen_marker = win._page_to_machine((50.0, 60.0))
    assert win._warn_stale_marker() is True
    win.close()


def test_axis_change_rebases_registered_reading(qapp):
    """对调/反转：校准记录的机器读数随标记一起换算到新轴描述。

    校准平移 = 读数 − 标记映射。若只换算标记而读数不动，平移被破坏，
    书写整体错位（错位量 = 同一物理点在新旧描述下的读数差）。
    """
    win = MainWindow()
    page = win.controller.doc.page
    h = page.height
    old = win._axis_opts()
    new = (not old[0], old[1], old[2])          # 只切对调
    sp = win.machine_panel.start_point
    # 标记在页面 (50, 60)，纸在台面上偏 t=(8, 12)mm（校准吸收）
    marker_q = (50.0, 60.0)
    sp.pen_marker = page_to_machine(marker_q, h, *old)
    t = (8.0, 12.0)
    sp.point = (sp.pen_marker[0] + t[0], sp.pen_marker[1] + t[1])
    sp.mode = MODE_REGISTERED

    win.machine_panel.swap_xy_check.setChecked(not old[0])
    assert (win.machine_panel.swap_xy_check.isChecked(),) == (new[0],)

    def phys(q, opts, off):
        """页面点 q 在 opts 描述下的机器坐标（含纸偏移）。"""
        m = page_to_machine(q, h, *opts)
        return (m[0] + off[0], m[1] + off[1])

    # 内容任一页点的机器落点 = 换算前同一物理点（新描述 = 旧坐标经 S 变换）
    def rebase(c):
        return page_to_machine(machine_to_page(c, h, *old), h, *new)

    for q in [(0.0, 0.0), (50.0, 60.0), (100.0, 200.0)]:
        before = phys(q, old, t)
        after = phys(q, new, (sp.point[0] - sp.pen_marker[0],
                              sp.point[1] - sp.pen_marker[1]))
        assert rebase(before) == pytest.approx(after, abs=1e-9)
    # 标记仍钉在同一页面点，读数 = 换算后的（旧标记 + 平移）
    assert win._machine_to_page(sp.pen_marker) == pytest.approx(marker_q)
    marker_old = page_to_machine(marker_q, h, *old)
    assert rebase((marker_old[0] + t[0], marker_old[1] + t[1])) == \
        pytest.approx(sp.point)
    win.close()


def test_registered_marker_drag_keeps_calibration(qapp):
    """校准模式拖标记：校准平移保持不变（内容不动），只挪参考钉。"""
    win = MainWindow()
    page = win.controller.doc.page
    h = page.height
    sp = win.machine_panel.start_point
    sp.mode = MODE_REGISTERED
    sp.pen_marker = page_to_machine((50.0, 60.0), h)
    sp.point = (sp.pen_marker[0] + 8.0, sp.pen_marker[1] + 12.0)

    win._on_pen_marker_moved(70.0, 90.0)

    assert sp.mode == MODE_REGISTERED
    assert win._machine_to_page(sp.pen_marker) == pytest.approx((70.0, 90.0))
    off = (sp.point[0] - sp.pen_marker[0], sp.point[1] - sp.pen_marker[1])
    assert off == pytest.approx((8.0, 12.0))     # 平移不变 → 输出不变
    win.close()


def test_restore_aligns_axis_baseline_before_refresh(qapp):
    """存档按其自身轴描述解释：恢复时基线先对齐，不得再换算一次。

    回归：恢复 swapped 配置后 _on_machine_config_changed 若按旧基线
    （默认描述）比较，会把已按 swapped 存储的标记/读数错误再换算。
    """
    win = MainWindow()
    page = win.controller.doc.page
    h = page.height
    opts = win._axis_opts()
    sp = win.machine_panel.start_point
    marker_q = (50.0, 60.0)
    sp.mode = MODE_REGISTERED
    sp.pen_marker = page_to_machine(marker_q, h, *opts)
    sp.point = (sp.pen_marker[0] + 8.0, sp.pen_marker[1] + 12.0)
    win.machine_panel._apply_config_to_ui()
    # 模拟恢复收尾：基线对齐当前描述后刷新，数据必须原样不动
    win._axis_state = win._axis_opts()
    win._on_machine_config_changed()
    assert win._machine_to_page(sp.pen_marker) == pytest.approx(marker_q)
    assert sp.point == pytest.approx((sp.pen_marker[0] + 8.0,
                                      sp.pen_marker[1] + 12.0))
    win.close()
