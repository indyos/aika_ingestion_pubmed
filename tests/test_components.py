"""PubMedSearch, PubMedMetadataParser, PmcFullTextFetcher, FrontMatterBuilder, QualityGate."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aika_ingestion_pubmed.builder import FrontMatterBuilder
from aika_ingestion_pubmed.eutils import EutilsError
from aika_ingestion_pubmed.licenses import normalize_license
from aika_ingestion_pubmed.models import (
    ClassificationMethod,
    DocType,
    Modality,
    Population,
    PubMedRecord,
)
from aika_ingestion_pubmed.pmc import PmcFullTextFetcher, parse_article_set
from aika_ingestion_pubmed.pubmed_parser import PubMedMetadataParser
from aika_ingestion_pubmed.quality import QualityGate
from aika_ingestion_pubmed.search import PubMedSearch

from .support import FakeNcbi, make_config, read_fixture

# ------------------------------------------------------------------ ricerca


def test_esearch_params_and_history(tmp_path: Path) -> None:
    fake = FakeNcbi()
    cfg = make_config(tmp_path)
    cfg.search.mindate, cfg.search.maxdate = "2020/01/01", "2026/12/31"
    cfg.search.max_results = 3
    result = PubMedSearch(fake.client(), cfg.search).search()
    assert result.pmids == ["10000001", "10000002", "10000003"]
    assert (result.webenv, result.query_key) == ("WEBENV_1", "1")
    (params,) = fake.calls("esearch.fcgi")
    assert params["db"] == "pubmed"
    assert params["usehistory"] == "y"
    assert params["retmax"] == "3"
    assert (params["datetype"], params["mindate"], params["maxdate"]) == (
        "pdat",
        "2020/01/01",
        "2026/12/31",
    )
    assert "Renal Dialysis" in params["term"]


def test_extra_filter_is_and_ed(tmp_path: Path) -> None:
    fake = FakeNcbi()
    cfg = make_config(tmp_path)
    cfg.search.extra_filter = "free full text[filter]"
    PubMedSearch(fake.client(), cfg.search).search()
    term = fake.calls("esearch.fcgi")[0]["term"]
    assert term.endswith(") AND (free full text[filter])")


def test_metadata_fetched_in_batches_through_history(tmp_path: Path) -> None:
    fake = FakeNcbi()
    cfg = make_config(tmp_path)
    search = PubMedSearch(fake.client(), cfg.search)
    result = search.search()
    batches = list(search.fetch_metadata_batches(result, batch_size=2))
    assert len(batches) == 3  # 5 risultati a batch di 2
    calls = fake.calls("efetch.fcgi", "pubmed")
    assert [(c["retstart"], c["retmax"]) for c in calls] == [("0", "2"), ("2", "2"), ("4", "1")]
    assert all(c["WebEnv"] == "WEBENV_1" and c["query_key"] == "1" for c in calls)
    parsed = [r.pmid for xml in batches for r in PubMedMetadataParser().parse(xml)]
    assert parsed == FakeNcbi.PMIDS


def test_esearch_error_payload_raises(tmp_path: Path) -> None:
    fake = FakeNcbi()
    fake.handler = lambda request: __import__("httpx").Response(  # type: ignore[method-assign]
        200, json={"error": "Invalid db name"}
    )
    with pytest.raises(EutilsError):
        PubMedSearch(fake.client(), make_config(tmp_path).search).search()


# ------------------------------------------------------------------ parser PubMed


@pytest.fixture(scope="module")
def records() -> dict[str, PubMedRecord]:
    parsed = PubMedMetadataParser().parse(read_fixture("pubmed/pubmed_batch.xml"))
    return {r.pmid: r for r in parsed}


def test_parser_extracts_all_metadata(records: dict[str, PubMedRecord]) -> None:
    r = records["10000001"]
    assert r.title.startswith("Nutritional management of adults")
    assert r.authors == ["Maria Rossi", "Renal Nutrition Working Group"]
    assert r.journal == "Journal of Renal Nutrition (fixture)"
    assert r.published == "2024-03-05"
    assert r.languages == ["eng"]
    assert "Potassium, Dietary" in r.mesh_terms
    assert r.publication_types == ["Journal Article", "Practice Guideline", "Review"]
    assert (r.doi, r.pmcid) == ("10.0000/fixture.0001", "PMC1000001")
    assert not r.retracted


def test_parser_structured_abstract_keeps_labels_and_markup(
    records: dict[str, PubMedRecord],
) -> None:
    abstract = records["10000001"].abstract
    assert [s.label for s in abstract] == ["BACKGROUND", "RECOMMENDATIONS"]
    assert "K<sup>+</sup> <= 5.5 mmol/L" in abstract[1].text


def test_parser_unstructured_abstract_and_partial_dates(
    records: dict[str, PubMedRecord],
) -> None:
    assert [s.label for s in records["10000002"].abstract] == [None]
    assert records["10000002"].published == "2023-01"  # MedlineDate «2023 Jan-Feb»
    assert records["10000003"].published == "2022"  # solo anno
    assert records["10000004"].published == "2019-11"  # mese numerico


def test_parser_flags_retracted_and_missing_pmcid(records: dict[str, PubMedRecord]) -> None:
    assert records["10000004"].retracted
    assert records["10000005"].pmcid is None
    assert records["10000005"].abstract == []
    assert not records["10000005"].retracted  # «Cites» non è una ritrattazione


@pytest.mark.parametrize("pt", ["Retracted Publication", "Retraction of Publication"])
def test_both_retraction_publication_types_detected(pt: str) -> None:
    xml = read_fixture("pubmed/pubmed_batch.xml").decode().replace("Retracted Publication", pt)
    rec = {r.pmid: r for r in PubMedMetadataParser().parse(xml.encode())}["10000004"]
    assert rec.retracted


def test_parser_detects_retraction_notice_link() -> None:
    xml = read_fixture("pubmed/pubmed_batch.xml").decode()
    xml = xml.replace(
        '<CommentsCorrections RefType="Cites">', '<CommentsCorrections RefType="RetractionIn">'
    )
    assert {r.pmid: r for r in PubMedMetadataParser().parse(xml.encode())}["10000005"].retracted


# ------------------------------------------------------------------ PMC


def test_pmc_fetch_in_batches_and_availability(tmp_path: Path) -> None:
    fake = FakeNcbi()
    fetcher = PmcFullTextFetcher(fake.client(), batch_size=2)
    articles = fetcher.fetch(["PMC1000001", "PMC1000002", "PMC1000003"])
    calls = fake.calls("efetch.fcgi", "pmc")
    assert [c["id"] for c in calls] == ["1000001,1000002", "1000003"]
    assert articles["PMC1000001"].has_body
    assert articles["PMC1000002"].has_body
    assert not articles["PMC1000003"].has_body  # <body> con soli spazi = non disponibile


def test_pmc_missing_article_is_absent(tmp_path: Path) -> None:
    fake = FakeNcbi()
    assert PmcFullTextFetcher(fake.client()).fetch(["PMC1000004"]) == {}


def test_license_extraction_variants() -> None:
    arts = {
        **parse_article_set(read_fixture("jats/guideline_structured.xml")),
        **parse_article_set(read_fixture("jats/review_edge_cases.xml")),
        **parse_article_set(read_fixture("jats/no_body.xml")),
    }
    assert arts["PMC1000001"].license_raw == "https://creativecommons.org/licenses/by/4.0/"
    assert arts["PMC1000002"].license_raw == "https://creativecommons.org/licenses/by-nc/4.0/"
    assert arts["PMC1000003"].license_raw == "https://creativecommons.org/licenses/by/4.0/"


def test_license_type_fallback() -> None:
    xml = (
        b"<pmc-articleset><article><front><article-meta>"
        b'<article-id pub-id-type="pmcid">PMC9</article-id>'
        b'<permissions><license license-type="CC BY-SA 4.0"/></permissions>'
        b"</article-meta></front><body><p>x</p></body></article></pmc-articleset>"
    )
    assert parse_article_set(xml)["PMC9"].license_raw == "CC BY-SA 4.0"


def test_xml_parsing_does_not_resolve_external_entities() -> None:
    xml = (
        b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        b"<pmc-articleset><article><front><article-meta>"
        b'<article-id pub-id-type="pmcid">PMC8</article-id></article-meta></front>'
        b"<body><p>&x;</p></body></article></pmc-articleset>"
    )
    art = parse_article_set(xml)["PMC8"]
    assert b"root:" not in art.xml


# ------------------------------------------------------------------ licenze


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://creativecommons.org/licenses/by/4.0/", "CC-BY-4.0"),
        ("http://creativecommons.org/licenses/by-sa/3.0/legalcode", "CC-BY-SA-3.0"),
        ("https://creativecommons.org/licenses/by-nc/4.0/", "CC-BY-NC-4.0"),
        ("https://creativecommons.org/licenses/by-nd/4.0/", "CC-BY-ND-4.0"),
        ("https://creativecommons.org/licenses/by-nc-nd/4.0/", "CC-BY-NC-ND-4.0"),
        ("https://creativecommons.org/publicdomain/zero/1.0/", "CC0-1.0"),
        ("CC BY 4.0", "CC-BY-4.0"),
        ("CC BY-NC-SA 4.0", "CC-BY-NC-SA-4.0"),
        ("CC0 1.0", "CC0-1.0"),
        ("CC BY", "CC-BY"),
        ("open-access", None),
        ("All rights reserved", None),
        ("", None),
        (None, None),
    ],
)
def test_license_normalization(raw: str | None, expected: str | None) -> None:
    assert normalize_license(raw) == expected


# ------------------------------------------------------------------ builder


@pytest.fixture
def builder(tmp_path: Path) -> FrontMatterBuilder:
    return FrontMatterBuilder(make_config(tmp_path).mapping)


def rec(**kw: object) -> PubMedRecord:
    base = {"pmid": "1", "title": "T", "languages": ["eng"]}
    return PubMedRecord(**{**base, **kw})  # pyright: ignore[reportArgumentType]


def test_doc_type_priority_and_tier(builder: FrontMatterBuilder) -> None:
    # «Practice Guideline» prevale su «Review» anche se compare dopo nell'elenco del record
    assert builder.doc_type(["Review", "Practice Guideline"]) == (
        DocType.GUIDELINE,
        ClassificationMethod.SOURCE,
    )
    assert builder.doc_type(["Journal Article", "Meta-Analysis", "Systematic Review"])[0] == (
        DocType.META_ANALYSIS
    )
    assert builder.doc_type(["Randomized Controlled Trial"])[0] == DocType.RCT
    assert builder.doc_type(["Case Reports"]) == (DocType.OTHER, ClassificationMethod.RULE)


def test_modality_rules(builder: FrontMatterBuilder) -> None:
    assert builder.modality(["Renal Dialysis"]) == [Modality.UNSPECIFIED]  # non deducibile
    assert builder.modality([]) == [Modality.UNSPECIFIED]
    assert builder.modality(["Peritoneal Dialysis"]) == [Modality.PERITONEAL_DIALYSIS]
    assert builder.modality(["Hemodiafiltration", "Kidney Transplantation"]) == [
        Modality.HEMODIALYSIS,
        Modality.TRANSPLANT,
    ]
    # CKD non in dialisi solo se non compaiono termini di dialisi
    assert builder.modality(["Renal Insufficiency, Chronic"]) == [Modality.CKD_NON_DIALYSIS]
    assert builder.modality(["Renal Insufficiency, Chronic", "Renal Dialysis"]) == [
        Modality.UNSPECIFIED
    ]
    assert builder.modality(["renal insufficiency, chronic", "peritoneal dialysis"]) == [
        Modality.PERITONEAL_DIALYSIS
    ]


def test_topics_population_language(builder: FrontMatterBuilder) -> None:
    assert builder.topics(["Potassium, Dietary", "Diet", "Humans"]) == [
        "potassium",
        "diet_general",
    ]
    assert builder.topics(["Humans"]) == []
    assert builder.population(["Adult", "Humans"]) == Population.ADULT
    assert builder.population(["Child"]) == Population.PEDIATRIC
    assert builder.population(["Child", "Aged"]) == Population.MIXED
    assert builder.population(["Humans"]) == Population.UNSPECIFIED
    assert builder.language(["ita"]) == "it"
    assert builder.language(["und", "eng"]) == "en"
    assert builder.language(["xxx"]) is None


def test_builder_draft_is_complete(builder: FrontMatterBuilder) -> None:
    record = PubMedMetadataParser().parse(read_fixture("pubmed/pubmed_batch.xml"))[0]
    draft = builder.build(
        record,
        pmcid="PMC1000001",
        title="Titolo",
        body="# Titolo\n",
        license_raw="https://creativecommons.org/licenses/by/4.0/",
        original_uri="gs://b/pmid-10000001.xml",
        retrieved_at=datetime(2026, 9, 26, 10, 0, tzinfo=UTC),
    )
    assert draft["id"] == "pmid-10000001"
    assert draft["source_uri"] == "https://pmc.ncbi.nlm.nih.gov/articles/PMC1000001/"
    assert draft["curation_status"].value == "pending"  # pyright: ignore[reportAttributeAccessIssue]
    assert draft["audience"].value == "clinician"  # pyright: ignore[reportAttributeAccessIssue]
    assert draft["evidence_tier"] == 1
    assert draft["retrieved_at"] == "2026-09-26T10:00:00Z"
    assert draft["license"] == "CC-BY-4.0"
    assert "review_notes" not in draft


# ------------------------------------------------------------------ quality gate


def full_draft(builder: FrontMatterBuilder, **kw: object) -> dict[str, object]:
    record = PubMedMetadataParser().parse(read_fixture("pubmed/pubmed_batch.xml"))[0]
    args: dict[str, object] = {
        "pmcid": "PMC1000001",
        "title": "Titolo",
        "body": "x" * 300,
        "license_raw": "https://creativecommons.org/licenses/by/4.0/",
        "original_uri": "gs://b/pmid-10000001.xml",
        "retrieved_at": datetime(2026, 9, 26, tzinfo=UTC),
    }
    return builder.build(record, **{**args, **kw})  # pyright: ignore[reportArgumentType]


def test_quality_gate_accepts_good_document(tmp_path: Path, builder: FrontMatterBuilder) -> None:
    gate = QualityGate(make_config(tmp_path).quality)
    result = gate.check(full_draft(builder), "x" * 300)
    assert result.ok and result.front_matter is not None


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"license_raw": "https://creativecommons.org/licenses/by-nc/4.0/"}, "license_not_allowed"),
        ({"license_raw": "https://creativecommons.org/licenses/by-nd/4.0/"}, "license_not_allowed"),
        ({"license_raw": None}, "license_missing"),
        ({"license_raw": "open-access"}, "license_missing"),
    ],
)
def test_quality_gate_license(
    tmp_path: Path, builder: FrontMatterBuilder, override: dict[str, object], reason: str
) -> None:
    gate = QualityGate(make_config(tmp_path).quality)
    result = gate.check(full_draft(builder, **override), "x" * 300)
    assert not result.ok and reason in result.reasons


def test_quality_gate_language_and_length(tmp_path: Path, builder: FrontMatterBuilder) -> None:
    gate = QualityGate(make_config(tmp_path).quality)
    draft = full_draft(builder)
    draft["language"] = "fr"
    assert gate.check(draft, "x" * 300).reasons == ["language_not_allowed"]
    draft.pop("language")
    assert gate.check(draft, "x" * 300).reasons == ["language_missing"]
    assert "body_too_short" in gate.check(full_draft(builder), "x" * 10).reasons


def test_quality_gate_schema_validation(tmp_path: Path, builder: FrontMatterBuilder) -> None:
    gate = QualityGate(make_config(tmp_path).quality)
    draft = full_draft(builder)
    draft["title"] = ""
    result = gate.check(draft, "x" * 300)
    assert result.reasons == ["schema_invalid"] and result.front_matter is None
