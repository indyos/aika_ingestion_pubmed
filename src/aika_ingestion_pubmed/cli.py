"""Riga di comando: ``run`` (un'esecuzione completa) e ``schema`` (JSON Schema del front matter)."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from .config import Secrets, load_config
from .corpus import CorpusWriter
from .eutils import EutilsClient
from .github import GitHubAppHost, PullRequestHost
from .logging_setup import configure_logging, log_event
from .pipeline import Pipeline
from .review import review_branch
from .schema import schema_text

logger = logging.getLogger(__name__)
DEFAULT_SCHEMA_PATH = Path("schema/front_matter.schema.json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aika-ingestion-pubmed", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="esegue un run di ingestion")
    run.add_argument("--config", type=Path, default=Path("config.yaml"))
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="non scrive XML originali, non crea branch né PR; stampa solo i conteggi",
    )

    review = sub.add_parser(
        "review",
        help="approva o rifiuta documenti nel branch di una PR (rifiutati → rejected/)",
    )
    review.add_argument("decision", choices=["approve", "reject"])
    review.add_argument("pmids", nargs="*", help="PMID da approvare o rifiutare")
    review.add_argument(
        "--branch", required=True, help="branch della PR, es. ingest/2026-09-26-pubmed-5"
    )
    review.add_argument("--notes", help="motivazione (obbligatoria per reject)")
    review.add_argument(
        "--all-pending",
        action="store_true",
        help="solo con approve: approva tutti i documenti ancora pending del branch",
    )
    review.add_argument("--config", type=Path, default=Path("config.yaml"))

    schema = sub.add_parser("schema", help="genera il JSON Schema del front matter")
    schema.add_argument("--output", type=Path, default=DEFAULT_SCHEMA_PATH)
    schema.add_argument(
        "--check", action="store_true", help="fallisce se il file su disco non è aggiornato"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    configure_logging()
    if args.command == "schema":
        return _schema(args.output, check=args.check)
    if args.command == "review":
        return _review(args)
    return _run(args.config, dry_run=args.dry_run)


def _schema(output: Path, *, check: bool) -> int:
    text = schema_text()
    if check:
        current = output.read_text(encoding="utf-8") if output.exists() else None
        if current != text:
            print(
                f"{output} non è aggiornato: rigenera con `aika-ingestion-pubmed schema`",
                file=sys.stderr,
            )
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(text.encode("utf-8"))
    return 0


def _review(args: argparse.Namespace) -> int:
    load_dotenv(Path(".env"))
    config = load_config(args.config)
    secrets = Secrets.from_env()
    if args.all_pending and args.decision != "approve":
        print("--all-pending si usa solo con approve", file=sys.stderr)
        return 2
    host: PullRequestHost | None = None
    if secrets.has_github_app:
        host = GitHubAppHost(
            repo=config.corpus.repo,
            app_id=str(secrets.github_app_id),
            installation_id=str(secrets.github_installation_id),
            private_key=str(secrets.github_private_key),
        )
    pmids = cast("list[str]", args.pmids)
    approve = pmids if args.decision == "approve" else []
    reject = pmids if args.decision == "reject" else []
    try:
        result = review_branch(
            CorpusWriter(config.corpus),
            config.corpus,
            cast("str", args.branch),
            approve=approve,
            reject=reject,
            approve_all_pending=bool(args.all_pending),
            notes=cast("str | None", args.notes),
            token=host.access_token() if host else None,
        )
    except Exception as exc:
        logger.error("review fallita: %s", exc)
        return 1
    print(f"approvati: {len(result.approved)}, rifiutati: {len(result.rejected)}")
    return 0


def _run(config_path: Path, *, dry_run: bool) -> int:
    load_dotenv(Path(".env"))  # non sovrascrive le variabili già presenti nell'ambiente
    config = load_config(config_path)
    secrets = Secrets.from_env()
    host: PullRequestHost | None = None
    if secrets.has_github_app and not dry_run:
        host = GitHubAppHost(
            repo=config.corpus.repo,
            app_id=str(secrets.github_app_id),
            installation_id=str(secrets.github_installation_id),
            private_key=str(secrets.github_private_key),
        )
    elif not dry_run:
        log_event(
            logger, "no_github_app", level=logging.WARNING, note="branch pushati senza aprire la PR"
        )
    try:
        with EutilsClient(
            tool=config.ncbi.tool,
            email=config.ncbi.email,
            api_key=secrets.ncbi_api_key,
            timeout_s=config.ncbi.timeout_s,
            max_retries=config.ncbi.max_retries,
        ) as client:
            pipeline = Pipeline(config, client, CorpusWriter(config.corpus), host)
            pipeline.run(dry_run=dry_run)
    except Exception:
        logger.exception("run fallito")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
