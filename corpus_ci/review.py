#!/usr/bin/env python3
"""Revisione di documenti nel branch di una PR (da copiare in ``scripts/`` del repository corpus).

Usato dal workflow «Review documenti» (Actions → Run workflow). Lavora sul working tree già in
checkout (``--root``):

* ``approve``: ``curation_status: approved``; il file resta in ``corpus/``;
* ``reject``: ``curation_status: rejected`` (``--notes`` obbligatorio); il file passa a ``rejected/``.

Il front matter è riscritto con la stessa serializzazione del connettore (chiavi nello stesso
ordine, ``review_notes`` subito dopo ``curation_status``); il corpo non cambia, quindi
``content_hash`` resta valido. Dipendenza: ``pyyaml``. Exit code 1 in caso di errore.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

FENCE = "---\n"
NAME_RE = re.compile(r"^pmid-([0-9]+)\.md$")
ALL_PENDING = "all-pending"


class ReviewError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    """SafeLoader senza risoluzione implicita delle date (``published`` resta stringa)."""


_Loader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def split_document(text: str) -> tuple[dict[str, Any], str]:
    text = text.replace("\r\n", "\n")
    if not text.startswith(FENCE):
        raise ReviewError("front matter mancante")
    end = text.find(f"\n{FENCE}", len(FENCE) - 1)
    if end == -1:
        raise ReviewError("front matter non chiuso")
    meta: Any = yaml.load(text[len(FENCE) : end + 1], Loader=_Loader)
    if not isinstance(meta, dict):
        raise ReviewError("front matter non è una mappa")
    body = text[end + 1 + len(FENCE) :].removeprefix("\n")
    return meta, body  # pyright: ignore[reportUnknownVariableType]


def render_document(meta: dict[str, Any], body: str) -> str:
    """Stessa serializzazione del connettore (``document.render_document``)."""
    yaml_text = yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1_000_000
    )
    return f"{FENCE}{yaml_text}{FENCE}\n{body}"


def decided(text: str, status: str, notes: str | None) -> str:
    meta, body = split_document(text)
    out: dict[str, Any] = {}
    for key, value in meta.items():
        if key == "review_notes":
            continue  # riscritto subito dopo curation_status
        out[key] = status if key == "curation_status" else value
        if key == "curation_status":
            previous = meta.get("review_notes")
            new_notes = notes or previous
            if new_notes:
                out["review_notes"] = new_notes
    return render_document(out, body)


def _pmids(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {m.group(1) for p in directory.iterdir() if (m := NAME_RE.match(p.name))}


def review(
    root: Path,
    *,
    decision: str,
    pmids: list[str],
    notes: str | None,
    corpus_dir: str = "corpus",
    rejected_dir: str = "rejected",
) -> tuple[list[str], list[str]]:
    """Applica la decisione; restituisce (approvati, rifiutati)."""
    notes = (notes or "").strip() or None
    if decision not in ("approve", "reject"):
        raise ReviewError(f"decisione non valida: {decision!r}")
    if decision == "reject" and not notes:
        raise ReviewError("per rifiutare serve una motivazione (notes)")
    corpus, rejected = root / corpus_dir, root / rejected_dir
    in_corpus, in_rejected = _pmids(corpus), _pmids(rejected)

    if pmids == [ALL_PENDING]:
        if decision != "approve":
            raise ReviewError("all-pending si usa solo con approve")
        pmids = sorted(
            p
            for p in in_corpus
            if split_document((corpus / f"pmid-{p}.md").read_text(encoding="utf-8"))[0].get(
                "curation_status"
            )
            == "pending"
        )
    bad = [p for p in pmids if not p.isdigit()]
    if bad:
        raise ReviewError(f"PMID non validi: {bad}")
    pmids = list(dict.fromkeys(pmids))
    if not pmids:
        raise ReviewError("nessun documento da approvare o rifiutare")
    missing = [p for p in pmids if p not in in_corpus]
    if missing:
        raise ReviewError(f"PMID non presenti in {corpus_dir}/ del branch: {missing}")
    if decision == "reject":
        already = [p for p in pmids if p in in_rejected]
        if already:
            raise ReviewError(f"PMID già presenti in {rejected_dir}/: {already}")

    for pmid in pmids:
        source = corpus / f"pmid-{pmid}.md"
        text = source.read_text(encoding="utf-8")
        if decision == "approve":
            source.write_bytes(decided(text, "approved", notes).encode("utf-8"))
        else:
            rejected.mkdir(parents=True, exist_ok=True)
            (rejected / f"pmid-{pmid}.md").write_bytes(
                decided(text, "rejected", notes).encode("utf-8")
            )
            source.unlink()
    return (pmids, []) if decision == "approve" else ([], pmids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--decision", required=True, choices=["approve", "reject"])
    parser.add_argument(
        "--pmids", required=True, help=f"PMID separati da spazi/virgole, o «{ALL_PENDING}»"
    )
    parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    pmids = [p for p in re.split(r"[\s,;]+", str(args.pmids).strip()) if p]
    try:
        approved, rejected = review(
            args.root, decision=args.decision, pmids=pmids, notes=str(args.notes)
        )
    except ReviewError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"approvati: {len(approved)}, rifiutati: {len(rejected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
