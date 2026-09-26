"""Client sottile su NCBI E-utilities: rate limit, retry con backoff, parametri comuni.

Unico canale di rete verso NCBI: nessun altro host è mai contattato da questo modulo.
Le richieste sono sempre POST: i parametri (compresa ``api_key``) restano nel corpo e non
compaiono negli URL, quindi nemmeno nei messaggi d'errore o nei log.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Final

import httpx

from .logging_setup import log_event

logger = logging.getLogger(__name__)

BASE_URL: Final = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
ALLOWED_ENDPOINTS: Final = frozenset({"esearch.fcgi", "efetch.fcgi"})
RATE_WITHOUT_KEY: Final = 3.0
RATE_WITH_KEY: Final = 10.0
RETRY_STATUSES: Final = frozenset({429, 500, 502, 503, 504})

type Params = Mapping[str, str | int]


class EutilsError(RuntimeError):
    """Errore non recuperabile nella chiamata a E-utilities."""


class RateLimiter:
    """Garantisce un intervallo minimo tra richieste consecutive (thread-safe)."""

    def __init__(
        self,
        rate_per_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s deve essere > 0")
        self._interval = 1.0 / rate_per_s
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self._interval
        delay = start - now
        if delay > 0:
            self._sleep(delay)


class EutilsClient:
    def __init__(
        self,
        *,
        tool: str,
        email: str,
        api_key: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 5,
        backoff_base_s: float = 1.0,
        backoff_max_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = BASE_URL,
    ) -> None:
        if not tool or not email:
            raise ValueError("`tool` ed `email` sono obbligatori per NCBI")
        self._common: dict[str, str] = {"tool": tool, "email": email}
        if api_key:
            self._common["api_key"] = api_key
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._sleep = sleep
        self._limiter = RateLimiter(
            RATE_WITH_KEY if api_key else RATE_WITHOUT_KEY, clock=clock, sleep=sleep
        )
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            transport=transport,
            headers={"User-Agent": f"{tool} (mailto:{email})"},
        )

    def __enter__(self) -> EutilsClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def post(self, endpoint: str, params: Params) -> httpx.Response:
        """POST con parametri comuni; ritenta su 429/5xx/errori di rete con backoff esponenziale."""
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"endpoint non ammesso: {endpoint}")
        data = {**self._common, **{k: str(v) for k, v in params.items()}}
        attempt = 0
        while True:
            self._limiter.acquire()
            failure: str
            retry_after: float | None = None
            try:
                response = self._http.post(endpoint, data=data)
            except httpx.TransportError as exc:
                failure = f"errore di rete ({type(exc).__name__})"
            else:
                if response.status_code not in RETRY_STATUSES:
                    if response.is_success:
                        return response
                    raise EutilsError(f"{endpoint}: HTTP {response.status_code}")
                failure = f"HTTP {response.status_code}"
                retry_after = _retry_after_seconds(response)
            if attempt >= self._max_retries:
                raise EutilsError(f"{endpoint}: {failure} dopo {attempt + 1} tentativi")
            delay = min(self._backoff_max, self._backoff_base * (2**attempt))
            if retry_after is not None:
                delay = max(delay, min(retry_after, self._backoff_max))
            attempt += 1
            log_event(
                logger,
                "eutils_retry",
                level=logging.WARNING,
                endpoint=endpoint,
                reason=failure,
                attempt=attempt,
                delay_s=delay,
            )
            self._sleep(delay)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
