"""Leakage guards (guide §17): online bundles must not contain the current
Arabic reference, PairAnalysis, target-derived annotations or private comparison
findings. Accepted examples from other eligible groups may include Arabic.
"""
from __future__ import annotations

from typing import Any

FORBIDDEN_ONLINE_KEYS = {'reference', 'arabic_reference', 'private_reference', 'pair_analysis',
                         'reference_comparison', 'reference_text', 'target_reference'}


def assert_no_leakage(bundle: dict[str, Any], forbidden_texts: list[str] | None = None) -> None:
    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in FORBIDDEN_ONLINE_KEYS:
                    raise AssertionError(f'forbidden key {k!r} in online bundle at {path}')
                walk(v, f'{path}.{k}')
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f'{path}[{i}]')
        elif isinstance(node, str) and forbidden_texts:
            for t in forbidden_texts:
                if t and len(t) >= 20 and t in node:
                    raise AssertionError(f'private reference text leaked into online bundle at {path}')
    walk(bundle, '$')
