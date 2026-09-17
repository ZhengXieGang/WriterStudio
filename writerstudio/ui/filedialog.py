"""文件对话框的起始目录解析（全项目统一）。

**背景**：此前各处直接传 ``settings.last_dir() or ""``，字体导入与 SVG
选择甚至传空串。传空串时起始位置交给桌面环境决定——KDE 的 KIO 文件
对话框会自己挑一个位置（常是桌面或主目录），观感上像「软件在乱翻我的
文件」，并伴随 ``kf.kio.widgets.kdirmodel`` 之类与自己无关的日志。

**统一优先级**（第一个存在且是目录者胜出）：

1. 上次打开/保存文件所在目录（``settings.last_dir``）；
2. 当前项目文件所在目录；
3. 系统「文档」目录（``QStandardPaths.DocumentsLocation``）；
4. 家目录（最后兜底，保证永远给出一个有效目录）。

第 1/2 项需要运行期的 settings 与项目路径。字体库面板、内容对话框等
没有（也不该）各自持有它们，故本模块用一份**可安装的进程级 context**：
主窗口构造时把三个取值/写值函数装进来，所有面板都拿到同一套解析。
单窗口桌面应用只需一份；测试可 ``reset()`` 还原。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QStandardPaths

#: 读取上次目录 / 读取当前项目目录 / 写入上次目录。由 MainWindow 安装。
_get_last_dir: Optional[Callable[[], str]] = None
_get_project_path: Optional[Callable[[], Optional[str]]] = None
_set_last_dir: Optional[Callable[[str], None]] = None


def install(get_last_dir: Optional[Callable[[], str]] = None,
            get_project_path: Optional[Callable[[], Optional[str]]] = None,
            set_last_dir: Optional[Callable[[str], None]] = None) -> None:
    """安装 context（主窗口构造时调用一次；取值函数随会话保持最新）。"""
    global _get_last_dir, _get_project_path, _set_last_dir
    _get_last_dir = get_last_dir
    _get_project_path = get_project_path
    _set_last_dir = set_last_dir


def reset() -> None:
    """清空 context（测试用）。"""
    install()


def _first_existing(candidates) -> str:
    for c in candidates:
        if not c:
            continue
        try:
            if Path(c).is_dir():
                return str(Path(c))
        except OSError:
            continue
    return ""


def start_dir() -> str:
    """按统一优先级解析起始目录（保证返回一个存在的目录，兜底家目录）。"""
    project_dir = None
    if _get_project_path is not None:
        try:
            p = _get_project_path()
        except Exception:
            p = None
        if p:
            project_dir = str(Path(p).parent)
    last = ""
    if _get_last_dir is not None:
        try:
            last = _get_last_dir() or ""
        except Exception:
            last = ""
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    return _first_existing([last, project_dir, docs, str(Path.home())])


def path_for(name: str) -> str:
    """保存对话框的「目录 + 文件名」：起始目录下用 ``name`` 的纯文件名。

    ``name`` 为绝对路径（如当前项目文件）时只取其文件名，避免起始目录
    与绝对名拼接出怪路径。
    """
    base = os.path.basename(name) if name else ""
    return str(Path(start_dir()) / base) if base else start_dir()


def remember(path: str | os.PathLike | None) -> None:
    """记住刚选中文件所在目录（供下次对话框起始位置）。"""
    if not path or _set_last_dir is None:
        return
    try:
        _set_last_dir(str(Path(path).parent))
    except Exception:
        pass
