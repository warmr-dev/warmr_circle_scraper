"""Search must not go silent when the paid backend runs dry.

Exa answered 402 ("exceeded your credits limit") to every query for two days.
choose_backend returned Exa alone because its key was present, the error was
swallowed into an empty list, and discovery reported "no results" with a free
keyless backend sitting right behind it.
"""
from __future__ import annotations

import pytest
import requests

from circle_leads.discovery import web_search
from circle_leads.discovery.web_search import (
    BackendUnavailable,
    ChainBackend,
    SearchResult,
    choose_backend,
)


class Backend:
    def __init__(self, name, *, results=None, raises=None):
        self.name = name
        self._results = results or []
        self._raises = raises
        self.calls = 0

    def search(self, query, *, count=10):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._results


def hit(url):
    return SearchResult(title="t", url=url, snippet="s")


@pytest.fixture(autouse=True)
def no_alerts(monkeypatch):
    monkeypatch.setattr(web_search, "_alert_backend_down", lambda *a, **k: None)


def test_a_dead_backend_falls_through_to_the_next():
    dead = Backend("exa", raises=BackendUnavailable("exa: HTTP 402 no credits"))
    alive = Backend("ddg", results=[hit("https://x.circle.so")])
    chain = ChainBackend([dead, alive])
    assert [r.url for r in chain.search("q")] == ["https://x.circle.so"]


def test_a_dead_backend_is_not_asked_twice():
    """A key does not un-revoke and credits do not reappear mid-run."""
    dead = Backend("exa", raises=BackendUnavailable("exa: HTTP 402"))
    alive = Backend("ddg", results=[hit("https://x.circle.so")])
    chain = ChainBackend([dead, alive])
    chain.search("one")
    chain.search("two")
    chain.search("three")
    assert dead.calls == 1
    assert alive.calls == 3


def test_a_transient_failure_does_not_retire_the_backend():
    flaky = Backend("exa", raises=requests.ConnectionError("reset"))
    alive = Backend("ddg", results=[hit("https://x.circle.so")])
    chain = ChainBackend([flaky, alive])
    chain.search("one")
    chain.search("two")
    assert flaky.calls == 2, "a blip must not cost us the best backend"


def test_an_empty_answer_moves_on_without_retiring_anything():
    empty = Backend("exa", results=[])
    alive = Backend("ddg", results=[hit("https://x.circle.so")])
    chain = ChainBackend([empty, alive])
    assert chain.search("q")
    assert "exa" not in chain._dead
    assert empty.calls == 1


def test_all_dead_returns_nothing_rather_than_raising():
    chain = ChainBackend([
        Backend("exa", raises=BackendUnavailable("402")),
        Backend("brave", raises=BackendUnavailable("401")),
    ])
    assert chain.search("q") == []


def test_a_human_is_told_the_first_time_a_backend_dies(monkeypatch):
    told = []
    monkeypatch.setattr(web_search, "_alert_backend_down",
                        lambda *a, **k: told.append(a))
    chain = ChainBackend([
        Backend("exa", raises=BackendUnavailable("exa: HTTP 402 no credits")),
        Backend("ddg", results=[hit("https://x.circle.so")]),
    ])
    chain.search("one")
    chain.search("two")
    assert len(told) == 1, "once, not on every query"
    assert told[0][0] == "exa"


def test_the_keyless_backend_is_always_present(monkeypatch):
    for key in ("EXA_API_KEY", "BRAVE_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    chain = choose_backend()
    assert chain.live == ["duckduckgo"]


def test_a_configured_key_goes_in_front_of_the_keyless_one(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "k")
    for key in ("BRAVE_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert choose_backend().live == ["exa", "duckduckgo"]


@pytest.mark.parametrize("status", [401, 402, 403])
def test_terminal_statuses_retire_a_backend(status):
    class Resp:
        status_code = status
        text = "nope"

    with pytest.raises(BackendUnavailable) as caught:
        web_search._raise_if_dead("exa", Resp())
    assert str(status) in str(caught.value)


@pytest.mark.parametrize("status", [200, 429, 500, 503])
def test_other_statuses_do_not(status):
    class Resp:
        status_code = status
        text = ""

    web_search._raise_if_dead("exa", Resp())   # no exception
