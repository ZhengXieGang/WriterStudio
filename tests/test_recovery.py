"""未保存内容的自动快照与异常退出恢复。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio import recovery  # noqa: E402
from writerstudio.ai.tools import AiTools  # noqa: E402
from writerstudio.settings import K_AI_ENABLED, Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """恢复文件写到临时目录，别碰真实的 ~/.writerstudio。"""
    monkeypatch.setenv("WRITERSTUDIO_HOME", str(tmp_path))
    yield


@pytest.fixture()
def win(qapp):
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    w = MainWindow(st)
    w.confirm_on_close = False
    yield w
    w._recovery_timer.stop()
    w.close()


def test_snapshot_written_only_when_dirty(win):
    assert not recovery.exists()
    AiTools(win).call("add_text", {"text": "AB", "size": 8.0,
                                   "font_names": ["futural"]})
    assert win._dirty
    win._autosave_recovery()
    assert recovery.exists()
    # 快照内容能被正常读回（与 .wsproj 同格式）
    project = recovery.load()
    assert len(project.document.objects) == 1


def test_snapshot_cleared_when_clean(win, tmp_path):
    AiTools(win).call("add_text", {"text": "AB", "size": 8.0,
                                   "font_names": ["futural"]})
    win._autosave_recovery()
    assert recovery.exists()
    win._mark_clean()                     # 保存/撤销回保存态
    assert not recovery.exists()


def test_offer_recovery_restores_content(win, monkeypatch):
    AiTools(win).call("add_text", {"text": "AB", "size": 8.0,
                                   "font_names": ["futural"]})
    win._autosave_recovery()
    # 新窗口：模拟异常退出后的重启
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    win2 = MainWindow(st)
    win2.confirm_on_close = False
    # 恢复询问是真·模态框，测试里必须替它作答，否则整个测试进程卡在
    # 事件循环里等点击（曾把全量测试挂在 85% 一直不动）
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k:
                                     QMessageBox.StandardButton.Yes))
    try:
        assert recovery.exists()
        assert win2.maybe_offer_recovery() is True
        assert len(win2.controller.doc.objects) == 1
        assert win2._dirty                    # 恢复出来仍是未保存状态
        assert win2.project_path is None      # 未绑定到任何项目文件
    finally:
        win2._recovery_timer.stop()
        win2.close()


def test_offer_recovery_can_decline(win, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    AiTools(win).call("add_text", {"text": "AB", "size": 8.0,
                                   "font_names": ["futural"]})
    win._autosave_recovery()
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    win2 = MainWindow(st)
    win2.confirm_on_close = False
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k:
                                     QMessageBox.StandardButton.No))
    try:
        assert win2.maybe_offer_recovery() is False
        assert not recovery.exists()          # 拒绝即丢弃快照
        assert len(win2.controller.doc.objects) == 0
    finally:
        win2._recovery_timer.stop()
        win2.close()


def test_offer_recovery_noop_without_snapshot(win):
    assert win.maybe_offer_recovery() is False


# ----------------------------------------------- 多实例：快照各写各的
def test_snapshot_is_per_instance(win, tmp_path):
    """两个实例各写一份快照，互不覆盖；恢复取最新那份。"""
    import os
    import time
    from writerstudio.project import ProjectData

    # 假装另一个进程（PID 与本次不同）先写了一版
    other = tmp_path / "recovery-999999.wsproj"
    doc_a = win.controller.doc
    from writerstudio.project import save_project
    save_project(str(other), ProjectData(document=doc_a))
    old = other.stat().st_mtime_ns

    # 本实例写自己的那份（不碰别人的）
    AiTools(win).call("add_text", {"text": "AB", "size": 8.0,
                                   "font_names": ["futural"]})
    win._autosave_recovery()
    mine = recovery.instance_path()
    assert mine.exists() and mine.name == f"recovery-{os.getpid()}.wsproj"
    assert other.exists()
    assert other.stat().st_mtime_ns == old          # 一个字节都没动

    # 两份都在时取最新的：本实例这份刚写，内容带 1 个对象
    if other.stat().st_mtime_ns > mine.stat().st_mtime_ns:
        time.sleep(0.01)
        win._autosave_recovery()
    assert recovery.newest() == mine
    assert len(recovery.load().document.objects) == 1

    # clear() 只清自己 + 属主已退出的遗留，活实例那份留着……这里
    # 999999 明显不存在 → 两份都该被清掉
    recovery.clear()
    assert not mine.exists() and not other.exists()


def test_snapshot_keeps_other_live_instance(tmp_path, monkeypatch):
    """属主还活着的快照不能被别的实例清掉（两实例共用 HOME 时的保护）。"""
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "writerstudio"])
    try:
        path = tmp_path / f"recovery-{child.pid}.wsproj"
        path.write_text("{}", encoding="utf-8")
        assert recovery._owner_alive(path)
        recovery.clear()
        assert path.exists()                    # 活实例的快照保留
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert not recovery._owner_alive(path)      # 退出后即可清
    recovery.clear()
    assert not path.exists()


def test_filelock_detects_other_instance(tmp_path, monkeypatch):
    """同一文件被另一活实例打开时能查到；陈旧登记（进程已退出）不误报。"""
    import json
    import subprocess
    import sys

    from writerstudio import filelock

    target = tmp_path / "shared.wsproj"
    target.write_text("{}", encoding="utf-8")
    filelock.claim(target)
    assert filelock.others(target) == []        # 只有自己不算

    d = filelock._dir()
    d.mkdir(parents=True, exist_ok=True)
    reg = d / f"{filelock._key(target)}.json"
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "writerstudio"])
    try:
        reg.write_text(json.dumps({"pid": child.pid, "start": None,
                                   "path": str(target)}), encoding="utf-8")
        who = filelock.others(target)
        assert [w["pid"] for w in who] == [child.pid]
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert filelock.others(target) == []        # 进程没了 → 不再提醒

    # 别人留下的登记不会被 release 动（只删自己那份）；claim 覆盖后可删
    assert reg.exists()
    filelock.claim(target)
    filelock.release(target)
    assert not reg.exists()
