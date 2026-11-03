"""Issue codes and pipeline exceptions (guide §14)."""
from __future__ import annotations

ISSUE_CODES = {
    'PAIR_ID_INVALID', 'DUPLICATE_ID', 'MISSING_PARTNER', 'EXTRACTION_FAILED',
    'OCR_REQUIRED', 'CONTEXT_OVERFLOW', 'ALIGNMENT_UNRESOLVED', 'PROFILE_INCOMPLETE',
    'MEMORY_REFERENCE_INVALID', 'SCOPE_MISMATCH', 'UNSUPPORTED_SUBSTITUTION',
    'PLAN_CONFLICT', 'PLAN_MISMATCH', 'MEANING_VIOLATION', 'ARABIC_QUALITY_FAILED',
    'SCHEMA_INVALID', 'PROVIDER_FAILED', 'PROVIDER_REFUSAL', 'BUDGET_EXHAUSTED',
    'QUALITY_UNKNOWN', 'CULTURAL_FIT_FAILED', 'COVERAGE_INCOMPLETE', 'QUOTE_UNRESOLVED',
    'CONFIG_INVALID', 'INPUT_INVALID', 'REFERENCE_INCOMPATIBLE',
}


class GlocalInductError(Exception):
    """Base class for all expected errors. Carries a machine-readable issue code."""
    code = 'INPUT_INVALID'

    def __init__(self, message: str = '', code: str | None = None, details: dict | None = None):
        super().__init__(message or self.code)
        if code is not None:
            self.code = code
        self.details = details or {}


class ExpectedPipelineError(GlocalInductError):
    """Any expected failure that must turn a request into `unresolved`."""


class ConfigError(GlocalInductError):
    code = 'CONFIG_INVALID'


class ExtractionError(ExpectedPipelineError):
    code = 'EXTRACTION_FAILED'


class ContextOverflow(ExpectedPipelineError):
    code = 'CONTEXT_OVERFLOW'


class SchemaInvalid(ExpectedPipelineError):
    code = 'SCHEMA_INVALID'


class ProviderFailed(ExpectedPipelineError):
    code = 'PROVIDER_FAILED'


class ProviderRefusal(ExpectedPipelineError):
    code = 'PROVIDER_REFUSAL'


class BudgetExhausted(ExpectedPipelineError):
    code = 'BUDGET_EXHAUSTED'


class QuoteUnresolved(ExpectedPipelineError):
    code = 'QUOTE_UNRESOLVED'


class PlanConflict(ExpectedPipelineError):
    code = 'PLAN_CONFLICT'


class PlanMismatch(ExpectedPipelineError):
    code = 'PLAN_MISMATCH'


class UnsupportedSubstitution(ExpectedPipelineError):
    code = 'UNSUPPORTED_SUBSTITUTION'


class MemoryReferenceInvalid(ExpectedPipelineError):
    code = 'MEMORY_REFERENCE_INVALID'


class CoverageIncomplete(ExpectedPipelineError):
    code = 'COVERAGE_INCOMPLETE'


class ProfileIncomplete(ExpectedPipelineError):
    code = 'PROFILE_INCOMPLETE'
