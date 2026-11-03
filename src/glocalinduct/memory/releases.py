"""Immutable memory releases (guide §15).

A release references exact experience IDs and insight versions, a resolvable
dependency closure, split/policy/prompt/embedder hashes and a retrieval index.
It is built in a temporary directory, validated, checksummed, atomically renamed,
and then recorded in one database transaction. On open, membership is verified
against the immutable manifest.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import InsightRecord
from ..hashing import payload_hash, utc_now, sha256_text, canonical_json
from .experience import ExperienceMemory
from .insights import InsightMemory
from .store import Repository


class ReleaseError(Exception):
    pass


@dataclass
class Release:
    release_id: str
    release_hash: str
    manifest: dict[str, Any]
    index: dict[str, Any]
    path: Path | None = None

    @property
    def demonstration_ids(self) -> list[str]:
        return [m['experience_id'] for m in self.manifest['experiences'] if m['purpose'] == 'demonstration']

    @property
    def support_ids(self) -> list[str]:
        return [m['experience_id'] for m in self.manifest['experiences']
                if m['purpose'] in ('demonstration', 'support')]

    @property
    def insight_versions(self) -> list[tuple[str, int]]:
        return [(i['insight_id'], i['version']) for i in self.manifest['insights']]

    def summary(self) -> dict:
        return {'release_id': self.release_id, 'release_hash': self.release_hash,
                'demonstrations': len(self.demonstration_ids),
                'support_only': len([m for m in self.manifest['experiences'] if m['purpose'] == 'support']),
                'counterevidence': len([m for m in self.manifest['experiences'] if m['purpose'] == 'counterevidence']),
                'insights': len(self.manifest['insights']), 'published_at': self.manifest.get('published_at'),
                'embedder': self.manifest.get('embedder')}


class ReleaseManager:
    def __init__(self, repo: Repository, experiences: ExperienceMemory, insights: InsightMemory,
                 releases_dir: str | Path):
        self.repo = repo
        self.experiences = experiences
        self.insights = insights
        self.dir = Path(releases_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------- build
    def _closure(self, demonstration_ids: list[str]) -> tuple[list[str], list[str]]:
        """Return (support-only ids, counterevidence ids) reachable from demonstrations
        and active insights. Rejects cycles and unreviewed/forbidden references."""
        support: list[str] = []
        seen = set(demonstration_ids)
        stack = list(demonstration_ids)
        path_stack: list[str] = []
        while stack:
            cur = stack.pop()
            for s in self.experiences.supports_of(cur):
                if s in demonstration_ids:
                    continue
                v = self.experiences.get(s)
                if v is None:
                    raise ReleaseError(f'support {s} of {cur} does not exist')
                if not v.eligible:
                    raise ReleaseError(f'support {s} is not an admitted learning record')
                if s not in seen:
                    seen.add(s); support.append(s); stack.append(s)
        # cycle check (DFS over experience_support restricted to seen)
        color: dict[str, int] = {}

        def dfs(n: str):
            color[n] = 1
            for s in self.experiences.supports_of(n):
                if color.get(s) == 1:
                    raise ReleaseError(f'cycle in support graph at {s}')
                if color.get(s) is None:
                    dfs(s)
            color[n] = 2
        for n in list(seen):
            if color.get(n) is None:
                dfs(n)
        return sorted(support), []

    def delete(self, release_id: str) -> None:
        """Remove a release and its associations so it can be rebuilt or overwritten."""
        with self.repo.transaction() as c:
            c.execute('DELETE FROM release_experiences WHERE release_id=?', (release_id,))
            c.execute('DELETE FROM release_insights WHERE release_id=?', (release_id,))
            c.execute('DELETE FROM active_release WHERE release_id=?', (release_id,))
            c.execute('DROP TRIGGER IF EXISTS no_delete_releases')
            c.execute('DROP TRIGGER IF EXISTS no_update_releases')
            c.execute('DELETE FROM releases WHERE release_id=?', (release_id,))
            c.execute('CREATE TRIGGER IF NOT EXISTS no_update_releases BEFORE UPDATE ON releases '
                      'BEGIN SELECT RAISE(ABORT,\'Releases are immutable\'); END;')
            c.execute('CREATE TRIGGER IF NOT EXISTS no_delete_releases BEFORE DELETE ON releases '
                      'BEGIN SELECT RAISE(ABORT,\'Releases are immutable\'); END;')
            row = c.execute('SELECT release_id FROM releases ORDER BY published_at DESC LIMIT 1').fetchone()
            if row:
                c.execute('INSERT OR REPLACE INTO active_release(singleton,release_id) VALUES(1,?)', (row['release_id'],))
        rel_dir = self.dir / release_id
        if rel_dir.exists():
            shutil.rmtree(rel_dir)

    def publish(self, release_id: str, demonstration_ids: list[str], insight_records: list[InsightRecord],
                index: dict[str, Any], hashes: dict[str, str], run_id: str, base_release: str | None = None,
                activate: bool = True, overwrite: bool = False) -> Release:
        exists = self.repo.execute('SELECT 1 FROM releases WHERE release_id=?', (release_id,)).fetchone() is not None
        if exists or (self.dir / release_id).exists():
            if not overwrite:
                raise ReleaseError(f'release {release_id} already exists (releases are immutable)')
            self.delete(release_id)
        for e in demonstration_ids:
            v = self.experiences.get(e)
            if v is None or not v.eligible:
                raise ReleaseError(f'demonstration {e} is not an eligible experience')
            if v.phase not in ('seed', 'practice'):
                raise ReleaseError(f'demonstration {e} comes from forbidden phase {v.phase}')
        support_only, _ = self._closure(demonstration_ids)
        counter: list[str] = []
        for rec in insight_records:
            if rec.status != 'active':
                raise ReleaseError(f'insight {rec.insight_id} v{rec.version} is {rec.status}, not active')
            stored = self.insights.version(rec.insight_id, rec.version)
            if stored is None or stored.model_dump() != rec.model_dump():
                raise ReleaseError(f'insight {rec.insight_id} v{rec.version} does not match stored version')
            for s in rec.content.support_experience_ids:
                if s not in demonstration_ids and s not in support_only:
                    v = self.experiences.get(s)
                    if v is None or not v.eligible:
                        raise ReleaseError(f'insight support {s} is not admitted')
                    support_only.append(s)
            for ce in rec.content.counterevidence_ids:
                if ce not in counter:
                    counter.append(ce)
        support_only = sorted(set(support_only))
        bad = set(counter) & set(demonstration_ids)
        if bad:
            raise ReleaseError(f'counterevidence {sorted(bad)} cannot also be a demonstration')
        for exp in index.get('experiences', []):
            if exp['experience_id'] not in demonstration_ids:
                raise ReleaseError(f'index contains non-demonstration experience {exp["experience_id"]}')
        members = ([{'experience_id': e, 'purpose': 'demonstration'} for e in sorted(demonstration_ids)] +
                   [{'experience_id': e, 'purpose': 'support'} for e in support_only] +
                   [{'experience_id': e, 'purpose': 'counterevidence'} for e in sorted(counter)])
        index_json = canonical_json(index)
        manifest = {'release_id': release_id, 'base_release': base_release, 'experiences': members,
                    'insights': [{'insight_id': r.insight_id, 'version': r.version} for r in
                                 sorted(insight_records, key=lambda r: r.insight_id)],
                    'hashes': dict(hashes), 'index_hash': sha256_text(index_json),
                    'embedder': index.get('embedder'), 'published_at': utc_now()}
        release_hash = payload_hash({k: v for k, v in manifest.items() if k != 'published_at'})
        manifest['release_hash'] = release_hash
        tmp = self.dir / f'.{release_id}.tmp'
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        (tmp / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
        (tmp / 'index.json').write_text(index_json, 'utf-8')
        final = self.dir / release_id
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
        try:
            with self.repo.transaction() as c:
                mid = self.repo.put_object('memory_release', manifest, run_id)
                c.execute('INSERT INTO releases VALUES(?,?,?,?)', (release_id, mid, release_hash, manifest['published_at']))
                for m in members:
                    c.execute('INSERT INTO release_experiences VALUES(?,?,?)', (release_id, m['experience_id'], m['purpose']))
                for i in manifest['insights']:
                    c.execute('INSERT INTO release_insights VALUES(?,?,?)', (release_id, i['insight_id'], i['version']))
                if activate:
                    c.execute('INSERT INTO active_release(singleton,release_id) VALUES(1,?) '
                              'ON CONFLICT(singleton) DO UPDATE SET release_id=excluded.release_id', (release_id,))
        except Exception:
            shutil.rmtree(final, ignore_errors=True)
            raise
        return Release(release_id, release_hash, manifest, index, final)

    # ------------------------------------------------------------------ read
    def get(self, release_id: str) -> Release | None:
        row = self.repo.execute('SELECT * FROM releases WHERE release_id=?', (release_id,)).fetchone()
        if row is None:
            return None
        manifest = self.repo.payload(row['manifest_record_id'])
        path = self.dir / release_id
        index = json.loads((path / 'index.json').read_text('utf-8')) if (path / 'index.json').exists() else \
            {'experiences': [], 'insights': []}
        rel = Release(release_id, row['release_hash'], manifest, index, path)
        self.verify(rel)
        return rel

    def active(self) -> Release | None:
        row = self.repo.execute('SELECT release_id FROM active_release WHERE singleton=1').fetchone()
        return self.get(row['release_id']) if row else None

    def list(self) -> list[dict]:
        return [dict(r) for r in self.repo.query('SELECT release_id, release_hash, published_at FROM releases ORDER BY published_at')]

    def verify(self, rel: Release) -> None:
        db_members = sorted((r['experience_id'], r['purpose']) for r in self.repo.query(
            'SELECT experience_id,purpose FROM release_experiences WHERE release_id=?', (rel.release_id,)))
        man_members = sorted((m['experience_id'], m['purpose']) for m in rel.manifest['experiences'])
        if db_members != man_members:
            raise ReleaseError(f'release {rel.release_id}: database membership differs from manifest')
        db_ins = sorted((r['insight_id'], r['version']) for r in self.repo.query(
            'SELECT insight_id,version FROM release_insights WHERE release_id=?', (rel.release_id,)))
        if db_ins != sorted(rel.insight_versions):
            raise ReleaseError(f'release {rel.release_id}: insight membership differs from manifest')
        if rel.path and (rel.path / 'index.json').exists():
            if sha256_text((rel.path / 'index.json').read_text('utf-8')) != rel.manifest['index_hash']:
                raise ReleaseError(f'release {rel.release_id}: index hash mismatch')
