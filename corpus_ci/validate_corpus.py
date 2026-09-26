#!/usr/bin/env python3
"""Validazione dei documenti del repository corpus (da copiare in ``scripts/``).

``main`` deve contenere solo documenti **già revisionati**:

* ``corpus/pmid-<PMID>.md``: solo ``curation_status: approved`` (i ``pending`` di una PR di
  ingestion non superano il controllo: il revisore deve prima approvarli o rifiutarli);
* ``rejected/pmid-<PMID>.md``: solo ``rejected``, con ``review_notes`` non vuoto (serve a non
  riproporre il PMID);
* un PMID non può stare in entrambe le cartelle.

In ogni file si controllano inoltre: nome file ``pmid-<PMID>.md``, front matter valido secondo
``schema/front_matter.schema.json``, ``id``/``pmid`` uguali al nome file (il JSON Schema non può
verificarlo), ``pmcid`` presente in ``source_uri``, ``content_hash`` = sha256 del solo corpo, corpo
non vuoto, nessun altro file o sottocartella.

Dipendenze: ``pyyaml`` e ``jsonschema``. Exit code 1 se c'è almeno un errore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import jsonschema
import yaml

FENCE = "---\n"
NAME_RE = re.compile(r"^pmid-([0-9]+)\.md$")


class _Loader(yaml.SafeLoader):
    """SafeLoader senza risoluzione implicita delle date: ``published: 2026-12-05`` resta stringa."""


_Loader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def split_document(text: str) -> tuple[dict[str, Any], str]:
    """Front matter (tipizzato) e corpo; stessa logica di ``parse_document`` del connettore."""
    text = text.replace("\r\n", "\n")
    if not text.startswith(FENCE):
        raise ValueError("front matter mancante")
    end = text.find(f"\n{FENCE}", len(FENCE) - 1)
    if end == -1:
        raise ValueError("front matter non chiuso")
    meta: Any = yaml.load(text[len(FENCE) : end + 1], Loader=_Loader)
    if not isinstance(meta, dict):
        raise ValueError("front matter non è una mappa")
    body = text[end + 1 + len(FENCE) :].removeprefix("\n")
    return meta, body  # pyright: ignore[reportUnknownVariableType]


def validate_file(path: Path, schema: dict[str, Any], *, expected_status: str) -> list[str]:
    match = NAME_RE.match(path.name)
    if not match:
        return ["nome file non conforme a pmid-<PMID>.md"]
    pmid = match.group(1)
    try:
        meta, body = split_document(path.read_text(encoding="utf-8"))
    except (ValueError, yaml.YAMLError, UnicodeDecodeError) as exc:
        return [str(exc)]

    errors = [
        f"schema: {'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
        for e in sorted(
            jsonschema.Draft202012Validator(schema).iter_errors(meta), key=lambda e: list(e.path)
        )
    ]
    if meta.get("id") != f"pmid-{pmid}":
        errors.append(f"id {meta.get('id')!r} diverso da pmid-{pmid} (nome file)")
    if str(meta.get("pmid", pmid)) != pmid:
        errors.append(f"pmid {meta.get('pmid')!r} diverso da {pmid} (nome file)")
    pmcid, source_uri = meta.get("pmcid"), str(meta.get("source_uri", ""))
    if pmcid and str(pmcid) not in source_uri:
        errors.append(f"pmcid {pmcid} non presente in source_uri {source_uri}")
    if not body.strip():
        errors.append("corpo vuoto")
    expected = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    if meta.get("content_hash") != expected:
        errors.append(f"content_hash non corrisponde al corpo (atteso {expected})")
    status = meta.get("curation_status")
    if status != expected_status:
        errors.append(
            f"curation_status {status!r}: in {path.parent.name}/ deve essere {expected_status!r}"
            + (
                " (approvalo o rifiutalo: Actions → Review documenti)"
                if status == "pending"
                else ""
            )
        )
    if expected_status == "rejected" and not str(meta.get("review_notes") or "").strip():
        errors.append("documento rifiutato senza review_notes (motivazione)")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."), help="radice del repository corpus")
    parser.add_argument("--corpus-dir", default="corpus")
    parser.add_argument("--rejected-dir", default="rejected")
    parser.add_argument("--schema", type=Path, default=Path("schema/front_matter.schema.json"))
    args = parser.parse_args(argv)

    root: Path = args.root
    schema: dict[str, Any] = json.loads((root / args.schema).read_text(encoding="utf-8"))
    expected = {args.corpus_dir: "approved", args.rejected_dir: "rejected"}

    failures = checked = 0
    seen: dict[str, str] = {}
    for directory, status in expected.items():
        folder = root / directory
        files = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []
        for path in files:
            checked += 1
            rel = path.relative_to(root).as_posix()
            if path.parent != folder:
                errors = [f"file in sottocartella di {directory}/ non ammesso"]
            else:
                errors = validate_file(path, schema, expected_status=status)
                match = NAME_RE.match(path.name)
                if match and match.group(1) in seen:
                    errors.append(f"PMID già presente in {seen[match.group(1)]}")
                elif match:
                    seen[match.group(1)] = rel
            for message in errors:
                print(f"::error file={rel}::{message}")
                failures += 1
    print(f"{checked} file controllati, {failures} errori")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
