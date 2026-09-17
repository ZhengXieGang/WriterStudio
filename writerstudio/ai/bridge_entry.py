"""PyInstaller 桥可执行文件入口（WriterStudioMCP / writerstudio-mcp）。

与 GUI 分开打包：桥不依赖 PySide6/matplotlib，单独 Analysis 产物小得多，
且 stdio MCP 服务不需要图形栈。GUI 程序包里只带数据，不 import 本模块。
"""

import sys

from writerstudio.ai.bridge import main

if __name__ == "__main__":
    sys.exit(main())
