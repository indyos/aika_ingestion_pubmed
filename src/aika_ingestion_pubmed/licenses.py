"""Normalizzazione delle licenze (es. URL Creative Commons → ``CC-BY-4.0``)."""

from __future__ import annotations

import re
from typing import Final

_CC_URL: Final = re.compile(
    r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/(\d\.\d)", re.IGNORECASE
)
_CC_TEXT: Final = re.compile(
    r"\bCC[\s-]?(0|BY(?:[\s-]?(?:NC|ND|SA))*)(?:[\s-]+(\d\.\d))?\b", re.IGNORECASE
)


def normalize_license(raw: str | None) -> str | None:
    """Forma normalizzata della licenza, o None se non riconosciuta."""
    if not raw:
        return None
    url = _CC_URL.search(raw)
    if url:
        kind, variant, version = url.group(1).lower(), url.group(2).upper(), url.group(3)
        if kind == "publicdomain":
            return "CC0-1.0" if variant == "ZERO" else None
        return f"CC-{variant}-{version}"
    text = _CC_TEXT.search(raw)
    if text:
        variant = re.sub(r"[\s-]+", "-", text.group(1).upper())
        variant = re.sub(r"(?<=BY)(?=NC|ND|SA)", "-", variant).replace("--", "-")
        if variant == "0":
            return "CC0-1.0"
        return f"CC-{variant}-{text.group(2)}" if text.group(2) else f"CC-{variant}"
    return None
