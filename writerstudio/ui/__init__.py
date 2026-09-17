"""界面层（PySide6）：画布、对象图形项、文档控制器、主窗口。"""

from .canvas import CanvasView
from .controller import DocumentController
from .font_panel import FontPanel
from .main_window import MainWindow
from .text_dialog import TextEditDialog

__all__ = ["CanvasView", "DocumentController", "FontPanel", "MainWindow",
           "TextEditDialog"]
