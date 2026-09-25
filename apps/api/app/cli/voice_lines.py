"""Provision which organization a Vapi assistant answers for.

Run inside the API container (operator access only — there is deliberately no
HTTP route for this; see `VoiceLineProvisioningService`):

    docker compose exec api python -m app.cli.voice_lines list
    docker compose exec api python -m app.cli.voice_lines assign \
        --org-slug lucky-hvac-services \
        --assistant-id 0796eff8-5b24-4e92-960f-6c24ce28b4a9 \
        [--phone-number-id <vapi phone number id>] [--phone-number +16305550100] \
        [--reassign-from <current organization id>] [--replace-existing]
    docker compose exec api python -m app.cli.voice_lines deactivate --org-slug ...
    docker compose exec api python -m app.cli.voice_lines activate --org-slug ...

Every change is one transaction: it either fully applies or leaves the
routing table exactly as it was. The table is printed after each change so
the operator sees which business now answers the line.

What this cannot do: create the assistant or phone number in Vapi, or verify
that the assistant's Custom-LLM URL and `x-vapi-secret` point at this
deployment. That is done in the Vapi dashboard (see docs/PILOT_LAUNCH.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.voice_line_provisioning_service import (
    VoiceLineProvisioningError,
    VoiceLineProvisioningService,
)
from app.domain.entities.organization import Organization
from app.domain.exceptions import DomainError
from app.infrastructure.database.repositories import (
    SqlAlchemyOrganizationRepository,
    SqlAlchemyVoiceLineRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal


async def _resolve_org(
    organizations: SqlAlchemyOrganizationRepository, args: argparse.Namespace
) -> Organization:
    if args.org_id:
        organization = await organizations.get_by_id(uuid.UUID(args.org_id))
    else:
        organization = await organizations.get_by_slug(args.org_slug)
    if organization is None:
        raise VoiceLineProvisioningError("No such organization.")
    return organization


async def _print_table(session: AsyncSession) -> None:
    lines = await SqlAlchemyVoiceLineRepository(session).list_all()
    organizations = SqlAlchemyOrganizationRepository(session)
    print("\nVoice line routing table:")
    if not lines:
        print("  (no voice lines)")
    for line in lines:
        organization = await organizations.get_by_id(line.organization_id)
        name = f"{organization.name} [{organization.slug}]" if organization else "?"
        state = "active" if line.is_active else "INACTIVE"
        print(
            f"  assistant {line.vapi_assistant_id} -> {name} ({line.organization_id}) "
            f"number={line.phone_number or '-'} phone_id={line.vapi_phone_number_id or '-'} {state}"
        )


async def _run(args: argparse.Namespace) -> int:
    async with AsyncSessionLocal() as session:
        organizations = SqlAlchemyOrganizationRepository(session)
        service = VoiceLineProvisioningService(
            voice_line_repository=SqlAlchemyVoiceLineRepository(session),
            organization_repository=organizations,
        )
        try:
            if args.command == "list":
                await _print_table(session)
                return 0
            organization = await _resolve_org(organizations, args)
            if args.command == "assign":
                result = await service.provision(
                    organization_id=organization.id,
                    vapi_assistant_id=args.assistant_id,
                    vapi_phone_number_id=args.phone_number_id,
                    phone_number=args.phone_number,
                    confirm_reassign_from=(
                        uuid.UUID(args.reassign_from) if args.reassign_from else None
                    ),
                    replace_existing=args.replace_existing,
                )
                print(f"{result.action}: assistant {result.line.vapi_assistant_id} now answers for "
                      f"{organization.name} [{organization.slug}]")
                if result.previous_organization_id:
                    print(f"  (moved from organization {result.previous_organization_id})")
                if result.replaced_assistant_id:
                    print(f"  (replaced assistant {result.replaced_assistant_id})")
            else:
                await service.set_active(organization.id, is_active=args.command == "activate")
                print(f"{args.command}d the voice line for {organization.name} [{organization.slug}]")
            await session.commit()
        except (DomainError, ValueError) as exc:
            await session.rollback()
            message = exc.message if isinstance(exc, DomainError) else str(exc)
            print(f"REFUSED: {message}", file=sys.stderr)
            return 2
        await _print_table(session)
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli.voice_lines")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show every assistant -> organization mapping")

    def with_org(sub: argparse.ArgumentParser) -> None:
        target = sub.add_mutually_exclusive_group(required=True)
        target.add_argument("--org-slug")
        target.add_argument("--org-id")

    assign = commands.add_parser("assign", help="map an assistant to an organization")
    with_org(assign)
    assign.add_argument("--assistant-id", required=True)
    assign.add_argument("--phone-number-id")
    assign.add_argument("--phone-number", help="E.164, e.g. +16305550100")
    assign.add_argument(
        "--reassign-from",
        help="the organization id the assistant currently answers for; required to move it",
    )
    assign.add_argument(
        "--replace-existing",
        action="store_true",
        help="allow retiring the target organization's current assistant",
    )
    for name in ("deactivate", "activate"):
        with_org(commands.add_parser(name, help=f"{name} an organization's voice line"))
    return parser


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
