"""Canonicalisation for phone numbers captured from speech.

Exists because a spoken number does not arrive in one form. A live
verification call on 2026-08-22 produced two `Customer` rows for one
caller: the model transcribed the same digits as `"1 2 3 4 5 6 7 8 9"` on
one turn and `"123456789"` on the next, and `customers.phone_number` is the
deduplication key, matched exactly. The caller's history was split in two,
which is precisely the failure the unified customer record exists to
prevent.

Deliberately not a full E.164 parser. Doing that properly needs a region,
a carrier database, and a dependency (`phonenumbers`), and none of the
three earns its place here: the only thing that has actually gone wrong is
formatting noise — spaces, dashes, brackets, dots — around digits the
caller stated. Stripping that noise fixes the observed defect without
pretending to validate numbers this system has no way to validate.
"""

from __future__ import annotations

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
    would merge unrelated callers into one record."""
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
