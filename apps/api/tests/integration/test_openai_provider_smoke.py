"""One real call to OpenAI to prove OpenAIProvider's structured-output
parsing actually matches the live API's response shape. Everything else in
this test suite runs against FakeAIProvider (see tests/fakes.py) to stay
free, deterministic, and offline — this is the single exception.

Auto-skipped unless OPENAI_API_KEY is set in the test environment, so it
never becomes a hard CI dependency."""

import os

import pytest

from app.core.config import get_settings
from app.domain.ai.provider import AIRequest
from app.infrastructure.ai.openai_provider import OpenAIProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set; skipping live OpenAI smoke test.",
)


@pytest.mark.asyncio
async def test_generate_reply_returns_a_well_formed_reply():
    settings = get_settings()
    provider = OpenAIProvider(settings)

    reply = await provider.generate_reply(
        AIRequest(
            system_prompt=(
                "You are the after-hours phone assistant for Acme HVAC. "
                "Business hours are Monday-Friday 8am-5pm. There are no "
                "emergency keywords configured."
            ),
            history=(),
            latest_customer_message="My furnace is making a loud banging noise and won't turn on.",
        )
    )

    assert reply.message_to_customer
    assert reply.classification is not None
    assert 0.0 <= reply.confidence <= 1.0
    assert reply.summary
