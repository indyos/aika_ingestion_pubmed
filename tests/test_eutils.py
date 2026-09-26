from __future__ import annotations

from urllib.parse import parse_qsl

import httpx
import pytest

from aika_ingestion_pubmed.eutils import EutilsClient, EutilsError, RateLimiter


class Clock:
    """Orologio finto: ``sleep`` avanza il tempo e registra le attese."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(
    handler: httpx.MockTransport | None = None,
    *,
    clock: Clock,
    api_key: str | None = None,
    **kwargs: float,
) -> tuple[EutilsClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="ok")

    client = EutilsClient(
        tool="aika_test",
        email="tester@example.org",
        api_key=api_key,
        transport=handler or httpx.MockTransport(respond),
        clock=clock.time,
        sleep=clock.sleep,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )
    return client, seen


def form(request: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(request.content.decode()))


# ------------------------------------------------------------------ parametri comuni


def test_common_params_always_present() -> None:
    clock = Clock()
    client, seen = make_client(clock=clock)
    client.post("esearch.fcgi", {"db": "pubmed", "term": "x"})
    params = form(seen[0])
    assert params["tool"] == "aika_test"
    assert params["email"] == "tester@example.org"
    assert params["db"] == "pubmed"
    assert "api_key" not in params


def test_api_key_sent_in_body_never_in_url() -> None:
    clock = Clock()
    client, seen = make_client(clock=clock, api_key="SECRET-KEY")
    client.post("efetch.fcgi", {"db": "pmc", "id": "1"})
    assert form(seen[0])["api_key"] == "SECRET-KEY"
    assert "SECRET-KEY" not in str(seen[0].url)


def test_tool_and_email_are_mandatory() -> None:
    with pytest.raises(ValueError, match="obbligatori"):
        EutilsClient(tool="", email="a@b.it")
    with pytest.raises(ValueError, match="obbligatori"):
        EutilsClient(tool="t", email="")


def test_only_eutils_endpoints_allowed() -> None:
    client, _ = make_client(clock=Clock())
    for bad in ("oa.fcgi", "../oa/oa.fcgi", "esummary.fcgi", "https://example.org/x"):
        with pytest.raises(ValueError, match="non ammesso"):
            client.post(bad, {})


# ------------------------------------------------------------------ rate limit


def test_rate_limit_without_key_is_3_per_second() -> None:
    clock = Clock()
    client, seen = make_client(clock=clock)
    start = clock.now
    for _ in range(7):
        client.post("efetch.fcgi", {"db": "pubmed"})
    assert len(seen) == 7
    # 7 richieste a 3/s: la settima non parte prima di 6/3 = 2 s dalla prima
    assert clock.now - start >= 2.0 - 1e-9
    assert all(abs(s - 1 / 3) < 1e-9 for s in clock.sleeps)


def test_rate_limit_with_key_is_10_per_second() -> None:
    clock = Clock()
    client, _ = make_client(clock=clock, api_key="k")
    start = clock.now
    for _ in range(11):
        client.post("efetch.fcgi", {"db": "pubmed"})
    assert clock.now - start >= 1.0 - 1e-9
    assert all(abs(s - 0.1) < 1e-9 for s in clock.sleeps)


def test_rate_limiter_never_allows_more_than_rate_in_any_window() -> None:
    clock = Clock()
    limiter = RateLimiter(3, clock=clock.time, sleep=clock.sleep)
    stamps: list[float] = []
    for _ in range(30):
        limiter.acquire()
        stamps.append(clock.now)
    for i, t in enumerate(stamps):
        in_window = [s for s in stamps[i:] if s < t + 1.0 - 1e-9]
        assert len(in_window) <= 3


def test_rate_limiter_rejects_nonpositive_rate() -> None:
    with pytest.raises(ValueError, match="> 0"):
        RateLimiter(0)


# ------------------------------------------------------------------ retry


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retry_with_exponential_backoff_on_429_and_5xx(status: int) -> None:
    clock = Clock()
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(status if len(attempts) <= 3 else 200, text="ok")

    client, _ = make_client(httpx.MockTransport(handler), clock=clock)
    response = client.post("efetch.fcgi", {"db": "pubmed"})
    assert response.text == "ok"
    assert len(attempts) == 4
    backoffs = [s for s in clock.sleeps if s >= 1.0]  # i rate-limit sleep sono < 1 s
    assert backoffs == [1.0, 2.0, 4.0]


def test_backoff_is_capped_and_retry_after_is_honoured() -> None:
    clock = Clock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, text="ok")

    client, _ = make_client(httpx.MockTransport(handler), clock=clock)
    client.post("efetch.fcgi", {"db": "pubmed"})
    assert 7.0 in clock.sleeps


def test_gives_up_after_max_retries() -> None:
    clock = Clock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    client, _ = make_client(httpx.MockTransport(handler), clock=clock, max_retries=2)
    with pytest.raises(EutilsError, match="HTTP 503 dopo 3 tentativi"):
        client.post("efetch.fcgi", {"db": "pubmed"})
    assert len(calls) == 3


def test_transport_errors_are_retried() -> None:
    clock = Clock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectTimeout("timeout", request=request)
        return httpx.Response(200, text="ok")

    client, _ = make_client(httpx.MockTransport(handler), clock=clock)
    assert client.post("efetch.fcgi", {"db": "pubmed"}).text == "ok"
    assert len(calls) == 3


def test_client_errors_are_not_retried_and_do_not_leak_the_key() -> None:
    clock = Clock()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400)

    client, _ = make_client(httpx.MockTransport(handler), clock=clock, api_key="SECRET-KEY")
    with pytest.raises(EutilsError) as info:
        client.post("efetch.fcgi", {"db": "pubmed"})
    assert len(calls) == 1
    assert "SECRET-KEY" not in str(info.value)
