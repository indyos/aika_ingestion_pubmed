"""Serializzazione deterministica dei documenti Markdown con front matter YAML."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import yaml

from .models import FrontMatter

_FENCE = "---\n"


def content_hash(body: str) -> str:
    """``sha256:`` del solo corpo (il front matter — ``retrieved_at`` incluso — non conta)."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_document(front_matter: FrontMatter, body: str) -> str:
    """File completo: chiavi in ordine fisso, opzionali vuote omesse, LF, UTF-8."""
    data = front_matter.to_ordered_dict()
    yaml_text = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=1_000_000,
    )
    return f"{_FENCE}{yaml_text}{_FENCE}\n{body}"


@dataclass(frozen=True)
class ParsedDocument:
    meta: dict[str, Any]
    body: str

    @property
    def curation_status(self) -> str | None:
        value = self.meta.get("curation_status")
        return str(value) if value is not None else None

    @property
    def content_hash(self) -> str | None:
        value = self.meta.get("content_hash")
        return str(value) if value is not None else None

    @property
    def review_notes(self) -> str | None:
        value = self.meta.get("review_notes")
        return str(value) if value else None


def parse_document(text: str) -> ParsedDocument:
    """Separa front matter e corpo. Il front matter è letto come stringhe (nessuna coercizione).

    Tollera i fine riga CRLF (file modificati su Windows): il testo viene normalizzato a LF.
    """
    text = text.replace("\r\n", "\n")
    if not text.startswith(_FENCE):
        raise ValueError("front matter mancante")
    end = text.find(f"\n{_FENCE}", len(_FENCE) - 1)
    if end == -1:
        raise ValueError("front matter non chiuso")
    raw = text[len(_FENCE) : end + 1]
    body = text[end + 1 + len(_FENCE) :]
    body = body.removeprefix("\n")
    meta: Any = yaml.load(raw, Loader=yaml.BaseLoader)
    if not isinstance(meta, dict):
        raise ValueError("front matter non è una mappa")
    return ParsedDocument(meta=meta, body=body)  # pyright: ignore[reportUnknownArgumentType]
