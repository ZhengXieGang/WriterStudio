#!/usr/bin/env python
"""AI 排版端到端演示：模拟一个 AI agent 通过 MCP 完成整页排版。

用法：
    1. 打开 WriterStudio（确认「AI 排版」服务已启动，默认 127.0.0.1:8765）
    2. pip install mcp
    3. python scripts/ai_layout_demo.py

脚本会像真实 agent 一样：查询页面/字体 → 逐块生成标题、说明、数据表格、
坐标系图、公式 → 布局检查 → 渲染预览自查 → 存盘。运行结束在软件里就能
看到整页内容（可在历史面板逐步撤销）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path


async def agent_flow(port: int, out_path: str | None) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "writerstudio.ai.bridge", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            print(f"[agent] 连接成功，{len(tools)} 个工具可用")

            async def call(name, **args):
                r = await s.call_tool(name, args)
                if r.isError:
                    raise RuntimeError(r.content[0].text)
                return json.loads(r.content[0].text)

            page = await call("get_page_info")
            print(f"[agent] 页面 {page['width_mm']}×{page['height_mm']}mm")

            fonts = (await call("list_fonts"))["fonts"]
            names = [f["name"] for f in fonts]
            zh = [f["name"] for f in fonts if f["kind"] == "stroke-json"][:1]
            chain = zh + ["futural"]
            print(f"[agent] {len(names)} 款字体，使用回退链 {chain}")

            # 逐块生成，位置由上一块的实际 bbox 推出
            def below(prev, gap=8.0, x=None):
                return {"x": x if x is not None else prev["bbox"]["x"],
                        "y": prev["bbox"]["y"] + prev["bbox"]["h"] + gap}

            title = await call("add_text", text="物理实验记录",
                               size=9, x=25, y=18, font_names=chain)
            info = await call("add_text",
                              text="班级________ 姓名________ 日期________",
                              size=5, font_names=chain, **below(title, 6))
            body = await call("add_markdown", x=25,
                              y=info["bbox"]["y"] + info["bbox"]["h"] + 6,
                              font_names=chain,
                              markdown=(
                                  "## 实验目的\n"
                                  "1. 练习使用电流表和电压表\n"
                                  "2. 验证欧姆定律\n\n"
                                  "## 实验数据\n"
                                  "| 次数 | 电压V | 电流A |\n|---|---|---|\n"
                                  "| 1 | 1.0 | 0.20 |\n"
                                  "| 2 | 2.0 | 0.41 |\n"
                                  "| 3 | 3.0 | 0.60 |\n"))
            await call("add_equation", latex=r"$I = U/R$",
                       x=170, y=body["bbox"]["y"], size_mm=5,
                       font_names=chain)
            graph = await call("add_tikz", font_names=chain, width_mm=75,
                               x=170, y=body["bbox"]["y"] + 16,
                               code=(
                                   "\\draw[->] (-0.3,0) -- (4.3,0) "
                                   "node[right] {$U/V$};\n"
                                   "\\draw[->] (0,-0.3) -- (0,3.2) "
                                   "node[above] {$I/A$};\n"
                                   "\\draw plot coordinates "
                                   "{(1,0.2) (2,0.41) (3,0.6)};\n"
                                   "\\draw[dashed] (3,0.6) -- (3,0) "
                                   "-- (0,0.6);\n"))
            print(f"[agent] 已生成 {5} 块内容，图宽 {graph['bbox']['w']:.0f}mm")

            check = await call("check_layout")
            bad = [i for i in check["issues"] if i["severity"] == "error"]
            print(f"[agent] 布局检查：{'通过' if not bad else bad}")

            r = await s.call_tool("render_preview", {"width_px": 1000})
            png = next(c for c in r.content if c.type == "image")
            out = Path(tempfile.gettempdir()) / "writerstudio_ai_preview.png"
            out.write_bytes(base64.b64decode(png.data))
            print(f"[agent] 预览图已存 {out}")

            if out_path:
                await call("save_project", path=out_path)
                print(f"[agent] 项目已存 {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("WRITERSTUDIO_MCP_PORT", 8765)))
    ap.add_argument("--save", help="排版结果另存为 .wsproj 的路径（可选）")
    args = ap.parse_args()
    asyncio.run(asyncio.wait_for(agent_flow(args.port, args.save), 300))
    return 0


if __name__ == "__main__":
    sys.exit(main())
