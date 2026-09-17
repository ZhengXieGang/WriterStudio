"""端到端验证：模拟 AI agent 用排版工具完成一整页物理实验内容。

对标真实用户文档（物理实验讲义：标题/正文/数据表格/坐标系图/公式）的
复杂度，完整走一遍「查询 → 逐块生成（按返回 bbox 决定下一块位置）→
布局检查 → 预览自查 → 存盘 → 导出 G-code」的 agent 工作流。
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from writerstudio.ai.tools import AiTools  # noqa: E402
from writerstudio.settings import K_AI_ENABLED, Settings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def tools(qapp):
    from writerstudio.ui.main_window import MainWindow
    st = Settings()
    st.set(K_AI_ENABLED, False)
    win = MainWindow(st)
    yield AiTools(win)
    win.close()


#: 内置字体链（真实用户环境里就是用户的手写字体，逻辑一致）
CHAIN = ["STRK-Kaiti", "futural"]


def _below(prev: dict, gap: float = 8.0) -> dict:
    """agent 的典型决策：下一块放在上一块下方。"""
    return {"x": prev["bbox"]["x"], "y": prev["bbox"]["y"]
            + prev["bbox"]["h"] + gap}


def test_full_page_layout_workflow(tools, tmp_path):
    t = tools
    # ---- 1. 查询环境 ----
    page = t.call("get_page_info", {})
    assert page["width_mm"] == pytest.approx(297.0)      # A4 横向
    fonts = t.call("list_fonts", {})["fonts"]
    chain = [f["name"] for f in fonts if f["name"] in CHAIN]
    assert chain                                          # 字体链存在

    # ---- 2. 逐块生成（每块位置由上一块的实际 bbox 推出） ----
    title = t.call("add_text", {
        "text": "实验一 示波器的使用", "size": 9, "x": 25, "y": 18,
        "font_names": chain})
    assert title["ok"] and not title.get("missing")

    info = t.call("add_text", {
        "text": "姓名________ 班级________ 日期________",
        "size": 5, "font_names": chain, **_below(title, 6)})
    assert info["fits_page"]

    body = t.call("add_markdown", {
        "markdown": (
            "## 实验目的\n"
            "1. 熟悉示波器各旋钮的作用\n"
            "2. 学会用示波器测量信号幅度与频率\n\n"
            "## 实验数据\n"
            "| 次数 | 峰峰值V | 周期ms |\n"
            "|---|---|---|\n"
            "| 1 | 4.0 | 5.0 |\n"
            "| 2 | 4.1 | 5.1 |\n"
            "| 3 | 3.9 | 4.9 |\n"),
        "size": 4.0, "wrap_width": 130, "x": 25,
        "y": info["bbox"]["y"] + info["bbox"]["h"] + 6,
        "font_names": chain})
    assert body["ok"]

    eq = t.call("add_equation", {
        "latex": "$T = 1/f$",
        "x": 170, "y": body["bbox"]["y"], "size_mm": 5, "font_names": chain})

    tikz_ok = True
    try:
        graph = t.call("add_tikz", {
            "code": (
                "\\draw[->] (-0.3,0) -- (4.3,0) node[right] {$t/s$};\n"
                "\\draw[->] (0,-1.4) -- (0,1.4) node[above] {$u/V$};\n"
                "\\draw[thick,domain=0:4,samples=100] "
                "plot (\\x, {sin(2*\\x r)});\n"),
            "width_mm": 70, "x": 170, "y": eq["bbox"]["y"] + eq["bbox"]["h"] + 10,
            "font_names": chain})
        assert graph["bbox"]["w"] == pytest.approx(70, rel=0.02)
    except Exception:
        tikz_ok = False                   # 无 TeX 环境时图形块跳过

    # ---- 3. 布局检查：不允许出界 ----
    check = t.call("check_layout", {})
    errors = [i for i in check["issues"] if i["severity"] == "error"]
    assert not errors, errors

    # ---- 4. 预览自查 ----
    preview = t.call("render_preview", {"width_px": 1200})
    import base64
    assert base64.b64decode(preview["png_base64"])[:8] == b"\x89PNG\r\n\x1a\n"

    # ---- 5. 存盘 → 重开 → 导出 G-code ----
    out = tmp_path / "physics_page.wsproj"
    assert t.call("save_project", {"path": str(out)})["ok"] is True

    kinds = {o["kind"] for o in t.call("list_objects", {})["objects"]}
    assert "text" in kinds and "markdown" in kinds
    if tikz_ok:
        assert "tikz" in kinds

    t.call("open_project", {"path": str(out)})
    assert len(t.call("list_objects", {})["objects"]) >= 4

    gcode = tmp_path / "physics_page.gcode"
    r = t.call("export_gcode", {"path": str(gcode)})
    assert r["strokes"] > 50              # 一整页内容的笔画量级
    text = gcode.read_text()
    assert "G21" in text and "G1" in text


def test_agent_error_recovery_flow(tools):
    """agent 传错字体后能凭错误信息自愈——错误里必须带可用字体清单。"""
    from writerstudio.ai.tools import ToolError
    try:
        tools.call("add_text", {"text": "自愈", "font_names": ["手写体甲"]})
        pytest.fail("应当报错")
    except ToolError as e:
        msg = str(e)
        assert "手写体甲" in msg and "STRK-Kaiti" in msg
    # 修正后成功
    r = tools.call("add_text", {"text": "自愈成功", "size": 6,
                                "font_names": CHAIN})
    assert r["ok"] is True
