from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from aika_ingestion_pubmed.cli import main
from aika_ingestion_pubmed.document import content_hash, parse_document, render_document
from aika_ingestion_pubmed.models import FrontMatter
from aika_ingestion_pubmed.schema import front_matter_schema, schema_text

from .support import ROOT

VALID: dict[str, Any] = {
    "schema_version": 1,
    "id": "pmid-12345",
    "title": "Nutrition in dialysis",
    "source_type": "pmc_fulltext",
    "source_uri": "https://pmc.ncbi.nlm.nih.gov/articles/PMC999/",
    "original_uri": "gs://bucket/pubmed/pmid-12345.xml",
    "pmid": "12345",
    "pmcid": "PMC999",
    "doi": "10.1000/x",
    "authors": ["Maria Rossi"],
    "journal": "J Ren Nutr",
    "published": "2024-03",
    "language": "en",
    "doc_type": "guideline",
    "evidence_tier": 1,
    "classification_method": "source",
    "modality": ["hemodialysis"],
    "population": "adult",
    "audience": "clinician",
    "topics": ["potassium"],
    "mesh_terms": ["Potassium, Dietary"],
    "license": "CC-BY-4.0",
    "retracted": False,
    "curation_status": "pending",
    "content_hash": "sha256:" + "a" * 64,
    "retrieved_at": "2026-09-26T10:00:00Z",
}


def with_change(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(VALID)
    for key, value in changes.items():
        if value is ...:
            del data[key]
        else:
            data[key] = value
    return data


INVALID: list[tuple[str, dict[str, Any]]] = [
    ("missing title", with_change(title=...)),
    ("empty title", with_change(title="")),
    ("bad doc_type", with_change(doc_type="blog_post")),
    ("tier 0", with_change(evidence_tier=0)),
    ("tier 6", with_change(evidence_tier=6)),
    ("bad language", with_change(language="eng")),
    ("empty modality", with_change(modality=[])),
    ("bad modality", with_change(modality=["dialysis"])),
    ("bad audience", with_change(audience="everyone")),
    ("bad status", with_change(curation_status="draft")),
    ("bad source_type", with_change(source_type="pdf")),
    ("bad id", with_change(id="12345")),
    ("bad published", with_change(published="2024-3")),
    ("bad hash", with_change(content_hash="sha256:abc")),
    ("bad retrieved_at", with_change(retrieved_at="2026-09-26 10:00")),
    ("unknown key", with_change(extra_key="x")),
    ("missing license", with_change(license=...)),
    ("retracted not bool", with_change(retracted="maybe")),
]


def test_valid_front_matter_accepted_by_pydantic_and_json_schema() -> None:
    FrontMatter.model_validate(VALID)
    jsonschema.validate(VALID, front_matter_schema())


def test_minimal_front_matter_without_optional_keys() -> None:
    minimal = {
        k: v
        for k, v in VALID.items()
        if k
        not in {"pmid", "pmcid", "doi", "authors", "journal", "published", "topics", "mesh_terms"}
    }
    FrontMatter.model_validate(minimal)
    jsonschema.validate(minimal, front_matter_schema())


@pytest.mark.parametrize(("label", "data"), INVALID, ids=[label for label, _ in INVALID])
def test_invalid_front_matter_rejected_by_both(label: str, data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FrontMatter.model_validate(data)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, front_matter_schema())


def test_id_must_match_pmid_only_pydantic_can_check() -> None:
    """Vincolo tra due campi: JSON Schema non lo esprime, la CI del corpus deve verificarlo
    a parte (nome file == `id` == `pmid-<pmid>`)."""
    data = with_change(pmid="999")
    with pytest.raises(ValidationError, match="coerente"):
        FrontMatter.model_validate(data)
    jsonschema.validate(data, front_matter_schema())  # limite noto dello schema


def test_key_order_is_fixed_and_empty_optionals_omitted() -> None:
    fm = FrontMatter.model_validate(with_change(topics=[], authors=[], doi=None, review_notes=None))
    keys = list(fm.to_ordered_dict())
    expected = [k for k in VALID if k not in {"topics", "authors", "doi"}]
    assert keys == expected


def test_required_keys_in_schema_match_contract() -> None:
    required = set(front_matter_schema()["required"])
    assert required == {
        "id", "title", "source_type", "source_uri", "original_uri", "language", "doc_type",
        "evidence_tier", "classification_method", "modality", "population", "audience",
        "license", "retracted", "curation_status", "content_hash", "retrieved_at",
    }  # fmt: skip
    # `schema_version` ha un default ma il contratto lo vuole sempre scritto nel file
    assert "schema_version" in FrontMatter.model_fields


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


# ------------------------------------------------------------------ documento


def test_document_roundtrip_and_yaml_types() -> None:
    fm = FrontMatter.model_validate(VALID)
    body = "# Titolo\n\n## Abstract\n\nTesto.\n"
    text = render_document(fm, body)
    assert text.startswith("---\nschema_version: 1\nid: pmid-12345\n")
    meta = yaml.safe_load(text.split("---\n")[1])
    assert meta["pmid"] == "12345" and isinstance(meta["pmid"], str)  # PMID sempre stringa
    assert meta["published"] == "2024-03" and isinstance(meta["published"], str)
    assert meta["retracted"] is False
    jsonschema.validate(meta, front_matter_schema())
    parsed = parse_document(text)
    assert parsed.body == body
    assert parsed.curation_status == "pending"
    assert parsed.content_hash == VALID["content_hash"]


def test_full_date_and_numeric_looking_strings_stay_strings() -> None:
    fm = FrontMatter.model_validate(with_change(published="2024-03-05", pmid="12345"))
    meta = yaml.safe_load(render_document(fm, "x\n").split("---\n")[1])
    assert meta["published"] == "2024-03-05" and isinstance(meta["published"], str)
    assert isinstance(meta["pmid"], str)


def test_content_hash_covers_body_only() -> None:
    body = "# T\n\n## Abstract\n\nTesto.\n"
    a = FrontMatter.model_validate(with_change(content_hash=content_hash(body)))
    b = FrontMatter.model_validate(
        with_change(content_hash=content_hash(body), retrieved_at="2030-01-01T00:00:00Z")
    )
    assert content_hash(body) == parse_document(render_document(a, body)).content_hash
    assert parse_document(render_document(b, body)).content_hash == content_hash(body)
    assert render_document(a, body) != render_document(b, body)  # cambia solo il front matter


def test_render_is_deterministic_and_lf_only() -> None:
    fm = FrontMatter.model_validate(VALID)
    assert render_document(fm, "x\n") == render_document(fm, "x\n")
    assert "\r" not in render_document(fm, "x\n")


def test_parse_document_tolerates_crlf() -> None:
    fm = FrontMatter.model_validate(VALID)
    body = "# T\n\nTesto.\n"
    text = render_document(fm, body)
    parsed = parse_document(text.replace("\n", "\r\n"))
    assert parsed.body == body
    assert parsed.curation_status == "pending"


def test_parse_document_rejects_missing_front_matter() -> None:
    with pytest.raises(ValueError, match="front matter"):
        parse_document("# solo corpo\n")
