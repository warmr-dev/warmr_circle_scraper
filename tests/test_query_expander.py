"""Tests for LLM-assisted niche expansion and broadened search templates.

No network: the LLM is a fake object; backends are never called.
"""

from __future__ import annotations

import pytest

from circle_leads.discovery.query_expander import expand_niches
from circle_leads.discovery import web_search


class _FakeLLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


def test_expand_parses_newline_list_and_prepends_seeds():
    llm = _FakeLLM("mobile app founders\nno-code makers\nReact Native developer\n")
    out = expand_niches(["Flutter Developer"], llm=llm, max_extra=10)
    assert out[0] == "Flutter Developer"  # seeds first
    assert "mobile app founders" in out
    assert "no-code makers" in out
    assert llm.calls == 1


def test_expand_parses_json_array():
    llm = _FakeLLM('["SaaS founders", "startup CTOs", "app agency owners"]')
    out = expand_niches(["Flutter Developer"], llm=llm, max_extra=10)
    assert "SaaS founders" in out
    assert "startup CTOs" in out


def test_expand_dedupes_seeds_and_case():
    # The model echoes a seed and a case variant -- neither should duplicate.
    llm = _FakeLLM("Flutter Developer\nflutter developer\nnew niche")
    out = expand_niches(["Flutter Developer"], llm=llm, max_extra=10)
    assert out.count("Flutter Developer") == 1
    assert sum(1 for x in out if x.lower() == "flutter developer") == 1
    assert "new niche" in out


def test_expand_respects_max_extra():
    llm = _FakeLLM("a\nb\nc\nd\ne")
    out = expand_niches(["seed"], llm=llm, max_extra=2)
    assert out == ["seed", "a", "b"]


def test_no_llm_returns_seeds_unchanged(monkeypatch):
    # No key/backend -> make_backend returns None -> seeds only, no error.
    monkeypatch.setattr(
        "circle_leads.classifier.ai_classifier.make_backend", lambda: None
    )
    out = expand_niches(["Flutter Developer", "Backend Engineer"])
    assert out == ["Flutter Developer", "Backend Engineer"]


def test_llm_error_returns_seeds():
    class Boom:
        def complete(self, s, u):
            raise RuntimeError("boom")

    assert expand_niches(["X", "Y"], llm=Boom()) == ["X", "Y"]


def test_empty_seeds_returns_empty():
    assert expand_niches([], llm=_FakeLLM("a\nb")) == []


def test_drops_sentences_and_numbering():
    llm = _FakeLLM(
        "1. SaaS founders\n"
        "2) no-code makers\n"
        "- indie hackers\n"
        "This is a long explanatory sentence that should be dropped entirely\n"
    )
    out = expand_niches(["seed"], llm=llm, max_extra=10)
    assert "SaaS founders" in out
    assert "no-code makers" in out
    assert "indie hackers" in out
    assert not any(len(x.split()) > 6 for x in out)


# --- Broadened search templates --------------------------------------------

def test_templates_broadened_for_fuzzy_coverage():
    defaults = web_search.DEFAULT_QUERY_TEMPLATES
    site = web_search.SITE_QUERY_TEMPLATES
    joined = " ".join(defaults + site).lower()
    # Related-topic / fuzzy phrasings are present.
    assert any("hire" in t for t in defaults)
    assert any("looking for" in t for t in defaults)
    assert any("founders" in t for t in defaults)
    assert any("free" in t for t in defaults)
    # Platform-wide site coverage.
    assert "inurl:circle.so" in joined
    assert "powered by circle" in joined
    # Every template still has the {q} slot.
    for t in defaults + site:
        assert "{q}" in t
