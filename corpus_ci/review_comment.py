#!/usr/bin/env python3
"""Decisioni di revisione da commenti sui file di una PR (da copiare in ``scripts/``).

Il revisore commenta un documento in «Files changed» con ``/approve`` oppure ``/reject motivo…``.
Il workflow ``review-comment.yml`` parte in due casi e passa a questo script il payload dell'evento:

* ``pull_request_review_comment`` (commento singolo): si elabora quel commento;
* ``pull_request_review`` (review inviata con «Finish your review»): si leggono dall'API tutti i
  commenti di quella review e si elabora ogni comando.

Ogni comando vale per il file commentato. Le decisioni sono idempotenti: ripeterle (o riceverle
da entrambi gli eventi) non cambia nulla e non produce risposte in più. Le risposte per il
revisore sono scritte in ``--results`` (JSON) e pubblicate dal workflow.

Exit code: 0 = tutto ok o nessun comando; 1 = almeno un comando non eseguibile.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from review import ReviewError, review, split_document

COMMAND = re.compile(r"^/(approve|reject)\b[ \t]*(.*)\Z", re.IGNORECASE | re.DOTALL)
FILE_PATH = re.compile(r"^corpus/pmid-([0-9]+)\.md$")
ALLOWED = {"OWNER", "MEMBER", "COLLABORATOR"}
API = "https://api.github.com"

type Fetch = Callable[[str], list[dict[str, Any]]]


def parse_comment(body: str, path: str) -> tuple[str, str, str] | None:
    """(decisione, PMID, motivazione) se il commento è un comando valido, None se non lo è."""
    match = COMMAND.match(body.strip())
    if not match:
        return None
    file_match = FILE_PATH.match(path)
    if not file_match:
        raise ReviewError(
            f"il comando va scritto su un documento in corpus/ (commento su «{path}»)"
        )
    return match.group(1).lower(), file_match.group(1), match.group(2).strip()


def _status(path: Path) -> str | None:
    if not path.is_file():
        return None
    return str(split_document(path.read_text(encoding="utf-8"))[0].get("curation_status"))


def apply_comment(root: Path, comment: dict[str, Any]) -> dict[str, Any] | None:
    """Esegue un commento. None se non è un comando; altrimenti {id, ok, changed, reply}."""
    body, path = str(comment.get("body") or ""), str(comment.get("path") or "")
    result: dict[str, Any] = {"id": comment.get("id"), "ok": True, "changed": False, "reply": None}
    try:
        parsed = parse_comment(body, path)
        if parsed is None:
            return None
        if comment.get("author_association") not in ALLOWED:
            raise ReviewError("solo proprietario, membri e collaboratori possono decidere")
        decision, pmid, notes = parsed
        in_corpus = _status(root / f"corpus/pmid-{pmid}.md")
        in_rejected = _status(root / f"rejected/pmid-{pmid}.md")
        if decision == "approve" and in_corpus == "approved":
            return result  # già fatto: nessuna risposta
        if decision == "reject" and in_rejected == "rejected":
            return result
        if decision == "approve" and in_rejected is not None:
            raise ReviewError(f"PMID {pmid} è già stato rifiutato (rejected/)")
        review(root, decision=decision, pmids=[pmid], notes=notes or None)
        result["changed"] = True
        result["reply"] = (
            f"✅ PMID {pmid} approvato."
            if decision == "approve"
            else f"🚫 PMID {pmid} rifiutato e spostato in `rejected/` — motivo: {notes}"
        )
    except ReviewError as exc:
        result.update(ok=False, reply=f"Non applicato: {exc}")
    return result


def github_fetch(token: str) -> Fetch:
    def fetch(path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            request = urllib.request.Request(
                f"{API}{path}{sep}per_page=100&page={page}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                chunk: list[dict[str, Any]] = json.load(response)
            items += chunk
            if len(chunk) < 100:
                return items
            page += 1

    return fetch


def comments_of_event(
    name: str, event: dict[str, Any], repo: str, fetch: Fetch
) -> list[dict[str, Any]]:
    """Commenti da elaborare per l'evento ricevuto (vuoto se l'evento non è pertinente)."""
    pull = event.get("pull_request") or {}
    head = pull.get("head") or {}
    if (head.get("repo") or {}).get("full_name") != repo:
        return []  # PR da fork: mai
    if not str(head.get("ref", "")).startswith("ingest/"):
        return []
    if name == "pull_request_review_comment":
        return [event["comment"]]
    if name == "pull_request_review":
        return fetch(
            f"/repos/{repo}/pulls/{pull['number']}/reviews/{event['review']['id']}/comments"
        )
    return []


def main(argv: list[str] | None = None, fetch: Fetch | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--event-file", type=Path, required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--repo", required=True, help="owner/nome del repository")
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args(argv)

    event: dict[str, Any] = json.loads(args.event_file.read_text(encoding="utf-8"))
    getter = fetch or github_fetch(os.environ.get("GH_TOKEN", ""))
    comments = comments_of_event(str(args.event_name), event, str(args.repo), getter)
    results = [r for c in comments if (r := apply_comment(args.root, c)) is not None]
    args.results.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    for r in results:
        print(f"commento {r['id']}: ok={r['ok']} changed={r['changed']} {r['reply'] or ''}")
    print(f"{len(comments)} commenti esaminati, {len(results)} comandi")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
