"""Service wiring: builds every component from a Config."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agents.roles import Agents
from .agents.runner import AgentRunner, Budget, PromptLibrary
from .config import Config, load_config
from .memory import ExperienceMemory, InsightMemory, PrivateReferenceStore, ReleaseManager, Repository, TextStore
from .memory.releases import Release
from .providers.base import Provider, make_provider
from .retrieval import MemorySearch
from .retrieval.embedding import Embedder, make_embedder
from .validation.policies import DomainPolicies, load_policies

EMPTY_RELEASE_ID = 'M-EMPTY'


@dataclass
class Services:
    config: Config
    repo: Repository
    texts: TextStore
    private: PrivateReferenceStore
    experiences: ExperienceMemory
    insights: InsightMemory
    releases: ReleaseManager
    embedder: Embedder
    search: MemorySearch
    provider: Provider
    evaluation_provider: Provider
    prompts: PromptLibrary
    policies: DomainPolicies
    runner: AgentRunner
    agents: Agents

    def budget(self, n_windows: int = 1) -> Budget:
        cfg = self.config
        calls = cfg.budgets.max_calls_per_document or cfg.budgets.max_calls_per_window * max(1, n_windows)
        deadline = cfg.budgets.short_request_seconds
        if n_windows > 1 and cfg.budgets.long_document_seconds:
            deadline = cfg.budgets.long_document_seconds
        return Budget(max_calls=calls, deadline_seconds=deadline, max_tokens=cfg.budgets.max_tokens_per_document,
                      context_window_tokens=cfg.runtime.context_window_tokens or 32000,
                      reserved_output_tokens=cfg.runtime.reserved_output_tokens or 4000)

    def version_hashes(self) -> dict[str, str]:
        return {'prompts': self.prompts.version_hash, 'policy': self.policies.policy_hash,
                'config_schema': self.config.schema_version, 'embedder': self.embedder.name}

    def ensure_empty_release(self) -> Release:
        """A memory-less release (empty retrieval is valid) for first inference and demos."""
        rel = self.releases.get(EMPTY_RELEASE_ID)
        if rel is None:
            rel = self.releases.publish(EMPTY_RELEASE_ID, [], [], {'embedder': self.embedder.identity(),
                                                                   'experiences': [], 'insights': []},
                                        self.version_hashes(), run_id='bootstrap', activate=False)
        return rel

    def resolve_release(self, release_id: str | None) -> Release:
        if release_id:
            rel = self.releases.get(release_id)
            if rel is None:
                raise KeyError(f'release {release_id} not found')
            return rel
        return self.releases.active() or self.ensure_empty_release()

    def close(self) -> None:
        self.repo.close()


def build_services(config: Config | None = None, provider: Provider | None = None,
                   evaluation_provider: Provider | None = None, embedder: Embedder | None = None,
                   db_path: str | Path | None = None) -> Services:
    cfg = config or load_config()
    repo = Repository(db_path or cfg.path('database'))
    texts = TextStore(cfg.path('source_store'))
    private = PrivateReferenceStore(cfg.path('private_references'))
    experiences = ExperienceMemory(repo)
    insights = InsightMemory(repo, experiences, cfg.insights.min_support_groups, cfg.insights.universal_min_domains,
                             cfg.insights.initial_importance, cfg.learning.insight_admission == 'immediate')
    releases = ReleaseManager(repo, experiences, insights, cfg.path('releases'))
    rc = cfg.retrieval
    emb = embedder or make_embedder(rc)
    search = MemorySearch(experiences, insights, texts, emb, rc.insights, rc.experiences, rc.candidate_pool,
                          rc.similarity_threshold, cfg.near_duplicate_jaccard, rc.query_mode, rc.knn_backend,
                          rc.insight_scope_filter)
    prov = provider or make_provider(cfg, 'generation')
    eval_prov = evaluation_provider or (prov if provider is not None else make_provider(cfg, 'evaluation'))
    prompts = PromptLibrary(cfg.path('prompts'))
    policies = load_policies(cfg.path('domain_policies'))
    runner = AgentRunner(prov, prompts, policies.policy_hash, cfg.reliability.technical_retries,
                         cfg.reliability.schema_corrections, eval_prov)
    agents = Agents(runner, policies)
    return Services(cfg, repo, texts, private, experiences, insights, releases, emb, search, prov, eval_prov, prompts,
                    policies, runner, agents)
