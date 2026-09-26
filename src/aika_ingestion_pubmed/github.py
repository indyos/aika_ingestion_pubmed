"""Accesso a GitHub tramite GitHub App (permessi minimi: contents e pull requests in scrittura)."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx
import jwt

API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class PullRequestHost(Protocol):
    """Ciò che il connettore chiede all'host del repository corpus."""

    def open_ingest_pmids(self, branch_prefix: str, dirs: Sequence[str]) -> set[str]:
        """PMID dei file in ``dirs`` toccati da PR aperte con branch che inizia per ``branch_prefix``."""
        ...

    def access_token(self) -> str | None:
        """Token per fetch/push via HTTPS (None se non serve, es. remote locale)."""
        ...

    def create_pull_request(self, *, branch: str, base: str, title: str, body: str) -> str:
        """Apre la PR e ne restituisce l'URL."""
        ...


class GitHubAppHost:
    def __init__(
        self,
        *,
        repo: str,
        app_id: str,
        installation_id: str,
        private_key: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        base_url: str = API_URL,
    ) -> None:
        self._repo = repo
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._clock = clock
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=30.0,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._token: str | None = None
        self._token_expiry = 0.0

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ autenticazione

    def access_token(self) -> str:
        now = self._clock()
        if self._token and now < self._token_expiry - 60:
            return self._token
        app_jwt = jwt.encode(
            {"iat": int(now) - 60, "exp": int(now) + 9 * 60, "iss": self._app_id},
            self._private_key,
            algorithm="RS256",
        )
        data = self._request(
            "POST",
            f"/app/installations/{self._installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}"},
        )
        self._token = str(data["token"])
        self._token_expiry = now + 55 * 60  # i token di installazione durano 1 ora
        return self._token

    # ------------------------------------------------------------------ API

    def open_ingest_pmids(self, branch_prefix: str, dirs: Sequence[str]) -> set[str]:
        alternatives = "|".join(re.escape(d) for d in dirs)
        pattern = re.compile(rf"^(?:{alternatives})/pmid-([0-9]+)\.md$")
        pmids: set[str] = set()
        for pr in self._paginate(f"/repos/{self._repo}/pulls", {"state": "open"}):
            if not str(pr["head"]["ref"]).startswith(branch_prefix):
                continue
            for f in self._paginate(f"/repos/{self._repo}/pulls/{pr['number']}/files", {}):
                m = pattern.match(str(f["filename"]))
                if m:
                    pmids.add(m.group(1))
        return pmids

    def create_pull_request(self, *, branch: str, base: str, title: str, body: str) -> str:
        data = self._request(
            "POST",
            f"/repos/{self._repo}/pulls",
            json={"title": title, "head": branch, "base": base, "body": body},
        )
        return str(data["html_url"])

    # ------------------------------------------------------------------ HTTP

    def _paginate(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            chunk = self._request(
                "GET", path, params={**params, "per_page": "100", "page": str(page)}
            )
            items += chunk
            if len(chunk) < 100:
                return items
            page += 1

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        all_headers = dict(headers or {})
        if "Authorization" not in all_headers:
            all_headers["Authorization"] = f"Bearer {self.access_token()}"
        response = self._http.request(method, path, headers=all_headers, params=params, json=json)
        if response.is_error:
            raise GitHubError(f"GitHub {method} {path}: HTTP {response.status_code}")
        return response.json()
