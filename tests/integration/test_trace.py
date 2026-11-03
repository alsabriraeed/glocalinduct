"""Step Inspector trace: events cover every agent/step, never change results, and the readers stay in the traces root."""
import asyncio
from pathlib import Path

import pytest

from glocalinduct import trace
from glocalinduct.agents.runner import UsageMeter
from glocalinduct.providers.fake import FakeProvider
from glocalinduct.workflows.consolidate import update_insights
from glocalinduct.workflows.evaluate import evaluate
from glocalinduct.workflows.ingest import ingest_pairs, split_groups, load_pairs
from glocalinduct.workflows.practice import collect_practice, practice_task
from glocalinduct.workflows.seed import build_seed_memory
from glocalinduct.workflows.common import RunStore

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures' / 'corpus'


@pytest.fixture
def traces(tmp_path):
    trace.configure(tmp_path / 'traces', 'ses_test', 'collect-practice')
    yield tmp_path / 'traces'
    trace._sink['dir'] = None
    trace._sink['command'] = None


def _events(root, session='ses_test'):
    out, start = [], 0
    while True:
        page = trace.read_events(root, session, start, trace.MAX_PAGE)
        out += page['events']
        if not page['events']:
            return out
        start = page['next']


def test_practice_retry_trace_shows_every_agent_and_the_new_prompt(make_services, traces):
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
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    pair = load_pairs(s)[0]
    task = asyncio.run(practice_task(pair, s, s.releases.get('M-EMPTY'), RunStore(s.repo, s.config.path('runs')), UsageMeter()))
    assert task.success and len(task.attempts) == 2

    evs = _events(traces)
    kinds = [e['kind'] for e in evs]
    for k in ('pair_start', 'attempt_start', 'source', 'retrieval', 'plan_frozen', 'candidate', 'evaluation', 'decision',
              'reference_comparison', 'feedback', 'experience', 'attempt_end', 'pair_end'):
        assert k in kinds, k
    modes = {e['data']['mode'] for e in evs if e['kind'] == 'llm_call'}
    assert {'analyse_source', 'make_plan', 'generate_arabic', 'reference_comparison'} <= modes
    assert 'candidate_quality' not in modes            # reference-only Quality evaluator: no criteria audit
    assert all(e['pair_id'] == pair.pair_id for e in evs if e['kind'] != 'command_start')
    # attempt 2's planner call carries the reflexion feedback produced by attempt 1
    fb_event = next(e for e in evs if e['kind'] == 'feedback')
    assert fb_event['attempt'] == 1 and fb_event['data']['for_attempt'] == 2
    plans = [e for e in evs if e['kind'] == 'llm_call' and e['data']['mode'] == 'make_plan']
    assert 'feedback' not in plans[0]['data']['input'] and plans[1]['data']['input']['feedback']
    assert plans[1]['attempt'] == 2
    # the stored experience record is visible, and prompts are retrievable by hash
    exp = [e for e in evs if e['kind'] == 'experience']
    assert [x['data']['outcome'] for x in exp] == ['failure', 'success'] and exp[1]['data']['eligible']
    h = plans[0]['data']['system_prompt_hash']
    assert 'GlocalInduct' in trace.read_prompt(traces, 'ses_test', h)


def test_full_pipeline_emits_step_events_and_results_are_unchanged(make_services, traces):
    def run(tracing):
        if not tracing:
            trace._sink['dir'] = None
        s = make_services()
        ingest_pairs(s, CORPUS); split_groups(s)
        seed = asyncio.run(build_seed_memory(s, 'M0'))
        practice = asyncio.run(collect_practice(s, 'M0'))
        cons = asyncio.run(update_insights(s, 'M0', 'M1'))
        rep = asyncio.run(evaluate(s, 'test', 'M1'))
        return seed['admitted'], practice['successes'], cons['release']['demonstrations'], rep['n']

    traced = run(True)
    evs = _events(traces)
    kinds = {e['kind'] for e in evs}
    assert {'admission', 'packets_built', 'packet', 'insight_batch', 'release_published', 'eval_row'} <= kinds
    assert any(e['phase'] == 'insights' and e['comparison_id'] for e in evs if e['kind'] == 'packet')
    assert traced == run(False)                     # tracing never changes the outcome
    # sequence numbers are strictly increasing within one process
    seqs = [e['seq'] for e in evs]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def test_tracing_off_writes_nothing(make_services, tmp_path):
    assert not trace.enabled()
    trace.emit('x', a=1)
    assert trace.store_prompt('hello') is None
    assert not (tmp_path / 'traces').exists()


def test_readers_reject_bad_identifiers_and_path_traversal(tmp_path):
    root = tmp_path / 'traces'
    trace.configure(root, 'ses_ok', 'x')
    try:
        trace.emit('probe', v=1)
        digest = trace.store_prompt('secret prompt')
    finally:
        trace._sink['dir'] = None
    (tmp_path / 'outside.txt').write_text('private', encoding='utf-8')
    assert trace.read_events(root, 'ses_ok', 0, 10)['events'][0]['kind'] == 'probe'
    assert trace.read_prompt(root, 'ses_ok', digest) == 'secret prompt'
    for bad in ('..', '../x', 'a/b', '', 'x' * 65, 'ses ok', 'ses_ok/../..'):
        assert trace.read_events(root, bad) is None
        assert trace.read_prompt(root, bad, digest) is None
    for bad_hash in ('../outside', digest[:-1], digest.upper(), '../../outside.txt', ''):
        assert trace.read_prompt(root, 'ses_ok', bad_hash) is None
    with pytest.raises(ValueError):
        trace.configure(root, '../escape', 'x')
    assert [s['session'] for s in trace.list_sessions(root)] == ['ses_ok']


def test_paging_and_half_written_line(tmp_path):
    root = tmp_path / 'traces'
    trace.configure(root, 'ses_p', 'x')
    try:
        for i in range(5):
            trace.emit('e', i=i)
        f = root / 'ses_p' / 'trace.jsonl'
        with f.open('a', encoding='utf-8') as fh:
            fh.write('{"kind": "partial"')              # writer is mid-line
    finally:
        trace._sink['dir'] = None
    page = trace.read_events(root, 'ses_p', 0, 3)
    assert [e['data']['i'] for e in page['events']] == [0, 1, 2] and page['next'] == 3
    rest = trace.read_events(root, 'ses_p', page['next'], 10)
    assert [e['data']['i'] for e in rest['events']] == [3, 4] and rest['next'] == 5
