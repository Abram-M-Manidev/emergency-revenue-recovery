import pytest
from pydantic import ValidationError

from app.application.schemas.ai_conversations import SendMessageRequest, StartConversationRequest


def test_empty_message_is_rejected():
    with pytest.raises(ValidationError):
        SendMessageRequest(message="")


def test_overlong_message_is_rejected():
    with pytest.raises(ValidationError):
        SendMessageRequest(message="x" * 2001)


def test_valid_message_is_accepted():
    request = SendMessageRequest(message="My furnace stopped working.")
    assert request.message == "My furnace stopped working."


def test_start_conversation_caller_phone_is_optional():
    request = StartConversationRequest()
    assert request.caller_phone_number is None

    with_phone = StartConversationRequest(caller_phone_number="+15551234567")
    assert with_phone.caller_phone_number == "+15551234567"


def test_overlong_caller_phone_is_rejected():
    with pytest.raises(ValidationError):
        StartConversationRequest(caller_phone_number="1" * 33)


def test_a_message_without_interruption_serialises_as_before_plus_two_empty_fields():
    import uuid
    from datetime import datetime, timezone

    from app.application.schemas.ai_conversations import MessageResponse
    from app.domain.entities.conversation_message import ConversationMessage, MessageRole

    now = datetime.now(timezone.utc)
    plain = MessageResponse.model_validate(ConversationMessage(
        id=uuid.uuid4(), conversation_id=uuid.uuid4(), role=MessageRole.ASSISTANT,
        content="Hello.", created_at=now,
    )).model_dump(mode="json")
    assert plain["interrupted_at"] is None and plain["interrupted"] is False
    assert {"id", "role", "content", "created_at"} <= plain.keys()

    cut = MessageResponse.model_validate(ConversationMessage(
        id=uuid.uuid4(), conversation_id=uuid.uuid4(), role=MessageRole.ASSISTANT,
        content="Hello.", created_at=now, interrupted_at=now,
    )).model_dump(mode="json")
    assert cut["interrupted"] is True and cut["interrupted_at"] is not None
