from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from aika_ingestion_pubmed.config import Config, Secrets, load_config
from aika_ingestion_pubmed.github import GitHubAppHost, GitHubError

from .support import ROOT, make_config

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
PUBLIC_KEY = KEY.public_key()


class FakeGitHub:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.pulls: list[dict[str, Any]] = []
        self.files: dict[int, list[str]] = {}
        self.created: list[dict[str, Any]] = []
        self.now_hint = 1_000_000.0 + 3600  # nessun JWT dal futuro (l'orologio dei test avanza)
        self.fail_pulls = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/app/installations/77/access_tokens":
            self.token_calls += 1
            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                PUBLIC_KEY,
                algorithms=["RS256"],
                options={"verify_exp": False, "verify_iat": False},  # orologio finto nei test
            )
            assert claims["iss"] == "12345"
            assert 0 < claims["exp"] - claims["iat"] <= 10 * 60  # limite di GitHub
            assert claims["iat"] <= self.now_hint
            return httpx.Response(201, json={"token": f"tok-{self.token_calls}"})
        assert request.headers["Authorization"] == f"Bearer tok-{self.token_calls}"
        if method == "GET" and path == "/repos/org/aika-corpus/pulls":
            if self.fail_pulls:
                return httpx.Response(500, text="secret internal detail")
            page = int(request.url.params["page"])
            return httpx.Response(200, json=self.pulls[(page - 1) * 100 : page * 100])
        if method == "GET" and path.endswith("/files"):
            number = int(path.split("/")[-2])
            return httpx.Response(200, json=[{"filename": f} for f in self.files[number]])
        if method == "POST" and path == "/repos/org/aika-corpus/pulls":
            payload = json.loads(request.content)
            self.created.append(payload)
            return httpx.Response(
                201, json={"html_url": "https://github.com/org/aika-corpus/pull/9"}
            )
        return httpx.Response(404)


def make_host(fake: FakeGitHub, clock: list[float] | None = None) -> GitHubAppHost:
    now = clock if clock is not None else [1_000_000.0]
    return GitHubAppHost(
        repo="org/aika-corpus",
        app_id="12345",
        installation_id="77",
        private_key=PRIVATE_PEM,
        transport=httpx.MockTransport(fake.handler),
        clock=lambda: now[0],
    )


def test_open_ingest_pmids_only_from_connector_branches() -> None:
    fake = FakeGitHub()
    fake.pulls = [
        {"number": 1, "head": {"ref": "ingest/2026-09-20-pubmed-1"}},
        {"number": 2, "head": {"ref": "fix/typo"}},  # PR umana: ignorata
        {"number": 3, "head": {"ref": "ingest/2026-09-21-pubmed-1"}},
    ]
    fake.files = {
        1: ["corpus/pmid-111.md", "README.md"],
        2: ["corpus/pmid-999.md"],
        3: ["corpus/pmid-222.md", "other/pmid-333.md"],
    }
    host = make_host(fake)
    assert host.open_ingest_pmids("ingest/", ("corpus",)) == {"111", "222"}


def test_pagination_of_open_pulls() -> None:
    fake = FakeGitHub()
    fake.pulls = [{"number": n, "head": {"ref": "ingest/x"}} for n in range(1, 151)]
    fake.files = {n: [f"corpus/pmid-{n}.md"] for n in range(1, 151)}
    assert len(make_host(fake).open_ingest_pmids("ingest/", ("corpus",))) == 150


def test_create_pull_request_returns_url() -> None:
    fake = FakeGitHub()
    url = make_host(fake).create_pull_request(
        branch="ingest/2026-09-26-pubmed-1", base="main", title="T", body="B"
    )
    assert url == "https://github.com/org/aika-corpus/pull/9"
    assert fake.created == [
        {"title": "T", "head": "ingest/2026-09-26-pubmed-1", "base": "main", "body": "B"}
    ]


def test_installation_token_is_cached_and_refreshed() -> None:
    fake = FakeGitHub()
    clock = [1_000_000.0]
    host = make_host(fake, clock)
    assert host.access_token() == "tok-1"
    assert host.access_token() == "tok-1"
    assert fake.token_calls == 1
    clock[0] += 3600  # scaduto
    assert host.access_token() == "tok-2"


def test_github_errors_do_not_leak_response_body() -> None:
    fake = FakeGitHub()
    fake.fail_pulls = True
    with pytest.raises(GitHubError, match="HTTP 500") as info:
        make_host(fake).open_ingest_pmids("ingest/", ("corpus",))
    assert "secret internal detail" not in str(info.value)


# ------------------------------------------------------------------ config


def test_repository_config_is_valid() -> None:
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.search.max_results == 200
    assert cfg.corpus.max_pr_size == 20
    assert cfg.quality.allowed_languages == ["en", "it"]


def test_configured_query_and_filter() -> None:
    """Query scelta dall'utente il 2026-09-26 (dieta come argomento principale) + subset PMC."""
    search = load_config(ROOT / "config.yaml").search
    for fragment in (
        '"Renal Dialysis"[Mesh]',
        "hemodialysis[TiAb]",
        '"Diet"[Majr]',
        '"Nutritional Status"[Majr]',
        '"Nutrition Therapy"[Majr]',
        "english[la] OR italian[la]",
    ):
        assert fragment in search.query
    assert search.extra_filter == "pubmed pmc[sb]"


def test_config_rejects_unknown_keys_and_bad_values(tmp_path: Path) -> None:
    raw: dict[str, Any] = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    bad = {**raw, "ncbi": {**raw["ncbi"], "surprise": 1}}
    with pytest.raises(ValidationError):
        Config.model_validate(bad)
    bad = {**raw, "search": {**raw["search"], "maxdate": None}}  # mindate senza maxdate
    with pytest.raises(ValidationError, match="insieme"):
        Config.model_validate(bad)
    bad = {**raw, "ncbi": {**raw["ncbi"], "email": "non-una-email"}}
    with pytest.raises(ValidationError):
        Config.model_validate(bad)
    tiers = dict(raw["mapping"]["evidence_tier"])
    del tiers["rct"]
    bad = {**raw, "mapping": {**raw["mapping"], "evidence_tier": tiers}}
    with pytest.raises(ValidationError, match="evidence_tier mancante"):
        Config.model_validate(bad)


def test_secrets_come_only_from_environment(tmp_path: Path) -> None:
    key_file = tmp_path / "key.pem"
    key_file.write_text("PEM", encoding="utf-8")
    secrets = Secrets.from_env(
        {
            "NCBI_API_KEY": "abc",
            "GITHUB_APP_ID": "1",
            "GITHUB_APP_INSTALLATION_ID": "2",
            "GITHUB_APP_PRIVATE_KEY_PATH": str(key_file),
        }
    )
    assert secrets.ncbi_api_key == "abc"
    assert secrets.github_private_key == "PEM"
    assert secrets.has_github_app
    empty = Secrets.from_env({})
    assert empty.ncbi_api_key is None and not empty.has_github_app
    assert "api_key" not in (ROOT / "config.yaml").read_text(encoding="utf-8").lower().replace(
        "ncbi_api_key", ""
    )


def test_make_config_helper_is_valid(tmp_path: Path) -> None:
    assert make_config(tmp_path).ncbi.email == "tester@example.org"
