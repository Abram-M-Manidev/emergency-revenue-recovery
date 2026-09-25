"""Pure helpers added by the 2026-09-25 production audit: what a model- or
Vapi-supplied value is reduced to before it reaches a fixed-width column,
and how an offered time is read to a caller."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.application.services.voice_tool_executor import _spoken
from app.domain.entities.conversation_outcome import (
    CUSTOMER_ADDRESS_MAX_LENGTH,
    CUSTOMER_NAME_MAX_LENGTH,
    CUSTOMER_PHONE_MAX_LENGTH,
    bounded_contact_text,
)
from app.shared.utils.phone import storable_phone_number


def test_contact_text_within_the_limit_is_untouched():
    assert bounded_contact_text("Jonathan", CUSTOMER_NAME_MAX_LENGTH) == "Jonathan"
    assert bounded_contact_text(None, CUSTOMER_NAME_MAX_LENGTH) is None
    assert bounded_contact_text("", CUSTOMER_NAME_MAX_LENGTH) == ""


def test_contact_text_over_the_limit_keeps_its_beginning():
    address = "16th Street, Lyle, California " * 30
    bounded = bounded_contact_text(address, CUSTOMER_ADDRESS_MAX_LENGTH)
    assert bounded is not None
    assert len(bounded) == CUSTOMER_ADDRESS_MAX_LENGTH
    assert address.startswith(bounded)


def test_a_storable_phone_number_is_canonical_and_fits():
    assert storable_phone_number("(630) 555-0184") == "6305550184"
    assert storable_phone_number("+1 630 555 0184") == "+16305550184"


def test_a_phone_number_that_cannot_fit_is_dropped_never_truncated():
    """A truncated number is a different, real-looking number."""
    recited = "my account is " + "4" * 30 + " and my number is 6305550184"
    assert len("".join(c for c in recited if c.isdigit())) > CUSTOMER_PHONE_MAX_LENGTH
    assert storable_phone_number(recited) is None


def test_spoken_words_are_not_a_storable_phone_number():
    assert storable_phone_number("one two three four five six seven eight nine") is None
    assert storable_phone_number("") is None
    assert storable_phone_number(None) is None


def test_a_time_this_year_is_read_without_the_year():
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime.now(zone)
    value = now.replace(month=now.month, day=min(now.day, 28), hour=9, minute=30)
    assert str(now.year) not in _spoken(value)
    assert _spoken(value).endswith("at 9:30 AM")


def test_a_time_in_another_year_is_read_with_the_year():
    """A wrong-year date must be audible. Month and day alone let the
    stale-year class of defect book a caller a year out unnoticed."""
    zone = ZoneInfo("America/Los_Angeles")
    now = datetime.now(zone)
    value = datetime(now.year + 1, 1, 5, 14, 0, tzinfo=zone)
    assert _spoken(value) == f"{value.strftime('%A')}, January 5, {now.year + 1} at 2 PM"
