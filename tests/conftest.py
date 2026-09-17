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


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """每个测试用独立的内存设置后端。

    QSettings 已重定向到临时目录，但同一次 pytest 会话内所有测试共享一个
    配置文件；主窗口关闭时会回写机器参数（含起点标记），先跑的测试会把
    「手动标记点」泄漏给后面的测试。换成每测试独立的内存后端，测试顺序
    不再影响结果。
    """
    from writerstudio import settings as _settings
    monkeypatch.setattr(_settings, "_QSettingsBackend",
                        _settings._MemoryBackend)
