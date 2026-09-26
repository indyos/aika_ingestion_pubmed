"""``PubMedSearch``: esearch (con history server) + efetch dei metadati a batch."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .config import SearchConfig
from .eutils import EutilsClient, EutilsError


@dataclass(frozen=True)
class SearchResult:
    pmids: list[str]
    count: int
    webenv: str
    query_key: str


class PubMedSearch:
    def __init__(self, client: EutilsClient, config: SearchConfig) -> None:
        self._client = client
        self._config = config

    def _term(self) -> str:
        if self._config.extra_filter:
            return f"({self._config.query}) AND ({self._config.extra_filter})"
        return self._config.query

    def search(self) -> SearchResult:
        cfg = self._config
        params: dict[str, str | int] = {
            "db": "pubmed",
            "term": self._term(),
            "retmode": "json",
            "retmax": cfg.max_results,
            "usehistory": "y",
            "sort": cfg.sort,
        }
        if cfg.mindate and cfg.maxdate:
            params.update(datetype="pdat", mindate=cfg.mindate, maxdate=cfg.maxdate)
        response = self._client.post("esearch.fcgi", params)
        try:
            payload: Any = json.loads(response.text)
            result: Any = payload["esearchresult"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EutilsError("risposta esearch non valida") from exc
        if "ERROR" in result or "error" in payload:
            raise EutilsError(f"esearch: {result.get('ERROR') or payload.get('error')}")
        try:
            return SearchResult(
                pmids=[str(p) for p in result["idlist"]],
                count=int(result["count"]),
                webenv=str(result["webenv"]),
                query_key=str(result["querykey"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise EutilsError("esearch: campi history mancanti") from exc

    def fetch_metadata_batches(self, result: SearchResult, batch_size: int) -> Iterator[bytes]:
        """``efetch db=pubmed`` sui risultati della history, a batch; solo i primi ``len(pmids)``."""
        total = len(result.pmids)
        for start in range(0, total, batch_size):
            response = self._client.post(
                "efetch.fcgi",
                {
                    "db": "pubmed",
                    "query_key": result.query_key,
                    "WebEnv": result.webenv,
                    "retstart": start,
                    "retmax": min(batch_size, total - start),
                    "retmode": "xml",
                    "rettype": "abstract",
                },
            )
            yield response.content
