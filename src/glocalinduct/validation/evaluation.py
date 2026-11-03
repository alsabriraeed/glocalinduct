"""Terminal decision logic (guide §13). The controller, not the evaluator, decides.

Accept only if every deterministic check passed, every applicable criterion passed
with score >= min, and no critical finding exists. No weighted average. Unknown
quality is unresolved. Safe retention with all checks passing is `conservative`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import PlanPayload, QualityEvaluationPayload

CRITERION_ISSUE = {
    'meaning_and_purpose': 'MEANING_VIOLATION',
    'cultural_fit': 'CULTURAL_FIT_FAILED',
    'arabic_quality': 'ARABIC_QUALITY_FAILED',
    'plan_compliance': 'PLAN_MISMATCH',
}


def comparison_issue_code(difference) -> str:
    """Issue code for a `candidate_defect` found by the reference comparison."""
    expl = (difference.explanation or '').lower()
    if any(w in expl for w in ('cultur', 'gesture', 'tradit', 'custom', 'fit', 'norm', 'arab')):
        return 'CULTURAL_FIT_FAILED'
    if difference.critical:
        return 'MEANING_VIOLATION'
    return 'REFERENCE_INCOMPATIBLE'


@dataclass
class Decision:
    status: str                                  # accepted|conservative|unresolved
    issue_codes: list[str] = field(default_factory=list)
    fallback_reasons: list[str] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)


def decide_result(report: QualityEvaluationPayload, plans: list[PlanPayload],
                  min_score: int = 3, deterministic_failures: list[str] | None = None) -> Decision:
    issues: list[str] = list(deterministic_failures or [])
    findings: list[dict] = []
    rc = report.reference_comparison
    if not report.criteria and rc is not None:
        # Reference-only evaluation: the comparison with the reference localization is the sole criterion.
        if rc.result == 'unknown':
            issues.append('QUALITY_UNKNOWN')
        elif rc.result == 'incompatible':
            for d in rc.differences:
                if d.classification == 'candidate_defect':
                    code = comparison_issue_code(d)
                    findings.append({'criterion': 'reference_comparison', 'issue_code': code,
                                     'severity': 'critical' if d.critical else 'substantive', 'affected_ids': [],
                                     'explanation': d.explanation, 'correction_goal': d.correction_goal})
                    issues.append(code)
    for c in report.criteria:
        for f in c.findings:
            findings.append({'criterion': c.criterion, 'issue_code': f.issue_code, 'severity': f.severity,
                             'affected_ids': f.affected_ids, 'explanation': f.explanation,
                             'correction_goal': f.correction_goal})
        if c.status == 'not_applicable':
            if report.mode == 'candidate_quality':
                issues.append(CRITERION_ISSUE[c.criterion])
            continue
        if c.status == 'unknown':
            issues.append('QUALITY_UNKNOWN')
        elif c.status == 'fail' or c.score is None or c.score < min_score:
            issues.append(CRITERION_ISSUE[c.criterion])
        if any(f.severity == 'critical' for f in c.findings):
            code = CRITERION_ISSUE[c.criterion]
            if code not in issues:
                issues.append(code)
    issues = list(dict.fromkeys(issues))
    if issues:
        return Decision('unresolved', issues, [], findings)
    fallbacks = [f'{d.item_id}: {d.fallback_reason}' for p in plans for d in p.decisions
                 if d.strategy == 'retain' and d.fallback_reason]
    if fallbacks:
        return Decision('conservative', [], fallbacks, findings)
    return Decision('accepted', [], [], findings)
