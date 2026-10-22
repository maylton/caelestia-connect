"""Minimal translation layer: English source strings, catalogs keyed by language."""

from __future__ import annotations

import locale
import os


def _language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return value.split(":")[0].split(".")[0]
    try:
        return (locale.getlocale()[0] or "en").split(".")[0]
    except ValueError:
        return "en"


LANG = _language()
_catalog: dict[str, str] = {}

if LANG.startswith("pt"):
    from .locale_pt_br import CATALOG as _catalog  # noqa: F811


def _(text: str) -> str:
    return _catalog.get(text, text)
