"""Modello del front matter (contratto verso il chunker) e strutture di dominio."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

SCHEMA_VERSION = 1


class DocType(StrEnum):
    GUIDELINE = "guideline"
    SYSTEMATIC_REVIEW = "systematic_review"
    META_ANALYSIS = "meta_analysis"
    RCT = "rct"
    OBSERVATIONAL = "observational"
    NARRATIVE_REVIEW = "narrative_review"
    CONSENSUS = "consensus"
    PATIENT_EDUCATION = "patient_education"
    OTHER = "other"


class ClassificationMethod(StrEnum):
    SOURCE = "source"
    RULE = "rule"
    LLM = "llm"
    MANUAL = "manual"


class Modality(StrEnum):
    HEMODIALYSIS = "hemodialysis"
    PERITONEAL_DIALYSIS = "peritoneal_dialysis"
    CKD_NON_DIALYSIS = "ckd_non_dialysis"
    TRANSPLANT = "transplant"
    UNSPECIFIED = "unspecified"


class Population(StrEnum):
    ADULT = "adult"
    PEDIATRIC = "pediatric"
    MIXED = "mixed"
    UNSPECIFIED = "unspecified"


class Audience(StrEnum):
    CLINICIAN = "clinician"
    PATIENT = "patient"


class CurationStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class FrontMatter(BaseModel):
    """Front matter YAML di ``corpus/pmid-<PMID>.md``.

    L'ordine di definizione dei campi è l'ordine **fisso** delle chiavi nel file.
    Le chiavi opzionali vuote vengono omesse in serializzazione.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    id: str = Field(pattern=r"^pmid-[0-9]+$")
    title: NonEmptyStr
    source_type: Literal["pmc_fulltext"]
    source_uri: NonEmptyStr
    original_uri: NonEmptyStr
    pmid: str | None = Field(default=None, pattern=r"^[0-9]+$")
    pmcid: str | None = Field(default=None, pattern=r"^PMC[0-9]+$")
    doi: NonEmptyStr | None = None
    authors: list[NonEmptyStr] | None = None
    journal: NonEmptyStr | None = None
    published: str | None = Field(default=None, pattern=r"^[0-9]{4}(-[0-9]{2}(-[0-9]{2})?)?$")
    language: str = Field(pattern=r"^[a-z]{2}$")
    doc_type: DocType
    evidence_tier: int = Field(ge=1, le=5)
    classification_method: ClassificationMethod
    modality: list[Modality] = Field(min_length=1)
    population: Population
    audience: Audience
    topics: list[NonEmptyStr] | None = None
    mesh_terms: list[NonEmptyStr] | None = None
    license: NonEmptyStr
    retracted: bool
    curation_status: CurationStatus
    review_notes: NonEmptyStr | None = None
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    retrieved_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")

    @model_validator(mode="after")
    def _id_matches_pmid(self) -> FrontMatter:
        if self.pmid is not None and self.id != f"pmid-{self.pmid}":
            raise ValueError("`id` deve essere `pmid-<PMID>` coerente con `pmid`")
        return self

    def to_ordered_dict(self) -> dict[str, Any]:
        """Dizionario JSON-compatibile, chiavi in ordine fisso, opzionali vuote omesse."""
        raw = self.model_dump(mode="json", exclude_none=True)
        return {k: v for k, v in raw.items() if not (isinstance(v, list) and not v)}


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
