"""Articoli PMC Open Access **reali** (CC BY 4.0, scaricati il 2026-09-26 con efetch): regressione."""

from __future__ import annotations

import re
import unicodedata

import pytest

from aika_ingestion_pubmed.jats import JatsToMarkdown
from aika_ingestion_pubmed.licenses import normalize_license
from aika_ingestion_pubmed.models import PubMedRecord
from aika_ingestion_pubmed.pmc import parse_article_set
from aika_ingestion_pubmed.pubmed_parser import PubMedMetadataParser
from aika_ingestion_pubmed.xmlutil import parse_xml

from .support import read_fixture

# PMID → PMCID
ARTICLES = {"40549189": "PMC12686079", "42739048": "PMC13567454", "42087061": "PMC13148085"}


def convert(pmcid: str) -> str:
    article = parse_article_set(read_fixture(f"jats_real/{pmcid}.xml"))[pmcid]
    root = parse_xml(article.xml)
    title = JatsToMarkdown.extract_title(root) or "Titolo"
    return JatsToMarkdown().convert(root, title=title)


@pytest.fixture(scope="module")
def records() -> dict[str, PubMedRecord]:
    xml = read_fixture("pubmed_real/three_articles.xml")
    return {r.pmid: r for r in PubMedMetadataParser().parse(xml)}


@pytest.mark.parametrize("pmid", ARTICLES)
def test_metadata_and_license(pmid: str, records: dict[str, PubMedRecord]) -> None:
    pmcid = ARTICLES[pmid]
    assert records[pmid].pmcid == pmcid
    article = parse_article_set(read_fixture(f"jats_real/{pmcid}.xml"))[pmcid]
    assert article.has_body
    assert normalize_license(article.license_raw) == "CC-BY-4.0"


@pytest.mark.parametrize("pmcid", ARTICLES.values())
def test_conversion_is_deterministic_and_clean(pmcid: str) -> None:
    body = convert(pmcid)
    assert body == convert(pmcid)
    assert unicodedata.is_normalized("NFC", body)
    assert "\r" not in body
    assert len(re.findall(r"^# ", body, re.MULTILINE)) == 1
    assert "## Abstract" in body
    # nessun tag JATS grezzo sopravvive alla conversione
    assert not re.search(r"</?(xref|italic|bold|sec|p|table-wrap|tbody|thead)\b", body)


def test_table_with_footnote_markers_and_superscripts() -> None:
    body = convert("PMC12686079")
    assert "**Table 1. Characteristics of participants" in body
    assert "| Characteristics | CKD group (*n* = 32) | Healthy control group (*n* = 34) |" in body
    assert re.search(r"^\| --- \| --- \| --- \| --- \|$", body, re.MULTILINE)
    assert "25<sup>th</sup> percentile" in body


def test_multilevel_sections_and_subscripts() -> None:
    body = convert("PMC13148085")
    assert "## Introduction" in body
    assert "### Patient tissue collection" in body
    assert "#### Animal models of AVF" in body
    assert "95% O<sub>2</sub>/5% CO<sub>2</sub>" in body


def test_pubmed_metadata_of_real_record(records: dict[str, PubMedRecord]) -> None:
    record = records["42087061"]
    assert record.doi == "10.1080/0886022X.2026.2663246"
    assert "Renal Dialysis" in record.mesh_terms
    assert record.retracted is False
