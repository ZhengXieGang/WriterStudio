"""文件对话框起始目录解析（ui/filedialog.py）。

用户反馈驱动：此前各处传 ``last_dir() or ""``（字体导入/SVG 更是空串），
空串时起始位置交给桌面环境（KDE KIO）自行决定，常落在桌面或主目录，
观感像「软件在乱翻文件」。现统一优先级：
last_dir → 当前项目目录 → 系统「文档」目录 → 家目录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QStandardPaths  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ui import filedialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _clean_context():
    filedialog.reset()
    yield
    filedialog.reset()


def _docs() -> str:
    return QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)


def test_priority_last_dir_wins(qapp, tmp_path):
    filedialog.install(get_last_dir=lambda: str(tmp_path),
                       get_project_path=lambda: "/nope/x.wsproj",
                       set_last_dir=None)
    assert Path(filedialog.start_dir()) == tmp_path


def test_priority_project_dir_when_no_last(qapp, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    filedialog.install(get_last_dir=lambda: "",
                       get_project_path=lambda: str(proj / "a.wsproj"),
                       set_last_dir=None)
    assert Path(filedialog.start_dir()) == proj


def test_priority_documents_when_nothing_valid(qapp):
    # runner/容器环境可能没有「文档」目录——start_dir 只认真实存在的
    # 目录，缺了会退回家目录。这里确保它存在，测试不依赖环境。
    Path(_docs()).mkdir(parents=True, exist_ok=True)
    filedialog.install(get_last_dir=lambda: "/no/such/dir",
                       get_project_path=lambda: None,
                       set_last_dir=None)
    assert filedialog.start_dir() == _docs()


def test_never_returns_empty(qapp):
    """任何情况下都给出一个存在的目录（绝不返回空串给桌面环境乱挑）。"""
    filedialog.install(get_last_dir=lambda: "/no/such",
                       get_project_path=lambda: "/no/such/x",
                       set_last_dir=None)
    d = filedialog.start_dir()
    assert d and Path(d).is_dir()


def test_path_for_uses_basename(qapp, tmp_path):
    """保存对话框：起始目录 + 纯文件名（绝对路径只取文件名，不拼出怪路径）。"""
    filedialog.install(get_last_dir=lambda: str(tmp_path),
                       get_project_path=lambda: None, set_last_dir=None)
    assert Path(filedialog.path_for("output.gcode")) == tmp_path / "output.gcode"
    p = filedialog.path_for("/home/other/桌面/2.wsproj")
    assert Path(p) == tmp_path / "2.wsproj"


def test_remember_writes_last_dir(qapp, tmp_path):
    saved = {}
    filedialog.install(get_last_dir=lambda: "",
                       get_project_path=lambda: None,
                       set_last_dir=lambda d: saved.__setitem__("d", d))
    f = tmp_path / "sub" / "a.wsproj"
    filedialog.remember(str(f))
    assert Path(saved["d"]) == tmp_path / "sub"


def test_remember_without_context_is_safe(qapp):
    filedialog.reset()
    filedialog.remember("/tmp/x/y.txt")      # 不得抛异常


def test_main_window_installs_context(qapp, tmp_path):
    """主窗口构造时应安装 context，使面板共用同一套解析。"""
    from writerstudio.ui.main_window import MainWindow
    win = MainWindow()
    try:
        # 项目路径在打开前为空 → 解析结果应是一个存在的目录
        d = filedialog.start_dir()
        assert d and Path(d).is_dir()
    finally:
        win.close()


def test_open_reference_dialog_passes_real_dir(qapp, monkeypatch):
    """端到端：参考图对话框实际收到的起始目录必须真实存在（非空串）。"""
    from writerstudio.ui.main_window import MainWindow
    from PySide6.QtWidgets import QFileDialog
    captured = {}

    def fake_open(parent, title, directory, filt, *a, **k):
        captured["dir"] = directory
        return ("", "")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_open))
    win = MainWindow()
    try:
        win._add_reference_image()
        assert captured["dir"], "起始目录不得为空串"
        assert Path(captured["dir"]).is_dir()
    finally:
        win.close()


def test_save_project_as_passes_real_dir(qapp, monkeypatch):
    from writerstudio.ui.main_window import MainWindow
    from PySide6.QtWidgets import QFileDialog
    captured = {}

    def fake_save(parent, title, path, filt, *a, **k):
        captured["path"] = path
        return ("", "")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(fake_save))
    win = MainWindow()
    try:
        win._save_project_as()
        assert Path(captured["path"]).parent.is_dir()
        assert Path(captured["path"]).name.endswith(".wsproj")
    finally:
        win.close()
