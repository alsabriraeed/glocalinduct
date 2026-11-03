"""Engineering contract checks; these make no model-quality claims."""
from pathlib import Path
import copy
import json
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import yaml
from pydantic import ValidationError
from glocalinduct.contracts import (QuoteRef, ResolvedSpan, Decision, QualityEvaluationPayload,
                       ReferenceComparisonPayload, AttemptExperience,
                       InsightUpdateBatchPayload, SourceProfilePayload, RunResult)


def criterion(name, status='pass', score=3):
    return {'criterion': name, 'status': status, 'score': score,
            'evidence': [], 'findings': [], 'explanation': 'Contract fixture'}


class ContractChecks(unittest.TestCase):
    def test_exported_schema_references_resolve(self):
        schemas = sorted((ROOT / 'schemas').glob('*.json'))
        self.assertEqual(len(schemas), 15)
        for path in schemas:
            schema=json.loads(path.read_text())
            self.assertEqual(schema['$schema'],'https://json-schema.org/draft/2020-12/schema')
            def check(node):
                if isinstance(node,dict):
                    if '$ref' in node:
                        self.assertTrue(node['$ref'].startswith('#/'))
                        target=schema
                        for key in node['$ref'][2:].split('/'):
                            target=target[key.replace('~1','/').replace('~0','~')]
                    for child in node.values():
                        check(child)
                elif isinstance(node,list):
                    for child in node:
                        check(child)
            check(schema)

    def test_arabic_offsets_are_codepoints(self):
        text = 'نص عَرَبِيّ'
        span = ResolvedSpan(text_hash='fixture',start=3,end=len(text),quote=text[3:])
        self.assertEqual(text[span.start:span.end],span.quote)
        with self.assertRaises(ValidationError):
            ResolvedSpan(text_hash='fixture',start=3,end=len(text.encode()),quote=text[3:])

    def test_subsequence_quote_resolution(self):
        from glocalinduct.validation.spans import TextRegistry, resolve_quote
        from glocalinduct.records import build_normalized_text, Block
        reg = TextRegistry()
        nt = build_normalized_text([Block(block_id='b0001', order=0, kind='paragraph',
                                          text='The Corgi dogs are beloved by the royal family and British public.')])
        reg.add('source', nt)
        ref = QuoteRef(text_key='source', block_id='b0001', quote='Corgi dogs are beloved by the British public', occurrence=0)
        span = resolve_quote(reg, ref, strict=True)
        self.assertIn('Corgi dogs', span.quote)
        self.assertIn('British public', span.quote)

    def test_quote_has_no_invented_extra_field(self):
        with self.assertRaises(ValidationError):
            QuoteRef(text_key='source',block_id='b1',quote='text',occurrence=0,accepted=True)

    def test_substitution_support_is_enforced_by_plan_validation_not_the_schema(self):
        payload = dict(decision_id='d1',item_id='i1',strategy='substitute',
            counterpart_ar='مصطلح',canonical_concept='fixture concept',term_policy='inflectable',
            support=[],insight_refs=[],preserve_requirement_ids=[],dependent_edits=[],
            fallback_reason=None,rationale='Contract fixture')
        # practice retries may ground a substitution in the glocalized-pair reflection; inference rejects it
        # in validation.plans (UNSUPPORTED_SUBSTITUTION), see tests/integration/test_infer.py.
        self.assertEqual(Decision.model_validate(payload).strategy, 'substitute')

    def test_decision_strategy_inputs_are_sanitized(self):
        # substitute with fallback_reason and no support/insight is routed to safe retention fallback
        sub_payload = dict(decision_id='d1', item_id='i1', strategy='substitute',
                           counterpart_ar='مصطلح', canonical_concept='fixture concept', term_policy='inflectable',
                           support=[], insight_refs=[], preserve_requirement_ids=[], dependent_edits=[],
                           fallback_reason='incidental item fallback', rationale='Contract fixture')
        d_sub = Decision.model_validate(sub_payload)
        self.assertEqual(d_sub.strategy, 'retain')
        self.assertEqual(d_sub.fallback_reason, 'incidental item fallback')
        self.assertIsNone(d_sub.counterpart_ar)

        # retain with counterpart is sanitized to None
        ret_payload = dict(decision_id='d2', item_id='i2', strategy='retain',
                           counterpart_ar='مصطلح', canonical_concept='concept', term_policy='inflectable',
                           support=[], insight_refs=[], preserve_requirement_ids=[], dependent_edits=[],
                           fallback_reason='retained safely', rationale='Contract fixture')
        d_ret = Decision.model_validate(ret_payload)
        self.assertEqual(d_ret.strategy, 'retain')
        self.assertIsNone(d_ret.counterpart_ar)
        self.assertIsNone(d_ret.canonical_concept)

    def test_quality_applicability_is_mode_defined(self):
        cs=[criterion(n) for n in ['meaning_and_purpose','cultural_fit','arabic_quality']]
        QualityEvaluationPayload(mode='candidate_quality',criteria=cs)
        QualityEvaluationPayload(mode='reference_quality',criteria=cs)
        cs_bad = [criterion('meaning_and_purpose','not_applicable',None), criterion('cultural_fit'), criterion('arabic_quality')]
        with self.assertRaises(ValidationError):
            QualityEvaluationPayload(mode='candidate_quality',criteria=cs_bad)

    def test_unknown_is_not_scored_as_pass(self):
        cs=[criterion(n) for n in ['meaning_and_purpose','cultural_fit','arabic_quality']]
        cs[0]=criterion('meaning_and_purpose','unknown',3)
        with self.assertRaises(ValidationError):
            QualityEvaluationPayload(mode='candidate_quality',criteria=cs)

    def test_compatible_cannot_hide_candidate_defect(self):
        diff=dict(classification='candidate_defect',source_quotes=[],candidate_quotes=[],
                  reference_quotes=[],explanation='Fixture defect',correction_goal='Fix',critical=False)
        with self.assertRaises(ValidationError):
            ReferenceComparisonPayload(mode='reference_comparison',result='compatible',
                                       differences=[diff],explanation='Fixture')

    def test_quality_payload_with_reference_comparison(self):
        cs = [criterion(n) for n in ['meaning_and_purpose','cultural_fit','arabic_quality']]
        ref_cmp = dict(result='compatible', differences=[], explanation='Fixture compatible')
        p = QualityEvaluationPayload(mode='candidate_quality', criteria=cs, reference_comparison=ref_cmp)
        self.assertIsNotNone(p.reference_comparison)
        self.assertEqual(p.reference_comparison.result, 'compatible')

    def test_reference_only_payload_needs_a_comparison_and_candidate_mode(self):
        ref_cmp = dict(result='compatible', differences=[], explanation='Fixture compatible')
        p = QualityEvaluationPayload(mode='candidate_quality', criteria=[], reference_comparison=ref_cmp)
        self.assertEqual(p.criteria, [])
        with self.assertRaises(ValidationError):
            QualityEvaluationPayload(mode='candidate_quality', criteria=[])                       # nothing to assess
        with self.assertRaises(ValidationError):
            QualityEvaluationPayload(mode='reference_quality', criteria=[], reference_comparison=ref_cmp)
        with self.assertRaises(ValidationError):
            QualityEvaluationPayload(mode='candidate_quality', criteria=[criterion('cultural_fit')],
                                     reference_comparison=ref_cmp)                               # partial criteria

    def test_reference_only_decision_is_decided_by_the_comparison(self):
        from glocalinduct.validation.evaluation import decide_result
        defect = dict(classification='candidate_defect', explanation='kept a foreign gesture', correction_goal='adapt it',
                      critical=True)
        def report(result, diffs=()):
            return QualityEvaluationPayload(mode='candidate_quality', criteria=[], reference_comparison=dict(
                result=result, differences=list(diffs), explanation='Fixture'))
        self.assertEqual(decide_result(report('compatible'), []).status, 'accepted')
        bad = decide_result(report('incompatible', [defect]), [])
        self.assertEqual((bad.status, bad.issue_codes), ('unresolved', ['CULTURAL_FIT_FAILED']))
        self.assertEqual(bad.findings[0]['criterion'], 'reference_comparison')
        unk = decide_result(report('unknown'), [])
        self.assertEqual((unk.status, unk.issue_codes), ('unresolved', ['QUALITY_UNKNOWN']))

    def test_deployment_attempt_cannot_enter_demonstrations(self):
        with self.assertRaises(ValidationError):
            AttemptExperience(kind='agent_attempt',task_id='t1',group_id='g1',phase='deployment',
                attempt_number=1,previous_attempt_id=None,source_profile_id='s1',release_id='M0',
                retrieval_id='r1',plan_id='p1',candidate_id='c1',quality_report_id='q1',
                comparison_report_id=None,feedback_received=None,outcome='success',
                eligible=True,issue_codes=[])

    def test_duplicate_target_operations_are_rejected(self):
        op=dict(operation_id='o1',operation='UPVOTE',target_insight_id='i1',expected_version=1,
                proposed_content=None,support_experience_ids=['e1'],counterevidence_ids=[],
                justification='Fixture')
        other=dict(op,operation_id='o2')
        with self.assertRaises(ValidationError):
            InsightUpdateBatchPayload(comparison_id='c1',operations=[op,other])

    def test_source_profile_rejects_unknown_dependencies(self):
        q=dict(text_key='source',block_id='b1',quote='text',occurrence=0)
        item=dict(item_id='i1',category='fixture',source_quotes=[q],semantic_function='fixture',
                  essentiality='incidental',factual_role='fixture',requirement_ids=[],depends_on_item_ids=['missing'])
        with self.assertRaises(ValidationError):
            SourceProfilePayload(purpose='fixture',purpose_basis='inferred',
                domains=[dict(domain='general',basis='inferred',evidence=[])],genre=None,audience=None,
                requirements=[],items=[item],covered_block_ids=['b1'],uncertainties=[])

    def test_unresolved_exposes_no_draft(self):
        base=dict(request_id='r1',release_id='M0',status='unresolved',arabic_text=None,
                  plan_ids=[],candidate_ids=[],evaluation_ids=[],fallback_reasons=[],
                  issue_codes=['BUDGET_EXHAUSTED'],usage=dict(model_calls=0,input_tokens=0,
                    output_tokens=0,elapsed_seconds=0.0,cost=None,currency=None))
        RunResult.model_validate(base)
        with self.assertRaises(ValidationError):
            RunResult.model_validate(dict(base,arabic_text='مسودة'))

    def test_config_ratios_and_domain_policies(self):
        cfg=yaml.safe_load((ROOT/'configs/default.yaml').read_text())
        self.assertAlmostEqual(sum(cfg['split'][k] for k in ['learning','development','test']),1)
        self.assertEqual(cfg['attempts'],{'practice':3,'deployment':1})
        policy=yaml.safe_load((ROOT/'configs/domain_policies.yaml').read_text())
        self.assertEqual(policy['shared']['strategies'],['retain','substitute'])
        self.assertIn('history',policy['domains'])
        self.assertIn('science',policy['domains'])


class StorageChecks(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript((ROOT/'storage/schema.sql').read_text())

    def tearDown(self):
        self.db.close()

    def obj(self, rid, kind='fixture'):
        self.db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
            (rid,kind,'1.0','run1','2026-10-25T00:00:00Z','fixture:'+rid,'{}'))

    def test_objects_are_immutable(self):
        self.obj('r1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE objects SET payload_json=? WHERE record_id=?',('{"x":1}','r1'))

    def test_foreign_keys_are_enabled(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO object_parents VALUES(?,?)',('missing','absent'))

    def test_comparison_cannot_vote_again_on_new_version(self):
        for rid in ['iv1','iv2','proposal','review']:
            self.obj(rid)
        for version in (1,2):
            self.db.execute('INSERT INTO insight_versions VALUES(?,?,?,?,?)',
                            ('i1',version,'iv'+str(version),version+1,'active'))
        sql='INSERT INTO insight_events VALUES(?,?,?,?,?,?,?,?,?)'
        self.db.execute(sql,('e1','comparison','i1','ADD',None,1,'proposal','review','fingerprint'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql,('e2','comparison','i1','UPVOTE',1,2,'proposal','review',None))

    def test_failed_attempt_cannot_be_eligible(self):
        self.obj('attempt')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO experiences VALUES(?,?,?,?,?,?,?)',
                ('attempt','agent_attempt','g1','t1','practice','failure',1))


if __name__ == '__main__':
    unittest.main()
