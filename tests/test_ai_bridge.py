"""MCP stdio 桥端到端测试：真实 MCP 客户端 → 桥子进程 → TCP → GUI 文档。

需要官方 ``mcp`` SDK（桥的运行依赖）；未安装则整文件跳过。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")
mcp = pytest.importorskip("mcp")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ai.server import AiTcpServer  # noqa: E402
from writerstudio.ai.tools import AiTools  # noqa: E402
from writerstudio.settings import K_AI_ENABLED, Settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def server(qapp, win):
    srv = AiTcpServer(AiTools(win), port=0, parent=win)
    assert srv.start(), srv.error
    yield srv
    srv.stop()


@pytest.fixture()
def win(qapp):
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    w = MainWindow(st)
    yield w
    w.close()


def _run_with_qt_pump(qapp, coro, timeout: float):
    """在后台线程跑 asyncio 场景，主线程持续泵 Qt 事件。

    GUI 的 TCP 服务运行在 Qt 主线程事件循环里；若在主线程直接
    ``asyncio.run``，事件循环没人泵、服务永远收不到请求，桥就会死等。
    """
    import threading
    import time
    outcome: dict = {}

    def _run():
        try:
            outcome["value"] = asyncio.run(asyncio.wait_for(coro, timeout))
        except BaseException as exc:       # noqa: BLE001 — 原样带回断言
            outcome["error"] = exc

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout + 15
    while t.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    t.join(5)
    if not outcome.get("ok", False) and "error" in outcome:
        raise outcome["error"]
    if t.is_alive():
        raise TimeoutError("桥接场景未在限时内完成")
    return outcome.get("value")


def test_bridge_end_to_end(qapp, server, win, tmp_path):
    """通过真 stdio 子进程跑完整链路：initialize → list_tools → 调工具。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import ImageContent, TextContent

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "writerstudio.ai.bridge", "--port", str(server.port)],
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.serverInfo.name == "writerstudio"

                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"add_text", "add_tikz", "render_preview",
                        "check_layout", "save_project"} <= names

                # 1) 查询页面的返回是合法 JSON 文本
                r = await session.call_tool("get_page_info", {})
                info = json.loads(r.content[0].text)
                assert info["width_mm"] == pytest.approx(297.0)

                # 2) 添加文本 → GUI 文档确实多了一个对象
                r = await session.call_tool(
                    "add_text", {"text": "桥接测试", "x": 30, "y": 30,
                                 "size": 6})
                added = json.loads(r.content[0].text)
                assert added["ok"] is True
                assert added["bbox"]["x"] == pytest.approx(30, abs=0.5)
                assert len(win.controller.doc.objects) == 1

                # 3) 参数错误 → isError 且错误面向 AI
                r = await session.call_tool(
                    "add_text", {"text": "x", "font_names": ["坏字体"]})
                assert r.isError
                assert "坏字体" in r.content[0].text

                # 4) 预览返回图像内容
                r = await session.call_tool("render_preview",
                                            {"width_px": 600})
                imgs = [c for c in r.content if isinstance(c, ImageContent)]
                texts = [c for c in r.content if isinstance(c, TextContent)]
                assert imgs and imgs[0].mimeType == "image/png"
                assert json.loads(texts[0].text)["width_px"] == 600

                # 5) 保存项目
                out = tmp_path / "bridge.wsproj"
                r = await session.call_tool("save_project",
                                            {"path": str(out)})
                assert json.loads(r.content[0].text)["ok"] is True
                assert out.exists()

    _run_with_qt_pump(qapp, scenario(), 120)


def test_bridge_requires_running_gui(qapp, tmp_path):
    """GUI 不在时，桥的工具调用返回可读错误（不是崩溃）。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "writerstudio.ai.bridge",
                  "--port", str(_free_port())],
            cwd=str(REPO_ROOT),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool("get_page_info", {})
                assert r.isError
                assert "无法连接 WriterStudio" in r.content[0].text

    _run_with_qt_pump(qapp, scenario(), 60)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
