"""`online`: the sequential learning loop. For every practice sample, in order:

  Step 3  collect-practice   practise the sample against the CURRENT memory release
  Step 4  update-insights    admit its reference, extract insights (A2 -> A5 -> controller) and republish the
                             rolling release, now containing everything learned so far
  Step 5  inspect-memory     record what the release contains after this sample

The next sample therefore retrieves the demonstrations and insights learned from all earlier samples (ExpeL's
online experience gathering / insight extraction, one task at a time). Only the `learning` partition is touched;
development and test stay blind until the evaluate step.

The loop is resumable: a sample whose reference was already admitted into the rolling release (or judged
ineligible) is skipped, and the loop continues from the rolling release instead of the base release.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .. import trace
from ..errors import ExpectedPipelineError, GlocalInductError
from ..services import Services
from .consolidate import update_insights
from .ingest import current_split
from .practice import collect_practice

MAX_CONSECUTIVE_FAILURES = 5


def _log(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def practice_pair_ids(services: Services) -> list[str]:
    split = current_split(services)
    subsets = {'practice'} if services.config.learning.reference_demonstrations else {'practice', 'seed'}
    return [pid for pid in sorted(split.pair_group)
            if split.partition_of_pair(pid) == 'learning' and split.subset_of_pair(pid) in subsets]


def completed_pairs(services: Services, release_id: str) -> set[str]:
    """Pairs whose Step 4 already finished: their practice reference is in the rolling release or was ineligible."""
    rel = services.releases.get(release_id)
    demos = set(rel.demonstration_ids) if rel else set()
    done: set[str] = set()
    for v in services.experiences.all():
        if v.kind == 'reference_example' and v.phase == 'practice':
            if (not v.eligible) or v.experience_id in demos:
                done.add(v.record.get('pair_id'))
    return done


def inspect_release(services: Services, release_id: str, previous: set[tuple[str, int]]) -> dict:
    """Step 5: compact snapshot of a release (counts + every insight with its importance)."""
    rel = services.resolve_release(release_id)
    insights = []
    for iid, ver in rel.insight_versions:
        rec = services.insights.version(iid, ver)
        insights.append({'insight_id': iid, 'version': ver, 'importance': rec.importance, 'status': rec.status,
                         'lesson': rec.content.lesson, 'support_groups': rec.support_group_ids,
                         'support_domains': rec.support_domains, 'origin': rec.content.origin})
    insights.sort(key=lambda r: (-r['importance'], r['insight_id']))
    keys = {(i['insight_id'], i['version']) for i in insights}
    return {'release': rel.summary(), 'insights': insights, 'keys': keys,
            'changed_insights': len(keys - previous)}


async def online_learning(services: Services, base_release: str = 'M0', release_id: str = 'M1',
                          limit: int | None = None, pair_ids: list[str] | None = None,
                          resume: bool = True) -> dict:
    base = services.resolve_release(base_release)          # raises KeyError when the base release is missing
    wanted = list(pair_ids) if pair_ids else practice_pair_ids(services)
    current = base.release_id
    done: set[str] = set()
    if resume and services.releases.get(release_id) is not None:
        current = release_id
        done = completed_pairs(services, release_id)
        _log(event='resume', rolling_release=release_id, completed_pairs=len(done))
    todo = [p for p in wanted if p not in done]
    if limit is not None and limit > 0:
        todo = todo[:limit]
    _log(event='start', base=base.release_id, rolling_release=release_id, start_from=current,
         pending_samples=len(todo), skipped_completed=len(wanted) - len([p for p in wanted if p not in done]))

    reports = services.config.path('reports'); reports.mkdir(parents=True, exist_ok=True)
    progress_file = reports / f'online_{release_id}_progress.jsonl'
    prev_keys: set[tuple[str, int]] = set()
    try:
        prev_keys = {(i, v) for i, v in services.resolve_release(current).insight_versions}
    except Exception:
        pass

    samples: list[dict] = []
    failures = 0
    for k, pid in enumerate(todo, 1):
        started = time.time()
        rel_before = services.resolve_release(current)
        entry = {'sample': k, 'of': len(todo), 'pair_id': pid, 'release_used': current,
                 'release_hash': rel_before.release_hash[:12],
                 'insights_available': len(rel_before.insight_versions),
                 'demonstrations_available': len(rel_before.demonstration_ids)}
        _log(event='sample_start', **entry)
        # ---- Step 3: practice against the current release
        try:
            trace.set_command('collect-practice')
            prac = await collect_practice(services, current, pair_id=pid)
            t = prac['detail'][0] if prac['detail'] else {}
            entry.update(practice_success=bool(t.get('success')), attempts=len(t.get('attempts', [])),
                         stop_reason=t.get('stop_reason'))
        except sqlite3.IntegrityError as e:     # an identical attempt was stored by an interrupted earlier run
            entry.update(practice_note=f'attempt already stored: {e}')
        except (ExpectedPipelineError, GlocalInductError) as e:
            entry.update(practice_error=f'{e.code}: {e}')
        # ---- Step 4: learn from it and republish the rolling release
        try:
            trace.set_command('update-insights')
            upd = await update_insights(services, current, release_id, overwrite=False, overwrite_release=True,
                                        pair_id=pid)
            ins = upd.get('insights', {})
            entry.update(insights_committed=ins.get('committed', 0), insights_rejected=ins.get('rejected', 0),
                         new_references=upd.get('new_references', 0), successful_attempts=upd.get('successful_attempts', 0))
            current = release_id        # the NEXT sample practises against the memory learned so far
            failures = 0
        except (ExpectedPipelineError, GlocalInductError) as e:
            entry.update(update_error=f'{e.code}: {e}')
            failures += 1
        except sqlite3.IntegrityError as e:
            entry.update(update_error=f'IntegrityError: {e}')
            failures += 1
        # ---- Step 5: inspect the release the next sample will use
        try:
            trace.set_command('inspect-memory')
            snap = inspect_release(services, current, prev_keys)
            prev_keys = snap.pop('keys')
            entry.update(release_after=current, insights_in_release=len(snap['insights']),
                         demonstrations_in_release=snap['release']['demonstrations'],
                         insights_changed=snap['changed_insights'])
            trace.emit('summary', result={**snap, 'sample': {'index': k, 'of': len(todo), 'pair_id': pid}})
        except Exception as e:                  # inspection must never stop the learning loop
            entry.update(inspect_error=f'{type(e).__name__}: {e}')
        entry['seconds'] = round(time.time() - started, 1)
        samples.append(entry)
        with progress_file.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        _log(event='sample_done', **entry)
        if failures >= MAX_CONSECUTIVE_FAILURES:
            _log(event='aborted', reason=f'{failures} consecutive Step 4 failures')
            break

    trace.set_command('online')
    final = services.resolve_release(current)
    return {'base': base.release_id, 'rolling_release': release_id, 'final_release': final.summary(),
            'samples_processed': len(samples),
            'practice_successes': sum(bool(s.get('practice_success')) for s in samples),
            'attempts': sum(s.get('attempts', 0) for s in samples),
            'insights_committed': sum(s.get('insights_committed', 0) for s in samples),
            'insights_rejected': sum(s.get('insights_rejected', 0) for s in samples),
            'errors': sum(1 for s in samples if any(k.endswith('_error') for k in s)),
            'progress_file': str(progress_file), 'last_samples': samples[-20:]}

