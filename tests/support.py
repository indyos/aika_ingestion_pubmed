"""Strumenti condivisi dai test: finto NCBI, repository git temporanei, finto host GitHub."""

from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qsl

import httpx
import yaml
from lxml import etree

from aika_ingestion_pubmed.config import Config
from aika_ingestion_pubmed.eutils import EutilsClient

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_fixture(rel: str) -> bytes:
    return (FIXTURES / rel).read_bytes()


def make_config(tmp_path: Path, *, allow_nc: bool = False, **corpus_overrides: Any) -> Config:
    """Config di test: quella del repository, con percorsi temporanei e soglie adatte alle fixture."""
    raw: dict[str, Any] = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    raw["ncbi"]["email"] = "tester@example.org"
    raw["search"]["mindate"] = None
    raw["search"]["maxdate"] = None
    raw["quality"]["min_body_chars"] = 200
    if allow_nc:
        raw["quality"]["allowed_licenses"].append("CC-BY-NC-4.0")
    raw["corpus"].update(
        repo="org/aika-corpus", local_path=str(tmp_path / "clone"), **corpus_overrides
    )
    raw["originals"] = {"dir": str(tmp_path / "originals"), "base_uri": "gs://bucket/pubmed"}
    return Config.model_validate(raw)


# ---------------------------------------------------------------------------- finto NCBI


class FakeNcbi:
    """Simula esearch/efetch su fixture locali. Registra ogni richiesta."""

    PMIDS: ClassVar[list[str]] = ["10000001", "10000002", "10000003", "10000004", "10000005"]

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.pubmed = etree.fromstring(read_fixture("pubmed/pubmed_batch.xml"))
        self.pmc: dict[str, bytes] = {}
        for name in ("guideline_structured", "review_edge_cases", "no_body"):
            root = etree.fromstring(read_fixture(f"jats/{name}.xml"))
            for art in root.iter("article"):
                pmcid = "".join(
                    e.text or ""
                    for e in art.iterfind("front/article-meta/article-id")
                    if e.get("pub-id-type") in ("pmcid", "pmc")
                )
                self.pmc[pmcid.removeprefix("PMC")] = etree.tostring(art)
        self.statuses: list[int] = []  # codici HTTP da restituire prima delle risposte vere

    # -- manipolazione delle fixture "upstream"

    def edit_article(self, numeric_id: str, old: str, new: str) -> None:
        data = self.pmc[numeric_id].decode("utf-8")
        assert old in data
        self.pmc[numeric_id] = data.replace(old, new).encode("utf-8")

    # -- trasporto

    def client(self, **kwargs: Any) -> EutilsClient:
        return EutilsClient(
            tool="aika_test",
            email="tester@example.org",
            transport=httpx.MockTransport(self.handler),
            sleep=lambda _s: None,
            **kwargs,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "eutils.ncbi.nlm.nih.gov", request.url
        params = dict(parse_qsl(request.content.decode()))
        endpoint = request.url.path.rsplit("/", 1)[-1]
        self.requests.append((endpoint, params))
        if self.statuses:
            return httpx.Response(self.statuses.pop(0))
        if endpoint == "esearch.fcgi":
            return httpx.Response(
                200,
                json={
                    "esearchresult": {
                        "count": str(len(self.PMIDS)),
                        "idlist": self.PMIDS[: int(params["retmax"])],
                        "webenv": "WEBENV_1",
                        "querykey": "1",
                    }
                },
            )
        assert endpoint == "efetch.fcgi"
        if params["db"] == "pubmed":
            start, count = int(params["retstart"]), int(params["retmax"])
            root = etree.Element("PubmedArticleSet")
            for art in list(self.pubmed.iter("PubmedArticle"))[start : start + count]:
                root.append(copy.deepcopy(art))
            return httpx.Response(200, content=etree.tostring(root))
        assert params["db"] == "pmc"
        out = b"<pmc-articleset>"
        for numeric in params["id"].split(","):
            out += self.pmc.get(numeric, b"")
        return httpx.Response(200, content=out + b"</pmc-articleset>")

    def calls(self, endpoint: str, db: str | None = None) -> list[dict[str, str]]:
        return [p for e, p in self.requests if e == endpoint and (db is None or p.get("db") == db)]


# ---------------------------------------------------------------------------- git


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=tester@example.org",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("utf-8")


def make_corpus_repos(tmp_path: Path) -> tuple[Path, Path]:
    """Crea ``origin`` (bare) e ``clone`` con un commit iniziale su ``main``."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--quiet", "--bare", "-b", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "-c", "core.autocrlf=false", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    git(clone, "checkout", "--quiet", "-B", "main")
    (clone / "corpus").mkdir()
    (clone / "corpus" / ".gitkeep").write_text("", encoding="utf-8")
    git(clone, "add", "--all")
    git(clone, "commit", "--quiet", "-m", "init")
    git(clone, "push", "--quiet", "origin", "main")
    return origin, clone


def merge_branch_into_main(origin: Path, branch: str) -> None:
    """Simula il merge di una PR: avanza ``main`` di origin al branch (fast-forward)."""
    git(origin, "update-ref", "refs/heads/main", f"refs/heads/{branch}")


def commit_on_main(origin: Path, tmp_path: Path, rel: str, text: str) -> None:
    """Un commit diretto su ``main`` di origin (es. modifica del revisore)."""
    work = tmp_path / "reviewer"
    if work.exists():
        git(work, "fetch", "--quiet", "origin", "main")
        git(work, "checkout", "--quiet", "-B", "main", "origin/main")
    else:
        subprocess.run(
            ["git", "clone", "--quiet", "-c", "core.autocrlf=false", str(origin), str(work)],
            check=True,
            capture_output=True,
        )
    (work / rel).write_bytes(text.encode("utf-8"))
    git(work, "add", "--all")
    git(work, "commit", "--quiet", "-m", f"review {rel}")
    git(work, "push", "--quiet", "origin", "main")


def show(origin: Path, ref: str, rel: str) -> str:
    return git(origin, "show", f"{ref}:{rel}")


def branches(origin: Path) -> list[str]:
    out = git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return sorted(b for b in out.split() if b != "main")


# ---------------------------------------------------------------------------- finto host


class FakeHost:
    def __init__(self, open_pmids: set[str] | None = None) -> None:
        self.open_pmids = open_pmids or set()
        self.created: list[dict[str, str]] = []
        self.fail: Callable[[], None] | None = None

    def open_ingest_pmids(self, branch_prefix: str, dirs: Sequence[str]) -> set[str]:
        return set(self.open_pmids)

    def access_token(self) -> str | None:
        return None

    def create_pull_request(self, *, branch: str, base: str, title: str, body: str) -> str:
        self.created.append({"branch": branch, "base": base, "title": title, "body": body})
        return f"https://github.com/org/aika-corpus/pull/{len(self.created)}"


def load_json(text: str) -> Any:
    return json.loads(text)
