from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.db.models import AgentV3PromptTemplate, SessionLocal

logger = logging.getLogger(__name__)


class AgentV3PromptSpec(BaseModel):
    id: str
    agent_id: str
    system_prompt: str
    developer_prompt: str | None = None
    variables: list[str] = Field(default_factory=list)
    profile: str | None = None
    version: str = "1.0.0"
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedPrompt:
    id: str
    agent_id: str
    system_prompt: str
    developer_prompt: str | None = None
    variables: tuple[str, ...] = ()
    profile: str | None = None
    version: str = "1.0.0"
    source: str = "code"

    def telemetry(self) -> dict[str, str]:
        return {
            "prompt_id": self.id,
            "prompt_agent_id": self.agent_id,
            "prompt_version": self.version,
            "prompt_source": self.source,
            "prompt_profile": self.profile or "",
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _row_to_spec(row: AgentV3PromptTemplate) -> AgentV3PromptSpec:
    return AgentV3PromptSpec(
        id=row.id,
        agent_id=row.agent_id,
        profile=row.profile,
        version=row.version,
        status=row.status,
        system_prompt=row.system_prompt,
        developer_prompt=row.developer_prompt,
        variables=_loads(row.variables_json, []),
        metadata=_loads(row.metadata_json, {}),
    )


def _row_to_resolved(row: AgentV3PromptTemplate) -> ResolvedPrompt:
    return ResolvedPrompt(
        id=row.id,
        agent_id=row.agent_id,
        profile=row.profile,
        version=row.version,
        system_prompt=row.system_prompt,
        developer_prompt=row.developer_prompt,
        variables=tuple(_loads(row.variables_json, [])),
        source="db",
    )


def resolve_prompt(
    prompt_id: str,
    *,
    agent_id: str,
    fallback_system_prompt: str,
    variables: list[str] | None = None,
    profile: str | None = None,
) -> ResolvedPrompt:
    """Resolve an Agent v3 prompt from DB with safe code fallback."""

    db = SessionLocal()
    try:
        query = (
            db.query(AgentV3PromptTemplate)
            .filter(
                AgentV3PromptTemplate.agent_id == agent_id,
                AgentV3PromptTemplate.status == "active",
            )
        )
        if profile:
            row = (
                query.filter(AgentV3PromptTemplate.profile == profile)
                .order_by(AgentV3PromptTemplate.updated_at.desc())
                .first()
            )
            if row:
                return _row_to_resolved(row)
        row = db.get(AgentV3PromptTemplate, prompt_id)
        if row and row.status == "active":
            return _row_to_resolved(row)
        row = (
            query.filter(AgentV3PromptTemplate.profile.is_(None))
            .order_by(AgentV3PromptTemplate.updated_at.desc())
            .first()
        )
        if row:
            return _row_to_resolved(row)
    except Exception:
        logger.warning("Agent v3 prompt DB resolution failed for %s; using code fallback", prompt_id, exc_info=True)
    finally:
        db.close()
    return ResolvedPrompt(
        id=prompt_id,
        agent_id=agent_id,
        system_prompt=fallback_system_prompt,
        variables=tuple(variables or ()),
        profile=profile,
        source="code",
    )


def list_prompts() -> list[AgentV3PromptSpec]:
    db = SessionLocal()
    try:
        rows = (
            db.query(AgentV3PromptTemplate)
            .order_by(
                AgentV3PromptTemplate.agent_id.asc(),
                AgentV3PromptTemplate.profile.asc(),
                AgentV3PromptTemplate.status.desc(),
                AgentV3PromptTemplate.updated_at.desc(),
            )
            .all()
        )
        return [_row_to_spec(row) for row in rows]
    finally:
        db.close()


def upsert_prompt(spec: AgentV3PromptSpec) -> AgentV3PromptSpec:
    db = SessionLocal()
    try:
        row = db.get(AgentV3PromptTemplate, spec.id)
        now = _now()
        if row is None:
            row = AgentV3PromptTemplate(id=spec.id, created_at=now)
            db.add(row)
        row.agent_id = spec.agent_id
        row.profile = spec.profile
        row.version = spec.version
        row.status = spec.status
        row.system_prompt = spec.system_prompt
        row.developer_prompt = spec.developer_prompt
        row.variables_json = _dumps(spec.variables)
        row.metadata_json = _dumps(spec.metadata)
        row.updated_at = now
        if row.status == "active":
            for active in (
                db.query(AgentV3PromptTemplate)
                .filter(
                    AgentV3PromptTemplate.agent_id == row.agent_id,
                    AgentV3PromptTemplate.profile == row.profile,
                    AgentV3PromptTemplate.status == "active",
                    AgentV3PromptTemplate.id != row.id,
                )
                .all()
            ):
                active.status = "archived"
                active.updated_at = now
        db.commit()
        db.refresh(row)
        return _row_to_spec(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def seed_defaults() -> dict[str, int]:
    specs = default_prompt_specs()
    db = SessionLocal()
    counts = {"inserted": 0, "skipped": 0}
    try:
        for spec in specs:
            if db.get(AgentV3PromptTemplate, spec.id):
                counts["skipped"] += 1
                continue
            db.add(
                AgentV3PromptTemplate(
                    id=spec.id,
                    agent_id=spec.agent_id,
                    profile=spec.profile,
                    version=spec.version,
                    status=spec.status,
                    system_prompt=spec.system_prompt,
                    developer_prompt=spec.developer_prompt,
                    variables_json=_dumps(spec.variables),
                    metadata_json=_dumps(spec.metadata),
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            counts["inserted"] += 1
        db.commit()
        return counts
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def activate_prompt(prompt_id: str) -> AgentV3PromptSpec | None:
    db = SessionLocal()
    try:
        row = db.get(AgentV3PromptTemplate, prompt_id)
        if row is None:
            return None
        rows = (
            db.query(AgentV3PromptTemplate)
            .filter(
                AgentV3PromptTemplate.agent_id == row.agent_id,
                AgentV3PromptTemplate.profile == row.profile,
                AgentV3PromptTemplate.status == "active",
                AgentV3PromptTemplate.id != row.id,
            )
            .all()
        )
        now = _now()
        for active in rows:
            active.status = "archived"
            active.updated_at = now
        row.status = "active"
        row.updated_at = now
        db.commit()
        db.refresh(row)
        return _row_to_spec(row)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def rollback_prompt(prompt_id: str) -> AgentV3PromptSpec | None:
    db = SessionLocal()
    try:
        row = db.get(AgentV3PromptTemplate, prompt_id)
        if row is None:
            return None
        active = (
            db.query(AgentV3PromptTemplate)
            .filter(
                AgentV3PromptTemplate.agent_id == row.agent_id,
                AgentV3PromptTemplate.profile == row.profile,
                AgentV3PromptTemplate.status == "active",
            )
            .order_by(AgentV3PromptTemplate.updated_at.desc())
            .first()
        )
        previous = (
            db.query(AgentV3PromptTemplate)
            .filter(
                AgentV3PromptTemplate.agent_id == row.agent_id,
                AgentV3PromptTemplate.profile == row.profile,
                AgentV3PromptTemplate.status == "archived",
            )
            .order_by(AgentV3PromptTemplate.updated_at.desc(), AgentV3PromptTemplate.created_at.desc())
            .first()
        )
        if previous is None:
            return None
        now = _now()
        if active:
            active.status = "archived"
            active.updated_at = now
        previous.status = "active"
        previous.updated_at = now
        db.commit()
        db.refresh(previous)
        return _row_to_spec(previous)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def default_prompt_specs() -> list[AgentV3PromptSpec]:
    # Lazy imports keep the prompt library independent from the runtime modules
    # during app startup and migration imports.
    from app.services.agent_v3 import document_subtree, research_subtree

    return [
        AgentV3PromptSpec(
            id="agent_v3.research.brief.default",
            agent_id="research_brief",
            system_prompt=research_subtree.BRIEF_PROMPT,
            variables=["message", "conversation_context", "quality_mode", "research_level", "output_format"],
            metadata={"kind": "classifier"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.research.coverage_contract.default",
            agent_id="coverage_contract",
            system_prompt=research_subtree.COVERAGE_CONTRACT_PROMPT,
            variables=["brief"],
            metadata={"kind": "coverage"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.research.lead.default",
            agent_id="research_lead",
            system_prompt=research_subtree.PLAN_PROMPT,
            variables=["message", "quality_mode", "output_format", "budget"],
            metadata={"kind": "planner"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.research.reflection.default",
            agent_id="reflection",
            system_prompt=research_subtree.REFLECTION_PROMPT,
            variables=["state"],
            metadata={"kind": "reflection"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.research.synthesis.default",
            agent_id="synthesis",
            system_prompt=research_subtree.SYNTHESIS_PROMPT,
            variables=["message", "evidence_pack", "profile"],
            metadata={"kind": "writer"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.research.citation_verifier.default",
            agent_id="citation_verifier",
            system_prompt=research_subtree.CITATION_VERIFICATION_PROMPT,
            variables=["answer", "evidence_pack"],
            metadata={"kind": "verifier"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.research.repair.default",
            agent_id="repair",
            system_prompt=research_subtree.REPAIR_PROMPT,
            variables=["answer", "judge", "evidence_pack"],
            metadata={"kind": "repair"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.document.plan.default",
            agent_id="document_planner",
            system_prompt=document_subtree.PLAN_PROMPT,
            variables=["message", "research_answer", "output_format"],
            metadata={"kind": "planner"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.document.write.default",
            agent_id="document_writer",
            system_prompt="You are the Agent v3 document writer. Produce only the document body in markdown.",
            variables=["message", "plan", "research_answer"],
            metadata={"kind": "writer"},
        ),
        AgentV3PromptSpec(
            id="agent_v3.document.section_write.default",
            agent_id="document_section_writer",
            system_prompt="You are the Agent v3 document section writer. Write only the requested section in markdown.",
            variables=["section", "outline", "research_answer", "source_context"],
            metadata={"kind": "writer"},
        ),
    ]
