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
            default_duration_minutes=None,
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
            default_duration_minutes=None,
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


# --- Tool contract wording ----------------------------------------------------
#
# These pin the two rules a live call on 2026-08-22 violated: it booked a
# correctly-classified non-emergency AC fault as a standard appointment and
# then confirmed it with "an emergency technician will be dispatched".


def test_the_tool_contract_is_absent_until_tools_are_enabled():
    """An assistant told how to book while holding no tools would be worse
    than one never told."""
    prompt = _build()
    # `book_appointment` is deliberately not asserted on: it is also a
    # `recommended_action` value in the response contract, which predates
    # tools and is always present.
    assert "check_availability" not in prompt
    assert "You have tools that perform real actions" not in prompt
    assert "emergency technician" not in prompt


def test_a_booked_appointment_must_be_confirmed_in_standard_language():
    prompt = _build(tools_enabled=True)

    assert "Never describe any part of it with emergency language" in prompt
    for forbidden in ("emergency technician", "emergency dispatch", "emergency service"):
        assert forbidden in prompt, f"the rule must name {forbidden!r} explicitly"
    assert "two contradictory answers" in prompt


def test_the_emergency_path_keeps_its_own_wording():
    """The separation cuts both ways: emergencies still get emergency
    language, and the rule says so."""
    prompt = _build(tools_enabled=True)

    assert 'classification "emergency"' in prompt
    assert "Emergency wording belongs to this path and only this path." in prompt


def test_the_dispatcher_claim_is_bound_to_the_tool_result_not_the_models_judgement():
    """The prompt half of emergency truthfulness. The backend gate is what
    actually enforces it (see `test_emergency_notification.py`), but the
    assistant composes its sentence from this text, so the branch has to be
    stated here in both directions."""
    prompt = _build(tools_enabled=True)

    assert '"dispatcher_alerted"' in prompt
    assert "Never decide it yourself" in prompt
    # The true branch.
    assert '"dispatcher_alerted": true' in prompt
    # The false branch, which is the one that used to be missing entirely.
    assert '"dispatcher_alerted": false' in prompt
    assert "Do NOT say a dispatcher has been alerted" in prompt
    assert "emergency services" in prompt


def test_the_prompt_never_licenses_an_unconditional_dispatcher_claim():
    """The exact sentence that was false on every emergency call: an
    instruction to tell the caller a dispatcher was alerted, with no
    condition attached to it."""
    prompt = _build(tools_enabled=True)

    assert "Tell the caller a dispatcher has been alerted" not in prompt


def test_the_never_claim_without_a_successful_tool_rule_survives():
    """The invariant every other rule is subordinate to."""
    prompt = _build(tools_enabled=True)

    assert 'never tell the caller something has been done unless the tool ' in prompt
    assert '"success": true' in prompt
    assert "Only if book_appointment returned" in prompt


def test_the_contract_asks_for_speakable_wording():
    """"1 moment." was read out as a digit on a live call. The reply is TTS
    input, not text on a screen."""
    prompt = _build(tools_enabled=True)

    assert "read aloud over a phone line" in prompt
    assert "complete sentences" in prompt
    assert '"one moment", not "1 moment"' in prompt


# --- Completion closing rule --------------------------------------------------
#
# Pins the rule the live call of 2026-08-27 violated. That turn booked
# correctly and set is_conversation_complete=true, then ended the spoken reply
# with "Is there anything else I can assist you with?" — telling the caller the
# call was over while inviting another turn.


def test_a_completed_conversation_must_close_rather_than_ask():
    prompt = _build()

    assert "When you mark the conversation complete" in prompt
    assert "must not end with a question" in prompt
    assert "For a booked appointment, confirm the details and then close." in prompt
    # An example, not a mandated phrase — any natural sign-off is allowed.
    assert '"Thank you for calling."' in prompt
    assert "or any natural equivalent" in prompt


def test_the_closing_rule_names_the_invitations_it_forbids():
    """A question mark is not the only way to invite another turn, so the
    rule names the invitation itself rather than just the punctuation."""
    prompt = _build()

    assert "ask whether the caller needs anything else" in prompt
    assert "invite them to carry on" in prompt
    assert "ask for any further information" in prompt


def test_the_closing_rule_leaves_an_unfinished_conversation_free_to_ask():
    """Scoped to completion only. A turn still missing the caller's address
    must go on asking for it."""
    prompt = _build()

    assert (
        "While the conversation is not complete, go on asking for whatever "
        "you still need." in prompt
    )


def test_the_closing_rule_applies_with_and_without_tools():
    """It lives in the response contract, not the tool contract: an FAQ,
    callback, or escalation completion sets the same flag with no tool in
    sight and has to close the same way."""
    for prompt in (_build(), _build(tools_enabled=True)):
        assert "When you mark the conversation complete" in prompt


def test_the_prompt_separates_offering_times_from_recording_a_choice():
    """The consent ladder's prompt half. The deterministic enforcement lives
    in `AppointmentService` (see `test_appointment_consent.py`); this is what
    stops the model walking into a refusal it could have avoided."""
    prompt = _build(tools_enabled=True)

    assert "select_appointment_slot" in prompt
    # Offering and booking must not happen in one breath.
    assert "STOP after offering" in prompt
    assert "SLOT_NOT_YET_HEARD" in prompt
    # And the two conditions on a booking are both stated.
    assert "SLOT_NOT_OFFERED" in prompt
    assert "SLOT_NOT_SELECTED" in prompt
    assert "Offering a time is not the caller choosing it" in prompt


def test_the_prompt_tells_the_model_to_ask_rather_than_guess_an_ambiguous_choice():
    prompt = _build(tools_enabled=True)

    assert "cannot tell which time they meant" in prompt
    assert "do not guess" in prompt


def test_the_selection_contract_is_absent_until_tools_are_enabled():
    """A prompt for a caller with no tools must not mention a tool it cannot
    call — the same rule the rest of the tool contract already follows."""
    prompt = _build(tools_enabled=False)

    assert "select_appointment_slot" not in prompt
    assert "SLOT_NOT_SELECTED" not in prompt
