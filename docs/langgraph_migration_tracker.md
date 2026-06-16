# Fronei LangGraph / Agentic Runtime Migration Tracker

## North Star

Every Fronei turn should follow one explicit, inspectable control flow:

```text
load context -> agentic triage -> full planner if needed -> gate / human input
-> execute selected tools -> final response or artifact -> trace + metrics
```

Simple turns should feel instant. Ambiguous or sensitive turns should pause for
the right user decision. Long research and document jobs should be durable,
progressive, and observable.

## Current Status

Status legend: `todo`, `in_progress`, `done`, `blocked`.

| Phase | Status | Scope | Notes |
|---|---:|---|---|
| 0 | done | Baseline, tracker, canonical graph state | Tracker, state, adapters, golden cases, and rollback flags are in place. |
| 1 | done | Feature-flagged graph shell around existing pipeline | Shell, shadow trace, and admin trace exposure are in place. |
| 2 | done | Triage / planner / gate graph nodes | Pure nodes built; shadow trace runs the planning sequence; high-confidence no-tool simple answers can use graph canary behind `turn_graph_enabled`. |
| 3 | done | Tool registry | Internal typed registry added; graph gate selects tool contracts; `answer_directly` has a tested execution adapter. |
| 4 | done | Research subgraph | Current research engine wrapped behind graph tool contract; callable stage nodes added for future monolith split. |
| 5 | done | Document / AgentDeck subgraph | Stage nodes and tool adapters added; main document path can use adapters behind `turn_graph_enabled`. |
| 6 | done | MCP adapters | Adapter catalog maps registered tools to candidate MCP backends. |
| 7 | done | Admin graph observability | Admin turn rows expose graph trace plus summary path/timing/tools/canary. |
| 8 | done | Cutover / cleanup | Central rollout helper enforces kill switch and answer-only canary; full backend suite is green. |

## Multi-Agent v2 Architecture

The next architecture layer is documented in
[`docs/multi_agent_orchestrator_architecture.md`](multi_agent_orchestrator_architecture.md).
It extends this graph migration into a clean runtime boundary for a full
orchestrator-led multi-agent system with configurable agents, prompts, goals,
judges, tools, and first-class guardrails. The existing app shell remains in
place; old planner/research/document branches are replaced path by path and
deleted after stable cutover.

New roadmap phases:

- [x] A - Clean runtime and policy foundation.
- [x] B - Guardrail control plane.
- [ ] C - Agent and prompt registry.
- [ ] D - Orchestrator agent.
- [ ] E - Deep research multi-agent subtree.
- [ ] F - Document multi-agent subtree.
- [ ] G - Admin dashboard.
- [ ] H - Feedback loop.
- [ ] I - Cutover and cleanup.

## Phase 0 Checklist

- [x] Create living migration tracker.
- [x] Define initial `TurnGraphState`.
- [x] Define graph event/node timing types.
- [x] Map existing pipeline fields to `TurnGraphState`.
- [x] Add golden prompt set for graph routing behavior.
- [x] Document cutover/rollback flags.

## Phase 1 Checklist

- [x] Add `turn_graph_enabled=false` feature flag.
- [x] Add graph shell module with `run_turn_graph_shell()`.
- [x] Ensure graph shell can execute without LangGraph installed.
- [x] Add unit tests for graph shell state/timing behavior.
- [x] Wire graph shell in shadow mode from the live chat path.
- [x] Add graph trace fields to admin turn profiler.

## Phase 2 Checklist

- [x] Add pure `load_context` graph node.
- [x] Add pure `triage` graph node wrapping deterministic continuation + LLM triage.
- [x] Add pure `planner` graph node wrapping current full planner.
- [x] Add pure `gate` graph node wrapping current plan gate.
- [x] Wire shadow trace to the real planning node sequence.
- [x] Compare graph node output against current `PipelineSetup` in tests.
- [x] Promote graph nodes to drive execution for a narrow canary path.

## Phase 3 Checklist

- [x] Define internal tool contract model.
- [x] Register core tools: answer, ask user, web, research, document, render, QA, memory, templates.
- [x] Map planner/gate capability state to selected tools.
- [x] Add tool execution adapters for the first canary tool.
- [x] Add MCP adapter boundary after internal contracts stabilize.

## MCP Adapter Boundary

Internal tools remain the graph's source of truth. MCP adapters should be thin
implementations behind a `TurnToolDef`, not separate routing concepts. The
graph selects `web_context`, `load_templates`, `render_artifact`, etc.; the
tool implementation may later call an MCP server or external connector when
that is the best backend. This preserves one planner/tool vocabulary while
allowing MCP to power specific capabilities.

## Phase 6 Checklist

- [x] Define MCP adapter contract.
- [x] Map registered tools to candidate MCP backends.
- [x] Keep MCP behind tool implementations, not planner routing.

## Phase 7 Checklist

- [x] Expose raw graph trace on admin turn rows.
- [x] Add graph summary path/timing/tool rollup.
- [x] Expose answer-direct canary marker in admin turn rows.

## Phase 8 Checklist

- [x] Centralize rollout decision helper.
- [x] Keep `turn_graph_enabled=false` as full kill switch.
- [x] Limit graph-driven execution to `answer_directly` canary.
- [x] Run final focused + full backend validation.
- [x] Mark rollout tracker complete after validation.

## Phase 4 Checklist

- [x] Define research subgraph stage vocabulary.
- [x] Map current research progress events to graph stages.
- [x] Add `deep_research` tool adapter around current `run_research()` contract.
- [x] Wire research tool adapter into live durable job path behind `turn_graph_enabled`.
- [x] Split current monolith into callable stage nodes once trace parity is stable.

## Phase 5 Checklist

- [x] Define document/AgentDeck stage vocabulary.
- [x] Add callable stage nodes for content plan, design plan, render, QA polish, final preview.
- [x] Add `generate_document` tool adapter.
- [x] Add `render_artifact` tool adapter.
- [x] Wire document adapters into live document path behind `turn_graph_enabled`.
- [x] Add QA polish adapter around current render/vision checks.

## Proposed Graph Nodes

| Node | Purpose | Existing code reused |
|---|---|---|
| `load_context` | Resolve conversation, memory, profile, active task | `conversations.py`, `personal_context.py` |
| `triage` | Cheap action decision: answer, ask, web/research/doc suggestion, or full planner | `chat_pipeline._run_fast_turn_triage` initially |
| `planner` | Full planner for non-trivial or risky turns | `planner.run_planner` |
| `gate` | Decide auto vs user confirmation/clarification | `plan_gate.evaluate` |
| `ask_user` | Interrupt/resume point for questions and capability choices | existing `plan_proposed` flow |
| `answer_directly` | Normal chat answer | `invoke_llm` path |
| `web_context` | Fast web context when accepted/needed | `web_context.gather_web_context` |
| `deep_research` | Durable research workflow | `research_orchestrator.run_research` |
| `document_generation` | Generate body/plan | `generate_document_output` |
| `artifact_render` | Render DOCX/XLSX/PPTX | `build_document_artifact` |
| `qa_polish` | Deferred/strict quality checks | `qa`, `pptx_render_qa` |
| `persist_result` | Save messages, turns, logs, memory | `conversations.py` |

## Tool Boundary Plan

Internal Python tools first:

- `answer_directly`
- `ask_user`
- `load_memory`
- `load_templates`
- `web_context`
- `deep_research`
- `generate_document`
- `render_artifact`
- `run_quality_check`

MCP candidates after the internal tool contract stabilizes:

- web/search providers
- external file/template storage
- Google Drive/Gmail
- enterprise knowledge stores
- long-term memory connectors

## Open Decisions

- Whether to use LangGraph checkpointers backed by Postgres directly or keep
  `ConversationTurn` as the persistence source of truth and store graph traces
  as turn metadata.
- Whether graph execution should replace the current durable thread worker or
  run inside it initially.
- How much of AgentDeck should be one graph versus nested subgraphs.

## Cutover / Rollback Flags

- `turn_graph_enabled=false` is the primary kill switch. While false, the live
  request path must continue using the current pipeline.
- Shadow-mode wiring should write graph traces beside current lifecycle/progress
  logs without controlling user-visible output. Current implementation stores
  graph traces in turn lifecycle JSON and exposes latest trace from the admin
  Turn API.
- Rollback path for each slice: flip `turn_graph_enabled=false`; keep adapter
  and tracker files because they are inert without live router wiring.

## Latest Updates

- 2026-06-15: Implemented Phase B guardrail control plane: deterministic guardrail service, guardrail/goal/agent-run tables, shadow graph hook, admin guardrail-events endpoint, adapters, migration, and offline tests. Full backend suite: 392 passed, 4 skipped.
- 2026-06-15: Implemented Phase A foundation: new inert `agent_runtime` package, core runtime schemas, file-backed default agents/prompts/model policies/tools/guardrails, compatibility context/goal adapters, registry loader, and focused tests.
- 2026-06-15: Closed architecture consistency gaps: added missing document agents, triage placement, `ToolDefinition`, typed runtime budget, direct-with-web tool path, goal locks, Phase A schema scope, and CI eval DoD.
- 2026-06-15: Refined multi-agent architecture with concurrency policy, failure taxonomy, memory invocation rules, MCP boundary, model policies, durable jobs, tenant isolation, latency targets, early eval gates, and product outcome mapping.
- 2026-06-15: Added multi-agent orchestrator architecture and roadmap with guardrails as a first-class control layer.
- 2026-06-15: Completed Phase 8 after full backend validation: 375 passed, 4 skipped.
- 2026-06-15: Added MCP adapter catalog, admin graph summary rollups, and centralized graph rollout guardrails.
- 2026-06-15: Wired main document generation/render branch through graph document adapters behind `turn_graph_enabled`; research-followup document path remains on current pipeline for now.
- 2026-06-15: Added QA polish adapter for artifact quality-check results.
- 2026-06-15: Started Phase 5 with document/AgentDeck stage nodes and tool adapters for document generation and artifact rendering.
- 2026-06-15: Added callable research stage nodes (`decompose/search/crawl/extract/sufficiency/synthesize/verify`) as the seam for gradually splitting the research monolith.
- 2026-06-15: Wired `deep_research` graph tool adapter into the live full-research worker behind `turn_graph_enabled`, preserving existing UI progress and downstream result handling.
- 2026-06-15: Started Phase 4 with research subgraph stage mapping and `deep_research` tool adapter around the existing research engine.
- 2026-06-15: Documented MCP boundary: MCP is an implementation backend for registered tools, not a second planner vocabulary.
- 2026-06-15: Added first internal tool execution adapter for `answer_directly`; live routing still uses existing execution path.
- 2026-06-15: Started Phase 3 with internal typed tool registry and deterministic selected-tool mapping from planner/gate state.
- 2026-06-15: Added graph-driven canary for high-confidence `answer_directly` turns with no enabled tools; web/research/document/confirmation paths remain on the current pipeline.
- 2026-06-15: Added shadow-vs-current-pipeline comparison test for the shared planner/gate contract.
- 2026-06-15: Switched live shadow trace from placeholder shell to real planning node sequence while keeping current pipeline authoritative.
- 2026-06-15: Started Phase 2 with pure graph nodes for `load_context`, `triage`, `planner`, and `gate`, plus a planning shadow graph helper.
- 2026-06-15: Wired feature-flagged graph shadow traces into normal chat and execute-plan turn creation; admin turn rows now expose latest `graph_trace`.
- 2026-06-15: Added routing golden-set fixture covering simple chat, current web-sensitive answers, ambiguous research, research-to-presentation, and document-output override behavior.
- 2026-06-15: Added zero-schema adapters for building `TurnGraphState` from existing conversation/turn records and serializing graph traces for future lifecycle/admin views.
- 2026-06-15: Created tracker and started Phase 0/1 foundation.
