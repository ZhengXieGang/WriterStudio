"""AI 排版工具层测试：坐标语义、内容生成、布局检查、可撤销、存盘/导出。"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ai.tools import AiTools, ToolError  # noqa: E402
from writerstudio.settings import K_AI_ENABLED, Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def win(qapp):
    """关闭 AI 服务的主窗口（本文件直接测工具层，不占端口）。"""
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    w = MainWindow(st)
    yield w
    w.close()


@pytest.fixture()
def tools(win):
    return AiTools(win)


# --------------------------------------------------------------- 页面与查询
def test_get_page_info_semantics(tools):
    r = tools.call("get_page_info", {})
    assert r["width_mm"] == pytest.approx(297.0)
    assert r["height_mm"] == pytest.approx(210.0)
    assert "左上" in r["coords"]           # AI 语义固定是左上原点
    assert r["object_count"] == 0


def test_set_page(tools):
    tools.call("set_page", {"width": 210, "height": 297, "margin": 8})
    r = tools.call("get_page_info", {})
    assert r["width_mm"] == pytest.approx(210.0)
    assert r["height_mm"] == pytest.approx(297.0)
    assert r["margin_mm"] == pytest.approx(8.0)
    tools.win.controller.undo()            # 可撤销
    assert tools.call("get_page_info", {})["width_mm"] == pytest.approx(297.0)


def test_list_fonts_names_valid(tools, win):
    r = tools.call("list_fonts", {})
    names = [f["name"] for f in r["fonts"]]
    assert names
    assert set(names) <= {e.name for e in win.font_manager.visible_entries()}


# --------------------------------------------------------------- 文本
def test_add_text_top_left_anchor_and_bbox(tools, win):
    r = tools.call("add_text", {"text": "测试标题", "x": 30, "y": 20,
                                "size": 6})
    obj = win.controller.doc.find(r["id"])
    assert obj is not None
    assert obj.source.kind == "text"
    # bbox 左上角应落在 (30, 20)（AI 语义）
    assert r["bbox"]["x"] == pytest.approx(30, abs=0.5)
    assert r["bbox"]["y"] == pytest.approx(20, abs=0.5)
    assert r["bbox"]["w"] > 0 and r["bbox"]["h"] > 0
    assert r["fits_page"] is True
    # 内部坐标系是 y 向上：内部包围盒顶 = page.height - 20
    box = obj.world_bbox()
    assert box.y1 == pytest.approx(win.controller.doc.page.height - 20, abs=0.5)


def test_add_text_unknown_font_lists_available(tools, win):
    avail = [e.name for e in win.font_manager.visible_entries()]
    with pytest.raises(ToolError) as ei:
        tools.call("add_text", {"text": "字", "font_names": ["不存在的字体"]})
    msg = str(ei.value)
    assert "不存在的字体" in msg
    assert avail[0] in msg                 # 错误里带可用字体线索


def test_add_text_empty_rejected(tools):
    with pytest.raises(ToolError):
        tools.call("add_text", {"text": "   "})
    with pytest.raises(ToolError):
        tools.call("add_text", {"text": "ok", "align": "middle"})


def test_add_text_autoplace_no_overlap(tools, win):
    r1 = tools.call("add_text", {"text": "第一块内容", "size": 6})
    r2 = tools.call("add_text", {"text": "第二块内容", "size": 6})
    assert r1["bbox"] != r2["bbox"]        # 自动错开
    assert not tools.call("check_layout", {})["issues"]


def test_update_text_regenerates_and_undoes(tools, win):
    r = tools.call("add_text", {"text": "旧文字", "size": 6, "x": 20, "y": 20})
    n_before = len(win.controller.doc.find(r["id"]).local_strokes)
    r2 = tools.call("update_text", {"object_id": r["id"], "text": "换成全新的长一点文字",
                                    "size": 8})
    obj = win.controller.doc.find(r["id"])
    assert obj.source.data["text"] == "换成全新的长一点文字"
    assert obj.source.data["size"] == pytest.approx(8.0)
    assert r2["bbox"]["w"] > r["bbox"]["w"]        # 字多了且更大
    win.controller.undo()                          # 撤销修改
    obj = win.controller.doc.find(r["id"])
    assert obj.source.data["text"] == "旧文字"
    assert len(obj.local_strokes) == n_before


def test_update_text_non_text_object_rejected(tools, win):
    r = tools.call("add_markdown", {"markdown": "# 标题\n正文"})
    with pytest.raises(ToolError):
        tools.call("update_text", {"object_id": r["id"], "text": "x"})


# --------------------------------------------------------------- 富内容
def test_add_markdown_with_table(tools, win):
    md = ("# 实验数据\n\n| 次数 | 电压V | 电流A |\n|---|---|---|\n"
          "| 1 | 3.0 | 0.30 |\n| 2 | 6.0 | 0.61 |\n")
    r = tools.call("add_markdown", {"markdown": md, "x": 20, "y": 60})
    obj = win.controller.doc.find(r["id"])
    assert obj.source.kind == "markdown"
    assert r["bbox"]["w"] > 20             # 表格线拉开了宽度
    assert obj.meta.get("md_table_box")    # 表格外框已记录


def test_add_markdown_empty_rejected(tools):
    with pytest.raises(ToolError):
        tools.call("add_markdown", {"markdown": ""})


def test_add_equation(tools, win):
    pytest.importorskip("matplotlib")
    from writerstudio.content.equation import mathtext_available
    if not mathtext_available():
        pytest.skip("mathtext 不可用")
    r = tools.call("add_equation", {"latex": "$F = ma$", "x": 200, "y": 100})
    assert win.controller.doc.find(r["id"]) is not None
    assert r["bbox"]["w"] > 0


def test_add_tikz_with_width(tools, win):
    from writerstudio.content.tikz import tikz_available
    if not tikz_available():
        pytest.skip("TeX/TikZ 不可用")
    code = "\\draw[->] (0,0) -- (3,0) node[right]{$t$}; \\draw[->] (0,0) -- (0,2) node[above]{$v$};"
    r = tools.call("add_tikz", {"code": code, "width_mm": 80, "x": 180, "y": 120})
    obj = win.controller.doc.find(r["id"])
    assert obj.source.kind == "tikz"
    assert r["bbox"]["w"] == pytest.approx(80, rel=0.02)   # width_mm 生效


def test_add_tikz_compile_error_is_tool_error(tools):
    from writerstudio.content.tikz import tikz_available
    if not tikz_available():
        pytest.skip("TeX/TikZ 不可用")
    with pytest.raises(ToolError):
        tools.call("add_tikz", {"code": "\\thiscommanddoesnotexist{1}"})


# --------------------------------------------------------------- 几何编辑
def test_move_object_absolute_and_delta(tools, win):
    r = tools.call("add_text", {"text": "移动我", "size": 6})
    oid = r["id"]
    r1 = tools.call("move_object", {"object_id": oid, "x": 100, "y": 150})
    assert r1["bbox"]["x"] == pytest.approx(100, abs=0.5)
    assert r1["bbox"]["y"] == pytest.approx(150, abs=0.5)
    r2 = tools.call("move_object", {"object_id": oid, "dx": 5, "dy": -8})
    assert r2["bbox"]["x"] == pytest.approx(105, abs=0.5)
    assert r2["bbox"]["y"] == pytest.approx(142, abs=0.5)
    with pytest.raises(ToolError):
        tools.call("move_object", {"object_id": "obj-9999", "x": 1, "y": 1})


def test_transform_object_scale_rotate(tools, win):
    r = tools.call("add_text", {"text": "缩放旋转", "size": 6, "x": 40, "y": 40})
    oid = r["id"]
    r1 = tools.call("transform_object", {"object_id": oid, "scale": 2.0})
    assert r1["bbox"]["w"] == pytest.approx(r["bbox"]["w"] * 2, rel=0.02)
    r2 = tools.call("transform_object", {"object_id": oid, "rotate_deg": 90})
    assert r2["bbox"]["h"] == pytest.approx(r["bbox"]["w"] * 2, rel=0.05)
    with pytest.raises(ToolError):
        tools.call("transform_object", {"object_id": oid})


def test_remove_and_clear_undo(tools, win):
    tools.call("add_text", {"text": "甲", "size": 6})
    r2 = tools.call("add_text", {"text": "乙", "size": 6})
    tools.call("remove_object", {"object_id": r2["id"]})
    assert len(win.controller.doc.objects) == 1
    win.controller.undo()
    assert len(win.controller.doc.objects) == 2
    tools.call("clear_page", {})
    assert len(win.controller.doc.objects) == 0
    win.controller.undo()
    assert len(win.controller.doc.objects) == 2


# --------------------------------------------------------------- 布局检查
def test_check_layout_out_of_page(tools):
    tools.call("add_text", {"text": "出界文字", "size": 6, "x": 280, "y": 200})
    r = tools.call("check_layout", {})
    assert r["ok"] is False
    types = [i["type"] for i in r["issues"]]
    assert "out_of_page" in types


def test_check_layout_overlap_and_margin(tools, win):
    tools.call("add_text", {"text": "重叠测试内容甲", "size": 6, "x": 50, "y": 50})
    tools.call("add_text", {"text": "重叠测试内容乙", "size": 6, "x": 52, "y": 51})
    r = tools.call("check_layout", {})
    types = [i["type"] for i in r["issues"]]
    assert "overlap" in types
    # 贴到页顶但不越出页面 → crosses_margin 警告（边距 10mm，不算 error）
    tools.call("move_object", {"object_id": win.controller.doc.objects[-1].id,
                               "x": 52, "y": 2})
    r2 = tools.call("check_layout", {})
    assert any(i["type"] == "crosses_margin" for i in r2["issues"])
    assert r2["ok"] is True                # 警告不算 error


# --------------------------------------------------------------- 预览/存盘
def test_render_preview_png(tools):
    tools.call("add_text", {"text": "预览渲染", "size": 8, "x": 40, "y": 40})
    r = tools.call("render_preview", {"width_px": 800})
    import base64
    raw = base64.b64decode(r["png_base64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(raw) > 2000
    assert r["height_px"] == pytest.approx(800 * 210 / 297, rel=0.01)


def test_save_and_open_project_roundtrip(tools, win, tmp_path):
    tools.call("add_text", {"text": "存盘往返", "size": 6, "x": 30, "y": 30})
    p = tmp_path / "roundtrip.wsproj"
    r = tools.call("save_project", {"path": str(p)})
    assert r["ok"] is True
    assert p.exists()
    assert win.project_path == str(p)
    tools.call("clear_page", {})
    r2 = tools.call("open_project", {"path": str(p)})
    assert r2["object_count"] == 1
    obj = win.controller.doc.objects[0]
    assert obj.source.data["text"] == "存盘往返"


def test_save_without_path_rejected(tools, win, monkeypatch):
    monkeypatch.setattr(win, "project_path", None)
    with pytest.raises(ToolError):
        tools.call("save_project", {})


def test_export_gcode(tools, tmp_path):
    tools.call("add_text", {"text": "G代码导出", "size": 6, "x": 30, "y": 30})
    p = tmp_path / "out.gcode"
    r = tools.call("export_gcode", {"path": str(p)})
    assert r["strokes"] > 0
    text = p.read_text()
    assert "G21" in text
    with pytest.raises(ToolError):
        tools.call("export_gcode", {"path": ""})


# --------------------------------------------------------------- 扰动透传
def test_content_tools_perturb_changes_output(tools, win):
    """markdown/公式接受 perturb+seed：不同种子产出不同笔画（抖动生效）。"""
    pytest.importorskip("matplotlib")
    from writerstudio.content.equation import mathtext_available
    if not mathtext_available():
        pytest.skip("mathtext 不可用")
    p = {"enabled": True, "size_sigma": 0.05, "stroke_x_sigma": 0.3}
    r1 = tools.call("add_equation", {"latex": "$E = mc^2$", "seed": 1,
                                     "perturb": p})
    r2 = tools.call("add_equation", {"latex": "$E = mc^2$", "seed": 2,
                                     "perturb": p})
    o1 = win.controller.doc.find(r1["id"])
    o2 = win.controller.doc.find(r2["id"])
    s1 = [tuple(round(v, 2) for pt in s.points for v in pt)
          for s in o1.local_strokes]
    s2 = [tuple(round(v, 2) for pt in s.points for v in pt)
          for s in o2.local_strokes]
    assert s1 != s2                       # 种子不同 → 笔画不同

    m1 = tools.call("add_markdown", {"markdown": "# 抖动\n**测试**段落",
                                     "perturb": p, "seed": 3})
    om = win.controller.doc.find(m1["id"])
    assert om.source.data["perturb"]["enabled"] is True


def test_add_text_direction_vertical(tools, win):
    r = tools.call("add_text", {"text": "床前明月光\n疑是地上霜",
                                "direction": "v-rl", "size": 8})
    obj = win.controller.doc.find(r["id"])
    assert obj.source.data["direction"] == "v-rl"
    assert r["bbox"]["w"] > 0 and r["bbox"]["h"] > 0
    with pytest.raises(ToolError):
        tools.call("add_text", {"text": "x", "direction": "斜排"})
