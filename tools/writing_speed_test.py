#!/usr/bin/env python3
"""书写参数对比测试：多组速度/抽稀参数，在同一页纵向各写一行同一段文字。

从上到下每一行对应一组参数（顺序即下表），写完后对比哪一行线条最干净、
最不像「机械抖动」，即可确定最佳设置。

用法::

    python tools/writing_speed_test.py                  # 只预览（不发送）
    python tools/writing_speed_test.py --send --port /dev/ttyUSB0
    python tools/writing_speed_test.py --set simplify   # 换「抽稀」对比轮

坐标以「笔当前所在位置」为原点（机器无回零，开串口复位后 MPos 即 0,0）：
请先把笔移到纸上想开始的位置，再运行 --send。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from writerstudio.core.document import Document            # noqa: E402
from writerstudio.core.geometry import AffineTransform     # noqa: E402
from writerstudio.fonts.builder import TextSpec, make_text_object  # noqa: E402
from writerstudio.fonts.manager import FontManager         # noqa: E402
from writerstudio.machine.config import GCodeConfig        # noqa: E402
from writerstudio.machine.gcode_gen import generate_from_document  # noqa: E402

# 测试文字与排版（取自用户项目的实际参数）
TEXT = "实验仪器设备说明"
FONTS = ["手写真迹", "手写符号A-1", "手写符号B-1", "手写符号C-1"]
SIZE = 6.5
LINE_GAP = 15.0          # 每行间距(mm)

# 第一轮：以「速度」为主变量（抽稀取温和值）
SPEED_SET = [
    ("① 速度1500  抽稀0.10  平滑0.20", dict(feed=1500, simplify=0.10, smoothing=0.20)),
    ("② 速度2000  抽稀0.10  平滑0.20", dict(feed=2000, simplify=0.10, smoothing=0.20)),
    ("③ 速度2500  抽稀0.15  平滑0.25", dict(feed=2500, simplify=0.15, smoothing=0.25)),
    ("④ 速度3000  抽稀0.15  平滑0.25", dict(feed=3000, simplify=0.15, smoothing=0.25)),
]
# 第二轮：以「抽稀」为主变量（速度取第一轮较优的 2000）
SIMPLIFY_SET = [
    ("① 速度2000  抽稀0.00", dict(feed=2000, simplify=0.00, smoothing=0.20)),
    ("② 速度2000  抽稀0.10", dict(feed=2000, simplify=0.10, smoothing=0.20)),
    ("③ 速度2000  抽稀0.20", dict(feed=2000, simplify=0.20, smoothing=0.20)),
    ("④ 速度2000  抽稀0.30", dict(feed=2000, simplify=0.30, smoothing=0.20)),
]
SETS = {"speed": SPEED_SET, "simplify": SIMPLIFY_SET}


def build_program(variants) -> tuple[list[str], list[str]]:
    """生成 Program 行；返回 (行, 每组说明)。

    坐标约定：**以笔当前所在位置为机器原点 (0,0)**（机器无回零，开串口复位后
    MPos 即 0,0），内容整体平移到第一象限，只朝 +X/+Y 书写，避免命令到
    纸外/不可达的负坐标；不设轴映射、不回起点。
    """
    from writerstudio.perturb.params import PerturbParams

    manager = FontManager()
    missing = [f for f in FONTS if manager.get(f) is None]
    if missing:
        print(f"警告：找不到字体 {missing}（将用可用字体替代）")

    # 1) 逐组生成对象（各自带该组参数），按行距错开
    objects = []          # (label, obj, params)
    for i, (label, p) in enumerate(variants):
        spec = TextSpec(text=TEXT, font_names=FONTS, size=SIZE)
        spec.perturb = PerturbParams.natural(SIZE)
        spec.perturb.smoothing = float(p["smoothing"])
        obj = make_text_object(spec, manager)
        # 行 i 沿 +Y 方向错开（正方向，落在纸内）
        obj.transform = AffineTransform.translate(0.0, i * LINE_GAP)
        objects.append((label, obj, p))

    # 2) 整体平移到第一象限：包围盒左下角 → (0,0)
    from writerstudio.core.geometry import BBox
    box = BBox()
    for _, obj, _ in objects:
        box = box.union(obj.world_bbox())
    shift = AffineTransform.translate(-box.x0, -box.y0)
    for _, obj, _ in objects:
        obj.transform = shift @ obj.transform

    # 3) 逐组生成 G-code（默认配置 = 无轴映射、无起点偏移）
    header = ["G21", "G90", "G94", "G17", "G1 Z0 F3000"]   # 抬笔起始
    lines: list[str] = []
    notes: list[str] = []
    for label, obj, p in objects:
        doc = Document()
        doc.add(obj)
        cfg = GCodeConfig(draw_feed=float(p["feed"]), travel_feed=3000.0,
                          pen_up_z=0.0, pen_down_z=6.0, pen_z_feed=3000.0,
                          simplify_mm=float(p["simplify"]),
                          return_to_start=False)
        cfg.header = []
        cfg.footer = []
        r = generate_from_document(doc, cfg, optimize=True)
        lines.extend(r.lines)
        notes.append(f"{label}  → 插补行 {sum(1 for l in r.lines if l.startswith('G1 X'))}"
                     f"  绘制 {r.draw_length:.0f}mm")
    lines.extend(["G1 Z0 F3000", "M5"])                     # 收尾抬笔
    return header + lines, notes


def bbox_of(lines: list[str]) -> tuple[float, float, float, float]:
    xs, ys = [], []
    for l in lines:
        for tok in l.split():
            if tok.startswith("X"):
                try: xs.append(float(tok[1:]))
                except ValueError: pass
            elif tok.startswith("Y"):
                try: ys.append(float(tok[1:]))
                except ValueError: pass
    if not xs or not ys:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), max(xs), min(ys), max(ys))


def main() -> int:
    ap = argparse.ArgumentParser(description="书写参数对比测试")
    ap.add_argument("--set", choices=sorted(SETS), default="speed",
                    help="对比轮：speed=速度为主 / simplify=抽稀为主")
    ap.add_argument("--send", action="store_true", help="实际发送到机器（默认只预览）")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="串口设备")
    args = ap.parse_args()

    variants = SETS[args.set]
    lines, notes = build_program(variants)
    x0, x1, y0, y1 = bbox_of(lines)
    print(f"=== 参数组（从上到下即第 1~{len(variants)} 行）===")
    for n in notes:
        print("   ", n)
    print(f"=== 程序 {len(lines)} 行  坐标范围 X[{x0:.1f},{x1:.1f}] "
          f"Y[{y0:.1f},{y1:.1f}] ===")

    if not args.send:
        print("\n（预览模式，未发送。加 --send --port /dev/ttyUSB0 实际书写）")
        return 0

    from writerstudio.machine.serial_link import GrblLink, State
    link = GrblLink()
    print(f"\n连接 {args.port} …")
    link.connect(args.port, 115200)
    time.sleep(1.2)
    link.realtime(b"~")          # 若处于 Hold 则恢复
    time.sleep(0.4)
    if link.state is State.ALARM:
        link.unlock()            # 仅报警时 $X 解锁
        time.sleep(0.5)
    # 读一次笔位确认（开串口复位后 MPos 即原点，笔就在 (0,0)）
    ev = []
    link.set_event_handler(lambda e: ev.append(e))
    link.request_status()
    time.sleep(0.7)
    st = [e for e in ev if e.kind == "status"]
    if st and st[-1].status is not None:
        s = st[-1].status
        print(f"笔当前位置 MPos={s.mpos}（作为起点 0,0）状态={s.raw.split('|')[0]}")
    link.set_event_handler(None)
    print("开始书写（请勿断电/拔线）…")
    link.load_job(lines)
    t0 = time.time()
    while time.time() - t0 < 300:
        acked, tot = link.progress()
        if tot and acked >= tot:
            break
        time.sleep(0.3)
    time.sleep(1.0)
    print(f"完成：{link.progress()[0]}/{link.progress()[1]} 行，用时 {time.time()-t0:.1f}s")
    link.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
