'use strict';
/* Step Inspector: read-only view over /api/trace/*.
 *
 * Shows, per workflow step (tab), what each agent received and produced: planner, generator,
 * quality evaluator, the feedback injected into the next prompt, and the stored experience record.
 * Trace content is untrusted model output and private data: every value is inserted with
 * textContent / createElement. There is intentionally no innerHTML here. */
(function () {
  const qs = s => document.querySelector(s);

  const TABS = [
    { id: 'ingest',   n: 1, label: 'Ingest & Split',  cmds: ['ingest', 'split'] },
    { id: 'seed',     n: 2, label: 'Seed Memory',     cmds: ['build-seed-memory', 'seed-memory'] },
    { id: 'practice', n: 3, label: 'Practice Loop',   cmds: ['collect-practice', 'practice'] },
    { id: 'insights', n: 4, label: 'Insights',        cmds: ['update-insights', 'consolidate'] },
    { id: 'inspect',  n: 5, label: 'Inspect Memory',  cmds: ['inspect-memory'] },
    { id: 'evaluate', n: 6, label: 'Evaluate',        cmds: ['evaluate'] },
  ];
  const ACTION_TAB = { ingest_split: 'ingest', build_seed: 'seed', collect_practice: 'practice', update_insights: 'insights',
                       inspect_memory: 'inspect', eval_dev: 'evaluate', eval_test: 'evaluate', build_all_memory: 'seed',
                       online_loop: 'practice' };
  const MODE_LABEL = {
    analyse_source: ['A3', 'Source analysis'], make_plan: ['A3', 'Planner'], generate_arabic: ['A4', 'Generator (Arabic)'],
    candidate_quality: ['A5', 'Quality evaluator and Report'], reference_comparison: ['A5', 'Quality evaluator and Report'],
    reference_quality: ['A5', 'Reference quality'], insight_review: ['A5', 'Insight reviewer'],
    analyse_pair: ['A1', 'Pair analyst'], propose_updates: ['A2', 'Insight proposer'],
    reflect_failure: ['A6', 'Reflector (LLM_reflect)'],
  };
  const PAGE = 300;

  // Sessions are loaded newest-first; each tab shows the latest session that has output for its command(s).
  const MAX_SESS = 12;
  const insp = { sess: new Map(), order: [], seq: 0, tab: 'practice', pair: null, att: {}, live: false, liveSess: null,
                 open: new Set(), busy: false, ready: false };
  const promptCache = new Map();

  // ------------------------------------------------------------------ DOM helpers
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const add = (parent, ...kids) => { for (const k of kids) if (k) parent.appendChild(k); return parent; };
  const badge = (text, kind) => el('span', 'ins-badge' + (kind ? ' ' + kind : ''), text);
  const fmt = o => { try { return JSON.stringify(o, null, 2); } catch (e) { return String(o); } };
  const short = (s, n = 22) => { s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s; };
  const ar = text => { const d = el('div', 'ins-ar', text); d.setAttribute('dir', 'rtl'); d.setAttribute('lang', 'ar'); return d; };
  const modelName = m => (m && typeof m === 'object') ? (m.model || m.name || m.provider || fmt(m)) : m;
  const quoteText = q => (q && typeof q === 'object') ? (q.quote ?? fmt(q)) : String(q ?? '');
  const statusKind = s => s === 'pass' || s === 'accepted' || s === 'compatible' || s === 'success' ? 'ok'
    : (s === 'fail' || s === 'unresolved' || s === 'incompatible' || s === 'failure' || s === 'error' ? 'bad' : 'warn');

  /* Collapsible block that builds its body lazily; open/closed state survives re-renders. */
  function fold(key, title, build, cls) {
    const d = el('details', 'ins-fold' + (cls ? ' ' + cls : ''));
    d.appendChild(el('summary', null, title));
    const body = el('div', 'ins-fold-body');
    d.appendChild(body);
    let built = false;
    const fill = () => { if (!built) { built = true; build(body); } };
    d.addEventListener('toggle', () => {
      if (d.open) { insp.open.add(key); fill(); } else { insp.open.delete(key); }
    });
    if (insp.open.has(key)) { d.open = true; fill(); }
    return d;
  }
  const jsonFold = (key, title, obj, cls) => fold(key, title, b => b.appendChild(el('pre', 'ins-pre', fmt(obj))), cls);

  function kv(parent, rows) {
    const g = el('div', 'ins-kv');
    for (const [k, v] of rows) {
      if (v === undefined || v === null || v === '') continue;
      add(g, el('span', 'ins-k', k), el('span', 'ins-v', typeof v === 'object' ? fmt(v) : v));
    }
    if (g.childNodes.length) parent.appendChild(g);
  }

  function genericSummary(body, obj) {
    if (!obj || typeof obj !== 'object') return;
    const rows = [];
    for (const [k, v] of Object.entries(obj)) {
      if (Array.isArray(v)) rows.push([k, v.length + ' item(s)']);
      else if (v && typeof v === 'object') rows.push([k, '{' + Object.keys(v).length + ' fields}']);
      else rows.push([k, v]);
    }
    kv(body, rows);
  }

  // ------------------------------------------------------------------ cards
  function timeOf(ev) { try { return new Date(ev.at).toLocaleTimeString(); } catch (e) { return ''; } }

  function card(ev, title, role, kind) {
    const c = el('article', 'ins-card' + (kind ? ' ' + kind : ''));
    const head = el('div', 'ins-card-head');
    head.setAttribute('role', 'button');
    head.setAttribute('tabindex', '0');
    head.setAttribute('title', 'Click to collapse or expand this section');
    if (role) head.appendChild(badge(role, 'role'));
    head.appendChild(el('b', 'ins-card-title', title));

    const colTag = el('span', 'ins-collapsed-tag', 'collapsed');
    head.appendChild(colTag);

    const right = el('div', 'ins-card-right');
    right.appendChild(el('span', 'ins-time', timeOf(ev)));

    const toggleBtn = el('button', 'ins-card-toggle');
    toggleBtn.type = 'button';
    toggleBtn.setAttribute('aria-label', 'Toggle section');
    toggleBtn.innerHTML = '<svg class="ins-chevron" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>';
    right.appendChild(toggleBtn);
    head.appendChild(right);

    const toggle = () => {
      c.classList.toggle('collapsed');
      toggleBtn.setAttribute('aria-expanded', String(!c.classList.contains('collapsed')));
    };

    head.addEventListener('click', (e) => {
      if (e.target.closest('a') || (e.target.closest('button') && !e.target.closest('.ins-card-toggle')) || e.target.closest('details')) return;
      toggle();
    });

    head.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        toggle();
      }
    });

    const body = el('div', 'ins-card-body');
    add(c, head, body);
    return { c, head, body, right };
  }

  function findingsList(parent, findings) {
    for (const f of findings || []) {
      const row = el('div', 'ins-finding sev-' + (f.severity || ''));
      const t = el('div');
      add(t, el('b', null, f.issue_code || 'finding'), el('span', 'ins-small', ' (' + (f.severity || '?') + ')'));
      row.appendChild(t);
      if (f.explanation) row.appendChild(el('div', 'ins-small', f.explanation));
      if (f.correction_goal) row.appendChild(el('div', 'ins-goal', '→ ' + f.correction_goal));
      parent.appendChild(row);
    }
  }

  function reflectionText(parent, fb) {
    if (fb && fb.reflection) {
      parent.appendChild(el('div', 'ins-sub', 'Reflection (free text, LLM_reflect)'));
      parent.appendChild(el('div', 'ins-text', fb.reflection));
    }
  }

  function feedbackBlock(feedback, title) {
    const b = el('div', 'ins-newprompt');
    b.appendChild(el('div', 'ins-newprompt-title', title));
    for (const fb of feedback || []) {
      b.appendChild(el('div', 'ins-small', 'scope: ' + (fb.scope || '?') + (fb.prior_attempt_id ? ' · prior attempt record ' + short(fb.prior_attempt_id, 30) : '')));
      reflectionText(b, fb);
      findingsList(b, fb.findings);
    }
    return b;
  }

  function criteriaList(parent, criteria, compact) {
    for (const c of criteria || []) {
      if (c.criterion === 'plan_compliance') continue;
      const row = el('div', 'ins-crit');
      const top = el('div', 'ins-crit-top');
      add(top, badge(c.status, statusKind(c.status)), el('b', null, String(c.criterion || '').replace(/_/g, ' ')),
          el('span', 'ins-small', c.score == null ? 'score –' : 'score ' + c.score + ' / 4'));
      row.appendChild(top);
      if (!compact) {
        if (c.explanation) row.appendChild(el('div', 'ins-small', c.explanation));
        findingsList(row, c.findings);
      }
      parent.appendChild(row);
    }
  }

  // ---- per-mode summaries of an agent's output
  const SUMMARY = {
    analyse_source(body, o) {
      kv(body, [['purpose', o.purpose]]);
      const items = o.items || [];
      if (items.length) {
        const t = el('div', 'ins-list-block');
        t.appendChild(el('div', 'ins-sub', items.length + ' cultural / sensitive item(s) found'));
        for (const it of items) {
          const q = (it.source_quotes && it.source_quotes[0]) ? quoteText(it.source_quotes[0]) : '';
          const r = el('div', 'ins-row');
          add(r, el('b', null, q), el('span', 'ins-small', ' ' + [it.category, it.essentiality, it.semantic_function].filter(Boolean).join(' · ')));
          t.appendChild(r);
        }
        body.appendChild(t);
      }
    },
    make_plan(body, o) {
      if (o.reflection) { body.appendChild(el('div', 'ins-sub', 'Reflection')); body.appendChild(el('div', 'ins-text', o.reflection)); }
      const ds = o.decisions || [];
      body.appendChild(el('div', 'ins-sub', ds.length + ' decision(s)'));
      for (const d of ds) {
        const r = el('div', 'ins-row');
        add(r, badge(d.strategy || '?', d.strategy === 'substitute' ? 'plum' : ''), el('b', null, ' ' + d.item_id + ' '));
        if (d.counterpart_ar) r.appendChild(ar(d.counterpart_ar));
        if (d.rationale) r.appendChild(el('div', 'ins-small', d.rationale));
        if (d.fallback_reason) r.appendChild(el('div', 'ins-small', 'fallback: ' + d.fallback_reason));
        body.appendChild(r);
      }
      if ((o.terminology || []).length) {
        body.appendChild(el('div', 'ins-sub', 'Terminology'));
        for (const t of o.terminology) {
          const r = el('div', 'ins-row');
          add(r, el('span', null, t.source_concept + ' → '), ar(t.preferred_ar));
          body.appendChild(r);
        }
      }
      if ((o.unresolved_issues || []).length) body.appendChild(el('div', 'ins-error', 'unresolved: ' + o.unresolved_issues.join('; ')));
    },
    generate_arabic(body, o) {
      for (const b of o.blocks || []) { body.appendChild(el('div', 'ins-small', 'block ' + b.source_block_id)); body.appendChild(ar(b.text_ar)); }
      if ((o.unresolved_issues || []).length) body.appendChild(el('div', 'ins-error', 'unresolved: ' + o.unresolved_issues.join('; ')));
    },
    candidate_quality(body, o) {
      if (!o.reference_comparison) criteriaList(body, o.criteria, true);   // reference-only evaluator: old traces hide criteria
      if (o.reference_comparison) {
        body.appendChild(el('div', 'ins-sub', 'Reference Comparison'));
        comparisonBody(body, o.reference_comparison, true);
      }
    },
    reference_quality(body, o) { criteriaList(body, o.criteria); },
    reference_comparison(body, o) { comparisonBody(body, o, true); },
    analyse_pair(body, o) {
      kv(body, [['proposed scope', o.proposed_scope], ['newly noticed source items', (o.newly_noticed_source_items || []).length || null]]);
      for (const a of o.alignments || []) {
        const r = el('div', 'ins-row');
        add(r, badge(a.observation || '?'), el('span', 'ins-small', ' items: ' + (a.item_ids || []).join(', ')));
        const s = (a.source_quotes || []).map(quoteText).join(' | '), t = (a.target_quotes || []).map(quoteText).join(' | ');
        if (s) r.appendChild(el('div', 'ins-small', 'EN: ' + s));
        if (t) { r.appendChild(el('div', 'ins-small', 'AR:')); r.appendChild(ar(t)); }
        body.appendChild(r);
      }
    },
    propose_updates(body, o) {
      const ops = o.operations || [];
      body.appendChild(el('div', 'ins-sub', ops.length + ' proposed operation(s)'));
      for (const op of ops) body.appendChild(el('div', 'ins-row ins-small', short(fmt(op).replace(/\s+/g, ' '), 400)));
    },
    insight_review(body, o) { genericSummary(body, o); },
  };

  function comparisonBody(body, o, compact) {
    body.appendChild(el('div', null)).appendChild(badge('result: ' + (o.result || '?'), statusKind(o.result)));
    if (compact) return;
    if (o.explanation) body.appendChild(el('div', 'ins-text', o.explanation));
    for (const d of o.differences || []) {
      const r = el('div', 'ins-finding sev-' + (d.critical ? 'critical' : 'substantive'));
      add(r, el('b', null, d.classification || 'difference'), el('span', 'ins-small', d.critical ? ' (critical)' : ''));
      if (d.explanation) r.appendChild(el('div', 'ins-small', d.explanation));
      if (d.correction_goal) r.appendChild(el('div', 'ins-goal', '→ ' + d.correction_goal));
      body.appendChild(r);
    }
  }

  async function loadPrompt(hash, pre, sess) {
    if (!hash) { pre.textContent = '(system prompt not recorded)'; return; }
    const key = sess + '/' + hash;
    if (promptCache.has(key)) { pre.textContent = promptCache.get(key); return; }
    try {
      const r = await fetch('/api/trace/' + encodeURIComponent(sess) + '/prompt/' + encodeURIComponent(hash));
      if (!r.ok) throw new Error('status ' + r.status);
      const d = await r.json();
      promptCache.set(key, d.text);
      pre.textContent = d.text;
    } catch (e) { pre.textContent = '(system prompt unavailable)'; }
  }

  // ------------------------------------------------------------------ event renderers
  function renderLLM(ev) {
    const d = ev.data, lab = MODE_LABEL[d.mode] || [d.role || '?', d.mode];
    const isCrit = d.output && d.output.criteria && d.output.criteria.some(c => (c.findings || []).some(f => f.severity === 'critical'));
    const { c, head, body, right } = card(ev, lab[1], lab[0], (d.error || isCrit) ? 'err' : '');
    const metaSpan = el('span', 'ins-meta', [modelName(d.model), `${d.input_tokens} in / ${d.output_tokens} out`, d.seconds + 's',
      d.schema_corrections ? d.schema_corrections + ' schema correction(s)' : null].filter(Boolean).join(' · '));
    if (right) head.insertBefore(metaSpan, right);
    else head.appendChild(metaSpan);
    if (d.error) body.appendChild(el('div', 'ins-error', d.error));
    if (isCrit) body.appendChild(el('div', 'ins-error', 'A critical finding was reported'));
    const fb = d.input && d.input.feedback;
    if (fb) body.appendChild(feedbackBlock(fb, 'NEW PROMPT SECTION: feedback from the previous attempt, injected into this call'));
    if (d.output) {
      const s = SUMMARY[d.mode];
      if (s) s(body, d.output); else genericSummary(body, d.output);
    }
    if (!d.output && d.raw_text) body.appendChild(el('pre', 'ins-pre', d.raw_text));
    return c;
  }

  function renderSource(ev, analysis) {
    const d = ev.data, { c, body } = card(ev, 'Source analysis');
    body.appendChild(el('div', 'ins-en', d.text));
    const purpose = analysis && analysis.data && analysis.data.output && analysis.data.output.purpose;
    if (purpose) kv(body, [['purpose', purpose]]);
    return c;
  }

  function renderRetrieval(ev) {
    const d = ev.data, ex = d.experiences || [], ins2 = d.insights || [];
    const { c, body } = card(ev, 'Retrieved examples and insights', null);
    if (!ex.length && !ins2.length) body.appendChild(el('div', 'ins-small', 'Nothing retrieved.'));
    for (const e of ex) {
      const r = el('div', 'ins-row');
      r.appendChild(el('div', 'ins-small', e.source_excerpt));
      r.appendChild(ar(e.arabic_excerpt));
      body.appendChild(r);
    }
    for (const i of ins2) {
      const r = el('div', 'ins-row');
      r.appendChild(badge('insight'));
      r.appendChild(el('div', 'ins-text', i.lesson));
      body.appendChild(r);
    }
    return c;
  }

  function renderEvent(ev) {
    const d = ev.data || {};
    switch (ev.kind) {
      case 'pair_start': return null;
      case 'source': return renderSource(ev, ev._analysis);
      case 'llm_call': return renderLLM(ev);
      case 'retrieval': return renderRetrieval(ev);
      case 'plan_frozen': return null;
      case 'candidate': {
        const { c, body } = card(ev, 'Arabic candidate (assembled)', 'A4');
        body.appendChild(ar(d.arabic_text));
        kv(body, [['candidate', short(d.candidate_id, 30)], ['realizations', (d.realizations || []).length]]);
        if ((d.unresolved_issues || []).length) body.appendChild(el('div', 'ins-error', 'unresolved: ' + d.unresolved_issues.join('; ')));
        body.appendChild(jsonFold('e' + ev._i + ':cand', 'Realizations & block map', { realizations: d.realizations, block_map: d.block_map, resolved: d.resolved_realizations }));
        return c;
      }
      case 'evaluation': {
        const { c, body } = card(ev, 'Quality evaluator and Report', 'A5', d.critical ? 'err' : '');
        if (d.critical) body.appendChild(el('div', 'ins-error', 'A critical finding was reported'));
        if (!(d.payload && d.payload.reference_comparison)) criteriaList(body, (d.payload || {}).criteria, true);
        if (d.payload && d.payload.reference_comparison) {
          body.appendChild(el('div', 'ins-sub', 'Reference Comparison'));
          comparisonBody(body, d.payload.reference_comparison, true);
        }
        return c;
      }
      case 'decision': {
        const { c, body } = card(ev, 'Controller decision', null, d.status === 'unresolved' ? 'err' : '');
        const t = el('div', 'ins-chips');
        t.appendChild(badge(d.status, statusKind(d.status)));
        for (const x of d.issue_codes || []) t.appendChild(badge(x, 'bad'));
        body.appendChild(t);
        for (const f of d.fallback_reasons || []) body.appendChild(el('div', 'ins-small', 'fallback: ' + f));
        if ((d.findings || []).length) body.appendChild(jsonFold('e' + ev._i + ':f', 'Findings', d.findings));
        return c;
      }
      case 'pipeline_error': {
        const { c, body } = card(ev, 'Pipeline error', null, 'err');
        body.appendChild(el('div', 'ins-error', (d.code || '') + ': ' + (d.message || '')));
        if (d.details && Object.keys(d.details).length) body.appendChild(jsonFold('e' + ev._i + ':det', 'Details', d.details));
        return c;
      }
      case 'reference_comparison': {
        const { c, body } = card(ev, 'Reference comparison (private reference)', 'A5');
        comparisonBody(body, d.payload || { result: d.result });
        if (d.note) body.appendChild(el('div', 'ins-small', d.note));
        return c;
      }
      case 'feedback': {
        const { c, body } = card(ev, `Reflexion feedback for attempt ${d.for_attempt} (becomes part of the next prompt)`, null, 'plum');
        body.appendChild(el('div', 'ins-small', 'scope: ' + d.scope + (d.language_only_repair ? ' · language-only repair: the frozen plan is kept and only A4 rewrites' : ' · the planner re-plans with this feedback')));
        for (const fb of d.feedback || []) { reflectionText(body, fb); findingsList(body, fb.findings); }
        body.appendChild(jsonFold('e' + ev._i + ':fb', 'Feedback object (exact JSON injected)', d.feedback));
        return c;
      }
      case 'reflection': {
        const { c, body } = card(ev, 'Failure reflection (free text for the next attempt)', 'A6', 'plum');
        body.appendChild(el('div', 'ins-text', d.reflection));
        if ((d.earlier_reflections || []).length) body.appendChild(jsonFold('e' + ev._i + ':earlier', 'Earlier reflections given to the reflector', d.earlier_reflections));
        return c;
      }
      case 'reflection_error': {
        const { c, body } = card(ev, 'Reflection unavailable, structured feedback used instead', 'A6', 'err');
        body.appendChild(el('div', 'ins-error', d.error || ''));
        return c;
      }
      case 'experience': {
        const r = d.record || {};
        const { c, body } = card(ev, 'Experience record stored', null, 'ok-card');
        const t = el('div', 'ins-chips');
        add(t, badge(d.outcome, statusKind(d.outcome)), badge(d.eligible ? 'eligible as example' : 'not eligible as example', d.eligible ? 'ok' : ''));
        for (const x of r.issue_codes || []) t.appendChild(badge(x, 'bad'));
        body.appendChild(t);
        kv(body, [['experience', d.experience_id], ['kind', r.kind], ['attempt', r.attempt_number], ['previous attempt', r.previous_attempt_id],
                  ['plan', r.plan_id], ['candidate', r.candidate_id], ['quality report', r.quality_report_id],
                  ['comparison report', r.comparison_report_id], ['supports (memory used)', (d.supports || []).join(', ')]]);
        const k = 'e' + ev._i;
        body.appendChild(jsonFold(k + ':rec', 'Experience record (JSON)', r));
        body.appendChild(jsonFold(k + ':meta', 'Retrieval metadata stored with it (summary, scope, plan, assessment)', d.retrieval_meta));
        return c;
      }
      case 'pair_end': {
        const { c, body } = card(ev, 'Task finished');
        const t = el('div', 'ins-chips');
        add(t, badge(d.success ? 'success' : 'failed', d.success ? 'ok' : 'bad'), badge('stop: ' + d.stop_reason), badge(d.attempts + ' attempt(s)'));
        body.appendChild(t);
        return c;
      }
      case 'admission': {
        const { c, body } = card(ev, 'Reference admission to memory', null, d.eligible ? 'ok-card' : 'err');
        const t = el('div', 'ins-chips');
        t.appendChild(badge(d.eligible ? 'admitted as demonstration' : 'excluded', d.eligible ? 'ok' : 'bad'));
        body.appendChild(t);
        for (const x of d.reasons || []) body.appendChild(el('div', 'ins-small', x));
        kv(body, [['experience', d.experience_id]]);
        const k = 'e' + ev._i;
        body.appendChild(jsonFold(k + ':rec', 'Experience record (JSON)', d.record));
        body.appendChild(jsonFold(k + ':meta', 'Retrieval metadata stored with it', d.retrieval_meta));
        return c;
      }
      case 'admission_error': case 'insight_batch_error': {
        const { c, body } = card(ev, ev.kind === 'admission_error' ? 'Admission failed' : 'Insight batch failed', null, 'err');
        body.appendChild(el('div', 'ins-error', (d.code || '') + ': ' + (d.message || '')));
        return c;
      }
      case 'pair_skipped': {
        const { c, body } = card(ev, 'Skipped (already admitted)');
        kv(body, [['pair', d.pair_id], ['experience', d.experience_id], ['eligible', String(d.eligible)]]);
        body.appendChild(el('div', 'ins-small', d.note));
        return c;
      }
      case 'packet': {
        const { c, body } = card(ev, `Comparison packet (${d.packet_kind}): ${(d.records || []).length} record(s)`);
        for (const r of d.records || []) {
          const row = el('div', 'ins-row');
          add(row, el('b', null, short(r.experience_id, 26)), el('span', 'ins-small', ` ${r.kind} · ${r.outcome || ''} · ${(r.domains || []).join(', ')}`));
          row.appendChild(el('div', 'ins-small', r.source_excerpt));
          if (r.arabic_excerpt) row.appendChild(ar(r.arabic_excerpt));
          body.appendChild(row);
        }
        body.appendChild(jsonFold('e' + ev._i + ':ex', (d.existing_insights || []).length + ' existing insight(s) shown to A2', d.existing_insights));
        return c;
      }
      case 'insight_batch': {
        const { c, body } = card(ev, 'Insight update (applied by controller)');
        if (d.note) body.appendChild(el('div', 'ins-small', d.note));
        const t = el('div', 'ins-chips');
        add(t, badge((d.committed || []).length + ' committed', 'ok'), badge((d.rejected || []).length + ' rejected', (d.rejected || []).length ? 'bad' : ''));
        body.appendChild(t);
        const k = 'e' + ev._i;
        if ((d.committed || []).length) body.appendChild(jsonFold(k + ':c', 'Committed', d.committed));
        if ((d.rejected || []).length) body.appendChild(jsonFold(k + ':r', 'Rejected (with reasons)', d.rejected));
        if (d.review) body.appendChild(jsonFold(k + ':rev', 'A5 review', d.review));
        return c;
      }
      case 'packets_built': {
        const { c, body } = card(ev, `Comparison packets built: ${d.count}`);
        for (const p of d.packets || []) body.appendChild(el('div', 'ins-small', `${p.kind} · ${p.records} record(s) · ${short(p.comparison_id, 30)}`));
        return c;
      }
      case 'release_published': {
        const { c, body } = card(ev, 'Memory release published', null, 'ok-card');
        kv(body, [['release', d.release && d.release.release_id], ['base', d.base], ['demonstrations', d.release && d.release.demonstrations], ['active insights', d.release && d.release.insights]]);
        body.appendChild(jsonFold('e' + ev._i + ':rel', 'Details', d));
        return c;
      }
      case 'command_start': {
        const { c, body } = card(ev, 'Command: ' + d.command);
        kv(body, [['arguments', d.args]]);
        return c;
      }
      case 'summary': {
        const { c, body } = card(ev, 'Result summary');
        genericSummary(body, d.result);
        body.appendChild(jsonFold('e' + ev._i + ':sum', 'Full JSON', d.result));
        return c;
      }
      case 'eval_row': {
        const row = d.row || {};
        const { c, body } = card(ev, 'Final result for this item', null, row.status === 'unresolved' ? 'err' : '');
        const t = el('div', 'ins-chips');
        t.appendChild(badge(row.status, statusKind(row.status)));
        if (row.reference_comparison) t.appendChild(badge('reference: ' + row.reference_comparison, statusKind(row.reference_comparison)));
        for (const x of row.issue_codes || []) t.appendChild(badge(x, 'bad'));
        body.appendChild(t);
        if (d.arabic_text) body.appendChild(ar(d.arabic_text));
        body.appendChild(jsonFold('e' + ev._i + ':row', 'Row', row));
        return c;
      }
      case 'attempt_start': case 'profile': case 'attempt_end': return null;
      default: {
        const { c, body } = card(ev, ev.kind);
        body.appendChild(el('pre', 'ins-pre', fmt(d)));
        return c;
      }
    }
  }

  function timeline(events, numbered = true) {
    const wrap = el('div', 'ins-timeline');
    let n = 0, lastCmp = null;
    const hasCandQualityLLM = events.some(e => e.kind === 'llm_call' && (e.data || {}).mode === 'candidate_quality');
    const hasCandQuality = hasCandQualityLLM || events.some(e => e.kind === 'evaluation');

    const refCmpLLM = events.find(e => e.kind === 'llm_call' && (e.data || {}).mode === 'reference_comparison');
    // Reference-only Quality evaluator: the reference comparison call IS the evaluator (no criteria call exists).
    const referenceOnly = !!refCmpLLM && !hasCandQualityLLM;
    const refCmpEv = events.find(e => e.kind === 'reference_comparison');
    const standaloneRefCmpPayload = (refCmpLLM && refCmpLLM.data && refCmpLLM.data.output) ||
                                    (refCmpEv && refCmpEv.data && (refCmpEv.data.payload || { result: refCmpEv.data.result }));

    const hasCandidate = events.some(e => e.kind === 'candidate');
    // Source text + A3 analysis are shown together as one 'Source analysis' card.
    const srcEv = events.find(e => e.kind === 'source');
    const anaEv = srcEv && events.find(e => e.kind === 'llm_call' && (e.data || {}).mode === 'analyse_source' && e.data.output);
    if (anaEv) srcEv._analysis = anaEv;

    for (const ev of events) {
      if (anaEv && ev === anaEv) continue;
      if (hasCandidate && ev.kind === 'llm_call' && (ev.data || {}).mode === 'generate_arabic') continue;
      if (referenceOnly && (ev.kind === 'evaluation' || ev.kind === 'reference_comparison')) continue;

      // 1. Avoid redundant evaluation event when candidate_quality LLM call is already present
      if (ev.kind === 'evaluation' && hasCandQualityLLM) {
        const wr = (ev.data || {}).window_reports || [];
        if (wr.length <= 1) continue;
      }

      // 2. If a reference comparison exists in this attempt, integrate it into Quality evaluator and Report
      if (standaloneRefCmpPayload && hasCandQuality) {
        if (ev.kind === 'llm_call' && (ev.data || {}).mode === 'candidate_quality' && ev.data.output) {
          if (!ev.data.output.reference_comparison) {
            ev.data.output.reference_comparison = standaloneRefCmpPayload;
          }
        } else if (ev.kind === 'evaluation' && ev.data && ev.data.payload) {
          if (!ev.data.payload.reference_comparison) {
            ev.data.payload.reference_comparison = standaloneRefCmpPayload;
          }
        }
      }

      // 3. Skip standalone reference comparison cards when integrated into Quality Evaluator,
      // or if an LLM call exists, skip the redundant trace event card so it never appears twice.
      if (hasCandQuality && !referenceOnly && (ev.kind === 'reference_comparison' || (ev.kind === 'llm_call' && (ev.data || {}).mode === 'reference_comparison'))) {
        continue;
      }
      if (!hasCandQuality && ev.kind === 'reference_comparison' && refCmpLLM) {
        continue; // never duplicate Card 10 and Card 11
      }

      if (ev.comparison_id && ev.comparison_id !== lastCmp) {
        lastCmp = ev.comparison_id;
        wrap.appendChild(el('div', 'ins-divider', 'Comparison packet ' + short(ev.comparison_id, 40)));
      }
      let node = null;
      try { node = renderEvent(ev); } catch (e) { node = el('div', 'ins-error', 'Could not render ' + ev.kind); }
      if (!node) continue;
      const head = node.querySelector('.ins-card-head');
      if (head && numbered) head.insertBefore(el('span', 'ins-step', String(++n)), head.firstChild);
      wrap.appendChild(node);
    }
    if (numbered && wrap.querySelectorAll('.ins-card').length > 1) {
      const tools = el('div', 'ins-timeline-tools');
      const colBtn = el('button', 'ins-tool-btn');
      colBtn.type = 'button';
      colBtn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 14 12 6 20 14"></polyline><line x1="4" y1="18" x2="20" y2="18"></line></svg><span>Collapse all</span>';
      colBtn.onclick = () => {
        wrap.querySelectorAll('.ins-card').forEach(x => x.classList.add('collapsed'));
      };
      const expBtn = el('button', 'ins-tool-btn');
      expBtn.type = 'button';
      expBtn.innerHTML = '<svg viewBox="0 0 24 24" width="13" height="13" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 10 12 18 20 10"></polyline><line x1="4" y1="6" x2="20" y2="6"></line></svg><span>Expand all</span>';
      expBtn.onclick = () => {
        wrap.querySelectorAll('.ins-card').forEach(x => x.classList.remove('collapsed'));
      };
      add(tools, colBtn, expBtn);
      wrap.insertBefore(tools, wrap.firstChild);
    }
    return wrap;
  }

  // ------------------------------------------------------------------ grouping / views
  const cmdTab = cmd => { const t = TABS.find(x => x.cmds.includes(cmd)); return t ? t.id : null; };

  /* Events of the most recent stored run that produced output for this tab. */
  function tabEvents(tab) {
    for (const id of insp.order) {
      const s = insp.sess.get(id);
      if (!s) continue;
      const evs = s.events.filter(e => cmdTab(e.command) === tab);
      if (evs.length) return evs;
    }
    return [];
  }

  function groupsOf(tab) {
    const map = new Map();
    for (const e of tabEvents(tab)) {
      let key = '__run', label = 'Run overview';
      if (e.phase === 'insights') { key = '__insights'; label = 'Insight extraction (A2 → A5)'; }
      else if (e.pair_id) { key = e.pair_id; label = 'Pair ' + e.pair_id; }
      let g = map.get(key);
      if (!g) { g = { key, label, events: [], status: null, isPair: !!e.pair_id && e.phase !== 'insights' }; map.set(key, g); }
      g.events.push(e);
      const d = e.data || {};
      if (e.kind === 'pair_end') g.status = [d.success ? 'ok' : 'bad', d.success ? 'success' : 'failed'];
      else if (e.kind === 'admission') g.status = [d.eligible ? 'ok' : 'bad', d.eligible ? 'admitted' : 'excluded'];
      else if (e.kind === 'admission_error') g.status = ['bad', 'error'];
      else if (e.kind === 'pair_skipped') g.status = ['', 'reused'];
      else if (e.kind === 'eval_row') g.status = [statusKind((d.row || {}).status), (d.row || {}).status];
    }
    const arr = [...map.values()];
    const run = (tab === 'practice' || tab === 'evaluate') ? [] : arr.filter(g => g.key === '__run');
    const rest = arr.filter(g => g.key !== '__run');
    for (const g of rest) if (!g.status && g.isPair && insp.live) g.status = ['warn', 'running…'];
    return [...run, ...rest];
  }

  function attemptsOf(g) {
    const m = new Map();
    for (const e of g.events) {
      if (e.attempt == null) continue;
      if (!m.has(e.attempt)) m.set(e.attempt, { n: e.attempt, outcome: null });
      if (e.kind === 'attempt_end') m.get(e.attempt).outcome = e.data.outcome;
    }
    return [...m.values()].sort((a, b) => a.n - b.n);
  }

  function renderGroup(tab, g) {
    const main = el('div', 'ins-group');
    const head = el('div', 'ins-group-head');
    head.appendChild(el('h3', null, g.label));
    if (g.status) head.appendChild(badge(g.status[1], g.status[0]));
    main.appendChild(head);
    if (g.key === '__insights') {
      const { rows } = insightRows(g.events);
      if (!rows.length) main.appendChild(el('div', 'ins-empty', 'No insights were extracted.'));
      for (const r of rows) {
        const row = el('div', 'ins-insight' + (r.status === 'rejected' ? ' rejected' : ''));
        row.appendChild(el('div', 'ins-insight-text', r.lesson));
        const meta = el('div', 'ins-insight-meta');
        add(meta, badge(r.status, r.status === 'committed' ? 'ok' : (r.status === 'rejected' ? 'bad' : '')));
        if (r.version) add(meta, badge('v' + r.version, 'neutral'));
        if (r.importance != null) add(meta, badge('importance ' + r.importance, 'neutral'));
        if (r.reason) meta.appendChild(el('span', 'ins-small', r.reason));
        row.appendChild(meta);
        main.appendChild(row);
      }
      return main;
    }
    const attempts = attemptsOf(g);
    if (!attempts.length) { main.appendChild(timeline(g.events)); return main; }

    // Practice: the closing bookkeeping cards (controller decision, stored experience record, task finished)
    // are not shown; a successful pair ends with its original and glocalized text instead.
    const CLOSING = tab === 'practice' ? new Set(['decision', 'experience', 'pair_end']) : new Set();
    const pre = g.events.filter(e => e.attempt == null && e.kind !== 'pair_end');
    if (pre.length) main.appendChild(timeline(pre, false));
    // Attempts as tabs: the latest attempt is shown unless the user picked another one for this pair.
    const akey = tab + ':' + g.key;
    let cur = insp.att[akey];
    if (!attempts.some(a => a.n === cur)) cur = attempts[attempts.length - 1].n;
    const bar = el('div', 'ins-atabs');
    bar.setAttribute('role', 'tablist');
    for (const a of attempts) {
      const b = el('button', 'ins-atab' + (a.n === cur ? ' active' : ''));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(a.n === cur));
      add(b, el('span', null, 'Attempt ' + a.n), a.outcome ? badge(a.outcome, statusKind(a.outcome)) : badge('running…', 'warn'));
      b.onclick = () => { insp.att[akey] = a.n; render(); };
      bar.appendChild(b);
    }
    main.appendChild(bar);
    main.appendChild(timeline(g.events.filter(e => e.attempt === cur && !CLOSING.has(e.kind))));

    const endEv = g.events.find(e => e.kind === 'pair_end');
    if (tab === 'practice') {
      if (endEv && (endEv.data || {}).success) {
        const src = g.events.find(e => e.kind === 'source');
        const cands = g.events.filter(e => e.kind === 'candidate');
        const fin = cands[cands.length - 1];
        if (src && fin) {
          const wrapFinal = el('div', 'ins-timeline');
          wrapFinal.style.marginTop = '14px';
          const origCard = card(src, 'Final Original Source Text (English)', null, null);
          origCard.body.appendChild(el('div', 'ins-en', (src.data || {}).text));
          const glocalCard = card(fin, 'Final Glocalized Output (Arabic)', null, 'ok-card');
          glocalCard.body.appendChild(ar((fin.data || {}).arabic_text));
          wrapFinal.appendChild(origCard.c);
          wrapFinal.appendChild(glocalCard.c);
          main.appendChild(wrapFinal);
        }
      }
    } else {
      const post = g.events.filter(e => e.attempt == null && e.kind === 'pair_end');
      if (post.length) main.appendChild(timeline(post, false));
    }
    return main;
  }

  function renderSummaryTab(tab, body) {
    const evs = tabEvents(tab);
    if (!evs.length) return false;
    body.appendChild(timeline(evs.filter(e => e.kind !== 'llm_call')));
    return true;
  }

  function renderInspectTab(body) {
    const evs = tabEvents('inspect');
    if (!evs.length) return false;
    // Omit 'Command: inspect-memory' and 'Result summary' sections
    const otherEvs = evs.filter(e => e.kind !== 'llm_call' && e.kind !== 'command_start' && e.kind !== 'summary');
    if (otherEvs.length) body.appendChild(timeline(otherEvs));

    const sm = evs.filter(e => e.kind === 'summary' && e.data && e.data.result && Array.isArray(e.data.result.insights)).pop();
    const res = sm && sm.data && sm.data.result;
    if (!res && !otherEvs.length) {
      if (insp.live) {
        body.appendChild(el('div', 'ins-empty', 'Inspecting memory release…'));
        return true;
      }
      return false;
    }
    if (res && Array.isArray(res.insights)) {
      const box = el('div', 'ins-group');
      const smp = res.sample ? ` · after sample ${res.sample.index}/${res.sample.of} (pair ${res.sample.pair_id})` : '';
      box.appendChild(el('h3', null, `Insights in ${res.release ? res.release.release_id : 'release'} (${res.insights.length})${smp}`));
      if (!res.insights.length) {
        box.appendChild(el('div', 'ins-empty', 'No insights found in this release.'));
      }
      for (const i of res.insights) {
        const r = el('div', 'ins-row');
        add(r, badge(i.status || '?'), el('b', null, ' ' + short(i.insight_id, 24) + ' v' + i.version),
            el('span', 'ins-small', ` importance ${i.importance} · ${(i.support_groups || []).length} support group(s)`));
        r.appendChild(el('div', 'ins-text', i.lesson));
        box.appendChild(r);
      }
      body.appendChild(box);
    }
    return true;
  }

  const EMPTY = {
    ingest: 'Run “Ingest & Split” above to see what was ingested and how the pairs were partitioned.',
    seed: 'Run “Build Seed Memory” above. For every seed pair you will see A3’s analysis, A1’s pair analysis, A5’s reference quality and whether it was admitted to memory.',
    practice: 'Run “Experiential Practice Loop” above. For every practice pair and attempt you will see the planner, generator and quality evaluator outputs, the feedback injected into the next prompt, and the stored experience record.',
    insights: 'Run “Consolidate into M1” above to see comparison packets, A2’s proposals, A5’s review and which insights were committed.',
    inspect: 'Run “Inspect Release” above.',
    evaluate: 'Run “Eval Dev” or “Eval Test” above to see each item’s plan, candidate, evaluation and reference comparison.',
  };

  /* Badge number on a step tab: pairs for pair-based steps, insights for the Insights tab, none otherwise. */
  function tabCount(tab) {
    if (tab === 'insights') return insightRows(tabEvents('insights')).rows.length;
    if (tab === 'seed' || tab === 'practice') return groupsOf(tab).filter(g => g.isPair).length;
    if (tab === 'evaluate') return tabEvents('evaluate').filter(e => e.kind === 'eval_row').length;
    return 0;
  }

  function renderTabs() {
    const bar = qs('#insTabs');
    if (!bar) return;
    bar.replaceChildren();
    for (const t of TABS) {
      const count = tabCount(t.id);
      const b = el('button', 'ins-tab' + (t.id === insp.tab ? ' active' : ''));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(t.id === insp.tab));
      add(b, el('span', 'ins-tab-n', t.n), el('span', null, t.label), count ? el('span', 'ins-tab-count', count) : null);
      b.onclick = () => { insp.tab = t.id; render(); };
      bar.appendChild(b);
    }
  }
  /* ---- Insights tab: plain-text insights extracted from the practice, listed per pair. */
  function expPairMap() {
    const m = new Map();
    for (const s of insp.sess.values()) {
      for (const e of s.events) {
        const d = e.data || {};
        if (e.pair_id && d.experience_id && (e.kind === 'experience' || e.kind === 'admission' || e.kind === 'pair_skipped')) m.set(d.experience_id, e.pair_id);
      }
    }
    return m;
  }

  function insightRows(evs) {
    const map = expPairMap(), pairs = new Set();
    for (const e of evs) if (e.kind === 'pair_start' && e.pair_id) pairs.add(e.pair_id);

    // Collect lesson text by insight_id or normalized lesson across all operations and summaries
    const lessonById = new Map();
    const idByLesson = new Map();

    // Check all session events to index canonical lessons and proposals (including seed memory & retrievals)
    const allEvents = [];
    for (const s of insp.sess.values()) {
      if (s.events) allEvents.push(...s.events);
    }
    const eventsToScan = allEvents.length ? allEvents : evs;

    for (const e of eventsToScan) {
      const d = e.data || {};
      const resIns = [];
      if (d.result && Array.isArray(d.result.insights)) resIns.push(...d.result.insights);
      if (d.release && Array.isArray(d.release.insights)) resIns.push(...d.release.insights);
      if (Array.isArray(d.insights)) resIns.push(...d.insights);

      for (const it of resIns) {
        if (it && typeof it === 'object' && it.insight_id && it.lesson) {
          lessonById.set(it.insight_id, it.lesson);
          idByLesson.set(it.lesson.trim().toLowerCase(), it.insight_id);
        }
      }

      if (e.kind === 'insight_batch') {
        const commMap = new Map((d.committed || []).filter(c => c && typeof c === 'object').map(c => [c.operation_id, c]));
        for (const op of (d.proposal || {}).operations || []) {
          if (!op || typeof op !== 'object') continue;
          const comm = commMap.get(op.operation_id);
          const l = (op.proposed_content || {}).lesson;
          if (!l) continue;
          const norm = l.trim().toLowerCase();
          const iid = (comm && comm.insight_id) || op.target_insight_id;
          if (iid) {
            lessonById.set(iid, l);
            idByLesson.set(norm, iid);
          }
        }
      }
    }

    // Track active committed insights by lineage (insight_id) to avoid duplicates across revisions (ADD vs EDIT/UPVOTE)
    const insightMap = new Map();

    for (const e of evs) {
      if (e.kind !== 'insight_batch') continue;
      const d = e.data || {};
      const committedMap = new Map((d.committed || []).map(c => [c.operation_id, c]));
      const ops = (d.proposal || {}).operations || [];

      for (const op of ops) {
        const comm = committedMap.get(op.operation_id);
        if (!comm) continue; // Only committed operations are shown in the Insights view

        const proposedLesson = (op.proposed_content || {}).lesson;
        let insightId = comm.insight_id || op.target_insight_id;

        // If insightId is missing (e.g. ADD in synced traces), resolve by proposed lesson
        if (!insightId && proposedLesson) {
          insightId = idByLesson.get(proposedLesson.trim().toLowerCase());
        }
        if (!insightId && proposedLesson) {
          insightId = 'ins_gen_' + proposedLesson.slice(0, 32);
        }
        if (!insightId) continue;

        const lesson = proposedLesson || lessonById.get(insightId) || '';

        const ids = [...(op.support_experience_ids || []), ...(op.counterevidence_ids || [])];
        const ps = ids.map(i => map.get(i)).filter(Boolean);
        ps.forEach(p => pairs.add(p));

        const existing = insightMap.get(insightId);

        if (!existing) {
          insightMap.set(insightId, {
            id: insightId,
            version: comm.version || 1,
            importance: comm.importance || 1,
            status: 'committed',
            lesson: lesson,
            pairs: new Set(ps)
          });
        } else {
          // Update version and preserve latest lesson content; union all contributing pairs
          if ((comm.version || 0) >= (existing.version || 0)) {
            existing.version = comm.version || existing.version;
            existing.importance = comm.importance || existing.importance;
          }
          if (lesson && !existing.lesson) {
            existing.lesson = lesson;
          } else if (proposedLesson) {
            existing.lesson = proposedLesson;
          }
          ps.forEach(p => existing.pairs.add(p));
        }
      }
    }

    const rows = [...insightMap.values()].map(item => ({
      insight_id: item.id,
      version: item.version,
      importance: item.importance,
      status: item.status,
      lesson: item.lesson || lessonById.get(item.id) || '',
      pairs: [...item.pairs].sort()
    }));

    return { rows, pairs: [...pairs].sort() };
  }

  function renderInsightsTab(body) {
    const evs = tabEvents('insights');
    if (!evs.length) { body.appendChild(el('div', 'ins-empty', EMPTY.insights)); return; }
    const { rows, pairs } = insightRows(evs);
    const entries = [{ key: '__all', label: 'All insights', rows }, ...pairs.map(p => ({ key: p, label: 'Pair ' + p, rows: rows.filter(r => r.pairs.includes(p)) }))];
    let cur = entries.some(x => x.key === insp.pair) ? insp.pair : '__all';
    const layout = el('div', 'ins-layout'), side = el('div', 'ins-side');
    for (const x of entries) {
      const b = el('button', 'ins-item' + (x.key === cur ? ' active' : ''));
      b.type = 'button';
      add(b, el('span', 'ins-item-label', x.label), badge(String(x.rows.length), x.rows.length ? 'ok' : ''));
      b.onclick = () => { insp.pair = x.key; render(); };
      side.appendChild(b);
    }
    const sel = entries.find(x => x.key === cur);
    const main = el('div', 'ins-group');
    main.appendChild(add(el('div', 'ins-group-head'), el('h3', null, sel.label + ': ' + sel.rows.length + ' insight' + (sel.rows.length === 1 ? '' : 's'))));
    if (!sel.rows.length) main.appendChild(el('div', 'ins-empty', 'No insights were extracted for this pair.'));
    for (const r of sel.rows) {
      const row = el('div', 'ins-insight' + (r.status === 'rejected' ? ' rejected' : ''));
      row.appendChild(el('div', 'ins-insight-text', r.lesson));
      const meta = el('div', 'ins-insight-meta');
      add(meta, badge(r.status, r.status === 'committed' ? 'ok' : (r.status === 'rejected' ? 'bad' : '')));
      if (r.version) add(meta, badge('v' + r.version, 'neutral'));
      if (r.importance != null) add(meta, badge('importance ' + r.importance, 'neutral'));
      if (sel.key === '__all' && r.pairs.length) meta.appendChild(el('span', 'ins-small', 'from pair ' + r.pairs.join(', ')));
      if (r.reason) meta.appendChild(el('span', 'ins-small', r.reason));
      row.appendChild(meta);
      main.appendChild(row);
    }
    add(layout, side, main);
    body.appendChild(layout);
  }

  function renderBody() {
    const root = qs('#insBody');
    if (!root) return;
    const y = window.scrollY;
    const body = el('div');
    const tab = insp.tab;
    if (tab === 'ingest') {
      if (!renderSummaryTab(tab, body)) body.appendChild(el('div', 'ins-empty', EMPTY[tab]));
    } else if (tab === 'inspect') {
      if (!renderInspectTab(body)) body.appendChild(el('div', 'ins-empty', EMPTY[tab]));
    } else if (tab === 'insights') {
      renderInsightsTab(body);
    } else {
      const groups = groupsOf(tab);
      if (!groups.length) {
        body.appendChild(el('div', 'ins-empty', EMPTY[tab]));
      } else {
        // Pair list on the left, output of the selected entry on the right. The selected pair is kept when
        // switching tabs, so the same pair can be followed through Seed, Practice and Evaluate.
        const keys = groups.map(g => g.key);
        let cur = keys.includes(insp.pair) ? insp.pair : '__run';
        if (!keys.includes(cur)) cur = keys[0];
        const layout = el('div', 'ins-layout');
        const side = el('div', 'ins-side');
        for (const g of groups) {
          const b = el('button', 'ins-item' + (g.key === cur ? ' active' : ''));
          b.type = 'button';
          add(b, el('span', 'ins-item-label', g.key === '__run' ? g.label : short(g.label, 26)), g.status ? badge(g.status[1], g.status[0]) : null);
          b.onclick = () => { insp.pair = g.key; render(); };
          side.appendChild(b);
        }
        add(layout, side, renderGroup(tab, groups.find(g => g.key === cur)));
        body.appendChild(layout);
      }
    }
    root.replaceChildren(body);
    window.scrollTo(window.scrollX, y);
  }

  function render() { renderTabs(); renderBody(); }

  // ------------------------------------------------------------------ data
  /* Fetch (incrementally) all events of one stored session; returns how many were added. */
  async function fetchSession(id) {
    let s = insp.sess.get(id);
    if (!s) { s = { events: [], next: 0, busy: false }; insp.sess.set(id, s); }
    if (s.busy) return 0;
    s.busy = true;
    let added = 0;
    try {
      for (;;) {
        const r = await fetch('/api/trace/' + encodeURIComponent(id) + '?start=' + s.next + '&limit=' + PAGE);
        if (!r.ok) break;
        const d = await r.json();
        for (const e of d.events) { e._i = ++insp.seq; e._s = id; s.events.push(e); }
        s.next = d.next;
        added += d.events.length;
        if (d.events.length < PAGE) break;
      }
    } catch (e) {
      console.warn('trace fetch failed', e);
    } finally {
      s.busy = false;
    }
    return added;
  }

  async function loadSessions() {
    try {
      const r = await fetch('/api/trace/sessions');
      if (!r.ok) return null;
      const d = await r.json();
      const ids = (d.sessions || []).map(s => s.session);
      if (insp.liveSess && insp.live && !ids.includes(insp.liveSess)) ids.unshift(insp.liveSess);
      const valid = new Set(ids);
      for (const k of [...insp.sess.keys()]) {
        if (!valid.has(k)) insp.sess.delete(k);
      }
      insp.order = ids;
      return d;
    } catch (e) { return null; }
  }

  function clearCache() {
    insp.sess.clear();
    insp.order = [];
    insp.open.clear();
    insp.att = {};
    insp.pair = null;
    insp.liveSess = null;
    promptCache.clear();
    render();
  }

  /* Load the newest stored runs so every step tab can show its latest output. */
  async function refresh() {
    if (insp.busy) return;
    insp.busy = true;
    try {
      await loadSessions();
      for (const id of insp.order.slice(0, MAX_SESS)) { await fetchSession(id); render(); }
      if (!tabEvents(insp.tab).length) {
        const t = TABS.find(x => tabEvents(x.id).length);
        if (t) { insp.tab = t.id; render(); }
      }
    } finally {
      insp.busy = false;
    }
  }

  async function init() {
    if (!insp.ready) {
      insp.ready = true;
      const rf = qs('#insRefresh');
      if (rf) rf.onclick = () => refresh();
      render();
      setInterval(() => {
        if (!insp.live && !document.hidden) refresh();
      }, 4000);
    }
    await refresh();
  }

  /* Called from the workflow status poll (every ~1.2 s while a workflow runs). */
  function onStatus(st) {
    const wasLive = insp.live;
    insp.live = !!st.running;
    const id = st.trace_session;
    if (!id) return;
    if (!insp.order.includes(id)) insp.order.unshift(id);
    if (st.running && id !== insp.liveSess) {
      insp.liveSess = id;
      insp.tab = ACTION_TAB[st.action] || insp.tab;     // jump to the step that just started
    }
    fetchSession(id).then(added => { if (added || wasLive !== insp.live) render(); });
  }

  window.inspInit = init;
  window.inspOnStatus = onStatus;
  window.inspTimeline = timeline;
  window.inspClear = clearCache;
})();
