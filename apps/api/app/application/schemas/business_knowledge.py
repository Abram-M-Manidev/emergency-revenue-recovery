from __future__ import annotations

import uuid
from datetime import date, datetime, time
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.entities.business_profile import BusinessType

_VALID_TIMEZONES = available_timezones()


def _validate_timezone(value: str) -> str:
    if value not in _VALID_TIMEZONES:
        raise ValueError(f"'{value}' is not a recognized IANA timezone.")
    return value


# --- Business profile ---


class UpdateBusinessProfileRequest(BaseModel):
    business_type: BusinessType
    display_name: str = Field(min_length=1, max_length=255)
    phone_number: str | None = Field(default=None, max_length=32)
    timezone: str = Field(max_length=64)
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=60)
    postal_code: str | None = Field(default=None, max_length=20)
    country: str = Field(default="US", min_length=2, max_length=2)
    website: str | None = Field(default=None, max_length=255)

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        return _validate_timezone(value)


class BusinessProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    business_type: BusinessType
    display_name: str
    phone_number: str | None
    timezone: str
    address_line1: str | None
    address_line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str
    website: str | None
    created_at: datetime
    updated_at: datetime


# --- Hours (weekly + exceptions) ---


class WeeklyHoursEntry(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    is_closed: bool = False
    open_time: time | None = None
    close_time: time | None = None

    @model_validator(mode="after")
    def _validate_times(self) -> WeeklyHoursEntry:
        if not self.is_closed:
            if self.open_time is None or self.close_time is None:
                raise ValueError("open_time and close_time are required unless the day is closed.")
            if self.open_time >= self.close_time:
                raise ValueError("open_time must be before close_time.")
        return self


class UpdateWeeklyHoursRequest(BaseModel):
    entries: list[WeeklyHoursEntry]

    @field_validator("entries")
    @classmethod
    def _validate_full_week(cls, value: list[WeeklyHoursEntry]) -> list[WeeklyHoursEntry]:
        days = sorted(entry.day_of_week for entry in value)
        if days != list(range(7)):
            raise ValueError("Exactly one entry for each day of the week (0-6) is required.")
        return value


class WeeklyHoursResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    day_of_week: int
    is_closed: bool
    open_time: time | None
    close_time: time | None


class CreateHoursExceptionRequest(BaseModel):
    date: date
    is_closed: bool = False
    open_time: time | None = None
    close_time: time | None = None
    label: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _validate_times(self) -> CreateHoursExceptionRequest:
        if not self.is_closed:
            if self.open_time is None or self.close_time is None:
                raise ValueError("open_time and close_time are required unless the day is closed.")
            if self.open_time >= self.close_time:
                raise ValueError("open_time must be before close_time.")
        return self


class HoursExceptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    date: date
    is_closed: bool
    open_time: time | None
    close_time: time | None
    label: str | None


# --- Service areas ---


class CreateServiceAreaRequest(BaseModel):
    label: str = Field(min_length=1, max_length=255)
    postal_code: str | None = Field(default=None, max_length=20)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _require_location(self) -> CreateServiceAreaRequest:
        if not self.postal_code and not self.city:
            raise ValueError("At least one of postal_code or city is required.")
        return self


class ServiceAreaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    postal_code: str | None
    city: str | None
    state: str | None


# --- Services offered ---


class CreateServiceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    is_emergency_eligible: bool = False
    is_active: bool = True
    default_duration_minutes: int | None = Field(default=None, gt=0, le=1440)


class UpdateServiceRequest(CreateServiceRequest):
    pass


class ServiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    category: str | None
    is_emergency_eligible: bool
    is_active: bool
    default_duration_minutes: int | None


# --- Emergency keywords ---


class CreateEmergencyKeywordRequest(BaseModel):
    phrase: str = Field(min_length=2, max_length=255)
    notes: str | None = Field(default=None, max_length=500)


class EmergencyKeywordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phrase: str
    notes: str | None


# --- FAQs ---


class CreateFAQRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1)
    category: str | None = Field(default=None, max_length=100)
    is_active: bool = True


class UpdateFAQRequest(CreateFAQRequest):
    pass


class FAQEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    question: str
    answer: str
    category: str | None
    is_active: bool
