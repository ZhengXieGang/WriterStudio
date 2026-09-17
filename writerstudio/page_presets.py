"""纸张预设：内置标准尺寸 + 用户自定义预设。

对应需求「增加 A4 纸张预设、增加纸张预设编辑器」。

* 内置预设覆盖 ISO A/B 系列、北美 Letter/Legal/Tabloid、照片/明信片等，
  统一以**纵向**尺寸（宽 ≤ 高）保存，界面里可一键切换横向。
* 用户预设保存在 :class:`~writerstudio.settings.Settings` 中（JSON 文本），
  可新增 / 修改 / 删除。

本模块为纯 Python，不依赖 Qt，便于单元测试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Optional

# 存储键（与 settings.py 保持一致；放在这里避免模块间循环依赖）
SETTINGS_KEY_PAGE_PRESETS = "page/presets"


@dataclass(frozen=True)
class PagePreset:
    """纸张预设（mm）。``builtin=True`` 的内置项不可删除/改名。"""

    name: str
    width: float
    height: float
    margin: float = 10.0
    builtin: bool = False

    def portrait(self) -> bool:
        return self.height >= self.width

    def oriented(self, landscape: bool) -> tuple[float, float]:
        """返回按方向调整后的 (宽, 高)。"""
        natural_landscape = not self.portrait()
        if landscape == natural_landscape:
            return (self.width, self.height)
        return (self.height, self.width)

    def to_data(self) -> dict:
        return {"name": self.name, "width": self.width, "height": self.height,
                "margin": self.margin}

    @classmethod
    def from_data(cls, d: dict) -> PagePreset:
        return cls(
            name=str(d.get("name", "自定义")),
            width=float(d.get("width", 210.0)),
            height=float(d.get("height", 297.0)),
            margin=float(d.get("margin", 10.0)),
        )


# ISO 216 名义尺寸（mm，纵向，四舍五入到整毫米；与纸厂标称一致）
_ISO_A = {
    0: (841, 1189), 1: (594, 841), 2: (420, 594), 3: (297, 420),
    4: (210, 297), 5: (148, 210), 6: (105, 148),
}
_ISO_B = {
    0: (1000, 1414), 1: (707, 1000), 2: (500, 707),
    3: (353, 500), 4: (250, 353), 5: (176, 250),
}


def _iso_a(n: int) -> tuple[float, float]:
    return (float(_ISO_A[n][0]), float(_ISO_A[n][1]))


def _iso_b(n: int) -> tuple[float, float]:
    return (float(_ISO_B[n][0]), float(_ISO_B[n][1]))


def builtin_presets() -> list[PagePreset]:
    """内置标准纸张（纵向，mm）。"""
    out: list[PagePreset] = []
    for n in range(0, 7):
        w, h = _iso_a(n)
        out.append(PagePreset(f"A{n}", w, h, 10.0, builtin=True))
    for n in range(0, 6):
        w, h = _iso_b(n)
        out.append(PagePreset(f"B{n}", w, h, 10.0, builtin=True))
    out.extend([
        PagePreset("Letter", 215.9, 279.4, 12.7, builtin=True),
        PagePreset("Legal", 215.9, 355.6, 12.7, builtin=True),
        PagePreset("Tabloid", 279.4, 431.8, 12.7, builtin=True),
        PagePreset("照片 4×6 英寸", 101.6, 152.4, 3.0, builtin=True),
        PagePreset("明信片 100×148", 100.0, 148.0, 5.0, builtin=True),
        PagePreset("正方形 200", 200.0, 200.0, 10.0, builtin=True),
    ])
    return out


BUILTIN_PRESETS: list[PagePreset] = builtin_presets()


class PagePresetStore:
    """内置 + 用户预设的统一入口，用户项由 ``settings`` 持久化。"""

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self._user: list[PagePreset] = []
        self._load()

    # --------------------------------------------------------------- 读写
    def _load(self) -> None:
        if self._settings is None:
            return
        raw = self._settings.get(SETTINGS_KEY_PAGE_PRESETS, "")
        if not raw:
            return
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return
        items: Iterable[dict] = data if isinstance(data, list) else []
        self._user = []
        for d in items:
            try:
                self._user.append(PagePreset.from_data(d))
            except Exception:
                continue

    def _save(self) -> None:
        if self._settings is None:
            return
        self._settings.set(SETTINGS_KEY_PAGE_PRESETS,
                           json.dumps([p.to_data() for p in self._user],
                                      ensure_ascii=False))

    # --------------------------------------------------------------- 查询
    def builtin(self) -> list[PagePreset]:
        return list(BUILTIN_PRESETS)

    def user(self) -> list[PagePreset]:
        return list(self._user)

    def all(self) -> list[PagePreset]:
        return self.builtin() + self.user()

    def names(self) -> list[str]:
        return [p.name for p in self.all()]

    def find(self, name: str) -> Optional[PagePreset]:
        for p in self.all():
            if p.name == name:
                return p
        return None

    def is_builtin(self, name: str) -> bool:
        return any(p.name == name for p in BUILTIN_PRESETS)

    # --------------------------------------------------------------- 增删改
    def add(self, preset: PagePreset) -> PagePreset:
        """新增用户预设；重名时覆盖同名用户预设。

        内置预设名不可被用户预设占用（否则列表会出现两个同名项、查找歧义）。
        """
        if self.is_builtin(preset.name):
            raise ValueError(f"「{preset.name}」是内置预设名，不能作为用户预设名")
        self.remove(preset.name, missing_ok=True)
        self._user.append(PagePreset(preset.name, preset.width, preset.height,
                                     preset.margin))
        self._save()
        return preset

    def update(self, old_name: str, preset: PagePreset) -> PagePreset:
        if self.is_builtin(old_name):
            if old_name == preset.name:
                # 内置项不可修改，同名更新视为无操作
                return preset
            # 内置项改名视为「另存为用户预设」
            return self.add(preset)
        for i, p in enumerate(self._user):
            if p.name == old_name:
                self._user[i] = PagePreset(preset.name, preset.width,
                                           preset.height, preset.margin)
                self._save()
                return preset
        return self.add(preset)

    def remove(self, name: str, *, missing_ok: bool = False) -> bool:
        if self.is_builtin(name):
            return False
        before = len(self._user)
        self._user = [p for p in self._user if p.name != name]
        changed = len(self._user) != before
        if changed:
            self._save()
        elif not missing_ok:
            return False
        return changed
