"""Workbench inference with `lookup_reference`: the Quality evaluator is the reference comparison only."""
import asyncio
from pathlib import Path

from glocalinduct.workflows import InferRequest, infer
from glocalinduct.workflows.ingest import ingest_pairs, split_groups, load_pairs

CORPUS = Path(__file__).resolve().parents[1] / 'fixtures' / 'corpus'


def test_lookup_reference_matches_ingested_pair_and_compares_only(make_services):
    s = make_services()
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    p = load_pairs(s)[0]
    out = asyncio.run(infer(InferRequest(text=p.source.normalized.text, context={'locale_scope': 'general_arab'}),
                            s, 'M-EMPTY', lookup_reference=True))
    rep = out.attempt.report
    assert rep is not None and rep.payload.criteria == [] and rep.payload.reference_comparison is not None
    modes = {c['mode'] for c in s.provider.calls}
    assert 'reference_comparison' in modes and 'candidate_quality' not in modes


def test_lookup_reference_without_match_skips_evaluation(make_services):
    s = make_services()
    ingest_pairs(s, CORPUS); split_groups(s); s.ensure_empty_release()
    out = asyncio.run(infer(InferRequest(text='A sentence that is in no ingested pair.', context={}),
                            s, 'M-EMPTY', lookup_reference=True))
    assert out.attempt.report is None and out.result.status in ('accepted', 'conservative')
    assert out.result.evaluation_ids == []
    modes = {c['mode'] for c in s.provider.calls}
    assert not modes & {'reference_comparison', 'candidate_quality'}
