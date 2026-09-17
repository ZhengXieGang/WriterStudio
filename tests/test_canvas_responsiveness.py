"""画布交互流畅度回归：后台字体解析与 UI 的 GIL 争抢防护。

背景：启动初期字体面板在后台线程逐款解析字库（纯 Python、全程握 GIL），
默认 5ms 线程切换间隔下，画布的缩放/拖动/选择会成片掉帧（实测移动拖拽
中位 1.2ms → 18ms）。三道防线，各配回归测试：

    1. 解析期间切换间隔压到 1ms，线程结束恢复系统默认；
    2. 画布交互（按下/拖动/滚轮）→ ``suspend_loading`` 暂停解析，
       静置 800ms 自动恢复；
    3. 宽度手柄的重排限流自适应上次重排耗时（大表格不再连续重排打满
       事件循环）。
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ui.canvas import CanvasView  # noqa: E402
from writerstudio.ui.controller import DocumentController  # noqa: E402
from writerstudio.ui.font_panel import FontPanel, _FontInfoWorker  # noqa: E402
from writerstudio.fonts.manager import FontManager  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def view(qapp):
    ctrl = DocumentController()
    v = CanvasView(ctrl)
    v.resize(400, 300)
    yield v


# ---------------------------------------------------------------------------
# 1. 字体解析线程：切换间隔保护 + 暂停标志
# ---------------------------------------------------------------------------
def test_worker_run_keeps_switch_interval(qapp):
    """线程跑完（空任务同步执行）后系统切换间隔必须还原。"""
    before = __import__("sys").getswitchinterval()
    fm = FontManager()
    worker = _FontInfoWorker([], fm)
    worker.run()          # 同步执行（不起线程），names 为空立即返回
    assert __import__("sys").getswitchinterval() == pytest.approx(before)


def test_worker_wait_if_paused_exits_on_stop(qapp):
    """暂停中再收到停止请求时，等待循环必须立即退出（线程可终结）。"""
    fm = FontManager()
    worker = _FontInfoWorker([], fm)
    worker.pause()
    worker.stop()
    t0 = time.perf_counter()
    worker._wait_if_paused()     # 不应阻塞：stop 优先于 paused
    assert time.perf_counter() - t0 < 0.2
    worker.resume()
    assert not worker._paused


def test_suspend_before_start_postpones_load_timer(qapp):
    """worker 尚未启动时 suspend 应顺延启动定时器，而不是空转。"""
    fm = FontManager()
    panel = FontPanel(fm)
    try:
        panel._load_timer.start(10_000)
        panel.suspend_loading(800)
        # 启动定时器被顺延为 max(800, 剩余) —— 剩余 10s 更长，保持不变
        assert panel._load_timer.remainingTime() >= 800
        assert not (panel._suspend_timer.isActive())
    finally:
        panel._load_timer.stop()
        panel.shutdown()


def test_suspend_running_worker_pauses_and_resumes(qapp):
    """worker 运行中：suspend 暂停、恢复定时器到点后解除暂停。"""
    fm = FontManager()
    panel = FontPanel(fm)
    try:
        worker = _FontInfoWorker([], fm)

        class _FakeRunningWorker:
            """把 isRunning 伪装成 True，pause/resume 转发到真实 worker。"""

            def isRunning(self):
                return True

            def pause(self):
                worker.pause()

            def resume(self):
                worker.resume()

        panel._worker = _FakeRunningWorker()
        panel.suspend_loading(30)
        assert worker._paused
        assert panel._suspend_timer.isActive()
        deadline = time.monotonic() + 2
        while panel._suspend_timer.isActive() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        assert not worker._paused     # 到点自动恢复
    finally:
        panel._worker = None
        panel.shutdown()


# ---------------------------------------------------------------------------
# 2. 画布交互 → interactionStarted 信号
# ---------------------------------------------------------------------------
def _press_at(view: CanvasView, vp: QPoint) -> None:
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(vp), QPointF(vp),
                     Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(view.viewport(), ev)


def _move_at(view: CanvasView, vp: QPoint) -> None:
    ev = QMouseEvent(QEvent.MouseMove, QPointF(vp), QPointF(vp),
                     Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(view.viewport(), ev)


def _wheel_at(view: CanvasView, vp: QPoint) -> None:
    ev = QWheelEvent(QPointF(vp), QPointF(vp), QPoint(0, 0), QPoint(0, 120),
                     Qt.NoButton, Qt.NoModifier, Qt.ScrollUpdate, False)
    QApplication.sendEvent(view.viewport(), ev)


def test_interaction_signal_on_press_move_wheel(view):
    events: list[int] = []
    view.interactionStarted.connect(lambda: events.append(1))
    view.set_pen_marker((50.0, 50.0))       # 避免拖动标记分支吞掉移动事件
    _press_at(view, QPoint(200, 150))
    _move_at(view, QPoint(210, 150))
    _wheel_at(view, QPoint(210, 150))
    assert len(events) >= 3


# ---------------------------------------------------------------------------
# 3. 宽度重排限流自适应
# ---------------------------------------------------------------------------
def test_frame_reflow_adaptive_throttle(qapp, monkeypatch):
    from writerstudio.ui.main_window import MainWindow

    win = MainWindow()
    try:
        applied: list[float] = []
        monkeypatch.setattr(win, "_apply_frame_reflow",
                            lambda: applied.append(time.monotonic()))

        obj = win.controller.doc.objects[0] if win.controller.doc.objects else None
        if obj is None:
            from writerstudio.core.document import make_static_object
            from writerstudio.core.sample import make_rect
            obj = make_static_object([make_rect(0, 0, 10, 10)], name="矩形")
            win.controller.doc.add(obj)

        # 上次重排耗时 1s → 要求间隔 ≥1250ms：刚应用过一次就再排队必须走定时器
        win._frame_last_apply = time.monotonic()
        win._frame_last_cost = 1000.0
        win._queue_frame_reflow(obj, "text", 60.0)
        assert applied == []                    # 未立即重排
        assert win._frame_timer.isActive()

        # 上次重排很快（0ms）→ 恢复 40ms 下限节奏
        win._frame_last_cost = 0.0
        win._frame_last_apply = 0.0             # 「很久没应用」→ 立即应用
        win._queue_frame_reflow(obj, "text", 70.0)
        assert len(applied) == 1
    finally:
        if win._frame_timer is not None:
            win._frame_timer.stop()
        win.controller.undo_stack.clear()
        win.close()
        win.deleteLater()
