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
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="WriterStudio",
    )
    if sys.platform == "darwin":
        app = BUNDLE(coll, name="WriterStudio.app",
                     icon=str(ROOT / "resources" / "icon.png"))
