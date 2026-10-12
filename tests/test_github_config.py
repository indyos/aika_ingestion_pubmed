from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aika_ingestion_pubmed.config import Config, Secrets, load_config

from .support import ROOT, make_config

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
