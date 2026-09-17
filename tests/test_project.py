"""P6 项目存档与设置持久化测试。"""

from __future__ import annotations

import json
import os

import pytest

from writerstudio.core.document import Document, PageSpec, make_static_object
from writerstudio.core.geometry import AffineTransform
from writerstudio.core.sample import make_rect, make_star
from writerstudio.core.strokes import Stroke
from writerstudio.fonts.builder import TextSpec, make_text_object
from writerstudio.fonts.manager import FontManager
from writerstudio.machine.config import GCodeConfig
from writerstudio.machine.start_point import StartPoint
from writerstudio.project import (
    FILE_SUFFIX,
    FORMAT_ID,
    ProjectData,
    load_project,
    object_from_data,
    object_to_data,
    project_from_data,
    project_to_data,
    regenerate_all,
    save_project,
)
from writerstudio.settings import (
    Settings,
    _MemoryBackend,
)


# ============================================================ 序列化单元
def test_stroke_roundtrip():
    from writerstudio.project import stroke_from_data, stroke_to_data
    s = Stroke([(1.5, -2.25), (3.0, 4.125)], closed=True)
    back = stroke_from_data(stroke_to_data(s))
    assert back.closed
    assert back.points == [(1.5, -2.25), (3.0, 4.125)]


def test_object_roundtrip_static():
    obj = make_static_object([make_rect(0, 0, 10, 5)], name="矩形",
                             transform=AffineTransform.translate(3, 4))
    back = object_from_data(object_to_data(obj))
    assert back.name == "矩形"
    assert back.transform.as_tuple() == (1.0, 0.0, 0.0, 1.0, 3.0, 4.0)
    assert [s.points for s in back.local_strokes] == \
           [s.points for s in obj.local_strokes]
    assert back.source.kind == "static"


def test_object_roundtrip_preserves_source_data():
    from writerstudio.core.document import SourceSpec
    obj = make_static_object([make_star(0, 0, 5)])
    obj.source = SourceSpec("text", {"text": "AB", "font_names": ["futural"],
                                     "size": 12.0, "perturb": {"enabled": True}})
    back = object_from_data(object_to_data(obj))
    assert back.source.kind == "text"
    assert back.source.data["text"] == "AB"
    assert back.source.data["perturb"]["enabled"] is True


def test_project_roundtrip_page_and_machine():
    doc = Document(page=PageSpec(180.0, 120.0, 7.0))
    doc.add(make_static_object([make_rect(0, 0, 5, 5)]))
    cfg = GCodeConfig(pen_mode="m3m5", draw_feed=2200, origin_offset=(1.5, 2.5))
    proj = ProjectData(document=doc, machine_config=cfg,
                       start_point=StartPoint("canvas", (11.0, 22.0)),
                       search_dirs=["/a/b"], preferred_fonts=["futural"])
    data = project_to_data(proj)
    assert data["format"] == FORMAT_ID
    back = project_from_data(data)
    assert back.document.page.width == 180.0
    assert back.document.page.margin == 7.0
    assert len(back.document.objects) == 1
    assert back.machine_config.pen_mode == "m3m5"
    assert back.machine_config.draw_feed == 2200
    assert back.machine_config.origin_offset == (1.5, 2.5)
    assert back.start_point.mode == "canvas"
    assert back.start_point.point == (11.0, 22.0)
    assert back.search_dirs == ["/a/b"]


def test_save_load_file(tmp_path):
    doc = Document()
    doc.add(make_static_object([make_star(0, 0, 10)], name="星"))
    proj = ProjectData(document=doc)
    p = tmp_path / "my"
    save_project(p, proj)          # 自动补后缀
    out = tmp_path / f"my{FILE_SUFFIX}"
    assert out.exists()
    back = load_project(out)
    assert len(back.document.objects) == 1
    assert back.document.objects[0].name == "星"


def test_corrupt_object_is_skipped_not_fatal():
    """单个对象损坏（坏点/transform 长度不对）只跳过它，项目仍能打开。"""
    good = object_to_data(make_static_object([make_rect(0, 0, 5, 5)], name="好"))
    bad_pts = dict(good)
    bad_pts["name"] = "坏点"
    bad_pts["strokes"] = [{"pts": [[1, 2], [3]], "closed": False}]  # 点缺 y
    bad_tf = dict(good)
    bad_tf["name"] = "坏transform"
    bad_tf["transform"] = [1, 0, 0, 1]     # 只有 4 个分量
    data = {
        "format": FORMAT_ID, "version": 1,
        "page": {"width": 297.0, "height": 210.0},
        "objects": [bad_pts, good, bad_tf],
    }
    back = project_from_data(data)
    names = [o.name for o in back.document.objects]
    assert names == ["好"]


def test_saved_file_is_valid_json(tmp_path):
    proj = ProjectData(document=Document())
    p = tmp_path / "x.wsproj"
    save_project(p, proj)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["format"] == FORMAT_ID
    assert data["version"] == 1


def test_load_rejects_wrong_format(tmp_path):
    p = tmp_path / "bad.wsproj"
    p.write_text(json.dumps({"format": "other", "version": 1}))
    with pytest.raises(ValueError):
        load_project(p)


def test_load_rejects_future_version(tmp_path):
    p = tmp_path / "future.wsproj"
    p.write_text(json.dumps({"format": FORMAT_ID, "version": 999}))
    with pytest.raises(ValueError):
        load_project(p)


def test_coordinate_precision_within_tolerance(tmp_path):
    obj = make_static_object([make_star(0.123456, 0.654321, 7.5)])
    doc = Document()
    doc.add(obj)
    p = tmp_path / "prec.wsproj"
    save_project(p, ProjectData(document=doc))
    back = load_project(p)
    a = obj.local_strokes[0].points
    b = back.document.objects[0].local_strokes[0].points
    max_err = max(max(abs(x1 - x2), abs(y1 - y2))
                  for (x1, y1), (x2, y2) in zip(a, b))
    assert max_err < 1e-3   # 4 位小数舍入，远小于书写精度


# ------------------------------------------------ 打开后重新生成
def test_regenerate_all_text_object():
    m = FontManager(user_font_dir="/nonexistent-xyz")
    doc = Document()
    obj = make_text_object(TextSpec(text="AB", font_names=["futural"], size=10), m)
    doc.add(obj)
    proj = ProjectData(document=doc)
    data = project_from_data(project_to_data(proj))
    n = regenerate_all(data, m)
    assert n == 1
    assert data.document.objects[0].local_strokes


def test_regenerate_keeps_strokes_when_font_missing():
    """字体缺失时应保留已存笔画，不破坏内容。"""
    from writerstudio.core.document import SourceSpec
    doc = Document()
    obj = make_static_object([make_rect(0, 0, 10, 10)], name="文本")
    obj.source = SourceSpec("text", {"text": "AB", "font_names": ["不存在的字体"],
                                     "size": 10})
    saved = [s.points for s in obj.local_strokes]
    doc.add(obj)
    m = FontManager(user_font_dir="/nonexistent-xyz")
    proj = project_from_data(project_to_data(ProjectData(document=doc)))
    regenerate_all(proj, m)
    assert [s.points for s in proj.document.objects[0].local_strokes] == saved


def test_regenerate_ignores_unknown_kind():
    doc = Document()
    doc.add(make_static_object([make_rect(0, 0, 1, 1)]))  # static
    proj = ProjectData(document=doc)
    m = FontManager(user_font_dir="/nonexistent-xyz")
    assert regenerate_all(proj, m) == 0


# ============================================================ 设置
def test_settings_basic_types():
    s = Settings(_MemoryBackend())
    s.set("a", 5)
    assert s.get_int("a") == 5
    s.set("b", "3.5")
    assert s.get_float("b") == 3.5
    s.set("c", True)
    assert s.get_bool("c") is True
    assert s.get_bool("missing", True) is True


def test_settings_font_dirs():
    s = Settings(_MemoryBackend())
    s.add_font_search_dir("/x")
    s.add_font_search_dir("/y")
    s.add_font_search_dir("/x")      # 去重
    assert s.font_search_dirs() == ["/x", "/y"]
    s.remove_font_search_dir("/x")
    assert s.font_search_dirs() == ["/y"]


def test_settings_last_font_chain_roundtrip():
    """上次使用的字体链可持久化（新建文本/内容时自动恢复，免重复选择）。"""
    s = Settings(_MemoryBackend())
    assert s.last_font_chain() == []          # 缺省为空
    s.set_last_font_chain(["手写真迹", "futural"])
    assert s.last_font_chain() == ["手写真迹", "futural"]
    # 空串被过滤，避免写入无效字体名
    s.set_last_font_chain(["", "futural", ""])
    assert s.last_font_chain() == ["futural"]


def test_settings_panel_state_roundtrip():
    """面板散项（点动步长/速度、连接选项、单步、休眠）可持久化。

    这些不属于 GCodeConfig，此前从不保存——用户每次重开都要重调点动步长/速度。
    """
    s = Settings(_MemoryBackend())
    s.set_panel_state(jog_step=7.5, jog_feed=3333, single_step=True,
                      sleep_after=True, dtr=False, rts=False)
    st = s.panel_state()
    assert st == {"jog_step": 7.5, "jog_feed": 3333, "single_step": True,
                  "sleep_after": True, "dtr": False, "rts": False}
    # 缺省值合理
    s2 = Settings(_MemoryBackend())
    d = s2.panel_state()
    assert d["jog_step"] == 10.0 and d["jog_feed"] == 2500
    assert d["dtr"] is True and d["rts"] is True


def test_pen_defaults_migration_from_legacy():
    """出厂遗留默认（m3m5 + 抬2/落0）一次性迁移到本机步进 Z 轴标定。"""
    s = Settings(_MemoryBackend())
    cfg = GCodeConfig(pen_mode="m3m5", pen_up_z=2.0, pen_down_z=0.0,
                      pen_z_feed=500.0)
    assert s.apply_machine_pen_defaults(cfg) is True
    assert cfg.pen_mode == "z"
    assert cfg.pen_up_z == 0.0
    assert cfg.pen_down_z == 6.0
    assert cfg.pen_z_feed == 3000.0


def test_pen_defaults_migration_fixes_inverted_z():
    """Z 模式但方向写反（抬6/落0、抬2/落0）也要迁移为正确的落6/抬0。"""
    for up in (6.0, 2.0):
        s = Settings(_MemoryBackend())
        cfg = GCodeConfig(pen_mode="z", pen_up_z=up, pen_down_z=0.0)
        assert s.apply_machine_pen_defaults(cfg) is True, up
        assert cfg.pen_up_z == 0.0 and cfg.pen_down_z == 6.0, up


def test_p40_write_defaults_migration():
    """P40 推荐书写参数一次性写入（Z 速/书写速度/斜抬/斜落），之后幂等。"""
    s = Settings(_MemoryBackend())
    cfg = GCodeConfig(pen_mode="z", pen_z_feed=3000.0, draw_feed=10100.0,
                      stroke_taper_mm=0.0, entry_taper_mm=0.0)
    assert s.apply_p40_write_defaults(cfg) is True
    assert cfg.pen_z_feed == 12000.0
    assert cfg.draw_feed == 4400.0
    assert cfg.stroke_taper_mm == 1.5
    assert cfg.entry_taper_mm == 0.5
    # 一次性：之后用户改成什么都不再覆盖
    cfg.pen_z_feed = 2000.0
    assert s.apply_p40_write_defaults(cfg) is False
    assert cfg.pen_z_feed == 2000.0


def test_pen_defaults_migration_is_one_shot():
    """迁移只做一次：用户主动选回 m3m5 不会再被覆盖。"""
    s = Settings(_MemoryBackend())
    cfg = GCodeConfig(pen_mode="m3m5", pen_up_z=2.0, pen_down_z=0.0)
    assert s.apply_machine_pen_defaults(cfg) is True      # 第一次迁移
    cfg2 = GCodeConfig(pen_mode="m3m5", pen_up_z=2.0, pen_down_z=0.0)
    assert s.apply_machine_pen_defaults(cfg2) is False    # 已迁移过，不再动
    assert cfg2.pen_mode == "m3m5"


def test_pen_defaults_migration_skips_custom_config():
    """用户已自定义（非遗留、方向正确）时不得改写。"""
    s = Settings(_MemoryBackend())
    cfg = GCodeConfig(pen_mode="z", pen_up_z=1.0, pen_down_z=3.0)
    assert s.apply_machine_pen_defaults(cfg) is False
    assert cfg.pen_down_z == 3.0
    # 遗留模式但 Z 值不同（用户已调过）同样不动
    s2 = Settings(_MemoryBackend())
    cfg2 = GCodeConfig(pen_mode="m3m5", pen_up_z=5.0, pen_down_z=1.0)
    assert s2.apply_machine_pen_defaults(cfg2) is False
    assert cfg2.pen_up_z == 5.0
    # Z 模式方向正确（落 > 抬）的自定义值不动
    s3 = Settings(_MemoryBackend())
    cfg3 = GCodeConfig(pen_mode="z", pen_up_z=0.0, pen_down_z=4.0)
    assert s3.apply_machine_pen_defaults(cfg3) is False
    assert cfg3.pen_down_z == 4.0


def test_settings_recent_files_order_and_limit():
    s = Settings(_MemoryBackend())
    for i in range(12):
        s.add_recent_file(f"/f{i}")
    recents = s.recent_files()
    assert len(recents) == 10            # 上限
    assert recents[0] == "/f11"          # 最新在前
    s.add_recent_file("/f5")             # 再次打开去重并置顶
    assert s.recent_files()[0] == "/f5"
    assert s.recent_files().count("/f5") == 1
    s.clear_recent_files()
    assert s.recent_files() == []


def test_settings_machine_roundtrip():
    s = Settings(_MemoryBackend())
    cfg = GCodeConfig(pen_mode="m7m9", pen_up_z=3.0, pen_down_z=-0.5,
                      pen_down_s=750, draw_feed=1800, travel_feed=4000)
    sp = StartPoint("canvas", (12.5, 34.5))
    s.save_machine(cfg, sp)
    cfg2 = GCodeConfig()
    sp2 = StartPoint()
    s.load_machine_into(cfg2, sp2)
    assert cfg2.pen_mode == "m7m9"
    assert cfg2.pen_up_z == 3.0
    assert cfg2.pen_down_s == 750
    assert cfg2.draw_feed == 1800
    assert sp2.mode == "canvas"
    assert sp2.point == (12.5, 34.5)


def test_settings_full_config_overrides_legacy_keys():
    """有 config_json 时散键不得反向覆盖。

    散键只是保存时同步写的旧版可读镜像；若加载时让它生效，后改的
    config_json（如抬落笔方式/深度）会被陈旧散键悄悄覆盖回去。
    """
    s = Settings(_MemoryBackend())
    s.set("machine/config_json", json.dumps(
        GCodeConfig(pen_mode="z", pen_up_z=0.0, pen_down_z=2.0).to_data()))
    s.set("machine/pen_mode", "m3m5")       # 旧版散键停留在过时值
    s.set("machine/pen_up_z", 2.0)
    s.set("machine/pen_down_z", 0.0)
    cfg = GCodeConfig()
    s.load_machine_into(cfg, StartPoint())
    assert cfg.pen_mode == "z"
    assert cfg.pen_up_z == 0.0
    assert cfg.pen_down_z == 2.0


def test_settings_legacy_keys_used_without_full_config():
    """无 config_json 的旧版数据仍走散键回退。"""
    s = Settings(_MemoryBackend())
    s.set("machine/pen_mode", "m7m9")
    s.set("machine/pen_up_z", 4.0)
    s.set("machine/pen_down_z", -1.0)
    cfg = GCodeConfig()
    s.load_machine_into(cfg, StartPoint())
    assert cfg.pen_mode == "m7m9"
    assert cfg.pen_up_z == 4.0
    assert cfg.pen_down_z == -1.0


def test_settings_machine_full_config_roundtrip():
    """模式专属参数与轴映射也必须持久化（此前只存 6 个散键）。"""
    s = Settings(_MemoryBackend())
    cfg = GCodeConfig(pen_mode="servo-angle", pen_up_s=50, pen_down_angle=30,
                      laser_power=800, swap_xy=True, invert_x=True,
                      origin_corner="left-top",
                      custom_start_gcode="G28; M3 S10")
    s.save_machine(cfg, StartPoint())
    cfg2 = GCodeConfig()
    s.load_machine_into(cfg2, StartPoint())
    assert cfg2.pen_mode == "servo-angle"
    assert cfg2.pen_up_s == 50
    assert cfg2.pen_down_angle == 30
    assert cfg2.laser_power == 800
    assert cfg2.swap_xy is True
    assert cfg2.invert_x is True
    assert cfg2.origin_corner == "left-top"
    assert cfg2.custom_start_gcode == "G28; M3 S10"


def test_settings_page_size():
    s = Settings(_MemoryBackend())
    s.set_page_size(320.0, 240.0)
    assert s.page_size() == (320.0, 240.0)


def test_memory_backend_isolation():
    s1 = Settings(_MemoryBackend())
    s2 = Settings(_MemoryBackend())
    s1.set("k", 1)
    assert s2.get("k") is None


# ============================================================ UI 集成
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _mem_settings():
    return Settings(_MemoryBackend())


def _make_window(settings=None):
    """构造主窗口并关闭关闭确认（无头测试避免模态框阻塞）。"""
    from writerstudio.ui.main_window import MainWindow
    win = MainWindow(settings=settings or _mem_settings())
    win.confirm_on_close = False
    win.resize(900, 600)
    win.show()
    return win



def test_main_window_new_and_save(qapp, tmp_path):
    win = _make_window()
    qapp.processEvents()

    # 新建清空文档
    win._new_project()
    assert len(win.controller.doc) == 0

    # 加个对象 → 应标记为脏
    win.controller.add_object(make_static_object([make_rect(0, 0, 10, 10)]))
    qapp.processEvents()
    assert win._dirty

    # 保存到指定路径
    path = str(tmp_path / "proj.wsproj")
    win.project_path = path
    assert win._save_project()
    assert not win._dirty
    from pathlib import Path
    assert Path(path).exists()
    win.close()


def test_main_window_open_restores(qapp, tmp_path):
    # 先造一个项目文件
    doc = Document(page=PageSpec(150.0, 100.0))
    doc.add(make_static_object([make_star(0, 0, 8)], name="星星",
                               transform=AffineTransform.translate(20, 20)))
    proj = ProjectData(document=doc,
                       machine_config=GCodeConfig(pen_mode="m3m5", draw_feed=1234),
                       start_point=StartPoint("origin", (0, 0)))
    p = tmp_path / "open.wsproj"
    save_project(p, proj)

    win = _make_window()
    qapp.processEvents()
    assert win._load_project_path(str(p))
    assert len(win.controller.doc) == 1
    assert win.controller.doc.objects[0].name == "星星"
    assert win.controller.doc.page.width == 150.0
    assert win.machine_panel.current_config().pen_mode == "m3m5"
    assert not win._dirty
    # 最近文件已记录
    assert str(p) in win.settings.recent_files()
    win.close()


def test_main_window_persists_settings(qapp, tmp_path):
    backend = _MemoryBackend()
    s = Settings(backend)
    win = _make_window(s)
    qapp.processEvents()
    win.machine_panel.draw_feed.setValue(1666.0)
    win.machine_panel.pen_mode.setCurrentIndex(
        win.machine_panel.pen_mode.findData("m7m9"))
    win._persist_settings()
    # 新窗口读取同一后端
    s2 = Settings(backend)
    assert s2.get_float("machine/draw_feed") == 1666.0
    assert s2.get("machine/pen_mode") == "m7m9"
    win.close()


def test_main_window_restores_font_dirs(qapp, tmp_path):
    # 造一个含 gfont 的目录（用一个内置 jhf 冒充，测目录扫描）
    import shutil
    from writerstudio.fonts.manager import BUILTIN_DIR
    d = tmp_path / "fonts"
    d.mkdir()
    src = BUILTIN_DIR / "hershey" / "futural.jhf"
    shutil.copy(src, d / "myfont.jhf")

    backend = _MemoryBackend()
    s = Settings(backend)
    s.add_font_search_dir(str(d))
    win = _make_window(s)
    qapp.processEvents()
    names = win.font_manager.names()
    assert "myfont" in names
    win.close()


# ============================================================ P6-e 易用性
def test_edit_object_routes_static(qapp, monkeypatch):
    """双击静态图形应提示无可编辑源，而非崩溃。"""
    win = _make_window()
    qapp.processEvents()
    infos = []
    monkeypatch.setattr("writerstudio.ui.main_window.QMessageBox.information",
                        lambda *a, **k: infos.append(a))
    obj = make_static_object([make_rect(0, 0, 5, 5)], name="图形")
    win._edit_object(obj)
    assert infos
    win.close()


def test_edit_object_text_flow(qapp, monkeypatch):
    from writerstudio.ui.text_dialog import TextEditDialog
    win = _make_window()
    qapp.processEvents()
    obj = make_text_object(TextSpec(text="AB", font_names=["futural"], size=10),
                           win.font_manager)
    w0 = obj.local_bbox().width

    def fake_exec(self):
        self.text_edit.setPlainText("ABCDEFG")
        return TextEditDialog.Accepted

    monkeypatch.setattr(TextEditDialog, "exec", fake_exec)
    win._edit_object(obj)
    assert obj.local_bbox().width > w0
    assert obj.source.data["text"] == "ABCDEFG"
    win.close()


def test_object_activated_callback(qapp, monkeypatch):
    """双击信号应触发主窗口的编辑路由。"""
    win = _make_window()
    qapp.processEvents()
    called = []
    monkeypatch.setattr(win, "_edit_object", lambda o: called.append(o))
    obj = make_static_object([make_rect(0, 0, 5, 5)], name="图形")
    win.controller.add_object(obj)
    win.controller.on_object_activated(obj)
    assert called and called[0] is obj
    win.close()


def test_undo_panel_reflects_stack(qapp):
    win = _make_window()
    qapp.processEvents()
    win._new_project()
    win.controller.add_object(make_static_object([make_rect(0, 0, 10, 10)]))
    qapp.processEvents()
    # 初始状态 + 1 步
    assert win.undo_panel.list.count() == 2
    assert win.controller.undo_stack.index() == 1
    win.close()


def test_content_dialog_load_from_object(qapp):
    from writerstudio.ui.content_dialog import ContentDialog, KIND_MARKDOWN
    from writerstudio.content.builder import make_markdown_object
    win = _make_window()
    qapp.processEvents()
    obj = make_markdown_object("# 标题\n\n内容 $x^2$", win.font_manager,
                               font_names=["futural"])
    dlg = ContentDialog(win.font_manager, initial_kind=KIND_MARKDOWN)
    dlg.load_from_object(obj)
    assert "# 标题" in dlg.md_edit.toPlainText()
    assert dlg.payload().data["text"].startswith("# 标题")
    dlg.close()
    win.close()


def test_frame_geometry_persisted_for_reopen():
    """框几何持久化：重开文档且重新生成产出空笔画时，框不被冲掉。

    回归：文本框/表格框存在不序列化的 meta 里，只有重生成成功才回填；
    字体链解析不出字形时笔画回滚保留（内容正常）而框几何丢失，画布把
    调整框画成 10×4mm 兜底——「内容正常、调整框大小不正常」。
    """
    from writerstudio.content.builder import make_markdown_object
    from writerstudio.core.document import SourceSpec
    from writerstudio.project import (
        ProjectData,
        project_from_data,
        project_to_data,
        regenerate_all,
    )

    m = FontManager(user_font_dir="/nonexistent-xyz")
    doc = Document()
    # 静态笔画 + 文本源：字形（中文）超出字体覆盖 → 重生成产出空笔画
    t = make_static_object([make_rect(0, 0, 30, 10)], name="文本")
    t.source = SourceSpec("text", {"text": "你好", "font_names": ["futural"],
                                   "size": 10,
                                   "layout_box": [0.0, 12.0, 40.0, 0.0]})
    md = make_markdown_object("| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n",
                              m, font_names=["futural"])
    assert md.source.data.get("table_box"), "渲染后应持久化表格外框"
    doc.add(t)
    doc.add(md)

    proj = project_from_data(project_to_data(ProjectData(document=doc)))
    # 文本对象重生成产出空笔画被回滚（不计数），Markdown 正常重生成
    assert regenerate_all(proj, m) == 1
    t2 = proj.document.objects[0]
    md2 = proj.document.objects[1]
    # 内容保留
    assert len(t2.local_strokes) == 1          # make_rect 的闭合方形笔画
    assert md2.local_strokes
    # 框几何在源数据里原样保留（未被失败尝试覆盖/清除）
    assert t2.source.data.get("layout_box") == [0.0, 12.0, 40.0, 0.0]
    assert md2.source.data.get("table_box") == \
        md.source.data["table_box"]
    # 回滚不得留下失败尝试的空排版
    assert t2.meta.get("layout") is None


def test_frame_geometry_survives_success_regen(qapp):
    from writerstudio.project import (
        ProjectData,
        project_from_data,
        project_to_data,
        regenerate_all,
    )

    m = FontManager(user_font_dir="/nonexistent-xyz")
    doc = Document()
    doc.add(make_text_object(TextSpec(text="Hello", font_names=["futural"],
                                      size=12, frame_width=50.0), m))
    obj = doc.objects[0]
    box0 = list(obj.source.data["layout_box"])
    proj = project_from_data(project_to_data(ProjectData(document=doc)))
    assert regenerate_all(proj, m) == 1
    obj2 = proj.document.objects[0]
    assert [float(v) for v in obj2.source.data["layout_box"]] == \
        pytest.approx(box0)
    assert obj2.meta.get("layout") is not None
