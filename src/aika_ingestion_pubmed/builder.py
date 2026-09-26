"""``FrontMatterBuilder``: regole di derivazione (MeSH → modality/topics, pt → doc_type)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from .config import MappingConfig
from .document import content_hash
from .licenses import normalize_license
from .models import (
    SCHEMA_VERSION,
    Audience,
    ClassificationMethod,
    CurationStatus,
    DocType,
    Modality,
    Population,
    PubMedRecord,
)

# ISO 639-2 (PubMed) → ISO 639-1.
_LANG_639_1: Final = {
    "eng": "en",
    "ita": "it",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "por": "pt",
    "dut": "nl",
    "nld": "nl",
    "rus": "ru",
    "pol": "pl",
    "tur": "tr",
    "jpn": "ja",
    "chi": "zh",
    "zho": "zh",
    "kor": "ko",
    "ara": "ar",
    "swe": "sv",
    "dan": "da",
    "nor": "no",
    "fin": "fi",
    "cze": "cs",
    "ces": "cs",
    "hun": "hu",
    "gre": "el",
    "ell": "el",
    "heb": "he",
}


def pmc_url(pmcid: str) -> str:
    return f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"


def pubmed_url(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


class FrontMatterBuilder:
    def __init__(self, mapping: MappingConfig) -> None:
        self._m = mapping
        self._modality = {k.casefold(): v for k, v in mapping.mesh_modality.items()}
        self._dialysis = {t.casefold() for t in mapping.dialysis_mesh_terms}
        self._pediatric = {t.casefold() for t in mapping.population_pediatric_mesh}
        self._adult = {t.casefold() for t in mapping.population_adult_mesh}

    def build(
        self,
        record: PubMedRecord,
        *,
        pmcid: str,
        title: str,
        body: str,
        license_raw: str | None,
        original_uri: str,
        retrieved_at: datetime,
        review_notes: str | None = None,
    ) -> dict[str, object]:
        """Bozza del front matter, da validare con :class:`QualityGate`."""
        doc_type, method = self.doc_type(record.publication_types)
        mesh = list(dict.fromkeys(record.mesh_terms))
        draft: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "id": f"pmid-{record.pmid}",
            "title": title,
            "source_type": "pmc_fulltext",
            "source_uri": pmc_url(pmcid),
            "original_uri": original_uri,
            "pmid": record.pmid,
            "pmcid": pmcid,
            "doi": record.doi,
            "authors": record.authors,
            "journal": record.journal,
            "published": record.published,
            "language": self.language(record.languages),
            "doc_type": doc_type,
            "evidence_tier": self._m.evidence_tier[doc_type],
            "classification_method": method,
            "modality": self.modality(mesh),
            "population": self.population(mesh),
            "audience": Audience.CLINICIAN,
            "topics": self.topics(mesh),
            "mesh_terms": mesh,
            "license": normalize_license(license_raw),
            "retracted": record.retracted,
            "curation_status": CurationStatus.PENDING,
            "review_notes": review_notes,
            "content_hash": content_hash(body),
            "retrieved_at": retrieved_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return {k: v for k, v in draft.items() if v is not None}

    def language(self, languages: list[str]) -> str | None:
        for code in languages:
            mapped = _LANG_639_1.get(code.casefold())
            if mapped:
                return mapped
        return None

    def doc_type(self, publication_types: list[str]) -> tuple[DocType, ClassificationMethod]:
        present = {pt.casefold() for pt in publication_types}
        for rule in self._m.publication_type_doc_type:
            if rule.publication_type.casefold() in present:
                return rule.doc_type, ClassificationMethod.SOURCE
        return DocType.OTHER, ClassificationMethod.RULE

    def modality(self, mesh: list[str]) -> list[Modality]:
        terms = {t.casefold() for t in mesh}
        found = {self._modality[t] for t in terms if t in self._modality}
        if Modality.CKD_NON_DIALYSIS in found and (
            terms & self._dialysis or found & {Modality.HEMODIALYSIS, Modality.PERITONEAL_DIALYSIS}
        ):
            found.discard(Modality.CKD_NON_DIALYSIS)
        found.discard(Modality.UNSPECIFIED)
        return [m for m in Modality if m in found] or [Modality.UNSPECIFIED]

    def topics(self, mesh: list[str]) -> list[str]:
        terms = {t.casefold() for t in mesh}
        return [
            topic
            for topic, descriptors in self._m.topics.items()
            if any(d.casefold() in terms for d in descriptors)
        ]

    def population(self, mesh: list[str]) -> Population:
        terms = {t.casefold() for t in mesh}
        pediatric = bool(terms & self._pediatric)
        adult = bool(terms & self._adult)
        if pediatric and adult:
            return Population.MIXED
        if pediatric:
            return Population.PEDIATRIC
        if adult:
            return Population.ADULT
        return Population.UNSPECIFIED
