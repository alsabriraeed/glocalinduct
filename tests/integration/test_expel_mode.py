"""ExpeL-aligned learning mode (`learning.*`): empty M0, comparison-only success, free-text reflections,
every failure paired with a success, insights active immediately."""
import asyncio
import json
from pathlib import Path

from glocalinduct.agents.runner import UsageMeter
from glocalinduct.providers.fake import FakeProvider
from glocalinduct.validation.leakage import FORBIDDEN_ONLINE_KEYS
from glocalinduct.workflows.common import RunStore
from glocalinduct.workflows.consolidate import update_insights
from glocalinduct.workflows.ingest import ingest_pairs, load_pairs, split_groups
from glocalinduct.workflows.practice import collect_practice, practice_task
from glocalinduct.workflows.seed import build_seed_memory

from conftest import EXPEL_LEARNING

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures' / 'corpus'
CFG = {'learning': EXPEL_LEARNING}


def _incompatible(explanation='a quantity differs'):
    return {'mode': 'reference_comparison', 'result': 'incompatible', 'differences': [{
        'classification': 'candidate_defect', 'source_quotes': [], 'candidate_quotes': [], 'reference_quotes': [],
        'explanation': explanation, 'correction_goal': 'preserve the quantity', 'critical': False}],
        'explanation': 'defect'}


def _compatible():
    return {'mode': 'reference_comparison', 'result': 'compatible', 'differences': [], 'explanation': 'ok'}


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def test_seed_is_empty_and_references_are_never_demonstrations(make_services):
    s = make_services(config_overrides=CFG)
    ingest_pairs(s, CORPUS); split_groups(s)
    seed = asyncio.run(build_seed_memory(s, 'M0'))
    assert seed['seed_pairs'] == 0 and seed['admitted'] == 0 and s.releases.get('M0').demonstration_ids == []
    assert not [c for c in s.provider.calls if c['mode'] in ('analyse_pair', 'reference_quality')]
    assert not [v for v in s.experiences.all() if v.kind == 'reference_example']


def test_reflection_accumulates_and_sees_the_glocalized_pair(make_services):
    state = {'cmp': 0}

    def compare(bundle):
        state['cmp'] += 1
        return _incompatible() if state['cmp'] <= 2 else _compatible()
    s = make_services(overrides={'reference_comparison': compare}, config_overrides=CFG)
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    pair = load_pairs(s)[0]
    task = asyncio.run(practice_task(pair, s, s.releases.get('M-EMPTY'), RunStore(s.repo, s.config.path('runs')),
                                     UsageMeter()))
    assert [a['outcome'] for a in task.attempts] == ['failure', 'failure', 'success'] and task.success
    refl = [c for c in s.provider.calls if c['mode'] == 'reflect_failure']
    assert len(refl) == 2
    assert refl[0]['bundle']['earlier_reflections'] == [] and len(refl[1]['bundle']['earlier_reflections']) == 1
    reference = s.private.get(pair.arabic_hash).text
    for c in refl:
        # the reflector is given the pair's glocalized Arabic so it can write hints and corrections
        assert c['bundle']['glocalized_pair']['glocalized_arabic'] == reference
        assert not (set(_keys(c['bundle'])) & FORBIDDEN_ONLINE_KEYS)
        assert c['bundle']['signals']['comparison']['result'] == 'incompatible'
        assert 'glocalized_quotes' in c['bundle']['signals']['comparison']['differences'][0]
    # the planner never receives the raw reference text, only the reflection derived from it
    for c in [c for c in s.provider.calls if c['mode'] == 'make_plan']:
        assert reference not in json.dumps(c['bundle'], ensure_ascii=False)
    # the planner sees the free-text reflections of ALL earlier failed attempts
    last_plan = [c for c in s.provider.calls if c['mode'] == 'make_plan'][-1]
    fbs = last_plan['bundle']['feedback']
    assert len(fbs) == 2 and all(f['reflection'] for f in fbs)
    attempts = sorted((v for v in s.experiences.all() if v.kind == 'agent_attempt'),
                      key=lambda v: v.record['attempt_number'])
    assert attempts[0].record['feedback_received'] is None and attempts[2].record['feedback_received']['reflection']


def test_success_is_decided_by_the_reference_comparison_alone(make_services):
    def failing_quality(bundle):
        crit = FakeProvider._criteria('candidate_quality')
        crit[0] = {**crit[0], 'status': 'fail', 'score': 1}
        return {'mode': 'candidate_quality', 'criteria': crit}
    s = make_services(overrides={'candidate_quality': failing_quality}, config_overrides=CFG)
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    task = asyncio.run(practice_task(load_pairs(s)[0], s, s.releases.get('M-EMPTY'),
                                     RunStore(s.repo, s.config.path('runs')), UsageMeter()))
    assert task.success and [a['outcome'] for a in task.attempts] == ['success']
    att = [v for v in s.experiences.all() if v.kind == 'agent_attempt'][0]
    assert att.eligible and att.outcome == 'success'


def test_a5_criteria_are_not_consulted_when_a_reference_exists(make_services):
    def must_not_run(bundle):
        raise AssertionError('the three-criteria audit must not run when a reference exists')
    s = make_services(overrides={'candidate_quality': must_not_run}, config_overrides=CFG)
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    task = asyncio.run(practice_task(load_pairs(s)[0], s, s.releases.get('M-EMPTY'),
                                     RunStore(s.repo, s.config.path('runs')), UsageMeter()))
    assert task.success and not task.attempts[0]['issue_codes']


def test_every_failure_is_paired_and_insights_are_active_immediately(make_services):
    state = {'cmp': 0}

    def compare(bundle):
        state['cmp'] += 1
        return _incompatible() if state['cmp'] <= 2 else _compatible()
    s = make_services(overrides={'reference_comparison': compare}, config_overrides=CFG)
    ingest_pairs(s, CORPUS); split_groups(s)
    asyncio.run(build_seed_memory(s, 'M0'))
    practice = asyncio.run(collect_practice(s, 'M0'))
    assert practice['tasks'] > 1 and practice['successes'] == practice['tasks']       # seed subset is practised too
    cons = asyncio.run(update_insights(s, 'M0', 'M1'))
    assert cons['new_references'] == 0 and cons['packets']['reference'] == 0
    assert cons['packets']['failure_success'] == 2                                      # both failures of task 1
    assert cons['provisional_insights'] == 0 and cons['active_insights'] >= 1
    assert all(i.status in ('active', 'retired') for i in s.insights.heads())
    demos = set(s.releases.get('M1').demonstration_ids)
    assert demos and all(s.experiences.get(d).kind == 'agent_attempt' for d in demos)


def test_insight_alone_may_justify_a_substitution():
    from glocalinduct.contracts import Decision
    base = dict(decision_id='d1', item_id='c1', strategy='substitute', counterpart_ar='100 درجة مئوية',
                canonical_concept='boiling temperature', term_policy='fixed', support=[], preserve_requirement_ids=[],
                dependent_edits=[], fallback_reason=None, rationale='r')
    ok = Decision(**base, insight_refs=[{'insight_id': 'ins_1', 'version': 1}])
    assert ok.insight_refs and not ok.support
    # an ungrounded substitution passes the schema; validation.plans rejects it at inference time
    assert Decision(**base, insight_refs=[]).strategy == 'substitute'


def test_quality_evaluator_is_reference_only_when_a_reference_exists(make_services):
    calls = {'candidate_quality': 0, 'compare_reference': 0}

    def blind_quality(bundle):
        calls['candidate_quality'] += 1
        return {'mode': 'candidate_quality', 'criteria': FakeProvider._criteria('candidate_quality')}

    def compare(bundle):
        calls['compare_reference'] += 1
        return {'mode': 'reference_comparison', 'result': 'compatible', 'differences': [], 'explanation': 'matches'}

    s = make_services(overrides={'candidate_quality': blind_quality, 'reference_comparison': compare}, config_overrides=CFG)
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    pair = load_pairs(s)[0]
    task = asyncio.run(practice_task(pair, s, s.releases.get('M-EMPTY'),
                                     RunStore(s.repo, s.config.path('runs')), UsageMeter()))
    assert task.success and [a['outcome'] for a in task.attempts] == ['success']
    assert calls['candidate_quality'] == 0          # no meaning / cultural-fit / Arabic-quality audit
    assert calls['compare_reference'] == 1          # the comparison is the only quality assessment, run once
    att = [v for v in s.experiences.all() if v.kind == 'agent_attempt'][0]
    assert att.record['quality_report_id'] and att.record['comparison_report_id']

