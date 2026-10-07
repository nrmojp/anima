"""Dashboard-only presentation catalogs; never translate stored user content."""

from functools import lru_cache
import html
import json
from pathlib import Path
import re
from http.cookies import CookieError, SimpleCookie

SUPPORTED_LOCALES = ("en", "ja")
TOKEN = re.compile(r"__ANIMA_I18N_([a-z0-9_]+)__")
LOCALE_COOKIE = "anima_dashboard_locale"


def locale_from_cookie(header: object) -> str | None:
    """Read a viewer preference, ignoring malformed or unsupported cookies."""
    if not isinstance(header, str):
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(header)
    except CookieError:
        return None
    candidate = cookies.get(LOCALE_COOKIE)
    return candidate.value if candidate and candidate.value in SUPPORTED_LOCALES else None


def normalize_locale(value: object) -> str:
    """Unsupported or malformed deployment metadata falls back to English."""
    return value if isinstance(value, str) and value in SUPPORTED_LOCALES else "en"


@lru_cache(maxsize=1)
def catalogs() -> dict[str, dict[str, str]]:
    return json.loads(Path(__file__).with_name("assets").joinpath("messages.json").read_text())


def message(value: str, locale: str) -> str:
    """Localize a known UI label, leaving extension-owned labels untouched."""
    target = catalogs()[normalize_locale(locale)]
    for language in catalogs().values():
        for key, text in language.items():
            if text == value:
                return target[key]
    return value


def render_asset(source: str, locale: str, *, script: bool = False) -> str:
    """Resolve trusted template tokens, safely escaping HTML or JS text."""
    language = catalogs()[normalize_locale(locale)]

    def replace(match: re.Match[str]) -> str:
        text = language[match[1]]
        if script:
            return (text.replace("\\", "\\\\").replace("'", "\\'")
                    .replace('"', '\\"').replace("`", "\\`")
                    .replace("${", "\\${").replace("\n", "\\n")
                    .replace("\r", "\\r").replace("\u2028", "\\u2028")
                    .replace("\u2029", "\\u2029"))
        return html.escape(text, quote=True)

    return TOKEN.sub(replace, source)
