"""Generazione del JSON Schema del front matter (usato dalla CI del repository corpus)."""

from __future__ import annotations

import json
from typing import Any

from .models import FrontMatter

SCHEMA_ID = "https://example.invalid/aika/front_matter.schema.json"


def front_matter_schema() -> dict[str, Any]:
    schema = FrontMatter.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Aika corpus document front matter"
    return schema


def schema_text() -> str:
    """Testo canonico del file ``schema/front_matter.schema.json`` (UTF-8, LF, newline finale)."""
    return json.dumps(front_matter_schema(), indent=2, ensure_ascii=False) + "\n"
