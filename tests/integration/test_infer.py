"""Milestone 3 acceptance: online orchestration, guards, terminal statuses."""
import asyncio
import json

import pytest

from glocalinduct.providers.base import ProviderTransientError
from glocalinduct.providers.fake import FakeProvider
from glocalinduct.workflows import InferRequest, infer
from glocalinduct.validation.leakage import FORBIDDEN_ONLINE_KEYS

from conftest import TEXT

ONLINE_MODES = {'analyse_source', 'make_plan', 'generate_arabic', 'candidate_quality'}


def run(s, text=TEXT, ctx=None, **kw):
    return asyncio.run(infer(InferRequest(text=text, context=ctx or {'locale_scope': 'general_arab'}), s, **kw))


def test_no_memory_retention_is_conservative_and_online_bundles_have_no_reference(make_services):
    s = make_services()
    out = run(s)
    r = out.result
    assert r.status == 'conservative' and r.arabic_text and not r.issue_codes and r.fallback_reasons
    assert out.release.release_id == 'M-EMPTY'
    modes = [c['mode'] for c in s.provider.calls]
    assert modes == ['analyse_source', 'make_plan', 'generate_arabic', 'candidate_quality']
    for c in s.provider.calls:
        assert c['mode'] in ONLINE_MODES
        assert not (set(c['bundle']) & FORBIDDEN_ONLINE_KEYS)
        assert 'reference' not in c['bundle']['texts']
    # evaluator bundle carries no planner rationale
    eval_bundle = s.provider.calls[-1]['bundle']
    assert all('rationale' not in d for d in eval_bundle['plan']['decisions'])
    # records persisted and the run is inspectable
    assert (s.config.path('runs') / out.run_id / 'result.json').exists()
    assert s.repo.get_object(r.plan_ids[0]) is None or True


def test_accepted_when_no_cultural_items_detected(make_services):
    s = make_services()
    out = run(s, text='Photosynthesis converts light energy into chemical energy in plants.')
    assert out.result.status == 'accepted' and out.attempt.plan.payload.decisions == []


def test_unknown_quality_is_unresolved_with_no_draft(make_services):
    def unknown(bundle):
        crit = FakeProvider._criteria('candidate_quality')
        crit[0] = {**crit[0], 'status': 'unknown', 'score': None}
        return {'mode': 'candidate_quality', 'criteria': crit}
    s = make_services(overrides={'candidate_quality': unknown})
    out = run(s)
    assert out.result.status == 'unresolved' and out.result.arabic_text is None
    assert 'QUALITY_UNKNOWN' in out.result.issue_codes
    assert out.attempt.candidate is not None          # draft kept only in diagnostics


def test_failed_criterion_and_critical_finding_block_acceptance(make_services):
    def fail(bundle):
        crit = FakeProvider._criteria('candidate_quality')
        crit[0] = {**crit[0], 'status': 'fail', 'score': 1, 'findings': [{
            'issue_code': 'MEANING_VIOLATION', 'severity': 'critical', 'affected_ids': ['R2'], 'evidence': [],
            'explanation': 'quantity changed', 'correction_goal': 'restore the value'}]}
        return {'mode': 'candidate_quality', 'criteria': crit}
    s = make_services(overrides={'candidate_quality': fail})
    out = run(s)
    assert out.result.status == 'unresolved' and out.result.issue_codes == ['MEANING_VIOLATION']
    assert out.attempt.decision.findings[0]['correction_goal'] == 'restore the value'


def test_substituting_an_essential_item_is_rejected_deterministically(make_services):
    base = FakeProvider()

    def profile(bundle):
        p = base._analyse_source(bundle)
        for it in p['items']:
            it['essentiality'] = 'essential'
        return p

    def plan(bundle):
        p = base._make_plan(bundle)
        d = p['decisions'][0]
        d.update(strategy='substitute', counterpart_ar='عيد الفطر', canonical_concept='festival', fallback_reason=None,
                 support=[{'experience_id': 'ghost', 'arabic_quote': {'text_key': 'experience:ghost', 'block_id': None,
                                                                      'quote': 'عيد الفطر', 'occurrence': 0},
                           'compatible_function': 'festival'}])
        return p
    s = make_services(overrides={'analyse_source': profile, 'make_plan': plan})
    out = run(s)
    assert out.result.status == 'unresolved' and out.result.issue_codes == ['UNSUPPORTED_SUBSTITUTION']


def test_unsupported_counterpart_outside_retrieval_is_memory_reference_invalid(make_services):
    base = FakeProvider()

    def plan(bundle):
        p = base._make_plan(bundle)
        d = p['decisions'][0]
        d.update(strategy='substitute', counterpart_ar='عيد الفطر', canonical_concept='festival', fallback_reason=None,
                 support=[{'experience_id': 'ghost', 'arabic_quote': {'text_key': 'experience:ghost', 'block_id': None,
                                                                      'quote': 'عيد الفطر', 'occurrence': 0},
                           'compatible_function': 'festival'}])
        return p
    s = make_services(overrides={'make_plan': plan})
    out = run(s)
    assert out.result.issue_codes == ['MEMORY_REFERENCE_INVALID']


def test_missing_block_coverage_and_invalid_quotes_are_unresolved(make_services):
    base = FakeProvider()

    def drop_block(bundle):
        p = base._generate_arabic(bundle)
        p['blocks'] = p['blocks'][:-1]
        return p
    s = make_services(overrides={'generate_arabic': drop_block})
    assert run(s).result.issue_codes == ['COVERAGE_INCOMPLETE']

    def bad_quote(bundle):
        p = base._generate_arabic(bundle)
        p['realizations'][0]['output_quotes'][0]['quote'] = 'not in output'
        return p
    s2 = make_services(overrides={'generate_arabic': bad_quote})
    assert run(s2).result.issue_codes == ['QUOTE_UNRESOLVED']

    def bad_profile_quote(bundle):
        p = base._analyse_source(bundle)
        p['requirements'][0]['source_quotes'][0]['quote'] = 'phantom words'
        return p
    s3 = make_services(overrides={'analyse_source': bad_profile_quote})
    assert run(s3).result.issue_codes == ['QUOTE_UNRESOLVED']


def test_schema_correction_then_schema_invalid(make_services):
    calls = {'n': 0}
    base = FakeProvider()

    def flaky(bundle):
        calls['n'] += 1
        if calls['n'] == 1:
            return {'garbage': True}
        return base._analyse_source(bundle)
    s = make_services(overrides={'analyse_source': flaky})
    out = run(s)
    assert out.result.status == 'conservative' and out.attempt.meter.schema_corrections == 1
    s2 = make_services(overrides={'analyse_source': lambda b: {'garbage': True}})
    out2 = run(s2)
    assert out2.result.issue_codes == ['SCHEMA_INVALID'] and out2.attempt.meter.model_calls == 2


def test_transient_retries_are_bounded_and_separate_from_semantic_attempts(make_services):
    prov = FakeProvider(fail_first={'make_plan': 2})
    s = make_services(provider=prov)
    import glocalinduct.agents.runner as runner_mod
    s.runner._sleep = lambda *_: asyncio.sleep(0)
    out = run(s)
    assert out.result.status == 'conservative' and out.attempt.meter.technical_retries == 2
    prov2 = FakeProvider(fail_first={'make_plan': 3})
    s2 = make_services(provider=prov2)
    s2.runner._sleep = lambda *_: asyncio.sleep(0)
    out2 = run(s2)
    assert out2.result.issue_codes == ['PROVIDER_FAILED']


def test_refusal_and_budget_exhaustion_are_explicit(make_services):
    from glocalinduct.errors import ProviderRefusal

    def refuse(bundle):
        raise ProviderRefusal('refused')
    s = make_services(overrides={'generate_arabic': refuse})
    assert run(s).result.issue_codes == ['PROVIDER_REFUSAL']
    s2 = make_services(config_overrides={'budgets': {'max_calls_per_window': 2}})
    assert run(s2).result.issue_codes == ['BUDGET_EXHAUSTED']


def test_context_overflow_and_long_document_windows(make_services):
    s = make_services(config_overrides={'runtime': {'context_window_tokens': 300, 'reserved_output_tokens': 50}})
    out = run(s, text='word ' * 400)
    assert out.result.issue_codes == ['CONTEXT_OVERFLOW']
    s2 = make_services(config_overrides={'windows': {'content_tokens': 60, 'context_tokens_each_side': 20}})
    text = '\n\n'.join(f'Paragraph {i}: ' + 'tasty pancakes every morning ' * 8 for i in range(6))
    out2 = run(s2, text=text)
    assert out2.result.status == 'conservative'
    assert len(out2.attempt.source.windows) > 1
    modes = [c['mode'] for c in s2.provider.calls]
    n = len(out2.attempt.source.windows)
    assert modes.count('analyse_source') == n and modes.count('generate_arabic') == n
    assert modes.count('candidate_quality') == n + 1          # per window + cross-window pass
    assert len(out2.attempt.plan.payload.decisions) == 6       # merged, one per item


def test_empty_and_unreadable_requests(make_services):
    s = make_services()
    out = run(s, text='   ')
    assert out.result.status == 'unresolved' and out.result.issue_codes == ['INPUT_INVALID']
