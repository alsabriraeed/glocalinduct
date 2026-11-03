"""Inference prototype: paste or upload English, set context, click Glocalize.

FastAPI app exposing `infer` (one semantic attempt, frozen release). Deployment
requests are a quarantine audit stream: nothing here writes to memory.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import load_config, resolve_model_preset
from .. import trace as tracing
from ..errors import GlocalInductError
from ..memory.store import SCHEMA_SQL
from ..providers.base import make_provider
from ..services import Services, build_services
from ..workflows import InferRequest, infer
from ..workflows.common import build_source_document, RunStore
from ..workflows.infer import compute_profile
from ..agents.runner import UsageMeter

STATIC = Path(__file__).resolve().parent / 'static'


class WorkflowRunner:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.running: bool = False
        self.action: str | None = None
        self.action_title: str | None = None
        self.logs: list[str] = []
        self.exit_code: int | None = None
        self.started_at: float | None = None
        self.task: asyncio.Task | None = None
        self.trace_session: str | None = None
        self.model_override: str | None = None

    async def run(self, action: str, title: str, commands: list[list[str]]):
        if self.running:
            raise HTTPException(400, f"Workflow '{self.action_title}' is already running. Please wait or cancel it.")
        self.running = True
        self.action = action
        self.action_title = title
        self.trace_session = f"ses_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        self.logs = [f"=== [Started] {title} at {time.strftime('%Y-%m-%d %H:%M:%S')} ==="]
        self.exit_code = None
        self.started_at = time.time()
        self.task = asyncio.create_task(self._execute_pipeline(commands))

    async def _execute_pipeline(self, commands: list[list[str]]):
        try:
            for i, cmd in enumerate(commands, 1):
                cli_prefix = ["--model", self.model_override] if self.model_override else []
                cmd_str = ' '.join(cli_prefix + cmd)
                self.logs.append(f"\n>>> Running Step {i}/{len(commands)}: glocalinduct {cmd_str}")
                full_cmd = [sys.executable, "-m", "glocalinduct.cli"] + cli_prefix + cmd
                self.process = await asyncio.create_subprocess_exec(
                    *full_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd="/app",
                    env={**os.environ, 'GLOCALINDUCT_TRACE_SESSION': self.trace_session or ''}
                )
                while True:
                    line = await self.process.stdout.readline()
                    if not line:
                        break
                    text = line.decode('utf-8', errors='replace').rstrip('\r\n')
                    self.logs.append(text)
                    if len(self.logs) > 2500:
                        self.logs = self.logs[-2500:]
                await self.process.wait()
                if self.process.returncode != 0:
                    self.exit_code = self.process.returncode
                    self.logs.append(f"✖ Step {i} failed with return code {self.process.returncode}")
                    return
                self.logs.append(f"✔ Step {i} finished successfully.")

            self.exit_code = 0
            self.logs.append(f"\n=== [Completed] {self.action_title} in {round(time.time() - self.started_at, 2)}s ===")
        except asyncio.CancelledError:
            self.logs.append("\n⚠ [Cancelled] Workflow was terminated by user.")
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            self.exit_code = -1
        except Exception as e:
            self.logs.append(f"\n✖ [Fatal Error] {str(e)}")
            self.exit_code = 1
        finally:
            self.running = False
            self.process = None

    def cancel(self):
        if self.running:
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            if self.task and not self.task.done():
                self.task.cancel()
            self.running = False


class WorkflowRunReq(BaseModel):
    action: str
    release: str | None = None
    base: str | None = None
    limit: int | None = None
    pair_id: str | None = None


def _slim(outcome) -> dict[str, Any]:
    d = outcome.to_dict()
    a = outcome.attempt
    src = d.get('source') or {}
    blocks = src.get('normalized', {}).get('blocks', []) if src else []
    cand = {b['source_block_id']: b['text_ar'] for b in (d['candidate']['blocks'] if d.get('candidate') else [])}
    items = (d.get('profile') or {}).get('items', [])
    decisions = (d.get('plan') or {}).get('payload', {}).get('decisions', []) if d.get('plan') else []
    dec_by_item = {x['item_id']: x for x in decisions}
    item_rows = []
    for it in items:
        dec = dec_by_item.get(it['item_id'], {})
        item_rows.append({'item_id': it['item_id'], 'category': it['category'], 'quote': it['source_quotes'][0]['quote'],
                          'block_id': it['source_quotes'][0]['block_id'], 'function': it['semantic_function'],
                          'essentiality': it['essentiality'], 'factual_role': it['factual_role'],
                          'strategy': dec.get('strategy'), 'counterpart_ar': dec.get('counterpart_ar'),
                          'term_policy': dec.get('term_policy'), 'fallback_reason': dec.get('fallback_reason'),
                          'rationale': dec.get('rationale'),
                          'support': [{'experience_id': s['experience_id'], 'quote': s['arabic_quote']['quote'],
                                       'function': s['compatible_function']} for s in dec.get('support', [])],
                          'insights': dec.get('insight_refs', [])})
    retrieval = d.get('retrieval') or {}
    memory = {'release': d['release'],
              'experiences': [{'experience_id': e['experience_id'], 'kind': e['kind'], 'group_id': e['group_id'],
                               'similarity': e['similarity'], 'domains': e['domains'], 'scope': e['scope'],
                               'source_excerpt': e['source_excerpt'][:300],
                               'arabic_excerpt': ' '.join(b['text'] for b in e['arabic_text']['blocks'])[:300]}
                              for e in retrieval.get('experiences', [])],
              'insights': [{'insight_id': i['insight_id'], 'version': i['version'], 'importance': i['importance'],
                            'similarity': i['similarity'], 'lesson': i['content']['lesson'],
                            'applies_when': i['content']['applies_when'], 'scope': i['content']['scope']}
                           for i in retrieval.get('insights', [])],
              'excluded_groups': retrieval.get('excluded_group_ids', []),
              'filter_decisions': retrieval.get('filter_decisions', [])[:40]}
    ev = d.get('evaluation')
    return {'run_id': d['run_id'], 'result': d['result'], 'error': d.get('error'), 'usage': d['usage'],
            'blocks': [{'block_id': b['block_id'], 'kind': b['kind'], 'source': b['text'], 'arabic': cand.get(b['block_id'])}
                       for b in blocks],
            'profile': {'purpose': (d.get('profile') or {}).get('purpose'), 'purpose_basis': (d.get('profile') or {}).get('purpose_basis'),
                        'domains': (d.get('profile') or {}).get('domains', []), 'genre': (d.get('profile') or {}).get('genre'),
                        'audience': (d.get('profile') or {}).get('audience'),
                        'requirements': (d.get('profile') or {}).get('requirements', []),
                        'uncertainties': (d.get('profile') or {}).get('uncertainties', [])} if d.get('profile') else None,
            'items': item_rows,
            'plan': {'plan_id': d['plan']['plan_id'], 'plan_hash': d['plan']['plan_hash'], 'version': d['plan']['version'],
                     'terminology': d['plan']['payload']['terminology'], 'reflection': d['plan']['payload']['reflection'],
                     'unresolved_issues': d['plan']['payload']['unresolved_issues']} if d.get('plan') else None,
            'evaluation': {'criteria': [{'criterion': c['criterion'], 'status': c['status'], 'score': c['score'],
                                         'explanation': c['explanation'],
                                         'findings': [{'issue_code': f['issue_code'], 'severity': f['severity'],
                                                       'explanation': f['explanation'], 'correction_goal': f['correction_goal'],
                                                       'affected_ids': f['affected_ids']} for f in c['findings']]}
                                        for c in ev['criteria']],
                           'reference_comparison': ev.get('reference_comparison')} if ev else None,
            'memory': memory, 'windows': len(src.get('windows', [])) if src else 0,
            'extraction_warnings': src.get('extraction_warnings', []) if src else []}


class ModelSwitchReq(BaseModel):
    model: str


class WorkspaceClearReq(BaseModel):
    scope: str = 'memory'   # 'traces' | 'memory' | 'all' | 'release'
    release_id: str | None = None


def create_app(config_path: str | None = None, provider: str | None = None, services: Services | None = None,
               model: str | None = None) -> FastAPI:
    app = FastAPI(title='GlocalInduct prototype', version='0.1.0')
    if services is None:
        rt: dict[str, Any] = {}
        if model:
            rt.update(resolve_model_preset(model))
        if provider:
            if not rt and provider in ('ollama', 'openai'):
                rt.update(resolve_model_preset(provider))
            rt['provider'] = provider
        overrides = {'runtime': rt} if rt else None
        cfg = load_config(config_path, overrides)
        cfg.assert_real_pipeline()
        services = build_services(cfg)
    app.state.services = services

    @app.get('/')
    def index():
        return FileResponse(STATIC / 'index.html', headers={'Cache-Control': 'no-cache, must-revalidate'})

    @app.get('/api/status')
    def status():
        s: Services = app.state.services
        active = s.releases.active()
        rels = s.releases.list()
        if not rels:
            s.ensure_empty_release(); rels = s.releases.list()
        return {'provider': s.provider.model_identity(), 'evaluation_provider': s.evaluation_provider.model_identity(),
                'available_models': [
                    {'id': 'gpt-4o', 'provider': 'openai', 'model': 'gpt-4o', 'label': 'openai / gpt-4o'},
                    {'id': 'qwen3.8', 'provider': 'ollama', 'model': 'qwen3.8', 'label': 'ollama / qwen3.8'},
                ],
                'releases': [r['release_id'] for r in rels], 'active_release': active.release_id if active else None,
                'demo_mode': s.provider.name == 'fake',
                'domains': s.policies.known_domains(), 'windows': s.config.windows.model_dump()}

    @app.post('/api/model')
    def switch_model(req: ModelSwitchReq):
        s: Services = app.state.services
        try:
            preset = resolve_model_preset(req.model)
            new_rt = s.config.runtime.model_copy(update=preset)
            new_cfg = s.config.model_copy(update={'runtime': new_rt})
            new_prov = make_provider(new_cfg, 'generation')
            new_eval_prov = make_provider(new_cfg, 'evaluation')
            s.config = new_cfg
            s.provider = new_prov
            s.evaluation_provider = new_eval_prov
            s.runner.provider = new_prov
            s.runner.evaluation_provider = new_eval_prov
            runner.model_override = req.model
            return {'status': 'ok', 'provider': s.provider.model_identity(),
                    'evaluation_provider': s.evaluation_provider.model_identity()}
        except Exception as e:
            raise HTTPException(400, f'Failed to switch model: {e}')

    @app.post('/api/glocalize')
    async def glocalize(text: str | None = Form(None), file: UploadFile | None = File(None),
                        context: str | None = Form('{}'), release: str | None = Form(None)):
        s: Services = app.state.services
        try:
            ctx = json.loads(context or '{}')
        except json.JSONDecodeError:
            raise HTTPException(400, 'context must be JSON')
        tmp_path = None
        try:
            if file is not None and file.filename:
                suffix = Path(file.filename).suffix.lower()
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix='upload_') as fh:
                    fh.write(await file.read())
                    tmp_path = fh.name
                req = InferRequest(file_path=tmp_path, context=ctx)
            else:
                req = InferRequest(text=text or '', context=ctx)
            try:
                outcome = await infer(req, s, release or None, evaluate=False)
            except KeyError as e:
                raise HTTPException(404, str(e))
            payload = _slim(outcome)
            if tmp_path and payload.get('result'):
                payload['uploaded_file'] = file.filename
            return JSONResponse(payload)
        except GlocalInductError as e:
            return JSONResponse({'error': {'code': e.code, 'message': str(e)}}, status_code=400)
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    @app.post('/api/discover-context')
    async def discover_context(text: str | None = Form(None), file: UploadFile | None = File(None)):
        s: Services = app.state.services
        tmp_path = None
        try:
            if file is not None and file.filename:
                suffix = Path(file.filename).suffix.lower()
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix='upload_') as fh:
                    fh.write(await file.read())
                    tmp_path = fh.name
                source = build_source_document(file_path=tmp_path, context={},
                                               content_tokens=s.config.windows.content_tokens,
                                               context_tokens=s.config.windows.context_tokens_each_side)
            else:
                raw_text = (text or '').strip()
                if not raw_text:
                    raise HTTPException(400, 'Please enter English text or upload a file first.')
                source = build_source_document(text=raw_text, context={},
                                               content_tokens=s.config.windows.content_tokens,
                                               context_tokens=s.config.windows.context_tokens_each_side)

            store = RunStore(s.repo, s.config.path('runs'))
            meter = UsageMeter()
            budget = s.budget(len(source.windows))
            profile = await compute_profile(source, s, store, meter, budget)

            domains = [t.domain for t in profile.payload.domains]
            return JSONResponse({
                'purpose': profile.payload.purpose,
                'purpose_basis': profile.payload.purpose_basis,
                'domains': domains,
                'genre': profile.payload.genre,
                'audience': profile.payload.audience,
                'locale_scope': 'general_arab',
                'locales': [],
                'setting': None,
                'time_period': None,
                'items_count': len(profile.payload.items),
                'items': [{'category': it.category, 'quote': it.source_quotes[0].quote,
                           'function': it.semantic_function} for it in profile.payload.items],
                'requirements_count': len(profile.payload.requirements),
            })
        except HTTPException:
            raise
        except Exception as e:
            return JSONResponse({'error': {'code': 'DISCOVERY_FAILED', 'message': str(e)}}, status_code=400)
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    @app.get('/api/releases/{release_id}')
    def release(release_id: str):
        s: Services = app.state.services
        try:
            rel = s.resolve_release(release_id)
        except KeyError:
            raise HTTPException(404, 'release not found')
        insights = [{'insight_id': i, 'version': v, **s.insights.version(i, v).model_dump(mode='json')['content']}
                    for i, v in rel.insight_versions]
        return {'release': rel.summary(), 'insights': insights, 'members': rel.manifest['experiences']}

    runner = WorkflowRunner()

    @app.get('/api/workflow/overview')
    def workflow_overview():
        s: Services = app.state.services
        split_file = Path('/app/workspace/staging/split_manifest.json')
        split_data = {}
        if split_file.exists():
            try:
                split_data = json.loads(split_file.read_text('utf-8'))
            except Exception:
                pass

        releases = []
        for r in s.releases.list():
            rel_id = r['release_id']
            try:
                rel_obj = s.releases.get(rel_id)
                smry = rel_obj.summary() if rel_obj else {}
                releases.append({
                    'release_id': rel_id,
                    'demonstrations': smry.get('demonstrations', 0),
                    'insights': smry.get('insights', 0),
                    'published_at': r.get('published_at'),
                    'is_active': bool(s.releases.active() and s.releases.active().release_id == rel_id)
                })
            except Exception:
                releases.append({'release_id': rel_id, 'is_active': False})

        pairs_cnt = 0
        try:
            rows = s.repo.query("SELECT COUNT(*) as cnt FROM objects WHERE record_type='pair_record'")
            if rows:
                pairs_cnt = rows[0]['cnt']
        except Exception:
            pass

        pair_group = split_data.get('pair_group') or {}
        part_of_group = split_data.get('partition_of_group') or {}
        sub_of_group = split_data.get('subset_of_group') or {}
        pairs_by_split: dict[str, list[str]] = {'seed': [], 'practice': [], 'development': [], 'test': [], 'all': []}
        try:
            pair_rows = s.repo.query("SELECT record_id FROM objects WHERE record_type='pair_record' ORDER BY record_id")
            all_pids = [r['record_id'].removeprefix('pair_') for r in (pair_rows or [])]
        except Exception:
            all_pids = sorted(pair_group.keys())
        def _cap_list(items: list[str], half: int = 150) -> list[str]:
            if len(items) <= half * 2:
                return items
            return items[:half] + items[-half:]

        for pid in all_pids:
            gid = pair_group.get(pid)
            part = part_of_group.get(gid)
            sub = sub_of_group.get(gid)
            if part == 'learning' and sub == 'seed':
                pairs_by_split['seed'].append(pid)
            elif part == 'learning' and sub == 'practice':
                pairs_by_split['practice'].append(pid)
            elif part == 'development':
                pairs_by_split['development'].append(pid)
            elif part == 'test':
                pairs_by_split['test'].append(pid)
        pairs_by_split['all'] = _cap_list(all_pids)
        for k in ('seed', 'practice', 'development', 'test'):
            pairs_by_split[k] = _cap_list(pairs_by_split[k])

        return {
            'pairs_total': pairs_cnt,
            'splits': split_data.get('counts', {}),
            'pairs_by_split': pairs_by_split,
            'manifest_hash': split_data.get('manifest_hash'),
            'releases': releases,
            'active_release': s.releases.active().release_id if s.releases.active() else 'M-EMPTY',
            'provider': s.provider.model_identity(),
            'running': runner.running,
            'active_action': runner.action,
            'active_title': runner.action_title
        }

    @app.get('/api/workflow/status')
    def workflow_status(from_line: int = 0):
        return {
            'running': runner.running,
            'action': runner.action,
            'title': runner.action_title,
            'started_at': runner.started_at,
            'elapsed_seconds': round(time.time() - runner.started_at, 1) if runner.started_at else 0,
            'exit_code': runner.exit_code,
            'trace_session': runner.trace_session,
            'total_lines': len(runner.logs),
            'logs': runner.logs[from_line:] if from_line < len(runner.logs) else []
        }

    # TODO(security): the trace endpoints below are unauthenticated and expose full prompts and
    # private reference Arabic. Add authentication/authorisation and rate limiting before any
    # deployment beyond a trusted localhost.
    @app.get('/api/trace/sessions')
    def trace_sessions():
        return {'sessions': tracing.list_sessions(app.state.services.config.path('traces')),
                'current': runner.trace_session}

    @app.get('/api/trace/{session}/prompt/{digest}')
    def trace_prompt(session: str, digest: str):
        text = tracing.read_prompt(app.state.services.config.path('traces'), session, digest)
        if text is None:
            raise HTTPException(404, 'not found')
        return {'hash': digest, 'text': text}

    @app.get('/api/trace/{session}')
    def trace_events(session: str, start: int = 0, limit: int = 200):
        res = tracing.read_events(app.state.services.config.path('traces'), session, start, limit)
        if res is None:
            raise HTTPException(404, 'not found')
        return res

    @app.delete('/api/releases/{release_id}')
    def delete_release(release_id: str):
        s: Services = app.state.services
        try:
            s.releases.delete(release_id)
            s.ensure_empty_release()
            return {'status': 'deleted', 'release_id': release_id}
        except Exception as e:
            raise HTTPException(400, f"Failed to delete release {release_id}: {e}")

    @app.post('/api/workspace/clear')
    def clear_workspace(req: WorkspaceClearReq):
        s: Services = app.state.services
        scope = (req.scope or 'memory').strip().lower()
        if scope not in ('traces', 'memory', 'all', 'release'):
            raise HTTPException(400, f"Unknown clear scope: {scope}")

        if runner.running:
            runner.cancel()

        def _clear_dir_contents(p: Path):
            if p.is_dir():
                for child in p.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
            p.mkdir(parents=True, exist_ok=True)

        try:
            if scope == 'release':
                if not req.release_id or req.release_id == 'M-EMPTY':
                    raise HTTPException(400, 'Please select a valid release (e.g. M0 or M1) to delete.')
                s.releases.delete(req.release_id)
                s.ensure_empty_release()
                return {'status': 'cleared', 'scope': scope, 'release_id': req.release_id}

            # Clear trace sessions and runner state for 'traces', 'memory', and 'all'
            _clear_dir_contents(s.config.path('traces'))
            runner.logs = []
            runner.trace_session = None
            runner.exit_code = None
            runner.action = None
            runner.action_title = None
            runner.started_at = None

            if scope in ('memory', 'all'):
                _clear_dir_contents(s.config.path('releases'))
                _clear_dir_contents(s.config.path('runs'))
                _clear_dir_contents(s.config.path('reports'))

                with s.repo.transaction() as c:
                    c.execute('DROP TRIGGER IF EXISTS no_delete_releases')
                    c.execute('DROP TRIGGER IF EXISTS no_update_releases')
                    c.execute('DROP TRIGGER IF EXISTS no_delete_events')
                    c.execute('DROP TRIGGER IF EXISTS no_update_events')
                    c.execute('DROP TRIGGER IF EXISTS no_delete_insights')
                    c.execute('DROP TRIGGER IF EXISTS no_update_insights')
                    c.execute('DROP TRIGGER IF EXISTS no_delete_objects')
                    c.execute('DROP TRIGGER IF EXISTS no_update_objects')

                    c.execute('DELETE FROM active_release')
                    c.execute('DELETE FROM release_experiences')
                    c.execute('DELETE FROM release_insights')
                    c.execute('DELETE FROM releases')
                    c.execute('DELETE FROM insight_events')
                    c.execute('DELETE FROM insight_evidence')
                    c.execute('DELETE FROM insight_heads')
                    c.execute('DELETE FROM insight_versions')
                    c.execute('DELETE FROM experience_support')
                    c.execute('DELETE FROM experiences')
                    c.execute('DELETE FROM run_checkpoints')

                    if scope == 'all':
                        c.execute('DELETE FROM object_parents')
                        c.execute('DELETE FROM objects')
                    else:
                        keep = "('pair_record', 'ingest_manifest', 'split_manifest')"
                        c.execute(f"DELETE FROM object_parents WHERE record_id IN (SELECT record_id FROM objects WHERE record_type NOT IN {keep}) "
                                  f"OR parent_id IN (SELECT record_id FROM objects WHERE record_type NOT IN {keep})")
                        c.execute(f"DELETE FROM objects WHERE record_type NOT IN {keep}")

                    c.execute("CREATE TRIGGER IF NOT EXISTS no_update_objects BEFORE UPDATE ON objects "
                              "BEGIN SELECT RAISE(ABORT,'Objects are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_delete_objects BEFORE DELETE ON objects "
                              "BEGIN SELECT RAISE(ABORT,'Objects are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_update_insights BEFORE UPDATE ON insight_versions "
                              "BEGIN SELECT RAISE(ABORT,'Insight versions are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_delete_insights BEFORE DELETE ON insight_versions "
                              "BEGIN SELECT RAISE(ABORT,'Insight versions are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_update_events BEFORE UPDATE ON insight_events "
                              "BEGIN SELECT RAISE(ABORT,'Insight events are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_delete_events BEFORE DELETE ON insight_events "
                              "BEGIN SELECT RAISE(ABORT,'Insight events are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_update_releases BEFORE UPDATE ON releases "
                              "BEGIN SELECT RAISE(ABORT,'Releases are immutable'); END;")
                    c.execute("CREATE TRIGGER IF NOT EXISTS no_delete_releases BEFORE DELETE ON releases "
                              "BEGIN SELECT RAISE(ABORT,'Releases are immutable'); END;")

                if scope == 'all':
                    _clear_dir_contents(s.config.path('source_store'))
                    _clear_dir_contents(s.config.path('private_references'))
                    for sp in (Path('/app/workspace/staging/split_manifest.json'),
                               s.config.path('database').parent / 'split_manifest.json'):
                        sp.unlink(missing_ok=True)

                s.ensure_empty_release()

            return {'status': 'cleared', 'scope': scope}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Failed to clear stored data ({scope}): {e}")

    @app.post('/api/workflow/run')
    async def workflow_run(req: WorkflowRunReq):
        action = req.action
        rel = req.release or 'M0'
        base = req.base or 'M0'

        if action == 'ingest_split':
            await runner.run('ingest_split', 'Step 1: Dataset Ingestion & Partitioning', [['ingest'], ['split', '--overwrite']])
        elif action == 'build_seed':
            cmd = ['build-seed-memory', '--release', rel, '--overwrite']
            if req.pair_id:
                cmd.extend(['--pair-id', req.pair_id])
                limit_str = f' (Pair: {req.pair_id})'
            else:
                limit_str = f' (Sample: {req.limit} pairs)' if req.limit else ''
                if req.limit:
                    cmd.extend(['--limit', str(req.limit)])
            await runner.run('build_seed', f'Step 2: Seed Memory Construction ({rel}){limit_str}', [cmd])
        elif action == 'collect_practice':
            cmd = ['collect-practice', '--release', rel]
            if req.pair_id:
                cmd.extend(['--pair-id', req.pair_id])
                limit_str = f' (Pair: {req.pair_id})'
            else:
                limit_str = f' (Sample: {req.limit} pairs)' if req.limit else ''
                if req.limit:
                    cmd.extend(['--limit', str(req.limit)])
            await runner.run('collect_practice', f'Step 3: Collect Practice Iterations ({rel}){limit_str}', [cmd])
        elif action == 'update_insights':
            target_rel = req.release or 'M1'
            cmd = ['update-insights', '--base', base, '--release', target_rel, '--overwrite']
            if req.pair_id:
                cmd.extend(['--pair-id', req.pair_id])
                limit_str = f' (Pair: {req.pair_id})'
            else:
                limit_str = f' (Sample: {req.limit} pairs)' if req.limit else ''
                if req.limit:
                    cmd.extend(['--limit', str(req.limit)])
            await runner.run('update_insights', f'Step 4: Consolidate Insights into {target_rel}{limit_str}', [cmd])
        elif action == 'online_loop':
            cmd = ['online', '--base', base, '--release', req.release or 'M1']
            if req.pair_id:
                cmd.extend(['--pair-id', req.pair_id])
                limit_str = f' (Pair: {req.pair_id})'
            else:
                limit_str = f' (Next {req.limit} pending samples)' if req.limit else ' (all pending samples)'
                if req.limit:
                    cmd.extend(['--limit', str(req.limit)])
            await runner.run('online_loop', f'Online Learning Loop: Steps 3 → 4 → 5 per sample{limit_str}', [cmd])
        elif action == 'build_all_memory':
            limit = req.limit or 5
            await runner.run('build_all_memory', f'Automated Memory Pipeline (Sample: {limit} pairs, M0 -> online 3→4→5 per sample -> M1)', [
                ['build-seed-memory', '--release', 'M0', '--limit', str(limit), '--overwrite'],
                ['online', '--base', 'M0', '--release', 'M1', '--limit', str(limit), '--no-resume'],
                ['inspect-memory', '--release', 'M1']
            ])
        elif action == 'inspect_memory':
            target_rel = req.release or 'M-EMPTY'
            await runner.run('inspect_memory', f'Inspect Memory Release ({target_rel})', [['inspect-memory', '--release', target_rel]])
        elif action in ('eval_dev', 'eval_test'):
            part, name = ('development', 'Development') if action == 'eval_dev' else ('test', 'Test')
            cmd = ['evaluate', '--partition', part, '--release', rel]
            if req.pair_id:
                cmd.extend(['--pair-id', req.pair_id])
                limit_str = f' (Pair: {req.pair_id})'
            else:
                limit_str = f' (Sample: {req.limit} pairs)' if req.limit else ''
                if req.limit:
                    cmd.extend(['--limit', str(req.limit)])
            await runner.run(action, f'Evaluate {name} Split ({rel}){limit_str}', [cmd])
        else:
            raise HTTPException(400, f"Unknown workflow action: {action}")

        return {'status': 'started', 'action': runner.action, 'title': runner.action_title}

    @app.post('/api/workflow/cancel')
    def workflow_cancel():
        runner.cancel()
        return {'status': 'cancelled'}

    app.mount('/static', StaticFiles(directory=str(STATIC)), name='static')
    return app
