"""Configurazione (``config.yaml``). I segreti arrivano solo da variabili d'ambiente."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from aika_ingestion_common.config import (
    CorpusConfig,
    GitHubSecrets,
    OriginalsConfig,
    QualityConfig,
)
from aika_ingestion_common.models import DocType, Modality
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NcbiConfig(_Strict):
    tool: str = "aika_ingestion_pubmed"
    email: str = Field(min_length=3, pattern=r"^[^@\s]+@[^@\s]+$")
    batch_size: int = Field(default=100, ge=1, le=500)
    timeout_s: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=5, ge=0, le=10)


class SearchConfig(_Strict):
    query: str = Field(min_length=1)
    mindate: str | None = Field(default=None, pattern=r"^[0-9]{4}(/[0-9]{2}(/[0-9]{2})?)?$")
    maxdate: str | None = Field(default=None, pattern=r"^[0-9]{4}(/[0-9]{2}(/[0-9]{2})?)?$")
    max_results: int = Field(default=200, ge=1, le=10_000)
    sort: str = "pub_date"
    # Filtro PubMed aggiuntivo in AND con la query (es. filtro Open Access): decisione aperta,
    # da verificare prima di attivarlo.
    extra_filter: str | None = None

    @model_validator(mode="after")
    def _dates_together(self) -> SearchConfig:
        if (self.mindate is None) != (self.maxdate is None):
            raise ValueError("`mindate` e `maxdate` vanno indicati insieme (richiesto da esearch)")
        return self


class PublicationTypeRule(_Strict):
    publication_type: str
    doc_type: DocType


class MappingConfig(_Strict):
    # Ordine = priorità: vince la prima regola che trova un publication type corrispondente.
    publication_type_doc_type: list[PublicationTypeRule]
    evidence_tier: dict[DocType, int]
    # MeSH descriptor -> modality
    mesh_modality: dict[str, Modality] = Field(default_factory=dict)
    # Se compare uno di questi descrittori, `ckd_non_dialysis` viene scartata.
    dialysis_mesh_terms: list[str] = Field(default_factory=list)
    # Vocabolario controllato dei topic: topic -> descrittori MeSH che lo attivano.
    topics: dict[str, list[str]] = Field(default_factory=dict)
    population_pediatric_mesh: list[str] = Field(default_factory=list)
    population_adult_mesh: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tiers_complete(self) -> MappingConfig:
        missing = [d.value for d in DocType if d not in self.evidence_tier]
        if missing:
            raise ValueError(f"evidence_tier mancante per doc_type: {', '.join(missing)}")
        bad = {d.value: t for d, t in self.evidence_tier.items() if not 1 <= t <= 5}
        if bad:
            raise ValueError(f"evidence_tier fuori intervallo 1-5: {bad}")
        return self


class Config(_Strict):
    ncbi: NcbiConfig
    search: SearchConfig
    mapping: MappingConfig
    quality: QualityConfig = Field(default_factory=QualityConfig)
    corpus: CorpusConfig
    originals: OriginalsConfig


def load_config(path: Path) -> Config:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Config.model_validate(raw)


class Secrets(GitHubSecrets):
    """Segreti generici della GitHub App più la chiave NCBI; mai dal file di configurazione."""

    ncbi_api_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Secrets:
        e = os.environ if env is None else env
        base = GitHubSecrets.from_env(e)
        return cls(**base.model_dump(), ncbi_api_key=e.get("NCBI_API_KEY") or None)
