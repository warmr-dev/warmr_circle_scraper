"""Evidence selection is source addressing, never a permissive quote fallback."""
import json

import pytest

from circle_leads.classifier import ai_classifier as ai
from tests.test_commercial_demand import answer


class SelectingBackend:
    model = 'test-grounded'

    def __init__(self, select_ids=None, **fields):
        self.calls = []
        self.select_ids = select_ids or (lambda spans: [spans[0]['evidence_id']])
        self.fields = fields

    def complete(self, system, user):
        request = json.loads(user)
        self.calls.append(request)
        result = answer(request['current_post']['content'], **self.fields)
        del result['supporting_excerpts']
        result['supporting_excerpt_ids'] = self.select_ids(request['evidence_spans'])
        return json.dumps(result)


def test_catalog_slices_cover_source_without_correcting_spelling_or_unicode():
    content = 'Résumé!\n\nCould  you benefit form it? “Yep…”\t' + ' token' * 40
    spans = list(ai._evidence_spans({'current':content}).values())
    for span in spans:
        assert span['quote'] == content[span['start']:span['end']]
    assert [word for span in spans for word in span['quote'].split()] == content.split()
    assert 'form' in spans[0]['quote'] and 'from' not in spans[0]['quote']
    assert len({s['evidence_id'] for s in spans}) == len(spans)


@pytest.mark.parametrize('text', ['Ramp!', '@Someone', 'Need a freelance designer.',
                                  'Need technical help at the end. ' * 100])
def test_selected_evidence_is_exact_and_both_decisions_keep_current_attribution(text):
    backend = SelectingBackend()
    verdict = ai.classify_with_llm(text, backend, context=[{
        'source_id':'root:1', 'content':'Somebody else needs a marketing agency.',
        'source_author_id':'different-author'}], current_metadata={'source_author_id':'current-author'})
    assert not verdict.error
    excerpt = verdict.supporting_excerpts[0]
    assert excerpt['source_id'] == 'current' and excerpt['quote'] in text
    assert verdict.evidence_quote == excerpt['quote']
    request = backend.calls[0]
    assert request['current_post']['content'] == text
    assert request['context'][0]['source_author_id'] == 'different-author'
    assert request['current_post']['source_author_id'] == 'current-author'


@pytest.mark.parametrize('selected', [[], ['missing'], [3], 'E1', [None]])
def test_invalid_selection_is_error_never_first_span_fallback(selected):
    backend = SelectingBackend(select_ids=lambda spans:selected)
    verdict = ai.classify_with_llm('A source post.', backend)
    assert verdict.error and verdict.failure_kind == 'validation'
    assert not verdict.supporting_excerpts and verdict.evidence_quote is None
    assert len(backend.calls) == 2


def test_context_only_selection_cannot_borrow_root_buyer_demand():
    backend = SelectingBackend(select_ids=lambda spans:[next(
        s['evidence_id'] for s in spans if s['source_id']=='root')])
    verdict = ai.classify_with_llm('Try that agency.', backend,
                                context=[{'source_id':'root','content':'Need an agency.'}])
    assert verdict.error == 'Evidence must include the current post.'
    assert not verdict.supporting_excerpts


@pytest.mark.parametrize('extra', [{'supporting_excerpts':[{'source_id':'current','quote':'invented'}]},
                                    {'evidence_quote':'invented'}])
def test_conflicting_literal_quote_and_selected_id_are_rejected(extra):
    spans = ai._evidence_spans({'current':'Original source.'})
    with pytest.raises(ValueError, match='Conflicting'):
        ai._resolve_evidence({'supporting_excerpt_ids':['E1'], **extra}, spans)


def test_legacy_quote_still_rejected_when_not_in_source():
    from tests.test_commercial_demand import Backend
    backend = Backend(answer('A made up quote.'))
    verdict = ai.classify_with_llm('Actual source text.', backend)
    assert verdict.error == 'unverified_evidence'


def test_last_span_remains_selectable_for_demand_at_end_of_long_post():
    text = ('A neutral introduction. ' * 400) + 'We need a freelance webinar producer.'
    backend = SelectingBackend(select_ids=lambda spans:[spans[-1]['evidence_id']])
    verdict = ai.classify_with_llm(text, backend)
    assert not verdict.error and 'webinar producer.' in verdict.evidence_quote
    assert backend.calls[0]['current_post']['content']==text


def test_few_shot_examples_validate_and_final_user_context_is_unchanged():
    user=json.dumps({'current_post':{'content':'private current post'},
                     'context':[{'content':'private full thread'}]})
    messages=ai._classification_messages(ai.SYSTEM_PROMPT,user)
    assert messages[0]=={'role':'system','content':ai.SYSTEM_PROMPT}
    assert messages[-1]=={'role':'user','content':user}
    assert all('private' not in x['content'] for x in messages[1:-1])
    for n in range(1,len(messages)-1,2):
        request=json.loads(messages[n]['content'])
        data=json.loads(messages[n+1]['content'])
        text=request['current_post']['content']
        result=ai._resolve_evidence(data,ai._evidence_spans({'current':text}))
        verdict=ai._validate_response(result,text,{'current':text},'fixture')
        assert not verdict.error
        assert (verdict.classification=='LEAD') == (data['demand_signal'] in ai.LEAD_RULE['demand_signal'])


@pytest.mark.parametrize('route',['batch','triage'])
@pytest.mark.parametrize('positive',[True,False])
def test_selected_spans_reach_audited_local_export_gate(tmp_path,monkeypatch,route,positive):
    from datetime import timedelta
    from sqlalchemy import select
    from circle_leads.config.settings import load_requirements
    from circle_leads.storage.database import Database,get_or_create_community,get_or_create_author,upsert_post
    from circle_leads.storage.models import Post,Lead,ActivityLog,utcnow
    from circle_leads.export.vini_ingest import lead_to_ingest_payload
    import circle_leads.pipeline as batch
    import circle_leads.triage.pipeline as triage
    module=batch if route=='batch' else triage
    backend=SelectingBackend(positive=positive)
    monkeypatch.setattr(module,'make_backend',lambda:backend)
    def forbid(*args,**kwargs):
        raise AssertionError('Outbound export attempted in local evidence test')
    monkeypatch.setattr(module,'push_leads_by_ids',forbid)
    db=Database(f'sqlite:///{tmp_path}/grounded.db')
    text='We need a freelance webinar producer.' if positive else 'I recommend that webinar producer.'
    record={'content':text,'source_content_id':'test','url':'https://fixture.circle.so/c/post/1',
            'published_at':utcnow().replace(tzinfo=None)-timedelta(hours=1),
            'author':{'source_author_id':'fixture-author','display_name':'Fixture Author'}}
    if route=='triage':
        triage.triage_records(db,[record],load_requirements(),community='fixture',use_llm=True,export=False)
    else:
        with db.session() as s:
            c=get_or_create_community(s,slug='fixture',url='https://fixture.circle.so')
            author=get_or_create_author(s,community_id=c.id,source_author_id='fixture-author',display_name='Fixture Author')
            upsert_post(s,community_id=c.id,record={**record,'author_id':author.id})
        batch.classify_pending(db,load_requirements(),use_llm=True,export=False)
    with db.session() as s:
        post=s.scalar(select(Post));lead=s.scalar(select(Lead))
        assert post.classified and post.classification_retry_at is None
        assert post.classification_audit['outcome']==('lead' if positive else 'not_lead')
        assert post.classification_audit['validation_policy']==ai.VALIDATION_POLICY_VERSION
        assert post.classification_audit['excerpts'][0]['quote'] in text
        assert bool(lead)==positive
        assert bool(lead_to_ingest_payload(lead,post,post.community,post.author) if lead else None)==positive
        assert s.scalar(select(ActivityLog).where(ActivityLog.kind=='classify')).detail['excerpts']


@pytest.mark.parametrize('fields',[
    {'seeking_customers':True,'separate_buyer_need':False},
    {'seeking_customers':False,'separate_buyer_need':False},
    {'seeking_customers':'false','separate_buyer_need':True},
    {'seeking_customers':False,'separate_buyer_need':None},
])
def test_positive_with_inconsistent_or_missing_buying_direction_remains_error(fields):
    from tests.test_commercial_demand import Backend
    text='We launched our product and seek introductions to potential customers.'
    verdict=ai.classify_with_llm(text,Backend(answer(text,**fields)))
    assert verdict.error and verdict.failure_kind=='validation'
    assert verdict.classification=='UNCERTAIN' and not verdict.supporting_excerpts


def test_supplier_with_distinct_buyer_need_still_qualifies():
    from tests.test_commercial_demand import Backend
    text='We sell billing software. Separately we need a freelance designer for our website.'
    verdict=ai.classify_with_llm(text,Backend(answer(text,seeking_customers=True,
                                                   separate_buyer_need=True,post_purpose='mixed_demand')))
    assert not verdict.error and verdict.classification=='LEAD'
    assert verdict.described['seeking_customers']=='true'
