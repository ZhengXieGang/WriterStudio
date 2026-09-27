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


def _isolated_env(tmp_path) -> dict:
    """让桥的子进程看不到本机的软件配置（端口回退会连上真在跑的实例）。

    ``test_bridge_requires_running_gui`` / ``test_bridge_reports_foreign_port_occupant``
    要的是「只有我指定的这个端口」这一确定环境；开发机上开着 WriterStudio
    时，配置回退会让它们连上真实例（或绕过占位端口），断言随之失效。
    """
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    return {**os.environ, "XDG_CONFIG_HOME": str(cfg), "PYTHONUNBUFFERED": "1"}


def test_bridge_requires_running_gui(qapp, tmp_path):
    """GUI 不在时，桥的工具调用返回可读错误（不是崩溃）。

    错误必须点明「没有程序在监听」并给出排查路径——历史问题是干等
    300 秒，客户端只看到「超时」而无任何线索。
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "writerstudio.ai.bridge",
                  "--port", str(_free_port())],
            cwd=str(REPO_ROOT),
            env=_isolated_env(tmp_path),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool("get_page_info", {})
                assert r.isError
                text = r.content[0].text
                assert "无法与 WriterStudio 通信" in text
                assert "没有程序在监听" in text      # 与「端口被占」区分开
                assert "AI 服务设置" in text          # 给出可执行的排查路径

    _run_with_qt_pump(qapp, scenario(), 60)


def test_bridge_reports_foreign_port_occupant(qapp, tmp_path):
    """端口被别的程序占用（连得上但不回话）→ 快速、明确地报错。

    历史问题：占位进程能完成 TCP 握手却不说本协议，桥一直等到 300 秒
    超时，客户端 30 秒就放弃，表现为「神秘超时」。现在探针 3 秒内判定。
    """
    import socket as _socket
    import threading
    import time
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # 占位进程：accept 后什么都不回（模拟 unidbg 之类的本地服务）
    srv = _socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _hold():
        srv.settimeout(0.5)
        conns = []
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                conns.append(conn)          # 收下连接、保持不回应
            except _socket.timeout:
                continue
            except OSError:
                break
        for c in conns:
            c.close()
        srv.close()

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    try:
        async def scenario():
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "writerstudio.ai.bridge", "--port", str(port)],
                cwd=str(REPO_ROOT),
                env=_isolated_env(tmp_path),
            )
            t0 = time.monotonic()
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    r = await session.call_tool("get_page_info", {})
                    assert r.isError
                    text = r.content[0].text
                    assert "回话" in text or "占用" in text, text
            # 探针 3 秒 + 握手开销；远小于旧实现的 300 秒哑等
            assert time.monotonic() - t0 < 30

        _run_with_qt_pump(qapp, scenario(), 90)
    finally:
        stop.set()
        holder.join(3)


def test_bridge_falls_back_to_gui_configured_port(qapp, win, tmp_path):
    """MCP 配置的端口不通时，回退读软件配置里的端口并连上（自愈）。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    srv = AiTcpServer(AiTools(win), port=0, parent=win)
    assert srv.start(), srv.error
    real_port = srv.port
    cfg_dir = tmp_path / "WriterStudio"
    cfg_dir.mkdir()
    (cfg_dir / "WriterStudio.conf").write_text(
        f"[ai]\nmcp_enabled=true\nmcp_port={real_port}\n", encoding="utf-8")
    wrong_port = _free_port()

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "writerstudio.ai.bridge", "--port", str(wrong_port)],
            cwd=str(REPO_ROOT),
            env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool("get_page_info", {})
                assert not r.isError, r.content[0].text
                assert json.loads(r.content[0].text)["width_mm"] > 0

    try:
        _run_with_qt_pump(qapp, scenario(), 90)
    finally:
        srv.stop()          # QTcpServer 只能在 Qt 主线程里停


def test_bridge_error_mentions_disabled_service(qapp, tmp_path):
    """软件配置里服务是关闭状态时，错误里要直接说出来。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cfg_dir = tmp_path / "WriterStudio"
    cfg_dir.mkdir()
    (cfg_dir / "WriterStudio.conf").write_text(
        "[ai]\nmcp_enabled=false\nmcp_port=" + str(_free_port()) + "\n",
        encoding="utf-8")

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "writerstudio.ai.bridge", "--port", str(_free_port())],
            cwd=str(REPO_ROOT),
            env={**os.environ, "XDG_CONFIG_HOME": str(tmp_path)},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                r = await session.call_tool("get_page_info", {})
                assert r.isError
                assert "关闭" in r.content[0].text

    _run_with_qt_pump(qapp, scenario(), 60)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
