"""字体管理器：扫描内置/用户字库、按需解析、缓存、导入。

重字体（如 8MB 的中文单线 JSON）采用**懒加载**：启动时只登记元信息，
真正排版用到时才解析并缓存，避免拖慢启动。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .gcode_lib import parse_gcode_char_dir
from .gfont import KIND_GFONT, font_names_from_gfont, parse_gfont
from .hershey import parse_hershey_jhf
from .model import (
    KIND_GCODE,
    KIND_HERSHEY,
    KIND_STROKE_JSON,
    KIND_TRUETYPE,
    FontFamily,
)
from .stroke_json import parse_stroke_json
from .truetype import parse_truetype

BUILTIN_DIR = Path(__file__).resolve().parent / "builtin"

_KIND_BY_DIR = {
    "hershey": KIND_HERSHEY,
    "stroke-json": KIND_STROKE_JSON,
    "truetype": KIND_TRUETYPE,
    "gcode": KIND_GCODE,
    "gfont": KIND_GFONT,
}

_PARSERS: dict[str, Callable[[Path], FontFamily]] = {
    KIND_HERSHEY: parse_hershey_jhf,
    KIND_STROKE_JSON: parse_stroke_json,
    KIND_TRUETYPE: parse_truetype,
    KIND_GFONT: parse_gfont,
}


@dataclass
class FontEntry:
    """一个可用字体的元信息 + 懒加载句柄。"""

    name: str
    kind: str
    path: Path
    display_name: str = ""
    _loaded: Optional[FontFamily] = field(default=None, repr=False)
    _error: Optional[str] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = self.name

    @property
    def loaded(self) -> bool:
        return self._loaded is not None

    @property
    def error(self) -> Optional[str]:
        return self._error

    def load(self) -> FontFamily:
        """解析并缓存字体；失败时抛出异常并记录。"""
        if self._loaded is not None:
            return self._loaded
        try:
            if self.kind == KIND_GCODE:
                fam = parse_gcode_char_dir(self.path, name=self.name)
            else:
                parser = _PARSERS[self.kind]
                fam = parser(self.path, name=self.name)
            fam.display_name = self.display_name
            # 之前一次失败留下的错误信息必须清掉，否则字体已能加载
            # 而 entry.error 仍报错
            self._error = None
            self._loaded = fam
            return fam
        except Exception as exc:  # 记录失败原因，便于界面上报
            self._error = str(exc)
            raise

    def try_load(self) -> Optional[FontFamily]:
        try:
            return self.load()
        except Exception:
            return None


class FontManager:
    """管理内置与用户字库。"""

    def __init__(self, user_font_dir: Optional[str | Path] = None,
                 builtin_dir: Optional[str | Path] = None,
                 extra_dirs: Optional[list[str | Path]] = None) -> None:
        self.builtin_dir = Path(builtin_dir) if builtin_dir else BUILTIN_DIR
        self.user_dir = Path(user_font_dir) if user_font_dir else _default_user_dir()
        self._extra_dirs: list[Path] = [Path(d) for d in (extra_dirs or [])]
        self._entries: dict[str, FontEntry] = {}
        self._order: list[str] = []
        self._pinned: set[str] = set()   # 置顶字体（下拉/列表排最前）
        self._hidden: set[str] = set()   # 隐藏字体（仅界面不显示，文件保留）
        # 字体集版本号：字库内容（增删/重扫描）变化时 +1，供渲染缓存做
        # 失效判断（同一批字体下重渲染结果不变，版本不动即可复用）
        self.version: int = 0
        self.refresh()

    # --------------------------------------------------------------- 扫描
    def refresh(self) -> None:
        self._entries.clear()
        self._order.clear()
        self._scan_root(self.builtin_dir, source="builtin")
        self._scan_root(self.user_dir, source="user")
        for d in self._extra_dirs:
            self._scan_flat(d, source="external")
        self.version += 1

    def add_search_dir(self, directory: str | Path) -> int:
        """把一个外部目录加入扫描（支持标准结构或「一堆 .gfont」的平铺目录）。

        返回新发现的字体数量。
        """
        d = Path(directory)
        if not d.exists():
            raise FileNotFoundError(d)
        if d not in self._extra_dirs:
            self._extra_dirs.append(d)
        before = len(self._entries)
        self._scan_flat(d, source="external")
        if len(self._entries) != before:
            self.version += 1
        return len(self._entries) - before

    def remove_search_dir(self, directory: str | Path) -> bool:
        d = Path(directory)
        if d in self._extra_dirs:
            self._extra_dirs.remove(d)
            self.refresh()
            return True
        return False

    def _scan_flat(self, root: Path, source: str) -> None:
        """平铺目录扫描：直接在目录下查找已知字体扩展名。"""
        if not root.exists():
            return
        exts = {".jhf", ".json", ".ttf", ".otf", ".ttc", ".woff", ".woff2", ".gfont"}
        for p in sorted(root.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in exts:
                continue
            kind = _infer_kind(p)
            if kind is None:
                continue
            if p.stem in self._entries:
                continue
            display = p.stem
            if kind == KIND_GFONT:
                meta = font_names_from_gfont(p)
                if meta.get("family"):
                    display = f"{meta['family']}（{p.stem}）"
            self._register(p.stem, kind, p, source, display=display)

    def _scan_root(self, root: Path, source: str) -> None:
        if not root.exists():
            return
        for sub in sorted(root.iterdir()):
            if not sub.is_dir():
                continue
            kind = _KIND_BY_DIR.get(sub.name)
            if kind is None:
                continue
            if kind == KIND_GCODE:
                for d in sorted(p for p in sub.iterdir() if p.is_dir()):
                    self._register(d.stem, kind, d, source, display=f"{d.stem}（gcode）")
            else:
                exts = _exts_for(kind)
                for p in sorted(sub.iterdir()):
                    if p.is_file() and p.suffix.lower() in exts:
                        self._register(p.stem, kind, p, source)

    def _register(self, name: str, kind: str, path: Path, source: str,
                  display: str = "") -> None:
        name = _unique_name(name, self._entries)
        entry = FontEntry(name=name, kind=kind, path=path,
                          display_name=display or name)
        self._entries[name] = entry
        self._order.append(name)

    # --------------------------------------------------------------- 查询
    def names(self) -> list[str]:
        return list(self._order)

    def entries(self) -> list[FontEntry]:
        return [self._entries[n] for n in self._order]

    def entries_by_kind(self, kind: str) -> list[FontEntry]:
        return [e for e in self.entries() if e.kind == kind]

    def get_entry(self, name: str) -> Optional[FontEntry]:
        return self._entries.get(name)

    def get(self, name: str) -> Optional[FontFamily]:
        entry = self._entries.get(name)
        return entry.try_load() if entry else None

    def require(self, name: str) -> FontFamily:
        entry = self._entries.get(name)
        if entry is None:
            raise KeyError(f"未找到字体：{name}")
        return entry.load()

    # ----------------------------------------------------------- 置顶/隐藏
    def pin(self, name: str, on: bool = True) -> None:
        """置顶/取消置顶一款字体（列表与下拉中排最前，方便选常用字体）。"""
        if on:
            self._pinned.add(name)
            self._hidden.discard(name)
        else:
            self._pinned.discard(name)

    def is_pinned(self, name: str) -> bool:
        return name in self._pinned

    def pinned_names(self) -> list[str]:
        return [n for n in self._order if n in self._pinned]

    def hide(self, name: str, on: bool = True) -> None:
        """隐藏/恢复一款字体：只在界面中不再显示，**不删除任何文件**。"""
        if on:
            self._hidden.add(name)
            self._pinned.discard(name)
        else:
            self._hidden.discard(name)

    def is_hidden(self, name: str) -> bool:
        return name in self._hidden

    def hidden_names(self) -> list[str]:
        return [n for n in self._order if n in self._hidden]

    def visible_entries(self) -> list[FontEntry]:
        """可供选择的字体：隐藏的排除，置顶的排最前（各自保持原相对顺序）。"""
        entries = self.entries()
        pinned = [e for e in entries if e.name in self._pinned]
        rest = [e for e in entries
                if e.name not in self._pinned and e.name not in self._hidden]
        return pinned + rest

    # --------------------------------------------------------- 导入/删除
    def import_font(self, src: str | Path, kind: Optional[str] = None,
                    display_name: str = "") -> FontEntry:
        """把外部字体文件复制进用户字库并登记。

        ``kind`` 为 None 时按扩展名推断。返回新登记的条目。
        """
        src = Path(src)
        if not src.exists():
            raise FileNotFoundError(src)
        kind = kind or _infer_kind(src)
        if kind is None:
            raise ValueError(f"无法识别字体类型：{src.name}")

        dest_dir = self.user_dir / _dir_for(kind)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        # 同名避免覆盖：加序号
        i = 1
        while dest.exists() and dest.resolve() != src.resolve():
            dest = dest_dir / f"{src.stem}-{i}{src.suffix}"
            i += 1
        if dest.resolve() != src.resolve():
            shutil.copy2(src, dest)

        name = _unique_name(dest.stem, self._entries)
        entry = FontEntry(name=name, kind=kind, path=dest,
                          display_name=display_name or dest.stem)
        self._entries[name] = entry
        self._order.append(name)
        self.version += 1
        return entry

    def remove(self, name: str, delete_file: bool = True) -> bool:
        entry = self._entries.get(name)
        if entry is None:
            return False
        # 只允许删除用户字库中的字体，内置字体不可删
        try:
            is_user = self.user_dir.resolve() in entry.path.resolve().parents
        except Exception:
            is_user = False
        if delete_file and is_user:
            try:
                if entry.path.is_file():
                    entry.path.unlink()
                elif entry.path.is_dir():
                    shutil.rmtree(entry.path)
            except Exception:
                pass
        self._entries.pop(name, None)
        if name in self._order:
            self._order.remove(name)
        # 置顶/隐藏集合同步清理：否则之后导入同名 stem 的新字体会
        # 意外继承「隐藏」状态，在所有列表里凭空消失
        self._pinned.discard(name)
        self._hidden.discard(name)
        self.version += 1
        return True


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _exts_for(kind: str) -> set[str]:
    if kind == KIND_HERSHEY:
        return {".jhf"}
    if kind == KIND_STROKE_JSON:
        return {".json"}
    if kind == KIND_TRUETYPE:
        return {".ttf", ".otf", ".ttc", ".woff", ".woff2"}
    if kind == KIND_GFONT:
        return {".gfont"}
    return set()


def _dir_for(kind: str) -> str:
    for d, k in _KIND_BY_DIR.items():
        if k == kind:
            return d
    return "truetype"

def _infer_kind(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext == ".jhf":
        return KIND_HERSHEY
    if ext in (".ttf", ".otf", ".ttc", ".woff", ".woff2"):
        return KIND_TRUETYPE
    if ext == ".json":
        return KIND_STROKE_JSON
    if ext == ".gfont":
        return KIND_GFONT
    if ext in (".nc", ".gcode", ".ngc"):
        return KIND_GCODE
    return None


def _unique_name(base: str, existing: dict) -> str:
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def _default_user_dir() -> Path:
    return Path.home() / ".writerstudio" / "fonts"
