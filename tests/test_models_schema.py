"""Integrazione con ``aika_ingestion_common``: il front matter PMC resta valido (schema v2) e lo
schema generato è quello dell'unione discriminata. I test a basso livello su modelli, documento
e schema vivono in ``aika_ingestion_common/tests``."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from aika_ingestion_common.document import content_hash, parse_document, render_document
from aika_ingestion_common.models import SCHEMA_VERSION, PmcFullTextFrontMatter
from aika_ingestion_common.schema import front_matter_schema, schema_text
from pydantic import ValidationError

from aika_ingestion_pubmed.cli import main

from .support import ROOT

VALID: dict[str, Any] = {
    "schema_version": 2,
    "id": "pmid-12345",
    "title": "Nutrition in dialysis",
    "source_type": "pmc_fulltext",
    "source_uri": "https://pmc.ncbi.nlm.nih.gov/articles/PMC999/",
    "original_uri": "gs://bucket/pubmed/pmid-12345.xml",
    "language": "en",
    "doc_type": "guideline",
    "evidence_tier": 1,
    "classification_method": "source",
    "modality": ["hemodialysis"],
    "population": "adult",
    "audience": "clinician",
    "topics": ["potassium"],
    "license": "CC-BY-4.0",
    "retracted": False,
    "curation_status": "pending",
    "content_hash": "sha256:" + "a" * 64,
    "retrieved_at": "2026-09-26T10:00:00Z",
    "pmid": "12345",
    "pmcid": "PMC999",
    "doi": "10.1000/x",
    "authors": ["Maria Rossi"],
    "journal": "J Ren Nutr",
    "published": "2024-03",
    "mesh_terms": ["Potassium, Dietary"],
}


def with_change(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    data.update(changes)
    return data


def test_pubmed_schema_version_is_v2() -> None:
    assert SCHEMA_VERSION == 2


def test_pmc_front_matter_valid_for_pydantic_and_json_schema() -> None:
    PmcFullTextFrontMatter.model_validate(VALID)
    jsonschema.validate(VALID, front_matter_schema())


def test_id_must_match_pmid() -> None:
    with pytest.raises(ValidationError, match="coerente"):
        PmcFullTextFrontMatter.model_validate(with_change(pmid="999"))


def test_v2_key_order_is_fixed_and_empty_optionals_omitted() -> None:
    fm = PmcFullTextFrontMatter.model_validate(with_change(topics=[], authors=[], doi=None))
    expected = [k for k in VALID if k not in {"topics", "authors", "doi"}]
    assert list(fm.to_ordered_dict()) == expected


def test_render_parse_roundtrip_keeps_pmid_a_string() -> None:
    fm = PmcFullTextFrontMatter.model_validate(VALID)
    body = "# Titolo\n\n## Abstract\n\nTesto.\n"
    text = render_document(fm, body)
    assert text.startswith("---\nschema_version: 2\nid: pmid-12345\n")
    meta = yaml.safe_load(text.split("---\n")[1])
    assert meta["pmid"] == "12345" and isinstance(meta["pmid"], str)
    assert meta["published"] == "2024-03" and isinstance(meta["published"], str)
    jsonschema.validate(meta, front_matter_schema())
    parsed = parse_document(text)
    assert parsed.body == body
    assert parsed.id == "pmid-12345"
    assert render_document(fm, body) == text and "\r" not in text


def test_content_hash_covers_body_only() -> None:
    body = "# T\n\nTesto.\n"
    a = PmcFullTextFrontMatter.model_validate(with_change(content_hash=content_hash(body)))
    b = a.model_copy(update={"retrieved_at": "2030-01-01T00:00:00Z"})
    assert parse_document(render_document(b, body)).content_hash == content_hash(body)
    assert render_document(a, body) != render_document(b, body)


def test_schema_file_is_in_sync_with_model() -> None:
    on_disk = (ROOT / "schema" / "front_matter.schema.json").read_text(encoding="utf-8")
    assert on_disk == schema_text(), (
        "schema/front_matter.schema.json non aggiornato: `uv run aika-ingestion-pubmed schema`"
    )


def test_cli_schema_generate_and_check(tmp_path: Path) -> None:
    out = tmp_path / "s" / "schema.json"
    assert main(["schema", "--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == schema_text()
    assert main(["schema", "--output", str(out), "--check"]) == 0
    out.write_text("{}", encoding="utf-8")
    assert main(["schema", "--output", str(out), "--check"]) == 1
