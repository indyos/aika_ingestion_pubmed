"""Lo script di revisione del repository corpus (workflow Actions) equivale a ``review_branch``."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from aika_ingestion_pubmed.document import parse_document
from aika_ingestion_pubmed.models import CurationStatus
from aika_ingestion_pubmed.review import _decided  # pyright: ignore[reportPrivateUsage]

from .support import ROOT
from .test_review import _validator

SCRIPT = ROOT / "corpus_ci" / "review.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("review_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Radice con due documenti pending prodotti dal connettore (fixture git dei test)."""
    from .support import show
    from .test_pipeline import DOC1, DOC2, Env

    env = Env(tmp_path / "env")
    env.run()
    root = tmp_path / "root"
    for rel in (DOC1, DOC2):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            show(env.origin, "ingest/2026-09-26-pubmed-1", rel), encoding="utf-8", newline="\n"
        )
    (root / "schema").mkdir()
    from aika_ingestion_pubmed.schema import schema_text

    (root / "schema" / "front_matter.schema.json").write_text(schema_text(), encoding="utf-8")
    return root


def test_script_output_is_byte_identical_to_connector(tree: Path) -> None:
    script = _script()
    original = (tree / "corpus/pmid-10000001.md").read_text(encoding="utf-8")
    expected_ok = _decided(original, CurationStatus.APPROVED, "ok")
    expected_ko = _decided(
        (tree / "corpus/pmid-10000002.md").read_text(encoding="utf-8"),
        CurationStatus.REJECTED,
        "fuori tema: motivo",
    )
    script.review(tree, decision="approve", ids=["pmid-10000001"], notes="ok")
    script.review(tree, decision="reject", ids=["pmid-10000002"], notes="fuori tema: motivo")
    assert (tree / "corpus/pmid-10000001.md").read_text(encoding="utf-8") == expected_ok
    assert (tree / "rejected/pmid-10000002.md").read_text(encoding="utf-8") == expected_ko
    assert not (tree / "corpus/pmid-10000002.md").exists()
    assert parse_document(expected_ok).curation_status == "approved"
    assert _validator().main(["--root", str(tree)]) == 0


def test_all_pending_and_main_cli(tree: Path) -> None:
    script = _script()
    assert script.main(["--root", str(tree), "--decision", "approve", "--ids", "all-pending"]) == 0
    for pmid in ("10000001", "10000002"):
        text = (tree / f"corpus/pmid-{pmid}.md").read_text(encoding="utf-8")
        assert parse_document(text).curation_status == "approved"
    assert _validator().main(["--root", str(tree)]) == 0


def test_script_errors(tree: Path) -> None:
    script = _script()
    err = script.ReviewError
    with pytest.raises(err, match="motivazione"):
        script.review(tree, decision="reject", ids=["pmid-10000001"], notes="")
    with pytest.raises(err, match="non presenti"):
        script.review(tree, decision="approve", ids=["pmid-99999999"], notes=None)
    with pytest.raises(err, match="non validi"):
        script.review(tree, decision="approve", ids=["abc"], notes=None)
    with pytest.raises(err, match="solo con approve"):
        script.review(tree, decision="reject", ids=["all-pending"], notes="x")
    assert script.main(["--root", str(tree), "--decision", "reject", "--ids", "pmid-1"]) == 1
