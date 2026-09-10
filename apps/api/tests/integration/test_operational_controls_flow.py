"""The two operator controls a controlled pilot cannot run without, end to
end against a real Postgres database and the real HTTP stack.

1. **The per-tenant voice kill switch** — stop one business's phone assistant
   without stopping anyone else's, and without taking down the dashboard the
   operator needs in order to investigate.
2. **Emergency notification configuration** — tell ESSR where a business's
   alerts go, which is what turns Phase 1's notification machinery from
   correct-but-inert into something a pilot can actually use.

Both are tenant-scoped, both are Owner-only, and both are asserted here for
the properties that only a real request path can show: that the tenant comes
from the caller's own JWT rather than from anything in the request, that a
lower-privileged member is refused, and — for notifications — that the stored
webhook URL never comes back out of the API.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.infrastructure.database.models import *  # noqa: F401,F403
from app.infrastructure.database.session import AsyncSessionLocal, Base, engine
from app.main import app

_SLACK_STYLE_WEBHOOK = (
    "https://hooks.example.com/services/T01ABCDEFG/B02HIJKLMN/ZZTOPSECRETTOKEN99"
)


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
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(client: AsyncClient, org_name: str, email: str) -> str:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "organization_name": org_name,
            "full_name": "Owner Owner",
            "email": email,
            "password": "super-secret-123",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["tokens"]["access_token"]


async def _invite_member(client: AsyncClient, owner_token: str, email: str) -> str:
    """A Member — the least-privileged non-technician role — used to prove
    these controls are not merely authenticated but authorized."""
    response = await client.post(
        "/api/v1/team/members",
        headers=_auth(owner_token),
        json={
            "full_name": "Mem Ber",
            "email": email,
            "temporary_password": "member-secret-123",
            "role": "Member",
        },
    )
    assert response.status_code in (200, 201), response.text
    login = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "member-secret-123"}
    )
    assert login.status_code == 200, login.text
    return login.json()["tokens"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# =============================================================================
# Voice kill switch
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_voice_assistant_is_enabled_by_default(client: AsyncClient):
    """Every existing organization keeps the behaviour it has today; turning
    the assistant off is always an explicit act."""
    token = await _register(client, "Kill Switch A", "ks-a@example.com")

    response = await client.get("/api/v1/organizations/current", headers=_auth(token))

    assert response.status_code == 200
    assert response.json()["voice_assistant_enabled"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_an_owner_can_disable_and_re_enable_the_voice_assistant(client: AsyncClient):
    token = await _register(client, "Kill Switch B", "ks-b@example.com")

    disabled = await client.patch(
        "/api/v1/organizations/current",
        headers=_auth(token),
        json={"voice_assistant_enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["voice_assistant_enabled"] is False
    # The account itself stays live — the dashboard the operator is using to
    # investigate must not go down with the phone line.
    assert disabled.json()["is_active"] is True

    re_enabled = await client.patch(
        "/api/v1/organizations/current",
        headers=_auth(token),
        json={"voice_assistant_enabled": True},
    )
    assert re_enabled.status_code == 200
    assert re_enabled.json()["voice_assistant_enabled"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_disabling_the_assistant_does_not_rename_the_organization(
    client: AsyncClient,
):
    """Partial update: fields not sent are left alone. Worth pinning because
    an operator flipping this during an incident is not expecting to touch
    anything else."""
    token = await _register(client, "Kill Switch C", "ks-c@example.com")

    response = await client.patch(
        "/api/v1/organizations/current",
        headers=_auth(token),
        json={"voice_assistant_enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Kill Switch C"


@pytest.mark.asyncio(loop_scope="session")
async def test_one_tenants_kill_switch_does_not_touch_another(client: AsyncClient):
    """Two organizations on one deployment. The tenant is taken from the
    caller's JWT and there is no request field naming an organization, so
    this is the property the whole design rests on."""
    first = await _register(client, "Kill Switch D", "ks-d@example.com")
    second = await _register(client, "Kill Switch E", "ks-e@example.com")

    await client.patch(
        "/api/v1/organizations/current",
        headers=_auth(first),
        json={"voice_assistant_enabled": False},
    )

    other = await client.get("/api/v1/organizations/current", headers=_auth(second))
    assert other.status_code == 200
    assert other.json()["voice_assistant_enabled"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_a_member_cannot_flip_the_kill_switch(client: AsyncClient):
    """Authorization, not just authentication. `organization:manage` is
    Owner-only by default and this is exactly the authority it represents."""
    owner = await _register(client, "Kill Switch F", "ks-f@example.com")
    member = await _invite_member(client, owner, "ks-f-member@example.com")

    response = await client.patch(
        "/api/v1/organizations/current",
        headers=_auth(member),
        json={"voice_assistant_enabled": False},
    )

    assert response.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_an_anonymous_caller_cannot_flip_the_kill_switch(client: AsyncClient):
    response = await client.patch(
        "/api/v1/organizations/current", json={"voice_assistant_enabled": False}
    )
    assert response.status_code == 401


# =============================================================================
# Emergency notification configuration
# =============================================================================


@pytest.mark.asyncio(loop_scope="session")
async def test_notifications_start_unconfigured(client: AsyncClient):
    """Null, not an error: a business that has not set alerting up is in an
    ordinary state, and the assistant simply never claims a dispatcher was
    alerted on its calls."""
    token = await _register(client, "Notify A", "nt-a@example.com")

    response = await client.get(
        "/api/v1/organizations/current/notifications", headers=_auth(token)
    )

    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_configuring_a_destination_returns_a_masked_hint_not_the_url(
    client: AsyncClient,
):
    """The single most important assertion in this module. A Slack or Teams
    incoming-webhook URL is the entire credential; if the API hands it back,
    every place that renders a settings page becomes a place it leaks."""
    token = await _register(client, "Notify B", "nt-b@example.com")

    response = await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["channel"] == "webhook"
    assert body["is_enabled"] is True
    # Recognisable...
    assert "hooks.example.com" in body["destination_hint"]
    # ...but not usable.
    assert "ZZTOPSECRETTOKEN99" not in body["destination_hint"]
    assert _SLACK_STYLE_WEBHOOK not in str(body)


@pytest.mark.asyncio(loop_scope="session")
async def test_reading_the_configuration_back_never_exposes_the_url(client: AsyncClient):
    token = await _register(client, "Notify C", "nt-c@example.com")
    await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    response = await client.get(
        "/api/v1/organizations/current/notifications", headers=_auth(token)
    )

    assert response.status_code == 200
    assert "ZZTOPSECRETTOKEN99" not in response.text
    assert "destination" not in response.json()


@pytest.mark.asyncio(loop_scope="session")
async def test_the_destination_is_stored_intact_even_though_it_is_never_returned(
    client: AsyncClient,
):
    """Masking is a presentation rule, not storage. The provider still needs
    the real URL, so this checks the row rather than the response."""
    token = await _register(client, "Notify D", "nt-d@example.com")
    await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    async with AsyncSessionLocal() as session:
        stored = (
            await session.execute(
                text(
                    "SELECT destination FROM organization_notification_settings "
                    "WHERE organization_id = (SELECT id FROM organizations "
                    "WHERE name = 'Notify D')"
                )
            )
        ).scalar_one()

    assert stored == _SLACK_STYLE_WEBHOOK


@pytest.mark.asyncio(loop_scope="session")
async def test_saving_twice_replaces_rather_than_duplicates(client: AsyncClient):
    """One configuration per tenant, enforced by a unique index. PUT is the
    right verb precisely because calling it twice must not leave two rows."""
    token = await _register(client, "Notify E", "nt-e@example.com")
    for destination in (
        "https://hooks.example.com/services/first/aaaa",
        "https://hooks.example.com/services/second/bbbb",
    ):
        response = await client.put(
            "/api/v1/organizations/current/notifications",
            headers=_auth(token),
            json={"channel": "webhook", "destination": destination},
        )
        assert response.status_code == 200, response.text

    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT count(*) FROM organization_notification_settings "
                    "WHERE organization_id = (SELECT id FROM organizations "
                    "WHERE name = 'Notify E')"
                )
            )
        ).scalar_one()
    assert rows == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_alerting_can_be_paused_without_re_entering_the_url(client: AsyncClient):
    """Every extra time a credential is typed or transmitted is another
    chance to leak it, so pausing must not require re-submitting it."""
    token = await _register(client, "Notify F", "nt-f@example.com")
    await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    paused = await client.patch(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"is_enabled": False},
    )
    assert paused.status_code == 200
    assert paused.json()["is_enabled"] is False
    # Still visible while paused — "switched off" and "never configured" must
    # be distinguishable to whoever did the switching.
    assert "hooks.example.com" in paused.json()["destination_hint"]

    resumed = await client.patch(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"is_enabled": True},
    )
    assert resumed.status_code == 200
    assert resumed.json()["is_enabled"] is True


@pytest.mark.asyncio(loop_scope="session")
async def test_pausing_an_unconfigured_organization_is_a_404(client: AsyncClient):
    """A mistake worth surfacing rather than a silent no-op: an operator who
    thinks they just enabled alerting should not be left believing it."""
    token = await _register(client, "Notify G", "nt-g@example.com")

    response = await client.patch(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"is_enabled": True},
    )

    assert response.status_code == 404


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_removes_the_stored_credential(client: AsyncClient):
    """What an operator does when the endpoint was wrong or the webhook has
    been rotated. It must leave no copy of the old credential behind."""
    token = await _register(client, "Notify H", "nt-h@example.com")
    await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    deleted = await client.delete(
        "/api/v1/organizations/current/notifications", headers=_auth(token)
    )
    assert deleted.status_code == 204

    async with AsyncSessionLocal() as session:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) FROM organization_notification_settings "
                    "WHERE organization_id = (SELECT id FROM organizations "
                    "WHERE name = 'Notify H')"
                )
            )
        ).scalar_one()
    assert remaining == 0

    after = await client.get(
        "/api/v1/organizations/current/notifications", headers=_auth(token)
    )
    assert after.json() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_an_unconfigured_organization_is_idempotent(client: AsyncClient):
    token = await _register(client, "Notify I", "nt-i@example.com")

    response = await client.delete(
        "/api/v1/organizations/current/notifications", headers=_auth(token)
    )

    assert response.status_code == 204


# --- validation, over the real request path ----------------------------------


@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.parametrize(
    "destination",
    [
        "https://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/hook",
        "https://10.0.0.1/hook",
        "https://user:pw@example.com/hook",
        "ftp://example.com/hook",
        "not-a-url",
    ],
)
async def test_an_unsafe_destination_is_rejected(client: AsyncClient, destination: str):
    """SSRF and transport rules enforced on the real endpoint, not only in the
    validator's own unit tests — the wiring is as much a part of the control
    as the rule is."""
    token = await _register(
        client, f"Notify Bad {abs(hash(destination)) % 10000}", f"nt-bad-{uuid.uuid4().hex[:8]}@example.com"
    )

    response = await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": destination},
    )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio(loop_scope="session")
async def test_a_rejection_does_not_echo_the_submitted_value_back(client: AsyncClient):
    """An operator can paste the wrong thing — a real token, into the wrong
    field. An error response is not a place to reflect it."""
    token = await _register(client, "Notify J", "nt-j@example.com")
    secret_ish = "https://127.0.0.1/services/T01/B02/REFLECTEDSECRET"

    response = await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(token),
        json={"channel": "webhook", "destination": secret_ish},
    )

    assert response.status_code == 422
    assert "REFLECTEDSECRET" not in response.text


# --- authorization and isolation ---------------------------------------------


@pytest.mark.asyncio(loop_scope="session")
async def test_a_member_cannot_read_or_change_notification_settings(client: AsyncClient):
    owner = await _register(client, "Notify K", "nt-k@example.com")
    member = await _invite_member(client, owner, "nt-k-member@example.com")
    await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(owner),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    read = await client.get(
        "/api/v1/organizations/current/notifications", headers=_auth(member)
    )
    write = await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(member),
        json={"channel": "webhook", "destination": "https://example.com/other"},
    )
    remove = await client.delete(
        "/api/v1/organizations/current/notifications", headers=_auth(member)
    )

    assert read.status_code == 403
    assert write.status_code == 403
    assert remove.status_code == 403


@pytest.mark.asyncio(loop_scope="session")
async def test_an_anonymous_caller_cannot_read_notification_settings(client: AsyncClient):
    response = await client.get("/api/v1/organizations/current/notifications")
    assert response.status_code == 401


@pytest.mark.asyncio(loop_scope="session")
async def test_one_tenant_cannot_see_anothers_notification_configuration(
    client: AsyncClient,
):
    """Two organizations, one deployment. The endpoint takes no organization
    parameter at all, so cross-tenant access is unrepresentable rather than
    merely refused — and this proves the scoping is real."""
    first = await _register(client, "Notify L", "nt-l@example.com")
    second = await _register(client, "Notify M", "nt-m@example.com")

    await client.put(
        "/api/v1/organizations/current/notifications",
        headers=_auth(first),
        json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
    )

    other = await client.get(
        "/api/v1/organizations/current/notifications", headers=_auth(second)
    )

    assert other.status_code == 200
    assert other.json() is None


@pytest.mark.asyncio(loop_scope="session")
async def test_deleting_one_tenants_configuration_leaves_anothers_intact(
    client: AsyncClient,
):
    first = await _register(client, "Notify N", "nt-n@example.com")
    second = await _register(client, "Notify O", "nt-o@example.com")
    for token in (first, second):
        await client.put(
            "/api/v1/organizations/current/notifications",
            headers=_auth(token),
            json={"channel": "webhook", "destination": _SLACK_STYLE_WEBHOOK},
        )

    await client.delete(
        "/api/v1/organizations/current/notifications", headers=_auth(first)
    )

    survivor = await client.get(
        "/api/v1/organizations/current/notifications", headers=_auth(second)
    )
    assert survivor.status_code == 200
    assert survivor.json() is not None
    assert survivor.json()["is_enabled"] is True
