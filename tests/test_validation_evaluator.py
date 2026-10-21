import json
import runpy
import sys
from pathlib import Path


def test_evaluator_defaults_to_no_provider_or_database_calls(tmp_path, monkeypatch, capsys):
    source = tmp_path / 'cohort.json'
    source.write_text(json.dumps({'posts':[{'post_id':1,'content':'Private input'}]}))
    monkeypatch.setattr(sys,'argv',['evaluate_validation_cohort.py',str(source)])
    runpy.run_path(str(Path(__file__).parents[1]/'scripts'/'evaluate_validation_cohort.py'),run_name='__main__')
    result=json.loads(capsys.readouterr().out)
    assert result == {'posts':1,'maximum_model_calls':2,'provider_calls':0,'database_writes':0,'exports':0}


def test_live_evaluator_refuses_wrong_candidate_before_provider_creation(tmp_path, monkeypatch):
    import pytest
    from circle_leads.classifier import ai_classifier as ai
    source=tmp_path/'cohort.json'
    source.write_text(json.dumps({'posts':[{'post_id':1,'content':'Private input'}]}))
    monkeypatch.setattr(sys,'argv',['evaluate_validation_cohort.py',str(source),
                                  '--allow-provider','--expected-classifier-sha256','wrong'])
    def forbid(*args,**kwargs):
        raise AssertionError('Provider must not be constructed before source verification')
    monkeypatch.setattr(ai,'OpenRouterBackend',forbid)
    with pytest.raises(SystemExit):
        runpy.run_path(str(Path(__file__).parents[1]/'scripts'/'evaluate_validation_cohort.py'),run_name='__main__')


def test_call_budget_survives_runner_restart_and_blocks_outbound(tmp_path):
    import pytest
    from scripts.evaluate_validation_cohort import _BudgetedBackend
    class Provider:
        model='fixture'
        calls=0
        def complete(self,*args):
            self.calls+=1
            return '{}'
    provider=Provider();ledger=tmp_path/'approval.json'
    first=_BudgetedBackend(provider,ledger,1)
    assert first.complete('system','private source')=='{}'
    second=_BudgetedBackend(provider,ledger,1)
    with pytest.raises(RuntimeError,match='exhausted'):
        second.complete('system','private source')
    assert provider.calls==1 and second.calls==0 and second.exhausted
    assert json.loads(ledger.read_text())=={'maximum':1,'spent':1}


def test_failed_provider_attempt_consumes_reservation(tmp_path):
    import pytest
    from scripts.evaluate_validation_cohort import _BudgetedBackend
    class Failed:
        model='fixture'
        def complete(self,*args):
            raise TimeoutError('unavailable')
    backend=_BudgetedBackend(Failed(),tmp_path/'approval.json',1)
    with pytest.raises(TimeoutError):
        backend.complete('system','private source')
    with pytest.raises(RuntimeError,match='exhausted'):
        backend.complete('system','private source')
    assert backend.calls==1
