"""Phone numbers: canonicalisation, and validation of numbers a caller gives.

Two jobs, kept apart on purpose.

`normalize_phone_number` / `storable_phone_number` only strip formatting
noise. They are what staff-configured numbers (transfer and paging
destinations) and column-width guards use, where nothing is being decided
about whether a person can be reached.

`check_phone_number` / `valid_phone_number` decide whether a number is a real,
complete phone number, using Google's libphonenumber metadata
(`phonenumbers`), and return it in E.164. They are what every number a
CALLER supplies passes through before it is stored or used as the customer
deduplication key. The 2026-10-31 test call is why: the caller said nine
digits of a ten-digit number. The old rule kept
any digits, so a number nobody can call back became the key of a second
customer record for the same person.

Region. A national-format number ("630 555 0184") means nothing without a
country. The region always comes from the business's own configured country
(`region_for_country`), never from a default: with no usable country, only a
number given in international form ("+44 …") can be accepted, and anything
else is reported as needing a country code rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, ValidationResult

_KEEPABLE = "0123456789"


def normalize_phone_number(raw: str | None) -> str | None:
    """The comparable form of a phone number, or None when there is nothing
    usable in it.

    Keeps the digits, and a single leading `+` when the caller's number was
    given in international form — that distinction is real (`+441234` and
    `441234` are different numbers), so it is preserved rather than
    flattened.

    Returns None for a blank or digit-free value, because "" and "unknown"
    are both "we do not have a number", and storing either as a dedupe key
    would merge unrelated callers into one record.

    Formatting only — this says nothing about whether the number is real.
    Use `valid_phone_number` for anything a caller supplied."""
    if raw is None:
        return None

    text = raw.strip()
    if not text:
        return None

    international = text.startswith("+")
    digits = "".join(character for character in text if character in _KEEPABLE)
    if not digits:
        return None

    return f"+{digits}" if international else digits


def storable_phone_number(raw: str | None) -> str | None:
    """`normalize_phone_number`, plus the one check every writer needs: that
    the result fits the column it is going into.

    Every phone column this value can reach (`conversation_outcomes`,
    `appointments`, `emergency_tickets`, `customers`) is 32 characters. The
    canonical form keeps digits only, so this almost never bites — but a
    caller reciting an account number and a phone number in one breath does
    exist, and an over-long value is a failed write that rolls back the
    whole turn (2026-09-24). None means "no usable number", which every
    caller already handles, rather than a truncated one, which would be a
    fabricated number indistinguishable from a real one."""
    # Imported here to keep this module free of domain imports at load time
    # for the many callers that only need `normalize_phone_number`.
    from app.domain.entities.conversation_outcome import CUSTOMER_PHONE_MAX_LENGTH

    canonical = normalize_phone_number(raw)
    if canonical is None or len(canonical) > CUSTOMER_PHONE_MAX_LENGTH:
        return None
    return canonical


# --- Validation of caller-supplied numbers ------------------------------------

#: Nothing usable was given: blank, or no digits at all.
PHONE_MISSING = "missing"
#: Too few digits to be a number in the region (the nine-digit US number).
PHONE_INCOMPLETE = "incomplete"
#: More digits than any number in the region has.
PHONE_TOO_LONG = "too_long"
#: Right shape, but not a number that can exist (e.g. a US 555 area code).
PHONE_INVALID = "invalid"
#: A national-format number for a business with no usable country.
PHONE_NEEDS_COUNTRY_CODE = "needs_country_code"

# ISO 3166 alpha-3 codes for the countries a business profile is most likely
# to hold in that form. Anything else must be stored as alpha-2; an
# unrecognised value yields no region rather than a guessed one.
_ALPHA3_TO_REGION = {
    "USA": "US",
    "CAN": "CA",
    "GBR": "GB",
    "IND": "IN",
    "AUS": "AU",
    "NZL": "NZ",
    "IRL": "IE",
}


@dataclass(frozen=True, slots=True)
class PhoneCheck:
    """The result of checking one caller-supplied number.

    Exactly one of `e164` and `problem` is set. `digits` is how many digits
    were heard, so the assistant can say "I only caught nine digits" without
    the number itself having to travel anywhere."""

    e164: str | None
    problem: str | None
    digits: int

    @property
    def is_valid(self) -> bool:
        return self.e164 is not None


def region_for_country(country: str | None) -> str | None:
    """The libphonenumber region for a business profile's `country`, or None
    when it is unset or not recognised. Never a default."""
    code = (country or "").strip().upper()
    code = _ALPHA3_TO_REGION.get(code, code)
    if code in phonenumbers.SUPPORTED_REGIONS:
        return code
    return None


def check_phone_number(raw: str | None, *, region: str | None) -> PhoneCheck:
    """Whether `raw` is a real, complete phone number, read in `region` when
    it is not in international form."""
    text = (raw or "").strip()
    digits = sum(1 for character in text if character in _KEEPABLE)
    if digits == 0:
        return PhoneCheck(e164=None, problem=PHONE_MISSING, digits=0)

    try:
        parsed = phonenumbers.parse(text, region)
    except NumberParseException as exc:
        if exc.error_type == NumberParseException.INVALID_COUNTRY_CODE:
            problem = PHONE_NEEDS_COUNTRY_CODE if region is None else PHONE_INVALID
        elif exc.error_type in (
            NumberParseException.TOO_SHORT_NSN,
            NumberParseException.TOO_SHORT_AFTER_IDD,
        ):
            problem = PHONE_INCOMPLETE
        elif exc.error_type == NumberParseException.TOO_LONG:
            problem = PHONE_TOO_LONG
        else:
            problem = PHONE_INVALID
        return PhoneCheck(e164=None, problem=problem, digits=digits)

    possible = phonenumbers.is_possible_number_with_reason(parsed)
    if possible in (ValidationResult.TOO_SHORT, ValidationResult.INVALID_LENGTH):
        # INVALID_LENGTH is a length between valid ones; for a caller that
        # is still "say it again", so it is reported the same way.
        return PhoneCheck(e164=None, problem=PHONE_INCOMPLETE, digits=digits)
    if possible == ValidationResult.TOO_LONG:
        return PhoneCheck(e164=None, problem=PHONE_TOO_LONG, digits=digits)
    if not phonenumbers.is_valid_number(parsed):
        return PhoneCheck(e164=None, problem=PHONE_INVALID, digits=digits)
    return PhoneCheck(
        e164=phonenumbers.format_number(parsed, PhoneNumberFormat.E164),
        problem=None,
        digits=digits,
    )


def valid_phone_number(raw: str | None, *, region: str | None = None) -> str | None:
    """The E.164 form of a real, complete number, or None.

    With no region, only an international-form number can pass — which is
    what a caller ID from the telephony layer is, and what every number
    already stored by `check_phone_number` is."""
    return check_phone_number(raw, region=region).e164


def legacy_phone_keys(e164: str) -> tuple[str, ...]:
    """The forms the same number may have been stored in before numbers were
    validated: digits only, with and without the country code, and with a
    leading `+` (`normalize_phone_number`'s output for each input style).

    Used only to FIND an existing customer whose key predates E.164, so a
    returning caller is matched instead of getting a second record. Nothing
    is rewritten or merged."""
    parsed = phonenumbers.parse(e164, None)
    national = phonenumbers.national_significant_number(parsed)
    with_country = f"{parsed.country_code}{national}"
    return tuple(
        key for key in dict.fromkeys((with_country, national, f"+{with_country}")) if key != e164
    )
