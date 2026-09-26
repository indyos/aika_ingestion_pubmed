"""Configurazione (``config.yaml``). I segreti arrivano solo da variabili d'ambiente."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import DocType, Modality


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


class QualityConfig(_Strict):
    min_body_chars: int = Field(default=1500, ge=0)
    allowed_languages: list[str] = Field(default_factory=lambda: ["en", "it"])
    allowed_licenses: list[str] = Field(
        default_factory=lambda: [
            "CC0-1.0",
            "CC-BY-4.0",
            "CC-BY-3.0",
            "CC-BY-SA-4.0",
            "CC-BY-SA-3.0",
        ]
    )


class CorpusConfig(_Strict):
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    local_path: Path
    remote: str = "origin"
    base_branch: str = "main"
    corpus_dir: str = "corpus"  # solo documenti approvati (dopo la revisione in PR)
    rejected_dir: str = "rejected"  # documenti rifiutati: memoria per non riproporli
    max_pr_size: int = Field(default=20, ge=1)
    branch_prefix: str = "ingest/"
    git_user_name: str = "aika-ingestion-bot"
    git_user_email: str = "aika-ingestion-bot@users.noreply.github.com"


class OriginalsConfig(_Strict):
    """Dove salvare gli XML originali (v1: directory locale)."""

    dir: Path
    # Prefisso dell'URI scritto in `original_uri` (es. gs://bucket/pubmed). Se assente si usa
    # l'URI file:// della directory locale.
    base_uri: str | None = None


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


class Secrets(_Strict):
    """Segreti letti dall'ambiente; mai dal file di configurazione."""

    ncbi_api_key: str | None = None
    github_app_id: str | None = None
    github_installation_id: str | None = None
    github_private_key: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Secrets:
        e = os.environ if env is None else env
        key = e.get("GITHUB_APP_PRIVATE_KEY")
        key_path = e.get("GITHUB_APP_PRIVATE_KEY_PATH")
        if key is None and key_path:
            key = Path(key_path).read_text(encoding="utf-8")
        return cls(
            ncbi_api_key=e.get("NCBI_API_KEY") or None,
            github_app_id=e.get("GITHUB_APP_ID") or None,
            github_installation_id=e.get("GITHUB_APP_INSTALLATION_ID") or None,
            github_private_key=key or None,
        )

    @property
    def has_github_app(self) -> bool:
        return bool(self.github_app_id and self.github_installation_id and self.github_private_key)
