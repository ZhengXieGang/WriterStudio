"""主窗口：菜单、工具栏、状态栏、对象/字体面板，整合画布与撤销栈。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QPointF, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..core.document import (
    ROTATE_180,
    ROTATE_CCW,
    ROTATE_CW,
    ROTATE_LABELS,
    Document,
    PageSpec,
    page_rotation,
)
from ..core.geometry import BBox
from ..core.reference import (
    KIND_IMAGE,
    KIND_SVG as REF_KIND_SVG,
    fit_reference_to_box,
)
from ..content.builder import (
    SOURCE_EQUATION,
    SOURCE_LATEX,
    SOURCE_MARKDOWN,
    SOURCE_SVG,
    SOURCE_TIKZ,
    make_equation_object,
    make_markdown_object,
    make_svg_object,
    regenerate_content_object,
)
from ..content.markdown import MarkdownStyle
from ..fonts.builder import (
    TextSpec,
    make_text_object,
    update_text_object,
)
from ..fonts.manager import FontManager
from ..machine.config import machine_to_page, page_to_machine
from ..machine.gcode_gen import generate_from_document
from ..machine.start_point import (
    MODE_CANVAS,
    MODE_ORIGIN,
    MODE_REGISTERED,
    content_bbox,
)
from ..page_presets import PagePresetStore
from ..content.stroke_edits import (
    record_delete as _record_stroke_delete,
    record_modify as _record_stroke_modify,
)
from ..perturb.apply import (
    BASE_STROKES_KEY,
    _strokes_to_data,
    apply_perturb,
    object_reference_size,
    resolve_perturb,
    supports_perturb,
)
from ..perturb.params import PerturbParams
from ..project import (
    FILE_SUFFIX,
    ProjectData,
    load_project,
    regenerate_all,
    save_project,
)
from ..settings import Settings
from .canvas import CanvasView
from .theme import THEMES, apply_theme
from .theme import available as theme_available
from .content_dialog import (
    KIND_EQUATION,
    KIND_LATEX,
    KIND_MARKDOWN,
    KIND_SVG,
    KIND_TIKZ,
    ContentDialog,
)
from .controller import DocumentController, snapshot_object
from . import filedialog
from .font_panel import FontPanel
from .gcode_preview import GCodePreviewDialog
from .machine_panel import MachinePanel
from ..machine.serial_link import State as LinkState
from .page_dialog import PageSetupDialog
from .text_edit_session import TextEditSession
from .perturb_panel import PerturbPanel
from .reference_panel import ReferencePanel, make_reference_from_file
from .text_dialog import TextEditDialog
from .text_props_panel import TextPropsPanel
from .undo_panel import UndoPanel


def _box_corners(box: BBox):
    """包围盒四角（用于对变换后的对象求新包围盒）。"""
    return [(box.x0, box.y0), (box.x1, box.y0), (box.x1, box.y1), (box.x0, box.y1)]


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.setWindowTitle("WriterStudio — ESP32 GRBL 写字机软件")
        self.resize(1280, 840)

        self.settings = settings if settings is not None else Settings()
        self.project_path: str | None = None
        # 文件对话框起始目录的统一 context（见 ui/filedialog.py）：
        # 其它面板（字体库/内容对话框）借此拿到同一套解析，无需各自持有设置
        filedialog.install(
            get_last_dir=self.settings.last_dir,
            get_project_path=lambda: self.project_path,
            set_last_dir=self.settings.set_last_dir,
        )
        self._dirty = False
        self._loaded_ready = False   # 初始化完成前不标记脏
        self.confirm_on_close = True  # 关闭前是否询问未保存修改（测试/自动化可关）
        self._pen_read_pending = False   # 「读取当前笔位」等待状态回报
        self._pen_register_pending = False   # 「以当前笔位校准标记」等待状态回报
        self._origin_job_pending = None  # 「以机械原点为起点」待发送的作业
        # 文字就地编辑会话（Photoshop 文本工具式：光标/框选/直接输入）
        self._text_session: TextEditSession | None = None
        self._session_sel: tuple[int, int] | None = None

        # 启动即为空白文档（不再预置演示图形），纸张尺寸/边距由设置恢复
        self.controller = DocumentController(self._initial_document())
        self.font_manager = FontManager()
        self.page_presets = PagePresetStore(self.settings)
        self.controller.on_object_activated = self._on_object_activated
        self.controller.on_reference_activated = self._on_reference_activated
        self.controller.on_text_click_edit = self._on_text_click_edit
        self.controller.text_frame_resizing = self._on_text_frame_resizing
        self.controller.table_frame_resizing = self._on_table_frame_resizing
        self.controller.frame_resize_finish = self._flush_frame_resize
        # 宽度手柄拖动中的实时重排按帧限流（见 _queue_frame_reflow）
        self._frame_ref = None
        self._frame_timer = None
        self._frame_last_apply = 0.0
        self._frame_last_cost = 0.0   # 上次重排耗时(ms)，自适应限流用

        self.canvas = CanvasView(self.controller)
        self.setCentralWidget(self.canvas)

        # 回车/F2（画布聚焦时）→ 进入选中文字的就地编辑
        from PySide6.QtGui import QShortcut
        self._text_edit_shortcuts = []
        for key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_F2):
            sc = QShortcut(QKeySequence(int(key)), self.canvas)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(self._edit_selected_text_inline)
            self._text_edit_shortcuts.append(sc)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        # 历史面板先于其它右侧面板加入：右侧停靠区按加入顺序竖排，
        # 这样历史落在最上方（右上角一小块），对象/机器控制在其下方。
        self._build_undo_panel()
        self._build_object_panel()
        self._build_font_panel()
        self._build_perturb_panel()
        self._build_machine_panel()
        self._build_reference_panel()
        self._bind_panel_actions()
        self._restore_settings()
        self._restore_machine_profile()
        # AI 排版服务（MCP 桥的连接目标；仅监听本机回环，见 ai/server.py）
        self.ai_server = None
        self._start_ai_server_from_settings()

        self._status_coord = QLabel("—")
        self._status_zoom = QLabel("—")
        # 纸张尺寸常驻状态栏，并且可点击 → 直接打开页面设置
        self._status_page = QLabel("—")
        self._status_page.setToolTip("点击修改纸张大小/方向/页边距")
        self._status_page.setCursor(Qt.PointingHandCursor)
        self._status_page.mousePressEvent = lambda _e: self._page_setup()
        self.statusBar().addPermanentWidget(QLabel("纸张:"))
        self.statusBar().addPermanentWidget(self._status_page)
        self.statusBar().addPermanentWidget(QLabel("坐标(mm):"))
        self.statusBar().addPermanentWidget(self._status_coord)
        self.statusBar().addPermanentWidget(QLabel("缩放:"))
        self.statusBar().addPermanentWidget(self._status_zoom)
        self._update_page_status()

        self.canvas.mouseMoved.connect(self._on_mouse_moved)
        self.canvas.zoomChanged.connect(self._on_zoom_changed)
        self.canvas.penMarkerMoved.connect(self._on_pen_marker_moved)
        # 画布交互期间让字体后台解析暂停（GIL 争抢会让拖动/缩放掉帧）
        self.canvas.interactionStarted.connect(self._suspend_font_loading)
        self.controller.documentChanged.connect(self._refresh_object_panel)
        self.controller.documentChanged.connect(self._on_document_changed)
        self.controller.documentChanged.connect(self._on_doc_geometry_changed)
        self.controller.documentChanged.connect(self._update_page_status)
        self.controller.documentReplaced.connect(self._refresh_object_panel)
        self.controller.documentReplaced.connect(self._update_page_status)
        self.controller.selectionChanged.connect(self._refresh_selection)
        self.controller.selectionChanged.connect(self._sync_global_perturb_to_selection)
        self.controller.selectionChanged.connect(self._sync_text_props)
        self.controller.undo_stack.cleanChanged.connect(self._on_clean_changed)

        self._refresh_object_panel()
        self.canvas.fit_page()
        self._on_zoom_changed(self.canvas.current_zoom())
        self.controller.undo_stack.clear()
        self._loaded_ready = True
        self._mark_clean()

    # --------------------------------------------------------------- actions
    def _build_actions(self) -> None:
        st = self.controller.undo_stack

        # -- 文件 --
        self.act_new = QAction("新建", self)
        self.act_new.setShortcut(QKeySequence.New)
        self.act_new.triggered.connect(self._new_project)

        self.act_open = QAction("打开…", self)
        self.act_open.setShortcut(QKeySequence.Open)
        self.act_open.triggered.connect(self._open_project)

        self.act_save = QAction("保存", self)
        self.act_save.setShortcut(QKeySequence.Save)
        self.act_save.triggered.connect(self._save_project)

        self.act_save_as = QAction("另存为…", self)
        self.act_save_as.setShortcut(QKeySequence.SaveAs)
        self.act_save_as.triggered.connect(self._save_project_as)

        self.act_page_setup = QAction("页面设置…", self)
        self.act_page_setup.setShortcut("Ctrl+Shift+N")
        self.act_page_setup.setToolTip("设置纸张大小、方向、页边距（快捷键 Ctrl+Shift+N）")
        self.act_page_setup.triggered.connect(self._page_setup)

        self.act_toggle_guide = QAction("显示页边距参考线", self)
        self.act_toggle_guide.setCheckable(True)
        self.act_toggle_guide.setChecked(True)
        self.act_toggle_guide.triggered.connect(self._toggle_page_guide)

        # -- 旋转纸张（连同内容一起转，避免内容转出纸外） --
        self.act_rotate_cw = QAction("顺时针旋转 90°", self)
        self.act_rotate_cw.setShortcut("Ctrl+R")
        self.act_rotate_cw.setToolTip("把纸张连同其上内容一起顺时针旋转 90°")
        self.act_rotate_cw.triggered.connect(lambda: self._rotate_page(ROTATE_CW))

        self.act_rotate_ccw = QAction("逆时针旋转 90°", self)
        self.act_rotate_ccw.setShortcut("Ctrl+Shift+R")
        self.act_rotate_ccw.setToolTip("把纸张连同其上内容一起逆时针旋转 90°")
        self.act_rotate_ccw.triggered.connect(lambda: self._rotate_page(ROTATE_CCW))

        self.act_rotate_180 = QAction("旋转 180°", self)
        self.act_rotate_180.setToolTip("把纸张连同其上内容一起旋转 180°")
        self.act_rotate_180.triggered.connect(lambda: self._rotate_page(ROTATE_180))

        self.act_undo = st.createUndoAction(self, "撤销")
        self.act_undo.setShortcut(QKeySequence.Undo)
        self.act_redo = st.createRedoAction(self, "重做")
        self.act_redo.setShortcut(QKeySequence.Redo)

        self.act_delete = QAction("删除", self)
        self.act_delete.setShortcut(QKeySequence.Delete)
        self.act_delete.setToolTip("删除选中对象；笔画编辑模式下删除选中的笔画")
        self.act_delete.triggered.connect(self._delete_selected)

        self.act_duplicate = QAction("复制对象", self)
        self.act_duplicate.setShortcut("Ctrl+D")
        self.act_duplicate.triggered.connect(self._duplicate_selected)

        self.act_stroke_edit = QAction("笔画编辑", self)
        self.act_stroke_edit.setCheckable(True)
        self.act_stroke_edit.setShortcut("E")
        self.act_stroke_edit.setToolTip(
            "进入笔画编辑：鼠标下的笔画自动高亮，点击确认选中；可移动/缩放/"
            "旋转/弯折，Delete 删除选中笔画（E 或 Esc 退出）")
        self.act_stroke_edit.toggled.connect(self._toggle_stroke_edit)

        self.act_select_all = QAction("全选", self)
        self.act_select_all.setShortcut(QKeySequence.SelectAll)
        self.act_select_all.triggered.connect(self._select_all)

        self.act_raise = QAction("上移图层", self)
        self.act_raise.setShortcut("Ctrl+]")
        self.act_raise.triggered.connect(lambda: self._move_z(1))

        self.act_lower = QAction("下移图层", self)
        self.act_lower.setShortcut("Ctrl+[")
        self.act_lower.triggered.connect(lambda: self._move_z(-1))

        self.act_toggle_visible = QAction("显示/隐藏", self)
        self.act_toggle_visible.setShortcut("Ctrl+H")
        self.act_toggle_visible.triggered.connect(self._toggle_visible_selected)

        self.act_toggle_lock = QAction("锁定/解锁", self)
        self.act_toggle_lock.setShortcut("Ctrl+L")
        self.act_toggle_lock.triggered.connect(self._toggle_lock_selected)

        # 对齐 / 分布
        self.act_align_left = self._mk_align("左对齐", "left")
        self.act_align_hcenter = self._mk_align("水平居中", "hcenter")
        self.act_align_right = self._mk_align("右对齐", "right")
        self.act_align_top = self._mk_align("顶对齐", "top")
        self.act_align_vcenter = self._mk_align("垂直居中", "vcenter")
        self.act_align_bottom = self._mk_align("底对齐", "bottom")
        self.act_distribute_h = QAction("水平等距分布", self)
        self.act_distribute_h.triggered.connect(lambda: self._distribute("h"))
        self.act_distribute_v = QAction("垂直等距分布", self)
        self.act_distribute_v.triggered.connect(lambda: self._distribute("v"))

        self.act_zoom_in = QAction("放大", self)
        self.act_zoom_in.setShortcut(QKeySequence.ZoomIn)
        self.act_zoom_in.triggered.connect(self.canvas.zoom_in)

        self.act_zoom_out = QAction("缩小", self)
        self.act_zoom_out.setShortcut(QKeySequence.ZoomOut)
        self.act_zoom_out.triggered.connect(self.canvas.zoom_out)

        self.act_fit = QAction("适应页面", self)
        self.act_fit.setShortcut("Ctrl+0")
        self.act_fit.triggered.connect(self.canvas.fit_page)

        self.act_zoom_100 = QAction("实际尺寸", self)
        self.act_zoom_100.setShortcut("Ctrl+1")
        self.act_zoom_100.triggered.connect(self.canvas.zoom_100)

        self.act_zoom_selection = QAction("缩放到选中", self)
        self.act_zoom_selection.setShortcut("Ctrl+Shift+F")
        self.act_zoom_selection.triggered.connect(self._zoom_selection)

        self.act_about = QAction("关于", self)
        self.act_about.triggered.connect(self._about)

        self.act_quit = QAction("退出", self)
        self.act_quit.setShortcut(QKeySequence.Quit)
        self.act_quit.triggered.connect(self.close)

        # -- 文本 / 字体 --
        self.act_new_text = QAction("插入文本…", self)
        self.act_new_text.setShortcut("Ctrl+T")
        self.act_new_text.triggered.connect(self._new_text)

        self.act_edit_text = QAction("编辑文本…", self)
        self.act_edit_text.setShortcut("Ctrl+Shift+T")
        self.act_edit_text.triggered.connect(self._edit_text)

        self.act_import_font = QAction("导入字体…", self)
        self.act_import_font.triggered.connect(self._import_font)

        self.act_show_fonts = QAction("显示字体库", self)
        self.act_show_fonts.setCheckable(True)
        self.act_show_fonts.setChecked(True)
        self.act_show_fonts.triggered.connect(self._toggle_font_panel)

        # -- 手写扰动（文本与矢量图形通用） --
        self.act_perturb_selected = QAction("手写扰动…", self)
        self.act_perturb_selected.setShortcut("Ctrl+P")
        self.act_perturb_selected.setToolTip("调整选中对象的手写扰动（文本逐字扰动，矢量线条手抖）")
        self.act_perturb_selected.triggered.connect(self._perturb_selected)

        self.act_reseed_selected = QAction("重新随机（换种子）", self)
        self.act_reseed_selected.setShortcut("Ctrl+Shift+P")
        self.act_reseed_selected.triggered.connect(self._reseed_selected)

        self.act_clear_perturb = QAction("清除扰动", self)
        self.act_clear_perturb.triggered.connect(self._clear_perturb)

        self.act_show_perturb_panel = QAction("显示扰动面板", self)
        self.act_show_perturb_panel.setCheckable(True)
        self.act_show_perturb_panel.setChecked(True)
        self.act_show_perturb_panel.triggered.connect(self._toggle_perturb_panel)

        # -- 富内容 --
        self.act_insert_md = QAction("插入 Markdown / 表格…", self)
        self.act_insert_md.setShortcut("Ctrl+M")
        self.act_insert_md.triggered.connect(lambda: self._insert_content(KIND_MARKDOWN))

        self.act_insert_eq = QAction("插入公式…", self)
        self.act_insert_eq.setShortcut("Ctrl+E")
        self.act_insert_eq.triggered.connect(lambda: self._insert_content(KIND_EQUATION))

        self.act_insert_tex = QAction("插入 LaTeX 文档…", self)
        self.act_insert_tex.triggered.connect(lambda: self._insert_content(KIND_LATEX))

        self.act_insert_tikz = QAction("插入 TikZ 图形…", self)
        self.act_insert_tikz.setToolTip("用 TikZ 画几何图形/流程图/坐标图（需 TeX 工具链）")
        self.act_insert_tikz.triggered.connect(lambda: self._insert_content(KIND_TIKZ))

        self.act_import_svg = QAction("导入 SVG 矢量图…", self)
        self.act_import_svg.setShortcut("Ctrl+Shift+I")
        self.act_import_svg.triggered.connect(lambda: self._insert_content(KIND_SVG))

        # -- 参考层（只对齐，不书写） --
        self.act_add_ref_image = QAction("添加参考图片…", self)
        self.act_add_ref_image.triggered.connect(self._add_reference_image)

        self.act_add_ref_svg = QAction("添加参考 SVG…", self)
        self.act_add_ref_svg.triggered.connect(self._add_reference_svg)

        self.act_show_reference = QAction("显示参考层面板", self)
        self.act_show_reference.setCheckable(True)
        self.act_show_reference.setChecked(True)
        self.act_show_reference.triggered.connect(
            lambda c: self.reference_dock.setVisible(c))
        # -- 机器 --
        self.act_gen_gcode = QAction("生成 G-code 预览", self)
        self.act_gen_gcode.setShortcut("Ctrl+G")
        self.act_gen_gcode.triggered.connect(self._generate_preview)

        self.act_export_gcode = QAction("导出 G-code…", self)
        self.act_export_gcode.triggered.connect(self._export_gcode)

        self.act_send_job = QAction("发送到机器", self)
        self.act_send_job.triggered.connect(self._send_job)

        self.act_show_machine = QAction("显示机器面板", self)
        self.act_show_machine.setCheckable(True)
        self.act_show_machine.setChecked(True)
        self.act_show_machine.triggered.connect(self._toggle_machine_panel)

        self.act_show_undo = QAction("显示历史面板", self)
        self.act_show_undo.setCheckable(True)
        self.act_show_undo.setChecked(True)
        self.act_show_undo.triggered.connect(
            lambda c: self.undo_panel.setVisible(c))

        self.act_show_objects = QAction("显示对象面板", self)
        self.act_show_objects.setCheckable(True)
        self.act_show_objects.setChecked(True)
        self.act_show_objects.triggered.connect(
            lambda c: self.dock.setVisible(c))

        self.act_ai_settings = QAction("AI 服务设置…", self)
        self.act_ai_settings.triggered.connect(self._show_ai_settings)

        self._apply_action_icons()

    # ------------------------------------------------------------- AI 服务
    def _start_ai_server_from_settings(self) -> None:
        if self.settings.ai_service_enabled():
            self.start_ai_server(self.settings.ai_service_port())

    def start_ai_server(self, port: int) -> bool:
        """在指定端口启动 AI 排版服务（先停掉旧实例）。失败返回 False。"""
        from ..ai.server import AiTcpServer
        from ..ai.tools import AiTools
        self.stop_ai_server()
        server = AiTcpServer(AiTools(self), port, parent=self)
        if not server.start():
            # 端口被占（常见：已开了一个 WriterStudio）不致命，仅提示
            self.statusBar().showMessage(
                f"AI 排版服务启动失败：{server.error}（端口 {port}）", 8000)
            server.deleteLater()
            return False
        self.ai_server = server
        self.statusBar().showMessage(
            f"AI 排版服务已启动：127.0.0.1:{server.port}", 5000)
        return True

    def stop_ai_server(self) -> None:
        if getattr(self, "ai_server", None) is not None:
            self.ai_server.stop()
            self.ai_server.deleteLater()
            self.ai_server = None

    def restart_ai_server(self) -> None:
        self.stop_ai_server()
        if self.settings.ai_service_enabled():
            self.start_ai_server(self.settings.ai_service_port())

    def _show_ai_settings(self) -> None:
        from ..ai.dialog import AiServiceDialog
        AiServiceDialog(self).exec()

    def _apply_action_icons(self) -> None:
        """给工具栏/菜单动作配图标：统一 fa6 单一家族（见 :mod:`.icons`）。

        图标库缺失或名字无效时 setIcon 被跳过，动作保持纯文字，不影响功能。
        """
        from .icons import action_icon
        pairs = (
            (self.act_undo, "undo"), (self.act_redo, "redo"),
            (self.act_new, "new"), (self.act_open, "open"),
            (self.act_save, "save"), (self.act_new_text, "new_text"),
            (self.act_edit_text, "edit_text"), (self.act_insert_md, "insert_md"),
            (self.act_insert_eq, "insert_eq"),
            (self.act_add_ref_image, "add_ref_image"),
            (self.act_duplicate, "duplicate"), (self.act_delete, "delete"),
            (self.act_stroke_edit, "stroke_edit"),
            (self.act_raise, "raise"), (self.act_lower, "lower"),
            (self.act_page_setup, "page_setup"),
            (self.act_rotate_cw, "rotate_cw"),
            (self.act_rotate_ccw, "rotate_ccw"),
            (self.act_zoom_out, "zoom_out"), (self.act_zoom_in, "zoom_in"),
            (self.act_fit, "fit"), (self.act_zoom_100, "zoom_100"),
        )
        for act, key in pairs:
            ic = action_icon(key)
            if ic is not None:
                act.setIcon(ic)

    def _build_menus(self) -> None:
        mb = self.menuBar()

        m_file = mb.addMenu("文件(&F)")
        m_file.addAction(self.act_new)
        m_file.addAction(self.act_open)
        m_file.addAction(self.act_save)
        m_file.addAction(self.act_save_as)
        self.recent_menu = QMenu("最近打开", self)
        m_file.addMenu(self.recent_menu)
        m_file.addSeparator()
        m_file.addAction(self.act_page_setup)
        m_file.addAction(self.act_toggle_guide)
        m_rotate = m_file.addMenu("旋转纸张")
        m_rotate.addAction(self.act_rotate_cw)
        m_rotate.addAction(self.act_rotate_ccw)
        m_rotate.addAction(self.act_rotate_180)
        m_file.addSeparator()
        m_file.addAction(self.act_export_gcode)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)

        m_edit = mb.addMenu("编辑(&E)")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)
        m_edit.addSeparator()
        m_edit.addAction(self.act_duplicate)
        m_edit.addAction(self.act_delete)
        m_edit.addSeparator()
        m_edit.addAction(self.act_select_all)

        m_obj = mb.addMenu("对象(&O)")
        m_obj.addAction(self.act_raise)
        m_obj.addAction(self.act_lower)
        m_obj.addSeparator()
        m_obj.addAction(self.act_toggle_visible)
        m_obj.addAction(self.act_toggle_lock)
        m_obj.addSeparator()
        m_align = m_obj.addMenu("对齐")
        for a in (self.act_align_left, self.act_align_hcenter, self.act_align_right,
                  self.act_align_top, self.act_align_vcenter, self.act_align_bottom):
            m_align.addAction(a)
        m_obj.addAction(self.act_distribute_h)
        m_obj.addAction(self.act_distribute_v)

        m_view = mb.addMenu("视图(&V)")
        m_view.addAction(self.act_zoom_in)
        m_view.addAction(self.act_zoom_out)
        m_view.addAction(self.act_fit)
        m_view.addAction(self.act_zoom_100)
        m_view.addAction(self.act_zoom_selection)
        m_view.addSeparator()
        m_view.addAction(self.act_show_fonts)
        m_view.addAction(self.act_show_perturb_panel)
        m_view.addAction(self.act_show_objects)
        m_view.addAction(self.act_show_machine)
        m_view.addAction(self.act_show_reference)
        m_view.addAction(self.act_show_undo)
        m_view.addSeparator()
        m_view.addMenu(self._build_theme_menu())

        m_help = mb.addMenu("帮助(&H)")
        m_help.addAction(self.act_about)

        m_text = mb.addMenu("文本(&T)")
        m_text.addAction(self.act_new_text)
        m_text.addAction(self.act_edit_text)
        m_text.addSeparator()
        m_text.addAction(self.act_import_font)
        m_text.addAction(self.act_show_fonts)

        m_content = mb.addMenu("内容(&C)")
        m_content.addAction(self.act_insert_md)
        m_content.addAction(self.act_insert_eq)
        m_content.addAction(self.act_insert_tex)
        m_content.addAction(self.act_insert_tikz)
        m_content.addSeparator()
        m_content.addAction(self.act_import_svg)

        m_ref = mb.addMenu("参考层(&R)")
        m_ref.addAction(self.act_add_ref_image)
        m_ref.addAction(self.act_add_ref_svg)
        m_ref.addSeparator()
        m_ref.addAction(self.act_show_reference)

        m_pen = mb.addMenu("扰动(&P)")
        m_pen.addAction(self.act_perturb_selected)
        m_pen.addAction(self.act_reseed_selected)
        m_pen.addSeparator()
        m_pen.addAction(self.act_clear_perturb)
        m_pen.addSeparator()
        m_pen.addAction(self.act_show_perturb_panel)

        m_machine = mb.addMenu("机器(&M)")
        m_machine.addAction(self.act_gen_gcode)
        m_machine.addAction(self.act_export_gcode)
        m_machine.addSeparator()
        m_machine.addAction(self.act_send_job)
        m_machine.addSeparator()
        m_machine.addAction(self.act_show_machine)

        m_ai = mb.addMenu("AI 排版(&A)")
        m_ai.addAction(self.act_ai_settings)

    def _build_theme_menu(self) -> QMenu:
        """「主题」子菜单：深色/浅色互斥切换（qdarktheme 热切换）。"""
        from .theme import theme_label
        menu = QMenu("主题", self)
        self._theme_group = QActionGroup(self)
        cur = self.settings.ui_theme()
        for key in THEMES:
            act = QAction(theme_label(key), self)
            act.setCheckable(True)
            act.setChecked(key == cur)
            act.setData(key)
            act.triggered.connect(
                lambda checked=False, k=key: self._apply_theme_setting(k))
            self._theme_group.addAction(act)
            menu.addAction(act)
        if not theme_available():
            for act in menu.actions():
                act.setEnabled(False)
            menu.setToolTip("主题库未安装，主题不可用")
        return menu

    def _apply_theme_setting(self, key: str) -> None:
        """切换主题：重设全局样式表并持久化（视图菜单 → 主题）。"""
        if not apply_theme(QApplication.instance(), key):
            self.statusBar().showMessage("主题切换失败（主题库不可用）", 5000)
            return
        self.settings.set_ui_theme(key)
        self.settings.sync()
        self._restyle_stroke_bar()

    def _restyle_stroke_bar(self) -> None:
        """笔画编辑浮条跟随主题配色（深/浅色下都不突兀，可热切换）。"""
        if not hasattr(self, "_stroke_bar"):
            return
        pal = self.palette()

        def rgba(c, alpha=235):
            return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"

        bg = rgba(pal.color(QPalette.ColorRole.Window))
        border = pal.color(QPalette.ColorRole.Mid).name()
        text = pal.color(QPalette.ColorRole.WindowText).name()
        hl = pal.color(QPalette.ColorRole.Highlight).name()
        hl_text = pal.color(QPalette.ColorRole.HighlightedText).name()
        self._stroke_bar.setStyleSheet(
            f"QWidget {{ background: {bg};"
            f" border: 1px solid {border}; border-radius: 6px; }}"
            f"QToolButton {{ border: none; padding: 3px 8px;"
            f" color: {text}; border-radius: 4px; }}"
            f"QToolButton:checked {{ background: {hl}; color: {hl_text}; }}")
    def _build_toolbar(self) -> None:
        tb = QToolBar("主工具栏", self)
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)   # 图标化（文字进工具提示）
        tb.setToolTip("主工具栏")
        self.addToolBar(tb)
        tb.addAction(self.act_undo)
        tb.addAction(self.act_redo)
        tb.addSeparator()
        tb.addAction(self.act_new)
        tb.addAction(self.act_open)
        tb.addAction(self.act_save)
        tb.addSeparator()
        tb.addAction(self.act_new_text)
        tb.addAction(self.act_edit_text)
        tb.addAction(self.act_insert_md)
        tb.addAction(self.act_insert_eq)
        tb.addSeparator()
        tb.addAction(self.act_add_ref_image)
        tb.addSeparator()
        tb.addAction(self.act_duplicate)
        tb.addAction(self.act_delete)
        tb.addAction(self.act_stroke_edit)
        tb.addSeparator()
        tb.addAction(self.act_raise)
        tb.addAction(self.act_lower)
        tb.addSeparator()
        tb.addAction(self.act_page_setup)
        tb.addAction(self.act_rotate_cw)
        tb.addAction(self.act_rotate_ccw)
        tb.addSeparator()
        tb.addAction(self.act_zoom_out)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit)
        tb.addAction(self.act_zoom_100)

    def _initial_document(self) -> Document:
        """新建空白文档，纸张尺寸/边距/预设沿用上次保存的设置。"""
        w, h = self.settings.page_size((297.0, 210.0))
        return Document(page=PageSpec(
            width=w, height=h,
            margin=self.settings.page_margin(10.0),
            preset_name=self.settings.page_preset_name("")))

    def _build_object_panel(self) -> None:
        self.dock = QDockWidget("对象", self)
        self.dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.obj_list = QListWidget()
        self.obj_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.obj_list.customContextMenuRequested.connect(self._object_panel_menu)
        self.obj_list.itemChanged.connect(self._on_object_item_changed)
        self.obj_list.itemSelectionChanged.connect(self._on_panel_selection)
        # 双击列表项 = 双击画布对象：打开对应的编辑界面
        self.obj_list.itemDoubleClicked.connect(self._on_object_list_double_clicked)
        self.dock.setWidget(self.obj_list)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)

    def _on_object_list_double_clicked(self, item) -> None:
        obj = self.controller.doc.find(item.data(Qt.UserRole))
        if obj is not None:
            self._on_object_activated(obj)

    def _object_panel_menu(self, pos) -> None:
        item = self.obj_list.itemAt(pos)
        if item is not None and not item.isSelected():
            self.obj_list.clearSelection()
            item.setSelected(True)
        objs = self._selected()
        if not objs:
            return
        menu = QMenu(self)
        if objs[0].source.kind == "text":
            menu.addAction(self.act_edit_text)
            menu.addSeparator()
        menu.addAction(self.act_toggle_visible)
        menu.addAction(self.act_toggle_lock)
        menu.addSeparator()
        menu.addAction(self.act_raise)
        menu.addAction(self.act_lower)
        menu.addSeparator()
        menu.addAction(self.act_delete)
        menu.exec(self.obj_list.mapToGlobal(pos))

    def _build_font_panel(self) -> None:
        self.font_panel = FontPanel(self.font_manager, self)
        self.font_panel.searchDirAdded.connect(self._on_font_dir_added)
        self.font_panel.pinHideChanged.connect(self._persist_font_pin_hide)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.font_panel)

    def _on_font_dir_added(self, directory: str) -> None:
        self.settings.add_font_search_dir(directory)
        self.settings.sync()

    def _persist_font_pin_hide(self) -> None:
        self.settings.set_font_pinned(self.font_manager.pinned_names())
        self.settings.set_font_hidden(self.font_manager.hidden_names())
        self.settings.sync()

    def _build_perturb_panel(self) -> None:
        self.perturb_dock = QDockWidget("手写扰动", self)
        self.perturb_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        # 顶部：选中单个文本对象时出现的「文字属性」区（即改即得）；
        # 下面是原有的手写扰动参数区
        self.text_props = TextPropsPanel(self.font_manager)
        self.text_props.propsChanged.connect(self._on_text_props_changed)
        self.text_props.setVisible(False)
        self.global_perturb = PerturbPanel(parent=self)
        self.global_perturb.paramsChanged.connect(self._on_global_perturb_changed)
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)
        v.addWidget(self.text_props)
        v.addWidget(self.global_perturb, 1)
        self.perturb_dock.setWidget(wrap)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.perturb_dock)
        self.tabifyDockWidget(self.font_panel, self.perturb_dock)
        self.font_panel.raise_()

    def _toggle_font_panel(self, checked: bool) -> None:
        self.font_panel.setVisible(checked)

    def _toggle_perturb_panel(self, checked: bool) -> None:
        self.perturb_dock.setVisible(checked)

    def _build_machine_panel(self) -> None:
        self.machine_dock = QDockWidget("机器控制", self)
        self.machine_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.machine_panel = MachinePanel(parent=self)
        self.machine_panel.generateRequested.connect(self._generate_preview)
        self.machine_panel.exportRequested.connect(self._export_gcode)
        self.machine_panel.sendRequested.connect(self._send_job)
        self.machine_panel.readPenAsStartRequested.connect(self._on_read_pen_as_start)
        self.machine_panel.registerPenRequested.connect(self._on_register_pen)
        self.machine_panel.markStartPickRequested.connect(self._on_mark_start_toggled)
        self.machine_panel.removeStartMarkerRequested.connect(self._on_remove_start_marker)
        self.machine_panel.movePenRequested.connect(self._on_move_pen_to_marker)
        self.machine_panel.statusUpdated.connect(self._on_machine_status_for_marker)
        self.machine_panel.statusUpdated.connect(self._on_machine_status_for_send)
        self.machine_panel.axisOptionsChanged.connect(self._on_machine_config_changed)
        # 能力探测结果：持久化 + 回填到 link（下次启动未重连前，
        # 耗时估算/进给建议仍可用上一台的档案）
        self.machine_panel.profileDetected.connect(self._on_machine_profile)
        # 起点模式/坐标/轴映射变化会改变「机器→页面」映射，
        # 需重画画布上的起点标记与机械原点
        self.machine_panel.startPointChanged.connect(self._on_machine_config_changed)
        # 包一层滚动区：机器面板内容多（连接/笔控/起点/映射/质量…），
        # 面板被挤压时仍可完整滚动操作，而不是互相叠压
        from PySide6.QtWidgets import QScrollArea, QFrame
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.machine_panel)
        self.machine_dock.setWidget(scroll)
        self.addDockWidget(Qt.RightDockWidgetArea, self.machine_dock)
        self.tabifyDockWidget(self.dock, self.machine_dock)
        self.dock.raise_()

    def _build_reference_panel(self) -> None:
        # ReferencePanel 本身就是 QDockWidget，直接作为停靠面板加入：
        # 若再套一层 QDockWidget 会出现两层标题栏（「参考层」重复）。
        self.reference_panel = ReferencePanel(self.controller, self)
        self.reference_dock = self.reference_panel
        self.reference_panel.addImageRequested.connect(self._add_reference_image)
        self.reference_panel.addSvgRequested.connect(self._add_reference_svg)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.reference_dock)
        self.tabifyDockWidget(self.font_panel, self.reference_dock)
        self.controller.documentChanged.connect(self.reference_panel.refresh_if_changed)
        self.controller.documentReplaced.connect(self.reference_panel.refresh)

    def _build_undo_panel(self) -> None:
        self.undo_panel = UndoPanel(self.controller, self)
        # 历史面板是最先加入右侧停靠区的面板 → 停在右上角，只占一小块；
        # 对象/机器控制随后加入、tabify 成一个标签组停在其下方。
        self.addDockWidget(Qt.RightDockWidgetArea, self.undo_panel)
        self.undo_panel.setMinimumHeight(110)
        self.undo_panel.setMaximumHeight(260)
        self.resizeDocks([self.undo_panel], [190], Qt.Vertical)

    def _bind_panel_actions(self) -> None:
        """让「视图」菜单的勾选状态与面板实际显隐**双向同步**。

        之前勾选框只在菜单里被点击时才更新：用户用面板标题栏的关闭按钮、
        或切到同一标签组的另一个面板后，菜单勾选会和实际状态脱节。

        判定「显示中」用 ``isHidden()`` 而非 ``isVisible()``：tabify 的 dock
        在非当前标签时 ``isVisible()`` 为 False，但那并不代表用户关掉了它。
        """
        pairs = [
            (self.font_panel, self.act_show_fonts),
            (self.perturb_dock, self.act_show_perturb_panel),
            (self.machine_dock, self.act_show_machine),
            (self.reference_dock, self.act_show_reference),
            (self.undo_panel, self.act_show_undo),
            (self.dock, self.act_show_objects),
        ]
        for panel, action in pairs:
            if panel is None or action is None:
                continue
            action.blockSignals(True)
            action.setChecked(not panel.isHidden())
            action.blockSignals(False)
            panel.visibilityChanged.connect(
                lambda _visible, a=action, p=panel: self._sync_panel_action(a, p))

    @staticmethod
    def _sync_panel_action(action, panel) -> None:
        action.blockSignals(True)
        action.setChecked(not panel.isHidden())
        action.blockSignals(False)

    def _toggle_machine_panel(self, checked: bool) -> None:
        self.machine_dock.setVisible(checked)

    def _on_global_perturb_changed(self) -> None:
        """全局面板改动时，实时应用到选中的对象（可撤销，连续拖动合并为一步）。"""
        objs = self._perturbable_objects()
        if not objs:
            self.statusBar().showMessage(
                "请先选择一个对象，再调整手写扰动参数。", 4000)
            return
        params = self.global_perturb.params()
        # 参数与对象当前值完全一致时跳过（防抖后的重复触发、程序化回填）
        if all(resolve_perturb(o) == params for o in objs):
            return
        entries = []
        for o in objs:
            old = snapshot_object(o)
            apply_perturb(o, params, self.font_manager)
            entries.append((o, old, snapshot_object(o)))
        key = "global-perturb:" + ",".join(sorted(o.id for o in objs))
        self.controller.replace_objects(entries, "调整手写扰动", merge_key=key)
        self.canvas.sync_scene()

    def _sync_global_perturb_to_selection(self) -> None:
        """选择变化时，把选中对象的扰动参数回填到全局面板。"""
        objs = self._perturbable_objects()
        if objs:
            self._load_global_perturb_from(objs[0])

    # ----------------------------------------------------------------- slots
    def _on_mouse_moved(self, p) -> None:
        self._status_coord.setText(f"{p.x():.1f}, {p.y():.1f}")

    def _suspend_font_loading(self) -> None:
        # 面板可能在关闭流程中已销毁而画布信号还连着（关窗时的事件排队）
        panel = getattr(self, "font_panel", None)
        if panel is not None:
            panel.suspend_loading(800)

    def _on_document_changed(self) -> None:
        # 笔画编辑所附着的对象被删除/撤销掉了 → 立即退出该模式，
        # 避免继续在游离对象上编辑（改了也不在文档里）
        se = getattr(self, "stroke_editor", None)
        if se is not None and se.active and se.obj is not None:
            if not any(o is se.obj for o in self.controller.doc.objects):
                self._exit_stroke_edit()
        # 文档内容变化即视为有未保存修改（撤销栈也会触发 cleanChanged）
        if self._loaded_ready:
            self._mark_dirty()

    def _update_page_status(self) -> None:
        page = self.controller.doc.page
        orient = "横向" if page.width > page.height else "纵向"
        name = f"{page.preset_name} " if page.preset_name else ""
        self._status_page.setText(
            f"{name}{page.width:.0f}×{page.height:.0f} mm（{orient}）")

    def _on_doc_geometry_changed(self) -> None:
        # 撤销/重做「页面设置」等会改变场景范围，需同步
        self.canvas._update_scene_rect()
        # 机械原点标注钉在纸张左上角，页面尺寸变化（含撤销旋转）后按
        # 新页面重算；构造期 machine_panel 尚未创建，恢复设置时统一初始化
        if getattr(self, "machine_panel", None) is not None:
            self._update_canvas_origin_marker()

    def _on_clean_changed(self, clean: bool) -> None:
        if clean:
            self._mark_clean()
        elif self._loaded_ready:
            self._dirty = True
            self._update_title()

    def _on_zoom_changed(self, z: float) -> None:
        self._status_zoom.setText(f"{z * 100:.0f}%")
        # 画布原生文字编辑：光标几何是场景坐标换算的，缩放后刷新一次
        if getattr(self, "_text_session", None) is not None:
            self._text_session._last_view_rect = None
            self.canvas.viewport().update()

    def _refresh_object_panel(self) -> None:
        self.obj_list.blockSignals(True)
        self.obj_list.clear()
        for obj in self.controller.doc.objects:
            item = QListWidgetItem(self._object_item_label(obj))
            item.setData(Qt.UserRole, obj.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if obj.visible else Qt.Unchecked)
            self.obj_list.addItem(item)
        self.obj_list.blockSignals(False)
        self._refresh_selection()

    @staticmethod
    def _object_item_label(obj) -> str:
        flags = []
        if not obj.visible:
            flags.append("隐藏")
        if obj.locked:
            flags.append("锁定")
        suffix = ("  [" + "/".join(flags) + "]") if flags else ""
        return f"{obj.name}  ({len(obj.local_strokes)} 笔){suffix}"

    def _on_object_item_changed(self, item) -> None:
        obj = self.controller.doc.find(item.data(Qt.UserRole))
        if obj is None:
            return
        want = item.checkState() == Qt.Checked
        if want != obj.visible:
            self.controller.set_object_flags(
                obj, "显示对象" if want else "隐藏对象", {"visible": want})
            self.canvas.sync_scene()

    def _refresh_selection(self) -> None:
        selected = {o.id for o in self.canvas.selected_objects()}
        self.obj_list.blockSignals(True)
        for i in range(self.obj_list.count()):
            it = self.obj_list.item(i)
            it.setSelected(it.data(Qt.UserRole) in selected)
        self.obj_list.blockSignals(False)

    def _on_panel_selection(self) -> None:
        ids = [it.data(Qt.UserRole) for it in self.obj_list.selectedItems()]
        scene = self.canvas.scene()
        scene.blockSignals(True)
        scene.clearSelection()
        for oid in ids:
            item = self.canvas._items.get(oid)
            if item is not None:
                item.setSelected(True)
        scene.blockSignals(False)
        self.controller.notify_selection()

    # ---------------------------------------------------------------- commands
    def _selected(self):
        return self.canvas.selected_objects()

    def _delete_selected(self) -> None:
        # 笔画编辑模式下 Delete 只删选中的笔画，不删整个对象
        # （想删整个对象先 Esc 退出笔画编辑）
        se = getattr(self, "stroke_editor", None)
        if se is not None and se.active:
            if not se.delete_selected():
                self.statusBar().showMessage("请先点选笔画再删除。", 3000)
            return
        # 文字编辑会话里 Delete 属于编辑按键（画布已拦截快捷键），此分支
        # 只是兜底，防止别处触发误删正在编辑的对象
        if getattr(self, "_text_session", None) is not None:
            return
        objs = self._selected()
        if objs:
            self.controller.remove_objects(objs)
        refs = self.canvas.selected_references()
        if refs:
            self.controller.remove_references(refs)

    def _duplicate_selected(self) -> None:
        from ..core.geometry import AffineTransform
        objs = self._selected()
        for o in objs:
            clone = o.clone()
            clone.transform = AffineTransform.translate(5.0, -5.0) @ o.transform
            self.controller.add_object(clone)
        for r in self.canvas.selected_references():
            clone = r.clone()
            clone.transform = AffineTransform.translate(5.0, -5.0) @ r.transform
            self.controller.add_reference(clone)
        if objs:
            self.canvas.sync_scene()

    def _select_all(self) -> None:
        for item in self.canvas._items.values():
            item.setSelected(True)
        for item in self.canvas._ref_items.values():
            item.setSelected(True)

    def _move_z(self, delta: int) -> None:
        for o in self._selected():
            self.controller.move_z(o, delta)
        self.canvas.sync_scene()

    # ------------------------------------------------- 显示/锁定 & 对齐分布
    def _mk_align(self, label: str, mode: str) -> QAction:
        act = QAction(label, self)
        act.triggered.connect(lambda: self._align(mode))
        return act

    def _toggle_visible_selected(self) -> None:
        objs = self._selected()
        if not objs:
            return
        # 以第一个对象的目标状态为准应用到全部
        target = not objs[0].visible
        self.controller.undo_stack.beginMacro(
            "显示对象" if target else "隐藏对象")
        for o in objs:
            self.controller.set_object_flags(
                o, "显示对象" if target else "隐藏对象", {"visible": target})
        self.controller.undo_stack.endMacro()
        self.canvas.sync_scene()

    def _toggle_lock_selected(self) -> None:
        objs = self._selected()
        if not objs:
            return
        target = not objs[0].locked
        self.controller.undo_stack.beginMacro(
            "锁定对象" if target else "解锁对象")
        for o in objs:
            self.controller.set_object_flags(
                o, "锁定对象" if target else "解锁对象", {"locked": target})
        self.controller.undo_stack.endMacro()
        self.canvas.sync_scene()

    def _targets(self):
        """对齐/分布的作用对象：书写对象 + 参考图（要求至少两个才对齐）。"""
        objs = self.canvas.selected_models()
        if len(objs) >= 2:
            return objs
        return self._selected() or objs

    def _align(self, mode: str) -> None:
        targets = [m for m in self._targets() if m.visible]
        if len(targets) < 2:
            self.statusBar().showMessage("请先选择至少两个对象再对齐。", 4000)
            return
        boxes = {id(m): m.world_bbox() for m in targets}
        boxes = {k: b for k, b in boxes.items() if not b.is_empty}
        if len(boxes) < 2:
            return
        if mode in ("left", "hcenter", "right"):
            if mode == "left":
                ref = min(b.x0 for b in boxes.values())
            elif mode == "right":
                ref = max(b.x1 for b in boxes.values())
            else:
                ref = (min(b.x0 for b in boxes.values())
                       + max(b.x1 for b in boxes.values())) / 2.0
        else:
            if mode == "bottom":
                ref = min(b.y0 for b in boxes.values())
            elif mode == "top":
                ref = max(b.y1 for b in boxes.values())
            else:
                ref = (min(b.y0 for b in boxes.values())
                       + max(b.y1 for b in boxes.values())) / 2.0
        from ..core.geometry import AffineTransform
        self.controller.undo_stack.beginMacro(f"对齐（{mode}）")
        for m in targets:
            b = m.world_bbox()
            if b.is_empty:
                continue
            if mode == "left":
                dx, dy = ref - b.x0, 0.0
            elif mode == "right":
                dx, dy = ref - b.x1, 0.0
            elif mode == "hcenter":
                dx, dy = ref - b.center[0], 0.0
            elif mode == "bottom":
                dx, dy = 0.0, ref - b.y0
            elif mode == "top":
                dx, dy = 0.0, ref - b.y1
            else:  # vcenter
                dx, dy = 0.0, ref - b.center[1]
            if dx == 0.0 and dy == 0.0:
                continue
            self.controller.set_model_transform(
                m, AffineTransform.translate(dx, dy) @ m.transform, "对齐")
        self.controller.undo_stack.endMacro()
        self.canvas.sync_scene()

    def _distribute(self, axis: str) -> None:
        targets = [m for m in self._targets() if m.visible]
        if len(targets) < 3:
            self.statusBar().showMessage("请先选择至少三个对象再分布。", 4000)
            return
        from ..core.geometry import AffineTransform
        key = (lambda m: m.world_bbox().center[0]) if axis == "h" \
            else (lambda m: m.world_bbox().center[1])
        ordered = sorted(targets, key=key)
        centers = [key(m) for m in ordered]
        lo, hi = centers[0], centers[-1]
        step = (hi - lo) / (len(ordered) - 1)
        self.controller.undo_stack.beginMacro(
            "水平分布" if axis == "h" else "垂直分布")
        for i, m in enumerate(ordered):
            want = lo + step * i
            cur = key(m)
            d = want - cur
            if abs(d) < 1e-9:
                continue
            delta = (d, 0.0) if axis == "h" else (0.0, d)
            self.controller.set_model_transform(
                m, AffineTransform.translate(*delta) @ m.transform, "分布")
        self.controller.undo_stack.endMacro()
        self.canvas.sync_scene()

    def _zoom_selection(self) -> None:
        if not self.canvas.zoom_to_selection():
            self.statusBar().showMessage("请先选择对象再缩放到选中。", 3000)

    # --------------------------------------------------------- 缺字 / 超界
    def _report_missing(self, obj) -> None:
        """若对象排版时跳过了缺字，在状态栏提示（避免用户以为已完整画出）。"""
        lay = obj.meta.get("layout")
        missing = list(getattr(lay, "missing", []) or [])
        # TikZ 文字重排的缺字记在 meta["missing"]（无 layout 对象）
        missing += [c for c in (obj.meta.get("missing") or [])
                    if c not in missing]
        if not missing:
            return
        shown = "".join(missing[:12])
        more = f" 等 {len(missing)} 个" if len(missing) > 12 else ""
        self.statusBar().showMessage(
            f"缺字未绘制：{shown}{more}（请添加含该字符的字体）", 8000)

    def _check_bounds(self):
        """检查书写对象是否超出页面，返回 (对象列表, 是否越界)。"""
        page = self.controller.doc.page
        pb = page.bbox()
        out = []
        for o in self.controller.doc.objects:
            if not o.visible:
                continue
            b = o.world_bbox()
            if b.is_empty:
                continue
            if (b.x0 < pb.x0 - 1e-6 or b.y0 < pb.y0 - 1e-6
                    or b.x1 > pb.x1 + 1e-6 or b.y1 > pb.y1 + 1e-6):
                out.append(o)
        return out

    def _warn_bounds(self) -> bool:
        """生成/导出前提示越界内容，返回是否应继续。"""
        out = self._check_bounds()
        if not out:
            return True
        names = "、".join(o.name for o in out[:5])
        more = f" 等 {len(out)} 个" if len(out) > 5 else ""
        if not self.isVisible():
            # 无人值守（测试/批处理）时只记状态栏，不弹模态框
            self.statusBar().showMessage(f"内容超出页面：{names}{more}", 8000)
            return True
        r = QMessageBox.warning(
            self, "内容超出页面",
            f"以下内容超出页面范围：\n{names}{more}\n\n仍要继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        return r == QMessageBox.Yes

    # ------------------------------------------------------------ 文本命令
    def _default_font_chain(self) -> list[str]:
        """新建内容的默认字体链：优先上次使用的，其次内置启发式。"""
        saved = [n for n in self.settings.last_font_chain()
                 if self.font_manager.get_entry(n)]
        if saved:
            return saved
        return self._builtin_font_chain()

    def _builtin_font_chain(self) -> list[str]:
        """内置回退链：中文单线字体 + 英文 Hershey。"""
        names: list[str] = []
        for entry in self.font_manager.entries():
            if entry.kind == "stroke-json":
                names.append(entry.name)
                break
        for cand in ("futural", "futuram", "scripts", "cursive"):
            if self.font_manager.get_entry(cand):
                names.append(cand)
                break
        if not names:
            entries = self.font_manager.entries()
            if entries:
                names.append(entries[0].name)
        return names

    def _remember_font_chain(self, names) -> None:
        """记住本次使用的字体链，供下次新建时自动恢复。"""
        names = [n for n in (names or []) if n]
        if names:
            self.settings.set_last_font_chain(names)
            self.settings.sync()

    def _default_text_spec(self) -> TextSpec:
        """为新建文本挑默认回退链：上次使用的字体，或内置启发式。"""
        return TextSpec(text="", font_names=self._default_font_chain(), size=10.0)

    def _new_text(self) -> None:
        spec = self._default_text_spec()
        dlg = TextEditDialog(spec, self.font_manager, self)
        if dlg.exec() != TextEditDialog.Accepted:
            return
        spec = dlg.result_spec()
        if not spec.text.strip():
            return
        self._remember_font_chain(spec.font_names)
        obj = make_text_object(spec, self.font_manager)
        if not obj.local_strokes:
            QMessageBox.warning(self, "无法生成", "所选字体无法绘制该文本（缺字）。")
            return
        self._place_object(obj)
        self.controller.add_object(obj)
        self.canvas.sync_scene()
        self.canvas.select_object(obj)
        self._report_missing(obj)

    def _edit_text(self) -> None:
        """编辑当前选中对象的源内容（文本/公式/Markdown/LaTeX/TikZ/SVG 通用）。

        此前只对纯文本对象生效，选了 Markdown/公式等点「编辑文本」毫无
        反应。现按来源类型分派到对应编辑器（等价于双击对象）；静态图形
        无可编辑源内容时给出说明。
        """
        objs = self._selected()
        if not objs or len(objs) > 1:
            msg = ("请先选择一个对象（文本/公式/Markdown/SVG 等）。"
                   if not objs else "请只选择一个对象再编辑。")
            if self.isVisible():
                QMessageBox.information(self, "编辑内容", msg)
            else:
                self.statusBar().showMessage(msg, 5000)
            return
        self._edit_object(objs[0])

    def _edit_object(self, obj) -> None:
        """按对象来源类型打开对应编辑器（供菜单与双击复用）。"""
        kind = obj.source.kind
        if kind == "text":
            spec = TextSpec.from_data(obj.source.data)
            dlg = TextEditDialog(spec, self.font_manager, self)
            if dlg.exec() != TextEditDialog.Accepted:
                return
            new_spec = dlg.result_spec()
            self._remember_font_chain(new_spec.font_names)
            before = snapshot_object(obj)
            update_text_object(obj, new_spec, self.font_manager)
            self.controller.replace_object(obj, before, snapshot_object(obj),
                                           "编辑文本")
            self.canvas.sync_scene()
            self._report_missing(obj)
        elif kind in (SOURCE_MARKDOWN, SOURCE_EQUATION, SOURCE_SVG,
                      SOURCE_LATEX, SOURCE_TIKZ):
            self._edit_content(obj)
        elif supports_perturb(obj):
            # 静态/矢量图形：直接打开扰动面板（手绘线条效果）
            self._perturb_selected()
        else:
            QMessageBox.information(
                self, "编辑对象",
                f"「{obj.name}」是静态图形，无可编辑的源内容。\n"
                "可拖动/缩放/旋转，或使用扰动菜单。")

    def _edit_content(self, obj) -> None:
        """重新打开富内容对话框编辑（Markdown/公式/SVG）。"""
        kind = obj.source.kind
        ui_kind = {SOURCE_MARKDOWN: KIND_MARKDOWN, SOURCE_EQUATION: KIND_EQUATION,
                   SOURCE_SVG: KIND_SVG, SOURCE_LATEX: KIND_LATEX,
                   SOURCE_TIKZ: KIND_TIKZ}.get(kind, KIND_MARKDOWN)
        dlg = ContentDialog(self.font_manager, initial_kind=ui_kind, parent=self)
        dlg.load_from_object(obj)
        if dlg.exec() != ContentDialog.Accepted:
            return
        payload = dlg.payload()
        if kind == SOURCE_MARKDOWN:
            self._remember_font_chain(payload.data.get("font_names"))
        new_obj = self._build_content_object(payload)
        if new_obj is None or not new_obj.local_strokes:
            return
        before = snapshot_object(obj)
        obj.source = new_obj.source
        obj.local_strokes = new_obj.local_strokes
        obj.name = new_obj.name
        # 继承新源的渲染缓存与缺字提示（否则首次调扰动要重新编译）
        from ..content.builder import transfer_render_cache
        transfer_render_cache(new_obj, obj)
        self.controller.replace_object(obj, before, snapshot_object(obj),
                                       "编辑内容")
        self.canvas.sync_scene()
        self._report_missing(obj)

    def _on_object_activated(self, obj) -> None:
        """双击对象 → 打开属性/内容编辑器（文本=文本编辑对话框）。"""
        if getattr(self, "_text_session", None) is not None:
            self._text_session.finish()      # 编辑中双击：先收尾再开对话框
        self.canvas.select_object(obj)
        self._edit_object(obj)

    def _on_text_click_edit(self, obj) -> None:
        """单击已选中的文本对象（未拖动）→ 进入画布就地编辑。"""
        self._start_text_edit(obj)

    def _edit_selected_text_inline(self) -> None:
        """回车/F2：把恰好选中的一个文本对象进入就地编辑。"""
        if getattr(self, "_text_session", None) is not None:
            return          # 已在编辑中
        objs = [o for o in self._selected() if o.source.kind == "text"]
        if len(objs) == 1:
            self._start_text_edit(objs[0])

    # ------------------------------------------------------- 画布文字编辑
    def _start_text_edit(self, obj, scene_pos=None) -> None:
        """在画布上就地编辑文本对象：真实光标/框选/直接输入（含输入法）。"""
        if obj.source.kind != "text":
            return
        if self._text_session is not None:
            if self._text_session.obj is obj:
                self.canvas.setFocus()
                return
            self._text_session.finish()
        if getattr(self, "stroke_editor", None) is not None \
                and self.stroke_editor.active:
            self._exit_stroke_edit()
        session = TextEditSession(self.canvas, obj, self)
        session.undo_snapshot = snapshot_object(obj)
        session.textChanged.connect(self._on_session_text_changed)
        session.charSelectionChanged.connect(self._on_session_selection_changed)
        session.finished.connect(self._on_session_finished)
        self._text_session = session
        self._session_sel = None
        self.canvas.text_session = session
        for sc in getattr(self, "_text_edit_shortcuts", []):
            sc.setEnabled(False)      # 回车在编辑中用于换行
        self.canvas.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.canvas.setFocus()
        session.start(QPointF(*scene_pos) if scene_pos else None)
        self.statusBar().showMessage(
            "文字编辑：直接输入（支持输入法）；拖动框选；回车换行；"
            "Esc 或点击空白处结束", 8000)
        self._sync_text_props()

    def _on_session_text_changed(self, text: str) -> None:
        """会话文本变化 → 写回对象（连续输入合并为一步撤销）。"""
        session = self._text_session
        if session is None:
            return
        obj = session.obj
        spec = TextSpec.from_data(obj.source.data)
        if spec.text == text:
            return
        spec.text = text
        update_text_object(obj, spec, self.font_manager)
        self.controller.replace_objects(
            [(obj, session.undo_snapshot, snapshot_object(obj))], "编辑文字",
            merge_key=f"inline-text:{obj.id}")
        self.canvas.sync_scene()
        session.sync_after_commit()
        self._sync_text_props()

    def _on_session_selection_changed(self, start: int, end: int) -> None:
        self._session_sel = (start, end) if end > start else None

    def _on_session_finished(self) -> None:
        for sc in getattr(self, "_text_edit_shortcuts", []):
            sc.setEnabled(True)
        self.canvas.text_session = None
        self._text_session = None
        self._session_sel = None
        self.canvas.viewport().update()      # 清除光标/选区覆盖
        self._sync_text_props()

    def _session_selected_chars(self) -> str:
        """画布编辑会话当前选中的字符（不在编辑状态则返回空）。"""
        session = self._text_session
        if session is None or not session.has_selection():
            return ""
        return session.selected_chars()

    #: 宽度手柄拖动中重排的最小间隔(ms)。大表格一次重排可达数十毫秒，
    #: 每个鼠标移动事件都重排会明显卡顿；限流后最多 ~25 次/秒，且松手时
    #: 一定套用最后一次宽度（_flush_frame_resize），不会提交到陈旧宽度。
    _FRAME_REFLOW_MS = 40

    def _on_text_frame_resizing(self, obj, width: float) -> None:
        """文本框宽度手柄拖动中：按新宽度重排（限流，松手统一入撤销栈）。"""
        self._queue_frame_reflow(obj, "text", width)

    def _on_table_frame_resizing(self, obj, width: float) -> None:
        """表格宽度手柄拖动中：改 Markdown 表格宽并重排单元格（限流）。

        与文本框宽度同一手感；宽度写进 ``style.table_width``（0=内容自适应）。
        """
        if obj.source.kind != SOURCE_MARKDOWN:
            return
        self._queue_frame_reflow(obj, "table", width)

    def _queue_frame_reflow(self, obj, kind: str, width: float) -> None:
        """把「按新宽度重排」限流到与重排耗时匹配的节奏。

        鼠标拖动产生的移动事件远多于屏幕刷新帧：每次都同步重排（表格可达
        数十毫秒）会堆积成卡顿。这里只在距上次应用足够久时立即重排，否则
        记下最新值、用单次定时器延后合并——期间多次拖动只重排一次。间隔
        下限 40ms（~25 次/秒）；上次重排更慢时按其 1.25 倍顺延，避免大表
        格连续重排把事件循环打满（松手时 _flush_frame_resize 一定套用最后
        一次宽度，不会提交到陈旧值）。
        """
        self._frame_ref = (obj, kind, float(width))
        now = time.monotonic()
        elapsed = (now - self._frame_last_apply) * 1000.0
        required = max(self._FRAME_REFLOW_MS, self._frame_last_cost * 1.25)
        if elapsed >= required:
            self._apply_frame_reflow()
            return
        if self._frame_timer is None:
            self._frame_timer = QTimer(self)
            self._frame_timer.setSingleShot(True)
            self._frame_timer.timeout.connect(self._apply_frame_reflow)
        self._frame_timer.start(max(1, int(required - elapsed)))

    def _apply_frame_reflow(self) -> None:
        ref = self._frame_ref
        if ref is None:
            return
        obj, kind, width = ref
        t0 = time.monotonic()
        self._frame_last_apply = t0
        if kind == "table":
            self._reflow_table(obj, width)
        else:
            self._reflow_text(obj, width)
        self._frame_last_cost = (time.monotonic() - t0) * 1000.0

    def _flush_frame_resize(self) -> None:
        """松手：立刻套用最后一次宽度，保证撤销快照拿到最终值。

        收尾做一次全场景同步（拖动中为省时只同步了正在改的对象）。
        """
        if self._frame_timer is not None:
            self._frame_timer.stop()
        self._apply_frame_reflow()
        self._frame_ref = None
        self.canvas.sync_scene()

    def _reflow_text(self, obj, width: float) -> None:
        spec = TextSpec.from_data(obj.source.data)
        spec.frame_width = max(0.0, float(width))
        update_text_object(obj, spec, self.font_manager)
        # 拖动中只同步这一个对象：全场景 sync 会把参考图等无关 item 也
        # update() 一遍，触发整视口重绘——每次重排多付一帧大图 blit，
        # 拖宽手柄时表现为可感的卡顿。松手时 flush 里补全量同步。
        self.canvas.sync_object(obj)

    def _reflow_table(self, obj, width: float) -> None:
        if obj.source.kind != SOURCE_MARKDOWN:
            return
        data = obj.source.data
        style = dict(data.get("style") or {})
        style["table_width"] = max(0.0, float(width))
        data["style"] = style
        regenerate_content_object(obj, self.font_manager)
        self.canvas.sync_object(obj)

    # ------------------------------------------------------- 文字属性面板
    def _sync_text_props(self) -> None:
        """选中单个文本对象时显示属性区并回填其参数；否则隐藏。"""
        objs = [o for o in self._selected() if o.source.kind == "text"]
        if len(objs) != 1:
            self.text_props.setVisible(False)
            return
        try:
            spec = TextSpec.from_data(objs[0].source.data)
        except Exception:
            self.text_props.setVisible(False)
            return
        self.text_props.setVisible(True)
        self.text_props.set_from_spec(spec)

    # --------------------------------------------------------- 笔画编辑工具
    def _build_stroke_editor(self) -> None:
        """构造笔画编辑会话与画布浮动操作条（进入模式时显示）。"""
        from .stroke_editor import StrokeEditor
        se = StrokeEditor(self.canvas)
        se.commit_stroke = self._commit_stroke_edit
        se.delete_stroke = self._delete_stroke
        se.commit_strokes = self._commit_strokes_edit
        se.delete_strokes = self._delete_strokes
        se.requestExit.connect(self._exit_stroke_edit)
        self.canvas.stroke_editor = se
        self.stroke_editor = se

        from PySide6.QtWidgets import QToolButton
        bar = QWidget(self.canvas.viewport())
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 4, 6, 4)
        h.setSpacing(4)
        # 浮动条不接收焦点：点按钮后键盘焦点仍留在画布上，Esc 才能退出
        bar.setFocusPolicy(Qt.NoFocus)
        self._bend_btn = QToolButton(bar)
        self._bend_btn.setFocusPolicy(Qt.NoFocus)
        self._bend_btn.setText("弯折")
        self._bend_btn.setCheckable(True)
        self._bend_btn.setToolTip(
            "弯折模式：拖动笔画上的点做局部扭曲（衰减半径≈字号的 1/3）")
        self._bend_btn.toggled.connect(
            lambda on: se.set_bend_mode(on))
        h.addWidget(self._bend_btn)
        dl = QToolButton(bar)
        dl.setFocusPolicy(Qt.NoFocus)
        dl.setText("删除笔画")
        dl.clicked.connect(self._delete_selected_stroke)
        h.addWidget(dl)
        done = QToolButton(bar)
        done.setFocusPolicy(Qt.NoFocus)
        done.setText("完成")
        done.setToolTip("退出笔画编辑（Esc）")
        done.clicked.connect(self._exit_stroke_edit)
        h.addWidget(done)
        bar.setStyleSheet(
            "QWidget { background: rgba(250,250,252,235);"
            " border: 1px solid #b8c4d0; border-radius: 6px; }"
            "QToolButton { border: none; padding: 3px 8px; }")
        self._stroke_bar = bar
        # 覆写为主题配色（背景/文字/选中色取自当前调色板，
        # 深/浅色主题及热切换都跟随，不再硬编码浅色）
        self._restyle_stroke_bar()
        bar.hide()
        # 浮条是视口浮动控件：QAbstractScrollArea 滚动的 blit 会把它挪出
        # 视口（平移/缩放后飘到画布外）——注册锚定位置，滚动后自动复位
        self.canvas.register_overlay(bar, QPoint(10, 10))

    def _toggle_stroke_edit(self, checked: bool) -> None:
        if checked:
            objs = self._selected()
            if not objs:
                self.act_stroke_edit.setChecked(False)
                self.statusBar().showMessage(
                    "请先选择一个对象，再进入笔画编辑。", 4000)
                return
            if not hasattr(self, "stroke_editor"):
                self._build_stroke_editor()
            self._finish_text_edit_if_any()
            se = self.stroke_editor
            se.attach(objs[0])
            self.canvas.setDragMode(QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(Qt.CrossCursor)
            self._stroke_bar.adjustSize()
            self._stroke_bar.move(10, 10)
            self._stroke_bar.show()
            self.statusBar().showMessage(
                "笔画编辑：鼠标下的笔画自动高亮，点击选中；拖动笔画请按在"
                "笔画线上（空白处拖动=框选，Shift 追加）；拖动即移动，"
                "按在笔画旁空白处的角点上缩放、上方手柄旋转（单选）；"
                "Delete 删除选中；勾选「弯折」后拖点扭曲。Esc 退出。", 9000)
        else:
            self._exit_stroke_edit()

    def _exit_stroke_edit(self) -> None:
        if hasattr(self, "stroke_editor") and self.stroke_editor.active:
            self.stroke_editor.detach()
            self.canvas.viewport().unsetCursor()
            self._stroke_bar.hide()
            self.canvas.viewport().update()
        if self.act_stroke_edit.isChecked():
            self.act_stroke_edit.blockSignals(True)
            self.act_stroke_edit.setChecked(False)
            self.act_stroke_edit.blockSignals(False)

    def _finish_text_edit_if_any(self) -> None:
        if getattr(self, "_text_session", None) is not None:
            self._text_session.finish()

    # 这些来源的笔画由源内容重新生成（调扰动/换种子/打开项目都会重渲染），
    # 手工编辑必须记进编辑层，否则会被重生成冲刷掉
    _REGENERATED_KINDS = ("text", "markdown", "equation", "latex", "svg", "tikz")

    def _record_stroke_edit(self, obj, index, *, local_stroke=None,
                            delete: bool = False, display_len: int = 0) -> None:
        """记录一次手工笔画编辑（删除或改动），供对象重生成后重新施加。

        由源内容重新生成的对象（文本/富内容）走 :mod:`content.stroke_edits`
        编辑层；静态/自由矢量对象沿用 ``base_strokes`` 缓存。
        """
        if obj.source.kind in self._REGENERATED_KINDS:
            if delete:
                _record_stroke_delete(obj.source.data, index, display_len)
            elif local_stroke is not None:
                _record_stroke_modify(obj.source.data, index, local_stroke,
                                      display_len)
            return
        self._sync_stroke_edit_base(obj, index, local_stroke=local_stroke,
                                    delete=delete)

    def _commit_stroke_edit(self, obj, index, local_stroke) -> None:
        """把一次笔画手势写回对象（撤销入栈，一次手势=一步）。"""
        old = snapshot_object(obj)
        strokes = [s.clone() for s in obj.local_strokes]
        if 0 <= index < len(strokes):
            self._record_stroke_edit(obj, index, local_stroke=local_stroke,
                                     display_len=len(strokes))
            strokes[index] = local_stroke
        obj.local_strokes = strokes
        self.controller.replace_objects(
            [(obj, old, snapshot_object(obj))], "编辑笔画",
            merge_key=f"stroke-edit:{obj.id}")
        self.canvas.sync_scene()

    def _delete_stroke(self, obj, index) -> None:
        old = snapshot_object(obj)
        strokes = [s.clone() for s in obj.local_strokes]
        if 0 <= index < len(strokes):
            self._record_stroke_edit(obj, index, delete=True,
                                     display_len=len(strokes))
            strokes.pop(index)
        obj.local_strokes = strokes
        self.controller.replace_objects(
            [(obj, old, snapshot_object(obj))], "删除笔画")
        self.canvas.sync_scene()

    def _commit_strokes_edit(self, obj, indices, locals_) -> None:
        """批量改动选中笔画（框选后拖动）：一次手势=一步撤销。"""
        old = snapshot_object(obj)
        strokes = [s.clone() for s in obj.local_strokes]
        n = len(strokes)
        pairs = [(i, lc) for i, lc in zip(indices, locals_) if 0 <= i < n]
        if not pairs:
            return
        for i, lc in pairs:
            self._record_stroke_edit(obj, i, local_stroke=lc, display_len=n)
            strokes[i] = lc
        obj.local_strokes = strokes
        self.controller.replace_objects(
            [(obj, old, snapshot_object(obj))], "编辑笔画",
            merge_key=f"stroke-edit:{obj.id}")
        self.canvas.sync_scene()

    def _delete_strokes(self, obj, indices) -> None:
        """批量删除选中笔画（框选后 Delete）：一次=一步撤销。

        按下标**从大到小**删除，前列下标不受影响；编辑层记录同理。
        """
        old = snapshot_object(obj)
        strokes = [s.clone() for s in obj.local_strokes]
        changed = False
        for i in sorted(set(int(x) for x in indices), reverse=True):
            if 0 <= i < len(strokes):
                self._record_stroke_edit(obj, i, delete=True,
                                         display_len=len(strokes))
                strokes.pop(i)
                changed = True
        if not changed:
            return
        obj.local_strokes = strokes
        self.controller.replace_objects(
            [(obj, old, snapshot_object(obj))], "删除笔画")
        self.canvas.sync_scene()

    @staticmethod
    def _sync_stroke_edit_base(obj, index, local_stroke=None,
                               delete: bool = False) -> None:
        """静态/自由矢量对象：笔画编辑要同步写回「原始笔画」缓存。

        扰动重算一律从 ``base_strokes`` 出发——不同步的话，用户编辑完
        笔画后一调扰动参数，编辑就被静默回退（数据丢失感）。
        """
        base = obj.source.data.get(BASE_STROKES_KEY)
        if base is None or not (0 <= index < len(base)):
            return
        if delete:
            base.pop(index)
        elif local_stroke is not None:
            base[index] = _strokes_to_data([local_stroke])[0]

    def _delete_selected_stroke(self) -> None:
        se = self.stroke_editor
        if hasattr(self, "stroke_editor") and se.active:
            if not se.delete_selected():
                self.statusBar().showMessage("请先点选一条笔画。", 3000)

    def _on_text_props_changed(self) -> None:
        """属性面板改动 → 覆盖写回选中文本对象（即改即得，可撤销）。"""
        objs = [o for o in self._selected() if o.source.kind == "text"]
        if not objs:
            return
        obj = objs[0]
        spec = TextSpec.from_data(obj.source.data)
        vals = self.text_props.values()
        field = self.text_props.last_field
        sel_chars = self._session_selected_chars()
        if field == "font" and sel_chars:
            # 行内编辑中有字符选区：字体只作用于选中字符（字符级覆盖）
            for ch in set(sel_chars):
                if ch.strip():
                    prev = spec.char_overrides.get(ch, {})
                    spec.char_overrides[ch] = {"font": vals["font"],
                                               "scale": prev.get("scale", 1.0)}
            label, key = "指定字符字体", "font-sel"
        else:
            # 覆盖语义：新值直接覆盖旧值（字体=重写回退链首位，其余顺延）
            if vals["font"]:
                spec.font_names = ([vals["font"]]
                                   + [n for n in spec.font_names
                                      if n != vals["font"]])
            spec.size = vals["size"]
            spec.char_spacing = vals["char_spacing"]
            spec.line_spacing = vals["line_spacing"]
            spec.frame_width = vals["frame_width"]
            label, key = "修改文字属性", field or "props"
        old = snapshot_object(obj)
        update_text_object(obj, spec, self.font_manager)
        self.controller.replace_objects(
            [(obj, old, snapshot_object(obj))], label,
            merge_key=f"text-props:{obj.id}:{key}")
        self.canvas.sync_scene()

    def _import_font(self) -> None:
        self.font_panel._import()

    # ------------------------------------------------------------ 富内容
    def _insert_content(self, kind: str) -> None:
        dlg = ContentDialog(self.font_manager, initial_kind=kind, parent=self,
                            default_font_chain=self._default_font_chain())
        if dlg.exec() != ContentDialog.Accepted:
            return
        payload = dlg.payload()
        if kind == KIND_MARKDOWN:
            self._remember_font_chain(payload.data.get("font_names"))
        try:
            obj = self._build_content_object(payload)
        except Exception as exc:
            QMessageBox.critical(self, "插入失败", str(exc))
            return
        if obj is None or not obj.local_strokes:
            QMessageBox.warning(self, "无内容", "未能生成任何笔画。")
            return
        self._place_object(obj)
        self.controller.add_object(obj)
        self.canvas.sync_scene()
        self.canvas.select_object(obj)
        self._report_missing(obj)

    def _build_content_object(self, payload):
        d = payload.data
        if payload.kind == KIND_MARKDOWN:
            style = MarkdownStyle(size=float(d.get("size", 4.0)),
                                  char_spacing=float(d.get("char_spacing", 0.0)),
                                  wrap_width=float(d.get("wrap_width", 120.0)),
                                  table_width=float(d.get("table_width", 0.0)),
                                  table_end_jitter=float(
                                      d.get("table_end_jitter", 0.5)),
                                  table_overshoot=float(
                                      d.get("table_overshoot", 0.6)),
                                  table_row_jitter=float(
                                      d.get("table_row_jitter", 0.0)),
                                  table_col_jitter=float(
                                      d.get("table_col_jitter", 0.0)))
            return make_markdown_object(
                d.get("text", ""), self.font_manager,
                font_names=d.get("font_names"),
                style=style,
                perturb=PerturbParams.from_data(d.get("perturb")),
            )
        if payload.kind == KIND_EQUATION:
            return make_equation_object(
                d.get("latex", ""), float(d.get("size", 6.0)),
                perturb=PerturbParams.from_data(d.get("perturb")),
                # 公式中链上画得出的字符用本软件字体重排（手写字迹）
                font_names=d.get("font_names") or None,
                manager=self.font_manager,
                text_scale=float(d.get("text_scale", 1.0) or 1.0))
        if payload.kind == KIND_LATEX:
            from ..content.builder import make_latex_object
            return make_latex_object(d.get("source", ""),
                                     d.get("target_width"),
                                     font_names=d.get("font_names") or None,
                                     manager=self.font_manager)
        if payload.kind == KIND_TIKZ:
            from ..content.builder import make_tikz_object
            # 节点文字用所选本软件字体重排（用户自己的手写字迹）
            return make_tikz_object(
                d.get("source", ""), d.get("target_width"),
                font_names=d.get("font_names") or None,
                manager=self.font_manager,
                text_scale=float(d.get("text_scale", 1.0) or 1.0))
        if payload.kind == KIND_SVG:
            path = d.get("path", "")
            if not path:
                raise ValueError("未选择 SVG 文件")
            return make_svg_object(
                path, d.get("target_width"),
                wobble_amplitude=float(d.get("wobble_amplitude", 0.0)),
                wobble_wavelength=float(d.get("wobble_wavelength", 20.0)),
                perturb=PerturbParams.from_data(d.get("perturb")),
            )
        return None

    def _place_object(self, obj) -> None:
        """把新对象放到页面左上；若与已有内容重叠则级联错开，避免叠在一起。"""
        from ..core.geometry import AffineTransform
        page = self.controller.doc.page
        box = obj.local_bbox()
        if box.is_empty:
            return
        base_x = page.margin
        base_y = page.height - page.margin - box.y1
        step = 8.0
        x, y = base_x, base_y
        for _ in range(40):
            candidate = AffineTransform.translate(x, y)
            new_box = BBox.from_points(
                candidate.apply(p) for p in _box_corners(box))
            if not self._overlaps_existing(new_box):
                obj.transform = candidate
                return
            x += step
            y -= step
            if x + box.width > page.width - page.margin or y < page.margin:
                x, y = base_x, base_y       # 回绕，仍重叠就接受（避免无限循环）
                step = max(step * 0.5, 2.0)
                if step <= 2.0:
                    break
        obj.transform = AffineTransform.translate(base_x, base_y)

    def _overlaps_existing(self, new_box: BBox) -> bool:
        for o in self.controller.doc.objects:
            if not o.visible:
                continue
            b = o.world_bbox()
            if b.is_empty:
                continue
            if (new_box.x0 < b.x1 and new_box.x1 > b.x0
                    and new_box.y0 < b.y1 and new_box.y1 > b.y0):
                return True
        return False

    # ------------------------------------------------------------ 扰动命令
    def _text_objects(self):
        return [o for o in self._selected() if o.source.kind == "text"]

    def _perturbable_objects(self):
        """所有支持扰动的选中对象（文本 + 矢量/静态图形）。"""
        return [o for o in self._selected() if supports_perturb(o)]

    def _perturb_selected(self) -> None:
        """打开扰动对话框：文本复用文本编辑器的扰动页签，矢量用独立面板。"""
        objs = self._perturbable_objects()
        if not objs:
            QMessageBox.information(
                self, "手写扰动",
                "请先选择一个对象（文本或矢量图形均可）。")
            return
        obj = objs[0]
        before = snapshot_object(obj)
        if obj.source.kind == "text":
            spec = TextSpec.from_data(obj.source.data)
            dlg = TextEditDialog(spec, self.font_manager, self)
            dlg.tabs.setCurrentIndex(1)  # 直接切到扰动页签
            if dlg.exec() != TextEditDialog.Accepted:
                return
            new_spec = dlg.result_spec()
            update_text_object(obj, new_spec, self.font_manager)
        else:
            params = resolve_perturb(obj) or PerturbParams()
            from .perturb_panel import PerturbDialog
            hint = object_reference_size(obj)
            dlg = PerturbDialog(params, title=f"手写扰动 — {obj.name}",
                                size_hint=hint, parent=self)
            if dlg.exec() != PerturbDialog.Accepted:
                return
            apply_perturb(obj, dlg.result_params(), self.font_manager)
        self.controller.replace_object(obj, before, snapshot_object(obj),
                                       "调整手写扰动")
        self._load_global_perturb_from(obj)
        self.canvas.sync_scene()

    def _reseed_selected(self) -> None:
        """换随机种子，重新渲染选中对象（保持其它参数不变），可撤销。"""
        objs = self._perturbable_objects()
        if not objs:
            return
        self.controller.undo_stack.beginMacro("重新随机")
        for o in objs:
            p = resolve_perturb(o)
            if p is None:
                continue
            if not p.enabled:
                p = PerturbParams.natural(object_reference_size(o), seed=p.seed)
            p.seed = (p.seed * 1103515245 + 12345) % (2 ** 31)
            before = snapshot_object(o)
            apply_perturb(o, p, self.font_manager)
            self.controller.replace_object(o, before, snapshot_object(o),
                                           "重新随机")
        self.controller.undo_stack.endMacro()
        self._load_global_perturb_from(objs[-1])
        self.canvas.sync_scene()

    def _clear_perturb(self) -> None:
        objs = self._perturbable_objects()
        if not objs:
            return
        off = PerturbParams(enabled=False)
        self.controller.undo_stack.beginMacro("清除扰动")
        for o in objs:
            before = snapshot_object(o)
            apply_perturb(o, off, self.font_manager)
            self.controller.replace_object(o, before, snapshot_object(o),
                                           "清除扰动")
        self.controller.undo_stack.endMacro()
        self._load_global_perturb_from(objs[-1])
        self.canvas.sync_scene()

    # --------------------------------------------------- 全局面板 ↔ 选中对象
    def _perturb_size_hint(self, obj) -> float:
        """扰动「尺寸提示」：文本用字号，矢量用内容特征尺寸。"""
        if obj.source.kind == "text":
            try:
                return TextSpec.from_data(obj.source.data).size
            except Exception:
                return 10.0
        return object_reference_size(obj)

    def _load_global_perturb_from(self, obj) -> None:
        """把对象当前的扰动参数回填到全局面板（切换选择时调用）。"""
        p = resolve_perturb(obj)
        if p is None:
            return
        self.global_perturb.set_params(p)
        self.global_perturb.set_size_hint(self._perturb_size_hint(obj))

    # ------------------------------------------------------------ 文件命令
    def _window_title(self) -> str:
        name = Path(self.project_path).name if self.project_path else "未命名"
        mark = " *" if self._dirty else ""
        return f"{name}{mark} — WriterStudio"

    def _update_title(self) -> None:
        self.setWindowTitle(self._window_title())

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _mark_clean(self) -> None:
        self._dirty = False
        self._update_title()

    def _collect_project(self) -> ProjectData:
        """把当前界面状态汇总为项目数据（含撤销历史日志）。"""
        dirs = [str(d) for d in getattr(self.font_manager, "_extra_dirs", [])]
        return ProjectData(
            document=self.controller.doc,
            machine_config=self.machine_panel.current_config(),
            start_point=self.machine_panel.current_start_point(),
            search_dirs=dirs,
            metadata={},
            # 撤销历史日志（已撤销的 redo 分支随保存丢弃，与文档一致）
            history=self.controller.saved_history(),
        )

    def _apply_project(self, project: ProjectData) -> None:
        # 先恢复外部字体目录，再做重生成（缺字体时可回退到已存笔画）
        self._loaded_ready = False
        for d in project.search_dirs:
            try:
                self.font_manager.add_search_dir(d)
            except Exception:
                pass
        self.font_panel.refresh()
        n = regenerate_all(project, self.font_manager)
        self.controller.set_document(project.document)
        # 撤销历史日志重建（当前文档 = 最终状态，重放到此即可回撤）
        self.controller.rebuild_history(project.history)
        # 机器配置与起点（含笔位标记）
        self.machine_panel.config = project.machine_config.clone()
        self.machine_panel.start_point = project.start_point.clone()
        self.machine_panel._apply_config_to_ui()
        # 轴基线对齐到刚加载的描述：项目里的起点数据按项目自己的轴描述
        # 存的，下面的 _on_machine_config_changed 只刷新显示，不得把已加载
        # 的数据当作「旧描述」再做一次换算
        self._axis_state = self._axis_opts()
        self._on_machine_config_changed()
        # 参考层
        self.reference_panel.refresh()
        self.canvas.fit_page()
        self._refresh_object_panel()
        self._loaded_ready = True
        return n

    def _confirm_discard(self) -> bool:
        """有未保存修改时询问；返回 True 表示可以继续。

        以下情形直接放行，避免模态框在无人值守时阻塞：
          * ``confirm_on_close`` 被显式关闭（测试/自动化）
          * 窗口未显示
        """
        if not self._dirty or not self.confirm_on_close or not self.isVisible():
            return True
        r = QMessageBox.question(
            self, "未保存的修改",
            "当前项目有未保存的修改，是否保存？",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if r == QMessageBox.Save:
            return self._save_project()
        return r == QMessageBox.Discard

    def _new_project(self) -> None:
        if not self._confirm_discard():
            return
        w, h = self.settings.page_size((297.0, 210.0))
        doc = Document(page=PageSpec(width=w, height=h,
                                     margin=self.settings.page_margin(10.0),
                                     preset_name=self.settings.page_preset_name("")))
        self.controller.set_document(doc)
        self.project_path = None
        self._mark_clean()
        self.canvas.fit_page()
        self.statusBar().showMessage("已新建项目", 3000)

    def _open_project(self) -> None:
        if not self._confirm_discard():
            return
        start_dir = filedialog.start_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "打开项目", start_dir,
            f"WriterStudio 项目 (*{FILE_SUFFIX});;所有文件 (*)")
        if not path:
            return
        self._load_project_path(path)

    def _load_project_path(self, path: str) -> bool:
        try:
            project = load_project(path)
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", f"{path}\n\n{exc}")
            return False
        n = self._apply_project(project)
        self.project_path = path
        self._mark_clean()
        self.settings.add_recent_file(path)
        self.settings.set_last_dir(str(Path(path).parent))
        self._rebuild_recent_menu()
        msg = f"已打开 {Path(path).name}"
        if n:
            msg += f"（重新生成 {n} 个对象）"
        self.statusBar().showMessage(msg, 4000)
        return True

    def _save_project(self) -> bool:
        if not self.project_path:
            return self._save_project_as()
        try:
            save_project(self.project_path, self._collect_project())
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return False
        self._mark_clean()
        try:
            self.controller.undo_stack.setClean()   # 保存后撤销栈基准点=当前，
            # 否则「保存→改动→撤销回保存态」标题栏仍显示未保存标记
        except Exception:
            pass
        self.settings.add_recent_file(self.project_path)
        self.settings.set_last_dir(str(Path(self.project_path).parent))
        self._rebuild_recent_menu()
        self.statusBar().showMessage(f"已保存 {Path(self.project_path).name}", 3000)
        return True

    def _save_project_as(self) -> bool:
        default = self.project_path or f"未命名{FILE_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", filedialog.path_for(default),
            f"WriterStudio 项目 (*{FILE_SUFFIX});;所有文件 (*)")
        if not path:
            return False
        if not path.lower().endswith(FILE_SUFFIX):
            path += FILE_SUFFIX
        self.project_path = path
        return self._save_project()

    def _page_setup(self) -> None:
        page = self.controller.doc.page
        dlg = PageSetupDialog(page, self.page_presets,
                              guide_visible=self.canvas.show_page_guide,
                              parent=self,
                              on_rotate=self._rotate_page)
        if dlg.exec() != PageSetupDialog.Accepted:
            return
        new = PageSpec(dlg.result_width(), dlg.result_height(),
                       dlg.result_margin(), dlg.result_preset_name())
        self.canvas.show_page_guide = dlg.guide_visible()
        self.act_toggle_guide.setChecked(self.canvas.show_page_guide)
        # 对话框内可能已实时旋转过页面（自带完整撤销宏）。旧值必须在对话框
        # 关闭后重新读取，否则一次 Ctrl+Z 只撤回页面尺寸、内容仍是转过的。
        cur = self.controller.doc.page
        old = PageSpec(cur.width, cur.height, cur.margin, cur.preset_name)
        if old != new:
            # 起点标记钉在纸面：页面高度参与坐标映射，改尺寸前后各换算
            # 一次，保证标记的画布（纸面）位置不变
            marker_q_old = None
            if self.machine_panel.start_point.pen_marker is not None:
                marker_q_old = self._machine_to_page(
                    self.machine_panel.start_point.pen_marker)
            self.controller.set_page(old, new)
            if marker_q_old is not None:
                self.machine_panel.start_point.pen_marker = \
                    self._page_to_machine(marker_q_old)
        self.settings.set_page_size(new.width, new.height)
        self.settings.set_page_margin(new.margin)
        self.settings.set_page_preset_name(new.preset_name)
        self.canvas._update_scene_rect()
        self.canvas.fit_page()
        self.statusBar().showMessage(
            f"页面：{new.width:.0f}×{new.height:.0f} mm"
            + (f"（{new.preset_name}）" if new.preset_name else ""), 4000)

    def _toggle_page_guide(self, checked: bool) -> None:
        self.canvas.show_page_guide = bool(checked)
        self.canvas.viewport().update()

    def _rotate_page(self, direction: str) -> None:
        """把纸张连同全部内容（对象、参考层）一起旋转，保持版面相对关系。

        旋转以纸面中心为轴并重新归一化到新页面原点，因此内容不会转出纸外；
        整组操作（含新页面尺寸）合并为**一条**撤销记录。

        书写坐标按固定映射（机械原点 = 纸张左上角）在生成时换算，因此
        旋转纸张**不再改写轴映射**——内容转了方向，机器仍从左上角原点
        出发书写，机械原点标注也始终钉在左上角。
        """
        page = self.controller.doc.page
        rot, new_w, new_h = page_rotation(direction, page.width, page.height)
        label = ROTATE_LABELS.get(direction, direction)
        objs = list(self.controller.doc.objects)
        refs = list(self.controller.doc.references)
        # 起点标记是纸面上的物理参考点：纸转它也转，否则校准锚点漂移
        marker_q_old = None
        if self.machine_panel.start_point.pen_marker is not None:
            marker_q_old = self._machine_to_page(
                self.machine_panel.start_point.pen_marker)

        self.controller.undo_stack.beginMacro(f"旋转纸张（{label}）")
        for o in objs:
            self.controller.set_model_transform(o, rot @ o.transform, "旋转纸张")
        for r in refs:
            self.controller.edit_reference(
                r, "旋转纸张", {"transform": rot @ r.transform})
        self.controller.set_page(
            PageSpec(page.width, page.height, page.margin, page.preset_name),
            PageSpec(new_w, new_h, page.margin, page.preset_name),
            f"旋转纸张（{label}）")
        self.controller.undo_stack.endMacro()

        if marker_q_old is not None:
            self.machine_panel.start_point.pen_marker = self._page_to_machine(
                rot.apply(marker_q_old))

        self.canvas._update_scene_rect()
        self.canvas.fit_page()
        self._on_machine_config_changed()
        self.settings.set_page_size(new_w, new_h)
        self.statusBar().showMessage(
            f"已{label}旋转纸张：{new_w:.0f}×{new_h:.0f} mm"
            f"（{len(objs)} 个对象、{len(refs)} 个参考图随之旋转；"
            f"机械原点保持在左上角）", 5000)

    # ------------------------------------------------------------ 参考层
    def _add_reference_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "添加参考图片", filedialog.start_dir(),
            "图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif);;所有文件 (*)")
        if not path:
            return
        self._add_reference(path, KIND_IMAGE)

    def _add_reference_svg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "添加参考 SVG", filedialog.start_dir(),
            "SVG (*.svg *.svgz);;所有文件 (*)")
        if not path:
            return
        self._add_reference(path, REF_KIND_SVG)

    def _add_reference(self, path: str, kind: str) -> None:
        try:
            ref = make_reference_from_file(path, kind=kind)
        except Exception as exc:
            QMessageBox.critical(self, "添加参考图失败", str(exc))
            return
        # 默认缩放到页边距内并居中
        page = self.controller.doc.page
        fit_reference_to_box(ref, page.margin_bbox(), align="center", margin=2.0)
        self.controller.add_reference(ref)
        self.reference_panel.refresh()
        self.reference_panel.select_ref(ref)
        self.canvas.select_reference(ref)
        filedialog.remember(path)
        self.statusBar().showMessage(f"已添加参考图：{Path(path).name}", 4000)

    def _on_reference_activated(self, ref) -> None:
        """双击参考图：选中并聚焦到面板（可改透明度/适应/删除）。"""
        self.reference_panel.refresh()
        self.reference_panel.select_ref(ref)
        self.reference_dock.show()
        self.reference_dock.raise_()

    # ------------------------------------------------------------ 写字起点
    #
    # 起点标记统一以**机器坐标**存储在 ``start_point.pen_marker``
    # （标记即起点），也即「内容开始书写时笔头应处的物理位置」：
    #   * 「读取当前笔位作为起点」= 取机器坐标写入起点并落标记（校准模式下
    #     等价于重做校准，标记不动）；
    #   * 「手动标记起点」= 画布单击写入起点并落标记，之后可拖动标记微调；
    #   * 「移动到标记起点」= 直接 G0 到该机器坐标。
    # 生成 G-code 时由 :meth:`_start_anchor` 把该起点解析成「内容页面锚点 +
    # 机器系偏移」，使内容从标定笔位开始写（而非恒从纸张左上角）。
    def _pen_marker_machine(self):
        return self.machine_panel.start_point.pen_marker

    def _axis_opts(self) -> tuple[bool, bool, bool]:
        """坐标轴修正选项（对调/反转，来自机器面板「坐标轴」组）。"""
        cfg = self._machine_config()
        return (bool(getattr(cfg, "swap_xy", False)),
                bool(getattr(cfg, "invert_x", False)),
                bool(getattr(cfg, "invert_y", False)))

    def _page_to_machine(self, p):
        """页面坐标 → 机器坐标（固定映射 + 坐标轴对调/反转，正向）。

        机械原点恒在纸张左上角，轴修正只改方向不挪原点；拾取/拖动标记
        的换算与生成 G-code 同一条（:func:`page_to_machine`）。
        """
        page = self.controller.doc.page
        swap, ix, iy = self._axis_opts()
        return page_to_machine(p, page.height, swap, ix, iy)

    def _machine_to_page(self, m):
        """机器坐标 → 页面坐标（:func:`_page_to_machine` 的逆）。

        带对调时与正向不同（不可用同一函数往返），标记显示、机械原点
        标注用本函数。
        """
        page = self.controller.doc.page
        swap, ix, iy = self._axis_opts()
        return machine_to_page(m, page.height, swap, ix, iy)

    def _pen_marker_page(self, marker_m):
        if marker_m is None:
            return None
        return self._machine_to_page(marker_m)

    def _on_machine_config_changed(self) -> None:
        """起点模式/坐标变化：重画起点标记与机械原点标注。"""
        self._reglue_marker_on_axis_change()
        self._refresh_pen_marker_canvas()
        self._update_canvas_origin_marker()

    def _reglue_marker_on_axis_change(self) -> None:
        """轴映射（对调/反转）变化时，把起点数据整体换算到新轴描述。

        对调/反转只是换一种描述机器轴的方式，纸没动、笔位校准标定的
        物理对应关系也不该动。起点标记（``pen_marker``）与校准记录的
        机器读数（``start.point``）都是**旧描述下的机器坐标**，必须一起
        换算到新描述，否则：

        * 标记的画布显示跳到别处（甚至页面外），参考点失效；
        * 更隐蔽的是校准读数不换算——校准平移 = 读数 − 标记映射，标记
          换算了而读数没换，平移被破坏，写出来的内容整体错位（错位量
          = 同一物理点在新旧描述下的读数差，对调时可达几十毫米），
          画布上却看不出任何异常，薄边距处表现为部分出纸。
        """
        cfg = self._machine_config()
        new_opts = (bool(cfg.swap_xy), bool(cfg.invert_x), bool(cfg.invert_y))
        old_opts = getattr(self, "_axis_state", None)
        self._axis_state = new_opts
        if old_opts is None or old_opts == new_opts:
            return
        sp = self.machine_panel.start_point
        if sp.pen_marker is None:
            return
        page = self.controller.doc.page
        h = page.height

        def rebase(c):
            return page_to_machine(machine_to_page(c, h, *old_opts),
                                   h, *new_opts)

        sp.pen_marker = rebase(sp.pen_marker)
        sp.point = rebase(sp.point)
        self.machine_panel._update_start_summary()
        self._mark_dirty()

    def _on_machine_profile(self, prof) -> None:
        """连接探测到固件能力档案：持久化，供下次会话直接复用。"""
        try:
            self.settings.set_machine_profile_data(prof.to_data())
        except Exception:
            pass

    def _restore_machine_profile(self) -> None:
        """把上次会话的机器档案回填到 link 与面板标签（未重连前可用）。"""
        from ..machine.profile import MachineProfile
        data = self.settings.machine_profile_data()
        if not data:
            return
        try:
            prof = MachineProfile.from_data(data)
        except Exception:
            return
        self.machine_panel.link.profile = prof
        self.machine_panel.fw_label.setText(prof.summary())

    def _refresh_pen_marker_canvas(self) -> None:
        """按固定映射，把起点标记（机器坐标）重绘到正确页面位置。"""
        marker = self._pen_marker_machine()
        if marker is None:
            self.canvas.set_pen_marker(None)
            return
        self.canvas.set_pen_marker(self._pen_marker_page(marker))

    def _update_canvas_origin_marker(self) -> None:
        """机械原点标注：圆点恒画在纸张左上角，轴箭头按「坐标轴」修正。

        机械零点在线性轴修正下不动，圆点位置与对调/反转无关；X/Y 箭头
        方向 = 机器 +X/+Y 单位向量经逆映射到页面的方向（与书写映射
        :func:`page_to_machine` 一致）。不依赖起点/校准，因此拖动起点
        不会带着它乱跑；纸张旋转后仍画在新页面的左上角。
        """
        pos = self._machine_to_page((0.0, 0.0))
        xd = tuple(b - a for a, b in zip(pos, self._machine_to_page((1.0, 0.0))))
        yd = tuple(b - a for a, b in zip(pos, self._machine_to_page((0.0, 1.0))))
        self.canvas.set_machine_origin(pos, xd, yd)

    def _read_machine_pos(self):
        """取机器当前机械坐标 (x, y)（mm），没有则返回 None。"""
        pos = self.machine_panel.machine_pos()
        if pos is None and self.machine_panel.link.status is not None:
            pos = self.machine_panel.link.status.pos
        if pos is None:
            return None
        return (float(pos[0]), float(pos[1]))

    def _set_start_from_machine(self, marker_m, message: str = "") -> None:
        """把机器坐标写入写字起点，并在画布同一位置落起点标记。"""
        self.machine_panel.start_point.pen_marker = marker_m
        self.machine_panel.set_start_point(MODE_CANVAS, marker_m[0], marker_m[1])
        # 起点变了 → origin_offset 变了 → 原点标注也要跟着挪
        self._on_machine_config_changed()
        if message:
            self.statusBar().showMessage(message, 5000)
        self._mark_dirty()

    def _on_read_pen_as_start(self) -> None:
        """读取机器笔头当前位置并作为写字起点（异步等一次状态回报）。

        全程只操作状态栏提示，不弹模态框；串口异常就地消化——
        槽里抛未捕获异常会让整个应用直接退出（PySide6 行为）。
        """
        pos = self._read_machine_pos()
        if pos is not None:
            self._apply_read_pen(pos)
            return
        link = self.machine_panel.link
        if link.connected:
            self._pen_read_pending = True
            try:
                link.request_status()
            except Exception as exc:
                self._pen_read_pending = False
                self.statusBar().showMessage(f"读取失败：{exc}", 5000)
                return
            self.statusBar().showMessage("正在读取笔头位置…", 2000)
        else:
            self.statusBar().showMessage(
                "未连接机器：请先连接，或点「手动标记起点」", 5000)

    def _apply_read_pen(self, pos) -> None:
        """把读到的笔位落到起点上。

        若已处于**笔位校准**模式：用户是让笔停到标记处再点读取，因此这里
        等价于重做一次校准——保持标记不动、用新笔位刷新标定（回归：此前
        会退回画布模式并把标记移到笔位，校准被静默丢弃、起点位置随之改变）。
        否则按画布模式把标记移到笔位。
        """
        if self.machine_panel.start_point.mode == MODE_REGISTERED:
            self._register_pen_at(pos)
            return
        self._set_start_from_machine(
            pos, f"已把笔头当前位置 ({pos[0]:.1f}, {pos[1]:.1f}) mm "
                 f"设为写字起点")

    def _on_machine_status_for_marker(self, status) -> None:
        # 校准请求优先（保持标记不动）
        if self._pen_register_pending:
            pos = getattr(status, "pos", None)
            if pos is None:
                return
            self._pen_register_pending = False
            self._register_pen_at((float(pos[0]), float(pos[1])))
            return
        if not self._pen_read_pending:
            return
        pos = getattr(status, "pos", None)
        if pos is None:
            return
        self._pen_read_pending = False
        self._apply_read_pen((float(pos[0]), float(pos[1])))

    def _on_register_pen(self) -> None:
        """以当前笔位校准标记：**标记不动**，记录「笔现在就在标记处」。

        用户先把红色标记放到纸上希望起笔的位置，再把笔**手动**移到纸上同一点，
        点此按钮。软件记录笔此刻的机器坐标 M 并切换到笔位校准模式，据此建立
        「纸张坐标 ↔ 机器坐标」的对应（标记所在的纸面点 = M），机器原点与纸张
        角落的偏差由此被物理标定掉。

        与「读取当前笔位作为起点」的区别：后者会把标记移动到笔位；本功能
        保持标记原地不动。
        """
        marker = self.machine_panel.start_point.pen_marker
        if marker is None:
            self.statusBar().showMessage(
                "请先放置起点标记（用「手动标记起点」或拖动红色标记）", 5000)
            return
        pos = self._read_machine_pos()
        if pos is not None:
            self._register_pen_at(pos)
            return
        link = self.machine_panel.link
        if link.connected:
            self._pen_register_pending = True
            try:
                link.request_status()
            except Exception as exc:
                self._pen_register_pending = False
                self.statusBar().showMessage(f"读取失败：{exc}", 5000)
                return
            self.statusBar().showMessage("正在读取笔头位置…", 2000)
        else:
            self.statusBar().showMessage(
                "未连接机器：请先连接并把笔移到标记处，再点校准", 5000)

    def _register_pen_at(self, machine_pos) -> None:
        """记录笔位并切到笔位校准模式（标记保持不变）。"""
        self.machine_panel.set_start_point(
            MODE_REGISTERED, machine_pos[0], machine_pos[1])
        self._on_machine_config_changed()
        self.statusBar().showMessage(
            f"已用笔位校准：标记处 = 机器坐标 "
            f"({machine_pos[0]:.1f}, {machine_pos[1]:.1f}) mm", 5000)
        self._mark_dirty()

    def _on_mark_start_toggled(self, checked: bool) -> None:
        """「手动标记起点」：画布单击一点作为写字起点。"""
        if checked:
            self.canvas.set_pick_mode(True, self._on_start_marked)
            self.statusBar().showMessage("请在画布上单击写字起点…")
        else:
            self.canvas.set_pick_mode(False, None)

    def _on_start_marked(self, x: float, y: float) -> None:
        """画布拾取到起点（页面坐标）→ 按轴映射换算机器坐标写入起点并落标记。"""
        self.machine_panel.reset_mark_start()
        marker = self._page_to_machine((x, y))
        self._set_start_from_machine(
            marker, f"写字起点已设为机器坐标 ({marker[0]:.1f}, {marker[1]:.1f}) mm")

    def _on_pen_marker_moved(self, x: float, y: float) -> None:
        """画布拖动起点标记（页面坐标）→ 起点跟随标记。

        若处于「笔位校准」模式：拖动只是把纸面参考钉挪到新位置，纸没动，
        校准得到的**平移量**必须保持——否则拖多少毫米，写出来的内容就
        整体错位多少毫米，画布上看不出。因此标记挪到新页面位置的同时，
        记录的机器读数按「新标记位置 + 原校准平移」重推，内容落点完全
        不变；若想让笔重新对准新标记，把笔移过去再点一次校准即可。
        """
        sp = self.machine_panel.start_point
        marker = self._page_to_machine((x, y))
        if sp.mode == MODE_REGISTERED and sp.pen_marker is not None:
            t = (sp.point[0] - sp.pen_marker[0],
                 sp.point[1] - sp.pen_marker[1])
            sp.pen_marker = marker
            self.machine_panel.set_start_point(
                MODE_REGISTERED, marker[0] + t[0], marker[1] + t[1])
            self._mark_dirty()
            self.statusBar().showMessage(
                "标记已移动，校准平移保持不变（书写内容不动）；"
                "如需以新位置为准，把笔移到标记处重新校准", 5000)
            return
        self._set_start_from_machine(marker)

    def _on_remove_start_marker(self) -> None:
        """「移除手动标记点」：清掉标记（退出笔位校准），画布不再显示。"""
        self.machine_panel.remove_start_marker()
        self._mark_dirty()
        self.statusBar().showMessage("已移除手动标记点", 4000)

    def _on_move_pen_to_marker(self) -> None:
        """把笔头移动到起点标记（机器坐标，直接 G0）。

        笔位校准模式下，标记的物理位置对应的机器坐标是校准记录的笔位
        （``start.point``），不是标记拖放时按轴映射算出的坐标——用后者会
        带着「原点与纸张的偏差」跑，正好差掉校准要修正的那段距离。
        """
        sp = self.machine_panel.current_start_point()
        if sp.mode == MODE_REGISTERED:
            marker = (sp.point[0], sp.point[1])
        else:
            marker = self._pen_marker_machine()
        if marker is None:
            self.statusBar().showMessage("尚未设置写字起点", 4000)
            return
        link = self.machine_panel.link
        if not link.connected:
            self.statusBar().showMessage("未连接机器：请先在上方连接串口", 4000)
            return
        cfg = self.machine_panel.current_config()
        mx, my = marker[0], marker[1]
        try:
            # 抬笔后再移动，避免划伤纸张
            for line in cfg.pen_up_lines():
                link.send_line(line)
            link.goto(mx, my, feed=cfg.travel_feed)
        except Exception as exc:
            self.statusBar().showMessage(f"移动失败：{exc}", 5000)
            return
        self.statusBar().showMessage(
            f"已命令笔头移动到机器坐标 ({mx:.1f}, {my:.1f}) mm", 4000)

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        recents = self.settings.recent_files()
        if not recents:
            a = self.recent_menu.addAction("（无）")
            a.setEnabled(False)
            return
        for p in recents:
            act = self.recent_menu.addAction(p)
            act.triggered.connect(lambda checked=False, path=p: self._open_recent(path))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("清除列表", self._clear_recent)

    def _open_recent(self, path: str) -> None:
        if not Path(path).exists():
            QMessageBox.warning(self, "文件不存在", path)
            recents = [p for p in self.settings.recent_files() if p != path]
            self.settings.set("files/recent", recents)
            self._rebuild_recent_menu()
            return
        if not self._confirm_discard():
            return
        self._load_project_path(path)

    def _clear_recent(self) -> None:
        self.settings.clear_recent_files()
        self._rebuild_recent_menu()

    # ------------------------------------------------------------ 设置恢复
    def _restore_settings(self) -> None:
        # 外部字体目录 + 置顶/隐藏字体
        for d in self.settings.font_search_dirs():
            try:
                self.font_manager.add_search_dir(d)
            except Exception:
                pass
        for n in self.settings.font_pinned():
            self.font_manager.pin(n, True)
        for n in self.settings.font_hidden():
            self.font_manager.hide(n, True)
        self.font_panel.refresh()
        # 机器参数（含起点标记）；恢复失败用默认值并记日志，
        # 但坐标轴/标记标注仍按当前（默认或部分恢复的）配置初始化
        try:
            self.settings.load_machine_into(self.machine_panel.config,
                                            self.machine_panel.start_point)
            # 兼容旧数据：画布模式记了起点坐标但没落标记 → 补落标记
            self.machine_panel.normalize_start_point()
            migrated = self.settings.apply_machine_pen_defaults(
                self.machine_panel.config)
            if self.settings.apply_p40_write_defaults(
                    self.machine_panel.config):
                migrated = True
                logging.getLogger(__name__).info(
                    "已把书写参数一次性迁移为 P40 推荐值"
                    "（Z 速 12000 / 书写 4400 / 收笔斜抬 1.5 / 入笔斜落 0.5）")
            # 先把（可能已迁移的）配置刷进控件，再落盘——否则 save_machine
            # 会经 current_config() 读回旧控件值，把迁移结果覆盖掉
            self.machine_panel._apply_config_to_ui()
            if migrated:
                logging.getLogger(__name__).info(
                    "已把抬落笔默认迁移为步进 Z 轴（抬0/落6, zSpeed 3000）")
                self.settings.save_machine(self.machine_panel.current_config(),
                                           self.machine_panel.current_start_point())
        except Exception:
            logging.getLogger(__name__).exception("恢复机器配置失败，使用默认值")
        # 面板散项（点动步长/速度、连接选项、单步/休眠、波特率）
        try:
            self.machine_panel.apply_panel_state(self.settings.panel_state())
            self.machine_panel.set_baud(self.settings.baud())
        except Exception:
            logging.getLogger(__name__).exception("恢复面板参数失败，使用默认值")
        # 轴基线对齐到刚恢复的描述（理由同 _apply_project：存档里的起点
        # 数据按存档时的轴描述解释，恢复本身不做换算）
        self._axis_state = self._axis_opts()
        self._on_machine_config_changed()
        # 页面边距/预设
        self.controller.doc.page.margin = self.settings.page_margin(10.0)
        self.controller.doc.page.preset_name = self.settings.page_preset_name("")
        self._rebuild_recent_menu()
        self._update_title()

    def _persist_settings(self) -> None:
        dirs = [str(d) for d in getattr(self.font_manager, "_extra_dirs", [])]
        self.settings.set_font_search_dirs(dirs)
        self.settings.set_font_pinned(self.font_manager.pinned_names())
        self.settings.set_font_hidden(self.font_manager.hidden_names())
        try:
            self.settings.save_machine(self.machine_panel.current_config(),
                                       self.machine_panel.current_start_point())
        except Exception:
            pass
        # 面板散项（点动步长/速度、连接选项、单步/休眠）——不属于
        # GCodeConfig，单独持久化，避免用户每次重开都要重设
        try:
            self.settings.set_panel_state(**self.machine_panel.panel_state())
            self.settings.set_baud(self.machine_panel.baud())
        except Exception:
            pass
        page = self.controller.doc.page
        self.settings.set_page_size(page.width, page.height)
        self.settings.set_page_margin(page.margin)
        self.settings.set_page_preset_name(page.preset_name)
        self.settings.sync()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 首次显示后给右侧「对象/机器控制」标签组一个能完整放下机器面板的
        # 默认宽度：过早调用会被首次布局覆盖，故挂到事件循环下一拍。
        if not getattr(self, "_dock_widths_applied", False):
            self._dock_widths_applied = True
            # 带接收者版本：窗口销毁时定时器自动取消，不致回调已删对象
            QTimer.singleShot(0, self, self._apply_default_dock_widths)

    def _apply_default_dock_widths(self) -> None:
        # 左侧字体/扰动/参考层标签组与右侧「对象/机器控制」标签组各给一个
        # 能完整显示内容的默认宽度：字体面板的 sizeHint 偏大（缩略图列表），
        # 不约束的话默认布局会把右侧挤到需要横向滚动、控件显示不全。
        self.resizeDocks([self.font_panel, self.perturb_dock, self.reference_dock],
                         [280, 280, 280], Qt.Horizontal)
        # 机器面板最宽行约 267px（qdarktheme 默认字体密度下），取 300 留些
        # 余量；窄屏上用户仍可手动拖窄（内部有滚动区兜底）。
        self.resizeDocks([self.dock, self.machine_dock], [300, 300],
                         Qt.Horizontal)

    def closeEvent(self, event) -> None:
        # 先结束进行中的就地编辑（文字编辑逐键即时提交、笔画编辑丢弃
        # 未松手的预览），再判断是否未保存——否则弹确认框时用户刚才
        # 输入的内容还没进文档，确认语义是错的
        if getattr(self, "_text_session", None) is not None:
            self._text_session.finish()
        if hasattr(self, "stroke_editor") and self.stroke_editor.active:
            self._exit_stroke_edit()
        if not self._confirm_discard():
            event.ignore()
            return
        self.global_perturb.flush()     # 挂起的扰动参数调整立即应用
        self._persist_settings()
        # 停掉字体面板的后台加载线程，避免退出后线程回调已销毁的控件
        try:
            self.font_panel.shutdown()
        except Exception:
            pass
        # 断开串口：不主动断的话读线程是 daemon、串口不会关闭，
        # 作业还在控制器缓冲里就会继续写
        try:
            self.machine_panel.link.disconnect()
        except Exception:
            pass
        self.stop_ai_server()
        super().closeEvent(event)

    # ------------------------------------------------------------ 机器命令
    def _machine_config(self):
        """取机器面板配置（书写坐标由固定映射决定，起点不再产生偏移）。

        起点系统现在只负责「写之前把笔停到哪」（首条 G0 / 完成后回位），
        内容恒按固定映射从纸张左上角原点铺开——WYSIWYG：页面上内容的
        位置/方向就是纸上的位置/方向。
        """
        return self.machine_panel.current_config()

    def _start_machine_xy(self):
        """起点是否已设置（未设置返回 None）。

        起点以机器坐标存储于 ``pen_marker``（画布/校准模式），或绝对原点
        模式；真正决定书写落点的是 :meth:`_start_anchor`，这里只回答
        「有没有设置起点」——发送前据此询问是否以机械原点开始。
        """
        sp = self.machine_panel.start_point
        if sp.mode == MODE_ORIGIN:
            return None
        return sp.pen_marker

    def _start_anchor(self):
        """解析当前写字起点，返回 ``(s_page, machine_offset)``；未设置返回 ``None``。

        * ``s_page`` —— 内容开始书写的**页面锚点**（首条 G0 的目标）：
          普通起点取「内容包围盒左下角」（对齐到笔位），笔位校准取标记
          所在的页面点。
        * ``machine_offset`` —— 机器系刚体平移
          ``标定笔位 − page_to_machine(s_page)``。它把纸张在台面上的实际
          位置烘焙进绝对坐标：映射后的 ``s_page`` 恰好落到用户标定的物理
          笔位，内容也就从标定处开始写（机器原点与纸张角落的偏差由此
          被物理标定掉）。

        标记在画布上的显示仍走**仅轴映射**（``_machine_to_page``），与
        起点偏移解耦，避免「标记→起点→偏移→标记」回环。
        """
        sp = self.machine_panel.start_point
        if sp.mode == MODE_ORIGIN:
            return None
        marker = sp.pen_marker
        if marker is None:
            return None
        page = self.controller.doc.page
        swap, ix, iy = self._axis_opts()

        def pm(p):
            return page_to_machine(p, page.height, swap, ix, iy)

        if sp.mode == MODE_REGISTERED:
            # 标记的页面点即用户放置的物理参考点，标定笔位记在 start.point
            s_page = self._machine_to_page(marker)
            target = (float(sp.point[0]), float(sp.point[1]))
        else:
            box = content_bbox(self.controller.doc)
            s_page = (box.x0, box.y0) if not box.is_empty \
                else self._machine_to_page(marker)
            target = (float(marker[0]), float(marker[1]))
        mx, my = pm(s_page)
        return s_page, (target[0] - mx, target[1] - my)

    def _build_gcode(self, optimize: bool = True):
        cfg = self._machine_config()
        anchor = self._start_anchor()
        start, offset = anchor if anchor is not None else (None, (0.0, 0.0))
        # start_point 是页面锚点；生成端先按固定映射换算成机器坐标，再叠加
        # 起点偏移——两者合起来让内容从标定的物理笔位开始写。
        return generate_from_document(self.controller.doc, cfg,
                                      optimize=optimize, start_point=start,
                                      start_offset=offset)

    def _generate_preview(self) -> None:
        if not self.controller.doc.objects:
            QMessageBox.information(self, "生成 G-code", "文档为空。")
            return
        sp = self.machine_panel.start_point
        if sp.mode != MODE_ORIGIN and sp.pen_marker is not None:
            q = self._machine_to_page(sp.pen_marker)
            page = self.controller.doc.page
            if not (0 <= q[0] <= page.width and 0 <= q[1] <= page.height):
                self.statusBar().showMessage(
                    f"提示：起点标记在页面外 ({q[0]:.1f}, {q[1]:.1f}) mm，"
                    "校准可能已失效，建议重新放置并校准", 8000)
        if not self._warn_bounds():
            return
        self.global_perturb.flush()
        from ..machine.gcode_gen import collect_grouped_strokes
        from ..machine.path_optimizer import order_for_writing
        result = self._build_gcode(optimize=True)
        strokes, groups = collect_grouped_strokes(self.controller.doc.objects)
        # 与生成 G-code 用同一条笔序规划，否则预览的空程线与实际书写顺序不一致
        ordered = order_for_writing(
            strokes, groups=groups,
            mode=getattr(self._machine_config(), "order_mode", "reading"))
        # 空程线用与笔画相同的文档坐标系（映射前），否则设置了起点偏移
        # 或轴映射时预览里空程线与笔画错位
        travels = []
        cur = ordered[0].points[0] if ordered else (0.0, 0.0)
        for s in ordered:
            if s.points[0] != cur:
                travels.append((cur, s.points[0]))
            cur = s.points[-1]
        dlg = GCodePreviewDialog(result, ordered, travels, self)
        dlg.exec()

    def _export_gcode(self) -> None:
        if not self.controller.doc.objects:
            return
        from PySide6.QtWidgets import QFileDialog
        if not self._warn_bounds():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 G-code", filedialog.path_for("output.gcode"),
            "G-code (*.gcode *.nc *.ngc);;所有文件 (*)")
        if not path:
            return
        filedialog.remember(path)
        self.global_perturb.flush()
        result = self._build_gcode(optimize=True)
        try:
            with open(path, "w", encoding="ascii", errors="replace") as f:
                f.write(result.text())
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self, "导出完成",
            f"已导出 {len(result.lines)} 行到\n{path}\n"
            f"绘制 {result.draw_length:.1f} mm，空程 {result.travel_length:.1f} mm")

    def _warn_stale_marker(self) -> bool:
        """起点标记显示在页面外 → 标记/校准已因轴映射或页面几何变化失效。

        标记是「纸面上的物理参考点」，正常只可能被放在纸内；它跑到页面
        外，说明放置之后坐标轴设置或页面尺寸变过（或来自旧版本数据的
        陈旧标定），此前的笔位校准已不可信——直接书写会整体错位，而
        画布上的内容位置看不出任何异常。允许用户确认知情后强行继续。
        """
        sp = self.machine_panel.start_point
        if sp.mode == MODE_ORIGIN or sp.pen_marker is None:
            return True
        q = self._machine_to_page(sp.pen_marker)
        page = self.controller.doc.page
        tol = 1e-6
        if (-tol <= q[0] <= page.width + tol
                and -tol <= q[1] <= page.height + tol):
            return True
        r = QMessageBox.warning(
            self, "起点标记在页面外",
            f"起点标记当前位于页面外 ({q[0]:.1f}, {q[1]:.1f}) mm。\n"
            "这通常说明放置标记后改过坐标轴设置或页面尺寸，\n"
            "先前的笔位校准已失效，直接书写会整体错位。\n\n"
            "建议：移除标记后重新放置并校准。仍要继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes

    def _send_job(self) -> None:
        link = self.machine_panel.link
        if not link.connected:
            QMessageBox.warning(self, "未连接", "请先在机器面板连接串口。")
            return
        if link.state is LinkState.ALARM:
            QMessageBox.warning(
                self, "机器报警",
                "控制器处于 ALARM 状态，作业不会被执行。\n"
                "请先回零（$H）或解锁（$X）再发送。")
            return
        if link.state.value in ("running", "paused"):
            QMessageBox.information(self, "正在发送", "已有作业在发送中。")
            return
        if not self._warn_stale_marker():
            return
        if not self._warn_bounds():
            return
        self.global_perturb.flush()
        result = self._build_gcode(optimize=True)
        if self._start_machine_xy() is None:
            r = QMessageBox.question(
                self, "写字起点",
                "当前还没有设置起点，是否以机械原点作为起点？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if r == QMessageBox.Yes:
                # 先读取笔的坐标，把笔移动到机械原点，再开始写
                self._send_job_from_origin(result)
                return
            # 否：不设起点，笔从当前位置直接开始（作业首条 G0 会移到首笔）
        self._dispatch_job(result)

    def _dispatch_job(self, result) -> None:
        try:
            self.machine_panel.link.load_job(result.lines)
        except Exception as exc:
            self.statusBar().showMessage(f"发送失败：{exc}", 6000)
            return
        self.machine_panel.add_draw_length(result.draw_length)  # 笔寿命累计
        self.machine_panel.set_progress(f"发送中：0%（0/{len(result.lines)} 行）")

    def _send_job_from_origin(self, result) -> None:
        """以机械原点为起点发送：先读笔位，再移到原点，然后开始作业。"""
        link = self.machine_panel.link
        pos = self._read_machine_pos()
        if pos is None:
            # 笔位尚未回报：记下作业，等一次状态回报再继续
            self._origin_job_pending = result
            try:
                link.request_status()
            except Exception as exc:
                self._origin_job_pending = None
                self.statusBar().showMessage(f"读取失败：{exc}", 5000)
                return
            self.statusBar().showMessage("正在读取笔头位置…", 2000)
            return
        self._origin_move_and_send(result, pos)

    def _origin_move_and_send(self, result, pos) -> None:
        link = self.machine_panel.link
        cfg = self.machine_panel.current_config()
        try:
            for line in cfg.pen_up_lines():       # 先抬笔，避免划伤纸面
                link.send_line(line)
            link.goto(0.0, 0.0, feed=cfg.travel_feed)   # 移动到机械原点
        except Exception as exc:
            self.statusBar().showMessage(f"移动失败：{exc}", 5000)
            return
        self.statusBar().showMessage(
            f"笔位 ({pos[0]:.1f}, {pos[1]:.1f}) mm，已移动到机械原点，开始书写…",
            4000)
        self._dispatch_job(result)

    def _on_machine_status_for_send(self, status) -> None:
        """「以机械原点为起点」等待笔位回报的续场。"""
        result = getattr(self, "_origin_job_pending", None)
        if result is None:
            return
        pos = getattr(status, "pos", None)
        if pos is None:
            return
        self._origin_job_pending = None
        self._origin_move_and_send(result, (float(pos[0]), float(pos[1])))

    def _about(self) -> None:
        QMessageBox.information(
            self, "关于 WriterStudio",
            "WriterStudio — ESP32 GRBL 写字机软件\n\n"
            "已实现：P1 画布 / P2 字体 / P3 手写扰动 / P4 富内容 / P5 机器控制 / "
            "P6 项目存档 / P7 排版对齐\n"
            "新增：纸张预设与编辑器、参考层（图片/SVG 对齐）、笔位对齐标记、\n"
            "对象显示/锁定、对齐与分布、缩放到选中、缺字与越界提示。",
        )
