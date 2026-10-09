"""应用入口：``python -m writerstudio`` 或安装后的 ``writerstudio``。"""

from __future__ import annotations


def _install_crash_log() -> None:
    """把致命信号（段错误/abort）时的 Python 栈写进 ``~/.writerstudio/crash.log``。

    Qt 内部崩溃的 core 里只有 C++ 帧（0.2.0 的四次 SIGSEGV 就是这样，栈上
    全是 libQt6Widgets 的地址，连不上 Python 侧发生了什么）；faulthandler
    在信号发生时 dump 所有线程的 Python 栈，是唯一能留下「当时在跑什么」
    的手段。写失败（只读 HOME 等）时静默跳过，不影响启动。
    """
    try:
        import faulthandler
        import os
        import time

        from .recovery import home_dir

        path = home_dir() / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a", buffering=1, encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=handle, all_threads=True)
        handle.write(f"\n=== 会话开始 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"pid={os.getpid()} ===\n")
    except Exception:
        pass


def main() -> int:
    import sys

    _install_crash_log()

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
    # 异常退出留下的未保存内容：问一句是否恢复（正常启动无该文件则跳过）
    try:
        win.maybe_offer_recovery()
    except Exception:
        pass
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
