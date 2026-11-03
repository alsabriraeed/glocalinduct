"""Milestone 4 acceptance: seed admission, practice retries, consolidation, releases, resume."""
import asyncio
from pathlib import Path

import pytest

from glocalinduct.contracts import Feedback
from glocalinduct.providers.fake import FakeProvider
from glocalinduct.workflows import InferRequest, infer
from glocalinduct.workflows.consolidate import update_insights
from glocalinduct.workflows.evaluate import evaluate
from glocalinduct.workflows.ingest import ingest_pairs, split_groups, load_pairs
from glocalinduct.workflows.practice import collect_practice, practice_task
from glocalinduct.workflows.seed import build_seed_memory
from glocalinduct.workflows.common import RunStore
from glocalinduct.agents.runner import UsageMeter

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures' / 'corpus'


def test_ingest_and_split_are_frozen_and_reproducible(make_services):
    s = make_services()
    rep = ingest_pairs(s, CORPUS)
    assert len(rep.pairs) == 14 and not rep.exclusions
    assert all(p.group_id for p in rep.pairs)
    m = split_groups(s)
    m2 = split_groups(s)                           # second call returns the frozen manifest
    assert m.manifest_hash == m2.manifest_hash and m.feasibility_pilot is False
    assert m.counts['learning'] + m.counts['development'] + m.counts['test'] == 14
    assert m.counts['seed'] + m.counts['practice'] == m.counts['learning']
    # private references are stored separately from online source texts
    p = load_pairs(s)[0]
    assert s.private.has(p.arabic_hash) and not s.texts.has(p.arabic_hash)
    assert 'reference' not in p.source.to_dict()


def test_seed_practice_consolidate_pipeline(make_services):
    s = make_services()
    ingest_pairs(s, CORPUS); split_groups(s)
    seed = asyncio.run(build_seed_memory(s, 'M0'))
    assert seed['release']['release_id'] == 'M0' and seed['admitted'] == seed['seed_pairs'] > 0
    m0 = s.releases.get('M0')
    assert len(m0.demonstration_ids) == seed['admitted']
    # offline modes received the reference; online ones never did
    for c in s.provider.calls:
        if c['mode'] in ('analyse_pair', 'reference_quality'):
            assert 'reference' in c['bundle']['texts']
        if c['mode'] in ('analyse_source', 'make_plan', 'generate_arabic', 'candidate_quality'):
            assert 'reference' not in c['bundle']['texts']
    practice = asyncio.run(collect_practice(s, 'M0'))
    assert practice['tasks'] > 0 and practice['successes'] == practice['tasks']
    # each attempt record exists; successful practice attempts are eligible; references to M0 frozen
    attempts = [v for v in s.experiences.all() if v.kind == 'agent_attempt']
    assert attempts and all(v.record['release_id'] == 'M0' for v in attempts)
    assert all(v.eligible == (v.outcome == 'success') for v in attempts)
    m0_hash_before = m0.release_hash
    cons = asyncio.run(update_insights(s, 'M0', 'M1'))
    assert cons['release']['release_id'] == 'M1' and cons['release']['demonstrations'] > len(m0.demonstration_ids)
    assert s.releases.get('M0').release_hash == m0_hash_before        # M0 unchanged
    assert cons['active_insights'] >= 1 or cons['provisional_insights'] >= 1
    # inference on M1 can now substitute an incidental item with corpus support
    out = asyncio.run(infer(InferRequest(text='Lina buys pancakes near Main Street every Saturday for 3 dollars.',
                                         context={'domains': ['education'], 'locale_scope': 'general_arab'}), s, 'M1'))
    strategies = {d.item_id: d.strategy for d in out.attempt.plan.payload.decisions}
    assert 'substitute' in strategies.values()
    sub = [d for d in out.attempt.plan.payload.decisions if d.strategy == 'substitute'][0]
    assert sub.support[0].experience_id in s.releases.get('M1').demonstration_ids
    assert sub.counterpart_ar in out.result.arabic_text
    # data leakage guard removed (user decision): no group is excluded unless a caller asks for it
    p = [p for p in load_pairs(s) if p.pair_id == '000013'][0]
    out2 = asyncio.run(infer(InferRequest(text=p.source.normalized.text, context={'domains': ['education'],
                                                                                   'locale_scope': 'general_arab'}), s, 'M1'))
    assert out2.attempt.retrieval.excluded_group_ids == []
    # evaluation freezes predictions and reports reference compatibility separately
    rep = asyncio.run(evaluate(s, 'test', 'M1'))
    assert rep['n'] >= 1 and rep['resolved_rate'] is not None and 'compatible' in rep['reference_comparison']


def test_failed_reference_is_stored_but_never_a_demonstration(make_services):
    def failing_quality(bundle):
        crit = FakeProvider._criteria('reference_quality')
        crit[0] = {**crit[0], 'status': 'fail', 'score': 1}
        return {'mode': 'reference_quality', 'criteria': crit}
    s = make_services(overrides={'reference_quality': failing_quality})
    ingest_pairs(s, CORPUS); split_groups(s)
    seed = asyncio.run(build_seed_memory(s, 'M0'))
    assert seed['admitted'] == 0 and len(seed['excluded']) == seed['seed_pairs']
    assert s.releases.get('M0').demonstration_ids == []
    refs = [v for v in s.experiences.all() if v.kind == 'reference_example']
    assert refs and not any(v.eligible for v in refs)       # stored for audit, ineligible


def test_practice_repair_paths(make_services):
    # The Quality evaluator is reference-only: an incompatible comparison yields reference_informed feedback and a new plan
    state = {'cmp': 0}

    def compare(bundle):
        state['cmp'] += 1
        if state['cmp'] == 1:
            return {'mode': 'reference_comparison', 'result': 'incompatible', 'differences': [{
                'classification': 'candidate_defect', 'source_quotes': [], 'candidate_quotes': [], 'reference_quotes': [],
                'explanation': 'a quantity differs', 'correction_goal': 'preserve the quantity', 'critical': False}],
                'explanation': 'defect'}
        return {'mode': 'reference_comparison', 'result': 'compatible', 'differences': [], 'explanation': 'ok'}
    s = make_services(overrides={'reference_comparison': compare})
    ingest_pairs(s, CORPUS); split_groups(s)
    s.ensure_empty_release()
    pair = load_pairs(s)[0]
    store = RunStore(s.repo, s.config.path('runs'))
    task = asyncio.run(practice_task(pair, s, s.releases.get('M-EMPTY'), store, UsageMeter()))
    assert [a['outcome'] for a in task.attempts] == ['failure', 'success'] and task.success
    assert task.attempts[0]['plan_version'] == 1 and task.attempts[1]['plan_version'] == 2   # reference-informed: new plan
    assert not [c for c in s.provider.calls if c['mode'] == 'candidate_quality']             # no criteria audit
    plan_calls = [c for c in s.provider.calls if c['mode'] == 'make_plan']
    assert [f['scope'] for f in plan_calls[-1]['bundle']['feedback']] == ['reference_informed']
    assert 'reference' not in plan_calls[-1]['bundle']['texts']           # no reference text in planner input
    attempts = [v for v in s.experiences.all() if v.kind == 'agent_attempt']
    assert attempts[0].record['feedback_received'] is None
    assert attempts[1].record['feedback_received']['scope'] == 'reference_informed'


def test_attempt_limit_and_error_attempts(make_services):
    def always_incompatible(bundle):
        return {'mode': 'reference_comparison', 'result': 'incompatible', 'differences': [{
            'classification': 'candidate_defect', 'source_quotes': [], 'candidate_quotes': [], 'reference_quotes': [],
            'explanation': 'a quantity differs', 'correction_goal': 'preserve the quantity', 'critical': False}],
            'explanation': 'defect'}
    s = make_services(overrides={'reference_comparison': always_incompatible})
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    pair = load_pairs(s)[0]
    task = asyncio.run(practice_task(pair, s, s.releases.get('M-EMPTY'), RunStore(s.repo, s.config.path('runs')), UsageMeter()))
    assert len(task.attempts) == s.config.attempts.practice and not task.success and task.stop_reason in ('attempt_limit', 'identical_candidate')
    s2 = make_services(overrides={'generate_arabic': lambda b: {'nonsense': 1}})
    ingest_pairs(s2, CORPUS); split_groups(s2); s2.ensure_empty_release()
    task2 = asyncio.run(practice_task(load_pairs(s2)[0], s2, s2.releases.get('M-EMPTY'), RunStore(s2.repo, s2.config.path('runs')), UsageMeter()))
    assert task2.attempts[0]['outcome'] == 'error' and task2.stop_reason == 'unrecoverable_error'
    att = [v for v in s2.experiences.all() if v.kind == 'agent_attempt'][0]
    assert att.outcome == 'error' and not att.eligible and 'SCHEMA_INVALID' in att.record['issue_codes']


def test_resume_reuses_committed_stage_by_cache_key(make_services):
    s = make_services()
    out1 = asyncio.run(infer(InferRequest(text='Plain sentence.', context={}), s))
    # rerun with identical inputs: profile stage can be resumed from the checkpoint cache key
    rid = s.repo.find_by_cache_key(s.repo.checkpoints(out1.run_id)[0]['cache_key'])
    assert rid is not None and s.repo.payload(rid)['source_text_id'] == out1.attempt.source.source_text_id
