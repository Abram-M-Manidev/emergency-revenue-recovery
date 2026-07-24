import uuid
from datetime import date, datetime, timezone

from app.application.services.prompt_builder import build_system_prompt
from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile, BusinessType
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea

_ORG_ID = uuid.uuid4()


def _profile() -> BusinessProfile:
    return BusinessProfile(
        id=uuid.uuid4(),
        organization_id=_ORG_ID,
        business_type=BusinessType.HVAC,
        display_name="Acme HVAC",
        phone_number=None,
        timezone="America/Chicago",
        address_line1=None,
        address_line2=None,
        city=None,
        state=None,
        postal_code=None,
        country="US",
        website=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _build(**overrides) -> str:
    kwargs = dict(
        profile=_profile(),
        weekly_hours=[],
        hours_exceptions=[],
        services=[],
        service_areas=[],
        faqs=[],
        emergency_keywords=[],
        today=date(2026, 1, 1),
        emergency_keyword_hint=False,
    )
    kwargs.update(overrides)
    return build_system_prompt(**kwargs)


def test_includes_business_name_and_type():
    prompt = _build()
    assert "Acme HVAC" in prompt
    assert "hvac" in prompt


def test_no_profile_falls_back_to_generic_wording():
    prompt = _build(profile=None)
    assert "this business" in prompt


def test_closed_days_are_marked_closed():
    hours = [
        WeeklyHours(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            day_of_week=6,
            is_closed=True,
            open_time=None,
            close_time=None,
        )
    ]
    prompt = _build(weekly_hours=hours)
    assert "Sunday: closed" in prompt


def test_past_hours_exceptions_are_excluded():
    exceptions = [
        HoursException(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            date=date(2025, 1, 1),
            is_closed=True,
            open_time=None,
            close_time=None,
            label="Past Holiday",
        )
    ]
    prompt = _build(hours_exceptions=exceptions, today=date(2026, 1, 1))
    assert "Past Holiday" not in prompt


def test_inactive_services_and_faqs_are_excluded():
    services = [
        Service(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            name="Retired Service",
            description=None,
            category=None,
            is_emergency_eligible=False,
            is_active=False,
        )
    ]
    faqs = [
        FAQEntry(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            question="Old question?",
            answer="Old answer.",
            category=None,
            is_active=False,
        )
    ]
    prompt = _build(services=services, faqs=faqs)
    assert "Retired Service" not in prompt
    assert "Old question?" not in prompt


def test_active_service_area_and_faq_are_included():
    services = [
        Service(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            name="Furnace Repair",
            description="Fix furnaces",
            category="Repair",
            is_emergency_eligible=True,
            is_active=True,
        )
    ]
    areas = [ServiceArea(id=uuid.uuid4(), organization_id=_ORG_ID, label="Downtown", postal_code=None, city=None, state=None)]
    faqs = [
        FAQEntry(
            id=uuid.uuid4(),
            organization_id=_ORG_ID,
            question="Do you offer 24/7 service?",
            answer="Yes.",
            category=None,
            is_active=True,
        )
    ]
    prompt = _build(services=services, service_areas=areas, faqs=faqs)
    assert "Furnace Repair" in prompt
    assert "emergency-eligible" in prompt
    assert "Downtown" in prompt
    assert "Do you offer 24/7 service?" in prompt


def test_emergency_keyword_hint_is_called_out():
    keywords = [EmergencyKeyword(id=uuid.uuid4(), organization_id=_ORG_ID, phrase="no heat", notes=None)]
    prompt = _build(emergency_keywords=keywords, emergency_keyword_hint=True)
    assert "no heat" in prompt
    assert "emergency keyword" in prompt.lower()
