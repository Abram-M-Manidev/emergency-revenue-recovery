"""LLM token usage, as the provider reported it.

The voice path's model runs on ERRS's own OpenAI key (Vapi's Custom-LLM
mode), so Vapi's cost report cannot see it: this is the only record of that
spend. Counts are what the provider returned — never estimated. A request
whose usage never arrived (a stream aborted or failed before its final
chunk) is counted in `requests_without_usage` rather than as zero tokens,
so an undercount is visible instead of silent.

`TokenUsageSink.record` is synchronous on purpose: the provider calls it
from inside a live stream, including while the stream is being torn down,
where nothing may be awaited and nothing may raise into the caller's turn.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One model request. `reported` is False when the provider never sent
    usage for it; the token counts are then 0 and mean "unknown"."""

    model: str
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0
    completion_tokens: int = 0
    reported: bool = True


@dataclass(frozen=True, slots=True)
class TokenUsageTotal:
    model: str
    requests: int
    requests_without_usage: int
    prompt_tokens: int
    cached_prompt_tokens: int
    completion_tokens: int


class TokenUsageSink(ABC):
    @abstractmethod
    def record(self, usage: TokenUsage) -> None:
        """Must not await and must not raise."""
        ...


class TokenUsageTally(TokenUsageSink):
    """Per-turn, in-memory accumulator. Persisted by `AIBrainService` once
    the provider is done with the turn."""

    def __init__(self) -> None:
        self._totals: dict[str, TokenUsageTotal] = {}

    def record(self, usage: TokenUsage) -> None:
        current = self._totals.get(usage.model) or TokenUsageTotal(usage.model, 0, 0, 0, 0, 0)
        self._totals[usage.model] = TokenUsageTotal(
            model=usage.model,
            requests=current.requests + 1,
            requests_without_usage=current.requests_without_usage + (0 if usage.reported else 1),
            prompt_tokens=current.prompt_tokens + usage.prompt_tokens,
            cached_prompt_tokens=current.cached_prompt_tokens + usage.cached_prompt_tokens,
            completion_tokens=current.completion_tokens + usage.completion_tokens,
        )

    def totals(self) -> list[TokenUsageTotal]:
        return list(self._totals.values())
