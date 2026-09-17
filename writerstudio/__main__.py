"""应用入口：``python -m writerstudio`` 或安装后的 ``writerstudio``。"""

from __future__ import annotations


def main() -> int:
    import sys

    from PySide6.QtWidgets import QApplication

    from .settings import Settings
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("WriterStudio")
    app.setOrganizationName("WriterStudio")

    settings = Settings()
    # 主题在创建任何窗口前套用（QApplication 级样式表全局生效）；
    # 主题库缺失时返回 False，退化为系统原生样式
    from .ui.theme import apply_theme
    apply_theme(app, settings.ui_theme())

    win = MainWindow(settings=settings)

    # 命令行可带一个项目文件直接打开
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        win._load_project_path(args[0])

    win.show()
    try:
        return app.exec()
    finally:
        # 任何异常退出路径都走一次正常关窗：closeEvent 里会停掉字体
        # 后台线程、断开串口——带着活线程解释器退出会段错误闪退
        try:
            win.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
