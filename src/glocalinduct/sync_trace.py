#!/usr/bin/env python3
"""Synchronize stored SQLite experiences/runs into Step Inspector trace events.

Allows a CLI pipeline run (e.g. ./run_qwen.sh) that started without GLOCALINDUCT_TRACE_SESSION
to appear live in the Step Inspector web UI with incremental append support.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / 'workspace' / 'staging' / 'memory.sqlite3'
TRACES_ROOT = ROOT / 'workspace' / 'traces'
RUNS_DIR = ROOT / 'workspace' / 'runs'


def _load_objects_map(conn: sqlite3.Connection, record_ids: list[str]) -> dict[str, dict]:
    if not record_ids:
        return {}
    out: dict[str, dict] = {}
    chunk_size = 400
    for i in range(0, len(record_ids), chunk_size):
        chunk = record_ids[i:i + chunk_size]
        q = f"SELECT record_id, record_type, created_at, payload_json FROM objects WHERE record_id IN ({','.join('?' * len(chunk))})"
        for r in conn.execute(q, chunk):
            out[r['record_id']] = {
                'record_id': r['record_id'],
                'record_type': r['record_type'],
                'created_at': r['created_at'],
                'payload': json.loads(r['payload_json']),
            }
    return out


def _write_session_events(session_name: str, events: list[dict]) -> int:
    sdir = TRACES_ROOT / session_name
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / 'prompts').mkdir(parents=True, exist_ok=True)
    tfile = sdir / 'trace.jsonl'
    tmp = sdir / 'trace.jsonl.tmp'
    with tmp.open('w', encoding='utf-8') as fh:
        for idx, ev in enumerate(events, 1):
            ev['seq'] = idx
            fh.write(json.dumps(ev, ensure_ascii=False) + '\n')
    tmp.replace(tfile)
    return len(events)


class TraceSyncer:
    def __init__(self):
        self.seed_count = -1
        self.proposal_count = -1
        self.seen_practice_eids: set[str] = set()
        self.practice_seq = 0
        self.practice_started = False

    def sync_once(self) -> int:
        if not DB_PATH.is_file():
            return 0
        # Remove old monolithic session if present
        old_mono = TRACES_ROOT / 'ses_qwen_live'
        if old_mono.is_dir():
            shutil.rmtree(old_mono, ignore_errors=True)

        conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        total_written = 0

        # 1. Split & Inspect (lightweight)
        split_row = conn.execute(
            "SELECT created_at, payload_json FROM objects WHERE record_type='split_manifest' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if split_row and self.seed_count == -1:
            sp = json.loads(split_row['payload_json'])
            evs = [
                {'at': split_row['created_at'], 'command': 'split', 'kind': 'command_start',
                 'data': {'command': 'split', 'args': {'overwrite': True}}},
                {'at': split_row['created_at'], 'command': 'split', 'kind': 'summary',
                 'data': {'result': {
                     'manifest_hash': sp.get('manifest_hash'),
                     'counts': sp.get('counts', {}),
                     'feasibility_pilot': sp.get('feasibility_pilot', False),
                     'groups': len(sp.get('partition_of_group', {})),
                 }}},
            ]
            total_written += _write_session_events('ses_qwen_1_split', evs)

        # 2. Seed Memory (only rewrite when seed count changes)
        cur_seed_cnt = conn.execute("SELECT COUNT(*) FROM experiences WHERE phase='seed'").fetchone()[0]
        if cur_seed_cnt != self.seed_count and cur_seed_cnt > 0:
            self.seed_count = cur_seed_cnt
            seed_rows = conn.execute(
                "SELECT e.experience_id, e.group_id, e.eligible, o.created_at, o.payload_json "
                "FROM experiences e JOIN objects o ON e.experience_id=o.record_id "
                "WHERE e.phase='seed' ORDER BY o.created_at, e.experience_id"
            ).fetchall()
            seed_evs: list[dict] = [{
                'at': seed_rows[0]['created_at'], 'command': 'build-seed-memory', 'kind': 'command_start',
                'data': {'command': 'build-seed-memory', 'args': {'release': 'M0', 'overwrite': True}},
            }]
            seed_eids = [r['experience_id'] for r in seed_rows]
            parent_links: dict[str, list[str]] = {eid: [] for eid in seed_eids}
            for i in range(0, len(seed_eids), 400):
                chunk = seed_eids[i:i + 400]
                for pr in conn.execute(
                    f"SELECT record_id, parent_id FROM object_parents WHERE record_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ):
                    parent_links[pr['record_id']].append(pr['parent_id'])
            objs = _load_objects_map(conn, sorted({pid for pids in parent_links.values() for pid in pids}))

            for r in seed_rows:
                eid = r['experience_id']
                at = r['created_at']
                pl = json.loads(r['payload_json'])
                rec = pl.get('record', {})
                ret = pl.get('retrieval', {})
                pair_id = rec.get('pair_id')
                p_objs = [objs[pid] for pid in parent_links.get(eid, []) if pid in objs]
                prof = next((x['payload'] for x in p_objs if x['record_type'] == 'source_profile'), None)
                pana = next((x['payload'] for x in p_objs if x['record_type'] == 'pair_analysis'), None)
                qrep = next((x['payload'] for x in p_objs if x['record_type'] == 'evaluation_report'), None)

                seed_evs.append({'pair_id': pair_id, 'phase': 'seed', 'at': at, 'command': 'build-seed-memory',
                                 'kind': 'pair_start', 'data': {'group_id': r['group_id'], 'source_text': ret.get('source_excerpt', '')}})
                seed_evs.append({'pair_id': pair_id, 'phase': 'seed', 'at': at, 'command': 'build-seed-memory',
                                 'kind': 'source', 'data': {'text': ret.get('source_excerpt', ''), 'windows': 1}})
                if prof and prof.get('payload'):
                    seed_evs.append({'pair_id': pair_id, 'phase': 'seed', 'at': at, 'command': 'build-seed-memory',
                                     'kind': 'llm_call', 'data': {
                                         'role': 'A3', 'mode': 'analyse_source', 'model': {'provider': 'ollama', 'model': 'qwen3.8'},
                                         'input_tokens': 0, 'output_tokens': 0, 'seconds': 0, 'output': prof['payload']}})
                if pana and pana.get('payload'):
                    seed_evs.append({'pair_id': pair_id, 'phase': 'seed', 'at': at, 'command': 'build-seed-memory',
                                     'kind': 'llm_call', 'data': {
                                         'role': 'A1', 'mode': 'analyse_pair', 'model': {'provider': 'ollama', 'model': 'qwen3.8'},
                                         'input_tokens': 0, 'output_tokens': 0, 'seconds': 0, 'output': pana['payload']}})
                if qrep and qrep.get('payload'):
                    seed_evs.append({'pair_id': pair_id, 'phase': 'seed', 'at': at, 'command': 'build-seed-memory',
                                     'kind': 'llm_call', 'data': {
                                         'role': 'A5', 'mode': 'reference_quality', 'model': {'provider': 'ollama', 'model': 'qwen3.8'},
                                         'input_tokens': 0, 'output_tokens': 0, 'seconds': 0, 'output': qrep['payload']}})
                seed_evs.append({'pair_id': pair_id, 'phase': 'seed', 'at': at, 'command': 'build-seed-memory',
                                 'kind': 'admission', 'data': {
                                     'experience_id': eid, 'eligible': bool(r['eligible']),
                                     'reasons': rec.get('exclusion_reasons', []), 'record': rec, 'retrieval_meta': ret}})
            total_written += _write_session_events('ses_qwen_2_seed', seed_evs)

        # 3. Insights & Inspect Memory (only rewrite when proposal count changes)
        cur_prop_cnt = conn.execute("SELECT COUNT(*) FROM objects WHERE record_type='insight_proposal'").fetchone()[0]
        if cur_prop_cnt != self.proposal_count and cur_prop_cnt > 0:
            self.proposal_count = cur_prop_cnt
            proposals = conn.execute(
                "SELECT record_id, run_id, created_at, payload_json FROM objects WHERE record_type='insight_proposal' ORDER BY created_at"
            ).fetchall()
            rejected_by_cmp: dict[str, list[dict]] = {}
            for rj in conn.execute("SELECT payload_json FROM objects WHERE record_type='rejected_proposal'"):
                rj_pl = json.loads(rj['payload_json'])
                reason = rj_pl.get('reason', 'rejected')
                if 'already added' in reason or 'already committed' in reason:
                    continue
                cid = rj_pl.get('comparison_id', '')
                op = rj_pl.get('operation', {})
                rejected_by_cmp.setdefault(cid, []).append({
                    'operation_id': op.get('operation_id', 'op1'),
                    'reason': reason,
                })
            ins_evs: list[dict] = []
            # Map experience_id -> pair_id so Insights tab can group by pair
            for er in conn.execute(
                "SELECT e.experience_id, e.task_id, e.phase, o.created_at, o.payload_json "
                "FROM experiences e JOIN objects o ON e.experience_id=o.record_id"
            ):
                pl = json.loads(er['payload_json'])
                pid = er['task_id'] or pl.get('record', {}).get('pair_id')
                if pid:
                    ins_evs.append({
                        'pair_id': pid, 'phase': er['phase'], 'at': er['created_at'], 'command': 'update-insights',
                        'kind': 'admission' if er['phase'] == 'seed' else 'experience',
                        'data': {'experience_id': er['experience_id'], 'eligible': True},
                    })
            # Query actual committed insight events from SQLite to preserve insight_id, version, importance
            events_by_cmp: dict[str, list[dict]] = {}
            for ev in conn.execute("SELECT event_id, comparison_id, insight_id, operation, to_version FROM insight_events"):
                events_by_cmp.setdefault(ev['comparison_id'], []).append(ev)

            imp_by_iv: dict[tuple[str, int], int] = {}
            for iv in conn.execute("SELECT insight_id, version, importance FROM insight_versions"):
                imp_by_iv[(iv['insight_id'], iv['version'])] = iv['importance']

            for pr in proposals:
                prop_pl = json.loads(pr['payload_json'])
                cid = prop_pl.get('comparison_id', '')
                ops = prop_pl.get('operations', [])
                rej_map = {x['operation_id']: x['reason'] for x in rejected_by_cmp.get(cid, [])}
                committed_ops = []
                cmp_evs = list(events_by_cmp.get(cid, []))
                for op in ops:
                    op_id = op.get('operation_id', 'op1')
                    if op_id in rej_map:
                        continue
                    ev_match = None
                    if op.get('target_insight_id'):
                        ev_match = next((e for e in cmp_evs if e['insight_id'] == op.get('target_insight_id')), None)
                    if not ev_match:
                        ev_match = next((e for e in cmp_evs if e['operation'] == op.get('operation')), None)
                    if ev_match:
                        cmp_evs.remove(ev_match)
                        iid = ev_match['insight_id']
                        ver = ev_match['to_version']
                        imp = imp_by_iv.get((iid, ver), 2)
                        committed_ops.append({
                            'operation_id': op_id,
                            'insight_id': iid,
                            'version': ver,
                            'importance': imp,
                            'status': 'active',
                            'event_id': ev_match['event_id'],
                        })
                    else:
                        committed_ops.append({'operation_id': op_id, 'status': 'active'})
                ins_evs.append({
                    'phase': 'insights', 'comparison_id': cid, 'at': pr['created_at'],
                    'command': 'update-insights', 'kind': 'insight_batch',
                    'data': {'proposal': prop_pl, 'committed': committed_ops, 'rejected': rejected_by_cmp.get(cid, [])},
                })
            total_written += _write_session_events('ses_qwen_4_insights', ins_evs)

            # Inspect Memory
            rel_rows = conn.execute("SELECT release_id, manifest_record_id, published_at FROM releases ORDER BY published_at DESC").fetchall()
            if rel_rows:
                latest_rel = rel_rows[0]
                m_obj = _load_objects_map(conn, [latest_rel['manifest_record_id']]).get(latest_rel['manifest_record_id'])
                if m_obj:
                    m_pl = m_obj['payload']
                    ins_heads = conn.execute(
                        "SELECT iv.insight_id, iv.version, iv.importance, iv.status, o.payload_json "
                        "FROM insight_heads ih JOIN insight_versions iv ON ih.insight_id=iv.insight_id AND ih.version=iv.version "
                        "JOIN objects o ON iv.record_id=o.record_id ORDER BY iv.importance DESC"
                    ).fetchall()
                    ins_summary = []
                    for ih in ins_heads:
                        ipl = json.loads(ih['payload_json'])
                        ins_summary.append({
                            'insight_id': ih['insight_id'],
                            'version': ih['version'],
                            'importance': ih['importance'],
                            'status': ih['status'],
                            'lesson': (ipl.get('content') or {}).get('lesson', ''),
                            'support_groups': ipl.get('support_group_ids', []),
                        })
                    insp_evs = [
                        {'at': latest_rel['published_at'], 'command': 'inspect-memory', 'kind': 'command_start',
                         'data': {'command': 'inspect-memory', 'args': {'release': latest_rel['release_id']}}},
                        {'at': latest_rel['published_at'], 'command': 'inspect-memory', 'kind': 'summary',
                         'data': {'result': {
                             'release': {
                                 'release_id': latest_rel['release_id'],
                                 'demonstrations': len(m_pl.get('experiences', [])),
                                 'insights': len(ins_summary),
                             },
                             'insights': ins_summary,
                         }}},
                    ]
                    total_written += _write_session_events('ses_qwen_5_inspect', insp_evs)

        # 4. Practice Loop (incremental append for newly completed attempts)
        prac_dir = TRACES_ROOT / 'ses_qwen_3_practice'
        prac_dir.mkdir(parents=True, exist_ok=True)
        (prac_dir / 'prompts').mkdir(parents=True, exist_ok=True)
        prac_file = prac_dir / 'trace.jsonl'
        if not self.seen_practice_eids and prac_file.is_file():
            prac_file.unlink(missing_ok=True)

        prac_rows = conn.execute(
            "SELECT e.experience_id, e.group_id, e.task_id, e.outcome, e.eligible, o.created_at, o.payload_json "
            "FROM experiences e JOIN objects o ON e.experience_id=o.record_id "
            "WHERE e.phase='practice' ORDER BY o.created_at, e.experience_id"
        ).fetchall()
        new_rows = [r for r in prac_rows if r['experience_id'] not in self.seen_practice_eids]
        if new_rows:
            new_eids = [r['experience_id'] for r in new_rows]
            p_links: dict[str, list[str]] = {eid: [] for eid in new_eids}
            for i in range(0, len(new_eids), 400):
                chunk = new_eids[i:i + 400]
                for pr in conn.execute(
                    f"SELECT record_id, parent_id FROM object_parents WHERE record_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ):
                    p_links[pr['record_id']].append(pr['parent_id'])
            p_objs_map = _load_objects_map(conn, sorted({pid for pids in p_links.values() for pid in pids}))

            cmp_cache: dict[str, dict] = {}
            for run_sub in RUNS_DIR.iterdir() if RUNS_DIR.is_dir() else []:
                if run_sub.is_dir():
                    for cf in run_sub.glob('comparison_*.json'):
                        try:
                            cd = json.loads(cf.read_text('utf-8'))
                            if cd.get('report_id'):
                                cmp_cache[cd['report_id']] = cd
                            if cd.get('subject_id'):
                                cmp_cache[cd['subject_id']] = cd
                        except Exception:
                            pass

            append_evs: list[dict] = []
            if not self.practice_started:
                self.practice_started = True
                append_evs.append({
                    'at': new_rows[0]['created_at'], 'command': 'collect-practice', 'kind': 'command_start',
                    'data': {'command': 'collect-practice', 'args': {'release': 'M0'}},
                })

            for r in new_rows:
                self.seen_practice_eids.add(r['experience_id'])
                eid = r['experience_id']
                at = r['created_at']
                pl = json.loads(r['payload_json'])
                rec = pl.get('record', {})
                ret = pl.get('retrieval', {})
                pair_id = r['task_id']
                att_num = rec.get('attempt_number', 1)
                p_objs = [p_objs_map[pid] for pid in p_links.get(eid, []) if pid in p_objs_map]
                prof = next((x['payload'] for x in p_objs if x['record_type'] == 'source_profile'), None)
                retr = next((x['payload'] for x in p_objs if x['record_type'] == 'retrieval_snapshot'), None)
                plan = next((x['payload'] for x in p_objs if x['record_type'] == 'adaptation_plan'), None)
                cand = next((x['payload'] for x in p_objs if x['record_type'] == 'arabic_candidate'), None)
                evals = [x['payload'] for x in p_objs if x['record_type'] == 'evaluation_report']

                if att_num == 1:
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'at': at, 'command': 'collect-practice',
                        'kind': 'pair_start', 'data': {'group_id': r['group_id'], 'source_text': ret.get('source_excerpt', ''), 'release': 'M0'}
                    })

                append_evs.append({
                    'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                    'kind': 'attempt_start', 'data': {'attempt_id': f'att_{pair_id}_{att_num}', 'prior_attempt_id': rec.get('previous_attempt_id')}
                })
                append_evs.append({
                    'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                    'kind': 'source', 'data': {'text': ret.get('source_excerpt', ''), 'windows': 1, 'release': 'M0'}
                })
                if prof and prof.get('payload'):
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                        'kind': 'llm_call', 'data': {
                            'role': 'A3', 'mode': 'analyse_source', 'model': {'provider': 'ollama', 'model': 'qwen3.8'},
                            'input_tokens': 0, 'output_tokens': 0, 'seconds': 0, 'output': prof['payload']}
                    })
                if retr:
                    ex_list = [{
                        'experience_id': e.get('experience_id'),
                        'source_excerpt': e.get('source_excerpt', ''),
                        'arabic_excerpt': ' '.join(b.get('text', '') for b in e['arabic_text'].get('blocks', []))
                        if isinstance(e.get('arabic_text'), dict) else '',
                    } for e in retr.get('experiences', [])]
                    ins_list = [{
                        'insight_id': i.get('insight_id'),
                        'lesson': (i.get('content') or {}).get('lesson', ''),
                    } for i in retr.get('insights', [])]
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                        'kind': 'retrieval', 'data': {'experiences': ex_list, 'insights': ins_list}
                    })
                fb_rec = rec.get('feedback_received')
                if plan and plan.get('payload'):
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                        'kind': 'llm_call', 'data': {
                            'role': 'A3', 'mode': 'make_plan', 'model': {'provider': 'ollama', 'model': 'qwen3.8'},
                            'input_tokens': 0, 'output_tokens': 0, 'seconds': 0,
                            'input': {'feedback': [fb_rec]} if fb_rec else {},
                            'output': plan['payload']}
                    })
                if cand:
                    ar_text = cand.get('arabic_text') or (cand.get('arabic') or {}).get('text', '')
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                        'kind': 'candidate', 'data': {
                            'candidate_id': cand.get('candidate_id'),
                            'arabic_text': ar_text,
                            'realizations': (cand.get('payload') or {}).get('realizations', []),
                            'unresolved_issues': (cand.get('payload') or {}).get('unresolved_issues', []),
                        }
                    })
                for ev_rep in evals:
                    if ev_rep.get('mode') == 'candidate_quality' and ev_rep.get('payload'):
                        append_evs.append({
                            'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                            'kind': 'evaluation', 'data': {'critical': ev_rep.get('critical', False), 'payload': ev_rep['payload']}
                        })
                cmp_rep = cmp_cache.get(rec.get('comparison_report_id')) or cmp_cache.get(rec.get('candidate_id'))
                if cmp_rep and cmp_rep.get('payload'):
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                        'kind': 'reference_comparison', 'data': {
                            'report_id': cmp_rep.get('report_id'),
                            'result': cmp_rep['payload'].get('result'),
                            'payload': cmp_rep['payload']}
                    })
                append_evs.append({
                    'pair_id': pair_id, 'phase': 'practice', 'attempt': att_num, 'at': at, 'command': 'collect-practice',
                    'kind': 'attempt_end', 'data': {
                        'attempt_id': f'att_{pair_id}_{att_num}',
                        'outcome': r['outcome'],
                        'stop': r['outcome'] == 'success',
                        'issue_codes': rec.get('issue_codes', [])}
                })
                if r['outcome'] == 'success' or att_num >= 10 or r['outcome'] == 'error':
                    append_evs.append({
                        'pair_id': pair_id, 'phase': 'practice', 'at': at, 'command': 'collect-practice',
                        'kind': 'pair_end', 'data': {
                            'success': r['outcome'] == 'success',
                            'stop_reason': 'success' if r['outcome'] == 'success' else ('error' if r['outcome'] == 'error' else 'attempt_limit'),
                            'attempts': att_num}
                    })

            with prac_file.open('a', encoding='utf-8') as fh:
                for ev in append_evs:
                    self.practice_seq += 1
                    ev['seq'] = self.practice_seq
                    fh.write(json.dumps(ev, ensure_ascii=False) + '\n')
            total_written += len(append_evs)

        conn.close()
        return total_written


def main():
    loop = '--loop' in sys.argv
    syncer = TraceSyncer()
    while True:
        n = syncer.sync_once()
        if not loop:
            print(f'Synced {n} trace events across sessions in {TRACES_ROOT}')
            break
        time.sleep(5)


if __name__ == '__main__':
    main()
