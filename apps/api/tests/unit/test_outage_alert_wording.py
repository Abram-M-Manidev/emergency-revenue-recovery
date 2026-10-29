"""What the AI-outage reply may say about a possible emergency's alert.

On the outage turn the alert has at most been QUEUED: it is sent only after
the turn commits. The reply used to say "I've flagged your call to the team
as a possible emergency" regardless — even when nowhere was configured to
send, or the alert had already failed. These tests pin each delivery state
to the only sentence its evidence supports.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.application.services.call_follow_up_service import _emergency_alert_sentence
from app.domain.notifications.emergency import (
    DeliveryStatus,
    NotificationChannel,
    NotificationDelivery,
)

_NOW = datetime(2026, 10, 30, 3, 0, tzinfo=timezone.utc)

_DELIVERED = "I've sent your call to the team as a possible emergency."
_BEING_SENT = (
    "I've logged your call as a possible emergency, and an alert to the team is being sent now."
)
_UNCONFIRMED = (
    "I've logged your call as a possible emergency, but I can't confirm that anyone "
    "on the team has been alerted."
)


def _delivery(
    status: DeliveryStatus, *, next_attempt_at: datetime | None, attempts: int = 0
) -> NotificationDelivery:
    return NotificationDelivery(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        ticket_id=uuid.uuid4(),
        channel=NotificationChannel.WEBHOOK,
        provider="fake",
        status=status,
        attempts=attempts,
        error_code=None,
        delivered_at=_NOW if status is DeliveryStatus.DELIVERED else None,
        created_at=_NOW,
        updated_at=_NOW,
        next_attempt_at=next_attempt_at,
    )


def test_a_delivered_alert_may_be_called_sent_to_the_team():
    sentence = _emergency_alert_sentence(
        _delivery(DeliveryStatus.DELIVERED, next_attempt_at=None, attempts=1)
    )
    assert sentence == _DELIVERED


def test_a_queued_never_attempted_alert_is_only_being_sent():
    sentence = _emergency_alert_sentence(_delivery(DeliveryStatus.PENDING, next_attempt_at=_NOW))
    assert sentence == _BEING_SENT


@pytest.mark.parametrize(
    "delivery",
    [
        pytest.param(
            _delivery(DeliveryStatus.NOT_CONFIGURED, next_attempt_at=None), id="not_configured"
        ),
        pytest.param(
            _delivery(DeliveryStatus.FAILED, next_attempt_at=None, attempts=8),
            id="failed_retries_spent",
        ),
        pytest.param(
            # Already tried and refused, waiting to retry: `is_queued` is
            # true, but "being sent now" would hide a known failure.
            _delivery(DeliveryStatus.FAILED, next_attempt_at=_NOW + timedelta(minutes=5),
                      attempts=2),
            id="failed_retry_pending",
        ),
        pytest.param(
            # PENDING with nothing scheduled is not a send in progress.
            _delivery(DeliveryStatus.PENDING, next_attempt_at=None), id="pending_unscheduled"
        ),
        pytest.param(None, id="no_delivery_or_unreadable"),
    ],
)
def test_every_unconfirmed_state_claims_nothing(delivery):
    assert _emergency_alert_sentence(delivery) == _UNCONFIRMED


_FORBIDDEN_IN_UNCONFIRMED = ("sent your call", "being sent", "flagged", "notified",
                             "dispatcher", "on the way", "will call", "contacted")


@pytest.mark.parametrize(
    "delivery",
    [
        None,
        _delivery(DeliveryStatus.NOT_CONFIGURED, next_attempt_at=None),
        _delivery(DeliveryStatus.FAILED, next_attempt_at=None, attempts=8),
        _delivery(DeliveryStatus.FAILED, next_attempt_at=_NOW, attempts=1),
    ],
)
def test_an_unconfirmed_sentence_never_implies_anyone_was_reached(delivery):
    sentence = _emergency_alert_sentence(delivery).lower()
    for phrase in _FORBIDDEN_IN_UNCONFIRMED:
        assert phrase not in sentence, phrase
    assert "can't confirm" in sentence


@pytest.mark.parametrize(
    "delivery",
    [
        None,
        _delivery(DeliveryStatus.PENDING, next_attempt_at=_NOW),
        _delivery(DeliveryStatus.DELIVERED, next_attempt_at=None, attempts=1),
        _delivery(DeliveryStatus.NOT_CONFIGURED, next_attempt_at=None),
    ],
)
def test_no_state_ever_claims_a_person(delivery):
    """Even a delivered alert only reached a channel (B4): never a
    dispatcher, a callback, or anyone on the way."""
    sentence = _emergency_alert_sentence(delivery).lower()
    for phrase in ("dispatcher", "on the way", "will call", "has been notified", "is coming"):
        assert phrase not in sentence, phrase
