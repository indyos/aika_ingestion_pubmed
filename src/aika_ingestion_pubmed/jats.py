"""``JatsToMarkdown``: JATS XML (PMC) → Markdown secondo il contratto verso il chunker.

Contratto del corpo:

* un solo ``# <titolo>``; livelli dei titoli senza salti;
* ``## Abstract`` sempre presente (sottosezioni ``###`` se strutturato);
* sezioni del ``<body>`` in ordine, con i titoli originali;
* **un paragrafo per riga**, nessun a capo a larghezza fissa;
* tabelle e figure come fenced div (``::: table id=…`` / ``::: figure id=…``), sempre intere;
* escluse bibliografia, affiliazioni, finanziamenti, conflitti, ringraziamenti, supplementari;
* serializzazione deterministica: stesso XML → stessa stringa, byte per byte.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from .inline import (
    escape_text,
    finalize,
    local_name,
    normalize,
    plain_text,
    protect_block_start,
    render_child,
    render_inline,
    text_of,
)
from .models import AbstractSection
from .xmlutil import Element

# Elementi mai convertiti (metadati, note editoriali, materiale escluso dal contratto).
_SKIP: Final = frozenset(
    {
        "title",
        "label",
        "supplementary-material",
        "ack",
        "ref-list",
        "ref",
        "fn-group",
        "fn",
        "aff",
        "contrib-group",
        "kwd-group",
        "permissions",
        "graphic",
        "media",
        "object-id",
        "alt-text",
        "long-desc",
        "attrib",
    }
)
# Figli di <p> resi come blocchi a sé (la parte inline del paragrafo viene spezzata attorno).
_BLOCK_IN_P: Final = frozenset(
    {
        "table-wrap",
        "table-wrap-group",
        "fig",
        "fig-group",
        "list",
        "def-list",
        "disp-formula",
        "boxed-text",
        "disp-quote",
        "preformat",
        "code",
        "statement",
    }
)
_MAIN_ABSTRACT_TYPES: Final = frozenset({"abstract", "main", "structured"})
_EXCLUDED_SEC_TYPES: Final = frozenset(
    {
        "supplementary-material",
        "coi-statement",
        "conflict",
        "conflicts",
        "funding",
        "funding-information",
        "financial-disclosure",
        "acknowledgments",
        "acknowledgements",
        "ack",
        "references",
    }
)
_EXCLUDED_SEC_TITLE: Final = re.compile(
    r"(?:\d+[.)]?\s*)?(?:"
    r"funding|financial support|sources? of funding|disclosures?|"
    r"conflicts? of interests?|competing interests?|"
    r"declarations? of (?:competing|conflicting) interests?|"
    r"acknowledge?ments?|supplementary (?:materials?|information|data)|"
    r"supporting information|references|bibliography|"
    r"finanziamenti?|conflitti? di interessi?|ringraziamenti|"
    r"materiale supplementare|bibliografia"
    r")[\s.:]*",
    re.IGNORECASE,
)
_ORDERED: Final = frozenset({"order", "alpha-lower", "alpha-upper", "roman-lower", "roman-upper"})
_FALLBACK_HEADING: Final = "Main text"

type _PartKind = Literal["text", "list", "block"]


@dataclass(frozen=True)
class _Cell:
    text: str
    colspan: int
    rowspan: int


class JatsToMarkdown:
    """Convertitore stateless: ogni chiamata a :meth:`convert` è indipendente."""

    def convert(
        self,
        article: Element,
        *,
        title: str,
        fallback_abstract: Sequence[AbstractSection] = (),
    ) -> str:
        return _Converter().convert(article, title, fallback_abstract)

    @staticmethod
    def extract_title(article: Element) -> str | None:
        el = article.find("front/article-meta/title-group/article-title")
        if el is None:
            return None
        return plain_text(el) or None


class _Converter:
    def __init__(self) -> None:
        self._tables = 0
        self._figures = 0

    # ------------------------------------------------------------------ documento

    def convert(
        self, article: Element, title: str, fallback_abstract: Sequence[AbstractSection]
    ) -> str:
        blocks = [f"# {escape_text(title)}", "## Abstract"]
        blocks += self._abstract(article, fallback_abstract)
        body = article.find("body")
        body_blocks = self._container(body, 2) if body is not None else []
        if body_blocks and not body_blocks[0].startswith("## "):
            # Contenuto senza titolo subito dopo l'abstract: senza un titolo finirebbe
            # sotto «## Abstract».
            body_blocks.insert(0, f"## {_FALLBACK_HEADING}")
        blocks += body_blocks
        return normalize("\n\n".join(b for b in blocks if b) + "\n")

    def _abstract(self, article: Element, fallback: Sequence[AbstractSection]) -> list[str]:
        for ab in article.iterfind("front/article-meta/abstract"):
            kind = (ab.get("abstract-type") or "abstract").casefold()
            if kind not in _MAIN_ABSTRACT_TYPES:
                continue  # graphical, toc, teaser, key-points…
            blocks = self._container(ab, 3)
            if blocks:
                return blocks
        blocks: list[str] = []
        for section in fallback:
            if section.label:
                blocks.append(f"### {escape_text(section.label)}")
            blocks.append(protect_block_start(section.text))
        return blocks

    # ------------------------------------------------------------------ blocchi

    def _container(self, el: Element, level: int) -> list[str]:
        blocks: list[str] = []
        for child in el:
            blocks += self._block(child, level)
        return blocks

    def _block(self, el: Element, level: int) -> list[str]:
        name = local_name(el)
        if not name or name in _SKIP:
            return []
        if name == "sec":
            return self._sec(el, level)
        if name == "p":
            return [text for _, text in self._p_parts(el, protect=True)]
        if name == "list":
            return _non_empty([self._list(el)])
        if name == "def-list":
            return _non_empty([self._def_list(el)])
        if name == "table-wrap":
            return self._table_wrap(el)
        if name == "table":
            return _non_empty([_table_markdown(el)])
        if name == "fig":
            return self._figure(el)
        if name == "disp-formula":
            return _non_empty([finalize(render_child(el))])
        if name == "boxed-text":
            return self._boxed(el, level)
        if name == "disp-quote":
            return self._quote(el)
        if name in ("preformat", "code"):
            return _non_empty([_code_block(el)])
        if name == "statement":
            return self._statement(el, level)
        # table-wrap-group, fig-group, alternatives, app, notes… → si scende nei figli.
        return self._container(el, level)

    def _sec(self, sec: Element, level: int) -> list[str]:
        if _is_excluded_sec(sec):
            return []
        blocks: list[str] = []
        child_level = level
        title = sec.find("title")
        if title is not None:
            text = plain_text(title)
            if text:
                blocks.append(f"{'#' * min(level, 6)} {escape_text(text)}")
                child_level = level + 1
        return blocks + self._container(sec, child_level)

    # ------------------------------------------------------------------ paragrafi

    def _p_parts(self, p: Element, *, protect: bool) -> list[tuple[_PartKind, str]]:
        """Spezza un <p> in testo inline e blocchi annidati (tabelle, liste, figure…)."""
        parts: list[tuple[_PartKind, str]] = []
        buf: list[str] = []

        def flush() -> None:
            text = finalize("".join(buf))
            buf.clear()
            if text:
                parts.append(("text", protect_block_start(text) if protect else text))

        if p.text:
            buf.append(escape_text(p.text))
        for child in p:
            name = local_name(child)
            if name in _BLOCK_IN_P:
                flush()
                if name == "list":
                    text = self._list(child)
                    if text:
                        parts.append(("list", text))
                else:
                    parts.extend(("block", b) for b in self._block(child, 2))
            else:
                buf.append(render_child(child))
            if child.tail:
                buf.append(escape_text(child.tail))
        flush()
        return parts

    # ------------------------------------------------------------------ liste

    def _list(self, lst: Element) -> str:
        ordered = (lst.get("list-type") or "").casefold() in _ORDERED
        lines: list[str] = []
        number = 0
        for item in lst:
            if local_name(item) != "list-item":
                continue
            parts = self._item_parts(item)
            if not parts:
                continue
            number += 1
            marker = f"{number}. " if ordered else "- "
            pad = " " * len(marker)
            first = True
            for kind, text in parts:
                if kind == "text":
                    if first:
                        lines.append(marker + text)
                    else:
                        lines += ["", pad + text]
                else:
                    if first:
                        lines.append(marker.rstrip())
                    elif kind == "block":
                        lines.append("")
                    lines += [pad + ln if ln else ln for ln in text.split("\n")]
                first = False
        return "\n".join(lines)

    def _item_parts(self, item: Element) -> list[tuple[_PartKind, str]]:
        parts: list[tuple[_PartKind, str]] = []
        loose = finalize(escape_text(item.text or ""))
        if loose:
            parts.append(("text", loose))
        for child in item:
            name = local_name(child)
            if name == "p":
                parts += self._p_parts(child, protect=False)
            elif name == "list":
                text = self._list(child)
                if text:
                    parts.append(("list", text))
            elif name and name not in _SKIP:
                parts.extend(("block", b) for b in self._block(child, 2))
            tail = finalize(escape_text(child.tail or ""))
            if tail:
                parts.append(("text", tail))
        return parts

    def _def_list(self, dl: Element) -> str:
        lines: list[str] = []
        for item in dl.iter("def-item"):
            term = item.find("term")
            definition = item.find("def")
            t = finalize(render_inline(term)) if term is not None else ""
            d = finalize(render_inline(definition)) if definition is not None else ""
            if t and d:
                lines.append(f"- **{t}**: {d}")
            elif t or d:
                lines.append(f"- {t or d}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ box, citazioni

    def _boxed(self, box: Element, level: int) -> list[str]:
        blocks: list[str] = []
        title = box.find("title")
        if title is None:
            title = box.find("caption/title")
        if title is not None and plain_text(title):
            blocks.append(f"**{finalize(render_inline(title))}**")
        return blocks + self._container(box, level)

    def _quote(self, q: Element) -> list[str]:
        lines = [
            text
            for child in q
            if local_name(child) == "p"
            for _, text in self._p_parts(child, protect=False)
        ]
        return _non_empty(["\n>\n".join(f"> {ln}" for ln in lines)])

    def _statement(self, st: Element, level: int) -> list[str]:
        head = _join(plain_text(e) for e in (st.find("label"), st.find("title")) if e is not None)
        blocks = [f"**{escape_text(head)}**"] if head else []
        return blocks + self._container(st, level)

    # ------------------------------------------------------------------ didascalie

    def _caption(self, el: Element) -> list[str]:
        """Didascalia: prima riga in grassetto con label, poi eventuali paragrafi."""
        label_el = el.find("label")
        label = plain_text(label_el).rstrip(".:") if label_el is not None else ""
        caption = el.find("caption")
        title = ""
        paragraphs: list[str] = []
        if caption is not None:
            title_el = caption.find("title")
            if title_el is not None:
                title = finalize(render_inline(title_el))
            paragraphs = [t for p in caption.iterfind("p") if (t := finalize(render_inline(p)))]
            if not title and paragraphs:
                title = paragraphs.pop(0)
        head = _join([f"{label}." if label else "", title])
        lines = [f"**{head}**"] if head else []
        return lines + [protect_block_start(p) for p in paragraphs]

    def _figure(self, fig: Element) -> list[str]:
        self._figures += 1
        parts = self._caption(fig)
        if not parts:
            return []
        fid = _div_id(fig.get("id"), f"f{self._figures}")
        return [_div("figure", fid, parts)]

    # ------------------------------------------------------------------ tabelle

    def _table_wrap(self, tw: Element) -> list[str]:
        self._tables += 1
        tid = _div_id(tw.get("id"), f"t{self._tables}")
        parts = self._caption(tw)
        table = next((e for e in tw.iter() if local_name(e) == "table"), None)
        if table is not None:
            md = _table_markdown(table)
            if md:
                parts.append(md)
        parts += self._table_foot(tw)
        if not parts:
            return []
        return [_div("table", tid, parts)]

    def _table_foot(self, tw: Element) -> list[str]:
        notes: list[str] = []
        for foot in tw.iterfind("table-wrap-foot"):
            for child in foot.iter():
                name = local_name(child)
                if name == "fn":
                    label_el = child.find("label")
                    label = plain_text(label_el) if label_el is not None else ""
                    text = _join(finalize(render_inline(p)) for p in child.iterfind("p"))
                    if text:
                        notes.append(f"<sup>{label}</sup> {text}" if label else text)
                elif name == "p" and not _inside(child, "fn"):
                    text = finalize(render_inline(child))
                    if text:
                        notes.append(protect_block_start(text))
        return notes


# ---------------------------------------------------------------------- helper


def _non_empty(blocks: Sequence[str]) -> list[str]:
    return [b for b in blocks if b]


def _join(parts: Iterable[str]) -> str:
    return " ".join(p for p in parts if p)


def _div(kind: str, ident: str, parts: Sequence[str]) -> str:
    return f"::: {kind} id={ident}\n\n" + "\n\n".join(parts) + "\n\n:::"


def _div_id(raw: str | None, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw.strip()) if raw and raw.strip() else fallback


def _inside(el: Element, ancestor: str) -> bool:
    parent = el.getparent()
    while parent is not None:
        if local_name(parent) == ancestor:
            return True
        parent = parent.getparent()
    return False


def _is_excluded_sec(sec: Element) -> bool:
    if (sec.get("sec-type") or "").casefold() in _EXCLUDED_SEC_TYPES:
        return True
    title = sec.find("title")
    return title is not None and _EXCLUDED_SEC_TITLE.fullmatch(plain_text(title)) is not None


def _code_block(el: Element) -> str:
    text = text_of(el).strip("\n")
    return f"```\n{text}\n```" if text.strip() else ""


def _int_attr(el: Element, name: str) -> int:
    try:
        return max(1, int(el.get(name) or 1))
    except ValueError:
        return 1


def _table_markdown(table: Element) -> str:
    """Tabella JATS → Markdown, con celle unite espanse (il valore è ripetuto in ogni cella)."""
    header_rows: list[list[_Cell]] = []
    body_rows: list[list[_Cell]] = []
    has_thead = False
    for child in table:
        name = local_name(child)
        if name == "thead":
            has_thead = True
            header_rows += [_cells(tr) for tr in child if local_name(tr) == "tr"]
        elif name in ("tbody", "tfoot"):
            body_rows += [_cells(tr) for tr in child if local_name(tr) == "tr"]
        elif name == "tr":
            body_rows.append(_cells(child))
    if not has_thead and body_rows:
        header_rows, body_rows = body_rows[:1], body_rows[1:]
    grid = _expand(header_rows + body_rows)
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    n_head = len(header_rows)
    header = [_merge_header([grid[r][c] for r in range(n_head)]) for c in range(width)]
    lines = [_row(header), _row(["---"] * width)]
    lines += [_row(r) for r in grid[n_head:]]
    return "\n".join(lines)


def _cells(tr: Element) -> list[_Cell]:
    cells: list[_Cell] = []
    for td in tr:
        if local_name(td) not in ("td", "th"):
            continue
        text = finalize(render_inline(td)).replace("|", "\\|")
        cells.append(_Cell(text, _int_attr(td, "colspan"), _int_attr(td, "rowspan")))
    return cells


def _expand(rows: Sequence[Sequence[_Cell]]) -> list[list[str]]:
    """Espande colspan/rowspan in una griglia rettangolare di testi."""
    grid: list[list[str]] = []
    pending: dict[int, tuple[int, str]] = {}  # colonna -> (righe ancora da coprire, testo)
    for cells in rows:
        row: list[str] = []

        def drain(col: int, row: list[str] = row) -> int:
            while col in pending:
                remaining, text = pending.pop(col)
                row.append(text)
                if remaining > 1:
                    pending[col] = (remaining - 1, text)
                col += 1
            return col

        col = 0
        for cell in cells:
            col = drain(col)
            for _ in range(cell.colspan):
                row.append(cell.text)
                if cell.rowspan > 1:
                    pending[col] = (cell.rowspan - 1, cell.text)
                col += 1
        while any(c >= col for c in pending):
            if col in pending:
                col = drain(col)
            else:
                row.append("")
                col += 1
        grid.append(row)
    return grid


def _merge_header(parts: Sequence[str]) -> str:
    merged: list[str] = []
    for part in parts:
        if part and (not merged or merged[-1] != part):
            merged.append(part)
    return " / ".join(merged)


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"
