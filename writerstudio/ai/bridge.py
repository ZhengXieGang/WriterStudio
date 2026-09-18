"""WriterStudio 的 stdio MCP 桥（独立进程，协议翻译）。

把 MCP 客户端（Claude Desktop、ZCode 等任何支持 MCP 的 AI 应用）的
``tools/call`` 请求翻译成对本机 WriterStudio GUI 的 TCP 调用。

客户端配置示例（Claude Desktop ``claude_desktop_config.json``）::

    {
      "mcpServers": {
        "writerstudio": {
          "command": "writerstudio-mcp",
          "args": ["--port", "8765"]
        }
      }
    }

源码运行时可写 ``python -m writerstudio.ai.bridge``。端口也可用环境变量
``WRITERSTUDIO_MCP_PORT`` 指定。GUI 端口号见软件「AI 排版」对话框。

依赖：官方 MCP SDK（``pip install mcp``）；本模块**懒加载**该依赖，
``writerstudio`` 包本身不要求安装它。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
from typing import Optional

DEFAULT_PORT = 8765
#: TikZ/LaTeX 编译可能耗时数十秒，等待上限放宽到 5 分钟
RPC_TIMEOUT = 300.0

INSTRUCTIONS = """\
WriterStudio 写字机排版服务：通过工具直接在用户打开的 WriterStudio 窗口里
完成整页排版，生成可直接发送给写字机打印的内容。

## 坐标约定（务必遵守）
* 单位一律毫米(mm)。原点在页面**左上角**：x 向右、y **向下**。
* 所有 add_* 工具的 (x, y) 是「内容包围盒左上角」的落点；返回值里的 bbox
  是实际落点结果（x/y/w/h，同坐标系），以此为准做后续排布，不要自己估算。
* 所有修改都可撤销（用户可见历史面板），放心试错。

## 推荐工作流
1. get_page_info 拿页面尺寸；list_fonts 拿可用手写字体（用户的手写字迹
   只有这些，不要假设系统字体可用）。
2. 逐块添加内容并排版：
   * 正文/标题/填空 → add_text（可给 frame_width 自动换行、perturb 手写扰动）
   * 多段落/数据表格/清单 → add_markdown（自带中文排版与表格线）
   * 坐标系图/函数曲线/几何图/电路示意图 → add_tikz（写 TikZ 代码，
     给 width_mm 控制成图宽度；节点文字会自动用用户手写字体重排）
   * 数学公式 → add_equation
3. 每步用返回的 bbox 决定下一块的位置（如 y + h + 8mm）。
4. check_layout 检查出界/越边距/重叠并修到无 error；
5. render_preview 看整页渲染图自查（图像返回），不满意就 adjust 再看；
6. save_project 存盘；需要 G-code 时 export_gcode（发送打印仍由用户在
   软件里操作）。

## 注意
* add_text 返回 missing 字段 = 该字体链画不出的字符（已留空），需换字体。
* 对象 id（形如 obj-0042）在 list_objects 的返回里，update/move/remove 都用它。
* 生成前确认页数尺寸与用户要求一致（set_page 可改）。
"""


def _rpc(port: int, name: str, arguments: dict) -> dict:
    """连接 GUI 服务，执行一次工具调用（阻塞 I/O，桥在 to_thread 里跑）。"""
    try:
        with socket.create_connection(("127.0.0.1", port),
                                      timeout=RPC_TIMEOUT) as sock:
            payload = json.dumps(
                {"v": 1, "id": 1, "name": name, "arguments": arguments},
                ensure_ascii=False).encode("utf-8")
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            buf = b""
            while len(buf) < 4 or len(buf) < 4 + struct.unpack_from(">I", buf, 0)[0]:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
        if len(buf) < 4:
            raise RuntimeError("WriterStudio 服务断开连接（可能软件正在退出）")
        (frame_len,) = struct.unpack_from(">I", buf, 0)
        resp = json.loads(buf[4:4 + frame_len].decode("utf-8"))
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise RuntimeError(
            f"无法连接 WriterStudio（127.0.0.1:{port}）：{exc}。请确认 "
            "1) WriterStudio 软件已打开；2) 软件菜单「AI 排版」中服务处于"
            "开启状态且端口一致") from exc
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error") or "工具执行失败")
    return resp.get("result") or {}


def _build_server(port: int):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("writerstudio", instructions=INSTRUCTIONS)

    def rpc(name, arguments):
        return _rpc(port, name, arguments)

    # -------------------------------------------------------------- 查询
    @mcp.tool()
    def get_page_info() -> str:
        """获取当前页面信息：宽高/边距(mm)、坐标约定、对象数、当前文件路径。"""
        return json.dumps(rpc("get_page_info", {}), ensure_ascii=False)

    @mcp.tool()
    def set_page(width: float, height: float, margin: float = 10.0) -> str:
        """设置页面尺寸与页边距（mm）。例：A4 纵向 width=210, height=297。"""
        return json.dumps(
            rpc("set_page", {"width": width, "height": height,
                             "margin": margin}), ensure_ascii=False)

    @mcp.tool()
    def list_fonts() -> str:
        """列出可用的手写字体（用户字迹）。font_names 参数需要这里的 name。"""
        return json.dumps(rpc("list_fonts", {}), ensure_ascii=False)

    @mcp.tool()
    def list_objects() -> str:
        """列出当前文档所有对象：id、类型、包围盒、内容摘要。"""
        return json.dumps(rpc("list_objects", {}), ensure_ascii=False)

    # -------------------------------------------------------------- 内容
    @mcp.tool()
    def add_text(text: str, x: Optional[float] = None, y: Optional[float] = None,
                 font_names: Optional[list[str]] = None,
                 size: float = 10.0, line_spacing: float = 1.4,
                 char_spacing: float = 0.0, align: str = "left",
                 frame_width: float = 0.0,
                 perturb: Optional[dict] = None,
                 seed: Optional[int] = None,
                 direction: str = "h") -> str:
        """添加文本对象（手写字迹）。

        text: 内容，\\n 换行；frame_width>0 时按该宽度自动换行(0=不换行)；
        align: left/center/right；size: 字号 mm（正文 5~8、标题 8~14）；
        x/y: 包围盒左上角落点(mm，页面左上原点)，省略则自动错开放置；
        font_names: 字体回退链(第一款中文+一款英文即可混排)，省略用当前默认；
        perturb: 手写扰动字典，如 {"enabled": true, "size_sigma": 0.03,
        "baseline_amplitude": 0.4}；seed: 扰动种子(同 seed 同结果)；
        direction: h=横排 / v-rl=竖排右→左(传统中文) / v-lr=竖排左→右。
        返回 id、实际 bbox、缺字列表。
        """
        return json.dumps(rpc("add_text", {
            "text": text, "x": x, "y": y, "font_names": font_names,
            "size": size, "line_spacing": line_spacing,
            "char_spacing": char_spacing, "align": align,
            "frame_width": frame_width, "perturb": perturb, "seed": seed,
            "direction": direction,
        }, ), ensure_ascii=False)

    @mcp.tool()
    def add_markdown(markdown: str, x: Optional[float] = None,
                     y: Optional[float] = None,
                     font_names: Optional[list[str]] = None,
                     size: float = 4.0, wrap_width: float = 120.0,
                     table_width: float = 0.0,
                     perturb: Optional[dict] = None,
                     seed: Optional[int] = None) -> str:
        """添加 Markdown 排版对象：多级标题、列表、粗体、数据表格（自动画
        表格线）、$行内公式$。适合多段落正文与表格。wrap_width 为自动换行
        宽度(mm)，table_width>0 时表格拉到该宽。perturb/seed：手写随机
        扰动及其种子（如 {"enabled": true, "size_sigma": 0.03}）。
        返回 id 与实际 bbox。"""
        return json.dumps(rpc("add_markdown", {
            "markdown": markdown, "x": x, "y": y, "font_names": font_names,
            "size": size, "wrap_width": wrap_width,
            "table_width": table_width, "perturb": perturb, "seed": seed,
        }), ensure_ascii=False)

    @mcp.tool()
    def add_tikz(code: str, x: Optional[float] = None, y: Optional[float] = None,
                 width_mm: Optional[float] = None,
                 font_names: Optional[list[str]] = None,
                 text_scale: float = 1.0, name: Optional[str] = None,
                 perturb: Optional[dict] = None,
                 seed: Optional[int] = None) -> str:
        """添加 TikZ 矢量图形（坐标系/函数曲线/几何图/示意图的首选）。

        code: TikZ 代码，\\begin{tikzpicture} 可省略；坐标单位默认 cm。
        width_mm: 成图宽度(mm)，按它整体缩放，强烈建议显式给出。
        节点文字自动用用户手写字体重排。例（10mm 宽的坐标轴）：
        \\draw[->] (0,0) -- (3,0) node[right]{$x$}; \\draw[->] (0,0) -- (0,2);
        perturb/seed：手写随机扰动及其种子（线条微微抖动更像手绘）。
        返回 id 与实际 bbox。"""
        return json.dumps(rpc("add_tikz", {
            "code": code, "x": x, "y": y, "width_mm": width_mm,
            "font_names": font_names, "text_scale": text_scale, "name": name,
            "perturb": perturb, "seed": seed,
        }), ensure_ascii=False)

    @mcp.tool()
    def add_equation(latex: str, x: Optional[float] = None,
                     y: Optional[float] = None, size_mm: float = 6.0,
                     font_names: Optional[list[str]] = None,
                     perturb: Optional[dict] = None,
                     seed: Optional[int] = None) -> str:
        """添加数学公式（matplotlib mathtext 语法，如 $F = ma$、
        $\\frac{a}{b}$）。size_mm 为公式主体高度。perturb/seed：手写
        随机扰动及其种子。返回 id 与实际 bbox。"""
        return json.dumps(rpc("add_equation", {
            "latex": latex, "x": x, "y": y, "size_mm": size_mm,
            "font_names": font_names, "perturb": perturb, "seed": seed,
        }), ensure_ascii=False)

    # -------------------------------------------------------------- 编辑
    @mcp.tool()
    def update_text(object_id: str, text: Optional[str] = None,
                    font_names: Optional[list[str]] = None,
                    size: Optional[float] = None,
                    line_spacing: Optional[float] = None,
                    char_spacing: Optional[float] = None,
                    align: Optional[str] = None,
                    frame_width: Optional[float] = None,
                    perturb: Optional[dict] = None) -> str:
        """修改文本对象内容/字体/字号等（只给要改的字段），重新排版。
        返回新的 bbox 与缺字列表。"""
        return json.dumps(rpc("update_text", {
            "object_id": object_id, "text": text, "font_names": font_names,
            "size": size, "line_spacing": line_spacing,
            "char_spacing": char_spacing, "align": align,
            "frame_width": frame_width, "perturb": perturb,
        }), ensure_ascii=False)

    @mcp.tool()
    def move_object(object_id: str, x: Optional[float] = None,
                    y: Optional[float] = None,
                    dx: Optional[float] = None, dy: Optional[float] = None) -> str:
        """移动对象。给 x/y = 把包围盒左上角移到该绝对位置(mm)；
        或给 dx/dy = 相对当前位置平移(mm)。两者只能选其一。"""
        return json.dumps(rpc("move_object", {
            "object_id": object_id, "x": x, "y": y, "dx": dx, "dy": dy,
        }), ensure_ascii=False)

    @mcp.tool()
    def transform_object(object_id: str, scale: Optional[float] = None,
                         rotate_deg: Optional[float] = None) -> str:
        """缩放和/或旋转对象（绕自身中心；rotate_deg 逆时针为正，度）。"""
        return json.dumps(rpc("transform_object", {
            "object_id": object_id, "scale": scale, "rotate_deg": rotate_deg,
        }), ensure_ascii=False)

    @mcp.tool()
    def remove_object(object_id: str) -> str:
        """删除一个对象（可撤销）。"""
        return json.dumps(rpc("remove_object", {"object_id": object_id}),
                          ensure_ascii=False)

    @mcp.tool()
    def clear_page() -> str:
        """清空页面上所有对象（可撤销）。"""
        return json.dumps(rpc("clear_page", {}), ensure_ascii=False)

    # -------------------------------------------------------------- 文件
    @mcp.tool()
    def open_project(path: str) -> str:
        """在软件中打开一个 .wsproj 项目文件（绝对路径），继续编辑。"""
        return json.dumps(rpc("open_project", {"path": path}),
                          ensure_ascii=False)

    @mcp.tool()
    def save_project(path: Optional[str] = None) -> str:
        """保存当前文档为 .wsproj（绝对路径；省略 path 则存到当前文件）。"""
        return json.dumps(rpc("save_project", {"path": path}),
                          ensure_ascii=False)

    @mcp.tool()
    def export_gcode(path: str) -> str:
        """把当前页面导出为 G-code 文件（.gcode 绝对路径）。实际发送到
        写字机由用户在软件界面操作。"""
        return json.dumps(rpc("export_gcode", {"path": path}),
                          ensure_ascii=False)

    # -------------------------------------------------------------- 检查
    @mcp.tool()
    def check_layout() -> str:
        """检查当前排版：对象包围盒、超出页面(error)、越过页边距与
        相互重叠(warning)。返回 issues 列表，无 error 才适合打印。"""
        return json.dumps(rpc("check_layout", {}), ensure_ascii=False)

    @mcp.tool()
    def render_preview(width_px: int = 1024) -> list:
        """渲染当前整页为 PNG 图像并返回（用于目视检查排版效果）。
        width_px 为图像宽度像素(200~4000)。"""
        result = rpc("render_preview", {"width_px": width_px})
        from mcp.types import ImageContent, TextContent
        summary = {k: v for k, v in result.items() if k != "png_base64"}
        return [
            TextContent(type="text",
                        text=json.dumps(summary, ensure_ascii=False)),
            ImageContent(type="image", data=result["png_base64"],
                         mimeType="image/png"),
        ]

    return mcp


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="writerstudio-mcp",
        description="WriterStudio 写字机排版 MCP 服务（stdio 桥）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("WRITERSTUDIO_MCP_PORT",
                                                   DEFAULT_PORT)),
                        help=f"WriterStudio GUI 服务端口（默认 {DEFAULT_PORT}，"
                             "也可用环境变量 WRITERSTUDIO_MCP_PORT）")
    args = parser.parse_args(argv)
    try:
        from mcp.server.fastmcp import FastMCP as _mcp_probe
        _mcp_probe  # 可用性探测（引用以通过静态检查）
    except ImportError as exc:
        print("缺少 MCP SDK，请先安装：pip install mcp", file=sys.stderr)
        print(f"（导入错误：{exc}）", file=sys.stderr)
        return 2
    mcp = _build_server(args.port)
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
