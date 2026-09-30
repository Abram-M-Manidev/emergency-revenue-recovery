"""Emergency paging: reaching a named on-call person, escalating to a backup,
and recording an explicit acknowledgement. See `page.py` for the model."""

from app.domain.paging.page import (
    AcknowledgementMethod,
    CallerPagingState,
    EmergencyPage,
    NewPageNotification,
    PageMessage,
    PageNotification,
    PageNotificationStatus,
    PageStatus,
    PagingOutcome,
    PagingReceipt,
    RecipientRole,
    caller_paging_state,
    every_attempt_failed,
)
from app.domain.paging.port import AckLinkSigner, PagingProvider
from app.domain.paging.settings import (
    InvalidPagingSettingsError,
    PagingChannel,
    PagingSettings,
    mask_paging_number,
    validate_paging_settings,
)

__all__ = [
    "AckLinkSigner",
    "AcknowledgementMethod",
    "CallerPagingState",
    "EmergencyPage",
    "InvalidPagingSettingsError",
    "NewPageNotification",
    "PageMessage",
    "PageNotification",
    "PageNotificationStatus",
    "PageStatus",
    "PagingChannel",
    "PagingOutcome",
    "PagingProvider",
    "PagingReceipt",
    "PagingSettings",
    "RecipientRole",
    "caller_paging_state",
    "every_attempt_failed",
    "mask_paging_number",
    "validate_paging_settings",
]
