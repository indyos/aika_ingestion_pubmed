"""Revisione dei documenti di una PR: approvare (resta in ``corpus/``) o rifiutare (``rejected/``).

Il connettore scrive ``curation_status: pending``. Nella PR il revisore decide file per file; su
``main`` devono arrivare solo documenti ``approved``. I rifiutati si spostano in ``rejected/`` (mai
letta dal chunker) con ``review_notes``: così il PMID non viene riproposto. Il corpo e quindi
``content_hash`` non cambiano mai.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .config import CorpusConfig
from .corpus import CorpusWriter
from .document import parse_document, render_document
from .logging_setup import log_event
from .models import CurationStatus, FrontMatter

logger = logging.getLogger(__name__)


class ReviewError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewResult:
    approved: list[str]
    rejected: list[str]


def _decided(text: str, status: CurationStatus, notes: str | None) -> str:
    parsed = parse_document(text)
    front_matter = FrontMatter.model_validate(parsed.meta)
    update: dict[str, object] = {"curation_status": status}
    if notes:
        update["review_notes"] = notes
    return render_document(front_matter.model_copy(update=update), parsed.body)


def review_branch(
    corpus: CorpusWriter,
    cfg: CorpusConfig,
    branch: str,
    *,
    approve: Sequence[str] = (),
    reject: Sequence[str] = (),
    approve_all_pending: bool = False,
    notes: str | None = None,
    token: str | None = None,
) -> ReviewResult:
    """Applica le decisioni sul branch ``branch`` di una PR, con un commit e un push."""
    if branch == cfg.base_branch:
        raise ReviewError(f"la revisione avviene nel branch della PR, non in {cfg.base_branch}")
    if reject and not notes:
        raise ReviewError("per rifiutare serve una motivazione (--notes)")
    corpus.open_branch(branch, token)
    try:
        in_corpus = corpus.branch_pmids(cfg.corpus_dir)
        in_rejected = corpus.branch_pmids(cfg.rejected_dir)
        to_reject = list(dict.fromkeys(reject))
        to_approve = list(dict.fromkeys(approve))
        if approve_all_pending:
            for pmid in sorted(in_corpus):
                pending = (
                    parse_document(corpus.read_file(corpus.relative_path(pmid))).curation_status
                    == CurationStatus.PENDING.value
                )
                if pending and pmid not in to_reject and pmid not in to_approve:
                    to_approve.append(pmid)
        overlap = set(to_approve) & set(to_reject)
        if overlap:
            raise ReviewError(f"PMID sia approvati sia rifiutati: {sorted(overlap)}")
        missing = [p for p in (*to_approve, *to_reject) if p not in in_corpus]
        if missing:
            raise ReviewError(f"PMID non presenti in {cfg.corpus_dir}/ del branch: {missing}")
        already = [p for p in to_reject if p in in_rejected]
        if already:
            raise ReviewError(f"PMID già presenti in {cfg.rejected_dir}/: {already}")
        if not (to_approve or to_reject):
            raise ReviewError("nessun documento da approvare o rifiutare")

        files: dict[str, str] = {}
        remove: list[str] = []
        for pmid in to_approve:
            rel = corpus.relative_path(pmid)
            files[rel] = _decided(corpus.read_file(rel), CurationStatus.APPROVED, notes)
        for pmid in to_reject:
            rel = corpus.relative_path(pmid)
            files[f"{cfg.rejected_dir}/pmid-{pmid}.md"] = _decided(
                corpus.read_file(rel), CurationStatus.REJECTED, notes
            )
            remove.append(rel)
        message = f"Revisione: {len(to_approve)} approvati, {len(to_reject)} rifiutati"
        corpus.commit_changes(files, remove, message)
        corpus.push(branch, token)
        log_event(
            logger,
            "review_pushed",
            branch=branch,
            approved=len(to_approve),
            rejected=len(to_reject),
        )
        return ReviewResult(approved=to_approve, rejected=to_reject)
    finally:
        corpus.back_to_base()
