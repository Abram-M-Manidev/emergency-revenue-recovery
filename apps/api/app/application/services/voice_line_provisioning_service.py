"""Mapping a Vapi assistant (and optionally its phone number) to exactly one
organization — the routing decision every inbound call depends on.

Why this is an operator action and not a tenant one
---------------------------------------------------
Every assistant lives in the platform operator's single Vapi account. A
tenant cannot prove it owns an assistant id, so letting an Owner "claim" one
from the dashboard would let any tenant squat on — or, once reassignment
exists, take over — another business's phone line. This service is therefore
driven only by the provisioning CLI (`python -m app.cli.voice_lines`), which
requires shell access to the API container: the same authority that already
holds the Vapi and database credentials. Owners get read-only visibility of
their own mapping through `GET /voice/line`.

Why it exists at all
--------------------
Lines were provisioned by hand-written SQL. On 2026-09-23 the pilot assistant
was still mapped to a QA organization, and the first real call recited the
wrong business's service areas; the same step also had an enum-casing trap
(`'vapi'` vs `'VAPI'`) that yields a row the ORM cannot read. This service
writes through the ORM and refuses every ambiguous change unless the operator
states the intent explicitly:

- an assistant that already routes to another organization is moved only when
  the operator names that organization as `confirm_reassign_from`;
- an organization that already has a line gets a different assistant only
  with `replace_existing`;
- a phone-number id already routing elsewhere is never silently taken.

The database backs all of it up: `voice_lines` is unique on the organization,
on the assistant id and on the phone-number id, so no sequence of calls here
can leave an assistant routing to two tenants.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Literal

import structlog

from app.domain.entities.voice_line import VoiceLine, VoiceProvider
from app.domain.exceptions import DomainError, EntityNotFoundError
from app.domain.repositories.organization_repository import OrganizationRepository
from app.domain.repositories.voice_line_repository import VoiceLineRepository

logger = structlog.get_logger("app.voice.provisioning")

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")

ProvisioningAction = Literal["created", "updated", "unchanged", "reassigned"]


class VoiceLineProvisioningError(DomainError):
    """A provisioning request that is unsafe or ambiguous as stated. The
    message always says what explicit confirmation would make it proceed."""


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    action: ProvisioningAction
    line: VoiceLine
    # The organization the assistant routed to before a reassignment.
    previous_organization_id: uuid.UUID | None = None
    # The assistant this organization answered with before `replace_existing`.
    replaced_assistant_id: str | None = None


def _vapi_id(value: str, field: str) -> str:
    """Vapi ids are UUIDs. Anything else is a typo, a truncated paste, or — the
    case worth catching — a credential pasted into the wrong field."""
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        raise VoiceLineProvisioningError(f"{field} must be a Vapi id (a UUID).") from None


class VoiceLineProvisioningService:
    def __init__(
        self,
        *,
        voice_line_repository: VoiceLineRepository,
        organization_repository: OrganizationRepository,
    ) -> None:
        self._lines = voice_line_repository
        self._organizations = organization_repository

    async def list_lines(self) -> list[VoiceLine]:
        return await self._lines.list_all()

    async def provision(
        self,
        *,
        organization_id: uuid.UUID,
        vapi_assistant_id: str,
        vapi_phone_number_id: str | None = None,
        phone_number: str | None = None,
        confirm_reassign_from: uuid.UUID | None = None,
        replace_existing: bool = False,
    ) -> ProvisioningResult:
        assistant_id = _vapi_id(vapi_assistant_id, "vapi_assistant_id")
        phone_id = (
            _vapi_id(vapi_phone_number_id, "vapi_phone_number_id")
            if vapi_phone_number_id
            else None
        )
        number = phone_number.strip() if phone_number else None
        if number is not None and not _E164.match(number):
            raise VoiceLineProvisioningError(
                "phone_number must be in E.164 form, e.g. +16305550100."
            )

        organization = await self._organizations.get_by_id(organization_id)
        if organization is None:
            raise EntityNotFoundError("Organization", str(organization_id))
        if not organization.is_active:
            raise VoiceLineProvisioningError(
                "That organization is deactivated; reactivate it before giving it a phone line."
            )

        by_assistant = await self._lines.get_by_vapi_assistant_id(assistant_id)
        current = await self._lines.get_by_organization_id(organization_id)
        by_phone = await self._lines.get_by_vapi_phone_number_id(phone_id) if phone_id else None

        # --- The assistant already routes to ANOTHER organization -------------
        if by_assistant is not None and by_assistant.organization_id != organization_id:
            if confirm_reassign_from != by_assistant.organization_id:
                raise VoiceLineProvisioningError(
                    "This assistant currently answers for organization "
                    f"{by_assistant.organization_id}. Moving it takes that "
                    "business's phone line away from them; to do it deliberately, "
                    "confirm the current owner with --reassign-from "
                    f"{by_assistant.organization_id}."
                )
            deleting = None
            if current is not None:
                if not replace_existing:
                    raise VoiceLineProvisioningError(
                        "The target organization already has a line (assistant "
                        f"{current.vapi_assistant_id}). Pass --replace-existing to "
                        "retire it as part of this reassignment."
                    )
                deleting = current
            self._refuse_phone_conflict(by_phone, writing=by_assistant, deleting=deleting)
            if deleting is not None:
                await self._lines.delete(deleting.id)
            line = await self._lines.update(
                by_assistant.id,
                organization_id=organization_id,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=phone_id or by_assistant.vapi_phone_number_id,
                phone_number=number or by_assistant.phone_number,
                is_active=True,
            )
            return self._audited(
                ProvisioningResult(
                    action="reassigned",
                    line=line,
                    previous_organization_id=by_assistant.organization_id,
                    replaced_assistant_id=deleting.vapi_assistant_id if deleting else None,
                )
            )

        # --- The assistant is already this organization's ---------------------
        if by_assistant is not None:
            self._refuse_phone_conflict(by_phone, writing=by_assistant, deleting=None)
            new_phone_id = phone_id or by_assistant.vapi_phone_number_id
            new_number = number or by_assistant.phone_number
            if (
                new_phone_id == by_assistant.vapi_phone_number_id
                and new_number == by_assistant.phone_number
                and by_assistant.is_active
            ):
                return ProvisioningResult(action="unchanged", line=by_assistant)
            line = await self._lines.update(
                by_assistant.id,
                organization_id=organization_id,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=new_phone_id,
                phone_number=new_number,
                is_active=True,
            )
            return self._audited(ProvisioningResult(action="updated", line=line))

        # --- A new assistant -------------------------------------------------
        if current is not None:
            if not replace_existing:
                raise VoiceLineProvisioningError(
                    "This organization already answers with assistant "
                    f"{current.vapi_assistant_id}. Pass --replace-existing to "
                    "switch it to the new one."
                )
            self._refuse_phone_conflict(by_phone, writing=current, deleting=None)
            line = await self._lines.update(
                current.id,
                organization_id=organization_id,
                vapi_assistant_id=assistant_id,
                vapi_phone_number_id=phone_id or current.vapi_phone_number_id,
                phone_number=number or current.phone_number,
                is_active=True,
            )
            return self._audited(
                ProvisioningResult(
                    action="updated", line=line, replaced_assistant_id=current.vapi_assistant_id
                )
            )

        self._refuse_phone_conflict(by_phone, writing=None, deleting=None)
        line = await self._lines.create(
            organization_id=organization_id,
            provider=VoiceProvider.VAPI,
            vapi_assistant_id=assistant_id,
            vapi_phone_number_id=phone_id,
            phone_number=number,
        )
        return self._audited(ProvisioningResult(action="created", line=line))

    async def set_active(self, organization_id: uuid.UUID, *, is_active: bool) -> VoiceLine:
        """Stops (or resumes) routing calls to this organization without
        forgetting the mapping."""
        line = await self._lines.get_by_organization_id(organization_id)
        if line is None:
            raise EntityNotFoundError("VoiceLine", str(organization_id))
        updated = await self._lines.update(
            line.id,
            organization_id=line.organization_id,
            vapi_assistant_id=line.vapi_assistant_id,
            vapi_phone_number_id=line.vapi_phone_number_id,
            phone_number=line.phone_number,
            is_active=is_active,
        )
        logger.warning(
            "voice_line_activation_changed",
            organization_id=str(organization_id),
            is_active=is_active,
        )
        return updated

    @staticmethod
    def _refuse_phone_conflict(
        by_phone: VoiceLine | None, *, writing: VoiceLine | None, deleting: VoiceLine | None
    ) -> None:
        allowed = {row.id for row in (writing, deleting) if row is not None}
        if by_phone is not None and by_phone.id not in allowed:
            raise VoiceLineProvisioningError(
                "That Vapi phone number already routes to organization "
                f"{by_phone.organization_id}. Re-provision or deactivate that "
                "organization's line first."
            )

    @staticmethod
    def _audited(result: ProvisioningResult) -> ProvisioningResult:
        # Warning level on purpose: this changes which business answers a
        # real phone line, and should stand out in any log review.
        logger.warning(
            "voice_line_provisioned",
            action=result.action,
            organization_id=str(result.line.organization_id),
            previous_organization_id=(
                str(result.previous_organization_id)
                if result.previous_organization_id
                else None
            ),
            vapi_assistant_id=result.line.vapi_assistant_id,
            replaced_assistant_id=result.replaced_assistant_id,
            has_phone_number_id=result.line.vapi_phone_number_id is not None,
        )
        return result
