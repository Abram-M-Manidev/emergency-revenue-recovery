"""Voice-line provisioning against real PostgreSQL.

Replaces hand-written SQL as the way an assistant is mapped to a business.
The failure this exists to prevent already happened once (2026-09-23): the
pilot assistant still routed to a QA organization and the first real call
answered as the wrong business. Every ambiguous change must be refused unless
the operator states the intent, and the database must make "one assistant,
two tenants" impossible regardless.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from fastapi import Header
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_ai_provider, verify_vapi_secret
from app.application.services.voice_line_provisioning_service import (
    VoiceLineProvisioningError,
    VoiceLineProvisioningService,
)
from app.cli import voice_lines as cli
from app.domain.entities.voice_line import VoiceProvider
from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.models.voice_call import VoiceCallModel
from app.infrastructure.database.models.voice_line import VoiceLineModel
from app.infrastructure.database.repositories import (
    SqlAlchemyOrganizationRepository,
    SqlAlchemyVoiceLineRepository,
)
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app, fastapi_app
from tests.fakes import ScriptedToolAIProvider, default_reply

_SECRET = "test-vapi-secret-provisioning"


def _secret_override(x_vapi_secret: str | None = Header(default=None)) -> None:
    if x_vapi_secret != _SECRET:
        from app.domain.exceptions import InvalidTokenError

        raise InvalidTokenError("Missing or invalid Vapi webhook secret.")


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def database_ready():
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Database not reachable, skipping integration test: {exc}")
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(loop_scope="session")
async def client(database_ready):
    scripted = ScriptedToolAIProvider()
    fastapi_app.dependency_overrides[get_ai_provider] = lambda: scripted
    fastapi_app.dependency_overrides[verify_vapi_secret] = _secret_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, scripted
    fastapi_app.dependency_overrides.pop(get_ai_provider, None)
    fastapi_app.dependency_overrides.pop(verify_vapi_secret, None)


async def _register(client: AsyncClient, label: str) -> tuple[str, uuid.UUID, str]:
    suffix = uuid.uuid4().hex[:8]
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": f"{label} {suffix}",
            "full_name": "Owner",
            "email": f"prov-{label.lower().replace(' ', '-')}-{suffix}@example.com",
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    org_id = uuid.UUID(body["user"]["organization_id"])
    async with AsyncSessionLocal() as session:
        slug = (await SqlAlchemyOrganizationRepository(session).get_by_id(org_id)).slug
    return body["tokens"]["access_token"], org_id, slug


async def _provision(**kwargs):
    async with AsyncSessionLocal() as session:
        service = VoiceLineProvisioningService(
            voice_line_repository=SqlAlchemyVoiceLineRepository(session),
            organization_repository=SqlAlchemyOrganizationRepository(session),
        )
        try:
            result = await service.provision(**kwargs)
        except Exception:
            await session.rollback()
            raise
        await session.commit()
        return result


async def _line_for_assistant(assistant_id: str) -> VoiceLineModel | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(VoiceLineModel).where(VoiceLineModel.vapi_assistant_id == assistant_id)
            )
        ).scalar_one_or_none()


def _new_id() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio(loop_scope="session")
async def test_a_provisioned_line_routes_calls_and_is_visible_only_to_its_owner(client):
    """Written through the ORM (so the enum-casing trap of hand-written SQL
    cannot recur), answers a real webhook call, and is visible to its own
    organization's Owner — and not to anyone else's."""
    http, scripted = client
    token_a, org_a, _ = await _register(http, "Visible A")
    token_b, _, _ = await _register(http, "Visible B")
    assistant = _new_id()

    result = await _provision(
        organization_id=org_a, vapi_assistant_id=assistant, phone_number="+16305550100"
    )
    assert result.action == "created"
    assert result.line.provider is VoiceProvider.VAPI

    scripted.queue_reply(default_reply(message_to_customer="Hello from A."))
    call_id = f"call_prov_{uuid.uuid4().hex[:8]}"
    call = await http.post(
        "/api/v1/voice/vapi/chat/completions",
        json={"call": {"id": call_id, "assistantId": assistant}, "messages": [{"role": "user", "content": "Hi"}]},
        headers={"x-vapi-secret": _SECRET},
    )
    assert call.status_code == 200
    assert call.json()["choices"][0]["message"]["content"] == "Hello from A."
    async with AsyncSessionLocal() as session:
        voice_call = (
            await session.execute(select(VoiceCallModel).where(VoiceCallModel.vapi_call_id == call_id))
        ).scalar_one()
    assert voice_call.organization_id == org_a

    mine = await http.get("/api/v1/voice/line", headers={"Authorization": f"Bearer {token_a}"})
    theirs = await http.get("/api/v1/voice/line", headers={"Authorization": f"Bearer {token_b}"})
    assert mine.status_code == 200 and mine.json()["vapi_assistant_id"] == assistant
    assert theirs.status_code == 200 and theirs.json() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_an_assistant_is_never_silently_moved_to_another_organization(client):
    http, _ = client
    _, org_a, _ = await _register(http, "Owner A")
    _, org_b, _ = await _register(http, "Claimant B")
    assistant = _new_id()
    await _provision(organization_id=org_a, vapi_assistant_id=assistant)

    # No confirmation: refused.
    with pytest.raises(VoiceLineProvisioningError, match="--reassign-from"):
        await _provision(organization_id=org_b, vapi_assistant_id=assistant)
    # "Confirming" the wrong owner (e.g. the target itself): refused.
    with pytest.raises(VoiceLineProvisioningError):
        await _provision(
            organization_id=org_b, vapi_assistant_id=assistant, confirm_reassign_from=org_b
        )
    assert (await _line_for_assistant(assistant)).organization_id == org_a

    # Naming the current owner is the explicit authorisation.
    moved = await _provision(
        organization_id=org_b, vapi_assistant_id=assistant, confirm_reassign_from=org_a
    )
    assert moved.action == "reassigned"
    assert moved.previous_organization_id == org_a
    line = await _line_for_assistant(assistant)
    assert line.organization_id == org_b
    async with AsyncSessionLocal() as session:
        assert await SqlAlchemyVoiceLineRepository(session).get_by_organization_id(org_a) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_an_organizations_existing_line_is_replaced_only_when_asked(client):
    http, _ = client
    _, org, _ = await _register(http, "Replace")
    first, second = _new_id(), _new_id()
    await _provision(organization_id=org, vapi_assistant_id=first)

    with pytest.raises(VoiceLineProvisioningError, match="--replace-existing"):
        await _provision(organization_id=org, vapi_assistant_id=second)
    assert (await _line_for_assistant(first)) is not None

    replaced = await _provision(organization_id=org, vapi_assistant_id=second, replace_existing=True)
    assert replaced.replaced_assistant_id == first
    assert await _line_for_assistant(first) is None
    assert (await _line_for_assistant(second)).organization_id == org


@pytest.mark.asyncio(loop_scope="session")
async def test_reassigning_into_an_organization_that_has_a_line_needs_both_confirmations(client):
    http, _ = client
    _, org_a, _ = await _register(http, "Donor")
    _, org_b, _ = await _register(http, "Recipient")
    moving, existing = _new_id(), _new_id()
    await _provision(organization_id=org_a, vapi_assistant_id=moving)
    await _provision(organization_id=org_b, vapi_assistant_id=existing)

    with pytest.raises(VoiceLineProvisioningError, match="--replace-existing"):
        await _provision(organization_id=org_b, vapi_assistant_id=moving, confirm_reassign_from=org_a)
    result = await _provision(
        organization_id=org_b,
        vapi_assistant_id=moving,
        confirm_reassign_from=org_a,
        replace_existing=True,
    )
    assert result.action == "reassigned"
    assert result.replaced_assistant_id == existing
    assert await _line_for_assistant(existing) is None
    assert (await _line_for_assistant(moving)).organization_id == org_b


@pytest.mark.asyncio(loop_scope="session")
async def test_a_phone_number_routing_elsewhere_is_never_taken(client):
    http, _ = client
    _, org_a, _ = await _register(http, "Phone A")
    _, org_b, _ = await _register(http, "Phone B")
    phone_id = _new_id()
    await _provision(organization_id=org_a, vapi_assistant_id=_new_id(), vapi_phone_number_id=phone_id)

    with pytest.raises(VoiceLineProvisioningError, match="phone number already routes"):
        await _provision(organization_id=org_b, vapi_assistant_id=_new_id(), vapi_phone_number_id=phone_id)
    async with AsyncSessionLocal() as session:
        assert await SqlAlchemyVoiceLineRepository(session).get_by_organization_id(org_b) is None


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"vapi_assistant_id": "asst_not_a_uuid"}, "Vapi id"),
        # The dangerous paste: a credential in the id field. Deliberately not
        # shaped like any real provider's key, so no secret scanner flags it.
        ({"vapi_assistant_id": "pasted-credential-not-an-assistant-id"}, "Vapi id"),
        ({"vapi_assistant_id": str(uuid.uuid4()), "vapi_phone_number_id": "nope"}, "Vapi id"),
        ({"vapi_assistant_id": str(uuid.uuid4()), "phone_number": "630-555-0100"}, "E.164"),
    ],
)
async def test_malformed_identifiers_are_refused_before_anything_is_written(client, kwargs, message):
    http, _ = client
    _, org, _ = await _register(http, "Malformed")
    with pytest.raises(VoiceLineProvisioningError, match=message):
        await _provision(organization_id=org, **kwargs)
    async with AsyncSessionLocal() as session:
        assert await SqlAlchemyVoiceLineRepository(session).get_by_organization_id(org) is None


@pytest.mark.asyncio(loop_scope="session")
async def test_provisioning_is_idempotent(client):
    http, _ = client
    _, org, _ = await _register(http, "Idempotent")
    assistant = _new_id()
    await _provision(organization_id=org, vapi_assistant_id=assistant, phone_number="+16305550101")
    again = await _provision(organization_id=org, vapi_assistant_id=assistant.upper())
    assert again.action == "unchanged"
    assert again.line.phone_number == "+16305550101"


@pytest.mark.asyncio(loop_scope="session")
async def test_the_database_itself_forbids_one_assistant_for_two_organizations(client):
    """The last line of defence, independent of the service: even raw SQL
    cannot map one assistant to two tenants."""
    http, _ = client
    _, org_a, _ = await _register(http, "Unique A")
    _, org_b, _ = await _register(http, "Unique B")
    assistant = _new_id()
    await _provision(organization_id=org_a, vapi_assistant_id=assistant)
    async with AsyncSessionLocal() as session:
        session.add(
            VoiceLineModel(
                organization_id=org_b,
                provider=VoiceProvider.VAPI,
                vapi_assistant_id=assistant,
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio(loop_scope="session")
async def test_the_cli_refuses_without_changing_anything_and_applies_when_confirmed(client, capsys):
    http, _ = client
    _, org_a, slug_a = await _register(http, "Cli A")
    _, org_b, slug_b = await _register(http, "Cli B")
    assistant = _new_id()
    parse = cli._parser().parse_args

    assert await cli._run(parse(["assign", "--org-slug", slug_a, "--assistant-id", assistant])) == 0
    refused = await cli._run(parse(["assign", "--org-slug", slug_b, "--assistant-id", assistant]))
    assert refused == 2
    assert "REFUSED" in capsys.readouterr().err
    assert (await _line_for_assistant(assistant)).organization_id == org_a

    confirmed = await cli._run(
        parse(
            ["assign", "--org-slug", slug_b, "--assistant-id", assistant, "--reassign-from", str(org_a)]
        )
    )
    assert confirmed == 0
    assert (await _line_for_assistant(assistant)).organization_id == org_b
    assert "reassigned" in capsys.readouterr().out

    assert await cli._run(parse(["deactivate", "--org-slug", slug_b])) == 0
    assert (await _line_for_assistant(assistant)).is_active is False
