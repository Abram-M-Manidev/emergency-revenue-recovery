"""Turns an organization's Business Knowledge (Milestone 2 data) into the
system prompt the AI Brain grounds its answers in. A pure function — no DB
or AI-provider calls — so it is trivially unit-testable and the one place
that decides what the LLM is allowed to know about a business.

Never hardcode business knowledge (per MASTER_PROJECT_VISION.docx's
Engineering Philosophy): every fact in the prompt is read from the
organization's own profile/hours/services/service areas/FAQs, not baked
into this template."""

from __future__ import annotations

from datetime import date

from app.domain.entities.business_hours import HoursException, WeeklyHours
from app.domain.entities.business_profile import BusinessProfile
from app.domain.entities.emergency_keyword import EmergencyKeyword
from app.domain.entities.faq_entry import FAQEntry
from app.domain.entities.known_caller import KnownCaller
from app.domain.entities.service import Service
from app.domain.entities.service_area import ServiceArea

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_RESPONSE_CONTRACT = """
Respond by deciding, for the customer's latest message in the context of \
the whole conversation:
- classification: "emergency", "non_emergency", or "unknown" (not enough \
information yet).
- recommended_action: "create_emergency_ticket", "book_appointment", \
"answer_faq", "escalate_to_human", or "none".
- Any of the customer's name, phone number, or address mentioned so far.
- Whether the conversation is complete (you have said everything needed \
and are waiting on nothing further from the customer).
- A one-sentence summary of the situation.
- The message to say back to the customer.

Never invent business details (hours, services, pricing, service areas) \
that are not listed below. If asked about something not covered, say you \
are not sure and offer to have a human follow up.
"""


def _known_caller_section(known_caller: KnownCaller | None) -> str | None:
    """The P5 grounding section, or None when the prompt must stay exactly
    as it was before P5.

    Caller ID is a lookup hint, not authentication — it is trivially
    spoofable — so every line here is written to stop the model treating a
    match as proof of identity, and to stop it reading stored details
    aloud. In particular the address is never placed in the prompt: the
    model is told only that one exists, so the worst a spoofed caller ID
    can obtain is the knowledge that this business has *an* address on
    file, which they already implied by calling."""
    if known_caller is None or known_caller.is_empty:
        return None

    lines = [
        "Caller records (internal context — NOT proof of identity):",
        "- This call arrived from a phone number our records associate "
        "with a previous customer. A phone number can be spoofed or "
        "shared, so treat everything below as what our records indicate, "
        "never as established fact about who is speaking.",
    ]
    if known_caller.has_name:
        lines.append(f'- Our records for this number show the name: "{known_caller.name}".')
    if known_caller.address_on_file:
        lines.append(
            "- We have a service address on file for this number. You are "
            "NOT told what it is and must NOT guess or state it. If the "
            "address matters for this call, ask the caller to confirm "
            "whether the address we already have is still the right one "
            "for this visit (for example: \"I have an address on file — is "
            "that still the right service address?\"). If they say it has "
            "changed, or give a different one, ask for the new address."
        )
    lines.extend(
        [
            "- Anything the caller says in this conversation always takes "
            "precedence over the records above. Never argue with them "
            "about their own details.",
            "- Do not read these records back unprompted, and do not "
            "mention that a lookup happened. Use them only to avoid asking "
            "for something we already have.",
            "- Ask only for details missing from both these records and "
            "this conversation.",
        ]
    )
    return "\n".join(lines)


def build_system_prompt(
    *,
    profile: BusinessProfile | None,
    weekly_hours: list[WeeklyHours],
    hours_exceptions: list[HoursException],
    services: list[Service],
    service_areas: list[ServiceArea],
    faqs: list[FAQEntry],
    emergency_keywords: list[EmergencyKeyword],
    today: date,
    emergency_keyword_hint: bool,
    known_caller: KnownCaller | None = None,
) -> str:
    sections: list[str] = []

    business_name = profile.display_name if profile else "this business"
    business_type = profile.business_type.value if profile else "service"
    sections.append(
        f"You are the after-hours phone assistant for {business_name}, a "
        f"{business_type} company. Your job is to determine whether a "
        "caller has an emergency, answer non-emergency questions using only "
        "the verified information below, and collect the caller's name, "
        "phone number, and address when relevant."
    )

    if weekly_hours:
        lines = []
        by_day = {h.day_of_week: h for h in weekly_hours}
        for day_index, day_name in enumerate(_DAY_NAMES):
            hours = by_day.get(day_index)
            if hours is None or hours.is_closed:
                lines.append(f"- {day_name}: closed")
            else:
                lines.append(f"- {day_name}: {hours.open_time}–{hours.close_time}")
        sections.append("Business hours:\n" + "\n".join(lines))

    upcoming_exceptions = [exc for exc in hours_exceptions if exc.date >= today]
    if upcoming_exceptions:
        lines = []
        for exc in upcoming_exceptions:
            if exc.is_closed:
                lines.append(f"- {exc.date} ({exc.label or 'closed'}): closed")
            else:
                lines.append(
                    f"- {exc.date} ({exc.label or 'special hours'}): "
                    f"{exc.open_time}–{exc.close_time}"
                )
        sections.append("Upcoming hours exceptions:\n" + "\n".join(lines))

    active_services = [s for s in services if s.is_active]
    if active_services:
        lines = [
            f"- {s.name}"
            + (f": {s.description}" if s.description else "")
            + (" (emergency-eligible)" if s.is_emergency_eligible else "")
            for s in active_services
        ]
        sections.append("Services offered:\n" + "\n".join(lines))

    if service_areas:
        lines = [f"- {a.label}" for a in service_areas]
        sections.append("Service areas covered:\n" + "\n".join(lines))

    active_faqs = [f for f in faqs if f.is_active]
    if active_faqs:
        lines = [f"- Q: {f.question}\n  A: {f.answer}" for f in active_faqs]
        sections.append("Frequently asked questions:\n" + "\n".join(lines))

    if emergency_keywords:
        phrases = ", ".join(k.phrase for k in emergency_keywords)
        sections.append(
            "Phrases this business considers likely emergency indicators "
            f"(a guide, not a hard rule — use judgment): {phrases}"
        )

    if emergency_keyword_hint:
        sections.append(
            "Note: the customer's latest message contains one of this "
            "business's emergency keywords. Weigh this heavily but confirm "
            "with the actual situation described before classifying as an "
            "emergency."
        )

    known_caller_section = _known_caller_section(known_caller)
    if known_caller_section:
        sections.append(known_caller_section)

    sections.append(_RESPONSE_CONTRACT.strip())

    return "\n\n".join(sections)
