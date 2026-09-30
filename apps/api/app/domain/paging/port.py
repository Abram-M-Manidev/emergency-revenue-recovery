"""The two outward-facing capabilities paging needs, as ports.

`PagingProvider` sends one SMS or places one automated call. `AckLinkSigner`
issues and checks the capability in an acknowledgement link. Both are
abstract here with zero infrastructure imports, so the application layer can
run the whole paging lifecycle against fakes, and no vendor SDK or HTTP
detail ever reaches domain or application code. Concrete adapters live in
`app/infrastructure/paging/`.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod

from app.domain.paging.page import PageMessage, PagingReceipt, RecipientRole
from app.domain.paging.settings import PagingChannel


class PagingProvider(ABC):
    """Sends one page and reports, honestly, what the provider said."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short stable identifier recorded on the notification row, so an
        operator can tell a real send from the development logger."""
        ...

    @abstractmethod
    def supports(self, channel: PagingChannel) -> bool:
        """Whether this provider can send on `channel` at all."""
        ...

    @abstractmethod
    async def send(self, message: PageMessage) -> PagingReceipt:
        """One attempt, no retries — the schedule belongs to the caller.

        Must never raise: every failure, including a timeout, comes back as a
        receipt with a short `error_code`. `ACCEPTED` means the provider took
        responsibility for the message and nothing more; an adapter must
        never report a person as reached or as having acknowledged.

        `message.to` is a personal phone number and `message.body` carries the
        caller's details: neither may be logged."""
        ...


class AckLinkSigner(ABC):
    """Issues and verifies the token in an acknowledgement link.

    The token is the whole authority to acknowledge one recipient's page, so
    it must be unguessable and bound to both the page and the role: a
    primary's link must not acknowledge as the backup, and knowing a page or
    ticket id must not be enough to forge one."""

    @abstractmethod
    def issue(self, page_id: uuid.UUID, role: RecipientRole) -> str: ...

    @abstractmethod
    def verify(self, token: str) -> tuple[uuid.UUID, RecipientRole] | None:
        """The page and role this token was issued for, or None when it is
        malformed or its signature does not match. Expiry is the caller's
        decision, because it depends on when the page was created."""
        ...
