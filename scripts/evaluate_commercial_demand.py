"""Bounded real-model comparison. Only model requests are outbound.

Credentials come from the environment. Cached results resume an interrupted
run. Local SQLite and payload serialization never contact the ingest endpoint.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import ModuleType
from unittest.mock import patch
from urllib.parse import quote

from sqlalchemy import select
from circle_leads.classifier.ai_classifier import AnthropicBackend, OpenAIBackend, OpenRouterBackend
from circle_leads.classifier.lead_classifier import classify, meets_requirements
from circle_leads.config.settings import load_requirements
from circle_leads.export.vini_ingest import lead_to_ingest_payload
from circle_leads.pipeline import classify_pending
from circle_leads.storage.database import Database, get_or_create_author, get_or_create_community, upsert_post
from circle_leads.storage.models import Lead, Post

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '0268e44670804036cb5faa3a367c5a04bdb960da'
CLASSIFIER_HASH = hashlib.sha256(b''.join((ROOT/p).read_bytes() for p in [
    'circle_leads/classifier/ai_classifier.py', 'circle_leads/classifier/lead_classifier.py',
    'circle_leads/classifier/keyword_rules.py', 'circle_leads/classifier/decisions.py'])).hexdigest()
LOCAL_EXPORT_LOCK = threading.Lock()


def baseline_classifier():
    import circle_leads.classifier as package
    names = ['circle_leads.classifier.keyword_rules','circle_leads.classifier.ai_classifier',
             'circle_leads.classifier.lead_classifier']
    originals = {n: sys.modules.get(n) for n in names}
    original_rules = package.keyword_rules
    try:
        for name in names:
            path = name.replace('.', '/') + '.py'
            source = subprocess.check_output(['git','show',f'{BASELINE}:{path}'],cwd=ROOT,text=True)
            module = ModuleType(name)
            sys.modules[name] = module
            if name.endswith('keyword_rules'): package.keyword_rules = module
            exec(compile(source,path,'exec'),module.__dict__)
        return sys.modules[names[-1]].classify, sys.modules[names[-1]].meets_requirements
    finally:
        for name,value in originals.items():
            if value is None: sys.modules.pop(name,None)
            else: sys.modules[name] = value
        package.keyword_rules = original_rules


class ReplayBackend:
    def __init__(self,raw,model): self.raw,self.model = raw,model
    def complete(self,system,user): return self.raw


class RecordingBackend:
    def __init__(self,backend): self.backend,self.model,self.raw = backend,backend.model,None
    def complete(self,system,user):
        self.raw = self.backend.complete(system,user)
        return self.raw


def context(case):
    return [{'source_id':f"post:{x['id']}",'content':x['content'],
             'author_id':x.get('author_id'),'url':x.get('url')}
            for x in case.get('context',[])]


def local_export(case,raw,model,req):
    # patch() changes module globals: concurrent fixture replays could swap
    # their recorded answers/context. Provider inference stays parallel; local
    # pipeline verification must run under one lock.
    with LOCAL_EXPORT_LOCK:
        return _local_export(case,raw,model,req)


def _local_export(case,raw,model,req):
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(f'sqlite:///{tmp}/eval.db')
        capture = datetime.fromisoformat(case.get('scraped_at') or '2026-09-29 12:01:00')
        with db.session() as session:
            c = get_or_create_community(session,slug='evaluation',url='https://evaluation.circle.so')
            author = get_or_create_author(session,community_id=c.id,
                source_author_id=case.get('source_author_id') or 'fixture-author',
                display_name=case.get('display_name') or 'Fixture buyer')
            post,_ = upsert_post(session,community_id=c.id,record={
                'source_content_id':'fixture','content':case['text'],
                'url':case.get('url') or case.get('provenance',{}).get('url') or 'https://evaluation.circle.so/c/post/fixture',
                'author_id':author.id,'published_at':datetime.fromisoformat(case.get('published_at') or '2026-09-29 12:00:00')})
            post.scraped_at = capture
        def forbidden(*a,**k): raise AssertionError('Outbound side effect attempted')
        # Re-evaluate the recorded response with identical source context; no
        # second provider call. Then run the real candidate/storage path.
        with patch('circle_leads.pipeline.make_backend',return_value=ReplayBackend(raw,model)), \
             patch('circle_leads.classifier.decisions.context_for_post',return_value=context(case)), \
             patch('circle_leads.pipeline.push_leads_by_ids',side_effect=forbidden), \
             patch('circle_leads.notify.notify',side_effect=forbidden), \
             patch('requests.sessions.Session.request',side_effect=forbidden), \
             patch('circle_leads.classifier.decisions.utcnow',return_value=capture):
            stats = classify_pending(db,req,use_llm=True,limit=1,export=False)
        with db.session() as session:
            lead = session.scalar(select(Lead))
            post = session.scalar(select(Post).where(Post.source_content_id=='fixture'))
            if lead:
                lead.created_at = capture  # freeze decision time, retain source timestamps
            payload = lead_to_ingest_payload(lead,post,post.community,post.author) if lead else None
            return {'stats':stats,'export_eligible':payload is not None,
                    'fresh_at_capture':capture-post.published_at <= timedelta(hours=48),
                    'expired_at_evaluation':datetime.now(timezone.utc).replace(tzinfo=None)-post.published_at > timedelta(hours=48),
                    'payload':payload,'audit':post.classification_audit}


def load_existing_vercel_openai_key(project, team):
    """Reuse a project's production key in memory; never persist or print it."""
    def get(path):
        result = subprocess.run(['vercel', 'api', path, '--method', 'GET'],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Vercel read-only credential retrieval failed; check project/scope access')
        return json.loads(result.stdout)
    project, team = quote(project, safe=''), quote(team, safe='')
    envs = get(f'/v9/projects/{project}/env?teamId={team}')['envs']
    candidates = [x for x in envs if x['key'] == 'OPENAI_API_KEY'
                  and 'production' in x.get('target', [])]
    if len(candidates) != 1:
        raise RuntimeError('Expected one production OPENAI_API_KEY in the selected project')
    env_id = quote(candidates[0]['id'], safe='')
    value = get(f'/v1/projects/{project}/env/{env_id}?teamId={team}').get('value')
    if not isinstance(value, str) or not value:
        raise RuntimeError('Selected production credential is unavailable')
    os.environ['OPENAI_API_KEY'] = value


def preflight_provider(backend):
    """Fail fast on provider/account access errors; no post content is sent."""
    try:
        backend._client.models.list()
    except Exception as exc:
        status = getattr(exc, 'status_code', None)
        raise RuntimeError(
            f'Provider access preflight failed ({type(exc).__name__}, status={status}); '
            'no cohort inference requests were scheduled.'
        ) from None


def regression_matches(case, data):
    expected = case['expected']
    new, replay = data['new'], data['local_export']
    return (not new['result']['llm_error'] and replay['stats']['errors'] == 0
            and new['result']['classification'] == ('LEAD' if expected else 'NOT_LEAD')
            and replay['audit']['outcome'] == ('lead' if expected else 'not_lead')
            and new['eligible'] == expected and replay['export_eligible'] == expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--provider',choices=['anthropic','openai','openrouter'],required=True)
    parser.add_argument('--model',required=True)
    parser.add_argument('--env-file')
    parser.add_argument('--vercel-project', help='Reuse this existing Circle project production key in memory')
    parser.add_argument('--vercel-team', help='Explicit Vercel team ID for read-only credential retrieval')
    parser.add_argument('--fixtures', type=Path, default=ROOT/'reports/commercial-demand/private/regressions.json')
    parser.add_argument('--cohort', type=Path, default=ROOT/'reports/commercial-demand/private/exit-five-100.json')
    parser.add_argument('--output', type=Path, help='Private comparison output path for a separate evaluation cohort')
    parser.add_argument('--regressions-only',action='store_true')
    parser.add_argument('--local-only',action='store_true',help='Replay existing responses; never call a provider')
    parser.add_argument('--retry-errors',action='store_true',help='Retry cached processing failures; retain prior receipts')
    args = parser.parse_args()
    if args.vercel_project or args.vercel_team:
        if not args.vercel_project or not args.vercel_team or args.provider != 'openai':
            parser.error('Vercel credential reuse requires project, team and provider=openai')
        if not args.local_only:
            load_existing_vercel_openai_key(args.vercel_project, args.vercel_team)
    if args.env_file:
        from dotenv import load_dotenv
        load_dotenv(args.env_file,override=False)
    for k in ['VINI_API_SECRET','SUPABASE_ANON_KEY','VINI_SUPABASE_ANON_KEY','TELEGRAM_BOT_TOKEN']:
        os.environ.pop(k,None)
    req = load_requirements()
    old,old_meets = baseline_classifier()
    folder = ROOT/'reports/commercial-demand'
    cases = json.loads(args.fixtures.read_text())['cases']
    if not args.regressions_only:
        cases += [{**p,'case':f"cohort-{p['id']}",'text':p['content']}
                  for p in json.loads(args.cohort.read_text())['posts']]
    cache = folder/'decisions'/f'{args.provider}-{args.model.replace("/", "_")}'
    cache.mkdir(parents=True,exist_ok=True)
    factory = {'anthropic':AnthropicBackend,'openai':OpenAIBackend,'openrouter':OpenRouterBackend}[args.provider]
    if not args.local_only:
        # Check credentials without disclosing community content. A global auth
        # failure must abort before scheduling the entire private cohort.
        preflight_provider(factory(model=args.model))
    def evaluate(case):
        path = cache/(case['case']+'.json')
        input_hash = hashlib.sha256(json.dumps(case,sort_keys=True).encode()).hexdigest()
        previous = None
        if path.exists():
            data=json.loads(path.read_text())
            if data['model']!=args.model or data['baseline_ref']!=BASELINE or data.get('input_hash')!=input_hash or data['provider']!=args.provider:
                raise ValueError('Cache model/baseline mismatch')
            if data.get('classifier_hash')==CLASSIFIER_HASH and not (args.retry_errors and data.get('processing_failure')):
                if case.get('expected') is not None and data['new']['raw']:
                    data['local_export']=local_export(case,data['new']['raw'],args.model,req)
                    data['regression_pass']=regression_matches(case,data)
                    path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
                return data
            if args.local_only:
                raise ValueError('Cannot refresh changed classifier or retry provider errors in local-only mode')
            previous = data
            # Preserve previous prompt/error evidence rather than replacing it silently.
            archive = path.with_name(path.stem + '.attempt-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.json')
            archive.write_text(path.read_text())
        if args.local_only:
            raise ValueError(f'Missing cached response: {path}; local-only forbids provider calls')
        data={'case':case['case'],'model':args.model,'provider':args.provider,
              'baseline_ref':BASELINE,'classifier_hash':CLASSIFIER_HASH,'input_hash':input_hash,
              'expected':case.get('expected')}
        for name,fn,check in [('old',old,old_meets),('new',classify,meets_requirements)]:
            if name=='old' and previous is not None and not previous['old']['result']['llm_error']:
                data['old']=previous['old']
                continue
            backend=RecordingBackend(factory(model=args.model))
            kwargs={'llm':backend,'model_name':args.model}
            if name=='new':
                kwargs['context']=context(case)
                kwargs['current_metadata']={k:case.get(k) for k in
                    ('id','url','author_id','source_author_id','source_content_id')}
            result=fn(case['text'],req,**kwargs)
            data[name]={'result':asdict(result),'eligible':check(result,req),'raw':backend.raw}
        data['changed']=(data['old']['eligible']!=data['new']['eligible'] or
                         data['old']['result']['classification']!=data['new']['result']['classification'])
        data['processing_failure']=bool(data['old']['result']['llm_error'] or data['new']['result']['llm_error'])
        if case.get('expected') is not None and data['new']['raw']:
            data['local_export']=local_export(case,data['new']['raw'],args.model,req)
            data['regression_pass']=regression_matches(case,data)
        path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
        print(f"{case['case']}: {data['old']['eligible']} -> {data['new']['eligible']}; error={data['processing_failure']}",flush=True)
        return data
    with ThreadPoolExecutor(max_workers=3) as pool: results=list(pool.map(evaluate,cases))
    (args.output or folder/'comparison.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(json.dumps({'evaluated':len(results),'changed':sum(x['changed'] for x in results),
                      'errors':sum(x['processing_failure'] for x in results),
                      'regressions_passed':sum(x.get('regression_pass',False) for x in results)}))

if __name__=='__main__': main()
