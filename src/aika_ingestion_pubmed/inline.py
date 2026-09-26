"""Rendering inline XML (JATS / PubMed) → Markdown.

Regole: corsivo/grassetto preservati, apici/pedici come ``<sup>``/``<sub>`` (così ``10<sup>9</sup>/L``
non diventa ``109/L``), richiami bibliografici rimossi, testo normalizzato (spazi, NFC).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from typing import Final, cast

from lxml import etree

from .xmlutil import Element

# Segnaposto per i richiami rimossi: serve a ripulire parentesi/virgole orfane.
_GONE: Final = ""

_ITALIC: Final = frozenset({"italic", "i", "em"})
_BOLD: Final = frozenset({"bold", "b", "strong"})
_TRANSPARENT: Final = frozenset(
    {
        "underline",
        "u",
        "sc",
        "monospace",
        "roman",
        "sans-serif",
        "strike",
        "overline",
        "named-content",
        "styled-content",
        "email",
        "abbrev",
        "x",
        "def",
        "term",
        "target",
        "milestone-start",
        "milestone-end",
    }
)
_DROP: Final = frozenset(
    {
        "inline-graphic",
        "inline-supplementary-material",
        "supplementary-material",
        "fn",
        "label",
        "alt-text",
        "long-desc",
        "object-id",
    }
)
# Elementi "a blocco" che in un contesto inline (celle, didascalie) vanno separati da spazio.
_BLOCKISH: Final = frozenset(
    {"p", "list", "list-item", "break", "def-item", "sec", "title", "caption", "td", "th", "tr"}
)
# Tipi di xref eliminati (richiami bibliografici, note, affiliazioni). Gli altri (table, fig,
# sec, ...) restano come testo: «see Table 2» non deve diventare «see».
_XREF_REMOVED: Final = frozenset({"bibr", "aff", "corresp", "author-notes", "fn"})

_WS: Final = re.compile(r"\s+")
_ESCAPE: Final = re.compile(r"([\\*_`])")
_LT: Final = re.compile(r"<(?=[A-Za-z/!?])")
_GONE_SEPARATORS: Final = re.compile(rf"{_GONE}(?:\s*[,;–—-]?\s*{_GONE})+")
_ONLY_GONE: Final = re.compile(rf"^[\s,;–—-]*{_GONE}[\s,;–—{_GONE}-]*$")
_EMPTY_BRACKETS: Final = re.compile(rf"\s*[(\[]\s*{_GONE}\s*[)\]]")
_LEAD_SEP: Final = re.compile(rf"([(\[])\s*{_GONE}\s*[,;]\s*")
_TRAIL_SEP: Final = re.compile(rf"[,;]\s*{_GONE}\s*([)\]])")
_BLOCK_START: Final = re.compile(r"^(#{1,6}(?=\s)|[>+\-](?=\s)|:::|---)")
_ORDERED_START: Final = re.compile(r"^(\d+)([.)])(?=\s)")


def local_name(el: Element) -> str:
    """Nome locale del tag (senza namespace); stringa vuota per commenti/PI/entità."""
    tag = el.tag
    # A runtime `tag` è una funzione per commenti/PI/entità; gli stub la dichiarano sempre str.
    if not isinstance(tag, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        return ""
    return etree.QName(tag).localname


def text_of(el: Element) -> str:
    """Tutto il testo discendente di ``el`` (``itertext`` non è tipizzato a ``str`` negli stub)."""
    return "".join(cast(Iterator[str], el.itertext()))


def escape_text(text: str) -> str:
    text = _ESCAPE.sub(r"\\\1", text)
    return _LT.sub("&lt;", text)


def plain_text(el: Element) -> str:
    """Testo puro normalizzato (per titoli, etichette)."""
    return normalize(_WS.sub(" ", text_of(el)).strip())


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def finalize(text: str) -> str:
    """Ripulisce il testo inline: richiami rimossi, spazi collassati, NFC."""
    text = _WS.sub(" ", text)
    text = _GONE_SEPARATORS.sub(_GONE, text)
    text = _EMPTY_BRACKETS.sub("", text)
    text = _LEAD_SEP.sub(r"\1", text)
    text = _TRAIL_SEP.sub(r"\1", text)
    text = re.sub(rf"\s*{_GONE}", "", text)
    return normalize(_WS.sub(" ", text).strip())


def protect_block_start(text: str) -> str:
    """Evita che un paragrafo venga letto come titolo/lista/citazione/fenced div."""
    text = _ORDERED_START.sub(lambda m: m.group(1) + "\\" + m.group(2), text, count=1)
    return _BLOCK_START.sub(lambda m: "\\" + m.group(0), text, count=1)


def render_inline(el: Element) -> str:
    """Contenuto inline di ``el`` (testo + figli) come Markdown grezzo, non ancora ``finalize``-d."""
    parts: list[str] = []
    if el.text:
        parts.append(escape_text(el.text))
    for child in el:
        parts.append(render_child(child))
        if child.tail:
            parts.append(escape_text(child.tail))
    return "".join(parts)


def render_child(el: Element) -> str:
    name = local_name(el)
    if not name or name in _DROP:
        return ""
    if name in _ITALIC:
        return _wrap(render_inline(el), "*")
    if name in _BOLD:
        return _wrap(render_inline(el), "**")
    if name in ("sup", "sub"):
        inner = render_inline(el)
        if not inner.strip() or _ONLY_GONE.match(inner):
            return _GONE if _GONE in inner else ""
        return f"<{name}>{inner.strip()}</{name}>"
    if name == "xref":
        if el.get("ref-type") in _XREF_REMOVED:
            return _GONE
        return render_inline(el)
    if name in ("ext-link", "uri"):
        return _link(el)
    if name in ("inline-formula", "disp-formula"):
        return _formula(el)
    if name == "math":
        return escape_text(_WS.sub(" ", text_of(el)).strip())
    if name == "alternatives":
        return _alternatives(el)
    if name == "break":
        return " "
    inner = render_inline(el)
    if name in _BLOCKISH:
        return f" {inner} "
    if name in _TRANSPARENT:
        return inner
    return inner


def _wrap(inner: str, marker: str) -> str:
    core = inner.strip()
    if not core:
        return inner
    lead = inner[: len(inner) - len(inner.lstrip())]
    trail = inner[len(inner.rstrip()) :]
    return f"{lead}{marker}{core}{marker}{trail}"


def _link(el: Element) -> str:
    text = render_inline(el)
    href = el.get("{http://www.w3.org/1999/xlink}href", "")
    plain = _WS.sub(" ", text_of(el)).strip()
    if href.startswith(("http://", "https://")) and plain and plain != href:
        return f"[{text.strip()}]({href})"
    return text


def _alternatives(el: Element) -> str:
    """``<alternatives>`` = stesso contenuto in più forme: se ne rende una sola."""
    names = [local_name(c) for c in el]
    if "tex-math" in names:
        return _formula(el)
    for child in el:
        if local_name(child) not in ("graphic", "media", "inline-graphic", ""):
            return render_child(child)
    return ""


def _formula(el: Element) -> str:
    """Formula: LaTeX se presente, altrimenti il testo MathML appiattito."""
    display = local_name(el) == "disp-formula"
    for child in el.iter():
        if local_name(child) == "tex-math":
            tex = _WS.sub(" ", text_of(child)).strip()
            tex = re.sub(r"^\\\[|\\\]$|^\$+|\$+$", "", tex).strip()
            if tex:
                return f"$${tex}$$" if display else f"${tex}$"
    return escape_text(_WS.sub(" ", text_of(el)).strip())
