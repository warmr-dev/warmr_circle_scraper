from types import SimpleNamespace
import pytest
from circle_leads.classifier import ai_classifier as ai


@pytest.fixture(autouse=True)
def clear_provider_env(monkeypatch):
    for name in ("DEEPSEEK_API_KEY","OPENAI_API_KEY","OPENROUTER_API_KEY","ANTHROPIC_API_KEY","CIRCLE_LEADS_LLM","AI_ANTHROPIC_ENABLED"):
        monkeypatch.delenv(name,raising=False)


def test_anthropic_key_alone_is_not_an_active_route(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY","unused")
    assert ai.make_backend() is None
    monkeypatch.setenv("CIRCLE_LEADS_LLM","anthropic")
    with pytest.raises(RuntimeError,match="disabled"):ai.make_backend()


def test_direct_deepseek_then_openai_via_router(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY","test")
    monkeypatch.setenv("OPENROUTER_API_KEY","test")
    backend=ai.make_backend()
    assert [type(b).__name__ for b in backend.backends]==["DeepSeekBackend","OpenRouterBackend"]
    assert backend.backends[1].model=="openai/gpt-5-mini"


def test_explicit_primary_is_preserved_before_reserves(monkeypatch):
    monkeypatch.setenv("CIRCLE_LEADS_LLM","openrouter")
    monkeypatch.setenv("CIRCLE_LEADS_OPENROUTER_MODEL","deepseek/deepseek-v4-flash")
    monkeypatch.setenv("DEEPSEEK_API_KEY","test")
    monkeypatch.setenv("OPENROUTER_API_KEY","test")
    backend=ai.make_backend()
    assert [type(b).__name__ for b in backend.backends]==["OpenRouterBackend","DeepSeekBackend","OpenRouterBackend"]
    assert backend.backends[0].model=="deepseek/deepseek-v4-flash"


def test_rejected_auth_can_advance_and_actual_model_is_retained():
    class Rejected(Exception):status_code=401
    def fail(*_):raise Rejected()
    backend=ai.ProviderChainBackend([SimpleNamespace(model="primary",complete=fail),SimpleNamespace(model="reserve",complete=lambda *_:'{}')])
    assert backend.complete("system","user")=="{}"
    assert backend.model==backend.last_model=="reserve"


@pytest.mark.parametrize("status",[None,429,500])
def test_unknown_provider_outcome_does_not_buy_a_second_call(status):
    called=[]
    class Failure(Exception):status_code=status
    def fail(*_):raise Failure()
    backend=ai.ProviderChainBackend([SimpleNamespace(model="primary",complete=fail),SimpleNamespace(model="reserve",complete=lambda *_:called.append(True))])
    with pytest.raises(Failure):backend.complete("system","user")
    assert not called
