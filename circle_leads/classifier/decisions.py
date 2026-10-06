"""Shared post evaluation, context and transactional decision receipts."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from urllib.parse import urldefrag

from sqlalchemy import or_, select

from circle_leads.classifier.ai_classifier import POLICY_VERSION, SCOUT_POLICY_REF
from circle_leads.classifier.lead_classifier import ClassificationResult, classify
from circle_leads.storage.activity import log_activity
from circle_leads.storage.models import Post, utcnow


def context_for_post(session, post, supplied=None):
    clauses = []
    if post.thread_id:
        clauses.append(Post.thread_id == post.thread_id)
    root = urldefrag(post.url or '')[0]
    if not post.thread_id and '/c/' in root:
        clauses.extend([Post.url == root, Post.url.startswith(root + '#', autoescape=True)])
    items = list(supplied or [])
    if clauses:
        siblings = session.scalars(select(Post).where(
            Post.community_id == post.community_id, Post.id != post.id, or_(*clauses)
        ).order_by(Post.published_at, Post.id)).all()
        for sibling in siblings:
            items.append({'source_id': f'post:{sibling.id}', 'content': sibling.content,
                          'author_id': sibling.author_id,
                          'source_author_id': sibling.author.source_author_id if sibling.author else None,
                          'url': sibling.url,
                          'published_at': str(sibling.published_at)})
    seen = {(post.content, post.url)}
    unique = []
    for item in items:
        key = (item['content'], item.get('url'))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def evaluate_post(session, post, requirements, *, llm=None, model_name=None,
                  semantic_requested=False, supplied_context=None):
    context = context_for_post(session, post, supplied_context)
    if semantic_requested and llm is None:
        result = ClassificationResult(classification='UNCERTAIN',
                                      reason='semantic_backend_unavailable',
                                      llm_error='semantic_backend_unavailable')
    else:
        result = classify(post.content, requirements, llm=llm,
                          model_name=model_name, context=context,
                          current_metadata={"url": post.url, "author_id": post.author_id,
                                            "source_author_id": post.author.source_author_id if post.author else None,
                                            "post_id": post.id, "source_content_id": post.source_content_id})
    now = utcnow().replace(tzinfo=None)
    previous = post.classification_audit or {}
    content_hash = hashlib.sha256(post.content.encode()).hexdigest()
    context_hash = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    attempts = (previous.get('attempt', 0) if previous.get('content_hash') == content_hash
                and previous.get('context_hash') == context_hash else 0) + 1
    outcome = ('error' if result.llm_error else
               'filtered_confidence' if result.is_lead and
               result.confidence < requirements.minimum_confidence else
               'lead' if result.is_lead else 'not_lead')
    references = {x['source_id']: x for x in context}
    references['current'] = {'url': post.url, 'author_id': post.author_id,
                             'source_content_id': post.source_content_id}
    excerpts = [{**x, 'source': {k: v for k, v in references.get(x['source_id'], {}).items()
                               if k != 'content'}} for x in result.supporting_excerpts]
    audit = {'outcome': outcome, 'reason': result.reason, 'error': result.llm_error,
             'excerpts': excerpts,
             'policy_version': POLICY_VERSION, 'scout_policy_ref': SCOUT_POLICY_REF,
             'classifier_version': result.classifier_version, 'model': result.model,
             'content_hash': content_hash, 'context_hash': context_hash,
             'attempted_at': now.isoformat() + 'Z', 'attempt': attempts,
             'confidence': result.confidence, 'demand_signal': result.demand_signal,
             'awareness': result.awareness, 'described': result.described,
             'context_sources': [x['source_id'] for x in context]}
    post.classification_audit = audit
    post.classified = outcome != 'error'
    post.classification_retry_at = (now + timedelta(minutes=(1, 5, 15, 60)[min(attempts-1, 3)])
                                    if outcome == 'error' else None)
    log_activity(session, kind='classify', community=post.community.slug,
                 level='error' if outcome == 'error' else 'info',
                 summary=f'{outcome}: post {post.id}',
                 detail={'post_id': post.id, **audit}, decided_by=result.decided_by,
                 items_seen=1)
    return result
