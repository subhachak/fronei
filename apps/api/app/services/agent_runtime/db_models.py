from __future__ import annotations

from sqlalchemy import Boolean, DateTime, Float, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class DBAgentDefinition(Base):
    __tablename__ = "agent_definitions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_template_id: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    model_policy_id: Mapped[str] = mapped_column(Text, nullable=False)
    guardrail_policy_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    judge_policy_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DBPromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    developer_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_schema: Mapped[str | None] = mapped_column(Text, nullable=True)
    variables: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DBModelPolicy(Base):
    __tablename__ = "model_policies"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_models: Mapped[str] = mapped_column(Text, nullable=False)
    primary_model: Mapped[str] = mapped_column(Text, nullable=False)
    fallback_models: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    max_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=16_000)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=2_000)
    max_cost_usd_per_call: Mapped[float] = mapped_column(Float, nullable=False, default=0.10)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=30_000)
    parallel_fallback_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quality_modes: Mapped[str] = mapped_column(Text, nullable=False, default='["draft","standard"]')
    sensitive_domain_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DBToolDefinition(Base):
    __tablename__ = "tool_definitions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[str] = mapped_column(Text, nullable=False)
    output_schema: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_agent_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    required_user_roles: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    guardrail_policy_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=15_000)
    retry_policy: Mapped[str] = mapped_column(Text, nullable=False, default='{"max_attempts":1}')
    idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    backend: Mapped[str] = mapped_column(Text, nullable=False, default="native")
    backend_ref: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DBGuardrailPolicy(Base):
    __tablename__ = "guardrail_policies"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    applies_to: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    checks: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    action_map: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    severity: Mapped[str] = mapped_column(Text, nullable=False, default="medium")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[str] = mapped_column(Text, nullable=False, default="1.0.0")
    created_at = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
