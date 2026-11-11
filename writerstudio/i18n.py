"""界面语言：跟随系统 locale，也可在「视图 → 语言」里手动固定。

源码里的中文就是中文文案；英文在 :mod:`writerstudio.i18n_en` 里按
「中文原文 → 英文」查表。查不到时回退原文——宁可露出中文，也不显示
空白或占位符。

用法约定：

* 静态文案：``tr("新建")``；
* 带占位符的动态消息：``trf("已保存 {name}", name=...)``——先翻译模板
  再 ``format``，这样英文语序可以调整；
* 只翻译**给人看**的文字。配置键、对象名、协议字段、日志等内部字符串
  不要过 ``tr()``，否则切到英文会改变行为。

语言解析顺序：环境变量 ``WRITERSTUDIO_LANG``（测试/冒烟用）> 设置项
``ui/language``（auto/zh/en）> 系统 ``LC_ALL``/``LC_MESSAGES``/``LANG``。
没有 locale 信息时按英文——这是 AppImage 目录收录时要求的默认行为
（非中文环境显示英文，中文环境仍显示中文）。
"""

from __future__ import annotations

import os

from .i18n_en import TRANSLATIONS

LANG_AUTO = "auto"
LANG_ZH = "zh"
LANG_EN = "en"
LANGUAGES = (LANG_AUTO, LANG_ZH, LANG_EN)
LANGUAGE_NAMES = {
    LANG_AUTO: "自动（跟随系统）",
    LANG_ZH: "中文",
    LANG_EN: "English",
}

#: 测试 / 打包冒烟用的强制覆盖，优先级最高
ENV_OVERRIDE = "WRITERSTUDIO_LANG"

_current: str | None = None


def _is_chinese_locale(value: str) -> bool:
    value = (value or "").strip().lower()
    return value.startswith("zh") or value.startswith("chinese")


def _qt_locale_name() -> str:
    """Qt 看到的系统 locale（Windows/macOS 从桌面启动时没有 LANG）。"""
    try:
        from PySide6.QtCore import QLocale
        return QLocale.system().name() or ""
    except Exception:
        return ""


def _python_locale_name() -> str:
    try:
        import locale
        name = locale.getlocale()[0] or ""
        return str(name)
    except Exception:
        return ""


def system_language() -> str:
    """按系统 locale 判断界面语言：中文环境用中文，其余一律英文。

    解析顺序：``LC_ALL``/``LC_MESSAGES``/``LANG``/``LANGUAGE``（Linux 与
    终端启动最可靠）→ Qt 的 ``QLocale.system()``（Windows/macOS 桌面启动
    时没有 LANG）→ Python ``locale``。都没有信息时按英文——AppImage 目录
    收录要求非中文环境显示英文。
    """
    for key in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(key) or ""
        if value.strip():
            return LANG_ZH if _is_chinese_locale(value) else LANG_EN
    for name in (_qt_locale_name(), _python_locale_name()):
        if name.strip():
            return LANG_ZH if _is_chinese_locale(name) else LANG_EN
    return LANG_EN


def normalize(preference: str | None) -> str:
    """把设置值归一到 ``auto`` / ``zh`` / ``en``。"""
    value = (preference or "").strip().lower()
    return value if value in LANGUAGES else LANG_AUTO


def resolve(preference: str | None = None) -> str:
    """把偏好（auto/zh/en，空=auto）解析成实际语言。"""
    pref = normalize(preference)
    if pref in (LANG_ZH, LANG_EN):
        return pref
    return system_language()


def update(preference: str | None = None) -> str:
    """重新计算当前语言；``preference`` 为空时只看环境变量/系统 locale。

    应用启动时调用一次；测试与冒烟脚本也用它切换语言。返回实际语言。
    """
    global _current
    override = (os.environ.get(ENV_OVERRIDE) or "").strip()
    if override:
        _current = resolve(override)
    else:
        _current = resolve(preference)
    return _current


def current_language() -> str:
    if _current is None:
        update()
    return _current  # type: ignore[return-value]


def is_english() -> bool:
    return current_language() == LANG_EN


def tr(text: str) -> str:
    """翻译静态文案（中文原文 → 当前语言）。"""
    if not text or current_language() == LANG_ZH:
        return text
    return TRANSLATIONS.get(text, text)


def trf(template: str, **values: object) -> str:
    """翻译带 ``{占位符}`` 的消息模板后再格式化。"""
    return tr(template).format(**values)
