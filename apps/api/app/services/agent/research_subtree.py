from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import ip_address
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

from app.config import get_settings
from app.services.agent import model_client
from app.services.agent.models import TurnRequest, Source, ToolCall, new_id
from app.services.agent.prompt_library import resolve_prompt

# All Pydantic models live in research_models.py (TD-01 extraction).
# Re-imported here so external callers continue to use the same module path.
from app.services.agent.research_models import (  # noqa: F401
    _RESEARCH_PROFILES,
    _merge_source_detail,
    ArchitectureExtractionCard,
    CitationVerification,
    ClaimVerification,
    CoverageCell,
    CoverageContract,
    DeepLinkCandidate,
    EvidenceClaim,
    EvidenceItem,
    EvidencePack,
    JudgeVerdict,
    PROFILE_POLICIES,
    RankedSource,
    ReflectionDecision,
    ResearchAgentDefinition,
    ResearchAgentId,
    ResearchAgentRegistry,
    ResearchBrief,
    ResearchBudget,
    ResearchBudgetLedger,
    ResearchFeedbackLoop,
    ResearchGoal,
    ResearchJudgeResult,
    ResearchPlan,
    ResearchProfile,
    ResearchProfilePolicy,
    ResearchPromptTemplate,
    ResearchStateStore,
    SearchWorkerPlan,
    SearchWorkerReport,
)

logger = logging.getLogger(__name__)

MAX_PARALLEL_READ_BATCHES = 4
MAX_PARALLEL_READ_BATCHES_DEEP = 6
MAX_URLS_PER_READ_BATCH = 6

# Profile + brief functions live in research_profiles.py (TD-01 extraction).
# Re-imported here for backward compat — existing callers can still use this module path.
from app.services.agent.research_profiles import (  # noqa: F401, E402
    BRIEF_PROMPT,
    PLAN_PROMPT,
    REPAIR_PROMPT,
    SYNTHESIS_PROMPT,
    _apply_profile_decision_guardrails,
    _fallback_success_criteria,
    _implementation_signal,
    _regulatory_signal,
    _request_for_research_objective,
    _secondary_profiles_for,
    _strategy_signal,
    _technical_signal,
    _vendor_comparison_signal,
    create_research_goal,
    generate_research_brief,
    get_research_registry,
    infer_research_profile,
    research_budget_for,
)

# Coverage contract functions live in research_contracts.py (TD-01 extraction).
from app.services.agent.research_contracts import (  # noqa: F401, E402
    COVERAGE_CONTRACT_PROMPT,
    _count_comparison_dimensions,
    _derive_fallback_dimensions,
    _derive_fallback_subjects,
    _extract_named_comparison_subjects,
    _implementation_plan_contract,
    _is_multi_subject_comparison,
    _market_landscape_contract,
    _policy_regulatory_contract,
    _strategy_brief_contract,
    _technical_architecture_contract,
    _vendor_comparison_contract,
    generate_coverage_contract,
)

# Pure utility functions live in research_utils.py (TD-01 extraction).
from app.services.agent.research_utils import (  # noqa: F401, E402
    _clean_urls,
    _dedupe,
    _estimate_relevance,
    _extract_urls_from_text,
    _looks_like_substantive_claim,
    _parse_json,
    classify_source_type,
    score_source_authority,
    score_technical_density,
)

# Planning, reflection, and judge logic live in research_planner.py (TD-01 extraction).
from app.services.agent.research_planner import (  # noqa: F401, E402
    CITATION_VERIFICATION_PROMPT,
    REFLECTION_PROMPT,
    _cell_terms,
    _citation_repair_instruction,
    _compact_search_subject,
    _compose_deep_worker_wave,
    _dedupe_workers,
    _domain_discovery_workers,
    _domain_for_query,
    _evidence_supports_cell,
    _fallback_plan,
    _implementation_plan_anchor_queries,
    _llm_vendor_comparison_subject,
    _longform_timeout_s,
    _market_landscape_anchor_queries,
    _max_attempts_per_cell,
    _max_iterations_for,
    _meaningful_tokens,
    _normalize_plan,
    _plan_preview_investigation_items,
    _plan_preview_source_strategy,
    _plan_preview_title,
    _policy_regulatory_anchor_queries,
    _profile_from_contract,
    _public_technical_subject,
    _strategy_brief_anchor_queries,
    _subject_phrase_is_useful,
    _targeted_query,
    _tech_arch_anchor_queries,
    _tech_arch_grounding_term,
    _text_supports_cell,
    _clean_search_subject_phrase,
    _extract_search_subject_phrase,
    _get_registry,
    _vendor_comparison_anchor_queries,
    build_research_plan_preview,
    judge_research_final,
    plan_from_brief_contract,
    plan_from_contract,
    plan_from_targeted_queries,
    plan_research,
    reflect,
    update_contract_from_evidence,
    verify_citations_semantically,
)



# Evidence binding, claim extraction, and passage scoring live in research_evidence.py (TD-01 extraction).
from app.services.agent.research_evidence import (  # noqa: F401, E402
    _AGENT_ROLE_TERMS,
    _FAILURE_MODE_TERMS,
    _STATE_OBJECT_TERMS,
    _TOOL_RENDERER_TERMS,
    bind_evidence,
    detect_contradictions,
    extract_architecture_cards,
    extract_evidence_claims,
    _architecture_card_confidence,
    _architecture_card_has_signal,
    _architecture_system_name,
    _best_architecture_quote,
    _candidate_passages,
    _chunk_long_passage,
    _claim_candidate_sentences,
    _claim_query_terms,
    _claim_role_for_text,
    _claim_type_for_text,
    _claim_type_priority,
    _extract_architecture_pattern,
    _extract_architecture_terms,
    _extract_metric_snippets,
    _extract_validation_loop,
    _freshness_risk_for_text,
    _lesson_for_agentdeck,
    _max_claims_for_item,
    _passage_confidence,
    _passage_signature,
    _score_claim_sentence,
    _score_passage,
    _select_evidence_passages,
)

# Answer synthesis, ranking, and source utilities live in research_synthesis.py (TD-01 extraction).
from app.services.agent.research_synthesis import (  # noqa: F401, E402
    build_gap_followup_workers,
    build_synthesis_prompt,
    extract_deep_link_candidates,
    is_public_source_url,
    judge_research,
    rank_sources,
    repair_research_answer,
    source_context_from_evidence,
    synthesize_answer,
    _architecture_cards_context,
    _arxiv_id_from_url,
    _domain_specific_link_candidates,
    _select_diverse_ranked_sources,
    _source_inventory_summary,
    _synthesis_report_contract,
    _synthesis_token_budget,
)

# Lead orchestration, worker execution, and main research loop live in research_lead.py (TD-01 extraction).
from app.services.agent.research_lead import (  # noqa: F401, E402
    LeadResearchAgent,
    lead_research_loop,
    verify_claims,
    _apply_source_provenance,
    _assigned_cell_for_worker,
    _chunk_urls,
    _ensure_source_provenance,
    _evidence_quality_issues,
    _framework_gap_queries,
    _framework_remediation_sources,
    _max_parallel_read_batches_for,
    _read_cap_for_batch,
    _retry_query_for_worker,
    _source_relevance_for_worker,
    _specificity_rewrite_issues,
    _worker_claim_pack,
    _worker_confidence,
    _worker_missing_evidence,
    _worker_report_from_sources,
    _worker_report_message,
)
