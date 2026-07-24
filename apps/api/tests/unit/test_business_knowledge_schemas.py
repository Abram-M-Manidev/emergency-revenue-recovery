import pytest
from pydantic import ValidationError

from app.application.schemas.business_knowledge import (
    CreateServiceAreaRequest,
    UpdateBusinessProfileRequest,
    UpdateWeeklyHoursRequest,
    WeeklyHoursEntry,
)
from app.domain.entities.business_profile import BusinessType


def _profile_kwargs(**overrides):
    kwargs = {
        "business_type": BusinessType.HVAC,
        "display_name": "Acme HVAC",
        "phone_number": None,
        "timezone": "America/Chicago",
        "address_line1": None,
        "address_line2": None,
        "city": None,
        "state": None,
        "postal_code": None,
        "country": "US",
        "website": None,
    }
    kwargs.update(overrides)
    return kwargs


def test_valid_timezone_is_accepted():
    profile = UpdateBusinessProfileRequest(**_profile_kwargs(timezone="America/Chicago"))
    assert profile.timezone == "America/Chicago"


def test_invalid_timezone_is_rejected():
    with pytest.raises(ValidationError):
        UpdateBusinessProfileRequest(**_profile_kwargs(timezone="Not/A_Zone"))


def _weekly_entry_kwargs(**overrides):
    kwargs = {"day_of_week": 0, "is_closed": False, "open_time": "08:00", "close_time": "17:00"}
    kwargs.update(overrides)
    return kwargs


def test_open_day_requires_both_times():
    with pytest.raises(ValidationError):
        WeeklyHoursEntry(**_weekly_entry_kwargs(open_time=None))


def test_open_time_must_be_before_close_time():
    with pytest.raises(ValidationError):
        WeeklyHoursEntry(**_weekly_entry_kwargs(open_time="18:00", close_time="09:00"))


def test_closed_day_does_not_require_times():
    entry = WeeklyHoursEntry(
        day_of_week=6, is_closed=True, open_time=None, close_time=None
    )
    assert entry.is_closed is True


def _full_week(overrides_by_day: dict[int, dict] | None = None) -> list[dict]:
    overrides_by_day = overrides_by_day or {}
    entries = []
    for day in range(7):
        base = _weekly_entry_kwargs(day_of_week=day)
        base.update(overrides_by_day.get(day, {}))
        entries.append(base)
    return entries


def test_full_week_of_entries_is_accepted():
    request = UpdateWeeklyHoursRequest(entries=_full_week())
    assert len(request.entries) == 7


def test_missing_day_is_rejected():
    entries = _full_week()[:-1]  # drop Sunday
    with pytest.raises(ValidationError):
        UpdateWeeklyHoursRequest(entries=entries)


def test_duplicate_day_is_rejected():
    entries = _full_week()
    entries[1]["day_of_week"] = 0  # duplicate Monday, no Tuesday
    with pytest.raises(ValidationError):
        UpdateWeeklyHoursRequest(entries=entries)


def test_service_area_requires_city_or_postal_code():
    with pytest.raises(ValidationError):
        CreateServiceAreaRequest(label="Downtown", postal_code=None, city=None, state=None)


def test_service_area_with_only_city_is_accepted():
    area = CreateServiceAreaRequest(label="Downtown", postal_code=None, city="Springfield", state="IL")
    assert area.city == "Springfield"
