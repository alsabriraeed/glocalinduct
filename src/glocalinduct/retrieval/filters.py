"""Eligibility gates applied before ranking (guide §10, README scope filters).

Unknown context is never compatibility. An explicitly unspecified cultural scope
cannot serve as positive substitution support. Genre, domain, setting and
time-period metadata are NOT used to exclude memory; only the locale rule gates.
"""
from __future__ import annotations


def _locale_ok(scope: dict, query: dict) -> tuple[bool, str]:
    cs = bool(scope.get('culture_specific'))
    ls = scope.get('locale_scope', 'unspecified')
    qs = query.get('locale_scope', 'unspecified')
    if not cs:
        return True, 'not culture-specific: no locale requirement'
    if ls == 'unspecified':
        return False, 'culture-specific scope with unspecified locale cannot support substitution'
    if ls == 'specified':
        if qs != 'specified':
            return False, 'locale-specific example but request locale is not specified'
        if not set(scope.get('locales', [])) & set(query.get('locales', [])):
            return False, 'locale mismatch'
        return True, 'locale overlap'
    # general_arab example
    return True, 'general Arab scope compatible'


def experience_compatible(scope: dict, exp_domains: list[str], query: dict) -> tuple[bool, str, bool]:
    """Return (eligible, reason, same_domain). Domain scoping removed as requested."""
    ok, why = _locale_ok(scope, query)
    if not ok:
        return False, why, False
    return True, 'compatible (genre/domain/setting unconstrained)', True


def insight_compatible(scope: dict, query: dict) -> tuple[bool, str, bool]:
    """Return (eligible, reason, same_domain). Domain scoping removed as requested."""
    level = scope.get('level')
    if level == 'universal':
        return True, 'universal preservation/realization lesson', True
    ok, why = _locale_ok(scope, query)
    if not ok:
        return False, why, False
    return True, 'scope compatible (genre/domain/setting unconstrained)', True
