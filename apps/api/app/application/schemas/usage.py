from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.domain.usage_report import UsageSummary


class ModelTokenUsageResponse(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int


class UsageSummaryResponse(BaseModel):
    billing_period: date
    calls: int
    billable_calls: int
    billable_minutes: int
    excluded_calls: dict[str, int]
    reconciliation: dict[str, int]
    unreconciled_calls: int
    rules_versions: list[str]
    llm_usage: list[ModelTokenUsageResponse]

    @classmethod
    def from_entity(cls, summary: UsageSummary) -> UsageSummaryResponse:
        return cls(
            billing_period=summary.billing_period,
            calls=summary.calls,
            billable_calls=summary.billable_calls,
            billable_minutes=summary.billable_minutes,
            excluded_calls=summary.excluded_calls,
            reconciliation=summary.reconciliation,
            unreconciled_calls=summary.unreconciled_calls,
            rules_versions=summary.rules_versions,
            llm_usage=[
                ModelTokenUsageResponse(
                    model=m.model,
                    requests=m.requests,
                    prompt_tokens=m.prompt_tokens,
                    completion_tokens=m.completion_tokens,
                )
                for m in summary.llm_usage
            ],
        )
