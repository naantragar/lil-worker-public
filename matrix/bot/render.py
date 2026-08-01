"""Markdown -> Matrix formatted_body (org.matrix.custom.html). Mirrors the intent of the Telegram
renderer but targets Matrix's HTML subset. Kept deliberately small and dependency-light."""
from __future__ import annotations

import markdown as _md


def to_html(text: str) -> str:
    """Render Markdown to the HTML Matrix clients accept. Fenced code, tables, lists, etc."""
    return _md.markdown(
        text,
        extensions=["fenced_code", "tables", "sane_lists", "nl2br"],
        output_format="html",
    )
