from __future__ import annotations

from dataclasses import dataclass

from app.services.agent_runtime.models import RuntimeBudget


class BudgetExceeded(Exception):
    """Raised when a turn exceeds its configured runtime budget."""


@dataclass
class RuntimeBudgetGuard:
    budget: RuntimeBudget
    total_cost_usd: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0

    def check_model_call(self) -> None:
        if self.model_calls >= self.budget.max_model_calls:
            raise BudgetExceeded("model call budget exhausted")
        self.model_calls += 1

    def check_tool_call(self) -> None:
        if self.tool_calls >= self.budget.max_tool_calls:
            raise BudgetExceeded("tool call budget exhausted")
        self.tool_calls += 1

    def record_cost(self, cost_usd: float) -> None:
        self.total_cost_usd += max(0.0, float(cost_usd or 0.0))
        if self.total_cost_usd > self.budget.max_turn_cost_usd:
            raise BudgetExceeded("turn cost budget exhausted")
