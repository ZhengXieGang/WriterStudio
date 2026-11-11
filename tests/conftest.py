"""测试全局配置。

把 ``QSettings`` 重定向到临时目录，避免运行测试时污染用户真实的
``~/.config/WriterStudio/WriterStudio.conf``（主窗口关闭时会保存设置）。

``QSettings`` 在 Linux 上按 ``$XDG_CONFIG_HOME``（缺省 ``~/.config``）定位
配置文件，因此在导入任何 Qt 模块前设置该环境变量即可隔离。
"""

from __future__ import annotations

import os
import tempfile

import pytest

_REDIRECTED = os.environ.setdefault(
    "XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="writerstudio-test-config-"))
os.makedirs(os.path.join(_REDIRECTED, "WriterStudio"), exist_ok=True)

# 现有用例大量断言中文界面文案（按钮/标题/状态），测试环境固定中文；
# i18n 的英文行为由 test_i18n.py 显式切换后再断言。直接覆盖而不是
# setdefault——开发机/CI 导出 LANG 或 WRITERSTUDIO_LANG 时结果也应一致。
os.environ["WRITERSTUDIO_LANG"] = "zh"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """每个测试用独立的内存设置后端。

    QSettings 已重定向到临时目录，但同一次 pytest 会话内所有测试共享一个
    配置文件；主窗口关闭时会回写机器参数（含起点标记），先跑的测试会把
    「手动标记点」泄漏给后面的测试。换成每测试独立的内存后端，测试顺序
    不再影响结果。
    """
    from writerstudio import i18n
    from writerstudio import settings as _settings
    monkeypatch.setenv("WRITERSTUDIO_LANG", "zh")
    i18n.update()               # 清掉上一个用例可能留下的语言缓存
    monkeypatch.setattr(_settings, "_QSettingsBackend",
                        _settings._MemoryBackend)
