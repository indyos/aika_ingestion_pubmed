"""Strutture di dominio PubMed/PMC.

Il front matter (contratto verso il chunker) e gli enum vivono in ``aika_ingestion_common.models``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AbstractSection:
    """Una sezione di abstract; ``label`` è None per gli abstract non strutturati."""

    label: str | None
    text: str


@dataclass(frozen=True)
class PubMedRecord:
    """Metadati estratti da un ``PubmedArticle``."""

    pmid: str
    title: str
    authors: list[str] = field(default_factory=lambda: [])
    journal: str | None = None
    published: str | None = None
    languages: list[str] = field(default_factory=lambda: [])
    mesh_terms: list[str] = field(default_factory=lambda: [])
    publication_types: list[str] = field(default_factory=lambda: [])
    doi: str | None = None
    pmcid: str | None = None
    abstract: list[AbstractSection] = field(default_factory=lambda: [])
    retracted: bool = False


@dataclass(frozen=True)
class PmcArticle:
    """Articolo PMC (JATS) recuperato via ``efetch db=pmc``."""

    pmcid: str
    xml: bytes
    license_raw: str | None
    has_body: bool
