"""The outbound-notification port.

Mirrors `app/domain/ai/provider.py`: a narrow abstract class in the domain
with zero infrastructure imports, so the application layer can orchestrate
emergency alerting without knowing whether it ends up as an HTTP POST, an
SMS, or a line in a log file. The concrete adapters live in
`app/infrastructure/notifications/`.

Why a port at all, when there is one channel today
--------------------------------------------------
Because there are already three implementations, and they differ in the only
way that matters: `NullNotificationProvider` reports that nothing was
configured, `LoggingNotificationProvider` reports success without telling
anyone (which is why production refuses to boot with it), and
`WebhookNotificationProvider` actually sends. Whether the assistant may say
a human was alerted is decided by which of those is wired in, so the seam
has to be explicit rather than a flag buried inside one class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.notifications.emergency import EmergencyAlert, NotificationReceipt


class NotificationPort(ABC):
    """Sends one emergency alert and reports, honestly, what happened."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short stable identifier, recorded on the delivery row so an
        operator reading it months later can tell which mechanism was in
        play — in particular whether a "delivered" was a real send or the
        development logger."""
        ...

    @abstractmethod
    async def send(
        self, alert: EmergencyAlert, destination: str | None
    ) -> NotificationReceipt:
        """Attempts delivery and returns a receipt.

        `destination` is the organization's own configured target, supplied
        per call rather than held on the provider: one instance serves every
        tenant in the process, so a cached destination would be a
        cross-tenant leak waiting for a refactor. Adapters that do not need
        one ignore it; an adapter that does and is handed None must report
        `NOT_CONFIGURED` rather than guessing or raising.

        It is a credential for every channel we support — a Slack or Teams
        incoming-webhook URL is the entire secret — so it must never be
        logged, echoed into a tool result, or placed in a prompt.

        Must never raise. A provider that raised would abort the voice turn
        mid-sentence — a silent hang-up on a caller who has just reported an
        emergency, which is strictly worse than telling them we could not
        confirm the alert. Every failure, including a timeout or a DNS error,
        comes back as a `FAILED` receipt carrying a short `error_code`.

        Should return only once the provider has accepted or refused. Retries
        and the overall time budget belong to the caller
        (`EmergencyNotificationService`), so this stays one observable
        attempt.
        """
        ...
