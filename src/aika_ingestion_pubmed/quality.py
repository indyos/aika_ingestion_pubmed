"""``QualityGate``: validazione schema, lunghezza minima, lingua e licenza ammesse."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from .config import QualityConfig
from .models import FrontMatter


@dataclass(frozen=True)
class GateResult:
    front_matter: FrontMatter | None
    reasons: list[str] = field(default_factory=lambda: [])

    @property
    def ok(self) -> bool:
        return self.front_matter is not None and not self.reasons


class QualityGate:
    def __init__(self, config: QualityConfig) -> None:
        self._cfg = config

    def check(self, draft: dict[str, object], body: str) -> GateResult:
        reasons: list[str] = []
        language = draft.get("language")
        if language is None:
            reasons.append("language_missing")
        elif language not in self._cfg.allowed_languages:
            reasons.append("language_not_allowed")
        license_ = draft.get("license")
        if license_ is None:
            reasons.append("license_missing")
        elif license_ not in self._cfg.allowed_licenses:
            reasons.append("license_not_allowed")
        if len(body) < self._cfg.min_body_chars:
            reasons.append("body_too_short")
        try:
            fm = FrontMatter.model_validate(draft)
        except ValidationError:
            # Le mancanze già segnalate sopra non vanno raddoppiate.
            if not reasons:
                reasons.append("schema_invalid")
            return GateResult(None, reasons)
        return GateResult(fm, reasons)
