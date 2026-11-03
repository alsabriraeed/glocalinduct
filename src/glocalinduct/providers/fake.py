"""Fake provider (guide §3 providers/fake.py).

Produces schema-valid payloads deterministically from the input bundle so the
whole pipeline can run offline in tests and demos. It makes NO claim about
glocalization quality: its "Arabic" is a labelled placeholder. Tests script
failures through `overrides`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .base import Provider, ProviderResponse

# A tiny lexicon used only by the fake to detect "cultural items" and propose counterparts.
FAKE_LEXICON: dict[str, dict[str, str]] = {
    'thanksgiving': {'category': 'holiday', 'function': 'seasonal family gathering', 'counterpart': 'عيد الفطر'},
    'christmas': {'category': 'holiday', 'function': 'seasonal celebration', 'counterpart': 'عيد الأضحى'},
    'super bowl': {'category': 'sport event', 'function': 'major televised sports event', 'counterpart': 'نهائي كأس آسيا'},
    'baseball': {'category': 'sport', 'function': 'popular team sport', 'counterpart': 'كرة القدم'},
    'pumpkin pie': {'category': 'food', 'function': 'festive dessert', 'counterpart': 'الكنافة'},
    'dollars': {'category': 'currency', 'function': 'unit of price', 'counterpart': 'ريالات'},
    'fahrenheit': {'category': 'measurement', 'function': 'temperature unit', 'counterpart': 'درجة مئوية'},
    'hot dog': {'category': 'food', 'function': 'casual street food', 'counterpart': 'شاورما'},
    'pancakes': {'category': 'food', 'function': 'breakfast dish', 'counterpart': 'الفطائر'},
    'main street': {'category': 'place', 'function': 'central commercial street', 'counterpart': 'السوق'},
}
ESSENTIAL_HINTS = ('history of', 'founded', 'treaty', 'president', 'theorem', 'law of')


class FakeProvider(Provider):
    name = 'fake'

    def __init__(self, overrides: dict[str, Callable[[dict], dict] | dict] | None = None,
                 fail_first: dict[str, int] | None = None):
        super().__init__(model='fake-model-1')
        self.overrides = overrides or {}
        self.fail_first = dict(fail_first or {})    # mode -> number of transient failures to inject
        self.calls: list[dict] = []

    def model_identity(self) -> dict[str, Any]:
        return {'provider': 'fake', 'model': self.model, 'exact_revision': True}

    async def generate(self, system: str, user: str, schema: dict[str, Any], max_output_tokens: int,
                       schema_name: str = 'output') -> ProviderResponse:
        bundle = json.loads(user)
        mode = bundle.get('mode', '')
        self.calls.append({'mode': mode, 'bundle': bundle})
        if self.fail_first.get(mode, 0) > 0:
            self.fail_first[mode] -= 1
            from .base import ProviderTransientError
            raise ProviderTransientError('injected transient failure')
        ov = self.overrides.get(mode)
        if ov is not None:
            payload = ov(bundle) if callable(ov) else ov
        else:
            payload = getattr(self, f'_{mode}')(bundle)
        text = json.dumps(payload, ensure_ascii=False)
        return ProviderResponse(text=text, input_tokens=len(user) // 4, output_tokens=len(text) // 4,
                                model=self.model, request_id='fake')

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _blocks(bundle: dict) -> list[dict]:
        return bundle['texts']['source']['blocks']

    @staticmethod
    def _owned(bundle: dict) -> list[dict]:
        owned = set(bundle.get('owned_block_ids') or [b['block_id'] for b in bundle['texts']['source']['blocks']])
        return [b for b in bundle['texts']['source']['blocks'] if b['block_id'] in owned]

    @staticmethod
    def _quote(block: dict, quote: str, key: str = 'source', occurrence: int = 0) -> dict:
        return {'text_key': key, 'block_id': block['block_id'], 'quote': quote, 'occurrence': occurrence}

    @staticmethod
    def _first_words(text: str, n: int = 6) -> str:
        words = text.split()
        return ' '.join(words[:n]) if words else text

    def _detect_items(self, blocks: list[dict]) -> list[dict]:
        items = []
        n = 0
        for b in blocks:
            low = b['text'].lower()
            for term, info in FAKE_LEXICON.items():
                idx = low.find(term)
                if idx < 0:
                    continue
                n += 1
                surface = b['text'][idx: idx + len(term)]
                essential = b['kind'] == 'heading' or any(h in low for h in ESSENTIAL_HINTS)
                items.append({'item_id': f'I{n}', 'category': info['category'],
                              'source_quotes': [self._quote(b, surface)], 'semantic_function': info['function'],
                              'essentiality': 'essential' if essential else 'incidental',
                              'factual_role': 'named subject' if essential else 'incidental illustration',
                              'requirement_ids': [], 'depends_on_item_ids': [], '_term': term, '_surface': surface,
                              '_block': b['block_id']})
        return items

    # ----------------------------------------------------------- A3 analyse
    def _analyse_source(self, bundle: dict) -> dict:
        owned = self._owned(bundle)
        meta = bundle.get('metadata', {})
        supplied = meta.get('provenance', {})
        ctx = meta.get('context', {})
        domains = [{'domain': d, 'basis': 'supplied' if supplied.get('domains') == 'supplied' else 'inferred',
                    'evidence': []} for d in (ctx.get('domains') or [])] or \
                  [{'domain': 'general', 'basis': 'inferred', 'evidence': []}]
        reqs = []
        for i, b in enumerate(owned, 1):
            reqs.append({'requirement_id': f'R{i}', 'kind': 'proposition' if b['kind'] != 'heading' else 'structure',
                         'description': f'Preserve the content of block {b["block_id"]}',
                         'source_quotes': [self._quote(b, self._first_words(b['text']))], 'metadata_field': None,
                         'protected': b['kind'] in ('code', 'quote')})
        if ctx.get('objectives'):
            reqs.append({'requirement_id': f'R{len(reqs) + 1}', 'kind': 'objective',
                         'description': 'Supplied objectives: ' + '; '.join(ctx['objectives']), 'source_quotes': [],
                         'metadata_field': 'objectives', 'protected': True})
        items = [{k: v for k, v in it.items() if not k.startswith('_')} for it in self._detect_items(owned)]
        return {'purpose': 'Inferred purpose: convey the content of the source to Arabic readers',
                'purpose_basis': 'inferred', 'domains': domains, 'genre': ctx.get('genre'),
                'audience': ctx.get('audience'), 'requirements': reqs, 'items': items,
                'covered_block_ids': [b['block_id'] for b in owned], 'uncertainties': []}

    # -------------------------------------------------------------- A3 plan
    def _make_plan(self, bundle: dict) -> dict:
        profile = bundle['profile']
        exps = bundle.get('retrieval', {}).get('experiences', [])
        insights = bundle.get('retrieval', {}).get('insights', [])
        decisions, terminology = [], []
        for n, item in enumerate(profile['items'], 1):
            surface = item['source_quotes'][0]['quote']
            term = surface.lower()
            info = FAKE_LEXICON.get(term)
            support = None
            if item['essentiality'] == 'incidental' and info:
                for e in exps:
                    for b in e['arabic_text']['blocks']:
                        if info['counterpart'] in b['text']:
                            support = (e, b); break
                    if support:
                        break
            if support:
                e, b = support
                decisions.append({'decision_id': f'D{n}', 'item_id': item['item_id'], 'strategy': 'substitute',
                                  'counterpart_ar': info['counterpart'], 'canonical_concept': info['function'],
                                  'term_policy': 'inflectable',
                                  'support': [{'experience_id': e['experience_id'],
                                               'arabic_quote': {'text_key': f'experience:{e["experience_id"]}',
                                                                'block_id': b['block_id'],
                                                                'quote': info['counterpart'], 'occurrence': 0},
                                               'compatible_function': info['function']}],
                                  'insight_refs': [{'insight_id': i['insight_id'], 'version': i['version']} for i in insights[:1]],
                                  'preserve_requirement_ids': [], 'dependent_edits': [], 'fallback_reason': None,
                                  'rationale': 'Incidental item with compatible accepted example'})
            else:
                fallback = None
                if item['essentiality'] == 'uncertain':
                    fallback = 'uncertain essentiality; retained safely'
                elif item['essentiality'] == 'incidental':
                    fallback = 'no compatible accepted counterpart in memory; retained safely'
                decisions.append({'decision_id': f'D{n}', 'item_id': item['item_id'], 'strategy': 'retain',
                                  'counterpart_ar': None, 'canonical_concept': None, 'term_policy': 'inflectable',
                                  'support': [], 'insight_refs': [], 'preserve_requirement_ids': [],
                                  'dependent_edits': [], 'fallback_reason': fallback,
                                  'rationale': 'Retained: essential or unsupported'})
            terminology.append({'source_concept': surface, 'preferred_ar': decisions[-1]['counterpart_ar'] or surface,
                                'item_ids': [item['item_id']], 'policy': 'inflectable'})
        reflection = None
        if bundle.get('feedback'):
            reflection = 'Earlier defect: ' + '; '.join(f['issue_code'] for fb in bundle['feedback'] for f in fb['findings']) \
                         + '. Permitted correction applied under the same strategy.'
        return {'decisions': decisions, 'terminology': terminology, 'reflection': reflection, 'unresolved_issues': []}

    # --------------------------------------------------------- A4 generate
    def _generate_arabic(self, bundle: dict) -> dict:
        owned = self._owned(bundle)
        decisions = {d['item_id']: d for d in bundle['plan']['decisions']}
        items = {i['item_id']: i for i in bundle['profile']['items']}
        blocks, realizations = [], []
        for b in owned:
            text = b['text']
            for iid, it in items.items():
                q = it['source_quotes'][0]
                if q['block_id'] != b['block_id']:
                    continue
                d = decisions.get(iid)
                if d and d['strategy'] == 'substitute':
                    text = text.replace(q['quote'], d['counterpart_ar'])
            ar = f'[ترجمة تجريبية] {text}'
            if bundle.get('feedback'):
                ar += f' (مراجعة {len(bundle["feedback"])})'      # a repair must change the wording
            blocks.append({'source_block_id': b['block_id'], 'text_ar': ar})
            for iid, it in items.items():
                q = it['source_quotes'][0]
                if q['block_id'] != b['block_id']:
                    continue
                d = decisions.get(iid)
                surface = d['counterpart_ar'] if d and d['strategy'] == 'substitute' else q['quote']
                realizations.append({'item_id': iid, 'output_quotes': [
                    {'text_key': f'candidate:{b["block_id"]}', 'block_id': b['block_id'], 'quote': surface, 'occurrence': 0}]})
        return {'blocks': blocks, 'realizations': realizations, 'unresolved_issues': []}

    # ------------------------------------------------------------ A5 modes
    @staticmethod
    def _criteria(mode: str, score: int = 3) -> list[dict]:
        out = []
        for c in ['meaning_and_purpose', 'cultural_fit', 'arabic_quality']:
            out.append({'criterion': c, 'status': 'pass', 'score': score, 'evidence': [], 'findings': [],
                        'explanation': 'Fake evaluator: structural pass (no semantic claim)'})
        return out

    def _candidate_quality(self, bundle: dict) -> dict:
        return {'mode': 'candidate_quality', 'criteria': self._criteria('candidate_quality')}

    def _reference_quality(self, bundle: dict) -> dict:
        return {'mode': 'reference_quality', 'criteria': self._criteria('reference_quality')}

    def _reference_comparison(self, bundle: dict) -> dict:
        return {'mode': 'reference_comparison', 'result': 'compatible', 'differences': [],
                'explanation': 'Fake comparison: compatible (no semantic claim)'}

    def _reflect_failure(self, bundle: dict) -> dict:
        sig = bundle.get('signals', {})
        codes = ', '.join(sig.get('issue_codes') or []) or 'none'
        n = len(bundle.get('earlier_reflections') or [])
        return {'mode': 'reflect_failure',
                'reflection': f"Fake reflection {n + 1}: outcome {sig.get('outcome', 'failure')}, issues {codes}. "
                              'Retain the affected item and keep the wording faithful to the source.'}

    def _insight_review(self, bundle: dict) -> dict:
        return {'mode': 'insight_review', 'reviews': [
            {'operation_id': op['operation_id'], 'checks': [
                {'check': c, 'status': 'pass', 'experience_ids': op.get('support_experience_ids', []),
                 'explanation': 'Fake review pass'} for c in
                ['evidence_support', 'applicability', 'scope', 'preservation', 'contradiction_handling']]}
            for op in bundle['proposal']['operations']]}

    # ---------------------------------------------------------- A1 analyse
    def _analyse_pair(self, bundle: dict) -> dict:
        src = bundle['texts']['source']['blocks']
        ref = bundle['texts']['reference']['blocks']
        profile = bundle['profile']
        ctx = bundle.get('metadata', {}).get('context', {})
        aligns = []
        item_block = {}
        for it in profile['items']:
            item_block.setdefault(it['source_quotes'][0]['block_id'], []).append(it['item_id'])
        for i, s in enumerate(src):
            t = ref[i] if i < len(ref) else None
            if t is None:
                aligns.append({'alignment_id': f'A{i + 1}', 'item_ids': item_block.get(s['block_id'], []),
                               'source_quotes': [self._quote(s, self._first_words(s['text']))], 'target_quotes': [],
                               'observation': 'omission', 'preserved_requirement_ids': [], 'violated_requirement_ids': [],
                               'uncertainty': None, 'note': 'No corresponding reference block'})
                continue
            ids = item_block.get(s['block_id'], [])
            aligns.append({'alignment_id': f'A{i + 1}', 'item_ids': ids,
                           'source_quotes': [self._quote(s, self._first_words(s['text']))],
                           'target_quotes': [self._quote(t, self._first_words(t['text'], 4), 'reference')],
                           'observation': 'retain' if ids else 'ordinary_translation',
                           'preserved_requirement_ids': [r['requirement_id'] for r in profile['requirements']
                                                         if r['source_quotes'] and r['source_quotes'][0]['block_id'] == s['block_id']],
                           'violated_requirement_ids': [], 'uncertainty': None, 'note': 'Block-level correspondence'})
        domains = [d['domain'] for d in profile['domains'] if d['domain'] != 'general']
        scope = {'level': 'domain' if domains else 'context', 'domains': domains, 'genres': [],
                 'culture_specific': False, 'locale_scope': ctx.get('locale_scope', 'unspecified'),
                 'locales': ctx.get('locales', []), 'setting': ctx.get('setting'), 'time_period': ctx.get('time_period'),
                 'cross_domain_transfer': False}
        return {'alignments': aligns, 'proposed_scope': scope,
                'scope_evidence': [self._quote(src[0], self._first_words(src[0]['text']))] if src else [],
                'covered_source_block_ids': [b['block_id'] for b in src],
                'covered_target_block_ids': [b['block_id'] for b in ref[:len(src)]],
                'unresolved_regions': [], 'newly_noticed_source_items': [], 'coverage_notes': []}

    # ------------------------------------------------------------ A2 induce
    def _propose_updates(self, bundle: dict) -> dict:
        records = bundle['packet']['records']
        kind = bundle['packet']['kind']
        ids = [r['experience_id'] for r in records]
        if len(ids) < 2 and kind != 'failure_success':
            return {'comparison_id': bundle['packet']['comparison_id'], 'operations': []}
        domains = sorted({d for r in records for d in r.get('domains', []) if d != 'general'})
        scope = {'level': 'domain' if domains else 'context', 'domains': domains, 'genres': [], 'culture_specific': False,
                 'locale_scope': 'unspecified', 'locales': [], 'setting': None, 'time_period': None,
                 'cross_domain_transfer': True}
        existing = bundle.get('existing_insights', [])
        lesson = ('Retain named subjects, quantities and technical terms; render them in natural Arabic '
                  'rather than literal English syntax.')
        for ins in existing:
            if ins['content']['lesson'] == lesson:
                return {'comparison_id': bundle['packet']['comparison_id'], 'operations': [
                    {'operation_id': 'OP1', 'operation': 'UPVOTE', 'target_insight_id': ins['insight_id'],
                     'expected_version': ins['version'], 'proposed_content': None, 'support_experience_ids': ids,
                     'counterevidence_ids': [], 'justification': 'New independent support for existing wording'}]}
        origin = 'reference' if kind == 'reference' else 'attempt'
        return {'comparison_id': bundle['packet']['comparison_id'], 'operations': [
            {'operation_id': 'OP1', 'operation': 'ADD', 'target_insight_id': None, 'expected_version': None,
             'proposed_content': {'lesson': lesson, 'applies_when': ['the source states facts, names or quantities'],
                                  'avoid_when': ['the item is an incidental example with compatible accepted support'],
                                  'recommended_strategy': 'retain', 'preserve': ['named subjects', 'quantities'],
                                  'scope': scope, 'support_experience_ids': ids, 'counterevidence_ids': [],
                                  'origin': origin},
             'support_experience_ids': ids, 'counterevidence_ids': [],
             'justification': 'Recurring accepted practice across independent groups'}]}
