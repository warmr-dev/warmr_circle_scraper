"""Rate limiting, backoff, and pagination behavior."""

import time

import pytest
import requests

from circle_leads.scraper.pagination import (
    AccessDeniedError,
    ApiError,
    CircleClient,
    QuotaTracker,
    RateLimiter,
    paginate,
    paginate_chat_messages,
)


class SeqResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class SeqSession:
    """Returns a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *a, **kw):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(session, **kw):
    kw.setdefault("requests_per_minute", 100000)
    kw.setdefault("backoff_base", 0.001)
    kw.setdefault("max_backoff", 0.002)
    return CircleClient("https://app.circle.so", lambda: {"Authorization": "x"},
                        session=session, **kw)


def envelope(records, has_next=False):
    return {"records": records, "has_next_page": has_next, "page": 1, "per_page": 100}


def test_rate_limiter_spaces_requests():
    limiter = RateLimiter(requests_per_minute=600)  # 0.1s apart
    start = time.monotonic()
    for _ in range(3):
        limiter.acquire()
    assert time.monotonic() - start >= 0.15


def test_401_raises_immediately_without_retry():
    session = SeqSession([SeqResponse({}, 401)] * 5)
    client = make_client(session)
    with pytest.raises(AccessDeniedError):
        client.get("/api/admin/v2/spaces")
    # Authorization failures must not be retried; retrying burns quota.
    assert session.calls == 1


def test_403_raises_immediately():
    session = SeqSession([SeqResponse({}, 403)])
    with pytest.raises(AccessDeniedError):
        make_client(session).get("/x")


def test_429_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session = SeqSession([
        SeqResponse({}, 429, {"Retry-After": "0"}),
        SeqResponse(envelope([{"id": 1}])),
    ])
    result = make_client(session).get("/x")
    assert result["records"] == [{"id": 1}]
    assert session.calls == 2


def test_500_is_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session = SeqSession([SeqResponse({}, 500), SeqResponse(envelope([]))])
    make_client(session).get("/x")
    assert session.calls == 2


def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session = SeqSession([SeqResponse({}, 503)] * 10)
    with pytest.raises(ApiError):
        make_client(session, max_retries=2).get("/x")
    assert session.calls == 3


def test_network_errors_are_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session = SeqSession([
        requests.ConnectionError("boom"),
        SeqResponse(envelope([{"id": 1}])),
    ])
    assert make_client(session).get("/x")["records"] == [{"id": 1}]


def test_quota_budget_stops_requests():
    quota = QuotaTracker(budget=2)
    session = SeqSession([SeqResponse(envelope([]))] * 5)
    client = make_client(session, quota=quota)
    client.get("/x")
    client.get("/x")
    with pytest.raises(ApiError, match="budget exhausted"):
        client.get("/x")


def test_paginate_follows_has_next_page():
    session = SeqSession([
        SeqResponse(envelope([{"id": 1}], has_next=True)),
        SeqResponse(envelope([{"id": 2}], has_next=True)),
        SeqResponse(envelope([{"id": 3}], has_next=False)),
    ])
    records = list(paginate(make_client(session), "/x"))
    assert [r["id"] for r in records] == [1, 2, 3]


def test_paginate_respects_max_pages():
    session = SeqSession([SeqResponse(envelope([{"id": i}], has_next=True)) for i in range(10)])
    records = list(paginate(make_client(session), "/x", max_pages=2))
    assert len(records) == 2


def test_paginate_handles_bare_array_response():
    session = SeqSession([SeqResponse([{"id": 1}, {"id": 2}])])
    assert len(list(paginate(make_client(session), "/x"))) == 2


def test_chat_pagination_walks_backwards_and_stops():
    session = SeqSession([
        SeqResponse(envelope([{"id": 30}, {"id": 29}])),
        SeqResponse(envelope([{"id": 29}])),  # already seen -> stop
    ])
    messages = list(paginate_chat_messages(make_client(session), "room-1", batch=2))
    assert [m["id"] for m in messages] == [30, 29]


def test_chat_pagination_stops_on_short_batch():
    session = SeqSession([SeqResponse(envelope([{"id": 5}]))])
    messages = list(paginate_chat_messages(make_client(session), "room-1", batch=100))
    assert len(messages) == 1
    assert session.calls == 1
