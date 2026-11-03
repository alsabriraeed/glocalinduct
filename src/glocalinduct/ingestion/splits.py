"""Group-level frozen splits (guide §4 step 3).

Groups are assigned once to learning/development/test with a fixed seed and a
documented stratification: groups are sorted by their dominant domain label,
shuffled deterministically within each stratum, then dealt proportionally.
Learning groups are split equally into seed and practice. Groups are never split.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..hashing import payload_hash


@dataclass
class SplitManifest:
    seed: int
    ratios: dict[str, float]
    grouping_version: str
    partition_of_group: dict[str, str]        # group_id -> learning|development|test
    subset_of_group: dict[str, str]           # learning groups -> seed|practice; others ''
    pair_group: dict[str, str]                # pair_id -> group_id
    counts: dict[str, int] = field(default_factory=dict)
    domains_unavailable: list[str] = field(default_factory=list)
    feasibility_pilot: bool = False
    manifest_hash: str = ''

    def partition_of_pair(self, pair_id: str) -> str:
        return self.partition_of_group[self.pair_group[pair_id]]

    def subset_of_pair(self, pair_id: str) -> str:
        return self.subset_of_group.get(self.pair_group[pair_id], '')

    def to_dict(self) -> dict:
        return {'seed': self.seed, 'ratios': self.ratios, 'grouping_version': self.grouping_version,
                'partition_of_group': self.partition_of_group, 'subset_of_group': self.subset_of_group,
                'pair_group': self.pair_group, 'counts': self.counts,
                'domains_unavailable': self.domains_unavailable, 'feasibility_pilot': self.feasibility_pilot,
                'manifest_hash': self.manifest_hash}

    @classmethod
    def from_dict(cls, d: dict) -> 'SplitManifest':
        return cls(**d)


def split_groups(pair_group: dict[str, str], pair_domains: dict[str, list[str]],
                 ratios: dict[str, float], seed: int, seed_fraction: float = 0.5,
                 grouping_version: str = 'grouping-1.0', min_groups_for_full_run: int = 10) -> SplitManifest:
    groups = sorted(set(pair_group.values()))
    pids_by_group: dict[str, list[str]] = {}
    for pid, gid in pair_group.items():
        pids_by_group.setdefault(gid, []).append(pid)
    # dominant domain per group for stratification
    stratum: dict[str, str] = {}
    all_domains: set[str] = set()
    for g in groups:
        counts: dict[str, int] = {}
        for pid in pids_by_group.get(g, ()):
            for d in pair_domains.get(pid, []) or ['general']:
                counts[d] = counts.get(d, 0) + 1
                all_domains.add(d)
        stratum[g] = max(sorted(counts), key=lambda d: counts[d]) if counts else 'general'
    rng = random.Random(seed)
    partition: dict[str, str] = {}
    order = ['learning', 'development', 'test']
    strata: dict[str, list[str]] = {}
    for g in groups:
        strata.setdefault(stratum[g], []).append(g)
    # deal with largest-remainder allocation per stratum; carry deficits across strata deterministically
    carry = {k: 0.0 for k in order}
    for s in sorted(strata):
        gs = sorted(strata[s])
        rng.shuffle(gs)
        n = len(gs)
        targets = {k: ratios[k] * n + carry[k] for k in order}
        alloc = {k: int(targets[k]) for k in order}
        remainder = n - sum(alloc.values())
        frac = sorted(order, key=lambda k: -(targets[k] - alloc[k]))
        for k in frac[:remainder]:
            alloc[k] += 1
        for k in order:
            carry[k] = targets[k] - alloc[k]
        i = 0
        for k in order:
            for g in gs[i:i + alloc[k]]:
                partition[g] = k
            i += alloc[k]
    subset: dict[str, str] = {}
    rng2 = random.Random(seed + 1)
    if len(strata) <= 1:
        learning = sorted(g for g in groups if partition[g] == 'learning')
        rng2.shuffle(learning)
        n_seed = int(round(len(learning) * seed_fraction))
        for i, g in enumerate(learning):
            subset[g] = 'seed' if i < n_seed else 'practice'
    else:
        sub_order = ['seed', 'practice']
        sub_ratios = {'seed': seed_fraction, 'practice': 1.0 - seed_fraction}
        carry_s = {k: 0.0 for k in sub_order}
        for s in sorted(strata):
            gs = sorted(g for g in strata[s] if partition[g] == 'learning')
            if not gs:
                continue
            rng2.shuffle(gs)
            n = len(gs)
            targets = {k: sub_ratios[k] * n + carry_s[k] for k in sub_order}
            alloc = {k: int(targets[k]) for k in sub_order}
            remainder = n - sum(alloc.values())
            frac = sorted(sub_order, key=lambda k: -(targets[k] - alloc[k]))
            for k in frac[:remainder]:
                alloc[k] += 1
            for k in sub_order:
                carry_s[k] = targets[k] - alloc[k]
            for g in gs[:alloc['seed']]:
                subset[g] = 'seed'
            for g in gs[alloc['seed']:]:
                subset[g] = 'practice'
        learning = sorted(g for g in groups if partition[g] == 'learning')
    counts = {k: sum(1 for g in groups if partition[g] == k) for k in order}
    counts['seed'] = sum(1 for g in learning if subset[g] == 'seed')
    counts['practice'] = sum(1 for g in learning if subset[g] == 'practice')
    manifest = SplitManifest(seed=seed, ratios=dict(ratios), grouping_version=grouping_version,
                             partition_of_group=partition, subset_of_group=subset,
                             pair_group=dict(sorted(pair_group.items())), counts=counts,
                             domains_unavailable=[], feasibility_pilot=len(groups) < min_groups_for_full_run)
    manifest.manifest_hash = payload_hash({k: v for k, v in manifest.to_dict().items() if k != 'manifest_hash'})
    return manifest
