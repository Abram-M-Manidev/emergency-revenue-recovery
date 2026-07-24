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
