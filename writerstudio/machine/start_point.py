"""写字起点设置。

三种模式（对应需求「写字起点设置」）：
    * ``canvas``  —— 画布指定点：用户在文档坐标中指定起点，导出时平移到该处。
    * ``origin``  —— 绝对原点：使用文档坐标原样输出（配合机器零点）。
    * ``current`` —— 机器当前位置：把当前机器坐标作为内容起点。

另有一种**笔位校准**模式（``registered``）：
    * 用户把红色标记放在纸上「希望笔开始写」的位置，再把笔**手动**移到纸上
      同一点，点「以当前笔位校准标记」。软件记录笔此刻的机器坐标 M，并据此
      建立「纸张坐标 ↔ 机器坐标」的对应：标记所在的纸面点 = 机器坐标 M。
      标记保持不动；之后内容按画布摆放书写。这解决了「机器原点与纸张角落
      有差距」的根本问题——原点是被物理标定出来的，而不是假设出来的。

核心是把「起点模式」解析为一个文档坐标下的目标点，再换算成
:class:`GCodeConfig.origin_offset`（机器坐标 = 文档坐标 + offset）。
``registered`` 模式的 offset 需要轴映射与标记参与，由主窗口层解析
（见 ``MainWindow._machine_config``）。

.. note::
   生成端现已不再读取 ``GCodeConfig.origin_offset``：写字起点在
   ``MainWindow._start_anchor()`` 里被解析为「内容页面锚点 +
   ``generate_gcode(start_offset=...)`` 的机器系平移」——把纸张的实际
   台面位置烘焙进绝对坐标，而不是改文档坐标。下面的 :func:`compute_offset`
   与 :func:`registered_offset` 保留为纯函数参考/历史实现，不再参与
   G-code 生成（起点语义的权威在 ``_start_anchor``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..core.geometry import BBox
from ..core.document import Document

MODE_CANVAS = "canvas"
MODE_ORIGIN = "origin"
MODE_CURRENT = "current"
MODE_REGISTERED = "registered"     # 以当前笔位校准标记（标记处 = 笔位）

MODE_LABELS = {
    MODE_CANVAS: "画布指定点",
    MODE_ORIGIN: "绝对原点",
    MODE_CURRENT: "机器当前位置",
    MODE_REGISTERED: "笔位校准（标记处=笔位）",
}


@dataclass
class StartPoint:
    """起点设置。

    ``pen_marker`` 是**机器坐标**(mm) 下的「笔位标记」：表示写字机笔头
    停在机器台面的哪个位置，「移动到标记起点」直接 G0 到该坐标。画布
    显示时按轴映射（不含起点偏移）换算回纸面位置，见
    ``MainWindow._axis_mapping``——带偏移换算会形成标记↔起点的回环。
    """

    mode: str = MODE_CANVAS
    point: tuple[float, float] = (0.0, 0.0)      # 画布模式：目标原点(mm)
    pen_marker: Optional[tuple[float, float]] = None   # 笔位标记（页面坐标）

    def clone(self) -> StartPoint:
        return StartPoint(self.mode, tuple(self.point),
                          tuple(self.pen_marker) if self.pen_marker else None)

    def to_data(self) -> dict:
        d: dict = {"mode": self.mode, "point": list(self.point)}
        if self.pen_marker is not None:
            d["pen_marker"] = list(self.pen_marker)
        return d

    @classmethod
    def from_data(cls, d: Optional[dict]) -> StartPoint:
        if not d:
            return cls()
        marker = d.get("pen_marker")
        return cls(d.get("mode", MODE_CANVAS),
                   tuple(d.get("point", (0.0, 0.0))),
                   tuple(marker) if marker else None)


def compute_offset(start: StartPoint,
                   content_bbox: BBox,
                   machine_pos: Optional[tuple[float, float, float]] = None
                   ) -> tuple[float, float]:
    """计算 G-code 的 origin_offset（机器坐标 = 文档坐标 + offset）。

    * ``canvas``：把内容包围盒的「左下角」放到 ``start.point``。
    * ``origin``：offset = (0, 0)（文档坐标即机器坐标）。
    * ``current``：让内容包围盒左下角对齐机器当前位置。
    """
    if start.mode == MODE_ORIGIN:
        return (0.0, 0.0)
    if start.mode == MODE_CURRENT:
        if machine_pos is None:
            return (0.0, 0.0)
        mx, my = machine_pos[0], machine_pos[1]
        if content_bbox.is_empty:
            return (mx, my)
        return (mx - content_bbox.x0, my - content_bbox.y0)
    if start.mode == MODE_REGISTERED:
        # 笔位校准需要轴映射与标记共同解析，由 registered_offset() 负责；
        # 这里返回 0 仅作兜底，调用方（MainWindow）不会走到本分支。
        return (0.0, 0.0)
    # canvas：把包围盒左下角放到指定点
    px, py = start.point
    if content_bbox.is_empty:
        return (px, py)
    return (px - content_bbox.x0, py - content_bbox.y0)


def registered_offset(start: StartPoint, axis) -> Optional[tuple[float, float]]:
    """笔位校准模式的文档域偏移：让**标记所在的纸面点**对应到记录的机器坐标。

    ``start.point`` = 校准时笔的机器坐标 M；``start.pen_marker`` = 红色标记的
    机器坐标（用户在画布上放的位置）。``axis`` 是仅含轴映射的 :class:`GCodeConfig`
    （``origin_offset=0``）。

    取标记的纸面点 P 与 M 的纸面点，偏移 = M_page − P，于是
    ``map_point(P) == M``：内容写在画布上的相对位置不变，而机器坐标被整体
    标定到物理笔位。标记的显示只依赖轴映射，因此**标记原地不动**。

    无标记时返回 ``None``（调用方退化为零偏移）。
    """
    if start.pen_marker is None:
        return None
    px, py = axis.unmap_point((start.pen_marker[0], start.pen_marker[1]))
    mx, my = axis.unmap_point((start.point[0], start.point[1]))
    return (mx - px, my - py)


def content_bbox(doc: Document, visible_only: bool = True) -> BBox:
    box = BBox()
    for o in doc.objects:
        if visible_only and not o.visible:
            continue
        box = box.union(o.world_bbox())
    return box
