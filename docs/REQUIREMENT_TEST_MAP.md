# Requirement → test map

Guide section → implementing module → test(s). All tests run offline with the fake provider.

| Guide § | Requirement | Module | Test |
|---|---|---|---|
| §4.1 | Exact string-ID pairing, leading zeros, differing titles, duplicate/missing partners, metadata rows | `ingestion/pairing.py` | `test_foundation::test_pairing_is_exact_string_id_with_leading_zeros_and_differing_titles` |
| §4.2 | TXT/MD/DOCX/PDF typed blocks, tables as rows, instructions are data, OCR_REQUIRED | `ingestion/extractors.py` | `test_markdown_extraction_preserves_structure…`, `test_docx_and_pdf_adapters` |
| §4.2 | NFC/LF, diacritics kept, code-point spans, exact quote + occurrence, no fuzzy | `ingestion/normalize.py`, `validation/spans.py` | `test_arabic_normalization…`, `test_repeated_quote_requires_occurrence…` |
| §4.3 | Union-find grouping, 5-token shingles ≥ 0.85, frozen reproducible group split, groups never split | `ingestion/grouping.py`, `ingestion/splits.py` | `test_grouping_and_frozen_split`, `test_learning::test_ingest_and_split…` |
| §4.4 | Owner windows, read-only context, overflow, sentence splitting | `ingestion/windows.py` | `test_windows_single_owner_and_context_and_overflow` |
| §1 | Domain policy union, unknown domain → shared | `validation/policies.py` | `test_domain_policy_union_and_unknown_domain` |
| §5 | Canonical hashing, immutable objects | `hashing.py`, `memory/store.py` | `test_memory::test_objects_are_immutable_and_hashed`, pack `StorageChecks` |
| §6 | Reference vs attempt variants, failed example never a demonstration | `memory/experience.py`, `memory/releases.py` | `test_release_is_immutable_and_rejects_failed_examples`, `test_failed_reference_is_stored_but_never_a_demonstration` |
| §7 | ADD/UPVOTE/DOWNVOTE/EDIT, admission gates, one vote per comparison, stale batch atomic, unknown review ≠ pass, EDIT re-checks support, retire at 0 | `memory/insights.py` | `test_unsupported_insight_cannot_activate…`, `test_operation_cannot_vote_twice…`, `test_review_unknown_is_not_pass…`, `test_edit_does_not_inherit…` |
| §15 | Releases immutable, manifest = membership, staging changes do not alter published release | `memory/releases.py` | `test_release_is_immutable…`, `test_seed_practice_consolidate_pipeline` |
| §10 | Scope gates, excluded groups, inferred near-duplicate exclusion, budget truncation | `retrieval/*` | `test_seed_practice_consolidate_pipeline` (exclusion), `test_infer::test_no_memory_retention…` |
| §11 | One decision per item, essentiality guard, support membership + exact Arabic quote, dependency closure, frozen plan hash | `validation/plans.py` | `test_substituting_an_essential_item…`, `test_unsupported_counterpart_outside_retrieval…` |
| §12 | Block coverage, realization resolution, fixed-surface identity, assembly and hash, multi-window | `validation/candidates.py`, `workflows/infer.py` | `test_missing_block_coverage_and_invalid_quotes…`, `test_context_overflow_and_long_document_windows` |
| §13 | No weighted average; unknown → unresolved; critical blocks; conservative = safe retention + all pass | `validation/evaluation.py` | `test_unknown_quality_is_unresolved…`, `test_failed_criterion_and_critical_finding…`, `test_no_memory_retention_is_conservative…` |
| §14 | Technical retries (2, backoff) separate from semantic attempts; one schema correction; refusal; budgets; unresolved exposes no draft | `agents/runner.py` | `test_transient_retries…`, `test_schema_correction…`, `test_refusal_and_budget…`, pack `test_unresolved_exposes_no_draft` |
| §17 | Allowlisted bundles; no reference/rationale online; offline modes get the reference | `agents/bundles.py`, `validation/leakage.py` | `test_no_memory_retention…`, `test_seed_practice_consolidate_pipeline` |
| §8 | Seed admission → A2 → A5 → M0 | `workflows/seed.py`, `workflows/learning.py` | `test_seed_practice_consolidate_pipeline` |
| §9 | ≤3 attempts, source-only then reference-informed feedback, language-only repair keeps plan, identical candidate stop, error attempts | `workflows/practice.py` | `test_practice_repair_paths`, `test_attempt_limit_and_error_attempts` |
| §9 | Post-practice reference admission, packets, M1, M0 unchanged | `workflows/consolidate.py` | `test_seed_practice_consolidate_pipeline` |
| §9/§18 | Frozen predictions before comparison; denominators include unresolved | `workflows/evaluate.py` | `test_seed_practice_consolidate_pipeline` |
| §14 | Resume by cache key | `memory/store.py`, `workflows/common.py` | `test_resume_reuses_committed_stage_by_cache_key` |
| §16 | Null runtime fields reject real calls; secrets excluded | `config.py` | `test_config_loads_and_rejects_null_runtime…`, `test_providers::test_config_runtime_validation…` |
| Brief M5 | Provider adapters, schema subset | `providers/*` | `test_providers::*` (mocked clients) |
| Prototype | Paste/upload → Glocalize | `web/app.py`, `web/static/index.html` | `test_web::test_web_prototype_paste_upload_and_errors` |
