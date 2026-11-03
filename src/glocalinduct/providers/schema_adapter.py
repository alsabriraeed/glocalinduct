"""Restrict a Pydantic JSON Schema to the subset structured-output APIs accept.

Numerical/string/array constraints are moved into descriptions and removed;
`additionalProperties: false` is set on every object. Local Pydantic validation
remains authoritative (brief: "Provider schemas may need an adapter").
"""
from __future__ import annotations

import copy
from typing import Any

_MOVE = ('minLength', 'maxLength', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
         'multipleOf', 'pattern', 'maxItems', 'uniqueItems')


def restrict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    s = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            notes = []
            for k in _MOVE:
                if k in node:
                    notes.append(f'{k}={node.pop(k)}')
            if 'minItems' in node and node['minItems'] not in (0, 1):
                notes.append(f'minItems={node.pop("minItems")}')
            if notes:
                node['description'] = (node.get('description', '') + ' (constraints: ' + ', '.join(notes) + ')').strip()
            if node.get('type') == 'object' or 'properties' in node:
                node['additionalProperties'] = False
                props = node.get('properties', {})
                node['required'] = list(props.keys())      # all fields explicit; None is allowed where typed
            node.pop('title', None)
            node.pop('default', None)
            for v in list(node.values()):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(s)
    s.pop('$schema', None)
    return s
