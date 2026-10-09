"""崩溃/意外终止后的文档恢复。

把「有未保存修改的文档」定期快照到 ``~/.writerstudio/recovery-<pid>.wsproj``，
下次启动时若发现快照就问一句是否恢复。此前进程被外部杀掉（容器回收、
nohup 会话被结束、断电）会直接丢掉内存里的整份文档——这类损失只能靠
事后从别处补，代价很高。

**每个实例一份快照**（按 PID 命名）：两个实例共用同一个 ``recovery.wsproj``
时后写的会把先写的整份盖掉，崩溃时就只剩一份（甚至谁都没得恢复）。启动时
取**最新**的一份，属主已退出的遗留文件顺手清掉，别的活实例那几份保留。

快照与 ``.wsproj`` 完全同格式（走同一个 ``save_project``/``load_project``
路径），所以「恢复」就是一次普通打开；正常保存或正常退出时删除本实例那份。

路径可用环境变量 ``WRITERSTUDIO_HOME`` 覆盖（测试与隔离实例用）。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

from .project import ProjectData, load_project, save_project

#: 快照文件名里的属主 PID（``recovery-1234.wsproj``）
_PID_RE = re.compile(r"^recovery-(\d+)\.wsproj$")


def home_dir() -> Path:
    override = os.environ.get("WRITERSTUDIO_HOME")
    if override:
        return Path(override)
    return Path.home() / ".writerstudio"


def recovery_path() -> Path:
    """旧版单文件快照路径（仍会被 :func:`snapshots` 认出来并清理）。"""
    return home_dir() / "recovery.wsproj"


def instance_path() -> Path:
    """本进程的快照路径：每实例一份，互不覆盖。"""
    return home_dir() / f"recovery-{os.getpid()}.wsproj"


def snapshots() -> list[Path]:
    """所有快照文件，新 → 旧。"""
    try:
        files = [p for p in home_dir().glob("recovery*.wsproj") if p.is_file()]
    except OSError:
        return []

    def mtime(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0

    return sorted(files, key=mtime, reverse=True)


def _owner_alive(path: Path) -> bool:
    """快照的属主进程是否还活着（旧单文件快照无从判断，当作已退出）。"""
    m = _PID_RE.match(path.name)
    if m is None:
        return False
    pid = int(m.group(1))
    if pid == os.getpid():
        return False
    if sys.platform.startswith("linux"):
        cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            raw = cmdline.read_bytes()
        except OSError:
            return False            # 没有该进程（或已换成别的程序）
        if not raw:
            # 进程刚 fork 出来、cmdline 还没写好：宁可信其活着，别删人家快照
            return True
        return b"writerstudio" in raw.lower()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _first_nonempty() -> Optional[Path]:
    for p in snapshots():
        try:
            if p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


def write(data: ProjectData) -> bool:
    """写入本实例的快照（:func:`save_project` 自己先写临时文件再替换，原子）。"""
    try:
        path = instance_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        save_project(str(path), data)
        return True
    except Exception:
        return False


def exists() -> bool:
    return _first_nonempty() is not None


def newest() -> Optional[Path]:
    """最新一份非空快照的路径（没有则为 None；界面提示里给用户看）。"""
    return _first_nonempty()


def load() -> ProjectData:
    """载入最新的快照（多实例/多次崩溃时取最近写的那份）。"""
    path = _first_nonempty()
    if path is None:
        raise FileNotFoundError("没有可用的恢复快照")
    return load_project(str(path))


def clear() -> None:
    """清掉本实例的快照与属主已退出的遗留快照（别的活实例那几份保留）。"""
    for p in snapshots():
        if p == instance_path() or not _owner_alive(p):
            try:
                p.unlink()
            except OSError:
                pass
