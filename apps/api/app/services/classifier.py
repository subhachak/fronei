import re
from app.schemas import TaskType, Complexity


KEYWORDS: dict[str, list[str]] = {
    "coding":        ["code", "bug", "function", "repo", "github", "python",
                      "typescript", "api", "stacktrace", "error"],
    "architecture":  ["architecture", "design", "platform", "system", "cloud",
                      "microservice", "domain", "api gateway", "diagram"],
    "summarization": ["summarize", "summary", "condense", "extract", "recap"],
    "writing":       ["rewrite", "draft", "tone", "email", "slide", "narrative", "copy"],
    "research":      ["latest", "current", "research", "compare", "market",
                      "news", "regulation", "pricing"],
    "math":          ["calculate", "formula", "probability", "roi", "tax", "mortgage"],
    "planning":      ["plan", "roadmap", "schedule", "strategy", "steps"],
}

_PATTERNS: dict[str, re.Pattern] = {
    task: re.compile(
        r'\b(?:' + '|'.join(re.escape(w) for w in words) + r')\b',
        re.IGNORECASE,
    )
    for task, words in KEYWORDS.items()
}


def classify_task(message: str) -> tuple[TaskType, Complexity, str]:
    scores: dict[str, int] = {
        task: len(pattern.findall(message))
        for task, pattern in _PATTERNS.items()
    }

    task = max(scores, key=scores.get) if max(scores.values()) > 0 else "unknown"

    length = len(message)
    high_markers   = ["ruthless", "deep", "complete", "production", "deploy",
                      "enterprise", "step by step", "fact-check"]
    medium_markers = ["compare", "review", "improve", "analyze", "build"]
    text = message.lower()

    if length > 2500 or any(m in text for m in high_markers):
        complexity: Complexity = "high"
    elif length > 700 or any(m in text for m in medium_markers):
        complexity = "medium"
    else:
        complexity = "low"

    reason = (f"Matched task='{task}' using word-boundary regex; "
              f"complexity='{complexity}' from prompt length and intent markers.")
    return task, complexity, reason
