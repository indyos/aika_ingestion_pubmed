#!/usr/bin/env python3
"""Revisione di documenti nel branch di una PR (da copiare in ``scripts/`` del repository corpus).

Usato dal workflow «Review documenti» (Actions → Run workflow). Lavora sul working tree già in
checkout (``--root``):

* ``approve``: ``curation_status: approved``; il file resta in ``corpus/``;
* ``reject``: ``curation_status: rejected`` (``--notes`` obbligatorio); il file passa a ``rejected/``.

Il front matter è riscritto con la stessa serializzazione del connettore (chiavi nello stesso
ordine, ``review_notes`` subito dopo ``curation_status``); il corpo non cambia, quindi
``content_hash`` resta valido. Dipendenza: ``pyyaml``. Exit code 1 in caso di errore.

**Generalizzato (2026-09-30) da PMID nudo a ``id`` con prefisso** (``pmid-<PMID>``, ``pdf-<slug>``,
``web-<slug>`` — schema v2, vedi ``aika_ingestion_pdf/CLAUDE.md`` e
``aika_ingestion_common/models.py``): accetta qualunque id nel formato dei tre connettori, non solo
PMID numerici. Verificato contro i 2 documenti PMID reali già in ``corpus/`` del repo corpus
(retrocompatibilità v1, non solo assunta) e contro un documento generato dal codice reale di questo
stesso connettore (non solo assunta la retrocompatibilità v1) — non verificato contro un documento
``pdf-*`` reale (nessuno ancora prodotto). Questa copia deve restare sincronizzata con
``aika_doc_ingestion/scripts/review.py`` (il repo corpus), che è la copia effettivamente distribuita.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

FENCE = "---\n"
# Stesso pattern id per i tre connettori (pmid-<PMID>, pdf-<slug>, web-<slug>) — vedi
# aika_ingestion_common/src/aika_ingestion_common/models.py.
ID_PATTERN = re.compile(r"^(pmid-[0-9]+|pdf-[a-z0-9][a-z0-9._-]*|web-[a-z0-9][a-z0-9._-]*)$")
NAME_RE = re.compile(r"^(.+)\.md$")
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


def _ids(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {m.group(1) for p in directory.iterdir() if (m := NAME_RE.match(p.name))}


def review(
    root: Path,
    *,
    decision: str,
    ids: list[str],
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
    in_corpus, in_rejected = _ids(corpus), _ids(rejected)

    if ids == [ALL_PENDING]:
        if decision != "approve":
            raise ReviewError("all-pending si usa solo con approve")
        ids = sorted(
            i
            for i in in_corpus
            if split_document((corpus / f"{i}.md").read_text(encoding="utf-8"))[0].get(
                "curation_status"
            )
            == "pending"
        )
    bad = [i for i in ids if not ID_PATTERN.match(i)]
    if bad:
        raise ReviewError(f"id non validi: {bad}")
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise ReviewError("nessun documento da approvare o rifiutare")
    missing = [i for i in ids if i not in in_corpus]
    if missing:
        raise ReviewError(f"id non presenti in {corpus_dir}/ del branch: {missing}")
    if decision == "reject":
        already = [i for i in ids if i in in_rejected]
        if already:
            raise ReviewError(f"id già presenti in {rejected_dir}/: {already}")

    for id_ in ids:
        source = corpus / f"{id_}.md"
        text = source.read_text(encoding="utf-8")
        if decision == "approve":
            source.write_bytes(decided(text, "approved", notes).encode("utf-8"))
        else:
            rejected.mkdir(parents=True, exist_ok=True)
            (rejected / f"{id_}.md").write_bytes(decided(text, "rejected", notes).encode("utf-8"))
            source.unlink()
    return (ids, []) if decision == "approve" else ([], ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--decision", required=True, choices=["approve", "reject"])
    parser.add_argument(
        "--ids", required=True, help=f"id separati da spazi/virgole, o «{ALL_PENDING}»"
    )
    parser.add_argument("--notes", default="")
    args = parser.parse_args(argv)
    ids = [i for i in re.split(r"[\s,;]+", str(args.ids).strip()) if i]
    try:
        approved, rejected = review(
            args.root, decision=args.decision, ids=ids, notes=str(args.notes)
        )
    except ReviewError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"approvati: {len(approved)}, rifiutati: {len(rejected)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
