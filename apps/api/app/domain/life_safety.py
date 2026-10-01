"""Recognising when a caller may be in immediate danger, and what the system
says — by itself, not via the model — when they are.

Why deterministic
-----------------
The assistant already treats emergencies as emergencies: it opens a ticket,
alerts and pages the business. But a gas leak, a carbon monoxide alarm or a
burning smell is not first a service job; it is a person who may need to get
out of the building. Leaving "tell them to leave and call 911" to the model
means it happens only on the conversation paths the model handles well, and
a stressed caller is exactly the unusual path. So:

1. `detect_hazards` matches the caller's own words against fixed patterns.
   It errs towards matching: telling someone with a merely smoky toaster to
   step outside is a small cost; missing a gas leak is not.
2. The first time a hazard is reported on a call, `safety_instruction`
   produces a fixed, reviewed sentence that is spoken BEFORE any model
   output, and stored as part of the reply so the transcript is truthful.
3. For the rest of the call, `life_safety_directive` is placed in the system
   prompt, binding the model's own replies: no troubleshooting, never tell
   the caller to operate equipment, never imply they should wait for a
   technician instead of calling emergency services, never claim emergency
   services were contacted (this system cannot contact them).

The instructions only ever move the caller AWAY from danger and point them
to emergency services. They never ask the caller to touch, shut off, reset
or relight anything: a caller manipulating a gas valve or a breaker panel on
the say-so of an automated assistant is the harm to avoid. They do not
diagnose beyond what routing needs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum


class Hazard(str, Enum):
    # Declaration order is speaking order: the most immediately lethal first.
    FIRE_SMOKE = "fire_smoke"
    GAS = "gas"
    CARBON_MONOXIDE = "carbon_monoxide"
    ELECTRICAL = "electrical"
    FLOODING = "flooding"


_PATTERNS: dict[Hazard, tuple[re.Pattern[str], ...]] = {
    Hazard.GAS: tuple(
        re.compile(p)
        for p in (
            r"\bgas\s+(leak|leaking|smell|odou?r)",
            r"\b(smell|smells|smelling|smelled|odou?r)\b[^.?!]{0,30}\bgas\b",
            r"\bleak(ing)?\b[^.?!]{0,20}\bgas\b",
            r"\brotten\s+eggs?\b",
            r"\b(propane|natural\s+gas)\b[^.?!]{0,20}\b(leak|smell)",
            r"\bhiss(ing|es)?\b[^.?!]{0,30}\bgas\b",
            r"\bgas\b[^.?!]{0,20}\bhiss",
        )
    ),
    Hazard.CARBON_MONOXIDE: tuple(
        re.compile(p)
        for p in (
            r"\bcarbon\s+monoxide\b",
            r"\bmonoxide\b",
            r"\bco\s+(alarm|detector|monitor)s?\b",
        )
    ),
    Hazard.FIRE_SMOKE: tuple(
        re.compile(p)
        for p in (
            r"\bsmoke\b(?!\s+(detector|alarm)s?\b[^.?!]*\b(chirp|beep|battery))",
            r"\bsmoking\b",
            r"\bfire\b",
            r"\bflames?\b",
            r"\bon\s+fire\b",
            r"\bburning\b",
            r"\bscorch(ed|ing)?\b",
            r"\bsmell(s|ing)?\s+(like\s+)?(something\s+)?burn",
        )
    ),
    Hazard.ELECTRICAL: tuple(
        re.compile(p)
        for p in (
            r"\bspark(s|ing|ed|y)?\b",
            r"\barc(ing|ed)?\b",
            r"\belectrocut",
            r"\b(got|getting|get)\s+(a\s+)?shock",
            r"\bshocked\b",
            r"\b(exposed|live|bare)\s+wires?\b",
            r"\bmelt(ing|ed)\b",
        )
    ),
    Hazard.FLOODING: tuple(
        re.compile(p)
        for p in (
            r"\bflood(s|ing|ed)?\b",
            r"\b(burst|broken|busted)\s+(water\s+)?pipe",
            r"\bpipe\s+(burst|broke)",
            r"\bwater\s+(is\s+)?(everywhere|pouring|gushing|rising|spraying)",
            r"\bstanding\s+water\b",
        )
    ),
}


def detect_hazards(text: str | None) -> frozenset[Hazard]:
    """The life-safety hazards the caller's words describe. Case-insensitive;
    deliberately generous."""
    if not text:
        return frozenset()
    lowered = text.lower()
    return frozenset(
        hazard
        for hazard, patterns in _PATTERNS.items()
        if any(pattern.search(lowered) for pattern in patterns)
    )


def hazards_in(texts: Iterable[str]) -> frozenset[Hazard]:
    found: set[Hazard] = set()
    for text in texts:
        found |= detect_hazards(text)
    return frozenset(found)


def emergency_number_for(country: str | None) -> str:
    """The emergency number to name. Only stated where it is known; anywhere
    else the caller is pointed at "your local emergency number" rather than
    a guessed one."""
    if (country or "").strip().upper() in {"US", "CA", "USA", "CAN"}:
        return "911"
    return "your local emergency number"


def _ordered(hazards: Iterable[Hazard]) -> list[Hazard]:
    present = set(hazards)
    return [hazard for hazard in Hazard if hazard in present]


def safety_instruction(hazards: Iterable[Hazard], *, emergency_number: str) -> str | None:
    """The fixed sentences spoken before anything else when a hazard is first
    reported. Short, imperative, and only ever about moving away from danger
    and calling for help."""
    ordered = _ordered(hazards)
    if not ordered:
        return None
    call = f"call {emergency_number}"
    sentences: list[str] = []
    if Hazard.FIRE_SMOKE in ordered:
        sentences.append(f"If there's smoke or fire, get everyone out of the building now and {call}.")
    if Hazard.GAS in ordered:
        sentences.append(
            "If you smell gas, please leave the building now. Don't switch anything on or off, "
            f"and once you're outside, {call} or your gas company."
        )
    if Hazard.CARBON_MONOXIDE in ordered:
        sentences.append(
            "If a carbon monoxide alarm is going off or anyone feels unwell, get everyone "
            f"outside into fresh air now and {call}."
        )
    if Hazard.ELECTRICAL in ordered:
        sentences.append(
            "Please stay well away from anything sparking and don't touch it. If there's "
            f"smoke or fire, get out and {call}."
        )
    if Hazard.FLOODING in ordered:
        sentences.append(
            "Please keep away from the water if it's near outlets, cords or appliances. If "
            f"anyone is in danger, {call}."
        )
    return "Your safety comes first. " + " ".join(sentences)


_DESCRIPTIONS = {
    Hazard.FIRE_SMOKE: "smoke or fire",
    Hazard.GAS: "a possible gas leak",
    Hazard.CARBON_MONOXIDE: "possible carbon monoxide",
    Hazard.ELECTRICAL: "sparking or an electrical danger",
    Hazard.FLOODING: "flooding",
}


def life_safety_directive(
    hazards: Iterable[Hazard], *, emergency_number: str, instruction_given: str | None
) -> str | None:
    """The prompt section binding the model for the rest of a call on which a
    hazard has been reported."""
    ordered = _ordered(hazards)
    if not ordered:
        return None
    described = ", ".join(_DESCRIPTIONS[h] for h in ordered)
    lines = [
        f"LIFE-SAFETY — this caller has reported {described}. Their safety comes "
        "before anything else on this call.",
    ]
    if instruction_given:
        lines.append(f'The system has already told them: "{instruction_given}"')
    lines += [
        "- If they may still be inside or near the hazard, briefly tell them again "
        f"to get somewhere safe and call {emergency_number}. Keep every reply to "
        "one or two short sentences.",
        "- Give NO troubleshooting steps. Never tell them to touch, open, close, "
        "shut off, reset, relight, unplug or operate any gas, electrical, heating "
        "or other equipment — not even if they ask. Say the technician will deal "
        "with the equipment once everyone is safe.",
        f"- Never suggest waiting for a technician instead of calling {emergency_number}.",
        "- You cannot contact emergency services or the gas company, and nobody "
        "has. Never say they have been called, notified or are coming.",
        "- Still record the emergency with create_service_request (classification "
        '"emergency") as soon as possible, without waiting for a name or address, '
        "so the business is alerted.",
    ]
    return "\n".join(lines)
