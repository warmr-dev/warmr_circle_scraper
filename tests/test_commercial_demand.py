"""Mechanics tests use canned semantic answers; real evaluations are separate."""
from datetime import datetime, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, func

from circle_leads.classifier.ai_classifier import MAX_CONTEXT_CHARS, classify_with_llm
from circle_leads.classifier.lead_classifier import classify
from circle_leads.config.settings import load_requirements
from circle_leads.export.vini_ingest import lead_to_ingest_payload
from circle_leads.storage.database import Database, get_or_create_community, upsert_post
from circle_leads.storage.models import ActivityLog, Lead, Post, utcnow
import circle_leads.pipeline as batch
import circle_leads.triage.pipeline as triage

CASES=json.loads((Path(__file__).parent/'fixtures/commercial_demand.json').read_text())['cases']
PRIVATE = Path(__file__).parents[1]/'reports/commercial-demand/private/regressions.json'
if PRIVATE.exists():
    CASES += [{**c, 'case':'source-'+c['case']} for c in json.loads(PRIVATE.read_text())['cases']]


def answer(text,positive=True,**extra):
    return {'hiring_scope':'not_hiring','need_owner':'current_author' if positive else 'none','service_direction':'seeking_help' if positive else 'neither', 'post_purpose':'demand' if positive else 'information', 'author_role':'buyer' if positive else 'other','wants':'service' if positive else 'nothing',
            'work_type':'other','work_mode':'unknown','demand_signal':'explicit_demand' if positive else 'none',
            'awareness':5 if positive else None,'confidence':.95,
            'reason':'Buyer asks for paid help.' if positive else 'Information without buyer demand.',
            'supporting_excerpts':[{'source_id':'current','quote':text}],**extra}


class Backend:
    model='test-model'
    def __init__(self,payload=None,error=None): self.payload,self.error,self.calls=payload,error,[]
    def complete(self,system,user):
        self.calls.append(json.loads(user))
        if self.error: raise self.error
        return json.dumps(self.payload)


def forbid(*a,**k): raise AssertionError('Outbound side effect attempted')


@pytest.mark.parametrize('case',CASES,ids=lambda x:x['case'])
@pytest.mark.parametrize('route',['batch','triage'])
def test_ground_truth_reaches_local_export_without_side_effects(case,route,tmp_path,monkeypatch):
    db=Database(f'sqlite:///{tmp_path}/db')
    backend=Backend(answer(case['text'],case['expected']))
    module=batch if route=='batch' else triage
    monkeypatch.setattr(module,'make_backend',lambda:backend)
    monkeypatch.setattr(module,'push_leads_by_ids',forbid)
    captured = datetime.fromisoformat(case['scraped_at']) if case.get('scraped_at') else utcnow().replace(tzinfo=None)
    published = datetime.fromisoformat(case['published_at']) if case.get('published_at') else captured-timedelta(hours=1)
    monkeypatch.setattr('circle_leads.classifier.decisions.utcnow', lambda: captured)
    record={'content':case['text'],'source_content_id':'fixture','url':'https://fixture.circle.so/c/post/1',
            'published_at':published,
            'author':{'source_author_id':'buyer-id','display_name':'Buyer'}}
    if route=='triage':
        triage.triage_records(db,[record],load_requirements(),community='fixture',use_llm=True,export=False)
    else:
        from circle_leads.storage.database import get_or_create_author
        with db.session() as s:
            c=get_or_create_community(s,slug='fixture',url='https://fixture.circle.so')
            a=get_or_create_author(s,community_id=c.id,source_author_id='buyer-id',display_name='Buyer')
            upsert_post(s,community_id=c.id,record={**record,'author_id':a.id})
        batch.classify_pending(db,load_requirements(),use_llm=True,export=False)
    with db.session() as s:
        post=s.scalar(select(Post));lead=s.scalar(select(Lead))
        assert post.classified and post.classification_retry_at is None
        assert post.classification_audit['model']=='test-model'
        assert bool(lead)==case['expected']
        post.scraped_at = captured
        if lead:
            lead.created_at = captured
        payload = lead_to_ingest_payload(lead,post,post.community,post.author) if lead else None
        assert bool(payload) == case['expected']
        if payload:
            assert captured - published <= timedelta(hours=48)
            assert datetime.fromisoformat(payload['source_event_at'].replace('Z', '+00:00')).replace(tzinfo=None) == published.replace(microsecond=0)
            if case.get('published_at'):
                assert utcnow().replace(tzinfo=None) - published > timedelta(hours=48)
        receipts=s.scalars(select(ActivityLog).where(ActivityLog.kind=='classify')).all()
        assert any(r.detail.get('post_id')==post.id and r.detail.get('excerpts') for r in receipts)


@pytest.mark.parametrize('text',[
    'Not hiring employees, but we need an SEO agency.',
    'I am a freelancer. Our studio needs a part-time webinar specialist.',
    'Should we outsource our UX research?',
    'Our accountant keeps missing deadlines.',
    'Any recommendations for someone to film our event?',
    'Our site conversion rate fell. What solutions should we explore?',
])
def test_mixed_keywords_and_early_stage_always_reach_model(text,dev_requirements):
    backend=Backend(answer(text,demand_signal='solution_exploration',awareness=3))
    result=classify(text,dev_requirements,llm=backend)
    assert result.is_lead and len(backend.calls)==1


@pytest.mark.parametrize('text',[
    'Our agency offers SEO services; book a call.',
    'Here is a tutorial showing how I built a Webflow calculator.',
    'We are hiring a receptionist for our in-house office.',
    'Where can my friend find entry-level marketing jobs?',
    'This role is filled; no additional help needed.',
    'Great post. I can offer my consulting services.',
])
def test_adjacent_exclusions_are_semantic_not_categories(text,dev_requirements):
    backend=Backend(answer(text,False))
    assert not classify(text,dev_requirements,llm=backend).is_lead


def test_tail_demand_and_context_are_not_truncated():
    text='A long introduction. '*500+'Need a videographer for tomorrow.'
    backend=Backend(answer('Need a videographer for tomorrow.'))
    assert classify_with_llm(text,backend).classification=='LEAD'
    assert backend.calls[0]['current_post']['content']==text
    too_long=Backend(answer(text))
    result=classify_with_llm('x'*MAX_CONTEXT_CHARS,too_long)
    assert result.error=='context_limit_exceeded' and too_long.calls==[]


@pytest.mark.parametrize('payload',[{'confidence':1},[],{'author_role':'buyer','demand_signal':'none'}])
def test_malformed_semantic_reply_is_retryable(payload):
    assert classify_with_llm('Some nonempty post',Backend(payload)).error


def test_fabricated_negative_evidence_is_also_retryable():
    assert classify_with_llm('Information only',Backend(answer('invented',False))).error


@pytest.mark.parametrize('route',['batch','triage'])
def test_error_backoff_and_retry_preserve_previous_lead(route,tmp_path,monkeypatch):
    db=Database(f'sqlite:///{tmp_path}/retry')
    now=utcnow().replace(tzinfo=None)
    text='Need a videographer for our commercial event.'
    record={'content':text,'url':'https://fixture.circle.so/c/post/1','source_content_id':'p1'}
    with db.session() as s:
        c=get_or_create_community(s,slug='fixture',url='https://fixture.circle.so')
        post,_=upsert_post(s,community_id=c.id,record=record)
        s.add(Lead(post_id=post.id,classification='LEAD',confidence=.95,reason='previous success'))
    backend=Backend(error=TimeoutError('provider timeout'))
    module=batch if route=='batch' else triage
    monkeypatch.setattr(module,'make_backend',lambda:backend)
    monkeypatch.setattr(module,'push_leads_by_ids',forbid)
    monkeypatch.setattr('circle_leads.classifier.decisions.utcnow',lambda:now)
    if route=='batch': batch.classify_pending(db,load_requirements(),use_llm=True,export=False)
    else:
        # Use the same hash identity triage generates, preserving existing row.
        from circle_leads.storage.database import content_hash
        with db.session() as s: s.scalar(select(Post)).source_content_id=f'triage:{content_hash(text)[:24]}'
        triage.triage_records(db,[record],load_requirements(),community='fixture',use_llm=True,export=False)
    with db.session() as s:
        p=s.scalar(select(Post));lead=s.scalar(select(Lead))
        assert not p.classified and p.classification_retry_at==now+timedelta(minutes=1)
        assert lead.reason=='previous success'
        assert lead_to_ingest_payload(lead,p,p.community,p.author) is None
        p.classification_retry_at=now-timedelta(seconds=1)
    good=Backend(answer(text))
    monkeypatch.setattr(batch,'make_backend',lambda:good)
    batch.classify_pending(db,load_requirements(),use_llm=True,retry_only=True,export=False)
    with db.session() as s:
        p=s.scalar(select(Post))
        assert p.classified and p.classification_retry_at is None
        assert p.classification_audit['attempt']==2


def test_retry_is_bounded_and_never_selects_legacy_backlog(tmp_path,monkeypatch):
    db=Database(f'sqlite:///{tmp_path}/bounded')
    text='Need an SEO agency.'
    with db.session() as s:
        c=get_or_create_community(s,slug='fixture',url='https://fixture.circle.so')
        for i in range(31):
            p,_=upsert_post(s,community_id=c.id,record={'source_content_id':str(i),'content':text+str(i)})
            if i<30: p.classification_retry_at=utcnow().replace(tzinfo=None)-timedelta(minutes=30-i)
    backend=Backend(error=TimeoutError())
    monkeypatch.setattr(batch,'make_backend',lambda:backend)
    stats=batch.classify_pending(db,load_requirements(),use_llm=True,retry_only=True,limit=100,export=False)
    assert stats['errors']==25 and len(backend.calls)==25
    with db.session() as s:
        assert s.scalar(select(Post).where(Post.source_content_id=='30')).classification_audit is None


def test_supplied_thread_context_is_used_before_replies_are_stored(tmp_path,monkeypatch):
    db=Database(f'sqlite:///{tmp_path}/context')
    text='Can anyone recommend someone?'
    backend=Backend(answer(text,demand_signal='recommendation_request',awareness=4))
    monkeypatch.setattr(triage,'make_backend',lambda:backend)
    triage.triage_records(db,[{'content':text,'url':'https://fixture.circle.so/c/post/1'},
         {'content':'We need UX research help.','url':'https://fixture.circle.so/c/post/1#comment_1'}],
         load_requirements(),community='fixture',use_llm=True,export=False)
    assert backend.calls[0]['context'][0]['content']=='We need UX research help.'


def test_migration_has_additive_columns_and_index():
    sql=(Path(__file__).parents[1]/'migrations/manual/2026-10-06_p31_commercial_demand.sql').read_text()
    assert 'add column if not exists classification_audit json' in sql
    assert 'add column if not exists classification_retry_at timestamp' in sql
    assert 'create index if not exists ix_posts_classification_retry_at' in sql
    assert 'update posts' not in sql.lower()


def test_retry_schedule_remains_retryable_after_repeated_failures(tmp_path, monkeypatch):
    from circle_leads.classifier.decisions import evaluate_post
    db = Database(f'sqlite:///{tmp_path}/schedule')
    now = utcnow().replace(tzinfo=None)
    monkeypatch.setattr('circle_leads.classifier.decisions.utcnow', lambda: now)
    with db.session() as s:
        c = get_or_create_community(s, slug='fixture', url='https://fixture.circle.so')
        post, _ = upsert_post(s, community_id=c.id, record={'source_content_id':'p', 'content':'Need an agency.'})
        for attempt, minutes in enumerate([1, 5, 15, 60, 60], 1):
            evaluate_post(s, post, load_requirements(), llm=Backend(error=TimeoutError()), model_name='test-model')
            assert post.classification_audit['attempt'] == attempt
            assert post.classification_retry_at == now + timedelta(minutes=minutes)
            assert not post.classified
        s.flush()
        assert s.scalar(select(func.count()).select_from(ActivityLog).where(ActivityLog.kind=='classify')) == 5


def test_thread_id_context_preserves_reply_author(tmp_path, monkeypatch):
    db = Database(f'sqlite:///{tmp_path}/thread')
    root = 'Need a UX consultant.'
    reply = 'My agency offers UX consulting.'
    class ThreadBackend(Backend):
        def complete(self, system, user):
            current = json.loads(user)['current_post']['content']
            self.payload = answer(current, current == root)
            return super().complete(system, user)
    backend = ThreadBackend()
    monkeypatch.setattr(triage, 'make_backend', lambda: backend)
    triage.triage_records(db, [
        {'content':root, 'thread_id':'thread-1', 'url':'https://fixture.circle.so/post/root',
         'author':{'source_author_id':'buyer', 'display_name':'Buyer'}},
        {'content':reply, 'thread_id':'thread-1', 'url':'https://fixture.circle.so/comment/1',
         'author':{'source_author_id':'supplier', 'display_name':'Supplier'}},
    ], load_requirements(), community='fixture', use_llm=True, export=False)
    assert backend.calls[0]['context'][0]['author_id'] == 'supplier'
    assert backend.calls[1]['current_post']['source_author_id'] == 'supplier'
    with db.session() as s:
        posts = s.scalars(select(Post).order_by(Post.id)).all()
        assert posts[0].thread_id == posts[1].thread_id == 'thread-1'
        assert posts[0].classification_audit['outcome'] == 'lead'
        assert posts[1].classification_audit['outcome'] == 'not_lead'
        assert s.scalar(select(func.count()).select_from(Lead)) == 1


def test_confidence_floor_is_distinct_from_non_demand(tmp_path, monkeypatch):
    db = Database(f'sqlite:///{tmp_path}/confidence')
    text = 'Need an SEO agency.'
    monkeypatch.setattr(batch, 'make_backend', lambda: Backend(answer(text, confidence=.2)))
    with db.session() as s:
        c = get_or_create_community(s, slug='fixture', url='https://fixture.circle.so')
        upsert_post(s, community_id=c.id, record={'source_content_id':'p', 'content':text})
    stats = batch.classify_pending(db, load_requirements(), use_llm=True, export=False)
    assert stats['filtered'] == 1 and stats['not_leads'] == 0
    with db.session() as s:
        post = s.scalar(select(Post))
        assert post.classified and post.classification_audit['outcome'] == 'filtered_confidence'
        assert post.classification_audit['demand_signal'] == 'explicit_demand'
        assert s.scalar(select(Lead)) is None


def test_reserved_context_source_cannot_replace_current_post():
    result = classify_with_llm('Information', Backend(answer('Need an agency.')),
                              context=[{'source_id':'current', 'content':'Need an agency.'}])
    assert result.error == 'duplicate_context_source_id'


def test_existing_sqlite_schema_addition_does_not_reset_history(tmp_path):
    import sqlite3
    path = tmp_path/'legacy.db'
    db = Database(f'sqlite:///{path}')
    with db.session() as s:
        c = get_or_create_community(s, slug='fixture', url='https://fixture.circle.so')
        p, _ = upsert_post(s, community_id=c.id, record={'source_content_id':'p', 'content':'Old classified post'})
        p.classified = True
    db.engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute('DROP INDEX ix_posts_classification_retry_at')
        connection.execute('ALTER TABLE posts DROP COLUMN classification_retry_at')
        connection.execute('ALTER TABLE posts DROP COLUMN classification_audit')
    upgraded = Database(f'sqlite:///{path}')
    with upgraded.session() as s:
        p = s.scalar(select(Post))
        assert p.classified and p.classification_audit is None and p.classification_retry_at is None
    assert 'ix_posts_classification_retry_at' in {i['name'] for i in inspect(upgraded.engine).get_indexes('posts')}


@pytest.mark.parametrize('field,value', [('source_id', []), ('quote', None)])
def test_invalid_excerpt_shape_is_processing_failure(field, value):
    payload = answer('Need an agency.')
    payload['supporting_excerpts'][0][field] = value
    assert classify_with_llm('Need an agency.', Backend(payload)).error


def test_local_evaluation_serializes_without_provider_or_ingest(monkeypatch):
    import importlib.util
    path = Path(__file__).parents[1]/'scripts/evaluate_commercial_demand.py'
    spec = importlib.util.spec_from_file_location('commercial_evaluation', path)
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    text = 'Need a videographer.'
    result = evaluation.local_export(
        {'text':text, 'published_at':'2026-09-28 12:00:00', 'scraped_at':'2026-09-28 12:01:00'},
        json.dumps(answer(text)), 'test-model', load_requirements())
    assert result['export_eligible'] and result['fresh_at_capture'] and result['expired_at_evaluation']
    assert result['payload']['source_event_at'] == '2026-09-28T12:00:00Z'
    assert result['payload']['classified_at'] == '2026-09-28T12:01:00Z'


def test_thread_id_context_does_not_mix_other_threads(tmp_path):
    from circle_leads.classifier.decisions import context_for_post
    db = Database(f'sqlite:///{tmp_path}/isolation')
    with db.session() as s:
        c = get_or_create_community(s, slug='fixture', url='https://fixture.circle.so')
        root, _ = upsert_post(s, community_id=c.id, record={'source_content_id':'root', 'content':'Root',
            'thread_id':'t1', 'url':'https://fixture.circle.so/c/post/root'})
        upsert_post(s, community_id=c.id, record={'source_content_id':'reply', 'content':'Same thread',
            'thread_id':'t1', 'url':'https://fixture.circle.so/c/post/root#comment1'})
        upsert_post(s, community_id=c.id, record={'source_content_id':'different', 'content':'Other thread',
            'thread_id':'t2', 'url':'https://fixture.circle.so/c/post/root#comment2'})
        assert [x['content'] for x in context_for_post(s, root)] == ['Same thread']


def test_evaluation_preflight_stops_before_disclosing_posts():
    import importlib.util
    from types import SimpleNamespace
    path = Path(__file__).parents[1]/'scripts/evaluate_commercial_demand.py'
    spec = importlib.util.spec_from_file_location('commercial_evaluation_preflight', path)
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    class AuthFailure(Exception):
        status_code = 401
    class Models:
        def list(self):
            raise AuthFailure('invalid credential')
    backend = SimpleNamespace(_client=SimpleNamespace(models=Models()))
    with pytest.raises(RuntimeError, match='status=401.*no cohort inference'):
        evaluation.preflight_provider(backend)


def test_existing_project_key_reuse_is_in_memory_only(monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    path = Path(__file__).parents[1]/'scripts/evaluate_commercial_demand.py'
    spec = importlib.util.spec_from_file_location('commercial_evaluation_credentials', path)
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    calls = []
    responses = iter([{'envs':[{'id':'env-test','key':'OPENAI_API_KEY','target':['production']}]},
                      {'value':'test-only-credential'}])
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(next(responses)))
    monkeypatch.setattr(evaluation.subprocess, 'run', fake_run)
    monkeypatch.setenv('OPENAI_API_KEY','previous-test-credential')
    evaluation.load_existing_vercel_openai_key('circle-test','team-test')
    assert evaluation.os.environ['OPENAI_API_KEY'] == 'test-only-credential'
    assert len(calls) == 2 and all(c[-2:] == ['--method','GET'] for c in calls)
    assert all('test-only-credential' not in str(c) for c in calls)


def test_parallel_evaluation_fixture_replays_keep_answers_and_context_isolated():
    import importlib.util
    from concurrent.futures import ThreadPoolExecutor
    path = Path(__file__).parents[1]/'scripts/evaluate_commercial_demand.py'
    spec = importlib.util.spec_from_file_location('commercial_evaluation_parallel', path)
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    cases = [
        {'text':'We need a freelance webinar manager.', 'expected':True},
        {'text':'Here is a tutorial about webinars.', 'expected':False},
    ] * 4
    def replay(case):
        return evaluation.local_export(case, json.dumps(answer(case['text'],case['expected'])),
                                       'test-model', load_requirements())
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(replay, cases))
    import hashlib
    for case, receipt in zip(cases, receipts):
        assert receipt['export_eligible'] == case['expected']
        assert receipt['stats']['errors'] == 0
        assert receipt['audit']['content_hash'] == hashlib.sha256(case['text'].encode()).hexdigest()
        assert receipt['audit']['outcome'] == ('lead' if case['expected'] else 'not_lead')


@pytest.mark.parametrize('classification,error,outcome', [
    ('UNCERTAIN','unverified_evidence','error'),
    ('LEAD',None,'filtered_confidence'),
])
def test_evaluation_does_not_count_errors_or_low_confidence_as_correct_negatives(classification,error,outcome):
    import importlib.util
    path = Path(__file__).parents[1]/'scripts/evaluate_commercial_demand.py'
    spec = importlib.util.spec_from_file_location('commercial_evaluation_acceptance', path)
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    data={'new':{'eligible':False,'result':{'classification':classification,'llm_error':error}},
          'local_export':{'export_eligible':False,'stats':{'errors':int(bool(error))},
                          'audit':{'outcome':outcome}}}
    assert not evaluation.regression_matches({'expected':False}, data)


def test_offline_payload_keeps_source_author_attribution():
    import importlib.util
    path = Path(__file__).parents[1]/'scripts/evaluate_commercial_demand.py'
    spec = importlib.util.spec_from_file_location('commercial_evaluation_attribution', path)
    evaluation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evaluation)
    case={'text':'We need a freelance webinar manager.', 'expected':True,
          'source_author_id':'source-actor-42','display_name':'Source author'}
    receipt=evaluation.local_export(case,json.dumps(answer(case['text'])),
                                    'test-model',load_requirements())
    assert receipt['payload']['source_author_id']=='source-actor-42'
    assert receipt['payload']['name']=='Source author'


@pytest.mark.parametrize('positive,scope',[
    (True, 'reader_feedback_or_supplier_research'),
    (False, 'author_commercial_project'),
    (True, 'unsupported_scope'),
])
def test_request_scope_conflicts_remain_processing_errors(positive,scope,dev_requirements):
    text='Our company is considering a new publishing approach.'
    result=classify(text,dev_requirements,llm=Backend(answer(text,positive,request_scope=scope)))
    assert result.classification == 'UNCERTAIN'
    assert result.llm_error
    assert not result.is_lead


@pytest.mark.parametrize('positive,scope',[
    (True, 'author_commercial_project'),
    (False, 'reader_feedback_or_supplier_research'),
])
def test_consistent_request_scope_is_preserved_in_description(positive,scope,dev_requirements):
    text='Publishing strategy and reader feedback are different intentions.'
    result=classify(text,dev_requirements,llm=Backend(answer(text,positive,request_scope=scope)))
    assert not result.llm_error
    assert result.is_lead == positive
    assert result.described['request_scope'] == scope
