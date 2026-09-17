"""「AI 排版服务」设置对话框：开关、端口与客户端配置示例。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
)

from ..settings import Settings

#: MCP 客户端通用配置片段（stdio；任何支持 MCP 的 AI 应用同构）
_CONFIG_TEMPLATE = """\
{
  "mcpServers": {
    "writerstudio": {
      "command": "writerstudio-mcp",
      "args": ["--port", "{port}"]
    }
  }
}
"""

_CONFIG_NOTE = """\
把上面的配置加入你的 AI 应用（Claude Desktop 的 claude_desktop_config.json、
ZCode/其他 MCP 客户端的 mcpServers 配置），重启该应用即可连接本软件。
command 按安装方式选择：发行版 Windows 用 WriterStudioMCP.exe；Linux
AppImage 用其文件路径且 args 前加 "--mcp"；macOS 用
WriterStudio.app/Contents/MacOS/WriterStudioMCP；源码运行无该命令时
command 用 "python"，args 用 ["-m", "writerstudio.ai.bridge",
"--port", "{port}"]。
服务只监听本机(127.0.0.1)，AI 修改的内容全部可在软件里撤销。"""


class AiServiceDialog(QDialog):
    """AI 排版服务（MCP）状态与配置。"""

    def __init__(self, window) -> None:
        super().__init__(window)
        self._win = window
        self.setWindowTitle("AI 排版服务（MCP）")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        self._status = QLabel(self)
        self._status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        self._enabled = QCheckBox("随软件启动 AI 排版服务", self)
        row.addWidget(self._enabled)
        row.addStretch(1)
        row.addWidget(QLabel("端口：", self))
        self._port = QSpinBox(self)
        self._port.setRange(1024, 65535)
        row.addWidget(self._port)
        layout.addLayout(row)

        apply_btn = QPushButton("保存并应用", self)
        apply_btn.clicked.connect(self._apply)
        row.addWidget(apply_btn)

        layout.addWidget(QLabel("AI 客户端配置（复制到 Claude Desktop 等）：", self))
        self._config = QTextEdit(self)
        self._config.setReadOnly(True)
        font = self._config.font()
        font.setFamily("monospace")
        self._config.setFont(font)
        layout.addWidget(self._config, 1)

        copy_row = QHBoxLayout()
        copy_row.addStretch(1)
        copy_btn = QPushButton("复制配置", self)
        copy_btn.clicked.connect(self._copy_config)
        copy_row.addWidget(copy_btn)
        layout.addLayout(copy_row)

        note = QLabel(_CONFIG_NOTE, self)
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)

        self._refresh()

    # ------------------------------------------------------------ 逻辑
    def _refresh(self) -> None:
        st: Settings = self._win.settings
        port = st.ai_service_port()
        self._enabled.setChecked(st.ai_service_enabled())
        self._port.setValue(port)
        server = getattr(self._win, "ai_server", None)
        if server is not None and server.port is not None:
            self._status.setText(
                f"状态：运行中 — 127.0.0.1:{server.port}"
                f"（共 {len(server._tools.names())} 个工具可用）")
        else:
            self._status.setText("状态：已停止")
        self._config.setPlainText(
            _CONFIG_TEMPLATE.format(port=port) + "\n" + _CONFIG_NOTE)

    def _apply(self) -> None:
        st: Settings = self._win.settings
        st.set_ai_service_enabled(self._enabled.isChecked())
        st.set_ai_service_port(self._port.value())
        st.sync()
        self._win.restart_ai_server()
        self._refresh()

    def _copy_config(self) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(
            _CONFIG_TEMPLATE.format(port=self._port.value()))
        self._status.setText("已复制 MCP 客户端配置到剪贴板")
