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
