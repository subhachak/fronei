# Domain Function Side-Effect Audit — Slice 0B

> **Purpose:** Before wiring any domain function into a real LangGraph node, every
> function must be classified by its side-effect profile.  Functions with external
> I/O (LLM, web, DB) are only called inside the node that owns them; functions
> that mutate shared state must not be called from parallel branches; pure
> functions may be called freely from any node.
>
> **Policy:** Nodes in the same parallel fan-out MUST NOT call functions classified
> as `state_mutate`.  Functions classified as `llm_call`, `web_call`, or
> `db_write` are billable; they must only execute when the budget gate has
> returned `CONTINUE` or `CONTINUE_WITH_REDUCED_SEARCH`.

---

## Classification key

| Tag | Meaning |
|-----|---------|
| `pure` | No I/O, no mutation.  Safe to call from any node or thread. |
| `state_mutate` | Mutates a shared `ResearchStateStore` object.  Not safe in parallel branches. |
| `llm_call` | Calls an LLM (via `model_client`).  Billable; subject to budget gate. |
| `web_call` | Issues HTTP / search requests to external services.  Billable; subject to budget gate. |
| `db_write` | Writes to the application database.  Not safe to retry without idempotency key. |
| `cpu_bound` | Expensive CPU work (regex, ranking, embedding).  Safe to thread-pool; no external I/O. |

---

## Retired pre-LangGraph lead runtime

The previous `research_lead.py` implementation has been retired. Production
research dispatch now enters the LangGraph runtime directly, so new side-effect
audits should classify the functions owned by the active node modules below.

| Function | Module | Classification | Former owning node | Notes |
|----------|--------|----------------|--------------------|-------|
| `_chunk_urls` | research_lead | `removed` | read | Helper; no I/O. |
| `_max_parallel_read_batches_for` | research_lead | `removed` | read | Policy lookup. |
| `_read_cap_for_batch` | research_lead | `removed` | read | Arithmetic only. |
| `_canonical_framework_sources` | research_lead | `removed` | bind | Built Source list from request data; no I/O. |
| `_normalized_url` | research_lead | `removed` | any | URL string normalisation. |
| `_source_text_for_url` | research_lead | `removed` | bind | Read from state dict; no mutation. |
| `_prioritized_sources_for_binding` | research_lead | `removed` | bind | Sorted/filtered existing state list. |
| `_framework_remediation_sources` | research_lead | `removed` | bind | Built remediation list. |
| `_evidence_quality_issues` | research_lead | `removed` | judge | Analysis only. |
| `_framework_gap_queries` | research_lead | `removed` | synthesize | Computed gap query strings. |
| `_generic_remediation_queries` | research_lead | `removed` | repair | Computed remediation strings. |
| `_assigned_cell_for_worker` | research_lead | `removed` | search | Lookup. |
| `_retry_query_for_worker` | research_lead | `removed` | search | Built retry query string. |
| `_worker_report_from_sources` | research_lead | `removed` | bind | Built report object. |
| `_worker_report_message` | research_lead | `removed` | bind | Formatted log message. |
| `_source_inventory_summary` | research_lead | `removed` | source_inventory | Aggregated URL list. |
| `verify_claims` | research_lead | `removed` | verify | Called LLM to verify citation accuracy. |
| `LeadResearchAgent._run_search_wave` | research_lead | `removed` | search | Issued web search queries via tools. |
| `LeadResearchAgent._dispatch_worker_wave` | research_lead | `removed` | search | Dispatched parallel search workers and wrote worker reports. |
| `LeadResearchAgent._bind_state_evidence` | research_lead | `removed` | bind | Mutated `state.evidence`; unsafe in parallel. |
| `LeadResearchAgent._expand_source_graph` | research_lead | `removed` | expand_source_graph | Fetched deep-link URLs and called `state.add_sources()`. |
| `LeadResearchAgent._follow_deep_links` | research_lead | `removed` | expand_source_graph | Similar to `_expand_source_graph`; legacy call site. |
| `LeadResearchAgent._escalate_starved_subjects` | research_lead | `removed` | search | Dispatched targeted escalation queries; could mutate state. |
| `LeadResearchAgent._remediate_weak_evidence_if_needed` | research_lead | `removed` | repair | Issued gap-fill searches. |
| `lead_research_loop` | research_lead | `removed` | top-level | Outer loop that wrapped all retired lead-runtime behavior. |

---

## research_synthesis.py

| Function | Module | Classification | Owning node | Notes |
|----------|--------|----------------|-------------|-------|
| `synthesize_answer` | research_synthesis | `llm_call` | synthesize | Single LLM call; streams result. |
| `judge_research` | research_synthesis | `llm_call` | judge | LLM-based quality judge. |
| `repair_research_answer` | research_synthesis | `llm_call` | repair | LLM-based answer repair. |
| `rank_sources` | research_synthesis | `cpu_bound` | rank | Pure ranking; no I/O. |
| `_select_diverse_ranked_sources` | research_synthesis | `pure` | rank | Selects from ranked list. |
| `extract_deep_link_candidates` | research_synthesis | `pure` | expand_source_graph | Parses source content; no I/O. |
| `build_gap_followup_workers` | research_synthesis | `pure` | repair | Builds gap-fill worker plans. |
| `build_synthesis_prompt` | research_synthesis | `pure` | synthesize | Prompt construction only. |
| `is_public_source_url` | research_synthesis | `pure` | any | URL filter; no I/O. |

---

## research_planner.py

| Function | Module | Classification | Owning node | Notes |
|----------|--------|----------------|-------------|-------|
| `plan_research` | research_planner | `llm_call` | plan | LLM call to build `ResearchPlan`. |
| `plan_from_contract` | research_planner | `pure` | plan | Deterministic plan from contract; no I/O. |
| `plan_from_brief_contract` | research_planner | `pure` | plan | Deterministic plan; no I/O. |
| `plan_from_targeted_queries` | research_planner | `pure` | repair | Builds plan from targeted queries. |
| `update_contract_from_evidence` | research_planner | `state_mutate` | bind | Mutates `state.contract`.  Must run serially. |
| `reflect` | research_planner | `llm_call` | judge | LLM-based reflection / loop decision. |
| `verify_citations_semantically` | research_planner | `llm_call` | verify | LLM semantic citation check. |
| `judge_research_final` | research_planner | `llm_call` | judge | Final quality verdict; LLM call. |

---

## research_evidence.py

| Function | Module | Classification | Owning node | Notes |
|----------|--------|----------------|-------------|-------|
| `classify_claims_llm` | research_evidence | `llm_call` | classify_claims | LLM call.  Billable; budget gate applies. |
| `bind_evidence` | research_evidence | `cpu_bound` | bind | Extracts and binds evidence; no external I/O; mutates `EvidencePack`. |
| `extract_evidence_claims` | research_evidence | `cpu_bound` | bind | Text processing; no I/O. |
| `extract_architecture_cards` | research_evidence | `cpu_bound` | bind | Text processing; no I/O. |

---

## research_contracts.py / research_models.py

| Function / Class | Module | Classification | Owning node | Notes |
|-----------------|--------|----------------|-------------|-------|
| `ResearchBudgetLedger.record_tool_call` | research_models | `state_mutate` | search / read | Mutates ledger in place. |
| `ResearchBudgetLedger.can_start_tool` | research_models | `pure` | budget_gate | Read-only predicate. |
| `ResearchBudgetLedger.remaining_tool_calls` | research_models | `pure` | budget_gate | Read-only. |
| `ResearchBudgetLedger.remaining_source_reads` | research_models | `pure` | budget_gate | Read-only. |
| `CoverageContract` (construction) | research_contracts | `llm_call` | contract | LLM call to derive subject/cell structure. |

---

## tools.py (external tool calls)

| Function | Classification | Owning node | Notes |
|----------|----------------|-------------|-------|
| `ResearchTools.web_search` | `web_call` | search | Issues search query to Tavily / DDGS. |
| `ResearchTools.extract_urls` | `web_call` | read | Fetches and extracts URL content. |

---

## Budget gate ruling summary

| Classification | Budget gate required? | Parallel-safe? |
|----------------|-----------------------|----------------|
| `pure` | No | Yes |
| `cpu_bound` | No | Yes |
| `state_mutate` | No | **No** (serial only) |
| `llm_call` | **Yes** | No |
| `web_call` | **Yes** | Conditionally (search workers run in a pool but under a shared ledger cap) |
| `db_write` | No (separate idempotency concern) | No |

---

*Last updated: Slice 0B.  Update this table when new domain functions are introduced
or existing functions change their I/O profile.*
