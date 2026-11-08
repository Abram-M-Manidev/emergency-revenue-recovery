from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.application.services.lead_service import CURRENT_CONSENT_VERSION


class EarlyAccessRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    business_name: str = Field(min_length=1, max_length=160)
    email: EmailStr = Field(max_length=254)
    business_type: Literal["HVAC", "Plumbing", "Electrical", "Multi-trade", "Other"]
    # Must be explicitly true: no consent, no record.
    consent: Literal[True]
    consent_version: str = Field(min_length=1, max_length=32)
    # Hidden from humans; any value marks the submission as automated.
    website: str | None = Field(default=None, max_length=200)

    @field_validator("name", "business_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("consent_version")
    @classmethod
    def _current_version(cls, value: str) -> str:
        # Agreement to wording other than what the site shows today is not
        # agreement to the current notice.
        if value != CURRENT_CONSENT_VERSION:
            raise ValueError("the consent notice has changed; reload the page")
        return value


class EarlyAccessResponse(BaseModel):
    status: Literal["received"] = "received"
