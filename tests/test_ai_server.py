"""AI TCP 服务测试：帧协议、粘包/半包、错误处理、真实主窗口集成。"""

from __future__ import annotations

import json
import os
import socket
import struct
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ai.server import AiTcpServer  # noqa: E402
from writerstudio.ai.tools import AiTools  # noqa: E402
from writerstudio.settings import K_AI_ENABLED, K_AI_PORT, Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def win(qapp):
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    w = MainWindow(st)
    yield w
    w.close()


@pytest.fixture()
def server(qapp, win):
    srv = AiTcpServer(AiTools(win), port=0, parent=win)
    assert srv.start(), srv.error
    assert srv.port and srv.port > 0        # port=0 → 系统分配
    yield srv
    srv.stop()


class _Client:
    """测试客户端：持久缓冲，一次 recv 多收的帧留给下一次（粘包正确性）。"""

    def __init__(self, port: int, qapp) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.sock.settimeout(0.05)         # 非阻塞轮询，配合 Qt 事件泵
        self.qapp = qapp
        self.buf = b""

    def send(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def recv(self) -> dict:
        """接收一个应答帧；服务端在 Qt 主线程，需一边泵事件一边读。"""
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            self.qapp.processEvents()      # 驱动 QTcpServer 的 readyRead
            frame = self._pop_frame()
            if frame is not None:
                return frame
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue                   # 缓冲里也许已有完整帧，回顶部检查
            if not chunk:
                break
            self.buf += chunk
        raise TimeoutError("等待服务响应超时")

    def _pop_frame(self):
        """缓冲里已有完整帧则取出返回（一次 recv 可能粘了多帧）。"""
        if len(self.buf) < 4:
            return None
        (n,) = struct.unpack_from(">I", self.buf, 0)
        if len(self.buf) < 4 + n:
            return None
        payload = self.buf[4:4 + n]
        self.buf = self.buf[4 + n:]
        return json.loads(payload.decode("utf-8"))

    def close(self) -> None:
        self.sock.close()


def test_ping_lists_tools(server, qapp):
    c = _Client(server.port, qapp)
    try:
        c.send({"v": 1, "id": 1, "name": "__ping"})
        r = c.recv()
        assert r["ok"] is True
        assert r["result"]["server"] == "writerstudio"
        assert "add_text" in r["result"]["tools"]
    finally:
        c.close()


def test_call_roundtrip_changes_document(server, win, qapp):
    c = _Client(server.port, qapp)
    try:
        c.send({"v": 1, "id": 2, "name": "add_text",
                "arguments": {"text": "网络调用", "x": 40, "y": 40}})
        r = c.recv()
        assert r["ok"] is True and r["id"] == 2
        assert r["result"]["bbox"]["x"] == pytest.approx(40, abs=0.5)
        assert len(win.controller.doc.objects) == 1
    finally:
        c.close()


def test_tcp_packets_coalesced_and_split(server, qapp):
    """粘包（两请求一包）与半包（一请求分两包）都必须正确分帧。"""
    c = _Client(server.port, qapp)
    try:
        # --- 粘包：一次发两个完整请求 ---
        req1 = json.dumps({"v": 1, "id": 1, "name": "get_page_info"}).encode()
        req2 = json.dumps({"v": 1, "id": 2, "name": "get_page_info"}).encode()
        c.sock.sendall(struct.pack(">I", len(req1)) + req1
                       + struct.pack(">I", len(req2)) + req2)
        r1, r2 = c.recv(), c.recv()
        assert r1["id"] == 1 and r2["id"] == 2

        # --- 半包：一个请求拆成 3 段发 ---
        req3 = json.dumps({"v": 1, "id": 3, "name": "get_page_info"}).encode()
        frame = struct.pack(">I", len(req3)) + req3
        c.sock.sendall(frame[:3])
        time.sleep(0.05)
        c.sock.sendall(frame[3:9])
        time.sleep(0.05)
        c.sock.sendall(frame[9:])
        r3 = c.recv()
        assert r3["id"] == 3 and r3["ok"] is True
    finally:
        c.close()


def test_bad_json_and_unknown_tool(server, qapp):
    c = _Client(server.port, qapp)
    try:
        data = b"{this is not json"
        c.sock.sendall(struct.pack(">I", len(data)) + data)
        r = c.recv()
        assert r["ok"] is False and "JSON" in r["error"]

        c.send({"v": 1, "id": 5, "name": "no_such_tool"})
        r = c.recv()
        assert r["id"] == 5 and r["ok"] is False
        assert "未知工具" in r["error"]

        c.send({"v": 1, "id": 6, "name": "add_text",
                "arguments": {"text": ""}})
        r = c.recv()
        assert r["ok"] is False and "text" in r["error"]
    finally:
        c.close()


def test_oversized_frame_kicks_client(server, qapp):
    c = _Client(server.port, qapp)
    try:
        c.sock.sendall(struct.pack(">I", 100 * 1024 * 1024))  # 谎报 100MB
        r = c.recv()
        assert r["ok"] is False and "帧过大" in r["error"]
        # 服务端随即断开（recv 返回空）
        deadline = time.monotonic() + 5
        closed = False
        while time.monotonic() < deadline:
            qapp.processEvents()
            try:
                if c.sock.recv(1024) == b"":
                    closed = True
                    break
            except socket.timeout:
                continue
        assert closed, "服务端未断开超大帧连接"
    finally:
        c.close()


def test_multiple_clients_isolated(server, win, qapp):
    c1, c2 = _Client(server.port, qapp), _Client(server.port, qapp)
    try:
        c1.send({"v": 1, "id": 1, "name": "add_text",
                 "arguments": {"text": "客户端一"}})
        c2.send({"v": 1, "id": 2, "name": "get_page_info"})
        r1, r2 = c1.recv(), c2.recv()
        assert r1["ok"] and r2["ok"]
        assert len(win.controller.doc.objects) == 1
    finally:
        c1.close()
        c2.close()


def test_window_settings_start_server(qapp, monkeypatch):
    """主窗口按设置自动启停 AI 服务（默认开启，绑定回环地址）。"""
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, True)
    st.set(K_AI_PORT, 0)               # 让系统分配，避免测试占固定端口
    w = MainWindow(st)
    try:
        assert w.ai_server is not None
        assert w.ai_server.port and w.ai_server.port > 0
        # 关闭开关后重启 → 服务停止
        st.set_ai_service_enabled(False)
        w.restart_ai_server()
        assert w.ai_server is None
    finally:
        w.close()
