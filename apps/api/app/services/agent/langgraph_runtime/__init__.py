"""LangGraph research runtime compatibility shell.

Slice 0A contains only stubs and public-shape compatibility. Real graph nodes
and domain-function wiring are intentionally left for later slices.
"""

from app.services.agent.langgraph_runtime.runtime import run_langgraph_research

__all__ = ["run_langgraph_research"]
