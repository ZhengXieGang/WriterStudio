"""同一项目文件被多个实例同时打开时的登记与提醒。

实例之间没有 IPC：每个实例在 ``~/.writerstudio/open/<key>.json`` 里登记
自己的 (pid, 进程启动时刻, 路径)。打开/保存前看一眼有没有**别的活着的
进程**也登记了同一个文件，有就提醒一句——两边各编各的，后保存的那份会
把前面整份盖掉（0.2.0 实测踩过：界面里做的改动整份消失）。

进程启动时刻用于排除「PID 被复用」的假警报；登记随实例退出删除，异常
退出留下的陈旧登记（进程已不存在）直接忽略。目录跟随 ``WRITERSTUDIO_HOME``
（与恢复快照同源），测试与隔离实例不会互相污染。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .recovery import home_dir


def _dir() -> Path:
    return home_dir() / "open"


def _key(path: str | Path) -> str:
    real = os.path.realpath(str(path))
    return hashlib.sha1(real.encode("utf-8", "replace")).hexdigest()[:16]


def _proc_start(pid: int) -> Optional[float]:
    """Linux 下取进程启动时刻（/proc/<pid>/stat 的 starttime，jiffies）。"""
    if not sys.platform.startswith("linux"):
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return float(text.rsplit(") ", 1)[1].split()[19])
    except Exception:
        return None


def _alive(info: dict) -> bool:
    """登记的属主进程是否还活着（且确实是本程序，排除 PID 复用）。"""
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform.startswith("linux"):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        if not cmdline:
            return True             # 刚启动、cmdline 未就绪：别误判成已退出
        if b"writerstudio" not in cmdline.lower():
            return False
        start = _proc_start(pid)
        stored = info.get("start")
        # 双方都能取到启动时刻时用它确认不是「同 PID 的另一进程」
        return start is None or stored is None or float(stored) == start
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _sweep() -> None:
    """顺手清掉属主已退出的陈旧登记（崩溃残留），目录不会无限长大。"""
    try:
        files = list(_dir().glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(info, dict) and not _alive(info):
            try:
                f.unlink()
            except OSError:
                pass


def claim(path: str | Path) -> None:
    """登记本实例打开了 ``path``（覆盖陈旧登记）。"""
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        _sweep()
        (_dir() / f"{_key(path)}.json").write_text(json.dumps({
            "pid": os.getpid(),
            "start": _proc_start(os.getpid()),
            "path": os.path.realpath(str(path)),
            "time": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass                      # 登记失败不影响正常读写


def release(path: str | Path) -> None:
    """撤销本实例的登记（只删自己写的那份）。"""
    f = _dir() / f"{_key(path)}.json"
    try:
        info = json.loads(f.read_text(encoding="utf-8"))
        if int(info.get("pid") or 0) == os.getpid():
            f.unlink()
    except Exception:
        pass


def others(path: str | Path) -> list[dict]:
    """其它活着的实例对该文件的登记（本进程的与陈旧的都不算）。"""
    try:
        info = json.loads((_dir() / f"{_key(path)}.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    return [info] if isinstance(info, dict) and _alive(info) else []
