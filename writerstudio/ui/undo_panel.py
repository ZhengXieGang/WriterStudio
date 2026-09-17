"""撤销历史面板：显示可撤销/重做的操作，支持点击跳转。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QListWidget,
    QListWidgetItem,
    QWidget,
)

from .controller import DocumentController


class UndoPanel(QDockWidget):
    """把 QUndoStack 的历史可视化，并允许跳转到任意步骤。"""

    def __init__(self, controller: DocumentController,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("历史", parent)
        self.controller = controller
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self.list = QListWidget()
        self.list.itemClicked.connect(self._on_click)
        self.setWidget(self.list)

        stack = controller.undo_stack
        stack.indexChanged.connect(self._refresh)
        stack.cleanChanged.connect(lambda _: self._refresh())
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._refresh_impl()
        except RuntimeError:
            # 窗口销毁过程中仍可能收到 cleanChanged 信号，忽略
            pass

    def _refresh_impl(self) -> None:
        stack = self.controller.undo_stack
        self.list.blockSignals(True)
        self.list.clear()
        idx = stack.index()
        total = stack.count()

        # 初始状态（可点击回到起点）
        init = QListWidgetItem("初始状态")
        init.setData(Qt.UserRole, 0)
        init.setForeground(Qt.gray)
        self.list.addItem(init)

        for i in range(total):
            cmd = stack.command(i)
            text = cmd.text() if cmd is not None else f"步骤 {i + 1}"
            done = i < idx
            item = QListWidgetItem(f"{i + 1}. {text}")
            item.setData(Qt.UserRole, i + 1)
            item.setForeground(Qt.darkGreen if done else Qt.gray)
            self.list.addItem(item)

        self.list.setCurrentRow(idx)   # 0 = 初始状态，i+1 = 第 i 步之后
        self.list.blockSignals(False)

    def _on_click(self, item: QListWidgetItem) -> None:
        target = item.data(Qt.UserRole)
        if target is None:
            return
        stack = self.controller.undo_stack
        target = max(0, min(int(target), stack.count()))
        while stack.index() > target and stack.canUndo():
            stack.undo()
        while stack.index() < target and stack.canRedo():
            stack.redo()
