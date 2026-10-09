"""把解析大文件时产生的临时内存还给系统。

Python 释放的对象回到 pymalloc/glibc 的空闲池后并不会自动归还内核：
字体解析、公式排版这类"集中分配、随即释放"的活干完后，RSS 会保持在高位
（实测打开一款 8 MB 的字库后常驻不降）。在重活结束的边界上调一次
:func:`trim_memory` 就能把空闲页交还给系统。
"""

from __future__ import annotations

import gc
import sys


def trim_memory() -> None:
    """回收循环垃圾并（Linux/glibc）把空闲堆页还给内核。

    成本约几毫秒，适合放在「一批解析任务结束」这类低频率边界上，不要放进
    每次编辑/重绘的热路径。
    """
    gc.collect()
    if not sys.platform.startswith("linux"):
        return                     # malloc_trim 是 glibc 专有扩展
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass                       # musl/其它 libc：退化为仅 gc.collect()
