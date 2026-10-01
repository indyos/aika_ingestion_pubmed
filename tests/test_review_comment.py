"""Comandi ``/approve`` e ``/reject`` scritti come commento sul file di una PR."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from aika_ingestion_pubmed.document import parse_document

from .support import ROOT
from .test_review_script import tree as tree

CI = ROOT / "corpus_ci"
REPO = "org/corpus"
DOC1, DOC2 = "corpus/pmid-10000001.md", "corpus/pmid-10000002.md"


def _comment() -> ModuleType:
    sys.path.insert(0, str(CI))  # lo script importa `review` dalla propria cartella
    try:
        spec = importlib.util.spec_from_file_location("review_comment", CI / "review_comment.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(CI))


def _c(cid: int, body: str, path: str, assoc: str = "OWNER") -> dict[str, Any]:
    return {"id": cid, "body": body, "path": path, "author_association": assoc}


def _event(**extra: Any) -> dict[str, Any]:
    pull = {"number": 6, "head": {"ref": "ingest/2026-09-26-pubmed-6", "repo": {"full_name": REPO}}}
    return {"pull_request": pull, **extra}


def _run(
    m: ModuleType, tree: Path, tmp: Path, name: str, event: dict[str, Any], comments: list[Any]
) -> tuple[int, list[dict[str, Any]]]:
    event_file, results = tmp / "event.json", tmp / "results.json"
    event_file.write_text(json.dumps(event), encoding="utf-8")
    code = m.main(
        [
            "--root", str(tree), "--event-file", str(event_file), "--event-name", name,
            "--repo", REPO, "--results", str(results),
        ],
        fetch=lambda _path: comments,
    )  # fmt: skip
    return code, json.loads(results.read_text(encoding="utf-8"))


def _status(tree: Path, rel: str) -> str | None:
    return parse_document((tree / rel).read_text(encoding="utf-8")).curation_status


def test_parse_comment() -> None:
    m = _comment()
    assert m.parse_comment("/approve", DOC1) == ("approve", "pmid-10000001", "")
    assert m.parse_comment("  /APPROVE ottimo", DOC1) == ("approve", "pmid-10000001", "ottimo")
    assert m.parse_comment("/reject fuori tema\nnon parla di dieta", DOC1) == (
        "reject",
        "pmid-10000001",
        "fuori tema\nnon parla di dieta",
    )
    assert m.parse_comment("mi sembra ok", DOC1) is None
    assert m.parse_comment("/approvedbut", DOC1) is None  # non è il comando
    with pytest.raises(m.ReviewError, match="documento in corpus/"):
        m.parse_comment("/approve", "README.md")


def test_single_comment_event_applies_to_the_commented_file(tree: Path, tmp_path: Path) -> None:
    m = _comment()
    ev = _event(comment=_c(11, "/approve", DOC1))
    code, results = _run(m, tree, tmp_path, "pull_request_review_comment", ev, [])
    assert code == 0 and results == [
        {"id": 11, "ok": True, "changed": True, "reply": "✅ `pmid-10000001` approvato."}
    ]
    assert _status(tree, DOC1) == "approved"
    assert _status(tree, DOC2) == "pending"  # gli altri file non cambiano

    ev = _event(comment=_c(12, "/reject fuori tema", DOC2))
    code, results = _run(m, tree, tmp_path, "pull_request_review_comment", ev, [])
    assert code == 0 and results[0]["changed"] is True
    rejected = parse_document((tree / "rejected/pmid-10000002.md").read_text(encoding="utf-8"))
    assert rejected.curation_status == "rejected" and rejected.review_notes == "fuori tema"
    assert not (tree / DOC2).exists()


def test_submitted_review_processes_every_command_comment(tree: Path, tmp_path: Path) -> None:
    m = _comment()
    comments = [
        _c(21, "/approve", DOC1),
        _c(22, "/reject non pertinente", DOC2),
        _c(23, "bel lavoro, ma controlla la tabella", DOC1),  # commento normale: ignorato
    ]
    ev = _event(review={"id": 99})
    code, results = _run(m, tree, tmp_path, "pull_request_review", ev, comments)
    assert code == 0 and [r["id"] for r in results] == [21, 22]
    assert _status(tree, DOC1) == "approved"
    assert _status(tree, "rejected/pmid-10000002.md") == "rejected"


def test_repeating_a_decision_is_silent(tree: Path, tmp_path: Path) -> None:
    """Stesso commento ricevuto da entrambi gli eventi: la seconda volta nessun cambio né risposta."""
    m = _comment()
    ev = _event(comment=_c(31, "/approve", DOC1))
    _run(m, tree, tmp_path, "pull_request_review_comment", ev, [])
    code, results = _run(m, tree, tmp_path, "pull_request_review_comment", ev, [])
    assert code == 0
    assert results == [{"id": 31, "ok": True, "changed": False, "reply": None}]


def test_errors_and_guards(tree: Path, tmp_path: Path) -> None:
    m = _comment()

    def run(ev: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        return _run(m, tree, tmp_path, "pull_request_review_comment", ev, [])

    code, results = run(_event(comment=_c(41, "/reject", DOC1)))  # senza motivazione
    assert code == 1 and "motivazione" in results[0]["reply"]
    code, results = run(_event(comment=_c(42, "/approve", "README.md")))
    assert code == 1 and "documento in corpus/" in results[0]["reply"]
    code, results = run(_event(comment=_c(43, "/approve", DOC1, assoc="NONE")))
    assert code == 1 and "collaboratori" in results[0]["reply"]
    assert _status(tree, DOC1) == "pending"

    fork = _event(comment=_c(44, "/approve", DOC1))
    fork["pull_request"]["head"]["repo"]["full_name"] = "someone/fork"
    assert run(fork) == (0, [])  # PR da fork: ignorata
    other = _event(comment=_c(45, "/approve", DOC1))
    other["pull_request"]["head"]["ref"] = "feature/x"
    assert run(other) == (0, [])  # branch non di ingestion
    assert _status(tree, DOC1) == "pending"


def test_workflow_is_valid_and_safe() -> None:
    workflow = yaml.safe_load((CI / "review-comment.yml").read_text(encoding="utf-8"))
    triggers = workflow[True]
    assert triggers["pull_request_review_comment"] == {"types": ["created"]}
    assert triggers["pull_request_review"] == {"types": ["submitted"]}
    assert "ingest/" in workflow["jobs"]["review"]["if"]
    # il testo dei commenti non entra mai in uno script di shell né nel messaggio di commit
    text = (CI / "review-comment.yml").read_text(encoding="utf-8")
    assert "github.event.comment.body" not in text and "github.event.review.body" not in text
