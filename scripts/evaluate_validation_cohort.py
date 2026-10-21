"""Offline semantic evaluation of an explicitly supplied cohort; never writes a DB.

Input is a private JSON object containing posts with content/context/metadata.
Default prints input counts only. --allow-provider explicitly permits sending
those private texts to OpenRouter / GPT-4o-mini. Output belongs outside Git.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cohort', type=Path)
    parser.add_argument('--allow-provider', action='store_true')
    args = parser.parse_args()
    data = json.loads(args.cohort.read_text())
    posts = data['posts']
    if not args.allow_provider:
        print(json.dumps({'posts':len(posts), 'maximum_model_calls':2*len(posts),
                          'provider_calls':0, 'database_writes':0, 'exports':0}))
        return
    from circle_leads.classifier.ai_classifier import classify_with_llm, OpenRouterBackend
    # Pin the reviewed destination/model; an unrelated environment change must
    # not silently send this private cohort to a different provider.
    backend = OpenRouterBackend(model='openai/gpt-4o-mini')
    for post in posts:
        verdict = classify_with_llm(post['content'], backend, model_name=backend.model,
                                    context=post.get('context', []),
                                    current_metadata=post.get('current_metadata'))
        print(json.dumps({'post_id':post['post_id'],
                          'source_sha256':hashlib.sha256(post['content'].encode()).hexdigest(),
                          'context_sha256':hashlib.sha256(json.dumps(post.get('context',[]),sort_keys=True).encode()).hexdigest(),
                          'baseline_error':post.get('audit',{}).get('error'),
                          'verdict':asdict(verdict)},ensure_ascii=False),flush=True)

if __name__ == '__main__':
    main()
