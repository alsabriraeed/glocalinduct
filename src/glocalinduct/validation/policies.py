"""Domain preservation policies (guide §1 "Domain policy").

Policies are configuration. The shared policy always applies; the controller adds
every relevant domain policy. Mixed domains use the union of preservation
requirements. Unknown domains fall back to the shared policy and are recorded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..hashing import payload_hash


@dataclass(frozen=True)
class ResolvedPolicy:
    domains: tuple[str, ...]
    unknown_domains: tuple[str, ...]
    preserve: tuple[str, ...]
    strategies: tuple[str, ...]
    substitution_rule: str
    combination_rule: str
    on_conflict: str

    def to_dict(self) -> dict:
        return {'domains': list(self.domains), 'unknown_domains': list(self.unknown_domains),
                'preserve': list(self.preserve), 'strategies': list(self.strategies),
                'substitution_rule': self.substitution_rule, 'combination_rule': self.combination_rule,
                'on_conflict': self.on_conflict}


@dataclass
class DomainPolicies:
    raw: dict
    policy_hash: str = ''
    _aliases: dict[str, str] = field(default_factory=lambda: {
        'social studies': 'social_studies', 'social-studies': 'social_studies',
        'legal': 'law', 'medical': 'health', 'medicine': 'health', 'literature': 'arts',
        'entertainment': 'fiction', 'story': 'fiction', 'economics': 'finance',
        'technology': 'engineering', 'tech': 'engineering', 'software': 'engineering',
    })

    def known_domains(self) -> list[str]:
        return sorted(self.raw.get('domains', {}).keys())

    def canonical(self, domain: str) -> str:
        d = domain.strip().lower().replace(' ', '_')
        return self._aliases.get(d, self._aliases.get(domain.strip().lower(), d))

    def resolve(self, domains: list[str]) -> ResolvedPolicy:
        shared = self.raw.get('shared', {})
        preserve: list[str] = list(shared.get('preserve', []))
        known: list[str] = []
        unknown: list[str] = []
        table = self.raw.get('domains', {})
        for d in domains or []:
            c = self.canonical(d)
            if c in table:
                if c not in known:
                    known.append(c)
                for p in table[c] or []:
                    if p not in preserve:
                        preserve.append(p)
            elif c not in unknown:
                unknown.append(c)
        return ResolvedPolicy(domains=tuple(known), unknown_domains=tuple(unknown), preserve=tuple(preserve),
                              strategies=tuple(shared.get('strategies', ['retain', 'substitute'])),
                              substitution_rule=shared.get('substitution_rule', ''),
                              combination_rule=self.raw.get('combination_rule', 'union_of_preservation_requirements'),
                              on_conflict=self.raw.get('on_conflict', 'preserve_and_record_uncertainty'))


def load_policies(path: str | Path) -> DomainPolicies:
    raw = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if set(raw.get('shared', {}).get('strategies', [])) != {'retain', 'substitute'}:
        raise ValueError('shared policy must declare exactly retain and substitute')
    return DomainPolicies(raw=raw, policy_hash=payload_hash(raw))
