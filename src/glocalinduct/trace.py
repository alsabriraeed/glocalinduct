"""Structured execution trace: a read-only side channel for the web "Step Inspector".

Workflows emit small JSON events (`emit`) describing what every agent received and
produced. Events are appended to `<traces>/<session>/trace.jsonl`; full system prompts
are stored once, content-addressed, under `<traces>/<session>/prompts/<sha256>.txt`.

Tracing never influences pipeline behaviour: it is off unless `configure` was called,
and `emit` swallows every error. Traces contain private reference Arabic and complete
prompts, so the readers below validate every identifier and keep file access inside
the traces root.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import itertools
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_SESSION_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_HASH_RE = re.compile(r'^[a-f0-9]{64}$')
MAX_PAGE = 500

_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar('glocalinduct_trace_ctx', default={})
_lock = threading.Lock()
_seq = itertools.count(1)
_sink: dict[str, Any] = {'dir': None, 'command': None}


# ----------------------------------------------------------------------- writer
def valid_session(session: str) -> bool:
    return bool(_SESSION_RE.match(session or ''))


def configure(root: Path | str, session: str, command: str | None = None) -> None:
    """Enable tracing for this process. Raises ValueError on an invalid session id."""
    if not valid_session(session):
        raise ValueError('invalid trace session id')
    d = (Path(root) / session).resolve()
    (d / 'prompts').mkdir(parents=True, exist_ok=True)
    _sink['dir'] = d
    _sink['command'] = command


def enabled() -> bool:
    return _sink['dir'] is not None


def set_command(command: str | None) -> None:
    _sink['command'] = command


def push(**kw: Any):
    """Merge context fields (pair_id, attempt, phase, ...) into every following event."""
    return _ctx.set({**_ctx.get(), **kw})


def pop(token) -> None:
    try:
        _ctx.reset(token)
    except Exception:
        pass


@contextlib.contextmanager
def scope(**kw: Any) -> Iterator[None]:
    tok = push(**kw)
    try:
        yield
    finally:
        pop(tok)


def update(**kw: Any) -> None:
    """Change context fields in place for the current scope (e.g. the attempt number)."""
    _ctx.set({**_ctx.get(), **kw})


def store_prompt(text: str) -> str | None:
    """Persist a prompt once; return its sha256 (or None when tracing is off)."""
    d = _sink['dir']
    if d is None:
        return None
    try:
        h = hashlib.sha256(text.encode('utf-8')).hexdigest()
        p = d / 'prompts' / f'{h}.txt'
        if not p.exists():
            with _lock:
                p.write_text(text, encoding='utf-8')
        return h
    except Exception:
        return None


def emit(kind: str, **data: Any) -> None:
    """Append one event. Never raises."""
    d = _sink['dir']
    if d is None:
        return
    try:
        ev = {**_ctx.get(), 'seq': next(_seq), 'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
              'command': _sink['command'], 'kind': kind, 'data': data}
        line = json.dumps(ev, ensure_ascii=False, default=str)
        with _lock:
            with (d / 'trace.jsonl').open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')
    except Exception:
        pass


def emit_lazy(kind: str, build) -> None:
    """Emit `kind` with data from `build()`; skipped when disabled, never raises."""
    if _sink['dir'] is None:
        return
    try:
        emit(kind, **build())
    except Exception:
        pass


# ----------------------------------------------------------------------- readers
def _session_dir(root: Path | str, session: str) -> Path | None:
    if not valid_session(session):
        return None
    base = Path(root).resolve()
    d = (base / session).resolve()
    if d.parent != base:
        return None
    return d


def list_sessions(root: Path | str, limit: int = 50) -> list[dict]:
    base = Path(root)
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        f = d / 'trace.jsonl'
        if d.is_dir() and valid_session(d.name) and f.is_file():
            st = f.stat()
            out.append({'session': d.name, 'bytes': st.st_size, 'modified': st.st_mtime})
    out.sort(key=lambda r: r['modified'], reverse=True)
    return out[:max(1, min(limit, 200))]


def read_events(root: Path | str, session: str, start: int = 0, limit: int = 200) -> dict | None:
    """Return events [start, start+limit) as parsed objects plus the next offset."""
    d = _session_dir(root, session)
    if d is None:
        return None
    f = d / 'trace.jsonl'
    start = max(0, int(start))
    limit = max(1, min(int(limit), MAX_PAGE))
    events: list[dict] = []
    if f.is_file():
        with f.open('r', encoding='utf-8') as fh:
            for idx, line in enumerate(fh):
                if idx < start:
                    continue
                if len(events) >= limit or not line.endswith('\n'):   # a half-written last line is read next poll
                    break
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({'kind': 'corrupt_line', 'seq': None, 'data': {}})
    return {'session': session, 'start': start, 'next': start + len(events), 'events': events}


def read_prompt(root: Path | str, session: str, digest: str) -> str | None:
    d = _session_dir(root, session)
    if d is None or not _HASH_RE.match(digest or ''):
        return None
    p = (d / 'prompts' / f'{digest}.txt').resolve()
    if p.parent != (d / 'prompts').resolve() or not p.is_file():
        return None
    return p.read_text(encoding='utf-8')
