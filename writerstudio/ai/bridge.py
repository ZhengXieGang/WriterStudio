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
import configparser
import json
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Optional

DEFAULT_PORT = 8765
#: 单次调用等待上限。MCP 客户端普遍 30s 就放弃，桥必须比它更早给出可读
#: 的错误；TikZ/LaTeX 这类长任务超过时限也拿不到结果——宁可明确报错并
#: 提示用 list_objects 核对，也不要让客户端哑等 30 秒。
RPC_TIMEOUT = 25.0
#: 协议探针时限：端口连得上却不说本协议 = 被别的程序占用。
PROBE_TIMEOUT = 3.0

#: 已经成功对话过的端口（进程级缓存，避免每次调用都探针）
_verified: set[int] = set()


class BridgeError(RuntimeError):
    """面向 AI 客户端的可读错误。"""


class ServiceDown(BridgeError):
    """端口上没有程序监听（软件没开或服务关闭）。"""


class PortNotOurs(BridgeError):
    """端口被别的程序占用：连得上，但不按本协议回话。"""


class ServiceBusy(BridgeError):
    """服务在线但没在时限内回话（长任务/卡住）。"""

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
* add_* 返回 missing 字段 = 该字体链画不出的字符（已留空），需换字体；
  add_markdown 也会报缺字（表头里的 kΩ 这类不会静默消失）。
* 落点锚点 anchor：top-left（默认）/ center / bottom-left / baseline-left 等——
  往表格格子里写字用 bottom-left 或 baseline-left 直接贴线，不必 add 后再
  move。move_object 同样支持 anchor。
* 排版前可用 measure_text 干跑量尺寸（不加内容、不脏历史）。
* 套打/对齐扫描件：add_reference_image 加底图（不参与输出），再用
  render_preview(include_references=true) 目视核对；参考图管理见
  list_references / update_reference / remove_reference。
* TikZ 报错先跑 check_tex：它会指出缺哪个宏包或哪个 texmf 路径没找到
  （隔离 HOME/TEXMFHOME 的常见坑）。
* 对象 id（形如 obj-0042）在 list_objects 的返回里，update/move/remove 都用它。
* 生成前确认页数尺寸与用户要求一致（set_page 可改）。
"""


def _config_service() -> tuple[Optional[int], Optional[bool]]:
    """读软件配置里的 AI 服务设置（端口 / 是否开启）。

    软件用 ``QSettings`` 保存（Linux/macOS 是 ``~/.config/WriterStudio/
    WriterStudio.conf`` 的 ``[ai]`` 段）。桥据此把「MCP 配置的端口」与
    「软件实际配置」对上：不一致时自动改试软件端口，并在错误里说清楚，
    而不是干等超时。Windows 的 QSettings 默认写注册表，这里读不到就
    返回 (None, None)，不影响主流程。
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    path = Path(base) / "WriterStudio" / "WriterStudio.conf"
    if not path.exists():
        return None, None
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
        if not cp.has_section("ai"):
            return None, None
        port = cp.getint("ai", "mcp_port", fallback=None)
        enabled = cp.getboolean("ai", "mcp_enabled", fallback=None)
        return port, enabled
    except (ValueError, OSError, configparser.Error):
        return None, None


def _call(port: int, name: str, arguments: dict, timeout: float) -> dict:
    """一次协议往返。失败按「没监听 / 不是本服务 / 没回话」分类。"""
    payload = json.dumps(
        {"v": 1, "id": 1, "name": name, "arguments": arguments},
        ensure_ascii=False).encode("utf-8")
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except ConnectionRefusedError as exc:
        raise ServiceDown("没有程序在监听（连接被拒绝）") from exc
    except socket.timeout as exc:
        raise ServiceBusy(f"连接超时（{timeout:.0f}s）") from exc
    except OSError as exc:
        raise ServiceDown(f"无法连接：{exc}") from exc

    with sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        buf = b""
        while True:
            if len(buf) >= 4:
                (need,) = struct.unpack_from(">I", buf, 0)
                if len(buf) >= 4 + need:
                    break
            try:
                chunk = sock.recv(65536)
            except socket.timeout as exc:
                if buf:
                    raise ServiceBusy(
                        f"回话不完整（{timeout:.0f}s 内只收到 {len(buf)} 字节）"
                    ) from exc
                raise PortNotOurs(
                    f"连得上但在 {timeout:.0f}s 内没有任何回话"
                    "——像是被其它程序占用的端口") from exc
            except OSError as exc:
                raise ServiceDown(f"连接中断：{exc}") from exc
            if not chunk:
                break
            buf += chunk

    if len(buf) < 4:
        raise PortNotOurs("连接后没有返回任何数据——不是 WriterStudio 服务")
    (frame_len,) = struct.unpack_from(">I", buf, 0)
    if frame_len <= 0 or frame_len > 64 * 1024 * 1024 or len(buf) < 4 + frame_len:
        raise PortNotOurs("返回的数据不是本软件的协议帧——端口被别的程序占用")
    try:
        resp = json.loads(buf[4:4 + frame_len].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PortNotOurs(
            f"返回的数据不是本软件的协议帧——端口被别的程序占用（{exc}）"
        ) from exc
    if not isinstance(resp, dict) or resp.get("v") != 1:
        raise PortNotOurs("返回的 JSON 不是本软件的协议应答——端口被别的程序占用")
    if not resp.get("ok"):
        raise BridgeError(str(resp.get("error") or "工具执行失败"))
    return resp.get("result") or {}


def _troubleshoot(problems: list[str], port: int, cfg_port: Optional[int],
                  cfg_enabled: Optional[bool]) -> str:
    lines = ["无法与 WriterStudio 通信："]
    lines += [f"  · {p}" for p in problems]
    if cfg_enabled is False:
        lines.append("  · 软件配置里 AI 服务处于**关闭**状态"
                     "（ai/mcp_enabled=false）：请打开软件，在菜单"
                     "「AI 排版 → AI 服务设置…」勾选开启并保存。")
    if cfg_port and cfg_port != port:
        lines.append(f"  · MCP 配置的端口（{port}）与软件配置（{cfg_port}）"
                     f"不一致，两者都已尝试；建议把 MCP 配置里的 --port "
                     f"改成 {cfg_port}，或把软件端口改成 {port}。")
    elif cfg_port is None:
        lines.append("  · 没读到软件配置（~/.config/WriterStudio/"
                     "WriterStudio.conf），无法自动核对端口。")
    lines.append("  排查顺序：1) 软件是否已打开；2) 「AI 排版 → AI 服务设置…」"
                 "里服务是否开启、端口是多少；3) 该端口是否被其它程序"
                 "（其它开发工具/服务）占用——占用时连得上但不会回话。")
    return "\n".join(lines)


def _rpc(port: int, name: str, arguments: dict) -> dict:
    """连接 GUI 服务，执行一次工具调用（阻塞 I/O，桥在 worker 线程里跑）。

    端口先探针验证（``__ping``，只读、无副作用）：
    * 连不上/不是本服务 → 依次尝试软件配置里的端口，全都失败才报错；
    * 探针通过后的正式调用**不重试**——调用可能已在软件里执行成功，
      重试会造成重复添加。
    """
    cfg_port, cfg_enabled = _config_service()
    candidates = [port]
    if cfg_port and cfg_port not in candidates:
        candidates.append(cfg_port)

    reachable: Optional[int] = None
    problems: list[str] = []
    for cand in candidates:
        if cand in _verified:
            reachable = cand
            break
        try:
            _call(cand, "__ping", {}, PROBE_TIMEOUT)
            _verified.add(cand)
            reachable = cand
            break
        except BridgeError as exc:
            problems.append(f"127.0.0.1:{cand}：{exc}")
    if reachable is None:
        raise BridgeError(_troubleshoot(problems, port, cfg_port, cfg_enabled))

    try:
        return _call(reachable, name, arguments, RPC_TIMEOUT)
    except ServiceBusy as exc:
        raise BridgeError(
            f"127.0.0.1:{reachable}：{exc}。注意：这次调用**可能已经在软件里"
            "执行成功**（只是回包没等到）——请先用 list_objects 核对，不要"
            "直接重试，以免重复添加内容。") from exc


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
    def list_fonts(filter: Optional[str] = None,
                   kind: Optional[str] = None) -> str:
        """列出可用的手写字体（用户字迹）。font_names 参数需要这里的 name。

        filter: 按名称子串过滤（用户可能装着上百款字体，全量返回很占上下文）；
        kind: 按类型过滤（如 hershey / stroke-json / truetype / gfont / gcode）。
        """
        return json.dumps(rpc("list_fonts", {"filter": filter, "kind": kind}),
                          ensure_ascii=False)

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
                 direction: str = "h",
                 anchor: str = "top-left") -> str:
        """添加文本对象（手写字迹）。

        text: 内容，\\n 换行；frame_width>0 时按该宽度自动换行(0=不换行)；
        align: left/center/right；size: 字号 mm（正文 5~8、标题 8~14）；
        x/y: 包围盒左上角落点(mm，页面左上原点)，省略则自动错开放置；
        font_names: 字体回退链(第一款中文+一款英文即可混排)，省略用当前默认；
        perturb: 手写扰动字典，如 {"enabled": true, "size_sigma": 0.03,
        "baseline_amplitude": 0.4}；seed: 扰动种子(同 seed 同结果)；
        direction: h=横排 / v-rl=竖排右→左(传统中文) / v-lr=竖排左→右；
        anchor: 包围盒的哪一点落到 (x,y)——往格子里写字（下沿贴线）用
        bottom-left 或 baseline-left（首行基线），居中用 center。
        返回 id、实际 bbox、缺字列表。
        """
        return json.dumps(rpc("add_text", {
            "text": text, "x": x, "y": y, "font_names": font_names,
            "size": size, "line_spacing": line_spacing,
            "char_spacing": char_spacing, "align": align,
            "frame_width": frame_width, "perturb": perturb, "seed": seed,
            "direction": direction, "anchor": anchor,
        }, ), ensure_ascii=False)

    @mcp.tool()
    def add_markdown(markdown: str, x: Optional[float] = None,
                     y: Optional[float] = None,
                     font_names: Optional[list[str]] = None,
                     size: float = 4.0, wrap_width: float = 120.0,
                     table_width: float = 0.0,
                     perturb: Optional[dict] = None,
                     seed: Optional[int] = None,
                     anchor: str = "top-left") -> str:
        """添加 Markdown 排版对象：多级标题、列表、粗体、数据表格（自动画
        表格线）、$行内公式$。适合多段落正文与表格。wrap_width 为自动换行
        宽度(mm)，table_width>0 时表格拉到该宽（是目标宽度：单元格里
        放不下的最短内容宽度可能把它撑宽，以返回的 bbox 为准）。
        perturb/seed：手写随机扰动及其种子（如
        {"enabled": true, "size_sigma": 0.03}）；anchor 同 add_text。
        返回 id 与实际 bbox 与缺字列表（缺字不再静默丢失）。"""
        return json.dumps(rpc("add_markdown", {
            "markdown": markdown, "x": x, "y": y, "font_names": font_names,
            "size": size, "wrap_width": wrap_width,
            "table_width": table_width, "perturb": perturb, "seed": seed,
            "anchor": anchor,
        }), ensure_ascii=False)

    @mcp.tool()
    def add_tikz(code: str, x: Optional[float] = None, y: Optional[float] = None,
                 width_mm: Optional[float] = None,
                 font_names: Optional[list[str]] = None,
                 text_scale: float = 1.0, name: Optional[str] = None,
                 perturb: Optional[dict] = None,
                 seed: Optional[int] = None,
                 anchor: str = "top-left") -> str:
        """添加 TikZ 矢量图形（坐标系/函数曲线/几何图/示意图的首选）。

        code: TikZ 代码，\\begin{tikzpicture} 可省略；坐标单位默认 cm。
        width_mm: 成图宽度(mm)，按它整体缩放，强烈建议显式给出。
        节点文字自动用用户手写字体重排。例（10mm 宽的坐标轴）：
        \\draw[->] (0,0) -- (3,0) node[right]{$x$}; \\draw[->] (0,0) -- (0,2);
        perturb/seed：手写随机扰动及其种子（线条微微抖动更像手绘）。
        anchor 同 add_text。编译失败时错误里会附环境诊断（也可先跑 check_tex）。
        返回 id 与实际 bbox。"""
        return json.dumps(rpc("add_tikz", {
            "code": code, "x": x, "y": y, "width_mm": width_mm,
            "font_names": font_names, "text_scale": text_scale, "name": name,
            "perturb": perturb, "seed": seed, "anchor": anchor,
        }), ensure_ascii=False)

    @mcp.tool()
    def add_equation(latex: str, x: Optional[float] = None,
                     y: Optional[float] = None, size_mm: float = 6.0,
                     font_names: Optional[list[str]] = None,
                     perturb: Optional[dict] = None,
                     seed: Optional[int] = None,
                     anchor: str = "top-left") -> str:
        """添加数学公式（matplotlib mathtext 语法，如 $F = ma$、
        $\\frac{a}{b}$）。size_mm 为公式主体高度。perturb/seed：手写
        随机扰动及其种子；anchor 同 add_text。返回 id 与实际 bbox。"""
        return json.dumps(rpc("add_equation", {
            "latex": latex, "x": x, "y": y, "size_mm": size_mm,
            "font_names": font_names, "perturb": perturb, "seed": seed,
            "anchor": anchor,
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
                    dx: Optional[float] = None, dy: Optional[float] = None,
                    anchor: str = "top-left") -> str:
        """移动对象。给 x/y = 把 anchor 指定的那一点移到该位置(mm)；
        或给 dx/dy = 相对当前位置平移(mm)。两者只能选其一。
        anchor 默认 top-left；贴行格底线用 bottom-left/baseline-left。"""
        return json.dumps(rpc("move_object", {
            "object_id": object_id, "x": x, "y": y, "dx": dx, "dy": dy,
            "anchor": anchor,
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

    # ---------------------------------------------------------- 参考图层
    @mcp.tool()
    def list_references() -> str:
        """列出参考图层（底图）：id、文件、bbox、透明度。参考图永不参与
        书写与 G-code，只用于把内容对齐到扫描件/表单模板上。"""
        return json.dumps(rpc("list_references", {}), ensure_ascii=False)

    @mcp.tool()
    def add_reference_image(path: str, x: Optional[float] = None,
                            y: Optional[float] = None,
                            anchor: str = "center",
                            width_mm: Optional[float] = None,
                            height_mm: Optional[float] = None,
                            opacity: Optional[float] = None,
                            kind: Optional[str] = None) -> str:
        """添加参考图（底图）：图片或 SVG，用于套打/对齐。

        path: 文件绝对路径；省略 x/y 时按页边距内等比适配并居中；
        x/y + anchor 指定落点（默认 center）；width_mm/height_mm 指定显示
        尺寸（只给一个按原比例）；opacity 0~1（默认 0.45）。
        返回 id 与 bbox。加完用 render_preview(include_references=true)
        目视核对内容与底图是否对齐。"""
        return json.dumps(rpc("add_reference_image", {
            "path": path, "x": x, "y": y, "anchor": anchor,
            "width_mm": width_mm, "height_mm": height_mm,
            "opacity": opacity, "kind": kind,
        }), ensure_ascii=False)

    @mcp.tool()
    def update_reference(reference_id: str, x: Optional[float] = None,
                         y: Optional[float] = None,
                         anchor: str = "top-left",
                         width_mm: Optional[float] = None,
                         height_mm: Optional[float] = None,
                         opacity: Optional[float] = None,
                         visible: Optional[bool] = None,
                         locked: Optional[bool] = None) -> str:
        """修改参考图：位置(x/y+anchor)、显示尺寸、透明度、可见/锁定。"""
        return json.dumps(rpc("update_reference", {
            "reference_id": reference_id, "x": x, "y": y, "anchor": anchor,
            "width_mm": width_mm, "height_mm": height_mm,
            "opacity": opacity, "visible": visible, "locked": locked,
        }), ensure_ascii=False)

    @mcp.tool()
    def remove_reference(reference_id: Optional[str] = None) -> str:
        """删除参考图（可撤销）；不给 id 时删除全部参考图。"""
        return json.dumps(rpc("remove_reference",
                              {"reference_id": reference_id}),
                          ensure_ascii=False)

    # ------------------------------------------------------------ 度量/自检
    @mcp.tool()
    def measure_text(text: str, size: float = 10.0,
                     font_names: Optional[list[str]] = None,
                     line_spacing: float = 1.4, char_spacing: float = 0.0,
                     align: str = "left", frame_width: float = 0.0,
                     direction: str = "h") -> str:
        """干跑测量：只算尺寸，不往文档里加东西（比先 add 再 remove 省事，
        也不弄脏撤销历史）。返回 width_mm/height_mm/行数与缺字列表——
        排版前先用它确认「放得下吗、该放哪儿」。"""
        return json.dumps(rpc("measure_text", {
            "text": text, "size": size, "font_names": font_names,
            "line_spacing": line_spacing, "char_spacing": char_spacing,
            "align": align, "frame_width": frame_width,
            "direction": direction,
        }), ensure_ascii=False)

    @mcp.tool()
    def check_tex() -> str:
        """TeX/TikZ 环境自检：引擎、转换器、缺失宏包与 texmf 查找路径。
        add_tikz 报错时先跑这个：它会区分「没装 TeX」与「隔离 HOME 导致
        个人 texmf 树 / xelatex 格式文件查不到」。"""
        return json.dumps(rpc("check_tex", {}), ensure_ascii=False)

    # -------------------------------------------------------------- 检查
    @mcp.tool()
    def check_layout() -> str:
        """检查当前排版：对象包围盒、超出页面(error)、越过页边距与
        相互重叠(warning)。返回 issues 列表，无 error 才适合打印。"""
        return json.dumps(rpc("check_layout", {}), ensure_ascii=False)

    @mcp.tool()
    def render_preview(width_px: int = 1024,
                       include_references: bool = False) -> list:
        """渲染当前整页为 PNG 图像并返回（用于目视检查排版效果）。
        width_px 为图像宽度像素(200~4000)。
        include_references=true 时把参考图层（底图）按透明度一并画出——
        套打表单时用它核对内容与底图是否对齐（默认不画，与输出一致）。"""
        result = rpc("render_preview", {
            "width_px": width_px, "include_references": include_references})
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
