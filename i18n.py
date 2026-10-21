"""Small, Qt-independent translation interface. Chinese source text is the msgid."""
from __future__ import annotations

import ctypes
import json
import locale
import sys
from functools import lru_cache
from pathlib import Path

LANGUAGES = {"system": "跟随系统 / System", "zh-CN": "简体中文", "ja-JP": "日本語",
             "en-US": "English", "ko-KR": "한국어"}
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
_language = "zh-CN"


def system_language():
    try:
        language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return locale.windows_locale.get(language_id, "en_US")
    except (AttributeError, OSError):
        return locale.getlocale()[0] or "en_US"


def resolve_language(value):
    candidate = system_language() if value == "system" else value
    if not isinstance(candidate, str):
        return "en-US"
    prefix = candidate.replace("_", "-").split("-")[0].lower()
    return {"zh": "zh-CN", "ja": "ja-JP", "en": "en-US", "ko": "ko-KR"}.get(prefix, "en-US")


def set_language(value):
    global _language
    _language = resolve_language(value)
    return _language


def current_language():
    return _language


@lru_cache(maxsize=8)
def catalogue(language):
    try:
        data = json.loads((ROOT / "locales" / f"{language}.json").read_text(encoding="utf-8"))
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def tr(source, **values):
    translated = catalogue(_language).get(source, catalogue("en-US").get(source, source))
    if values:
        try:
            return translated.format(**values)
        except (KeyError, ValueError, IndexError):
            return source.format(**values)
    return translated


def translate_message(source):
    """Localize backend messages while leaving HID/error classification unchanged."""
    translated = tr(source)
    if translated != source or source in catalogue(_language):
        return translated
    for prefix in ("上次更新 ", "约 ", "读取失败：", "耳机读取失败：", "暂时无法读取：", "未更改成功："):
        if source.startswith(prefix):
            return tr(prefix) + translate_message(source[len(prefix):])
    return source
