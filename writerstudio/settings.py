"""应用设置的持久化（基于 ``QSettings``）。

保存的项：
    * 外部字体搜索目录
    * 最近打开的项目文件
    * 机器默认参数（波特率、抬落笔、速度、写字起点）
    * 界面偏好（窗口尺寸、面板可见性）

为便于测试（无需真实 Qt 配置后端），:class:`Settings` 支持注入一个
``backend``；默认使用 ``QSettings``。后端需实现 ``value(key, default)``
与 ``setValue(key, value)``。
"""

from __future__ import annotations

from typing import Any, Optional

ORG = "WriterStudio"
APP = "WriterStudio"

K_FONT_DIRS = "fonts/search_dirs"
K_FONT_PINNED = "fonts/pinned"
K_FONT_HIDDEN = "fonts/hidden"
K_LAST_FONT_CHAIN = "fonts/last_chain"   # 上次使用的字体回退链（自动记忆）
K_RECENT = "files/recent"
K_LAST_DIR = "files/last_dir"
K_BAUD = "machine/baud"
K_JOG_STEP = "machine/jog_step"      # 手动点动步长(mm)
K_JOG_FEED = "machine/jog_feed"      # 手动点动速度(mm/min)
K_SINGLE_STEP = "machine/single_step"  # 单步发送
K_SLEEP_AFTER = "machine/sleep_after"  # 完成后休眠
K_DTR = "machine/dtr"                # 连接后 DTR 电平
K_RTS = "machine/rts"                # 连接后 RTS 电平
K_MACHINE_CONFIG = "machine/config_json"
K_MACHINE_PROFILE = "machine/profile_json"
K_PEN_DEFAULTS_V2 = "machine/pen_defaults_v3"   # 抬落笔默认一次性迁移标记
K_PEN_DEFAULTS_V4 = "machine/pen_defaults_v4"   # P40 书写参数一次性迁移标记
K_PEN_MODE = "machine/pen_mode"
K_PEN_UP_Z = "machine/pen_up_z"
K_PEN_DOWN_Z = "machine/pen_down_z"
K_PEN_S = "machine/pen_s"
K_DRAW_FEED = "machine/draw_feed"
K_TRAVEL_FEED = "machine/travel_feed"
K_START_MODE = "machine/start_mode"
K_START_X = "machine/start_x"
K_START_Y = "machine/start_y"
K_PEN_MARK_X = "machine/pen_mark_x"
K_PEN_MARK_Y = "machine/pen_mark_y"
K_PEN_MARK_ON = "machine/pen_mark_on"
K_PAGE_W = "page/width"
K_PAGE_H = "page/height"
K_PAGE_MARGIN = "page/margin"
K_PAGE_PRESET = "page/preset_name"
K_AI_ENABLED = "ai/mcp_enabled"    # AI 排版服务（MCP，仅本机回环）
K_AI_PORT = "ai/mcp_port"          # AI 排版服务端口
K_GEOMETRY = "ui/geometry"
K_UI_THEME = "ui/theme"
K_MAX_RECENT = 10


class _QSettingsBackend:
    def __init__(self) -> None:
        from PySide6.QtCore import QSettings
        self._s = QSettings(ORG, APP)

    def value(self, key: str, default: Any = None) -> Any:
        return self._s.value(key, default)

    def setValue(self, key: str, value: Any) -> None:
        self._s.setValue(key, value)

    def sync(self) -> None:
        self._s.sync()


class _MemoryBackend:
    """测试用内存后端。"""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def value(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def setValue(self, key: str, value: Any) -> None:
        self._d[key] = value

    def sync(self) -> None:
        pass


class Settings:
    """薄封装：提供类型安全的读写与列表辅助。"""

    def __init__(self, backend: Optional[Any] = None) -> None:
        self._b = backend if backend is not None else _QSettingsBackend()

    # ------------------------------------------------------------ 基础
    def get(self, key: str, default: Any = None) -> Any:
        v = self._b.value(key, default)
        return default if v is None else v

    def set(self, key: str, value: Any) -> None:
        self._b.setValue(key, value)

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key, default)
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return bool(v)

    def get_float(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(key, default)))
        except (TypeError, ValueError):
            return default

    def get_list(self, key: str) -> list[str]:
        v = self.get(key, [])
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v else []
        try:
            return [str(x) for x in v]
        except TypeError:
            return []

    def sync(self) -> None:
        try:
            self._b.sync()
        except Exception:
            pass

    # ------------------------------------------------------- 字体搜索目录
    def font_search_dirs(self) -> list[str]:
        return self.get_list(K_FONT_DIRS)

    def set_font_search_dirs(self, dirs: list[str]) -> None:
        self.set(K_FONT_DIRS, list(dict.fromkeys(d for d in dirs if d)))

    def add_font_search_dir(self, directory: str) -> list[str]:
        dirs = self.font_search_dirs()
        if directory and directory not in dirs:
            dirs.append(directory)
            self.set_font_search_dirs(dirs)
        return dirs

    def remove_font_search_dir(self, directory: str) -> list[str]:
        dirs = [d for d in self.font_search_dirs() if d != directory]
        self.set_font_search_dirs(dirs)
        return dirs

    # ----------------------------------------------------- 字体置顶/隐藏
    def font_pinned(self) -> list[str]:
        return self.get_list(K_FONT_PINNED)

    def set_font_pinned(self, names: list[str]) -> None:
        self.set(K_FONT_PINNED, list(dict.fromkeys(n for n in names if n)))

    def font_hidden(self) -> list[str]:
        return self.get_list(K_FONT_HIDDEN)

    def set_font_hidden(self, names: list[str]) -> None:
        self.set(K_FONT_HIDDEN, list(dict.fromkeys(n for n in names if n)))

    # ------------------------------------------------------- 上次使用的字体
    def last_font_chain(self) -> list[str]:
        """上次使用的字体回退链（新建文本/Markdown 时自动恢复，免重复选择）。"""
        return self.get_list(K_LAST_FONT_CHAIN)

    def set_last_font_chain(self, names: list[str]) -> None:
        self.set(K_LAST_FONT_CHAIN, [n for n in names if n])

    # ----------------------------------------------------------- 最近文件
    def recent_files(self) -> list[str]:
        return self.get_list(K_RECENT)

    def add_recent_file(self, path: str) -> list[str]:
        recents = [p for p in self.recent_files() if p != path]
        recents.insert(0, path)
        recents = recents[:K_MAX_RECENT]
        self.set(K_RECENT, recents)
        return recents

    def clear_recent_files(self) -> None:
        self.set(K_RECENT, [])

    def last_dir(self) -> str:
        return str(self.get(K_LAST_DIR, ""))

    def set_last_dir(self, directory: str) -> None:
        self.set(K_LAST_DIR, directory)

    # --------------------------------------------------------- 机器参数
    def save_machine(self, config, start_point) -> None:
        """保存机器配置与写字起点（接受 duck-typed 对象）。

        完整配置走 ``machine/config_json``（新键，覆盖全部字段，含轴映射、
        舵机角度、激光功率、自定义 G-code 等）；下面的散键继续写入仅为
        旧版本回退可读。
        """
        import json
        try:
            self.set(K_MACHINE_CONFIG, json.dumps(config.to_data()))
        except Exception:
            pass
        self.set(K_PEN_MODE, config.pen_mode)
        self.set(K_PEN_UP_Z, config.pen_up_z)
        self.set(K_PEN_DOWN_Z, config.pen_down_z)
        self.set(K_PEN_S, config.pen_down_s)
        self.set(K_DRAW_FEED, config.draw_feed)
        self.set(K_TRAVEL_FEED, config.travel_feed)
        self.set(K_START_MODE, start_point.mode)
        self.set(K_START_X, start_point.point[0])
        self.set(K_START_Y, start_point.point[1])
        marker = getattr(start_point, "pen_marker", None)
        if marker is not None:
            self.set(K_PEN_MARK_X, float(marker[0]))
            self.set(K_PEN_MARK_Y, float(marker[1]))
            self.set(K_PEN_MARK_ON, True)
        else:
            self.set(K_PEN_MARK_ON, False)

    def load_machine_into(self, config, start_point) -> None:
        """把已保存的机器参数写入给定的配置对象。"""
        import json
        from dataclasses import fields as dc_fields
        raw = self.get(K_MACHINE_CONFIG, "")
        loaded = False
        if raw:
            try:
                from .machine.config import GCodeConfig
                fresh = GCodeConfig.from_data(json.loads(raw))
                for f in dc_fields(GCodeConfig):
                    setattr(config, f.name, getattr(fresh, f.name))
                loaded = True
            except Exception:
                pass    # 损坏的 JSON：退回散键/默认值
        if not loaded:
            # 散键仅作旧版（无 config_json）数据的回退；有完整配置时绝不
            # 再套用散键——散键只是保存时的旧版可读镜像，若让它生效，
            # 后改的 config_json 会被陈旧散键悄悄覆盖回去（如抬落笔方式）。
            config.pen_mode = str(self.get(K_PEN_MODE, config.pen_mode))
            config.pen_up_z = self.get_float(K_PEN_UP_Z, config.pen_up_z)
            config.pen_down_z = self.get_float(K_PEN_DOWN_Z, config.pen_down_z)
            config.pen_down_s = self.get_int(K_PEN_S, config.pen_down_s)
            config.draw_feed = self.get_float(K_DRAW_FEED, config.draw_feed)
            config.travel_feed = self.get_float(K_TRAVEL_FEED, config.travel_feed)
        start_point.mode = str(self.get(K_START_MODE, start_point.mode))
        start_point.point = (
            self.get_float(K_START_X, start_point.point[0]),
            self.get_float(K_START_Y, start_point.point[1]),
        )
        if self.get_bool(K_PEN_MARK_ON, False):
            start_point.pen_marker = (
                self.get_float(K_PEN_MARK_X, 0.0),
                self.get_float(K_PEN_MARK_Y, 0.0),
            )

    def save_pen_marker(self, marker) -> None:
        if marker is None:
            self.set(K_PEN_MARK_ON, False)
            return
        self.set(K_PEN_MARK_X, float(marker[0]))
        self.set(K_PEN_MARK_Y, float(marker[1]))
        self.set(K_PEN_MARK_ON, True)

    def pen_marker_visible(self) -> bool:
        return self.get_bool(K_PEN_MARK_ON, False)

    # --------------------------------------------------- 机器能力档案
    def machine_profile_data(self) -> dict | None:
        """上次连接探测到的固件参数快照（``machine/profile_json``）。"""
        import json
        raw = self.get(K_MACHINE_PROFILE, "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def set_machine_profile_data(self, data: dict | None) -> None:
        import json
        if data is None:
            self.set(K_MACHINE_PROFILE, "")
            return
        try:
            self.set(K_MACHINE_PROFILE, json.dumps(data))
        except Exception:
            pass

    def baud(self, default: int = 115200) -> int:
        return self.get_int(K_BAUD, default)

    def set_baud(self, baud: int) -> None:
        self.set(K_BAUD, baud)

    # --------------------------------------------------- 抬落笔默认迁移
    def apply_machine_pen_defaults(self, config) -> bool:
        """一次性把出厂遗留/写反的抬落笔配置迁移到本机步进 Z 轴标定。

        命中其一即改写（只做一次，之后用户任何选择都不再覆盖）：

        * **遗留出厂默认**：``m3m5``（主轴 M3/M5）+ 抬笔 Z2 / 落笔 Z0 ——
          很多用户从没选过笔控方式，配置里一直是它，导致这台 Z 轴机器不动作。
        * **Z 模式但方向写反**：``z`` 模式下抬笔 Z 抬得比落笔还高
          （up>down，且为早期版本产生过的 2/0、6/0）——弹簧回位笔架应当
          **落笔更高（下压）**，写反会「抬笔时下压、落笔时离开纸面」。

        弹簧回位笔架的常见标定：落笔 6 / 抬笔 0 / Z 速度 3000。
        返回是否改写了配置。
        """
        if self.get_bool(K_PEN_DEFAULTS_V2, False):
            return False
        self.set(K_PEN_DEFAULTS_V2, True)
        m = config.pen_mode
        up, down = float(config.pen_up_z), float(config.pen_down_z)
        legacy = (m == "m3m5" and abs(up - 2.0) < 1e-9 and abs(down) < 1e-9)
        inverted = (m == "z" and up > down and abs(down) < 1e-9
                    and (abs(up - 2.0) < 1e-9 or abs(up - 6.0) < 1e-9))
        if not (legacy or inverted):
            return False
        config.pen_mode = "z"
        config.pen_up_z = 0.0
        config.pen_down_z = 6.0
        config.pen_z_feed = 3000.0
        return True

    def apply_p40_write_defaults(self, config) -> bool:
        """把 P40 实测推荐的书写参数一次性写入（只做一次，之后用户全权）。

        依据：连接实机探测 XY 加速度 3000、
        Z 速率上限 15000；对真实作业量化出「Z 抬落每笔固定 240ms 是最大
        开销」「进给 10100 受加速度限制根本跑不满」「末端满压停留 0.2~0.4s
        造成墨点」。据此迁移：

        * ``pen_z_feed`` 3000 → **12000**（固件上限内，Z 开销省 5 倍）；
        * ``draw_feed`` → **4400**（中位笔画可达峰速，再高是空转）；
        * 收笔斜抬 **1.5mm** / 入笔斜落 **0.5mm** 默认开启（去墨点）。

        与 v2 同策略：命中即整组改写一次并落标记，之后用户任何修改不再
        被覆盖。对没有 Z 轴的机器，斜抬参数会被生成端自动忽略，无损。
        """
        if self.get_bool(K_PEN_DEFAULTS_V4, False):
            return False
        self.set(K_PEN_DEFAULTS_V4, True)
        config.pen_z_feed = 12000.0
        config.draw_feed = 4400.0
        config.stroke_taper_mm = 1.5
        config.entry_taper_mm = 0.5
        return True

    # --------------------------------------------------- 机器面板散项
    def panel_state(self) -> dict:
        """手动点动/连接等面板参数（不属于 GCodeConfig，单独持久化）。

        这些值此前从不保存，用户每次重开软件都要重设——尤其点动步长/速度。
        """
        return {
            "jog_step": self.get_float(K_JOG_STEP, 10.0),
            "jog_feed": self.get_int(K_JOG_FEED, 2500),
            "single_step": self.get_bool(K_SINGLE_STEP, False),
            "sleep_after": self.get_bool(K_SLEEP_AFTER, False),
            "dtr": self.get_bool(K_DTR, True),
            "rts": self.get_bool(K_RTS, True),
        }

    def set_panel_state(self, *, jog_step: float, jog_feed: int,
                        single_step: bool, sleep_after: bool,
                        dtr: bool, rts: bool) -> None:
        self.set(K_JOG_STEP, float(jog_step))
        self.set(K_JOG_FEED, int(jog_feed))
        self.set(K_SINGLE_STEP, bool(single_step))
        self.set(K_SLEEP_AFTER, bool(sleep_after))
        self.set(K_DTR, bool(dtr))
        self.set(K_RTS, bool(rts))

    # --------------------------------------------------------------- 界面
    def window_geometry(self):
        return self.get(K_GEOMETRY, None)

    def set_window_geometry(self, geometry) -> None:
        if geometry is not None:
            self.set(K_GEOMETRY, geometry)

    def ui_theme(self) -> str:
        """界面主题（dark/light；缺省深色）。"""
        from .ui.theme import normalize_theme
        return normalize_theme(str(self.get(K_UI_THEME, "") or ""))

    def set_ui_theme(self, theme: str) -> None:
        self.set(K_UI_THEME, theme)

    def page_size(self, default=(297.0, 210.0)) -> tuple[float, float]:
        return (self.get_float(K_PAGE_W, default[0]),
                self.get_float(K_PAGE_H, default[1]))

    def set_page_size(self, w: float, h: float) -> None:
        self.set(K_PAGE_W, w)
        self.set(K_PAGE_H, h)

    def page_margin(self, default: float = 10.0) -> float:
        return self.get_float(K_PAGE_MARGIN, default)

    def set_page_margin(self, margin: float) -> None:
        self.set(K_PAGE_MARGIN, float(margin))

    def page_preset_name(self, default: str = "") -> str:
        return str(self.get(K_PAGE_PRESET, default) or "")

    def set_page_preset_name(self, name: str) -> None:
        self.set(K_PAGE_PRESET, name)

    # --------------------------------------------------- AI 排版服务（MCP）
    AI_PORT_DEFAULT = 8765

    def ai_service_enabled(self, default: bool = True) -> bool:
        """AI 排版服务是否随软件启动（仅监听本机回环，无外部暴露）。"""
        return self.get_bool(K_AI_ENABLED, default)

    def set_ai_service_enabled(self, on: bool) -> None:
        self.set(K_AI_ENABLED, bool(on))

    def ai_service_port(self) -> int:
        return self.get_int(K_AI_PORT, self.AI_PORT_DEFAULT)

    def set_ai_service_port(self, port: int) -> None:
        self.set(K_AI_PORT, int(port))
