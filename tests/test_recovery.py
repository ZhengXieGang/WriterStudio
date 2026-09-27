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
