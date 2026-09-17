"""P5 机器控制测试：G-code 生成、笔序优化、GRBL 协议、串口流控。"""

from __future__ import annotations

import math
import random
import time

import pytest

from writerstudio.core.document import Document, PageSpec
from writerstudio.core.sample import make_rect
from writerstudio.core.strokes import Stroke
from writerstudio.core.document import make_static_object
from writerstudio.machine import grbl
from writerstudio.machine.config import (
    PEN_M3M5,
    PEN_M7M9,
    PEN_Z,
    GCodeConfig,
)
from writerstudio.machine.gcode_gen import (
    format_duration,
    generate_from_document,
    generate_gcode,
)
from writerstudio.machine.path_optimizer import (
    draw_length,
    optimize_order,
    total_travel,
)
from writerstudio.machine.serial_link import FakeSerial, GrblLink, State


# ===================================================== GCodeConfig
def test_config_feed_formatting_not_truncated():
    c = GCodeConfig(draw_feed=1500.0, travel_feed=3000.0, pen_z_feed=500.0)
    assert c._ff(1500.0) == "1500"      # 回归：曾误截为 "15"
    assert c._ff(500.0) == "500"


def test_config_coord_formatting():
    c = GCodeConfig(decimals=3)
    assert c._f(1.5) == "1.5"
    assert c._f(2.0) == "2"
    assert c._f(-0.25) == "-0.25"


def test_config_pen_modes():
    # Z 抬笔亦为受控进给（Z 速度升降通用），非 G0 最高速
    assert GCodeConfig(pen_mode=PEN_Z, pen_up_z=0, pen_down_z=2,
                       pen_z_feed=3000).pen_up_lines() == ["G1 Z0 F3000"]
    assert GCodeConfig(pen_mode=PEN_M3M5).pen_up_lines() == ["M5"]
    assert GCodeConfig(pen_mode=PEN_M7M9).pen_up_lines() == ["M9"]
    assert GCodeConfig(pen_mode=PEN_M3M5, pen_down_s=800).pen_down_lines() == ["M3 S800"]


def test_config_pen_modes_param_mapping():
    """四种笔控类型的参数映射齐全（速度/位置/指令各就各位）。"""
    from writerstudio.machine.config import (
        PEN_CUSTOM, PEN_LASER, PEN_SERVO_ANGLE)
    # Stepper：zOn/zOff/zSpeed
    z = GCodeConfig(pen_mode=PEN_Z, pen_down_z=6.0, pen_up_z=0.0,
                    pen_z_feed=12000)
    assert z.pen_down_lines() == ["G1 Z6 F12000"]
    assert z.pen_up_lines() == ["G1 Z0 F12000"]
    # Servo：penDownPos/penUpPos
    sv = GCodeConfig(pen_mode=PEN_SERVO_ANGLE, pen_down_angle=0, pen_up_s=50)
    assert sv.pen_down_lines() == ["M3 S0"]
    assert sv.pen_up_lines() == ["M3 S50"]
    # Laser：laserS / laserPreviewS（预览功率持久化）
    ls = GCodeConfig(pen_mode=PEN_LASER, laser_power=1000,
                     laser_preview_power=100)
    assert ls.pen_down_lines() == ["M3 S1000"]
    assert ls.pen_up_lines() == ["M5"]
    assert ls.laser_preview_power == 100
    # Custom：customOnCode/customOffCode
    cu = GCodeConfig(pen_mode=PEN_CUSTOM,
                     pen_up_command="M5", pen_down_command="M3 S500")
    assert cu.pen_down_lines() == ["M3 S500"]
    assert cu.pen_up_lines() == ["M5"]


def test_config_map_point_scale_mirror_offset():
    c = GCodeConfig(scale=2.0, origin_offset=(10.0, 5.0))
    assert c.map_point((1.0, 2.0)) == (12.0, 9.0)
    c2 = GCodeConfig(mirror_x=True)
    assert c2.map_point((3.0, 4.0)) == (-3.0, 4.0)


def test_config_roundtrip():
    c = GCodeConfig(pen_mode=PEN_M3M5, draw_feed=2000, origin_offset=(5.0, 6.0),
                    header=["G21"], footer=["M5"])
    back = GCodeConfig.from_data(c.to_data())
    assert back.pen_mode == PEN_M3M5
    assert back.origin_offset == (5.0, 6.0)
    assert back.draw_feed == 2000


# ===================================================== G-code 生成
def _two_strokes():
    return [
        Stroke([(0, 0), (10, 0)]),
        Stroke([(0, 5), (10, 5)]),
    ]


def test_generate_basic_structure():
    cfg = GCodeConfig(page_size=(100.0, 80.0))
    r = generate_gcode(_two_strokes(), cfg, optimize=False)
    text = "\n".join(r.lines)
    assert "G21" in r.lines and "G90" in r.lines
    assert text.count("G1 ") >= 2
    # 落笔与抬笔都有（Z 模式两者都是受控进给 G1），按配置的 Z 值判断，
    # 不写死具体数值（默认值会随机器标定调整）；开入笔斜落时落笔行
    # 只到「半压」高度（to_z），不等于 pen_down_z，故只数非抬笔的 Z 行
    up = f"G1 Z{cfg._f(cfg.pen_up_z)}"
    ups = [l for l in r.lines if l.startswith(up + " ")]
    downs = [l for l in r.lines
             if l.startswith("G1 Z") and not l.startswith(up + " ")]
    assert len(ups) >= 1 and len(downs) >= 1


def test_generate_fixed_top_left_mapping():
    # 固定映射：机械原点 = 纸张左上角，机器 y = 纸高 − 文档 y
    r = generate_gcode([Stroke([(0, 5), (10, 5)])],
                       GCodeConfig(page_size=(100.0, 80.0)))
    assert any("Y75" in l for l in r.lines if l.startswith("G1"))


def test_generate_ignores_origin_offset():
    # 固定映射下起点偏移不再影响内容落点（起点只是停笔位）
    r = generate_gcode([Stroke([(0, 0), (10, 0)])],
                       GCodeConfig(origin_offset=(100.0, 50.0),
                                   page_size=(100.0, 80.0)))
    assert any("X0 Y80" in l for l in r.lines)
    assert any("X10 Y80" in l for l in r.lines)
    assert not any("X100" in l for l in r.lines)


def test_generate_empty_returns_header_footer():
    r = generate_gcode([], GCodeConfig())
    assert r.stroke_count == 0
    assert r.lines  # 至少 header/footer


def test_generate_stats():
    r = generate_gcode(_two_strokes(), GCodeConfig(page_size=(100.0, 80.0)),
                       optimize=False)
    assert r.draw_length == pytest.approx(20.0)
    assert r.estimated_seconds > 0


def test_generate_from_document():
    doc = Document(page=PageSpec(297, 210))
    doc.add(make_static_object([make_rect(0, 0, 20, 10)]))
    r = generate_from_document(doc, GCodeConfig())
    assert r.stroke_count >= 1
    assert r.draw_length > 0


def test_generate_skips_invisible():
    doc = Document()
    o = make_static_object([make_rect(0, 0, 5, 5)])
    o.visible = False
    doc.add(o)
    r = generate_from_document(doc, GCodeConfig())
    assert r.stroke_count == 0


def test_format_duration():
    assert format_duration(45) == "45秒"
    assert format_duration(125) == "2分5秒"
    assert format_duration(3700).startswith("1小时")


# ===================================================== 笔序优化
def test_optimize_reduces_travel():
    strokes = [
        Stroke([(0, 0), (1, 0)]),
        Stroke([(50, 0), (51, 0)]),
        Stroke([(2, 0), (3, 0)]),     # 在 0 与 50 之间
    ]
    before = total_travel(strokes)
    ordered = optimize_order(strokes)
    after = total_travel(ordered)
    assert after < before


def test_optimize_allows_reverse():
    strokes = [Stroke([(10, 0), (0, 0)]), Stroke([(11, 0), (21, 0)])]
    ordered = optimize_order(strokes, start=(0, 0), allow_reverse=True)
    # 应从 (0,0) 附近开始，省去折返
    assert ordered[0].points in ([(0, 0), (10, 0)], [(10, 0), (0, 0)])
    assert total_travel(ordered, start=(0, 0)) < total_travel(strokes, start=(0, 0))


def test_optimize_merges_touching():
    a = Stroke([(0, 0), (5, 0)])
    b = Stroke([(5.001, 0), (10, 0)])   # 与 a 相接
    merged = optimize_order([a, b], tolerance=0.1)
    assert len(merged) == 1
    assert draw_length(merged) == pytest.approx(10.0, abs=0.01)


def test_optimize_preserves_total_length():
    strokes = [Stroke([(0, 0), (3, 4)]), Stroke([(10, 0), (13, 0)])]
    l0 = draw_length(strokes)
    l1 = draw_length(optimize_order(strokes))
    assert l1 == pytest.approx(l0)


def test_optimize_empty():
    assert optimize_order([]) == []


# ===================================================== 阅读顺序书写
def _char_groups():
    """两行字，每字一组（组内两条笔画），第一行在上。"""
    strokes, groups = [], []
    gid = 0
    for y in (20.0, 10.0):                 # 第一行在上
        for x in (0.0, 10.0, 20.0):
            strokes.append(Stroke([(x, y), (x + 2, y)], False, "glyph", gid))
            strokes.append(Stroke([(x, y + 2), (x + 2, y + 2)], False, "glyph", gid))
            groups.extend([gid, gid])
            gid += 1
    return strokes, groups


def test_reading_order_writes_char_by_char():
    """阅读顺序：写完一组再写下一组，组内不重排（逐字阅读顺序）。"""
    from writerstudio.machine.path_optimizer import order_reading
    strokes, groups = _char_groups()
    # 打乱输入（先给第二行的组做输入顺序乱序的情况）：把第二行组挪到前面
    shuffled = strokes[6:] + strokes[:6]
    shuffled_groups = groups[6:] + groups[:6]
    out = order_reading(shuffled, shuffled_groups)
    # 输出应按位置重排：第一行（组 0/1/2）在前，第二行（组 3/4/5）在后
    out_groups = [s.group for s in out]
    assert out_groups == sorted(out_groups)          # 组号即阅读顺序
    # 每个组内笔画连续（写完一组再写下一组，不交错）
    seen = []
    for g in out_groups:
        if not seen or seen[-1] != g:
            seen.append(g)
    assert len(seen) == len(set(seen))               # 每组只出现一段


def test_reading_order_keeps_group_on_merge():
    """组内相接笔画合并后仍保留分组号（否则排序会丢归属）。"""
    from writerstudio.machine.path_optimizer import order_reading
    strokes = [Stroke([(0, 0), (5, 0)], False, "glyph", 0),
               Stroke([(5.001, 0), (10, 0)], False, "glyph", 0)]
    out = order_reading(strokes, [0, 0])
    assert len(out) == 1
    assert out[0].group == 0


def test_order_for_writing_falls_back_without_groups():
    """无分组信息时回退到最短空程（图形对象），不报错。"""
    from writerstudio.machine.path_optimizer import order_for_writing
    strokes = [Stroke([(0, 0), (1, 0)]), Stroke([(50, 0), (51, 0)]),
               Stroke([(2, 0), (3, 0)])]
    out = order_for_writing(strokes, groups=None, mode="reading")
    assert len(out) == 3


def test_generate_gcode_reading_order_end_to_end():
    """端到端：文档笔画带分组时，G-code 按阅读顺序书写（不全局乱跳）。"""
    from writerstudio.machine.gcode_gen import generate_from_document
    doc = Document()
    strokes, groups = _char_groups()
    # 拆成三个对象（每个对象即一组，模拟独立的文本块/图形）
    doc.add(make_static_object(strokes[:4], name="第一字"))
    doc.add(make_static_object(strokes[4:8], name="第二字"))
    doc.add(make_static_object(strokes[8:], name="下行"))
    r = generate_from_document(doc, GCodeConfig(order_mode="reading"))
    ys = [float(l.split("Y")[1].split(" ")[0])
          for l in r.lines if l.startswith(("G0 X", "G1 X"))]
    assert ys[0] == 190.0                # 文档 y=20 → 机器 y = 210−20
    assert ys.index(200.0) > 3           # 文档 y=10 的行在较后才能出现


def test_order_mode_roundtrip():
    c = GCodeConfig(order_mode="shortest")
    assert GCodeConfig.from_data(c.to_data()).order_mode == "shortest"
    assert GCodeConfig().order_mode == "reading"     # 默认阅读顺序


# ===================================================== GRBL 协议
def test_parse_status_mpos():
    st = grbl.parse_status("<Idle|MPos:1.000,2.500,0.000|FS:0,0>")
    assert st.state == "Idle"
    assert st.mpos == (1.0, 2.5, 0.0)
    assert st.is_idle


def test_parse_status_wpos_and_fs():
    st = grbl.parse_status("<Run|WPos:10.0,20.0,0.0|FS:500,1000>")
    assert st.state == "Run"
    assert st.wpos == (10.0, 20.0, 0.0)
    assert st.feed == 500.0
    assert st.speed == 1000.0
    assert st.is_running


def test_parse_status_alarm_state():
    st = grbl.parse_status("<Alarm|MPos:0,0,0>")
    assert st.is_alarm


def test_parse_status_invalid():
    assert grbl.parse_status("ok") is None
    assert grbl.parse_status("not a status") is None


def test_classify_lines():
    assert grbl.classify("ok") is grbl.ResponseKind.OK
    assert grbl.classify("error:9") is grbl.ResponseKind.ERROR
    assert grbl.classify("ALARM:1") is grbl.ResponseKind.ALARM
    assert grbl.classify("<Idle|MPos:0,0,0>") is grbl.ResponseKind.STATUS
    assert grbl.classify("$100=80.000") is grbl.ResponseKind.SETTING
    assert grbl.classify("Grbl 1.1h ['$' for help]") is grbl.ResponseKind.VERSION


def test_error_and_alarm_messages():
    assert "9" in grbl.error_message("error:9")
    assert "锁定" in grbl.error_message("error:9")
    assert "1" in grbl.alarm_message("ALARM:1")
    assert grbl.error_message("error:999").startswith("错误 999")


def test_jog_command():
    # 新版固件：$J= + G21 单位（版本感知策略）
    assert grbl.jog_command(10, -5, feed=800) == "$J=G21G91 X10 Y-5 F800"
    assert grbl.jog_command(0, 0, 1, feed=500) == "$J=G21G91 Z1 F500"
    assert grbl.jog_command(1, 0, absolute=True).startswith("$J=G21G90")
    # 旧版固件（<1.1）：退化为普通相对运动
    assert grbl.jog_command(10, feed=800, grbl_version=0.9) == "G21G91 X10 F800"


def test_parse_settings():
    d = grbl.parse_settings(["$0=10", "$100=80.000", "$130=300.000", "ok"])
    assert d[0] == "10"
    assert d[100] == "80.000"
    assert 130 in d


# ===================================================== 串口流控（回环）
def _wait(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_link_connect_and_ok_ack():
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    assert link.connected
    link.send_line("G21")
    assert _wait(lambda: "G21" in fake.out_lines)
    link.disconnect()
    assert fake.closed


def test_link_connect_failure_resets_state():
    """打开串口失败（占用/权限）后状态必须回到「未连接」，不能卡在连接中。"""
    link = GrblLink()
    with pytest.raises(Exception):
        link.connect("/dev/writerstudio-nonexistent-port")
    assert link.state is State.DISCONNECTED
    assert not link.connected


def test_link_progress_before_connect():
    """连接前调用 progress() 不应炸（计数器在 __init__ 就位）。"""
    link = GrblLink()
    assert link.progress() == (0, 0)
    assert link.in_flight_bytes() == 0


def test_link_truncated_status_recovers():
    """状态串缺 '>' 时残片作废，后续正常行不丢失。"""
    fake = FakeSerial()
    link = GrblLink()
    seen = []
    link.set_event_handler(lambda e: seen.append(e.kind) if e.kind == "ok" else None)
    link.connect_backend(fake)
    fake.feed("<Idle|MPos:0.000,0.000,0.000")   # 无 '>'，残缺
    fake.feed("\n")
    link.send_line("G21")
    assert _wait(lambda: "ok" in seen, timeout=3.0)
    link.disconnect()


def test_link_flow_control_respects_buffer():
    """字符计数流控：在途字节数不应超过 RX_BUFFER_SIZE(+正在写的一行)。"""
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    lines = [f"G1 X{i} Y0 F1500" for i in range(200)]
    link.load_job(lines)
    # 首轮 pump 后，在途应 <= 缓冲上限
    assert link.in_flight_bytes() <= 128 + 32
    # 等待全部完成
    assert _wait(lambda: link.progress()[0] >= len(lines), timeout=5.0)
    link.disconnect()


def test_link_job_completes_and_acks_all():
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    lines = ["G0 X0 Y0", "G1 X10 Y0 F1500", "G0 X0 Y0"]
    link.load_job(lines)
    assert _wait(lambda: link.progress() == (3, 3), timeout=3.0)
    assert link.state is State.IDLE
    link.disconnect()


def test_link_job_clears_stale_g92_offset():
    """发作业前自动清残留工件偏移。

    残留的工件偏移会让烘焙绝对坐标的作业整体错位，Z 偏移还会
    放大落笔深度冲击舵机。G92 用 ``G92.1``；部分固件把偏移持久化
    在 EEPROM 的 G54 里（WCO 非零），须用 ``G10 L2 P1`` 清零。
    """
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job(["G1 X1 Y0 F1500"])
    assert _wait(lambda: link.progress() == (1, 1), timeout=3.0)
    assert "G10 L2 P1 X0 Y0 Z0" in fake.out_lines
    assert "G92.1" in fake.out_lines
    # 两条清理都必须先于作业任务行
    i_job = fake.out_lines.index("G1 X1 Y0 F1500")
    assert fake.out_lines.index("G10 L2 P1 X0 Y0 Z0") < i_job
    assert fake.out_lines.index("G92.1") < i_job
    link.disconnect()


def test_link_job_clears_manual_g92_origin():
    """本会话手工设过 G92 的，作业发送前也必须清掉。

    作业烘焙**绝对机械坐标**，要求工件坐标 ≡ 机械坐标；手工 G92 会让
    每个绝对坐标整体错位，而校准读的是 MPos（不受 G92 影响），重校也
    救不回——只能无条件清理（回归：旧版「尊重手工零点」会让整版错位）。
    """
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.set_origin("XY")
    assert _wait(lambda: "G92 XY0" in fake.out_lines)
    link.load_job(["G1 X1 Y0 F1500"])
    assert _wait(lambda: link.progress() == (1, 1), timeout=3.0)
    i_job = fake.out_lines.index("G1 X1 Y0 F1500")
    assert fake.out_lines.index("G10 L2 P1 X0 Y0 Z0") < i_job
    assert fake.out_lines.index("G92.1") < i_job
    link.disconnect()


def test_link_syncs_external_hold_state():
    """控制器在软件之外进入 Hold 时必须可见（否则界面误报空闲、作业不执行）。"""
    fake = FakeSerial()
    fake.state = "Hold:0"                   # 轮询也会回报同一状态
    link = GrblLink()
    link.connect_backend(fake)
    fake.feed("<Hold:0|MPos:0.000,0.000,0.000|FS:0,0|Pn:XY>\n")
    assert _wait(lambda: link.state is State.PAUSED, timeout=2.0)
    # 恢复：发 ~ 后控制器回报 Idle → 状态回到空闲
    link.resume()
    fake.state = "Idle"
    assert _wait(lambda: link.state is State.IDLE, timeout=2.0)
    link.disconnect()


def test_link_syncs_external_alarm_state():
    fake = FakeSerial()
    fake.state = "Alarm"
    link = GrblLink()
    link.connect_backend(fake)
    fake.feed("<Alarm|MPos:0.000,0.000,0.000>\n")
    assert _wait(lambda: link.state is State.ALARM, timeout=2.0)
    link.disconnect()


def test_link_ignores_transient_idle_during_job():
    """作业进行中的瞬时 Idle 不得把 RUNNING 打回空闲。"""
    fake = FakeSerial(simulate_full=True)   # 不发 job 行，保持「进行中」
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job(["G1 X1 Y0 F1500"])
    assert link.state is State.RUNNING
    fake.feed("<Idle|MPos:0.000,0.000,0.000>\n")
    time.sleep(0.2)
    assert link.state is State.RUNNING
    link.disconnect()


def test_link_abort_resumes_offset_clearing():
    """软复位后固件从 EEPROM 还原 G92——中止后的下一次作业要重新清理。"""
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.set_origin("XY")
    assert _wait(lambda: "G92 XY0" in fake.out_lines)
    link.abort()
    link.load_job(["G1 X1 Y0 F1500"])
    assert _wait(lambda: link.progress() == (1, 1), timeout=3.0)
    assert "G92.1" in fake.out_lines
    link.disconnect()


def test_link_reports_error_but_continues():
    fake = FakeSerial(responses={"G1 X1 Y1": "error:9"})
    events = []
    link = GrblLink(on_event=lambda e: events.append(e))
    link.connect_backend(fake)
    link.load_job(["G1 X1 Y1", "G1 X2 Y2"])
    assert _wait(lambda: link.progress()[0] >= 2, timeout=3.0)
    errors = [e for e in events if e.kind == "error"]
    assert errors and "9" in errors[0].text
    link.disconnect()


def test_link_alarm_sets_state():
    fake = FakeSerial(responses={"G0 X0 Y0": "ALARM:1"})
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job(["G0 X0 Y0"])
    assert _wait(lambda: link.state is State.ALARM, timeout=3.0)
    link.disconnect()


def test_link_pause_and_resume():
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job([f"G1 X{i} Y0" for i in range(50)])
    link.pause()
    assert link.state is State.PAUSED
    # 暂停期间 ! 实时字符已发出（不进入 out_lines）
    link.resume()
    assert link.state is State.RUNNING
    assert _wait(lambda: link.progress()[0] >= 50, timeout=5.0)
    link.disconnect()


def test_link_abort_clears_queue():
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job([f"G1 X{i} Y0" for i in range(100)])
    link.abort()
    assert link.state is State.IDLE
    link.disconnect()


def test_link_status_poll_from_fake():
    fake = FakeSerial(initial_pos=(12.0, 34.0, 0.0))
    statuses = []
    link = GrblLink(on_event=lambda e: statuses.append(e) if e.kind == "status" else None)
    link.connect_backend(fake)
    link.request_status()
    assert _wait(lambda: len(statuses) > 0, timeout=2.0)
    assert link.status.mpos[0] == pytest.approx(12.0)
    link.disconnect()


def test_link_jog_and_home_sent():
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.jog(10, 0, feed=1000)
    link.home()
    link.set_origin("XY")
    assert _wait(lambda: any(l.startswith("$J") for l in fake.out_lines))
    assert any(l == "$H" for l in fake.out_lines)
    assert any(l == "G92 XY0" for l in fake.out_lines)
    link.disconnect()


def test_link_goto_clears_work_offset_first():
    """手动绝对移动（移动笔到标记起点）前也要清工件偏移。

    固件 G54 残留偏移会让 G0 绝对坐标整体错位——笔被移动到错误的物理点位
    （WCO 非零时尤甚，正是「移动到错误点位」类故障的常见根因）。
    """
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.goto(100.0, 80.0, feed=2000)
    assert _wait(lambda: any(l.startswith("G0 G90 X100 Y80") for l in fake.out_lines))
    assert "G10 L2 P1 X0 Y0 Z0" in fake.out_lines
    assert "G92.1" in fake.out_lines
    # 清零必须先于移动
    i_move = next(i for i, l in enumerate(fake.out_lines) if l.startswith("G0"))
    assert fake.out_lines.index("G10 L2 P1 X0 Y0 Z0") < i_move
    link.disconnect()


def test_link_goto_clears_manual_origin():
    """手工 G92 后的绝对移动也要先清偏移（理由同作业：绝对坐标 =
    机械坐标，偏移只会把笔送到错误点位，且校准无法察觉）。"""
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.set_origin("XY")
    assert _wait(lambda: "G92 XY0" in fake.out_lines)
    link.goto(50.0, 40.0)
    assert _wait(lambda: any(l.startswith("G0") for l in fake.out_lines))
    assert "G10 L2 P1 X0 Y0 Z0" in fake.out_lines
    assert "G92.1" in fake.out_lines
    i_move = next(i for i, l in enumerate(fake.out_lines)
                  if l.startswith("G0"))
    assert fake.out_lines.index("G92.1") < i_move
    link.disconnect()


def test_link_disconnected_state():
    link = GrblLink()
    assert link.state is State.DISCONNECTED
    assert not link.connected
    with pytest.raises(ConnectionError):
        link.send_line("G21")


# ---------------------------------------------------------------------------
# 扩展能力（ESP32 固件扩展响应、单步、收尾等）
# ---------------------------------------------------------------------------
def test_parse_version():
    assert grbl.parse_version("Grbl 1.1h ['$' for help]") == pytest.approx(1.1)
    assert grbl.parse_version("Grbl 0.9j ['$' for help]") == pytest.approx(0.9)
    assert grbl.parse_version("ok") is None


def test_parse_version_esp32_bracket_format():
    """ESP32 定制固件的 [VER:] 版本行。"""
    line = "[VER:1.1.202220207:]"
    assert grbl.parse_version(line) == pytest.approx(1.1)
    assert grbl.classify(line) is grbl.ResponseKind.VERSION
    assert grbl.parse_version("[OPT:PHBS]") is None


def test_parse_firmware_info():
    info = grbl.parse_firmware_info("[VER:1.1.202220207:]")
    assert info.get("version") == "1.1.202220207:"
    assert grbl.parse_firmware_info("[OPT:PHBS]").get("options") == "PHBS"
    assert grbl.parse_firmware_info(
        "[MSG:Using machine:TestWriter]").get("machine") == "TestWriter"
    assert grbl.parse_firmware_info("[MSG:Mode=BT:Name=x]").get("message") \
        == "Mode=BT:Name=x"
    assert grbl.parse_firmware_info("ok") == {}


def test_link_detects_esp32_version():
    """连接后收到 [VER:] 行应记录 grbl_version（供版本感知 jog 与 UI 显示）。"""
    fake = FakeSerial(responses={"$I": "[VER:1.1.202220207:]"})
    link = GrblLink()
    versions = []
    link.set_event_handler(
        lambda e: versions.append(e.text) if e.kind == "version" else None)
    link.connect_backend(fake)
    assert _wait(lambda: link.grbl_version is not None, timeout=3.0)
    assert link.grbl_version == pytest.approx(1.1)
    assert versions and versions[0] == "[VER:1.1.202220207:]"
    link.disconnect()


def test_legacy_status_format():
    """旧版逗号格式状态串（新旧两种格式都兼容）。"""
    st = grbl.parse_status("<Idle,MPos:1.500,2.500,0.000,WPos:0.000,0.000,0.000>")
    assert st is not None
    assert st.state == "Idle"
    assert st.mpos == pytest.approx((1.5, 2.5, 0.0))


def test_esp_response_classify():
    assert grbl.classify("[ESP410]ok") is grbl.ResponseKind.ESP
    assert grbl.parse_esp_response("[ESP410]some,payload") == (410, "some,payload")


def test_new_commands():
    assert grbl.sleep_command() == "$SLP"
    assert grbl.build_info_command() == "$I"
    assert grbl.return_to_start_command() == "G90 G0 X0 Y0 F3000"
    assert grbl.set_origin_offset_command(10, 20) == "G92 X10 Y20 Z0"
    assert grbl.dwell_command(0.2) == "G4 P0.2"


def test_parse_settings_with_comments():
    d = grbl.parse_settings_comments(["$110=18000.000 (x rate, mm/min)", "ok"])
    assert d[110] == ("18000.000", "x rate, mm/min")


def test_link_version_capture_and_jog():
    fake = FakeSerial()
    versions = []
    link = GrblLink(on_event=lambda e: versions.append(e) if e.kind == "version" else None)
    link.connect_backend(fake)
    fake.feed("Grbl 1.1h ['$' for help]\n")
    assert _wait(lambda: link.grbl_version is not None)
    link.jog(5, 0, feed=900)
    assert _wait(lambda: any(l.startswith("$J=G21G91") for l in fake.out_lines))
    link.disconnect()


def test_link_single_step_mode():
    fake = FakeSerial(simulate_full=True)   # 不自动回 ok：手动逐条确认
    link = GrblLink()
    link.connect_backend(fake)
    link.single_step = True
    lines = [f"G1 X{i}" for i in range(10)]
    link.load_job_lines(lines)
    time.sleep(0.15)

    def n_job():
        return len([l for l in fake.out_lines if l.startswith("G1 X")])

    # 单步：清理行逐个发送，作业行在清理行全部确认前不得发出
    assert len([l for l in fake.out_lines if l.startswith("G10 L2")]) == 1
    assert n_job() == 0
    fake.feed("ok\n"); time.sleep(0.15)     # 确认 G10 → 发 G92.1
    assert n_job() == 0
    fake.feed("ok\n"); time.sleep(0.15)     # 确认 G92.1 → 发第 1 条作业行
    assert n_job() == 1
    fake.feed("ok\n"); time.sleep(0.15)     # 每确认一条才补发下一条
    assert n_job() == 2
    link.disconnect()


def test_link_pause_injects_pen_up():
    from writerstudio.machine.config import GCodeConfig, PEN_SERVO_ANGLE
    cfg = GCodeConfig(pen_mode=PEN_SERVO_ANGLE, pen_up_s=50, pen_down_angle=10)
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job_lines([f"G1 X{i}" for i in range(50)])
    assert _wait(lambda: len(fake.out_lines) >= 3)
    link.pause(pen_cfg=cfg, use_hold=False)
    # 抬笔指令（M3 S50）应被注入并在缓冲允许后发出
    assert _wait(lambda: any(l == "M3 S50" for l in fake.out_lines), timeout=2.0)
    n_at_pause = len(fake.out_lines)
    time.sleep(0.15)
    # 暂停后作业行不再推进（只多出注入行）
    assert len(fake.out_lines) <= n_at_pause + 1
    link.resume()
    assert _wait(lambda: any(l == "M3 S10" for l in fake.out_lines), timeout=2.0)
    link.disconnect()


def test_link_abort_pens_up_and_returns():
    cfg = GCodeConfig(pen_mode=PEN_M3M5, pen_down_s=1000)
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job_lines([f"G1 X{i}" for i in range(50)])
    assert _wait(lambda: len(fake.out_lines) >= 3)
    link.abort(pen_cfg=cfg, return_start=True)
    assert _wait(lambda: any(l == "M5" for l in fake.out_lines), timeout=2.0)
    assert _wait(lambda: any(l.startswith("G90 G0 X0 Y0") for l in fake.out_lines),
                 timeout=2.0)
    link.disconnect()


def test_link_abort_returns_to_job_home():
    """「回起点」应回作业实际书写起点（首个 G0 定位点），而非机器零点。"""
    cfg = GCodeConfig(pen_mode=PEN_M3M5)
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.load_job_lines(["G0 X30 Y20"] + [f"G1 X{i}" for i in range(50)])
    assert _wait(lambda: len(fake.out_lines) >= 3)
    link.abort(pen_cfg=cfg, return_start=True)
    assert _wait(lambda: any(l == "G90 G0 X30 Y20 F3000" for l in fake.out_lines),
                 timeout=2.0)
    link.disconnect()


def test_link_pause_when_idle_is_noop():
    """空闲时误点暂停/继续：不能注入落笔（激光等于点亮）也不能进 PAUSED。"""
    from writerstudio.machine.config import GCodeConfig, PEN_LASER
    cfg = GCodeConfig(pen_mode=PEN_LASER, laser_power=900)
    fake = FakeSerial()
    link = GrblLink()
    link.connect_backend(fake)
    link.pause(pen_cfg=cfg, use_hold=False)
    link.resume()
    time.sleep(0.1)
    assert link.state is not State.PAUSED
    assert not any("M3" in l or "M5" in l for l in fake.out_lines)
    link.disconnect()


def test_link_job_done_event():
    fake = FakeSerial()
    done = []
    link = GrblLink(on_event=lambda e: done.append(1) if e.kind == "job_done" else None)
    link.connect_backend(fake)
    link.load_job_lines(["G0 X0", "G1 X10", "G1 X0"])
    assert _wait(lambda: done, timeout=5.0)
    link.disconnect()


def test_link_job_done_waits_for_motion():
    """全部 ack ≠ 机械停稳：状态仍是 Run 时不得宣布完成，回 Idle 后才发。"""
    fake = FakeSerial()
    fake.state = "Run"            # 模拟机械还在走
    done = []
    link = GrblLink(on_event=lambda e: done.append(1) if e.kind == "job_done" else None)
    link.connect_backend(fake)
    link.load_job_lines(["G1 X10", "G1 X0"])
    assert _wait(lambda: link.progress() == (2, 2), timeout=5.0)
    time.sleep(0.8)               # ack 完了，状态还在 Run
    assert not done
    fake.state = "Idle"           # 机械停下 → 完成事件到来
    assert _wait(lambda: done, timeout=5.0)
    link.disconnect()


def test_gcode_pen_modes_and_delays():
    from writerstudio.machine.gcode_gen import generate_gcode
    from writerstudio.machine.config import PEN_SERVO_ANGLE, PEN_LASER
    stroke = Stroke([(0, 0), (10, 0)])

    cfg = GCodeConfig(pen_mode=PEN_SERVO_ANGLE, pen_up_s=50, pen_down_angle=10,
                      pen_on_delay=0.2, pen_off_delay=0.1,
                      page_size=(100.0, 80.0))
    r = generate_gcode([stroke], cfg, optimize=False)
    assert "M3 S50" in r.lines          # 抬笔 = 角度 50
    assert "M3 S10" in r.lines          # 落笔 = 角度 10
    assert "G4 P0.2" in r.lines         # 落笔后等待
    assert "G4 P0.1" in r.lines         # 抬笔前等待

    cfg = GCodeConfig(pen_mode=PEN_LASER, laser_power=800,
                      page_size=(100.0, 80.0))
    r = generate_gcode([stroke], cfg, optimize=False)
    assert "M3 S800" in r.lines
    assert "M5" in r.lines


def test_gcode_custom_start_end_and_return():
    from writerstudio.machine.gcode_gen import generate_gcode
    stroke = Stroke([(0, 0), (10, 0)])
    cfg = GCodeConfig(custom_start_gcode="M3 S0;G4 P0.5",
                      custom_end_gcode="M5",
                      return_to_start=True, page_size=(100.0, 80.0))
    r = generate_gcode([stroke], cfg, optimize=False)
    assert "M3 S0" in r.lines and "G4 P0.5" in r.lines
    idx_draw = next(i for i, ln in enumerate(r.lines) if ln.startswith("G1 X10"))
    assert r.lines.index("M3 S0") < idx_draw
    assert "M5" in r.lines
    assert "G90 G0 X0 Y80" in r.lines   # 回到首笔画机器起点 (0, 80)


def test_gcode_end_returns_to_park_point():
    """start_point 是写之前的停笔位（文档坐标经固定映射），完成后回到那里。"""
    from writerstudio.machine.gcode_gen import generate_gcode
    stroke = Stroke([(0, 0), (10, 0)])
    cfg = GCodeConfig(page_size=(100.0, 80.0), return_to_start=True)
    r = generate_gcode([stroke], cfg, optimize=False, start_point=(30.0, 40.0))
    assert "G0 X30 Y40" in r.lines            # 先移动到停笔位
    assert "G90 G0 X30 Y40" in r.lines        # 完成后回停笔位

    cfg = GCodeConfig(page_size=(100.0, 80.0), return_to_start=False)
    r = generate_gcode([stroke], cfg, optimize=False)
    assert "G90 G0 X30 Y40" not in r.lines


def test_gcode_simplify_reduces_points():
    """抽稀：共线密集点合并、首尾保留、0=关闭。"""
    from writerstudio.machine.gcode_gen import generate_gcode
    pts = [(i * 0.1, (0.01 if i % 2 else -0.01)) for i in range(100)]
    pts[-1] = (10.0, 5.0)
    stroke = Stroke(pts)
    ps = {"page_size": (100.0, 80.0)}
    r_off = generate_gcode([stroke], GCodeConfig(simplify_mm=0.0, **ps),
                           optimize=False)
    r_on = generate_gcode([stroke], GCodeConfig(simplify_mm=0.5, **ps),
                          optimize=False)
    assert r_on.point_count < r_off.point_count
    # 首尾点必须保留
    assert any("G0 X0" in ln for ln in r_on.lines)
    assert any(ln.startswith("G1 X10 Y75") for ln in r_on.lines)


# ===================================================== 页面旋转的等价轴映射
def test_with_page_rotated_zero_turns_is_identity():
    cfg = GCodeConfig(origin_corner="left-top", swap_xy=True,
                      page_size=(297.0, 210.0))
    new = cfg.with_page_rotated(0, (297.0, 210.0), (297.0, 210.0))
    assert new.origin_corner == "left-top"
    assert new.swap_xy and not new.invert_x and not new.invert_y


def test_with_page_rotated_equivalent_mapping():
    """旋转后映射必须与原映射描述同一物理注册：任意同一点机器坐标不变。"""
    old_size = (297.0, 210.0)
    new_size = (210.0, 297.0)
    cfg = GCodeConfig(origin_corner="left-top", page_size=old_size)
    new = cfg.with_page_rotated(1, old_size, new_size)
    assert (new.origin_corner, new.swap_xy, new.invert_x, new.invert_y) == \
        ("right-top", True, True, False)
    to_old = lambda x, y: (new_size[1] - y, x)   # 新页面坐标 → 旧页面坐标
    for x, y in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (155.5, 88.25)):
        ax, ay = new.map_axis(x, y)
        bx, by = cfg.map_axis(*to_old(x, y))
        assert abs(ax - bx) < 1e-9 and abs(ay - by) < 1e-9


def test_with_page_rotated_round_trip_restores_state():
    """顺时针转 90° 再逆时针转回来，轴映射应还原（逐点映射等价）。

    注：none 与 left-bottom 在 map_axis 中等价（都不做角落平移），往返后
    落到等价类内的哪个代表均可，但映射本身必须逐点一致。
    """
    old_size = (297.0, 210.0)
    mid_size = (210.0, 297.0)
    cfg = GCodeConfig(origin_corner="left-bottom", swap_xy=True,
                      invert_y=True, page_size=old_size)
    back = cfg.with_page_rotated(1, old_size, mid_size) \
              .with_page_rotated(3, mid_size, old_size)
    for x, y in ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (150.0, 77.0)):
        ax, ay = cfg.map_axis(x, y)
        bx, by = back.map_axis(x, y)
        assert abs(ax - bx) < 1e-9 and abs(ay - by) < 1e-9
    assert back.swap_xy == cfg.swap_xy
    assert back.invert_x == cfg.invert_x
    assert back.invert_y == cfg.invert_y


# ===================================================== 笔序优化性能重构的等价性
def _bruteforce_optimize(strokes, start=None, allow_reverse=True,
                         tolerance=0.2):
    """重构前的逐对扫描参考实现（语义基准，用于对拍）。"""
    from writerstudio.machine.path_optimizer import _merge_touching
    items = [s.clone() for s in strokes if len(s.points) >= 2]
    if not items:
        return []
    merged = _merge_touching(items, tolerance)
    cur = start if start is not None else merged[0].points[0]
    remaining = list(merged)
    ordered = []
    while remaining:
        best_i, best_d, best_rev = -1, math.inf, False
        for i, s in enumerate(remaining):
            d_start = ((cur[0] - s.points[0][0]) ** 2
                       + (cur[1] - s.points[0][1]) ** 2)
            if d_start < best_d:
                best_d, best_i, best_rev = d_start, i, False
            if allow_reverse:
                d_end = ((cur[0] - s.points[-1][0]) ** 2
                         + (cur[1] - s.points[-1][1]) ** 2)
                if d_end < best_d:
                    best_d, best_i, best_rev = d_end, i, True
        if best_i < 0:
            break
        s = remaining.pop(best_i)
        if best_rev:
            s = Stroke(list(reversed(s.points)), s.closed, s.role, s.group)
        ordered.append(s)
        cur = s.points[-1]
    return ordered


def _random_layout(rng, n):
    """随机散布的 2~3 点折线（含少量共点/相接，制造并列与合并机会）。"""
    out = []
    for _ in range(n):
        x = rng.randrange(0, 60)
        y = rng.randrange(0, 60)
        pts = [(x, y), (x + rng.randrange(1, 5), y + rng.randrange(-2, 3))]
        if rng.random() < 0.5:
            pts.append((pts[-1][0] + rng.randrange(1, 4),
                        pts[-1][1] + rng.randrange(-2, 3)))
        out.append(Stroke(pts))
    return out


def test_optimize_order_matches_bruteforce():
    """网格加速的最近邻与逐对扫描在随机布局上输出完全一致。"""
    rng = random.Random(20260913)
    for trial in range(30):
        strokes = _random_layout(rng, rng.randrange(2, 60))
        start = (rng.randrange(0, 60), rng.randrange(0, 60))
        allow_rev = trial % 2 == 0
        got = optimize_order(strokes, start=start, allow_reverse=allow_rev)
        want = _bruteforce_optimize(strokes, start=start,
                                    allow_reverse=allow_rev)
        assert [(s.points, s.closed) for s in got] == \
               [(s.points, s.closed) for s in want], f"trial={trial}"


def test_merge_touching_matches_bruteforce():
    """网格加速的相接合并与逐个扫描版输出完全一致。"""
    from writerstudio.machine.path_optimizer import _merge_touching

    def bruteforce_merge(slist, tolerance):
        tol2 = tolerance * tolerance
        open_strokes = [s for s in slist if not s.closed]
        closed = [s for s in slist if s.closed]
        used = [False] * len(open_strokes)
        result = []
        for i, s in enumerate(open_strokes):
            if used[i]:
                continue
            used[i] = True
            chain = list(s.points)
            extended = True
            while extended:
                extended = False
                for j, t in enumerate(open_strokes):
                    if used[j]:
                        continue
                    if (chain[-1][0] - t.points[0][0]) ** 2 + \
                            (chain[-1][1] - t.points[0][1]) ** 2 <= tol2:
                        chain.extend(t.points[1:])
                        used[j] = True
                        extended = True
                        break
                    if (chain[-1][0] - t.points[-1][0]) ** 2 + \
                            (chain[-1][1] - t.points[-1][1]) ** 2 <= tol2:
                        chain.extend(list(reversed(t.points))[1:])
                        used[j] = True
                        extended = True
                        break
            result.append(Stroke(chain, False, s.role, s.group))
        result.extend(closed)
        return result

    rng = random.Random(42)
    for trial in range(30):
        slist = _random_layout(rng, rng.randrange(2, 50))
        got = _merge_touching([s.clone() for s in slist], 0.3)
        want = bruteforce_merge([s.clone() for s in slist], 0.3)
        assert [(s.points, s.closed) for s in got] == \
               [(s.points, s.closed) for s in want], f"trial={trial}"


def test_optimize_order_large_input_fast():
    """千笔级自由手绘排笔序应在几十毫秒级（原 O(n²) 要数秒，界面冻结）。"""
    rng = random.Random(7)
    strokes = _random_layout(rng, 3000)
    t0 = time.perf_counter()
    out = optimize_order(strokes, start=(30, 30))
    dt = time.perf_counter() - t0
    # 相接笔画会被合并，输出条数以逐对扫描参考实现为准
    want = _bruteforce_optimize(strokes, start=(30, 30))
    assert len(out) == len(want)
    assert dt < 0.5, f"optimize_order 3000 笔耗时 {dt*1e3:.0f}ms"
