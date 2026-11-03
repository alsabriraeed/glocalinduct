"""Command-line interface (guide §16). Deterministic exit codes, JSON summaries.

exit 0 = success; 1 = expected pipeline/config failure; 2 = usage error; 3 = unresolved inference.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import trace
from .config import load_config, resolve_model_preset
from .errors import GlocalInductError
from .services import build_services


def _runtime_overrides(args) -> dict | None:
    rt: dict = {}
    if getattr(args, 'model', None):
        rt.update(resolve_model_preset(args.model))
    if getattr(args, 'provider', None):
        if not rt and args.provider in ('ollama', 'openai'):
            rt.update(resolve_model_preset(args.provider))
        rt['provider'] = args.provider
    return {'runtime': rt} if rt else None


def _services(args):
    overrides = _runtime_overrides(args)
    cfg = load_config(args.config, overrides)
    cfg.assert_real_pipeline()
    return build_services(cfg)


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1))
    trace.emit('summary', result=obj)


def _configure_trace(args) -> None:
    """Enable the Step Inspector trace for both web runner and CLI runs."""
    if args.cmd == 'serve':
        return
    if os.environ.get('GLOCALINDUCT_TRACE') == '0':
        return
    session = os.environ.get('GLOCALINDUCT_TRACE_SESSION') or \
        ('ses_online' if args.cmd in ('online', 'learn-online') else 'ses_qwen_live')
    cmd_map = {
        'seed-memory': 'build-seed-memory',
        'practice': 'collect-practice',
        'consolidate': 'update-insights',
        'online': 'collect-practice',
        'learn-online': 'collect-practice',
    }
    canon_cmd = cmd_map.get(args.cmd, args.cmd)
    try:
        cfg = load_config(args.config)
        trace.configure(cfg.path('traces'), session, canon_cmd)
        trace.emit('command_start', command=canon_cmd,
                   args={k: v for k, v in vars(args).items() if k not in ('fn', 'cmd', 'config')})
    except Exception:
        pass    # tracing must never break a command


def cmd_ingest(args):
    from .workflows.ingest import ingest_pairs
    s = _services(args)
    rep = ingest_pairs(s, args.data_root)
    _print({**rep.summary(), 'exclusions': rep.exclusions})
    return 0


def cmd_split(args):
    from .workflows.ingest import split_groups
    s = _services(args)
    m = split_groups(s, overwrite=getattr(args, 'overwrite', False))
    _print({'manifest_hash': m.manifest_hash, 'counts': m.counts, 'feasibility_pilot': m.feasibility_pilot,
            'groups': len(m.partition_of_group)})
    return 0


def cmd_seed(args):
    from .workflows.seed import build_seed_memory
    s = _services(args)
    limit = getattr(args, 'limit', None)
    pair_id = getattr(args, 'pair_id', None)
    overwrite = getattr(args, 'overwrite', False)
    _print(asyncio.run(build_seed_memory(s, args.release, limit=limit, overwrite=overwrite, pair_id=pair_id)))
    return 0


def cmd_practice(args):
    from .workflows.practice import collect_practice
    s = _services(args)
    limit = getattr(args, 'limit', None)
    pair_id = getattr(args, 'pair_id', None)
    _print(asyncio.run(collect_practice(s, args.release, limit=limit, pair_id=pair_id)))
    return 0


def cmd_update(args):
    from .workflows.consolidate import update_insights
    s = _services(args)
    overwrite = getattr(args, 'overwrite', False)
    limit = getattr(args, 'limit', None)
    pair_id = getattr(args, 'pair_id', None)
    _print(asyncio.run(update_insights(s, args.base, args.release, overwrite=overwrite, limit=limit, pair_id=pair_id)))
    return 0


def cmd_online(args):
    from .workflows.online import online_learning
    s = _services(args)
    pids = [p for x in (args.pair_id or []) for p in x.split(',') if p]
    _print(asyncio.run(online_learning(s, args.base, args.release, limit=args.limit, pair_ids=pids or None,
                                       resume=not args.no_resume)))
    return 0


def cmd_infer(args):
    from .workflows import InferRequest, infer
    s = _services(args)
    if args.input:
        req_data = json.loads(Path(args.input).read_text('utf-8'))
        req = InferRequest(text=req_data.get('text'), file_path=req_data.get('file'), context=req_data.get('context', {}),
                           budget_overrides=req_data.get('budget', {}))
    elif args.file:
        req = InferRequest(file_path=args.file, context=json.loads(args.context or '{}'))
    else:
        text = sys.stdin.read() if args.text == '-' else args.text
        req = InferRequest(text=text, context=json.loads(args.context or '{}'))
    out = asyncio.run(infer(req, s, args.release))
    d = out.to_dict()
    if args.full:
        _print(d)
    else:
        _print({'run_id': out.run_id, **d['result']})
    return 0 if out.result.status != 'unresolved' else 3


def cmd_evaluate(args):
    from .workflows.evaluate import evaluate
    s = _services(args)
    pair_id = getattr(args, 'pair_id', None)
    releases_arg = getattr(args, 'releases', None) or args.release
    if releases_arg and ',' in releases_arg:
        results = []
        for rel in [r.strip() for r in releases_arg.split(',') if r.strip()]:
            rep = asyncio.run(evaluate(s, args.partition, rel, limit=args.limit, pair_id=pair_id))
            rows = rep.pop('rows')
            results.append({**rep, 'rows': rows if args.full else len(rows)})
        _print({'partition': args.partition, 'evaluations': results})
        return 0
    rep = asyncio.run(evaluate(s, args.partition, releases_arg, limit=args.limit, pair_id=pair_id))
    rows = rep.pop('rows')
    _print({**rep, 'rows': rows if args.full else len(rows)})
    return 0


def cmd_inspect(args):
    s = _services(args)
    rel = s.resolve_release(args.release)
    insights = []
    for iid, ver in rel.insight_versions:
        rec = s.insights.version(iid, ver)
        insights.append({'insight_id': iid, 'version': ver, 'importance': rec.importance, 'status': rec.status,
                         'lesson': rec.content.lesson, 'scope': rec.content.scope.model_dump(mode='json'),
                         'support_groups': rec.support_group_ids, 'support_domains': rec.support_domains,
                         'origin': rec.content.origin})
    members = []
    for m in rel.manifest['experiences']:
        v = s.experiences.get(m['experience_id'])
        members.append({'experience_id': m['experience_id'], 'purpose': m['purpose'], 'kind': v.kind,
                        'group_id': v.group_id, 'phase': v.phase, 'outcome': v.outcome,
                        'domains': v.retrieval.get('domains'), 'scope': v.retrieval.get('scope')})
    excluded = [{'experience_id': v.experience_id, 'kind': v.kind, 'group_id': v.group_id,
                 'reasons': v.record.get('exclusion_reasons') or v.record.get('issue_codes')}
                for v in s.experiences.all() if not v.eligible]
    _print({'release': rel.summary(), 'hashes': rel.manifest['hashes'], 'insights': insights, 'members': members,
            'excluded_records': excluded, 'all_releases': s.releases.list()})
    return 0


def cmd_serve(args):
    import uvicorn
    from .web.app import create_app
    app = create_app(args.config, args.provider, model=getattr(args, 'model', None))
    uvicorn.run(app, host=args.host, port=args.port, log_level='info')
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--config', default=argparse.SUPPRESS, help='path to configs/default.yaml')
    common.add_argument('--provider', default=argparse.SUPPRESS, help='override runtime.provider (fake|anthropic|openai|ollama|...)')
    common.add_argument('--model', '-m', default=argparse.SUPPRESS, help='override model/preset (e.g. gpt-4o or qwen3.8)')

    p = argparse.ArgumentParser(prog='glocalinduct', description='GlocalInduct: experiential EN→AR glocalization', parents=[common])
    p.set_defaults(config=None, provider=None, model=None)
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('ingest', parents=[common]); a.add_argument('--data-root', default=None); a.set_defaults(fn=cmd_ingest)
    a = sub.add_parser('split', parents=[common]); a.add_argument('--overwrite', action='store_true', default=False); a.set_defaults(fn=cmd_split)
    for name in ('build-seed-memory', 'seed-memory'):
        a = sub.add_parser(name, parents=[common]); a.add_argument('--release', default='M0'); a.add_argument('--limit', type=int, default=None); a.add_argument('--pair-id', default=None); a.add_argument('--overwrite', action='store_true', default=False); a.set_defaults(fn=cmd_seed)
    for name in ('collect-practice', 'practice'):
        a = sub.add_parser(name, parents=[common]); a.add_argument('--release', '--base-release', dest='release', default='M0'); a.add_argument('--limit', type=int, default=None); a.add_argument('--pair-id', default=None); a.set_defaults(fn=cmd_practice)
    for name in ('update-insights', 'consolidate'):
        a = sub.add_parser(name, parents=[common]); a.add_argument('--base', '--base-release', dest='base', default='M0'); a.add_argument('--release', default='M1'); a.add_argument('--limit', type=int, default=None); a.add_argument('--pair-id', default=None); a.add_argument('--overwrite', action='store_true', default=False)
        a.set_defaults(fn=cmd_update)
    for name in ('online', 'learn-online'):
        a = sub.add_parser(name, parents=[common], help='Steps 3 -> 4 -> 5 sequentially for every practice sample')
        a.add_argument('--base', '--base-release', dest='base', default='M0')
        a.add_argument('--release', default='M1', help='rolling release republished after every sample')
        a.add_argument('--limit', type=int, default=None, help='process at most N pending samples')
        a.add_argument('--pair-id', action='append', default=None, help='restrict to these pair ids (repeat or comma-separate)')
        a.add_argument('--no-resume', action='store_true', default=False,
                       help='start from the base release and do not skip completed samples')
        a.set_defaults(fn=cmd_online)
    a = sub.add_parser('infer', parents=[common]); a.add_argument('--input', default=None, help='request.json with text|file, context, budget')
    a.add_argument('--text', default=None, help='source text, or - for stdin'); a.add_argument('--file', default=None)
    a.add_argument('--context', default=None, help='JSON context'); a.add_argument('--release', default=None)
    a.add_argument('--full', action='store_true'); a.set_defaults(fn=cmd_infer)
    a = sub.add_parser('evaluate', parents=[common]); a.add_argument('--partition', default='test'); a.add_argument('--release', default=None); a.add_argument('--releases', default=None)
    a.add_argument('--limit', type=int, default=None); a.add_argument('--pair-id', default=None); a.add_argument('--full', action='store_true'); a.set_defaults(fn=cmd_evaluate)
    a = sub.add_parser('inspect-memory', parents=[common]); a.add_argument('--release', default=None); a.set_defaults(fn=cmd_inspect)
    a = sub.add_parser('serve', parents=[common]); a.add_argument('--host', default='127.0.0.1'); a.add_argument('--port', type=int, default=8000)
    a.set_defaults(fn=cmd_serve)
    args = p.parse_args(argv)
    _configure_trace(args)
    try:
        return args.fn(args)
    except GlocalInductError as e:
        _print({'error': e.code, 'message': str(e), 'details': e.details})
        return 1
    except KeyError as e:
        _print({'error': 'INPUT_INVALID', 'message': str(e)})
        return 1


if __name__ == '__main__':
    sys.exit(main())
