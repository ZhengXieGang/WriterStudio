"""GUI 内嵌 AI 服务：本机回环 TCP 上的 JSON 请求应答服务。

* 仅监听 ``127.0.0.1``——只接受本机连接（MCP 桥与本软件同机运行）。
* 运行在 Qt 主线程、纯事件驱动（``QTcpServer``/``readyRead`` 信号）：
  请求到达时文档操作直接在主线程执行，与界面刷新天然串行，
  没有线程安全问题，也不需要跨线程投递。
* 帧格式：4 字节大端长度 + UTF-8 JSON 正文（带长度前缀，天然处理
  粘包/半包；上限 64MB，防畸形客户端耗尽内存）。

请求::

    {"v": 1, "id": 7, "name": "add_text", "arguments": {...}}

``name`` 为 ``__ping`` 时只返回服务信息（桥探测用），其余名字分派给
:class:`writerstudio.ai.tools.AiTools`。

应答::

    {"v": 1, "id": 7, "ok": true, "result": {...}}
    {"v": 1, "id": 7, "ok": false, "error": "原因（面向 AI 的可读说明）"}
"""

from __future__ import annotations

import json
import struct
from typing import Optional

from PySide6.QtCore import QObject
from PySide6.QtNetwork import (
    QAbstractSocket,
    QHostAddress,
    QTcpServer,
    QTcpSocket,
)

from .tools import AiTools, ToolError

PROTOCOL_VERSION = 1
#: 单帧上限：整页预览 PNG base64 也就 ~2MB，64MB 已是宽裕的防护值
_MAX_FRAME = 64 * 1024 * 1024


class AiTcpServer(QObject):
    """把 :class:`AiTools` 暴露为本机 TCP JSON 服务。"""

    def __init__(self, tools: AiTools, port: int = 8765,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._tools = tools
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._buffers: dict[QTcpSocket, bytes] = {}
        self._wanted_port = int(port)
        self._bound_port: Optional[int] = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        """监听；port=0 时由系统分配。成功返回 True（端口见 :attr:`port`）。"""
        ok = self._server.listen(QHostAddress.LocalHost, self._wanted_port)
        self._bound_port = self._server.serverPort() if ok else None
        return ok

    def stop(self) -> None:
        for sock in list(self._buffers):
            sock.abort()
        self._buffers.clear()
        if self._server.isListening():
            self._server.close()
        self._bound_port = None

    @property
    def port(self) -> Optional[int]:
        """实际监听端口（未监听时为 None）。"""
        return self._bound_port

    @property
    def error(self) -> str:
        return self._server.errorString() or "监听失败"

    # ------------------------------------------------------------ 连接
    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.setParent(self)
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._on_disconnected(s))
            self._buffers[sock] = b""

    def _on_disconnected(self, sock: QTcpSocket) -> None:
        self._buffers.pop(sock, None)
        try:
            sock.deleteLater()
        except RuntimeError:
            pass                  # C++ 对象已随连接关闭被销毁

    def _on_ready_read(self, sock: QTcpSocket) -> None:
        buf = self._buffers.get(sock)
        if buf is None:
            return
        buf += bytes(sock.readAll())
        while True:
            if len(buf) < 4:
                break
            (frame_len,) = struct.unpack_from(">I", buf, 0)
            if frame_len > _MAX_FRAME:
                # 畸形/恶意帧：回错误后断开，防止无限缓冲。
                # disconnectFromHost（而非 abort）会把待发数据写完再关，
                # 否则错误响应还没出门连接就被掐断，客户端只看到 EOF。
                self._send(sock, {"v": PROTOCOL_VERSION, "id": None,
                                  "ok": False,
                                  "error": f"帧过大（>{_MAX_FRAME} 字节）"})
                sock.disconnectFromHost()
                return
            if len(buf) < 4 + frame_len:
                break                       # 半包：等下一次 readyRead
            payload = buf[4:4 + frame_len]
            buf = buf[4 + frame_len:]
            self._handle(sock, payload)
        self._buffers[sock] = buf

    # ------------------------------------------------------------ 处理
    def _handle(self, sock: QTcpSocket, payload: bytes) -> None:
        try:
            req = json.loads(payload.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("请求必须是 JSON 对象")
        except (UnicodeDecodeError, ValueError) as exc:
            self._send(sock, {"v": PROTOCOL_VERSION, "id": None, "ok": False,
                              "error": f"请求不是合法 JSON：{exc}"})
            return
        rid = req.get("id")
        name = req.get("name")
        if name == "__ping":
            self._send(sock, {
                "v": PROTOCOL_VERSION, "id": rid, "ok": True,
                "result": {
                    "server": "writerstudio",
                    "protocol": PROTOCOL_VERSION,
                    "tools": self._tools.names(),
                    "page": {
                        "width_mm": self._tools._doc.page.width,
                        "height_mm": self._tools._doc.page.height,
                    },
                }})
            return
        try:
            result = self._tools.call(str(name or ""), req.get("arguments"))
            self._send(sock, {"v": PROTOCOL_VERSION, "id": rid,
                              "ok": True, "result": result})
        except ToolError as exc:
            self._send(sock, {"v": PROTOCOL_VERSION, "id": rid,
                              "ok": False, "error": str(exc)})

    def _send(self, sock: QTcpSocket, obj: dict) -> None:
        if sock.state() != QAbstractSocket.SocketState.ConnectedState:
            return
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            sock.write(struct.pack(">I", len(data)) + data)
        except RuntimeError:
            # socket 已被 deleteLater（对端刚断开）——忽略即可
            pass
