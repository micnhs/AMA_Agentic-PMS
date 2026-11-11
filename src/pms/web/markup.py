"""Minimal, escaping-by-default HTML builder (no template engine).

Every value that is not already :class:`Markup` is escaped with :func:`html.escape` (quotes included),
both as element content and as attribute value. Only code in this package creates :class:`Markup`.
"""
from __future__ import annotations

import html
import json
from typing import Any, Iterable
from urllib.parse import quote

VOID_TAGS = frozenset({"br", "hr", "img", "input", "link", "meta", "col", "source", "wbr"})


class Markup(str):
    """A string that is safe HTML (already escaped)."""

    __slots__ = ()


def escape(value: Any) -> Markup:
    if isinstance(value, Markup):
        return value
    if value is None:
        return Markup("")
    return Markup(html.escape(str(value), quote=True))


def _flatten(children: Iterable[Any]) -> Iterable[Any]:
    for child in children:
        if child is None or child is False:
            continue
        if isinstance(child, (list, tuple)) or (hasattr(child, "__iter__") and hasattr(child, "__next__")):
            yield from _flatten(child)
        else:
            yield child


def join(*children: Any) -> Markup:
    """Concatenate children; plain strings are escaped."""
    return Markup("".join(escape(c) for c in _flatten(children)))


def _attr_name(key: str) -> str:
    return key.rstrip("_").replace("_", "-")


def attrs(values: dict[str, Any]) -> Markup:
    parts = []
    for key, value in values.items():
        if value is None or value is False:
            continue
        name = _attr_name(key)
        if value is True:
            parts.append(f" {name}")
        else:
            parts.append(f' {name}="{html.escape(str(value), quote=True)}"')
    return Markup("".join(parts))


def el(tag: str, *children: Any, **attributes: Any) -> Markup:
    """``el("td", "text", class_="x", data_step="A1")`` -> ``<td class="x" data-step="A1">text</td>``."""
    if tag in VOID_TAGS:
        return Markup(f"<{tag}{attrs(attributes)}>")
    return Markup(f"<{tag}{attrs(attributes)}>{join(*children)}</{tag}>")


def raw(text: str) -> Markup:
    """Trusted constant HTML from this package (never user or letter content)."""
    return Markup(text)


def url_part(value: Any) -> str:
    """One path segment (case id, finding id) for an href."""
    return quote(str(value), safe="")


def json_text(value: Any, limit: int | None = 400) -> str:
    """Compact JSON for before/after values in tables (escaped later like any text)."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if limit is not None and len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def table(headers: list[Any], rows: list[list[Any]], *, empty: str = "None.", row_attrs: list[dict[str, Any]] | None
          = None, **attributes: Any) -> Markup:
    """A table with a header row; ``row_attrs`` gives per-row attributes (class, data-*).

    The table sits in a ``div.table-wrap`` that scrolls sideways when the table is wider than its panel, so a wide
    table never widens the page or runs across the panel border."""
    if not rows:
        return el("p", empty, class_="empty")
    head = el("thead", el("tr", [el("th", h, scope="col") for h in headers]))
    body_rows = []
    for index, row in enumerate(rows):
        extra = (row_attrs[index] if row_attrs and index < len(row_attrs) else {}) or {}
        body_rows.append(el("tr", [c if isinstance(c, Markup) and c.startswith("<td") else el("td", c) for c in row],
                            **extra))
    return el("div", el("table", head, el("tbody", body_rows), **attributes), class_="table-wrap")


def dl(items: list[tuple[Any, Any]], **attributes: Any) -> Markup:
    """Definition list of (label, value) pairs; empty values are shown as 'unknown'."""
    out = []
    for label, value in items:
        out.append(el("dt", label))
        out.append(el("dd", value if value not in (None, "", []) else el("span", "unknown", class_="unknown")))
    return el("dl", out, **attributes)
