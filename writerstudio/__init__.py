"""WriterStudio — ESP32 GRBL 写字机软件。

包结构：
    writerstudio.core    纯 Python 数据层（几何/笔画/文档模型）
    writerstudio.ui      PySide6 界面层（画布/交互/主窗口）
    writerstudio.fonts   字体系统（P2）
    writerstudio.perturb 手写扰动引擎（P3）
    writerstudio.content Markdown/LaTeX/SVG（P4）
    writerstudio.gcode   G-code 生成与发送（P5）
    writerstudio.machine 机器连接（P5）
"""

__version__ = "0.1.0"
