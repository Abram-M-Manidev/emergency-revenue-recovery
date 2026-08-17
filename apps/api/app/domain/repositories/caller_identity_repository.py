"""Association between a telephony caller ID and the customers that have
used it.

Kept separate from `CustomerRepository` because the two answer genuinely
different questions. `CustomerRepository.get_by_phone_number` matches on
`customers.phone_number` — the *callback number the caller stated*, which
is the C1 deduplication key. This port matches on `caller_number` — the
*line the call arrived from*, as observed by Vapi. The first real PSTN
call proved they are not interchangeable: caller ID `+91…` against a
stored phone of `123456789`, zero matches. Merging the two lookups would
quietly turn caller ID into a second deduplication key for customers,
which is exactly what P5 must not do.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.customer import Customer


class CallerIdentityRepository(ABC):
    @abstractmethod
    async def find_customers_by_caller_number(
        self, organization_id: uuid.UUID, caller_number: str
    ) -> list[Customer]:
        """Every customer in this organization associated with this caller
        ID, most recently seen first.

        Returns a list rather than a single customer on purpose: a
        household or office line legitimately belongs to several people,
        and the caller is whoever picked up the phone. Collapsing that to
        "most recent" would greet the wrong person by name. The decision
        of what to do with more than one result belongs to the caller of
        this method (`AIBrainService` grounds only on exactly one).

        `organization_id` is applied in the query itself, never by
        filtering afterwards, so a caller ID can never reach across
        tenants."""
        ...

    @abstractmethod
    async def associate(
        self, organization_id: uuid.UUID, *, customer_id: uuid.UUID, caller_number: str
    ) -> None:
        """Idempotently record that `caller_number` has been used by this
        customer, refreshing `last_seen_at` when the pair already exists.

        Called once per turn on a voice conversation, so it must be cheap
        and must never raise on a repeat — a live emergency call cannot be
        failed by a bookkeeping write."""
        ...
