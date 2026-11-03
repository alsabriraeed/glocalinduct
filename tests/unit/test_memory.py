"""Milestone 2 acceptance: immutable objects, reviewed insight transactions, releases."""
import sqlite3

import pytest

from glocalinduct.contracts import (AttemptExperience, InsightContent, InsightOperation, InsightReviewPayload,
                                    InsightUpdateBatchPayload, OperationReview, ReferenceExperience, ReviewCheck, Scope)
from glocalinduct.memory import ExperienceMemory, InsightMemory, ReleaseManager, Repository, TextStore
from glocalinduct.memory.insights import StaleBatch
from glocalinduct.memory.releases import ReleaseError
from glocalinduct.ingestion import extract_text, build_normalized_text


def scope(level='domain', domains=('science',), culture_specific=False):
    return Scope(level=level, domains=list(domains), genres=[], culture_specific=culture_specific,
                 locale_scope='general_arab', locales=[], setting=None, time_period=None, cross_domain_transfer=False)


def ref(pair_id, group_id, eligible=True):
    return ReferenceExperience(kind='reference_example', pair_id=pair_id, group_id=group_id, source_text_id='s',
                               source_profile_id='p', arabic_text_id='a', pair_analysis_id='pa', quality_report_id='q',
                               scope=scope(), eligible=eligible, exclusion_reasons=[] if eligible else ['failed'])


def content(lesson='Keep SI units untouched.', level='domain', domains=('science',)):
    return InsightContent(lesson=lesson, applies_when=['units appear'], avoid_when=[], recommended_strategy='retain',
                          preserve=['quantities'], scope=scope(level, domains), support_experience_ids=[],
                          counterevidence_ids=[], origin='reference')


def passing_review(op_ids):
    return InsightReviewPayload(mode='insight_review', reviews=[OperationReview(operation_id=o, checks=[
        ReviewCheck(check=c, status='pass', experience_ids=[], explanation='ok') for c in
        ['evidence_support', 'applicability', 'scope', 'preservation', 'contradiction_handling']]) for o in op_ids])


@pytest.fixture
def mem(tmp_path):
    repo = Repository(tmp_path / 'm.sqlite3')
    ex = ExperienceMemory(repo)
    ins = InsightMemory(repo, ex)
    rel = ReleaseManager(repo, ex, ins, tmp_path / 'releases')
    ids = {}
    for pid, grp, dom in [('p1', 'g1', 'science'), ('p2', 'g2', 'science'), ('p3', 'g3', 'history')]:
        ids[pid] = ex.admit_reference(ref(pid, grp), {'domains': [dom], 'source_text_hash': 'x', 'arabic_text_hash': 'y'}, 'run')
    ids['bad'] = ex.admit_reference(ref('p4', 'g4', eligible=False), {'domains': ['science']}, 'run')
    return repo, ex, ins, rel, ids


def test_objects_are_immutable_and_hashed(mem):
    repo, *_ = mem
    rid = repo.put_object('fixture', {'b': 1, 'a': 'نص'}, 'run')
    obj = repo.get_object(rid)
    assert obj['payload'] == {'a': 'نص', 'b': 1}
    with pytest.raises(sqlite3.IntegrityError):
        repo.execute('UPDATE objects SET payload_json=? WHERE record_id=?', ('{}', rid))


def test_unsupported_insight_cannot_activate_and_two_groups_can(mem):
    repo, ex, ins, rel, ids = mem
    op = InsightOperation(operation_id='o1', operation='ADD', target_insight_id=None, expected_version=None,
                          proposed_content=content(), support_experience_ids=[ids['p1']], counterevidence_ids=[],
                          justification='j')
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c1', operations=[op]), passing_review(['o1']),
                        [ids['p1']], 'run')
    assert r.committed[0]['status'] == 'provisional' and r.committed[0]['importance'] == 2
    iid = r.committed[0]['insight_id']
    # upvote with a second independent group -> active
    up = InsightOperation(operation_id='o2', operation='UPVOTE', target_insight_id=iid, expected_version=1,
                          proposed_content=None, support_experience_ids=[ids['p2']], counterevidence_ids=[], justification='j')
    r2 = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c2', operations=[up]), passing_review(['o2']),
                         [ids['p2']], 'run')
    assert r2.committed[0]['status'] == 'active' and r2.committed[0]['importance'] == 3
    assert len(ins.lineage(iid)) == 2 and ins.version(iid, 1).status == 'provisional'   # old version untouched


def test_operation_cannot_vote_twice_and_stale_version_rejected(mem):
    repo, ex, ins, rel, ids = mem
    add = InsightOperation(operation_id='o1', operation='ADD', target_insight_id=None, expected_version=None,
                           proposed_content=content(), support_experience_ids=[ids['p1']], counterevidence_ids=[], justification='j')
    iid = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c1', operations=[add]), passing_review(['o1']),
                          [ids['p1']], 'run').committed[0]['insight_id']
    # same comparison again on the same lineage -> rejected, no new version
    up = InsightOperation(operation_id='o2', operation='UPVOTE', target_insight_id=iid, expected_version=1,
                          proposed_content=None, support_experience_ids=[ids['p1']], counterevidence_ids=[], justification='j')
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c1', operations=[up]), passing_review(['o2']), [ids['p1']], 'run')
    assert r.rejected and not r.committed and ins.head(iid).version == 1
    # the identical committed batch replays without a new vote
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c1', operations=[add]), passing_review(['o1']), [ids['p1']], 'run')
    assert r.replayed and ins.head(iid).version == 1
    # stale expected version -> whole batch rejected atomically
    with pytest.raises(StaleBatch):
        ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c3', operations=[
            up.model_copy(update={'expected_version': 5})]), passing_review(['o2']), [ids['p1']], 'run')
    assert ins.head(iid).version == 1
    # duplicate ADD fingerprint rejected
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c4', operations=[add]), passing_review(['o1']), [ids['p1']], 'run')
    assert r.rejected[0]['reason'].startswith('identical lesson')


def test_review_unknown_is_not_pass_and_failed_example_is_not_support(mem):
    repo, ex, ins, rel, ids = mem
    add = InsightOperation(operation_id='o1', operation='ADD', target_insight_id=None, expected_version=None,
                           proposed_content=content(), support_experience_ids=[ids['p1']], counterevidence_ids=[], justification='j')
    rv = passing_review(['o1'])
    rv = rv.model_copy(update={'reviews': [rv.reviews[0].model_copy(update={'checks': [
        rv.reviews[0].checks[0].model_copy(update={'status': 'unknown'})] + list(rv.reviews[0].checks[1:])})]})
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c1', operations=[add]), rv, [ids['p1']], 'run')
    assert r.rejected and 'unknown' in r.rejected[0]['reason']
    bad = add.model_copy(update={'support_experience_ids': [ids['bad']]})
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c2', operations=[bad]), passing_review(['o1']), [ids['bad']], 'run')
    assert 'not an admitted' in r.rejected[0]['reason']


def test_edit_does_not_inherit_unrelated_support_and_downvote_retires(mem):
    repo, ex, ins, rel, ids = mem
    add = InsightOperation(operation_id='o1', operation='ADD', target_insight_id=None, expected_version=None,
                           proposed_content=content(), support_experience_ids=[ids['p1'], ids['p2']], counterevidence_ids=[], justification='j')
    iid = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c1', operations=[add]), passing_review(['o1']),
                          [ids['p1'], ids['p2']], 'run').committed[0]['insight_id']
    assert ins.head(iid).status == 'active'
    edit = InsightOperation(operation_id='o2', operation='EDIT', target_insight_id=iid, expected_version=1,
                            proposed_content=content('Keep SI units untouched; do not localise currency.'),
                            support_experience_ids=[ids['p1']], counterevidence_ids=[], justification='narrowed')
    r = ins.apply_batch(InsightUpdateBatchPayload(comparison_id='c2', operations=[edit]), passing_review(['o2']), [ids['p1']], 'run')
    head = ins.head(iid)
    assert head.version == 2 and head.importance == 3 and head.status == 'provisional'   # only one group now
    assert head.content.support_experience_ids == [ids['p1']]
    for n, cid in enumerate(['c5', 'c6', 'c7'], 1):
        down = InsightOperation(operation_id='d', operation='DOWNVOTE', target_insight_id=iid, expected_version=head.version,
                                proposed_content=None, support_experience_ids=[], counterevidence_ids=[ids['p3']], justification='j')
        ins.apply_batch(InsightUpdateBatchPayload(comparison_id=cid, operations=[down]), passing_review(['d']), [ids['p3']], 'run')
        head = ins.head(iid)
    assert head.importance == 0 and head.status == 'retired'


def test_release_is_immutable_and_rejects_failed_examples(mem):
    repo, ex, ins, rel, ids = mem
    index = {'embedder': 'fake', 'experiences': [{'experience_id': ids['p1'], 'vector': [1.0]}], 'insights': []}
    r0 = rel.publish('M0', [ids['p1'], ids['p2']], [], index, {'split': 'h'}, 'run')
    assert rel.active().release_id == 'M0' and r0.demonstration_ids == sorted([ids['p1'], ids['p2']])
    with pytest.raises(ReleaseError):
        rel.publish('M1', [ids['bad']], [], {'experiences': [], 'insights': []}, {}, 'run')
    with pytest.raises(ReleaseError):
        rel.publish('M0', [ids['p1']], [], {'experiences': [], 'insights': []}, {}, 'run')
    # staging changes after publication do not alter the published release
    ex.admit_reference(ref('p9', 'g9'), {'domains': ['arts']}, 'run2')
    assert rel.get('M0').release_hash == r0.release_hash and len(rel.get('M0').demonstration_ids) == 2
    with pytest.raises(sqlite3.IntegrityError):
        repo.execute('DELETE FROM releases WHERE release_id=?', ('M0',))


def test_attempt_records_and_text_store(mem, tmp_path):
    repo, ex, ins, rel, ids = mem
    att = AttemptExperience(kind='agent_attempt', task_id='t1', group_id='g7', phase='practice', attempt_number=1,
                            previous_attempt_id=None, source_profile_id='sp', release_id='M0', retrieval_id='r',
                            plan_id='pl', candidate_id='c', quality_report_id='q', comparison_report_id='cmp',
                            feedback_received=None, outcome='success', eligible=True, issue_codes=[])
    aid = ex.admit_attempt(att, {'domains': ['science']}, 'run', supports=[ids['p1']])
    assert ex.supports_of(aid) == [ids['p1']]
    with pytest.raises(sqlite3.IntegrityError):      # support must reference an existing experience
        ex.admit_attempt(att.model_copy(update={'task_id': 't2'}), {}, 'run', supports=['missing'])
    fail = att.model_copy(update={'outcome': 'failure', 'eligible': False, 'task_id': 't3'})
    ex.admit_attempt(fail, {'domains': ['science']}, 'run')
    assert len(ex.eligible_ids()) == 4
    store = TextStore(tmp_path / 'texts')
    nt = build_normalized_text(extract_text('مرحبا بالعالم'))
    h = store.put(nt)
    assert store.get(h).text == 'مرحبا بالعالم'
