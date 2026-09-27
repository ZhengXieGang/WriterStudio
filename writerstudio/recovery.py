"""崩溃/意外终止后的文档恢复。

把「有未保存修改的文档」定期快照到 ``~/.writerstudio/recovery.wsproj``，
下次启动时若有该文件就问一句是否恢复。此前进程被外部杀掉（容器回收、
nohup 会话被结束、断电）会直接丢掉内存里的整份文档——这类损失只能靠
事后从别处补，代价很高。

快照与 ``.wsproj`` 完全同格式（走同一个 ``save_project``/``load_project``
路径），所以「恢复」就是一次普通打开；正常保存或正常退出时删除该文件。

路径可用环境变量 ``WRITERSTUDIO_HOME`` 覆盖（测试与隔离实例用）。
"""

from __future__ import annotations

import os
from pathlib import Path

from .project import ProjectData, load_project, save_project


def home_dir() -> Path:
    override = os.environ.get("WRITERSTUDIO_HOME")
    if override:
        return Path(override)
    return Path.home() / ".writerstudio"


def recovery_path() -> Path:
    return home_dir() / "recovery.wsproj"


def write(data: ProjectData) -> bool:
    """原子写入快照（先写临时文件再替换，避免写到一半被杀留下半份文件）。"""
    path = recovery_path()
    # 临时文件必须保留 .wsproj 后缀：save_project 会把非 .wsproj 的路径
    # 改写扩展名，用 "*.wsproj.tmp" 会写飞、os.replace 再找不到源文件
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_project(str(tmp), data)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def exists() -> bool:
    try:
        return recovery_path().stat().st_size > 0
    except OSError:
        return False


def load() -> ProjectData:
    return load_project(str(recovery_path()))


def clear() -> None:
    """删除快照（正常保存/正常退出后调用；不存在时静默）。"""
    try:
        recovery_path().unlink()
    except OSError:
        pass
