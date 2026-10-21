"""Offline semantic evaluation of an explicitly supplied cohort; never writes a DB.

Input is a private JSON object containing posts with content/context/metadata.
Default prints input counts only. --allow-provider explicitly permits sending
those private texts to OpenRouter / GPT-4o-mini. Output belongs outside Git.
"""
from __future__ import annotations
import argparse
import fcntl
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path


class _BudgetedBackend:
    """Reserve every actual outbound call against one durable approval ledger."""
    def __init__(self, backend, ledger: Path, maximum: int):
        self.backend, self.ledger, self.maximum = backend, ledger, maximum
        self.model = backend.model
        self.calls = 0
        self.exhausted = False

    def complete(self, system, user):
        # Lock/check/persist before sending, across runner restarts. Failed
        # provider requests consume their reservation too; never refund/retry.
        with self.ledger.open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            text = handle.read()
            state = json.loads(text) if text else {'maximum':self.maximum,'spent':0}
            if state['maximum'] != self.maximum:
                raise RuntimeError('Approval ledger limit mismatch')
            if state['spent'] >= self.maximum:
                self.exhausted = True
                raise RuntimeError('Approved model-call budget exhausted')
            state['spent'] += 1
            handle.seek(0)
            handle.truncate()
            json.dump(state,handle)
            handle.flush()
            os.fsync(handle.fileno())
        self.calls += 1
        return self.backend.complete(system,user)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cohort', type=Path)
    parser.add_argument('--allow-provider', action='store_true')
    parser.add_argument('--expected-classifier-sha256', required=False,
                        help='Required before live calls: SHA-256 of the reviewed classifier file.')
    parser.add_argument('--budget-ledger', type=Path,
                        help='Reuse this file across all runs under the same approval.')
    parser.add_argument('--max-calls', type=int,
                        help='Explicit approved total across runs sharing the ledger.')
    args = parser.parse_args()
    data = json.loads(args.cohort.read_text())
    posts = data['posts']
    if not args.allow_provider:
        print(json.dumps({'posts':len(posts), 'maximum_model_calls':2*len(posts),
                          'provider_calls':0, 'database_writes':0, 'exports':0}))
        return
    from circle_leads.classifier import ai_classifier as ai
    classifier_sha256 = hashlib.sha256(Path(ai.__file__).read_bytes()).hexdigest()
    if args.expected_classifier_sha256 != classifier_sha256:
        parser.error('Live evaluation requires the exact --expected-classifier-sha256; no calls made.')
    classify_with_llm, OpenRouterBackend = ai.classify_with_llm, ai.OpenRouterBackend
    # Pin the reviewed destination/model; an unrelated environment change must
    # not silently send this private cohort to a different provider.
    if args.budget_ledger is None or args.max_calls is None or args.max_calls <= 0:
        parser.error('Live evaluation requires --budget-ledger and positive --max-calls.')
    backend = _BudgetedBackend(OpenRouterBackend(model='openai/gpt-4o-mini'),
                               args.budget_ledger, args.max_calls)
    for post in posts:
        previous_calls = backend.calls
        verdict = classify_with_llm(post['content'], backend, model_name=backend.model,
                                    context=post.get('context', []),
                                    current_metadata=post.get('current_metadata'))
        print(json.dumps({'post_id':post['post_id'],
                          'source_sha256':hashlib.sha256(post['content'].encode()).hexdigest(),
                          'context_sha256':hashlib.sha256(json.dumps(post.get('context',[]),sort_keys=True).encode()).hexdigest(),
                          'baseline_error':post.get('audit',{}).get('error'),
                          'classifier_sha256':classifier_sha256,
                          'classifier_version':ai.CLASSIFIER_VERSION,
                          'policy_version':ai.POLICY_VERSION,
                          'validation_policy':ai.VALIDATION_POLICY_VERSION,
                          'system_prompt_sha256':hashlib.sha256(ai.SYSTEM_PROMPT.encode()).hexdigest(),
                          'model_calls':backend.calls - previous_calls,
                          'verdict':asdict(verdict)},ensure_ascii=False),flush=True)
        if backend.exhausted:
            raise SystemExit('Approved call budget exhausted; no further requests sent.')

if __name__ == '__main__':
    main()
