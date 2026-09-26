"""Revisione in PR (approve/reject), regole della CI del corpus e PMID rifiutati."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from aika_ingestion_pubmed.corpus import CorpusWriter
from aika_ingestion_pubmed.document import content_hash, parse_document
from aika_ingestion_pubmed.review import ReviewError, review_branch
from aika_ingestion_pubmed.schema import schema_text

from .support import branches, git, merge_branch_into_main, show
from .test_pipeline import DOC1, DOC2, Env

BRANCH = "ingest/2026-09-26-pubmed-1"
REJ2 = "rejected/pmid-10000002.md"
SCRIPT = Path(__file__).resolve().parent.parent / "corpus_ci" / "validate_corpus.py"


def _validator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_corpus", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(env: Env, tmp_path: Path, files: list[str]) -> Path:
    """Copia i file del branch della PR in una radice temporanea (come farebbe la CI)."""
    root = tmp_path / "ci_root"
    (root / "schema").mkdir(parents=True, exist_ok=True)
    (root / "schema" / "front_matter.schema.json").write_text(schema_text(), encoding="utf-8")
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(show(env.origin, BRANCH, rel), encoding="utf-8", newline="\n")
    return root


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    e.run()
    return e


def _review(env: Env, **kw: object) -> None:
    review_branch(CorpusWriter(env.config.corpus), env.config.corpus, BRANCH, **kw)  # type: ignore[arg-type]


def test_pending_documents_fail_ci_until_reviewed(env: Env, tmp_path: Path) -> None:
    validate = _validator()
    root = _tree(env, tmp_path, [DOC1, DOC2])
    assert validate.main(["--root", str(root)]) == 1  # due file pending


def test_approve_and_reject_produce_a_mergeable_tree(env: Env, tmp_path: Path) -> None:
    _review(env, approve=["10000001"])
    _review(env, reject=["10000002"], notes="fuori tema: non riguarda la dieta")

    doc1 = parse_document(show(env.origin, BRANCH, DOC1))
    assert doc1.curation_status == "approved"
    assert doc1.content_hash == content_hash(doc1.body)  # il corpo non cambia mai

    rejected = parse_document(show(env.origin, BRANCH, REJ2))
    assert rejected.curation_status == "rejected"
    assert rejected.review_notes == "fuori tema: non riguarda la dieta"
    assert DOC2 not in git(env.origin, "ls-tree", "-r", "--name-only", BRANCH)

    root = _tree(env, tmp_path, [DOC1, REJ2])
    assert _validator().main(["--root", str(root)]) == 0
    # il clone resta pulito e staccato dal branch di revisione
    assert git(env.clone, "status", "--porcelain") == ""


def test_approve_all_pending_skips_explicit_rejections(env: Env) -> None:
    _review(env, reject=["10000002"], notes="non pertinente")
    _review(env, approve_all_pending=True)
    assert parse_document(show(env.origin, BRANCH, DOC1)).curation_status == "approved"


def test_review_errors(env: Env) -> None:
    with pytest.raises(ReviewError, match="motivazione"):
        _review(env, reject=["10000002"])
    with pytest.raises(ReviewError, match="non presenti"):
        _review(env, approve=["99999999"])
    with pytest.raises(ReviewError, match="sia approvati sia rifiutati"):
        _review(env, approve=["10000001"], reject=["10000001"], notes="x")
    with pytest.raises(ReviewError, match="nessun documento"):
        _review(env)
    with pytest.raises(ReviewError, match="non in main"):
        review_branch(CorpusWriter(env.config.corpus), env.config.corpus, "main", approve=["1"])
    assert git(env.clone, "status", "--porcelain") == ""


def test_rejected_pmid_is_not_proposed_again(env: Env) -> None:
    _review(env, approve=["10000001"])
    _review(env, reject=["10000002"], notes="non pertinente")
    merge_branch_into_main(env.origin, BRANCH)
    assert branches(env.origin)  # branch della PR ancora presente
    stats = env.run()
    assert stats.excluded["rejected_in_corpus"] == 1  # 10000002
    assert stats.new == 0


def test_ci_rejects_bad_files(env: Env, tmp_path: Path) -> None:
    _review(env, approve=["10000001"])
    _review(env, reject=["10000002"], notes="non pertinente")
    validate = _validator()
    root = _tree(env, tmp_path, [DOC1, REJ2])
    schema = __import__("json").loads((root / "schema/front_matter.schema.json").read_text("utf-8"))
    doc1, rej = root / DOC1, root / REJ2

    text = doc1.read_text(encoding="utf-8")
    doc1.write_text(text.replace("id: pmid-10000001", "id: pmid-7"), encoding="utf-8")
    errors = validate.validate_file(doc1, schema, expected_status="approved")
    assert any("diverso da pmid-10000001" in e for e in errors)
    doc1.write_text(text + "\nmodifica manuale\n", encoding="utf-8")
    assert any(
        "content_hash" in e
        for e in validate.validate_file(doc1, schema, expected_status="approved")
    )
    doc1.write_text(text, encoding="utf-8")

    rtext = rej.read_text(encoding="utf-8")
    rej.write_text(rtext.replace("review_notes: non pertinente\n", ""), encoding="utf-8")
    assert any(
        "review_notes" in e for e in validate.validate_file(rej, schema, expected_status="rejected")
    )
    rej.write_text(rtext, encoding="utf-8")

    # stesso PMID in entrambe le cartelle, file estranei
    (root / "corpus" / "pmid-10000002.md").write_text(rtext, encoding="utf-8")
    (root / "corpus" / "note.txt").write_text("x", encoding="utf-8")
    assert validate.main(["--root", str(root)]) == 1
