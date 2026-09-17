# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：Windows 便携单文件 exe，Linux/macOS 目录版（mac 出 .app）。

PyInstaller 不能跨平台交叉编译——三个平台各跑一次：
    pyinstaller writerstudio.spec        （在对应平台的本机或 CI 上）

资源定位：代码里 ``Path(__file__).parent / "builtin"``（内置字体）在
bundle 内依然成立——datas 目标路径保持 writerstudio/ 包前缀，PyInstaller
会把模块 __file__ 镜像到 _MEIPASS 下的同款目录树。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "writerstudio" / "fonts" / "builtin"),
     "writerstudio/fonts/builtin"),
]
# 主题 QSS/JSON 与图标字体（hooks-contrib 之外的保险收集）
datas += collect_data_files("qdarktheme")
datas += collect_data_files("qtawesome")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest",
              # 本机可能经 system-site-packages 混入其它 Qt 绑定，
              # PyInstaller 禁止多绑定共存（只认 PySide6）
              "PyQt5", "PyQt6", "PySide2"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# ---- AI 排版桥（MCP stdio 服务）：独立小可执行文件 -------------------------
# 桥不依赖 PySide6/matplotlib（排除掉），只带 mcp SDK 及其依赖。
bridge_a = Analysis(
    [str(ROOT / "writerstudio" / "ai" / "bridge_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["mcp.server.fastmcp", "mcp.server.stdio", "mcp.types",
                   # pydantic 的二进制扩展与 anyio 的异步后端都是动态加载，
                   # 静态分析看不到，必须显式声明
                   "pydantic", "pydantic_core", "pydantic_core._pydantic_core",
                   "anyio", "anyio._backends._asyncio"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PySide6", "PyQt5", "PyQt6", "PySide2",
              "matplotlib", "PySide6.QtNetwork"],
    noarchive=False,
)
bridge_pyz = PYZ(bridge_a.pure)

if sys.platform == "win32":
    # Windows：单文件便携 exe（自带解压，双击即用、免安装）
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="WriterStudio",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=str(ROOT / "resources" / "icon.ico"),
        disable_windowed_traceback=False,
    )
    # AI 桥：console=True——stdio 服务需要真实的 stdout（windowed 下为 None）
    mcp_exe = EXE(
        bridge_pyz,
        bridge_a.scripts,
        bridge_a.binaries,
        bridge_a.datas,
        [],
        name="WriterStudioMCP",
        debug=False,
        strip=False,
        upx=False,
        console=True,
        icon=str(ROOT / "resources" / "icon.ico"),
    )
else:
    # Linux / macOS：目录版（启动快、避开 onefile 的 /tmp 解压问题）；
    # macOS 在 windowed+onedir 下额外产出 WriterStudio.app
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="WriterStudio",
        debug=False,
        strip=False,
        upx=False,
        console=False,
    )
    mcp_exe = EXE(
        bridge_pyz,
        bridge_a.scripts,
        [],
        exclude_binaries=True,
        name="WriterStudioMCP",
        debug=False,
        strip=False,
        upx=False,
        console=True,
    )
    coll = COLLECT(
        exe,
        mcp_exe,
        a.binaries,
        a.datas,
        # 桥可执行文件的二进制/数据也要进目录（exclude_binaries=True 时
        # EXE 本身不带，漏掉会让 WriterStudioMCP 缺 pydantic/mcp 等依赖）
        bridge_a.binaries,
        bridge_a.datas,
        strip=False,
        upx=False,
        name="WriterStudio",
    )
    if sys.platform == "darwin":
        app = BUNDLE(coll, name="WriterStudio.app",
                     icon=str(ROOT / "resources" / "icon.png"))
