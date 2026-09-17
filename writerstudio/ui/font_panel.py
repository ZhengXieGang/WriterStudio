"""字体面板：直观展示字体列表（缩略图 + 名称 + 类型 + 字数）、支持导入外部字体。

全量解析字体是重活（.gfont 中文库一款几百毫秒、大型 TrueType 数秒），
因此面板分**两阶段**在后台线程加载：

    1. **轻量缩略图**：只解析示例文字（``中Aa123``）对应的字形
       （:mod:`~writerstudio.fonts.preview`），毫秒级一款，并落盘缓存
       （按 路径+mtime+示例文字 键控，重启零解析直接回填）；
    2. **全量解析**：逐款 ``entry.load()`` 回填字形数并预热字体缓存，
       较慢但不阻塞界面，且随时可停。

缩略图渲染：白底，示例文字按各自 advance 排成一行，整体等比缩放居中
（Y 轴翻转，因为字体坐标 Y 向上、位图 Y 向下）。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QStandardPaths, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..fonts.manager import FontManager
from ..fonts.preview import SAMPLE_CHARS, load_preview
from . import filedialog
from .style import unify_inputs

_KIND_LABEL = {
    "hershey": "Hershey 单线",
    "stroke-json": "单线笔画库",
    "truetype": "TrueType 轮廓",
    "gcode": "gcode 字库",
    "gfont": "gfont 单线",
}

_THUMB_W = 118
_THUMB_H = 34
_THUMB_BG = QColor(255, 255, 255)     # 缩略图白底
_THUMB_INK = QColor(40, 42, 55)


def _sample_chars(font) -> list[str]:
    """挑几个能代表该字体面貌的字符：优先 ``中文Aa123``，全缺时退回任意字形。"""
    picked = [c for c in SAMPLE_CHARS if font.has(c)]
    if len(picked) < 3:
        for c in sorted(font.glyphs.keys()):
            if c not in picked and c.strip():
                picked.append(c)
            if len(picked) >= 5:
                break
    return picked[:7]


def render_font_image(font, width: int = _THUMB_W, height: int = _THUMB_H,
                      color: QColor | None = None) -> Optional[QImage]:
    """把字体的示例字符渲染成一张等比缩放的缩略图（QImage，可离线程绘制）。"""
    chars = _sample_chars(font)
    if not chars:
        return None
    # 按 advance 把示例字符排成一行（字体单位）
    placed: list[list[tuple[float, float]]] = []
    x = 0.0
    for ch in chars:
        g = font.glyph(ch)
        if g is None:
            continue
        for s in g.strokes:
            if s:
                placed.append([(x + px, py) for px, py in s])
        adv = g.advance or font.default_advance or font.units_per_em * 0.5
        x += adv
    pts = [p for s in placed for p in s]
    if not pts:
        return None

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    w = max(1e-6, x1 - x0)
    h = max(1e-6, y1 - y0)
    margin = 3.0
    scale = min((width - 2 * margin) / w, (height - 2 * margin) / h)
    ox = (width - w * scale) / 2.0 - x0 * scale
    oy = margin + y1 * scale          # 使 y1（最高点）落在上边距

    img = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    img.fill(_THUMB_BG)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(color or _THUMB_INK)
    pen.setWidthF(1.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    for s in placed:
        if len(s) == 1:
            painter.drawPoint(int(ox + s[0][0] * scale), int(oy - s[0][1] * scale))
        else:
            poly = QPolygonF([QPointF(ox + px * scale, oy - py * scale)
                              for px, py in s])
            painter.drawPolyline(poly)
    painter.end()
    return img


def font_thumbnail(font, width: int = _THUMB_W, height: int = _THUMB_H,
                   color: QColor | None = None) -> Optional[QPixmap]:
    """把字体的示例字符渲染成缩略图（QPixmap 版，须在 GUI 线程调用）。"""
    img = render_font_image(font, width, height, color)
    return None if img is None else QPixmap.fromImage(img)


# --------------------------------------------------------------- 缩略图缓存
def _thumb_cache_key(entry) -> str:
    """缓存键：格式|路径|mtime|示例文字|尺寸。取不到 mtime 返回空（不缓存）。"""
    try:
        st = entry.path.stat()
        seed = (f"{entry.kind}|{entry.path}|{st.st_mtime_ns}"
                f"|{SAMPLE_CHARS}|{_THUMB_W}x{_THUMB_H}")
    except OSError:
        return ""
    return hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()


def _thumb_cache_dir() -> Path:
    return Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "thumbs"


def _thumb_cache_load(key: str) -> Optional[QImage]:
    if not key:
        return None
    img = QImage(str(_thumb_cache_dir() / f"{key}.png"))
    return None if img.isNull() else img


def _thumb_cache_save(key: str, img: QImage) -> None:
    if not key or img is None:
        return
    try:
        d = _thumb_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        img.save(str(d / f"{key}.png"))
    except Exception:
        pass            # 缓存写失败不影响功能


class _FontInfoWorker(QThread):
    """后台两阶段加载：先逐款出轻量缩略图，再全量解析填字形数。"""

    previewReady = Signal(str, object)   # name, QImage|None（轻量，先出图）
    loaded = Signal(str, object)         # name, FontFamily | None（None = 加载失败）

    def __init__(self, names: list[str], manager: FontManager) -> None:
        super().__init__()
        self._names = list(names)
        self._manager = manager
        self._stop = False
        self._paused = False

    @property
    def stopped(self) -> bool:
        """是否被请求过提前停止（区别于自然跑完）。"""
        return self._stop

    def stop(self) -> None:
        self._stop = True

    def pause(self) -> None:
        """请求在「下一款字体之间」暂停解析（画布交互期间让路）。"""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def run(self) -> None:  # noqa: D102
        # 整体兜底：QThread.run 里抛出的未捕获异常在 PySide6 下会
        # std::terminate 直接把整个进程带崩（偶发「未响应后闪退」的
        # 一类来源），这里绝不放异常出去。
        try:
            self.setPriority(QThread.LowPriority)   # 字体解析让着 UI 线程
            self._run_locked()
        except Exception:
            pass
        finally:
            self._paused = False

    def _run_locked(self) -> None:
        # 字体解析是纯 Python，全程握着 GIL；默认 5ms 的线程切换间隔意味着
        # UI 线程的每个事件处理都可能先等上几毫秒才能拿到 GIL——启动初期
        # 缩放/拖动会成片掉帧（实测移动拖拽中位 1.2ms → 加载期间 18ms）。
        # 解析期间把切换间隔压到 1ms，UI 每次最多让一步就能抢到 GIL；
        # 线程结束后立即恢复系统默认。
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(0.001)
        try:
            self._run_phases()
        finally:
            sys.setswitchinterval(old_interval)

    def _wait_if_paused(self) -> None:
        while self._paused and not self._stop:
            self.msleep(30)

    def _run_phases(self) -> None:
        # ---- 阶段 1：轻量缩略图（只解析示例字符；命中缓存零解析）----
        for i, name in enumerate(self._names):
            if self._stop:
                return
            self._wait_if_paused()
            entry = self._manager.get_entry(name)
            if entry is None:
                continue
            try:
                img = self._preview_image(entry)
            except Exception:
                img = None
            if self._stop:
                return
            self.previewReady.emit(name, img)
            # 大字库解析动辄几百 ms，纯 Python 解析与 UI 线程争抢 GIL；
            # 每款之间让出一点时间，启动阶段操作不掉帧
            if i % 8 == 7:
                self.msleep(15)
        # ---- 阶段 2：全量解析（字形数 + 预热字体缓存；大库较慢但在后台）----
        for name in self._names:
            if self._stop:
                return
            self._wait_if_paused()
            entry = self._manager.get_entry(name)
            if entry is None:
                continue
            try:
                fam = entry.load()
            except Exception:
                fam = None
            if self._stop:
                return
            self.loaded.emit(name, fam)
            self.msleep(5)               # 大库（如 800ms 级）之后喘口气

    def _preview_image(self, entry) -> Optional[QImage]:
        key = _thumb_cache_key(entry)
        img = _thumb_cache_load(key)
        if img is not None:
            return img
        preview = load_preview(entry.path, entry.kind)
        if preview is not None:
            img = render_font_image(preview)
        else:
            # 快路径不支持的格式（内置 hershey/gcode 等小库）：全量加载
            fam = entry.try_load()
            img = None if fam is None else render_font_image(fam)
        if img is not None:
            _thumb_cache_save(key, img)
        return img


# 已启动、尚未结束的加载线程。QThread 不能被 GC 回收，否则运行中被销毁会崩溃；
# 这里持引用，线程结束后自动移除。
_LIVE_WORKERS: set["_FontInfoWorker"] = set()


class FontPanel(QDockWidget):
    """字体管理器面板。"""

    searchDirAdded = Signal(str)   # 新增外部字体目录
    pinHideChanged = Signal()      # 置顶/隐藏变化（主窗口负责持久化）

    def __init__(self, manager: FontManager, parent: Optional[QWidget] = None) -> None:
        super().__init__("字体库", parent)
        self.manager = manager
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self._worker: Optional[_FontInfoWorker] = None
        self._counts: dict[str, int] = {}     # name → 字形数（-1 表示加载失败）
        self._thumbs: dict[str, QImage] = {}  # name → 轻量缩略图（阶段 1 产出）
        self._kind_filter = ""
        # 延迟启动加载（窗口快速关闭时不再白跑）
        self._load_timer = QTimer(self)
        self._load_timer.setSingleShot(True)
        self._load_timer.setInterval(250)
        self._load_timer.timeout.connect(self._ensure_worker)
        # 画布交互期间暂停后台加载：单发定时器到点后自动恢复
        self._suspend_timer = QTimer(self)
        self._suspend_timer.setSingleShot(True)
        self._suspend_timer.timeout.connect(self._resume_loading)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 6, 6, 6)

        # 搜索 + 类型过滤
        filt = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索字体名…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _: self._populate())
        filt.addWidget(self.search_edit, 1)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("全部类型", "")
        for key, label in _KIND_LABEL.items():
            self.kind_combo.addItem(label, key)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_filter)
        filt.addWidget(self.kind_combo)
        layout.addLayout(filt)

        self.info = QLabel("—")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color:#666;")
        layout.addWidget(self.info)

        self.list = QListWidget()
        self.list.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self.list.setUniformItemSizes(True)
        self.list.setSpacing(1)
        self.list.currentItemChanged.connect(self._on_current)
        layout.addWidget(self.list, 1)

        from .icons import icon as _qicon
        btns = QHBoxLayout()
        imp = QPushButton("导入字体…")
        _ic = _qicon("import_font")
        if _ic is not None:
            imp.setIcon(_ic)
        imp.clicked.connect(self._import)
        addir = QPushButton("添加字体目录…")
        addir.setToolTip("扫描一个外部字体目录")
        _ic = _qicon("add_dir")
        if _ic is not None:
            addir.setIcon(_ic)
        addir.clicked.connect(self._add_dir)
        btns.addWidget(imp)
        btns.addWidget(addir)
        layout.addLayout(btns)

        btns2 = QHBoxLayout()
        self.pin_btn = QPushButton("置顶")
        self.pin_btn.setToolTip("把常用字体排到列表/下拉的最前面，再次点击取消置顶")
        _ic = _qicon("pin")
        if _ic is not None:
            self.pin_btn.setIcon(_ic)
        self.pin_btn.clicked.connect(self._toggle_pin)
        hide_btn = QPushButton("隐藏")
        hide_btn.setToolTip("在字体库与各下拉列表中隐藏该字体（文件保留在原处）")
        _ic = _qicon("hide")
        if _ic is not None:
            hide_btn.setIcon(_ic)
        hide_btn.clicked.connect(self._hide_current)
        hidden_btn = QPushButton("已隐藏…")
        hidden_btn.setToolTip("查看并恢复已隐藏的字体")
        hidden_btn.clicked.connect(self._show_hidden_dialog)
        rm = QPushButton("移除")
        rm.setToolTip("从字体库移除该字体；用户字体删除文件，\n"
                      "内置字体仅从列表移除（文件保留，可在「已隐藏…」恢复）")
        _ic = _qicon("delete")
        if _ic is not None:
            rm.setIcon(_ic)
        rm.clicked.connect(self._remove)
        reload_btn = QPushButton("重新扫描")
        reload_btn.clicked.connect(self._rescan)
        _ic = _qicon("rescan")
        if _ic is not None:
            reload_btn.setIcon(_ic)
        # 五个按钮分两行：挤一行时面板最小宽被撑到 ~450px，远超默认
        # 停靠宽 280px——Qt 会强制把整列撑宽，画布被挤出画面外
        btns2.addWidget(self.pin_btn)
        btns2.addWidget(hide_btn)
        btns2.addWidget(hidden_btn)
        btns2.addStretch(1)
        layout.addLayout(btns2)
        btns3 = QHBoxLayout()
        btns3.addWidget(rm)
        btns3.addWidget(reload_btn)
        btns3.addStretch(1)
        layout.addLayout(btns3)

        self.setWidget(body)
        # 只在本面板**实际可见**时才后台加载字形信息。字体面板默认与
        # 手写扰动/参考层合并为标签组，非当前标签时不可见 → 不加载，避免
        # 每次打开主窗口都拉起一轮全字库解析（测试/启动都更快）。
        self._populate()
        unify_inputs(self)

    # --------------------------------------------------------------- 显示/隐藏
    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 稍作延迟再加载：窗口若很快被关闭（如单元测试、快速开关面板），
        # 定时器随之取消，不会白跑一轮全字库解析。
        self._load_timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._load_timer.stop()
        self._stop_worker()

    def ensure_loaded(self) -> None:
        """立即启动后台加载（可见性检测之外的手动触发）。"""
        if self.isVisible():
            self._ensure_worker()

    def suspend_loading(self, ms: int = 800) -> None:
        """画布正在交互（按下/滚轮/拖动）：后台字体解析暂停 ``ms`` 毫秒。

        字库解析是纯 Python、与 UI 争抢 GIL，即便把切换间隔压到 1ms，拖动
        缩放仍有可感毛刺；交互期间直接让解析线程睡等，松手后自动续跑。
        线程尚未启动时（启动定时器还在倒计时）则顺延启动。
        """
        if self._worker is not None and self._worker.isRunning():
            self._worker.pause()
            self._suspend_timer.start(ms)
        elif self._load_timer.isActive():
            self._load_timer.start(max(ms, self._load_timer.remainingTime()))

    def _resume_loading(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.resume()

    # --------------------------------------------------------------- 刷新
    def refresh(self) -> None:
        """重建列表并（在可见时）启动后台信息加载。

        注意：**不会**阻塞等待正在运行的线程——它逐款加载完后自行回填；
        重复调用 refresh 只是重建列表，不会重复拉起线程、也不会卡界面。
        """
        self._populate()
        if self.isVisible():
            self._load_timer.start()

    def _rescan(self) -> None:
        """真正重扫磁盘：内置/用户/外部目录重新登记（此前只会重建列表，
        新拷入/删除的字体文件不会反映出来）。"""
        self._stop_worker()
        self.manager.refresh()
        self.refresh()

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        names = [e.name for e in self.manager.entries()]
        if not names:
            return
        worker = _FontInfoWorker(names, self.manager)
        worker.previewReady.connect(self._on_preview_ready)
        worker.loaded.connect(self._on_font_loaded)
        worker.finished.connect(self._on_worker_finished)
        _LIVE_WORKERS.add(worker)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if worker is not None:
            _LIVE_WORKERS.discard(worker)
        if self._worker is worker:
            self._worker = None
        # 上一轮被 hideEvent 提前叫停 → 部分字体停留在「加载中…」。
        # 面板可见时自动补跑一轮（自然跑完则不重启，避免循环加载）。
        if (self.isVisible() and self._worker is None
                and worker is not None and getattr(worker, "stopped", False)):
            self._load_timer.start()

    def _stop_worker(self) -> None:
        """请求后台线程停止。

        只置停止标志、不阻塞等待：线程会在下一款字体之间自行退出，其间发出的
        ``loaded`` 信号因接收者已销毁而被 Qt 自动断开，不会回调到已删控件。
        引用保留到 ``finished`` 才清除，避免隐藏/显示来回切时重复起线程。
        """
        if self._worker is not None:
            self._worker.stop()

    def shutdown(self) -> None:
        """窗口关闭时调用：停止后台加载并**等待线程退出**。

        只置标志不等待的话，QThread 对象可能在解析大字库（几百 ms～秒级）
        期间随窗口销毁，触发「QThread: Destroyed while thread is still
        running」甚至退出段错误。
        """
        self._load_timer.stop()
        self._suspend_timer.stop()
        self._stop_worker()
        if self._worker is not None:
            self._worker.wait(5000)

    def _on_kind_filter(self) -> None:
        self._kind_filter = self.kind_combo.currentData() or ""
        self._populate()

    def _visible_entries(self):
        q = self.search_edit.text().strip().lower()
        out = []
        for e in self.manager.visible_entries():
            if self._kind_filter and e.kind != self._kind_filter:
                continue
            if q and q not in e.name.lower() and q not in e.display_name.lower():
                continue
            out.append(e)
        return out

    def _populate(self) -> None:
        """（重建）列表，保留当前选择。"""
        prev = self.list.currentItem()
        prev_name = prev.data(Qt.UserRole) if prev is not None else None
        self.list.clear()
        for e in self._visible_entries():
            item = self._make_item(e)
            self.list.addItem(item)
        # 恢复选择
        if prev_name:
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.UserRole) == prev_name:
                    self.list.setCurrentRow(i)
                    break
        self._update_info(self.list.currentItem())

    def _make_item(self, entry) -> QListWidgetItem:
        count = self._counts.get(entry.name)
        item = QListWidgetItem()
        item.setData(Qt.UserRole, entry.name)
        item.setToolTip(str(entry.path))
        self._fill_item(item, entry, count, entry._loaded)
        return item

    def _fill_item(self, item: QListWidgetItem, entry, count, font) -> None:
        kind = _KIND_LABEL.get(entry.kind, entry.kind)
        if count is None:
            meta = "…" if font is None else f"{font.coverage()} 字"
        elif count < 0:
            meta = "加载失败"
        else:
            meta = f"{count} 字"
        pin_tag = "[置顶] " if self.manager.is_pinned(entry.name) else ""
        item.setText(f"{pin_tag}{entry.display_name}\n{kind} · {meta}")
        # 缩略图：优先用阶段 1 的轻量图（搜索/过滤重建列表时也秒显），
        # 没有才当场渲染（字体已全量加载的场合）
        img = self._thumbs.get(entry.name)
        if img is not None:
            item.setIcon(QPixmap.fromImage(img))
        elif font is not None:
            thumb = font_thumbnail(font)
            if thumb is not None:
                item.setIcon(thumb)
                self._thumbs[entry.name] = thumb.toImage()
        if count is not None and count < 0:
            item.setForeground(QColor(170, 60, 60))

    def _on_preview_ready(self, name: str, img) -> None:
        """阶段 1：轻量缩略图就绪 → 立即回填该行（不必等全量解析）。"""
        if img is None or img.isNull():
            return
        self._thumbs[name] = img
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.UserRole) == name:
                item.setIcon(QPixmap.fromImage(img))
                break

    def _on_font_loaded(self, name: str, font) -> None:
        """后台加载完成一款 → 回填该行（缩略图在 GUI 线程绘制）。"""
        entry = self.manager.get_entry(name)
        if entry is None:
            return
        self._counts[name] = font.coverage() if font is not None else -1
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.UserRole) == name:
                self._fill_item(item, entry, self._counts[name], font)
                break
        cur = self.list.currentItem()
        if cur is not None and cur.data(Qt.UserRole) == name:
            self._update_info(cur)

    def _update_info(self, item: Optional[QListWidgetItem]) -> None:
        total = len(self.manager.entries())
        if item is None:
            self.info.setText(f"共 {total} 款字体")
            return
        name = item.data(Qt.UserRole)
        entry = self.manager.get_entry(name)
        if entry is None:
            return
        kind = _KIND_LABEL.get(entry.kind, entry.kind)
        count = self._counts.get(name)
        if count is None:
            glyph_line = "字形：加载中…"
        elif count < 0:
            glyph_line = "字形：无法加载"
        else:
            glyph_line = f"字形：{count} 个"
        loaded = entry._loaded
        em = f"\nem：{loaded.units_per_em:g}" if loaded is not None else ""
        self.info.setText(f"{entry.display_name}  [{kind}]\n{glyph_line}{em}")

    # --------------------------------------------------------------- 列表交互
    def _on_current(self, item: Optional[QListWidgetItem], *_a) -> None:
        self._update_info(item)
        self._update_pin_button(item)

    # ------------------------------------------------------------- 置顶/隐藏
    def _update_pin_button(self, item: Optional[QListWidgetItem]) -> None:
        if item is None:
            self.pin_btn.setEnabled(False)
            self.pin_btn.setText("置顶")
            return
        self.pin_btn.setEnabled(True)
        self.pin_btn.setText(
            "取消置顶" if self.manager.is_pinned(item.data(Qt.UserRole))
            else "置顶")

    def _toggle_pin(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        self.manager.pin(name, not self.manager.is_pinned(name))
        self._populate()
        self._update_pin_button(self.list.currentItem())
        self.pinHideChanged.emit()

    def _hide_current(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        entry = self.manager.get_entry(name)
        if entry is None:
            return
        if QMessageBox.question(
                self, "隐藏字体",
                f"在字体库与各下拉列表中隐藏「{entry.display_name}」？\n\n"
                "文件保留在原处，可随时在「已隐藏…」中恢复显示。"
                ) == QMessageBox.Yes:
            self.manager.hide(name, True)
            self.refresh()
            self.pinHideChanged.emit()

    def _show_hidden_dialog(self) -> None:
        names = self.manager.hidden_names()
        dlg = QDialog(self)
        dlg.setWindowTitle("已隐藏的字体")
        v = QVBoxLayout(dlg)
        tip = QLabel("以下字体在界面中隐藏（文件未删除）。选中后点「恢复显示」，"
                     "可多选。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        lst = QListWidget()
        lst.setSelectionMode(QAbstractItemView.MultiSelection)
        for n in names:
            entry = self.manager.get_entry(n)
            lst.addItem(entry.display_name if entry else n)
        lst.setMinimumSize(280, 220)
        v.addWidget(lst, 1)
        btns = QDialogButtonBox()
        ok = btns.addButton("恢复显示", QDialogButtonBox.AcceptRole)
        btns.addButton(QDialogButtonBox.Cancel)
        ok.setEnabled(False)
        lst.itemSelectionChanged.connect(
            lambda: ok.setEnabled(bool(lst.selectedItems())))
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        v.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            restored = [names[lst.row(i)] for i in lst.selectedIndexes()]
            for n in restored:
                self.manager.hide(n, False)
            if restored:
                self.refresh()
                self.pinHideChanged.emit()

    # --------------------------------------------------------------- 操作
    def _import(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "导入字体", filedialog.start_dir(),
            "字体文件 (*.gfont *.jhf *.json *.ttf *.otf *.ttc *.woff *.woff2);;所有文件 (*)",
        )
        if not paths:
            return
        ok, fail = 0, []
        for p in paths:
            try:
                self.manager.import_font(p)
                ok += 1
            except Exception as exc:
                fail.append(f"{p}: {exc}")
        filedialog.remember(paths[0])
        self.refresh()
        if fail:
            QMessageBox.warning(self, "部分导入失败", "\n".join(fail[:10]))

    def _add_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择字体目录",
            filedialog.start_dir())
        if not d:
            return
        try:
            n = self.manager.add_search_dir(d)
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))
            return
        filedialog.remember(d)
        self.searchDirAdded.emit(d)
        self.refresh()
        QMessageBox.information(self, "已添加字体目录",
                                f"从\n{d}\n发现 {n} 款字体。")

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        name = item.data(Qt.UserRole)
        entry = self.manager.get_entry(name)
        if entry is None:
            return
        try:
            is_user = self.manager.user_dir.resolve() in entry.path.resolve().parents
        except Exception:
            is_user = False
        if not is_user:
            # 内置字体打包在程序目录里，文件不删：从列表移除 = 记住隐藏，
            # 「重新扫描」/「已隐藏…」都能恢复显示
            if QMessageBox.question(
                    self, "移除内置字体",
                    f"从字体库移除内置字体「{entry.display_name}」？\n\n"
                    "文件保留在程序目录中，可随时在「已隐藏…」里恢复显示。"
                    ) == QMessageBox.Yes:
                self.manager.hide(name, True)
                self.refresh()
                self.pinHideChanged.emit()
            return
        if QMessageBox.question(self, "移除字体",
                                f"确定移除字体「{entry.display_name}」？\n"
                                "将同时删除字体库中的文件。"
                                ) == QMessageBox.Yes:
            self.manager.remove(name)
            self.refresh()
