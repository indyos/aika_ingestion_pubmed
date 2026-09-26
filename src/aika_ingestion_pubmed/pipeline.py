"""Flusso di un run: ricerca → metadati → filtri → full text OA → Markdown → PR."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .builder import FrontMatterBuilder, pubmed_url
from .config import Config
from .corpus import Change, CorpusWriter
from .document import content_hash, render_document
from .eutils import EutilsClient
from .github import PullRequestHost
from .jats import JatsToMarkdown
from .logging_setup import log_event, log_fields
from .models import CurationStatus, PmcArticle, PubMedRecord
from .pmc import PmcFullTextFetcher
from .pubmed_parser import PubMedMetadataParser
from .quality import QualityGate
from .search import PubMedSearch
from .xmlutil import parse_xml

logger = logging.getLogger(__name__)


@dataclass
class RunStats:
    found: int = 0
    excluded: Counter[str] = field(default_factory=lambda: Counter[str]())
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    pull_requests: list[str] = field(default_factory=lambda: [])

    def as_log(self) -> dict[str, object]:
        return {
            "found": self.found,
            "excluded": dict(sorted(self.excluded.items())),
            "excluded_total": sum(self.excluded.values()),
            "new": self.new,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "pull_requests": self.pull_requests,
        }


@dataclass(frozen=True)
class _Prepared:
    change: Change
    record: PubMedRecord
    original_xml: bytes


class Pipeline:
    def __init__(
        self,
        config: Config,
        client: EutilsClient,
        corpus: CorpusWriter,
        host: PullRequestHost | None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cfg = config
        self._client = client
        self._corpus = corpus
        self._host = host
        self._now = now
        self._search = PubMedSearch(client, config.search)
        self._parser = PubMedMetadataParser()
        self._pmc = PmcFullTextFetcher(client, config.ncbi.batch_size)
        self._jats = JatsToMarkdown()
        self._builder = FrontMatterBuilder(config.mapping)
        self._gate = QualityGate(config.quality)
        self._rejected: set[str] = set()

    # ------------------------------------------------------------------ run

    def run(self, *, dry_run: bool = False) -> RunStats:
        stats = RunStats()
        token = self._host.access_token() if self._host else None
        self._corpus.fetch(token)
        existing = self._corpus.existing_pmids()
        self._rejected = self._corpus.rejected_pmids()
        in_open_pr = (
            self._host.open_ingest_pmids(
                self._cfg.corpus.branch_prefix,
                (self._cfg.corpus.corpus_dir, self._cfg.corpus.rejected_dir),
            )
            if self._host
            else set[str]()
        )

        result = self._search.search()
        stats.found = len(result.pmids)
        log_event(logger, "search_done", found=stats.found, total_matches=result.count)

        prepared: list[_Prepared] = []
        seen: set[str] = set()
        for xml in self._search.fetch_metadata_batches(result, self._cfg.ncbi.batch_size):
            candidates = self._filter(self._parser.parse(xml), existing, in_open_pr, seen, stats)
            prepared += self._process(candidates, existing, stats)

        prepared.sort(key=lambda p: (p.change.front_matter.evidence_tier, p.change.pmid))
        stats.new = sum(p.change.status == "new" for p in prepared)
        stats.updated = sum(p.change.status == "updated" for p in prepared)

        if prepared and not dry_run:
            self._save_originals(prepared)
            stats.pull_requests = self._open_pull_requests(prepared, token)
        log_fields(logger, "run_done", {"dry_run": dry_run, **stats.as_log()})
        return stats

    # ------------------------------------------------------------------ fasi

    def _filter(
        self,
        records: Iterable[PubMedRecord],
        existing: set[str],
        in_open_pr: set[str],
        seen: set[str],
        stats: RunStats,
    ) -> list[PubMedRecord]:
        """Esclusioni che non richiedono il full text."""
        kept: list[PubMedRecord] = []
        for record in records:
            if record.pmid in seen:
                continue
            seen.add(record.pmid)
            reason = self._exclusion(record, existing, in_open_pr, stats)
            if reason is None:
                kept.append(record)
            else:
                stats.excluded[reason] += 1
                log_event(logger, "excluded", pmid=record.pmid, reason=reason)
        return kept

    def _exclusion(
        self, record: PubMedRecord, existing: set[str], in_open_pr: set[str], stats: RunStats
    ) -> str | None:
        if record.retracted:
            return "retracted"
        if record.pmid in self._rejected:
            return "rejected_in_corpus"
        try:
            if record.pmid in existing:
                status = self._corpus.read(record.pmid).curation_status
                if status == CurationStatus.REJECTED.value:
                    return "rejected_in_corpus"
        except Exception as exc:  # documento illeggibile: non blocca il run
            stats.errors += 1
            log_event(
                logger,
                "error",
                pmid=record.pmid,
                stage="read_corpus",
                error=str(exc),
                level=logging.ERROR,
            )
            return "error_read_corpus"
        if record.pmid in in_open_pr:
            return "in_open_pr"
        if not record.pmcid:
            return "no_pmcid"
        return None

    def _process(
        self, records: list[PubMedRecord], existing: set[str], stats: RunStats
    ) -> list[_Prepared]:
        if not records:
            return []
        pmcids = [r.pmcid for r in records if r.pmcid]
        articles = self._pmc.fetch(pmcids)
        out: list[_Prepared] = []
        for record in records:
            article = articles.get(record.pmcid or "")
            if article is None or not article.has_body:
                stats.excluded["no_open_access_fulltext"] += 1
                log_event(logger, "excluded", pmid=record.pmid, reason="no_open_access_fulltext")
                continue
            try:
                prepared = self._prepare(record, article, existing, stats)
            except Exception as exc:  # un errore su un documento non ferma il run
                stats.errors += 1
                log_event(
                    logger,
                    "error",
                    pmid=record.pmid,
                    stage="convert",
                    error=str(exc),
                    level=logging.ERROR,
                )
                continue
            if prepared is not None:
                out.append(prepared)
        return out

    def _prepare(
        self, record: PubMedRecord, article: PmcArticle, existing: set[str], stats: RunStats
    ) -> _Prepared | None:
        root = parse_xml(article.xml)
        title = self._title(record, JatsToMarkdown.extract_title(root))
        body = self._jats.convert(root, title=title, fallback_abstract=record.abstract)
        previous = self._corpus.read(record.pmid) if record.pmid in existing else None
        draft = self._builder.build(
            record,
            pmcid=article.pmcid,
            title=title,
            body=body,
            license_raw=article.license_raw,
            original_uri=self._original_uri(record.pmid),
            retrieved_at=self._now(),
            review_notes=previous.review_notes if previous else None,
        )
        gate = self._gate.check(draft, body)
        if not gate.ok or gate.front_matter is None:
            for reason in gate.reasons:
                stats.excluded[f"quality:{reason}"] += 1
            log_event(
                logger, "excluded", pmid=record.pmid, reason="quality_gate", details=gate.reasons
            )
            return None
        if previous is not None and previous.content_hash == content_hash(body):
            stats.unchanged += 1
            log_event(logger, "unchanged", pmid=record.pmid)
            return None
        status = "updated" if previous is not None else "new"
        change = Change(gate.front_matter, render_document(gate.front_matter, body), status)
        return _Prepared(change, record, article.xml)

    @staticmethod
    def _title(record: PubMedRecord, jats_title: str | None) -> str:
        if jats_title:
            return jats_title
        title = record.title.strip()
        if title.startswith("[") and title.endswith("]."):  # titolo tradotto tra parentesi
            title = title[1:-2]
        return title.removesuffix(".")

    def _original_uri(self, pmid: str) -> str:
        cfg = self._cfg.originals
        name = f"pmid-{pmid}.xml"
        if cfg.base_uri:
            return f"{cfg.base_uri.rstrip('/')}/{name}"
        return (cfg.dir.resolve() / name).as_uri()

    def _save_originals(self, prepared: list[_Prepared]) -> None:
        directory = self._cfg.originals.dir
        directory.mkdir(parents=True, exist_ok=True)
        for p in prepared:
            Path(directory / f"pmid-{p.record.pmid}.xml").write_bytes(p.original_xml)

    # ------------------------------------------------------------------ PR

    def _open_pull_requests(self, prepared: list[_Prepared], token: str | None) -> list[str]:
        cfg = self._cfg.corpus
        today = self._now().strftime("%Y-%m-%d")
        urls: list[str] = []
        stem = f"{cfg.branch_prefix}{today}-pubmed-"
        taken = self._corpus.remote_branches(stem, token)  # run precedenti dello stesso giorno
        n = 0
        try:
            for start in range(0, len(prepared), cfg.max_pr_size):
                batch = [p.change for p in prepared[start : start + cfg.max_pr_size]]
                n += 1
                while f"{stem}{n}" in taken:
                    n += 1
                branch = f"{stem}{n}"
                files = {self._corpus.relative_path(c.pmid): c.text for c in batch}
                title = f"Ingestion PubMed {today} #{n}: {len(batch)} documenti"
                self._corpus.commit_branch(branch, files, title)
                self._corpus.push(branch, token)
                if self._host is None:
                    log_event(logger, "branch_pushed_no_host", branch=branch)
                    continue
                url = self._host.create_pull_request(
                    branch=branch, base=cfg.base_branch, title=title, body=pr_body(batch)
                )
                urls.append(url)
                log_event(
                    logger, "pull_request_opened", branch=branch, url=url, documents=len(batch)
                )
        finally:
            self._corpus.back_to_base()
        return urls


def pr_body(changes: list[Change]) -> str:
    """Descrizione della PR: tabella con id, titolo, doc_type, modality, topics, stato, link."""
    lines = [
        "| id | titolo | doc_type | modality | topics | stato | PubMed |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in changes:
        fm = c.front_matter
        cells = [
            f"`{fm.id}`",
            fm.title,
            fm.doc_type.value,
            ", ".join(m.value for m in fm.modality),
            ", ".join(fm.topics or []),
            c.status,
            f"[{c.pmid}]({pubmed_url(c.pmid)})",
        ]
        lines.append("| " + " | ".join(x.replace("|", "\\|") for x in cells) + " |")
    lines += [
        "",
        "Tutti i documenti sono in stato `pending`: servono revisione e approvazione clinica.",
    ]
    return "\n".join(lines)
