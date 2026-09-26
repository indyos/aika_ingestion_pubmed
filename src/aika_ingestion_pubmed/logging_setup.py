"""Log strutturato JSON (una riga per evento)."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname.lower(),
            "event": getattr(record, "event", record.getMessage()),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=False, default=str)


def log_fields(
    logger: logging.Logger, event: str, fields: Mapping[str, object], level: int = logging.INFO
) -> None:
    logger.log(level, event, extra={"event": event, "fields": dict(fields)})


def log_event(
    logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: object
) -> None:
    log_fields(logger, event, fields, level)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx logga gli URL: non serve e le richieste non contengono segreti, ma restiamo sobri.
    logging.getLogger("httpx").setLevel(logging.WARNING)
