"""「AI 排版服务」设置对话框：配置文本生成与端口替换回归。

历史 bug：配置模板是 JSON 正文（含大量字面花括号），却用 ``str.format``
做端口替换，点开菜单即抛 ``KeyError: '\\n  "mcpServers"'``；且说明文字里
的 ``{port}`` 从未被替换、原样显示。本测试覆盖这两个点。
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ai.dialog import _CONFIG_TEMPLATE  # noqa: E402
from writerstudio.settings import K_AI_ENABLED, K_AI_PORT, Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_config_template_json_braces_survive_substitution():
    """模板替换端口后仍是合法 JSON，且花括号未被吞掉。"""
    from writerstudio.ai.dialog import _fill
    text = _fill(_CONFIG_TEMPLATE, 8765)
    data = json.loads(text)                    # 解析失败即说明花括号被破坏
    server = data["mcpServers"]["writerstudio"]
    assert server["args"] == ["--port", "8765"]


def test_dialog_opens_and_shows_port(qapp):
    """对话框能正常构造（历史 bug 在构造时即崩溃）并显示实际端口。"""
    from writerstudio.ui.main_window import MainWindow
    from writerstudio.ai.dialog import AiServiceDialog

    st = Settings()
    st.set(K_AI_ENABLED, False)
    st.set(K_AI_PORT, 8899)
    win = MainWindow(st)
    try:
        dlg = AiServiceDialog(win)
        text = dlg._config.toPlainText()
        cfg, _, note = text.partition("\n\n")
        assert json.loads(cfg)["mcpServers"]["writerstudio"]["args"] == [
            "--port", "8899"]
        assert "8899" in note
        assert "{port}" not in text            # 占位符全部替换，不留原文
        assert "{port}" not in dlg._note.text()
    finally:
        win.confirm_on_close = False
        win.close()


def test_copy_config_puts_valid_json_on_clipboard(qapp):
    """「复制配置」写入剪贴板的必须是替换好端口的合法 JSON。"""
    from writerstudio.ui.main_window import MainWindow
    from writerstudio.ai.dialog import AiServiceDialog

    st = Settings()
    st.set(K_AI_ENABLED, False)
    st.set(K_AI_PORT, 8123)
    win = MainWindow(st)
    try:
        dlg = AiServiceDialog(win)
        dlg._copy_config()
        data = json.loads(QApplication.clipboard().text())
        assert data["mcpServers"]["writerstudio"]["args"][-1] == "8123"
    finally:
        win.confirm_on_close = False
        win.close()


def test_apply_starts_service_and_shows_running(qapp):
    """「保存并应用」按新端口拉起服务，状态行随之更新为运行中。"""
    import socket

    from writerstudio.ui.main_window import MainWindow
    from writerstudio.ai.dialog import AiServiceDialog

    with socket.socket() as s:            # 取一个空闲端口，避免与 CI 上
        s.bind(("127.0.0.1", 0))          # 其它服务冲突
        port = s.getsockname()[1]

    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    try:
        dlg = AiServiceDialog(win)
        dlg._enabled.setChecked(True)
        dlg._port.setValue(port)
        dlg._apply()
        assert getattr(win, "ai_server", None) is not None
        assert str(port) in dlg._status.text()
        assert "运行中" in dlg._status.text()
    finally:
        win.confirm_on_close = False
        win.close()                       # 关闭时停止服务，不泄漏端口
