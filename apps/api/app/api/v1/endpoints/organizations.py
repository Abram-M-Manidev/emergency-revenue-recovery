"""Organization settings endpoints (Milestone 9): view and rename the
caller's own organization, or deactivate/reactivate it. Every route derives
its organization scope from `current_user.organization_id` — never a
client-supplied id.

Gated entirely behind `organization:manage`, which only the Owner role
holds by default (Admin/Member never did, even before this milestone) —
so this stays Owner-only, matching the least-privilege default the RBAC
catalogue already encodes rather than introducing a new read-only
permission just for this milestone."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import (
    get_call_disclosure_service,
    get_call_transfer_settings_service,
    get_notification_settings_service,
    get_organization_service,
    get_paging_settings_service,
    require_permission,
)
from app.application.schemas.call_disclosure import (
    CallDisclosureSettingsResponse,
    ConfigureCallDisclosureRequest,
)
from app.application.schemas.call_transfer import (
    CallTransferSettingsResponse,
    ConfigureCallTransferRequest,
)
from app.application.schemas.notification_settings import (
    ConfigureNotificationsRequest,
    NotificationSettingsResponse,
    SetNotificationsEnabledRequest,
)
from app.application.schemas.organization import OrganizationResponse, UpdateOrganizationRequest
from app.application.schemas.paging import ConfigurePagingRequest, PagingSettingsResponse
from app.application.services.call_disclosure_service import CallDisclosureService
from app.application.services.call_transfer_settings_service import (
    CallTransferSettingsService,
)
from app.application.services.notification_settings_service import (
    NotificationSettingsService,
)
from app.application.services.organization_service import OrganizationService
from app.application.services.paging_settings_service import PagingSettingsService
from app.domain.call_transfer.settings import InvalidTransferNumberError
from app.domain.entities.rbac import Permissions
from app.domain.entities.user import User
from app.domain.notifications.settings import InvalidNotificationDestinationError
from app.domain.paging.settings import InvalidPagingSettingsError

router = APIRouter(prefix="/organizations", tags=["organizations"])

_manage_user = require_permission(Permissions.ORGANIZATION_MANAGE)


@router.get("/current", response_model=OrganizationResponse)
async def get_current_organization(
    user: User = Depends(_manage_user),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationResponse:
    organization = await service.get_current(user.organization_id)
    return OrganizationResponse.model_validate(organization)


@router.patch("/current", response_model=OrganizationResponse)
async def update_current_organization(
    payload: UpdateOrganizationRequest,
    user: User = Depends(_manage_user),
    service: OrganizationService = Depends(get_organization_service),
) -> OrganizationResponse:
    organization = await service.update_current(
        # Always the caller's own organization, from their validated JWT —
        # there is no request field that can redirect this to another tenant.
        user.organization_id,
        name=payload.name,
        is_active=payload.is_active,
        voice_assistant_enabled=payload.voice_assistant_enabled,
    )
    return OrganizationResponse.model_validate(organization)


# --- Emergency notification configuration ------------------------------------
#
# Mounted under /organizations because that is what it configures — one
# tenant's operational setup — and gated behind the same Owner-only
# `organization:manage` the rest of this router uses. Deliberately NOT a new
# permission: adding one would need an RBAC backfill migration for every
# existing organization, and "who may change where emergency alerts go" is
# exactly the authority `organization:manage` already represents.
#
# Every route derives its tenant from `user.organization_id` — the caller's
# own validated JWT. There is no path or body parameter naming an
# organization, so cross-tenant access is not merely rejected here, it is
# unrepresentable.


@router.get("/current/notifications", response_model=NotificationSettingsResponse | None)
async def get_notification_settings(
    user: User = Depends(_manage_user),
    service: NotificationSettingsService = Depends(get_notification_settings_service),
) -> NotificationSettingsResponse | None:
    """This organization's alerting configuration, or null if never set.

    Returns a masked hint of the destination, never the destination — see
    `NotificationSettingsResponse`. Null and disabled are distinguishable on
    purpose: an operator has to be able to tell "I switched this off" from
    "this was never configured"."""
    settings = await service.get(user.organization_id)
    if settings is None:
        return None
    return NotificationSettingsResponse.model_validate(settings)


@router.put("/current/notifications", response_model=NotificationSettingsResponse)
async def configure_notification_settings(
    payload: ConfigureNotificationsRequest,
    user: User = Depends(_manage_user),
    service: NotificationSettingsService = Depends(get_notification_settings_service),
) -> NotificationSettingsResponse:
    """Sets where emergency alerts go for this organization.

    PUT rather than POST: there is exactly one configuration per tenant, and
    calling this twice must leave the same single row rather than a second
    one."""
    try:
        settings = await service.configure(
            user.organization_id,
            channel=payload.channel,
            destination=payload.destination,
            is_enabled=payload.is_enabled,
        )
    except InvalidNotificationDestinationError as exc:
        # 422, alongside every other field-level validation failure. The
        # message names the rule that was broken (https required, private
        # address, credentials in the URL) but never echoes the submitted
        # value back — an error response is not a place to reflect a
        # credential the caller may have pasted by mistake.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return NotificationSettingsResponse.model_validate(settings)


@router.patch("/current/notifications", response_model=NotificationSettingsResponse)
async def set_notification_settings_enabled(
    payload: SetNotificationsEnabledRequest,
    user: User = Depends(_manage_user),
    service: NotificationSettingsService = Depends(get_notification_settings_service),
) -> NotificationSettingsResponse:
    """Pauses or resumes alerting, leaving the destination untouched.

    Exists so an operator pausing alerts for maintenance never has to
    re-handle the webhook URL — every extra time a credential is typed,
    pasted or transmitted is another chance to leak it."""
    settings = await service.set_enabled(
        user.organization_id, is_enabled=payload.is_enabled
    )
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No notification destination is configured for this organization.",
        )
    return NotificationSettingsResponse.model_validate(settings)


@router.delete(
    "/current/notifications", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_notification_settings(
    user: User = Depends(_manage_user),
    service: NotificationSettingsService = Depends(get_notification_settings_service),
) -> Response:
    """Removes the configuration and the stored destination with it.

    Idempotent: deleting a configuration that is not there is a 204, because
    the caller's intent — "this organization should have no destination
    stored" — is satisfied either way."""
    await service.remove(user.organization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Human fallback (call transfer) configuration ------------------------------
#
# Same authority and the same tenant rule as notification settings above:
# `organization:manage`, tenant taken only from the caller's own JWT.


@router.get("/current/call-transfer", response_model=CallTransferSettingsResponse | None)
async def get_call_transfer_settings(
    user: User = Depends(_manage_user),
    service: CallTransferSettingsService = Depends(get_call_transfer_settings_service),
) -> CallTransferSettingsResponse | None:
    """Where this organization's calls may be handed to a person, or null if
    never set (in which case every transfer is reported unavailable and the
    assistant offers a callback instead)."""
    settings = await service.get(user.organization_id)
    return CallTransferSettingsResponse.model_validate(settings) if settings else None


@router.put("/current/call-transfer", response_model=CallTransferSettingsResponse)
async def configure_call_transfer_settings(
    payload: ConfigureCallTransferRequest,
    user: User = Depends(_manage_user),
    service: CallTransferSettingsService = Depends(get_call_transfer_settings_service),
) -> CallTransferSettingsResponse:
    """Sets the office (business hours) and on-call (after hours) numbers.
    Refuses non-E.164 numbers and the business's own AI voice line, which
    would loop the caller straight back into the assistant."""
    try:
        settings = await service.configure(
            user.organization_id,
            business_hours_number=payload.business_hours_number,
            after_hours_number=payload.after_hours_number,
            transfer_emergencies=payload.transfer_emergencies,
            is_enabled=payload.is_enabled,
        )
    except InvalidTransferNumberError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return CallTransferSettingsResponse.model_validate(settings)


@router.delete(
    "/current/call-transfer", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_call_transfer_settings(
    user: User = Depends(_manage_user),
    service: CallTransferSettingsService = Depends(get_call_transfer_settings_service),
) -> Response:
    """Removes human-transfer configuration. Idempotent."""
    await service.delete(user.organization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Emergency paging configuration -------------------------------------------
#
# Same authority and tenant rule as above: `organization:manage` (Owner-only by
# default), tenant taken only from the caller's own JWT.


@router.get("/current/paging", response_model=PagingSettingsResponse | None)
async def get_paging_settings(
    user: User = Depends(_manage_user),
    service: PagingSettingsService = Depends(get_paging_settings_service),
) -> PagingSettingsResponse | None:
    """Who this organization pages about an emergency, or null if never set
    (in which case nobody is paged and the assistant never says anyone was)."""
    settings = await service.get(user.organization_id)
    return PagingSettingsResponse.model_validate(settings) if settings else None


@router.put("/current/paging", response_model=PagingSettingsResponse)
async def configure_paging_settings(
    payload: ConfigurePagingRequest,
    user: User = Depends(_manage_user),
    service: PagingSettingsService = Depends(get_paging_settings_service),
) -> PagingSettingsResponse:
    """Sets the primary and backup on-call numbers, channels and the
    acknowledgement timeout. The 422 names the rule broken and never echoes
    a submitted number."""
    try:
        settings = await service.configure(
            user.organization_id,
            is_enabled=payload.is_enabled,
            primary_number=payload.primary_number,
            backup_number=payload.backup_number,
            sms_enabled=payload.sms_enabled,
            voice_enabled=payload.voice_enabled,
            ack_timeout_seconds=payload.ack_timeout_seconds,
        )
    except InvalidPagingSettingsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return PagingSettingsResponse.model_validate(settings)


@router.delete("/current/paging", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_paging_settings(
    user: User = Depends(_manage_user),
    service: PagingSettingsService = Depends(get_paging_settings_service),
) -> Response:
    """Removes paging configuration and the stored numbers. Idempotent."""
    await service.delete(user.organization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Caller disclosure (AI / recording notice) ---------------------------------
#
# Same authority and tenant rule as above. Unlike the other settings, a
# GET always returns a policy: with nothing saved, the DEFAULT applies (both
# notices on), and the response says so rather than returning null — "what
# will callers hear?" must always have an answer.


@router.get("/current/disclosure", response_model=CallDisclosureSettingsResponse)
async def get_call_disclosure_settings(
    user: User = Depends(_manage_user),
    service: CallDisclosureService = Depends(get_call_disclosure_service),
) -> CallDisclosureSettingsResponse:
    context = await service.context_for(user.organization_id)
    stored = await service.get_settings(user.organization_id)
    return CallDisclosureSettingsResponse.build(
        context.policy, business_name=context.business_name, stored=stored
    )


@router.put("/current/disclosure", response_model=CallDisclosureSettingsResponse)
async def configure_call_disclosure_settings(
    payload: ConfigureCallDisclosureRequest,
    user: User = Depends(_manage_user),
    service: CallDisclosureService = Depends(get_call_disclosure_service),
) -> CallDisclosureSettingsResponse:
    stored = await service.configure(
        user.organization_id,
        ai_disclosure=payload.ai_disclosure_enabled,
        recording_notice=payload.recording_notice_enabled,
    )
    context = await service.context_for(user.organization_id)
    return CallDisclosureSettingsResponse.build(
        stored.policy, business_name=context.business_name, stored=stored
    )


@router.delete(
    "/current/disclosure", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def reset_call_disclosure_settings(
    user: User = Depends(_manage_user),
    service: CallDisclosureService = Depends(get_call_disclosure_service),
) -> Response:
    """Back to the default (both notices on). Idempotent."""
    await service.reset(user.organization_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
