"""Flusso completo su repository git temporanei: new, updated, unchanged, rejected, già in PR."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
import yaml

from aika_ingestion_pubmed.config import Config
from aika_ingestion_pubmed.corpus import CorpusWriter
from aika_ingestion_pubmed.document import parse_document
from aika_ingestion_pubmed.jats import JatsToMarkdown
from aika_ingestion_pubmed.pipeline import Pipeline, RunStats
from aika_ingestion_pubmed.schema import front_matter_schema

from .support import (
    FakeHost,
    FakeNcbi,
    branches,
    commit_on_main,
    git,
    make_config,
    make_corpus_repos,
    merge_branch_into_main,
    show,
)

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
DOC1, DOC2 = "corpus/pmid-10000001.md", "corpus/pmid-10000002.md"


class Env:
    """Ambiente di un run: NCBI finto + origin/clone git + host finto."""

    def __init__(self, tmp_path: Path, **config_kw: object) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp_path
        self.origin, self.clone = make_corpus_repos(tmp_path)
        self.fake = FakeNcbi()
        self.host = FakeHost()
        self.config: Config = make_config(tmp_path, allow_nc=True, **config_kw)

    def run(self, *, dry_run: bool = False, host: FakeHost | None = None) -> RunStats:
        self.fake.requests.clear()
        with self.fake.client() as client:
            pipeline = Pipeline(
                self.config,
                client,
                CorpusWriter(self.config.corpus),
                host if host is not None else self.host,
                now=lambda: NOW,
            )
            return pipeline.run(dry_run=dry_run)

    def merge_all_pr_branches(self) -> None:
        for branch in branches(self.origin):
            merge_branch_into_main(self.origin, branch)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ------------------------------------------------------------------ primo run


def test_first_run_opens_one_pr_with_valid_files(env: Env) -> None:
    stats = env.run()
    assert (stats.found, stats.new, stats.updated, stats.unchanged, stats.errors) == (5, 2, 0, 0, 0)
    assert dict(stats.excluded) == {
        "no_open_access_fulltext": 1,  # 10000003: <body> vuoto
        "retracted": 1,  # 10000004
        "no_pmcid": 1,  # 10000005
    }
    (pr,) = env.host.created
    assert pr["base"] == "main"
    assert pr["branch"] == "ingest/2026-09-26-pubmed-1"
    assert branches(env.origin) == ["ingest/2026-09-26-pubmed-1"]

    # un solo commit sul branch, sopra main
    log = git(env.origin, "log", "--format=%s", "main..ingest/2026-09-26-pubmed-1").splitlines()
    assert len(log) == 1

    schema = front_matter_schema()
    for rel, pmid in ((DOC1, "10000001"), (DOC2, "10000002")):
        text = show(env.origin, "ingest/2026-09-26-pubmed-1", rel)
        meta = yaml.safe_load(text.split("---\n")[1])
        jsonschema.validate(meta, schema)
        assert meta["id"] == f"pmid-{pmid}"
        assert meta["curation_status"] == "pending"
        assert meta["audience"] == "clinician"
        assert meta["retracted"] is False
        assert meta["original_uri"] == f"gs://bucket/pubmed/pmid-{pmid}.xml"
        assert (env.tmp / "originals" / f"pmid-{pmid}.xml").exists()


def test_document_contents_follow_the_contract(env: Env) -> None:
    env.run()
    branch = "ingest/2026-09-26-pubmed-1"
    doc = parse_document(show(env.origin, branch, DOC1))
    assert doc.meta["doc_type"] == "guideline" and doc.meta["evidence_tier"] == "1"
    assert doc.meta["modality"] == ["hemodialysis"]
    assert doc.meta["topics"] == ["potassium"]
    assert doc.meta["license"] == "CC-BY-4.0"
    assert doc.meta["title"].startswith("Nutritional management")
    assert doc.body.startswith("# Nutritional management")
    assert "Includes protein lost in dialysate" in doc.body

    other = parse_document(show(env.origin, branch, DOC2))
    assert other.meta["license"] == "CC-BY-NC-4.0"  # ammessa da questa config di test
    assert other.meta["population"] == "mixed"
    assert other.meta["modality"] == ["peritoneal_dialysis"]
    assert other.meta["doc_type"] == "narrative_review"
    assert other.meta["published"] == "2023-01"


def test_pr_body_table_and_ordering_by_evidence_tier(env: Env) -> None:
    env.run()
    body = env.host.created[0]["body"]
    lines = body.splitlines()
    assert lines[0] == "| id | titolo | doc_type | modality | topics | stato | PubMed |"
    rows = [ln for ln in lines if ln.startswith("| `pmid-")]
    assert [r.split(" | ")[0] for r in rows] == ["| `pmid-10000001`", "| `pmid-10000002`"]
    assert "guideline | hemodialysis | potassium | new | [10000001]" in rows[0]
    assert "(https://pubmed.ncbi.nlm.nih.gov/10000001/)" in rows[0]
    assert "pending" in body


def test_max_pr_size_splits_into_several_prs(tmp_path: Path) -> None:
    env = Env(tmp_path, max_pr_size=1)
    stats = env.run()
    assert [pr["branch"] for pr in env.host.created] == [
        "ingest/2026-09-26-pubmed-1",
        "ingest/2026-09-26-pubmed-2",
    ]
    assert len(stats.pull_requests) == 2
    # ordinati per evidence_tier: la linea guida (1) prima della review narrativa (4)
    assert "pmid-10000001" in env.host.created[0]["body"]
    assert "pmid-10000002" in env.host.created[1]["body"]


def test_originals_are_the_fetched_xml(env: Env) -> None:
    env.run()
    xml = (env.tmp / "originals" / "pmid-10000001.xml").read_bytes()
    assert xml.startswith(b"<?xml") and b"<article" in xml


# ------------------------------------------------------------------ criterio: nessuna PR se nulla cambia


def test_rerun_without_upstream_changes_opens_no_pr(env: Env) -> None:
    env.run()
    env.merge_all_pr_branches()
    env.host.created.clear()
    before = branches(env.origin)

    stats = env.run()
    assert (stats.new, stats.updated, stats.unchanged, stats.errors) == (0, 0, 2, 0)
    assert stats.pull_requests == [] and env.host.created == []
    assert branches(env.origin) == before  # nessun nuovo branch


def test_rerun_only_retrieved_at_would_differ_but_is_unchanged(env: Env) -> None:
    env.run()
    env.merge_all_pr_branches()
    env.host.created.clear()
    later = datetime(2027, 1, 1, tzinfo=UTC)
    with env.fake.client() as client:
        stats = Pipeline(
            env.config, client, CorpusWriter(env.config.corpus), env.host, now=lambda: later
        ).run()
    assert stats.unchanged == 2 and env.host.created == []


# ------------------------------------------------------------------ updated


def test_upstream_change_produces_updated_pr_and_keeps_review_notes(env: Env) -> None:
    env.run()
    env.merge_all_pr_branches()
    original = show(env.origin, "main", DOC1)
    approved = original.replace("curation_status: pending", "curation_status: approved")
    approved = approved.replace("retracted: false\n", "retracted: false\n", 1)
    approved = approved.replace(
        "content_hash:",
        "review_notes: OK per pazienti in HD, verificato dal nefrologo\ncontent_hash:",
    )
    commit_on_main(env.origin, env.tmp, DOC1, approved)
    env.host.created.clear()

    env.fake.edit_article("1000001", "We searched three databases.", "We searched four databases.")
    stats = env.run()

    assert (stats.new, stats.updated, stats.unchanged) == (0, 1, 1)
    (pr,) = env.host.created
    assert "| updated |" in pr["body"] and "| new |" not in pr["body"]
    doc = parse_document(show(env.origin, pr["branch"], DOC1))
    assert "four databases" in doc.body
    assert doc.meta["curation_status"] == "pending"  # torna in revisione
    assert doc.meta["review_notes"] == "OK per pazienti in HD, verificato dal nefrologo"
    # il file del secondo documento non è toccato dalla PR
    diff = git(env.origin, "diff", "--name-only", f"main...{pr['branch']}").split()
    assert diff == [DOC1]


# ------------------------------------------------------------------ esclusioni


def test_rejected_documents_are_skipped_without_fetching_fulltext(env: Env) -> None:
    env.run()
    env.merge_all_pr_branches()
    rejected = show(env.origin, "main", DOC2).replace(
        "curation_status: pending", "curation_status: rejected"
    )
    commit_on_main(env.origin, env.tmp, DOC2, rejected)
    env.host.created.clear()
    env.fake.edit_article("1000002", "Final text.", "Final text, changed upstream.")

    stats = env.run()
    assert stats.excluded["rejected_in_corpus"] == 1
    assert (stats.new, stats.updated) == (0, 0)
    fetched = {i for p in env.fake.calls("efetch.fcgi", "pmc") for i in p["id"].split(",")}
    assert "1000002" not in fetched  # scartato dal corpus prima di scaricare il full text
    assert "1000001" in fetched
    assert env.host.created == []


def test_suspended_documents_are_not_skipped(env: Env) -> None:
    """Solo `rejected` blocca: uno `suspended` può tornare in revisione se cambia."""
    env.run()
    env.merge_all_pr_branches()
    suspended = show(env.origin, "main", DOC2).replace(
        "curation_status: pending", "curation_status: suspended"
    )
    commit_on_main(env.origin, env.tmp, DOC2, suspended)
    stats = env.run()
    assert stats.excluded["rejected_in_corpus"] == 0
    assert stats.unchanged == 2


def test_documents_in_open_prs_are_skipped(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.host = FakeHost(open_pmids={"10000001"})
    stats = env.run()
    assert stats.excluded["in_open_pr"] == 1
    assert stats.new == 1
    assert "pmid-10000001" not in env.host.created[0]["body"]
    fetched = {i for p in env.fake.calls("efetch.fcgi", "pmc") for i in p["id"].split(",")}
    assert "1000001" not in fetched  # già in una PR aperta: non si scarica di nuovo
    assert "1000002" in fetched


def test_quality_gate_exclusions_are_counted(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.config = make_config(tmp_path)  # senza CC BY-NC
    stats = env.run()
    assert stats.new == 1
    assert stats.excluded["quality:license_not_allowed"] == 1


def test_short_bodies_rejected(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.config.quality.min_body_chars = 100_000
    stats = env.run()
    assert stats.new == 0
    assert stats.excluded["quality:body_too_short"] == 2
    assert env.host.created == []


# ------------------------------------------------------------------ errori e dry-run


def test_error_on_one_document_does_not_stop_the_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = JatsToMarkdown.convert

    def flaky(self: JatsToMarkdown, article: object, **kw: object) -> str:
        if kw["title"] == "Dietary potassium in peritoneal dialysis":
            raise RuntimeError("boom")
        return real(self, article, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(JatsToMarkdown, "convert", flaky)
    stats = env.run()
    assert stats.errors == 1 and stats.new == 1
    assert "pmid-10000001" in env.host.created[0]["body"]


def test_dry_run_writes_nothing(env: Env) -> None:
    stats = env.run(dry_run=True)
    assert stats.new == 2
    assert env.host.created == []
    assert branches(env.origin) == []
    assert not (env.tmp / "originals").exists()
    assert git(env.clone, "status", "--porcelain").strip() == ""


def test_clone_left_clean_and_detached_at_base(env: Env) -> None:
    env.run()
    assert git(env.clone, "status", "--porcelain").strip() == ""
    assert (
        git(env.clone, "rev-parse", "HEAD").strip()
        == git(env.clone, "rev-parse", "origin/main").strip()
    )


# ------------------------------------------------------------------ criterio: solo E-utilities


def test_only_eutils_endpoints_are_called(env: Env) -> None:
    env.run()
    endpoints = {e for e, _ in env.fake.requests}
    assert endpoints == {"esearch.fcgi", "efetch.fcgi"}
    assert {p["db"] for e, p in env.fake.requests} == {"pubmed", "pmc"}
    assert all(p["tool"] and p["email"] for _, p in env.fake.requests)  # parametri comuni


def test_no_document_without_open_access_fulltext_reaches_corpus(env: Env) -> None:
    env.run()
    files = git(env.origin, "ls-tree", "-r", "--name-only", "ingest/2026-09-26-pubmed-1").split()
    assert "corpus/pmid-10000003.md" not in files  # abstract senza full text
    assert "corpus/pmid-10000004.md" not in files  # ritrattato
    assert "corpus/pmid-10000005.md" not in files  # senza PMCID


def test_run_is_deterministic_across_environments(tmp_path: Path) -> None:
    """Stessi input → stessi byte nei file del corpus (indipendente dal percorso del clone)."""
    outputs = []
    for name in ("a", "b"):
        env = Env(tmp_path / name)
        env.run()
        outputs.append(show(env.origin, "ingest/2026-09-26-pubmed-1", DOC1))
    assert outputs[0] == outputs[1]


def test_structured_log_summary(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    from aika_ingestion_pubmed.logging_setup import JsonFormatter

    with caplog.at_level(logging.INFO):
        env.run()
    (summary,) = [r for r in caplog.records if getattr(r, "event", "") == "run_done"]
    payload = json.loads(JsonFormatter().format(summary))
    assert payload["event"] == "run_done"
    assert payload["found"] == 5 and payload["new"] == 2
    assert payload["excluded"] == {"no_open_access_fulltext": 1, "no_pmcid": 1, "retracted": 1}
    assert {"updated", "unchanged", "errors", "pull_requests"} <= payload.keys()


def test_branch_number_skips_names_already_on_remote(env: Env) -> None:
    """Secondo run nello stesso giorno: il branch ``-1`` esiste già sul remoto → si usa ``-2``."""
    git(env.origin, "branch", "ingest/2026-09-26-pubmed-1", "main")
    env.run()
    (pr,) = env.host.created
    assert pr["branch"] == "ingest/2026-09-26-pubmed-2"
    assert "#2" in pr["title"]
    assert branches(env.origin) == ["ingest/2026-09-26-pubmed-1", "ingest/2026-09-26-pubmed-2"]
