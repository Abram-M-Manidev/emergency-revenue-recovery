"""The approved conversational context for a caller the system recognises.

Deliberately not a `Customer`. Caller ID is a *lookup hint*, never proof of
identity — it is trivially spoofable over SIP — so what reaches the model
is a hand-picked projection rather than the customer record. Everything
that could identify, authenticate, or be harvested is excluded: the
customer id, organization id, phone number, email, notes, and timestamps
are all absent by construction, so no prompt-building mistake can leak
them.

`address_on_file` is a boolean rather than the address itself. The
assistant needs to know an address *exists* so it can confirm rather than
re-ask, but it must not recite a stored address merely because a caller ID
matched — that would hand a spoofer a service address for the cost of one
phone call. The value is carried in `address` for the rare case the
conversation genuinely establishes the right to use it; the prompt
contract (`prompt_builder`) is what forbids volunteering it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class KnownCaller:
    """What the AI Brain may know about a recognised caller.

    Built only when exactly one customer is associated with the caller ID;
    an ambiguous number yields no `KnownCaller` at all (see
    `CallerIdentityRepository.find_customers_by_caller_number`).
    """

    name: str | None
    address: str | None

    @property
    def has_name(self) -> bool:
        return bool(self.name and self.name.strip())

    @property
    def address_on_file(self) -> bool:
        return bool(self.address and self.address.strip())

    @property
    def is_empty(self) -> bool:
        """A customer row that carries neither usable field grounds nothing
        — the caller is recognised, but there is nothing worth telling the
        model, so the prompt stays byte-identical to the unknown-caller
        one."""
        return not self.has_name and not self.address_on_file
