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

    sections.append(_RESPONSE_CONTRACT.strip())

    return "\n\n".join(sections)
