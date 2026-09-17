""".gfont 单线字体解析器。

文件格式::

    ┌── 文件头 ──────────────────────────────────────────┐
    │ u32 version(5..9) │ u32 加密元数据块长度            │
    │ 加密块：XTEA(16 循环)+CBC 变体，密钥 1989082619920828 │
    │   解密后（大端）：                                   │
    │     UTF 作者 │ i32 类别 │ UTF 显示名 │ UTF 副名      │
    │     UTF 描述 │ i32 默认字号 │ i32 字形数             │
    │     v≥2: UTF 版本串  v≥4: 跳过一个 UTF              │
    │     v≥7: UTF uuid    v≥9: 6×f32 度量(默认步距等)    │
    │ 随后是若干「预览字形」记录（≤30 个，可忽略）          │
    └───────────────────────────────────────────────────┘
    ┌── 之后是一个标准 ZIP 归档 ─────────────────────────┐
    │ 每个条目名 = 字符的 Unicode 码点十进制字符串        │
    │ 条目内容（大端）:                                  │
    │     u16 codepoint                                   │
    │     u32 n          —— 浮点分量个数（= 2 × 点数）    │
    │     f32[n]         —— x0,y0,x1,y1,... 坐标         │
    │     u32 point_count                                 │
    │     u8[point_count] —— 抬落笔标志（0=抬笔/起笔，1=落笔）│
    └───────────────────────────────────────────────────┘

坐标系：原始数据 **Y 轴向下**，墨迹坐标各自相对原点摆放（无统一格子）。
本解析器统一转为项目约定：**Y 轴向上、基线 y=0**。

度量：.gfont 字库只存墨迹，**不含任何边距/步距信息**，排版所需度量
按字符类别推断（全部为本项目原创启发式）：

* ``units_per_em``：含表意文字的字库取其墨迹宽度的 97 百分位（≈书写格子）；
  纯西文/符号字库改用大写字高中位数锚定（cap ≈ 0.70 em），否则不同符号
  字库的 em 相差一个量级，混排时符号会比汉字大出数倍。
* 步距（advance）= 墨迹宽 + 类别边距（见 ``_CLASS_BEARINGS``），相邻字符
  墨迹间隙全字体一致，消灭「宽字贴死、窄字空洞」；细长字形有最小步距。
* 墨迹左缘统一对齐到类别左边距（否则按原始坐标会出现标点左置悬空、
  首字伸出行外）。
* 个别「写大了」的标点按类别相对 cap 高收小（只缩不放，见 ``_PUNCT_SIZE``）；
  汉字墨迹宽超过 0.90 em 时收到 0.90 em；孤立扁平横画（「一」）另收到
  0.60 em——手写单横画明显短于字身，不收会显得突兀地长；
* **盒形字分级**（印刷字号层级，实测 Noto/等线）：「口/囗」是独立小字
  （无内容空框）→ 收到中位宽 ×0.75、中位高 ×0.80；「回/田/国/因/园/圈」
  的字框是字身（框内有内容）→ 收到中位宽 ×0.86、中位高 ×0.92。
  造字时方框普遍写得撑满格子；两者绝不能混为一级（口与国字框同宽
  是错的）。检测全走几何特征：外框四边贴边覆盖 + 全部墨迹贴边 +
  框心有无墨迹。
"""

from __future__ import annotations

import statistics
import struct
import zipfile
from pathlib import Path
from typing import Optional

from .model import FontFamily, Glyph

KIND_GFONT = "gfont"

# kvenjoy 头部加密密钥（int[16]，即十进制串 "1989082619920828"）
_KEY = (1, 9, 8, 9, 0, 8, 2, 6, 1, 9, 9, 2, 0, 8, 2, 8)
_M32 = 0xFFFFFFFF
_DELTA = 0x9E3779B9


def _pack_be(b8) -> tuple:
    return (((b8[0] << 24) | (b8[1] << 16) | (b8[2] << 8) | b8[3]) & _M32,
            ((b8[4] << 24) | (b8[5] << 16) | (b8[6] << 8) | b8[7]) & _M32)


def _key_words() -> tuple:
    return tuple((( _KEY[i] << 24) | (_KEY[i + 1] << 16) | (_KEY[i + 2] << 8) | _KEY[i + 3]) & _M32
                 for i in range(0, 16, 4))


_KW = _key_words()


def _xtea_dec(v0: int, v1: int) -> bytes:
    """官方 c()：XTEA 两轮解密 ×16 循环，sum 从 delta<<4 递减；大端输出。"""
    s = (_DELTA << 4) & _M32
    for _ in range(16):
        v1 = (v1 - ((((v0 << 4) & _M32) + _KW[2]) & _M32 ^ ((v0 + s) & _M32)
                    ^ (((v0 >> 5) + _KW[3]) & _M32))) & _M32
        v0 = (v0 - ((((v1 << 4) & _M32) + _KW[0]) & _M32 ^ ((v1 + s) & _M32)
                    ^ (((v1 >> 5) + _KW[1]) & _M32))) & _M32
        s = (s - _DELTA) & _M32
    return struct.pack(">II", v0, v1)


def _c(mixed8) -> list:
    v0, v1 = _pack_be(mixed8)
    return list(_xtea_dec(v0, v1))


def _decrypt_header(enc: bytes) -> bytes:
    """解密 v5+ 的加密元数据块，返回去填充后的明文。

    结构：首块解密作 IV（其首字节低 3 位记录填充偏移），随后 CBC 式链；
    头部 1 字节填充标记 + 2 字节随机填充，尾部 7 字节填充。
    加密块长度不是 8 的倍数时按官方行为截断到整块（个别文件头部尾部损坏）。
    """
    data = list(enc[: (len(enc) // 8) * 8])
    n8 = len(data)
    if n8 < 8:
        return bytes(data)
    e = _c(data[0:8])
    pad = e[0] & 7
    plain_len = n8 - 1 - pad - 2 - 7
    prev = [0] * 8          # XOR 伙伴：首块输出时为 0，其后为上一密文块
    off = 8
    idx = pad + 1
    out = bytearray()

    def block_step() -> None:
        nonlocal prev, e, off, idx
        prev = data[off - 8:off]
        e = _c([e[i] ^ data[off + i] for i in range(8)])
        off += 8
        idx = 0

    cnt = 1                                     # 阶段 1：跳过 2 字节头部填充
    while cnt <= 2:
        if idx < 8:
            idx += 1
            cnt += 1
        elif idx == 8:
            block_step()
    remaining = plain_len                       # 阶段 2：输出明文
    while remaining > 0:
        if idx < 8:
            out.append(e[idx] ^ prev[idx])
            idx += 1
            remaining -= 1
        elif idx == 8:
            block_step()
    cnt = 1                                     # 阶段 3：跳过 7 字节尾部填充
    while cnt <= 7:
        if idx < 8:
            idx += 1
            cnt += 1
        elif idx == 8:
            block_step()
    return bytes(out)


def _read_utf(buf: bytes, off: int) -> tuple:
    """Java DataInput.readUTF：u16 长度 + 修改版 UTF-8。"""
    ln = struct.unpack_from(">H", buf, off)[0]
    off += 2
    raw = bytes(buf[off:off + ln])
    return raw.replace(b"\xc0\x80", b"\x00").decode("utf-8", "replace"), off + ln


# 视为「全角」的码点区间（CJK 统一表意文字、扩展、兼容、全角标点等）
_FULLWIDTH_RANGES = (
    (0x1100, 0x115F),    # 韩文字母
    (0x2E80, 0x303E),    # CJK 部首、标点
    (0x3041, 0x33FF),    # 假名、CJK 兼容
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本区
    (0xA000, 0xA4CF),    # 彝文
    (0xAC00, 0xD7A3),    # 韩文音节
    (0xF900, 0xFAFF),    # 兼容表意
    (0xFE30, 0xFE4F),    # CJK 兼容形式
    (0xFF00, 0xFF60),    # 全角 ASCII
    (0xFFE0, 0xFFE6),    # 全角符号
    (0x20000, 0x3FFFD),  # 扩展 B+
)


def _is_fullwidth(cp: int) -> bool:
    for lo, hi in _FULLWIDTH_RANGES:
        if lo <= cp <= hi:
            return True
    return False


# 字形类别 → (左边距, 右边距)（em 单位）。
# .gfont 字库只存「墨迹」，不含任何边距/步距信息（字形坐标各自相对
# 原点随意摆放），排版所需的边距只能按类别统一推断：
# 相邻字符墨迹间隙 = 前字右边距 + 后字左边距，全字体一致，消灭
# 「宽字贴死、窄字空洞」的格子式间距。
_CLASS_BEARINGS = {
    "cjk": (0.07, 0.07),          # 表意文字：左右对称
    "punct-small": (0.04, 0.14),  # 、。，贴前字，尾部留呼吸
    "punct-colon": (0.06, 0.14),  # ：；同上
    "punct-tall": (0.08, 0.08),   # ！？居中
    "fw-bracket": (0.06, 0.06),
    "fw-latin": (0.06, 0.06),
    "ascii": (0.06, 0.06),
    "other": (0.05, 0.05),
}

# 各「过大的标点」类别：(目标墨迹高, 触发阈值) —— 均为 cap 高的倍数，
# 只缩不放。手写造字时标点普遍写得比印刷标准大得多（顿号可达 0.5 cap，
# 印刷标准约 0.15 em ≈ 0.21 cap）。
_PUNCT_SIZE = {
    "punct-small": (0.24, 0.34),
    "punct-colon": (0.55, 0.72),
    "punct-tall": (1.02, 1.15),
}

# 孤立扁平横画（「一」）的收窄：手写里单横画明显短于字身，不该撑满格子。
_FLAT_CJK_H = 0.30      # 扁平判定：墨迹高 < 该值 × em
_FLAT_HZ_CAP = 0.60     # 扁平横画目标宽度（× em）


def _glyph_class(cp: int) -> str:
    """码点 → 度量类别（决定边距与标点尺寸归一）。"""
    if _is_fullwidth(cp):
        if cp in (0x3001, 0x3002, 0xFF0C, 0xFF0E, 0xFF61, 0xFF64):
            return "punct-small"      # 、。，｡､．
        if cp in (0xFF1A, 0xFF1B):
            return "punct-colon"      # ：；
        if cp in (0xFF01, 0xFF1F):
            return "punct-tall"       # ！？
        if 0xFF01 <= cp <= 0xFF5E:
            return "fw-latin"         # 全角字母数字
        if (0x3008 <= cp <= 0x3011 or 0x300C <= cp <= 0x301F
                or cp in (0xFF08, 0xFF09, 0xFF3B, 0xFF3D,
                          0xFF5B, 0xFF5D, 0xFF5F, 0xFF60)):
            return "fw-bracket"       # 括号引号书名号
        return "cjk"                  # 汉字/部首/假名等表意块
    if 0x20 <= cp <= 0x7E:
        return "ascii"
    return "other"


def _cap_height(records: dict) -> float:
    """大写字母墨迹高中位数（作为西文度量的锚）；样本不足返回 0。"""
    hs = [b[3] - b[1] for cp, (_s, b) in records.items()
          if 0x41 <= cp <= 0x5A and b[3] > b[1]]
    if len(hs) < 4:
        return 0.0
    return statistics.median(hs)


def _median_cjk_height(records: dict) -> float:
    hs = [b[3] - b[1] for cp, (_s, b) in records.items()
          if _glyph_class(cp) == "cjk" and b[3] > b[1]]
    if len(hs) < 2:
        return 0.0
    return statistics.median(hs)


def _median_cjk_width(records: dict) -> float:
    ws = [b[2] - b[0] for cp, (_s, b) in records.items()
          if _glyph_class(cp) == "cjk" and b[2] > b[0]]
    if len(ws) < 2:
        return 0.0
    return statistics.median(ws)


def _has_box_frame(strokes, ink_w: float, ink_h: float) -> bool:
    """墨迹是否构成「外框」：四个边各被贴边的笔画覆盖过半。

    用于识别「口/回/田/国/月」这类**盒形字**——造字时方框普遍写得撑满
    整个格子，显得比邻字大一号。按几何特征而非字表判定：对包围盒的
    左/右/上/下四条边，各存在一条笔画贴着它走过至少一半边长（允许
    手写抖动）。手写「口」常分三笔写、并不闭合，因此不要求单笔画
    闭合；一/二/三（横线只有端点落在竖边上）、十/人（竖画在中央而
    不在边缘）都不会命中。
    """
    if ink_w <= 0 or ink_h <= 0:
        return False
    tol = 0.16 * min(ink_w, ink_h)
    pts = [(x, y) for st in strokes for (x, y) in st]
    if len(pts) < 4:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    w, h = x1 - x0, y1 - y0

    def edge_run(vertical: bool, edge: float, need: float) -> bool:
        """某条边被**同一条笔画**贴边覆盖 ≥ need：手写「口」常分三笔、
        一笔一毛不闭合，但每条边总有一条笔画沿它走过；「三」的横线
        只有首尾端点落在左右缘上（单笔画贴边跨度≈0），不会误判。"""
        for st in strokes:
            vals = []
            for x, y in st:
                fixed, free = (x, y) if vertical else (y, x)
                if abs(fixed - edge) <= tol:
                    vals.append(free)
            if len(vals) >= 2 and max(vals) - min(vals) >= need:
                return True
        return False

    return (edge_run(True, x0, 0.5 * h)       # 左缘
            and edge_run(True, x1, 0.5 * h)   # 右缘
            and edge_run(False, y0, 0.5 * w)  # 顶缘
            and edge_run(False, y1, 0.5 * w))  # 底缘


# 盒形字的印刷字号层级（实测 Noto Sans CJK / 等线，相对中位汉字墨迹）：
# 「口」是**独立小字**（宽 ≈0.75×、高 ≈0.80× 中位），而「回/田/国/因/园/圈」
# 的**字框**是字身（宽 ≈0.86×、高 ≈0.92× 中位）——两者差着一级，绝不
# 能混为一类（把口收到和国字框同宽是错的）。
_EMPTY_FRAME_W, _EMPTY_FRAME_H = 0.75, 0.80
_FRAME_W, _FRAME_H = 0.86, 0.92


def _frame_has_interior(strokes, ink_w: float, ink_h: float) -> bool:
    """框内是否有笔画（区分「口/囗」空框与「回/田/国」带内容的字框）。"""
    if ink_w <= 0 or ink_h <= 0:
        return False
    pts = [(x, y) for st in strokes for (x, y) in st]
    if not pts:
        return False
    bx = min(p[0] for p in pts)
    by = min(p[1] for p in pts)
    x0, x1 = bx + 0.30 * ink_w, bx + 0.70 * ink_w
    y0, y1 = by + 0.30 * ink_h, by + 0.70 * ink_h
    for x, y in pts:
        if x0 <= x <= x1 and y0 <= y <= y1:
            return True
    return False


def _all_points_near_perimeter(strokes, ink_w: float, ink_h: float) -> bool:
    """所有墨迹点是否都贴着字形包围盒的边（口 = 三笔沿边走，全部贴边；
    「叫」里作为偏旁的口则不然——其他笔画的墨迹在框外/框中间）。"""
    if ink_w <= 0 or ink_h <= 0:
        return False
    pts = [(x, y) for st in strokes for (x, y) in st]
    if not pts:
        return False
    bx = min(p[0] for p in pts)
    by = min(p[1] for p in pts)
    tol = 0.16 * min(ink_w, ink_h)
    for x, y in pts:
        d = min(x - bx, bx + ink_w - x, y - by, by + ink_h - y)
        if d > tol:
            return False
    return True


def _size_norm_scale(cp: int, ink_w: float, ink_h: float, cap: float,
                     em: float) -> float:
    """个别「写大了」的字形的收小系数（只缩不放，1.0=不动）。

    * 孤立扁平横画（「一」这类，墨迹高 < 0.30 em）→ 收到 0.60 em。手写单横
      画明显短于字身，若与其它宽字同走 0.90 em 上限，仍会比整行字都长；
    * 其余汉字墨迹宽超过 0.90 em → 收到 0.90 em。「二」「三」的长横与字身
      同宽，属这一档，不能跟着扁平规则一起压低；
    * 标点按类别相对 cap 高归一（顿号/句号 ≈ 0.24 cap、比字高 1.02 cap）。
    """
    cls = _glyph_class(cp)
    if cls == "cjk":
        if ink_h < _FLAT_CJK_H * em:
            if ink_w > _FLAT_HZ_CAP * em:
                return _FLAT_HZ_CAP * em / ink_w
            return 1.0
        if ink_w > 0.90 * em:
            return 0.90 * em / ink_w
        return 1.0
    if cls in _PUNCT_SIZE:
        if cap <= 0:
            cap = 0.70 * em
        target, limit = _PUNCT_SIZE[cls]
        if ink_h > limit * cap:
            return target * cap / ink_h
    return 1.0


def _parse_glyph(data: bytes):
    """解析单条字形记录 → (码点, 笔画列表[原始坐标], 墨迹包围盒) 或 None。"""
    if len(data) < 10:
        return None
    cp, n = struct.unpack_from(">HI", data, 0)
    if n == 0 or n % 2:
        return None
    end = 6 + n * 4
    if end + 4 > len(data):
        return None
    pc = struct.unpack_from(">I", data, end)[0]
    if pc != n // 2:
        return None
    flags = data[end + 4:end + 4 + pc]
    if len(flags) != pc:
        return None
    vals = struct.unpack_from(">%df" % n, data, 6)

    strokes: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for i in range(pc):
        x = vals[i * 2]
        y = vals[i * 2 + 1]
        if x < x0: x0 = x
        if x > x1: x1 = x
        if y < y0: y0 = y
        if y > y1: y1 = y
        if flags[i] == 0:                 # 抬笔 → 起新笔画
            if cur:
                strokes.append(cur)
            cur = [(x, y)]
        else:                             # 落笔 → 续画
            if not cur:
                cur = [(x, y)]
            else:
                cur.append((x, y))
    if cur:
        strokes.append(cur)
    if not strokes:
        return None
    return cp, strokes, (x0, y0, x1, y1)


def _entry_codepoint(entry: str) -> Optional[int]:
    """ZIP 条目名 → 码点。

    v≤2 条目名是单个十进制码点；v3+ 增补平面字符以 UTF-16 码元十进制
    串 ``_`` 连接命名（如 😀 → ``55357_56832``），需合并代理对还原。
    非法/越界/孤立代理区名称一律返回 None（跳过该条目）。
    """
    try:
        cp = int(entry)      # 注意：int() 接受 "55_357" 这类下划线分隔，
                             # 含 '_' 的名称必须走代理对分支
    except ValueError:
        cp = None
    if cp is not None and "_" not in entry:
        return cp if _valid_codepoint(cp) else None
    parts = entry.split("_")
    if not parts or len(parts) > 4:
        return None
    try:
        units = [int(p) for p in parts]
    except ValueError:
        return None
    if not all(0 <= u <= 0xFFFF for u in units):
        return None
    try:
        chars = bytes(b for u in units for b in (u >> 8, u & 0xFF)) \
            .decode("utf-16-be")
    except UnicodeDecodeError:
        return None
    if len(chars) != 1:
        return None
    cp = ord(chars)
    return cp if _valid_codepoint(cp) else None


def _valid_codepoint(cp: int) -> bool:
    # 排除越界值与孤立代理区（chr() 会产生非法字形键）
    return 0 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF)


def read_gfont_records(path: str | Path) -> dict[int, tuple[list[list[tuple[float, float]]],
                                                          tuple[float, float, float, float]]]:
    """读取 ``.gfont``，返回 {码点: (笔画, 包围盒)}（原始坐标，Y 向下）。"""
    path = Path(path)
    records: dict[int, tuple] = {}
    with zipfile.ZipFile(path) as zf:
        for entry in zf.namelist():
            cp = _entry_codepoint(entry)
            if cp is None:
                continue
            try:
                data = zf.read(entry)
            except Exception:
                continue
            r = _parse_glyph(data)
            if r is None:
                continue
            _cp_internal, strokes, bbox = r
            records[cp] = (strokes, bbox)
    return records


def parse_gfont(path: str | Path, name: Optional[str] = None,
                sample_limit: int = 3000) -> FontFamily:
    """解析 ``.gfont`` 为 :class:`FontFamily`。

    ``sample_limit`` 为统计度量（pitch/基线）时最多采样的字形数。
    """
    path = Path(path)
    records = read_gfont_records(path)
    if not records:
        raise ValueError(f"{path} 中未解析出任何字形")

    # 显示名优先级：显式参数 > 加密头里的真实名称 > 文件名
    if name is None:
        try:
            name = gfont_metadata(path).get("name") or path.stem
        except Exception:
            name = path.stem

    # -- 统计中文字身，确定 pitch 与基线 --
    full_w: list[float] = []
    full_top: list[float] = []      # 原始 miny（Y 向下 = 上边缘）
    full_bottom: list[float] = []   # 原始 maxy（Y 向下 = 下边缘/基线）
    for cp, (strokes, bbox) in list(records.items())[:sample_limit]:
        if not _is_fullwidth(cp):
            continue
        x0, y0, x1, y1 = bbox
        full_w.append(x1 - x0)
        full_top.append(y0)
        full_bottom.append(y1)

    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * q))]

    if full_w:
        pitch = _pct(full_w, 0.97)
        baseline_raw = _pct(full_bottom, 0.90)
        cell_h = baseline_raw - _pct(full_top, 0.05)
    else:  # 纯西文/符号字体
        allw = [b[2] - b[0] for _, b in records.values()]
        allb = [b[3] for _, b in records.values()]
        pitch = _pct(allw, 0.95)
        baseline_raw = _pct(allb, 0.90)
        cell_h = baseline_raw - _pct([b[1] for _s, b in records.values()], 0.05)
    if pitch <= 0:
        pitch = cell_h or 100.0
    units_per_em = float(pitch)

    # 纯西文/符号字库（无表意文字）的「格子」往往只有少数几个全角字形可
    # 统计，估出的 pitch 不可靠且各字体间差到一个量级；混排时同一字号下
    # 符号会比汉字大出数倍。改用大写字高锚定：cap ≈ 0.70 em（印刷惯例）。
    cap_h = _cap_height(records)
    if _median_cjk_height(records) <= 0 and cap_h > 0:
        units_per_em = cap_h / 0.70

    # -- 生成字形 --
    med_w = _median_cjk_width(records)
    med_h = _median_cjk_height(records)
    glyphs: dict[str, Glyph] = {}
    for cp, (strokes, bbox) in records.items():
        x0, _y0, x1, _y1 = bbox
        ink_w = x1 - x0
        ink_h = _y1 - _y0
        cls = _glyph_class(cp)
        lb, rb = _CLASS_BEARINGS[cls]
        # 盒形字分级（按印刷字号层级，只缩不放）：
        #   * 空框「口/囗」= 独立小字 → 收到 0.75/0.80 × 中位宽/高；
        #   * 框内有内容的字框「回/田/国/…」= 框即字身 → 收到
        #     0.86/0.92 × 中位宽/高。
        # 检测全走几何特征：外框（四边各有一条笔画贴边走过半）+ 全部
        # 墨迹贴边（口分三笔写也命中；「叫」等偏旁口因其他笔画离边
        # 而不命中）+ 框心有无墨迹（区分口与回/田/国）。
        is_frame = (cls == "cjk" and ink_h >= 0.30 * units_per_em
                    and _has_box_frame(strokes, ink_w, ink_h))
        xs = ys = 1.0
        if is_frame:
            if (_all_points_near_perimeter(strokes, ink_w, ink_h)
                    and not _frame_has_interior(strokes, ink_w, ink_h)):
                tw = med_w * _EMPTY_FRAME_W
                th = med_h * _EMPTY_FRAME_H
            else:
                tw = med_w * _FRAME_W
                th = med_h * _FRAME_H
            if tw > 0:
                xs = min(1.0, tw / ink_w)
            if th > 0:
                ys = min(1.0, th / ink_h)
        # Y 向下 → Y 向上，基线归零；随后按类别归一尺寸（绕基线缩放）。
        # 盒形字走专项通道（上面的 xs/ys），不再叠加统一缩放
        s = 1.0 if is_frame else _size_norm_scale(cp, ink_w, ink_h, cap_h,
                                                  units_per_em)
        conv = [[(x * s * xs, (baseline_raw - y) * s * ys) for x, y in st]
                for st in strokes]
        # 步距 = 墨迹宽 + 类别边距（.gfont 数据只有墨迹，无步距信息）
        if cp == 0x20:
            advance = units_per_em * 0.48
        elif cp == 0x3000:
            advance = units_per_em
        else:
            advance = ink_w * s * xs + (lb + rb) * units_per_em
            if cls in ("ascii", "fw-latin", "other"):
                # 细长字形（1 l I |）墨迹极窄，纯按墨迹+边距会挤成一团
                advance = max(advance, units_per_em * 0.30)
        # 墨迹左缘对齐到左边距（原始坐标以各自为基准，必须重定位）
        dx = lb * units_per_em - x0 * s * xs
        if dx != 0.0:
            conv = [[(x + dx, y) for x, y in st] for st in conv]
        glyphs[chr(cp)] = Glyph(char=chr(cp), strokes=conv,
                                advance=float(advance))

    # 缺空格时合成（半角宽）
    if " " not in glyphs:
        glyphs[" "] = Glyph(" ", [], advance=units_per_em * 0.48)

    return FontFamily(
        name=name or path.stem,
        kind=KIND_GFONT,
        units_per_em=units_per_em,
        glyphs=glyphs,
        source=str(path),
    )


def gfont_metadata(path: str | Path) -> dict:
    """解密文件头，读取字体元数据（作者/名称/描述/字数等）。

    返回字段：``version``、``author``（作者）、``name``（显示名）、
    ``sub``（副名/系列）、``desc``（描述）、``default_size``（默认字号）、
    ``glyph_count``（声明字形数）、``metrics``（v9 六个度量浮点，可能为空）。
    解析失败时各字段为空值，不抛异常。
    """
    path = Path(path)
    info: dict = {"version": 0, "author": "", "name": "", "sub": "",
                  "desc": "", "default_size": 0, "glyph_count": 0,
                  "metrics": None}
    try:
        file_size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(8)
            if len(head) < 8:
                return info
            ver, enc_len = struct.unpack_from(">II", head, 0)
            info["version"] = ver
            if ver < 5 or enc_len <= 0 or enc_len > file_size:
                return info
            enc = f.read(enc_len)
        plain = _decrypt_header(enc)
    except Exception:
        return info

    try:
        off = 0
        info["author"], off = _read_utf(plain, off)
        info["category"] = struct.unpack_from(">i", plain, off)[0]
        off += 4
        info["name"], off = _read_utf(plain, off)
        info["sub"], off = _read_utf(plain, off)
        info["desc"], off = _read_utf(plain, off)
        info["default_size"], off = struct.unpack_from(">i", plain, off)[0], off + 4
        info["glyph_count"], off = struct.unpack_from(">i", plain, off)[0], off + 4
        if ver >= 2:
            info["font_version"], off = _read_utf(plain, off)
        if ver >= 9:
            info["metrics"] = list(struct.unpack_from(">6f", plain, off))
    except Exception:
        pass
    # 个别损坏文件（如目录中的方正兰亭超细黑体，加密块长度非 8 倍数）解出的是
    # 乱码字节：解密"成功"但内容为控制符/替换符。校验文本字段，垃圾一律清空，
    # 让上层回退到文件名。
    for key in ("author", "name", "sub", "desc", "font_version"):
        v = info.get(key)
        if isinstance(v, str) and v and (
                "\ufffd" in v
                or any(ord(c) < 0x20 or ord(c) == 0x7F for c in v)
                or sum(c.isprintable() and not c.isspace() for c in v) * 2
                < len(v)):
            info[key] = ""
    return info


def font_names_from_gfont(path: str | Path) -> dict:
    """兼容旧接口：从加密头读取作者/字体名/描述。"""
    m = gfont_metadata(path)
    return {"author": m.get("author", ""), "family": m.get("name", ""),
            "style": m.get("desc", "")}
