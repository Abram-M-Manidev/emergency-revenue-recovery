"""What a caller is told about who — or what — they are talking to, and
whether the call is recorded.

Where this is decided, and where it is not
------------------------------------------
A call reaches ERRS through Vapi:

    PSTN -> Twilio number -> Vapi answers -> [Vapi first message] ->
    caller speaks -> Vapi Custom-LLM request -> ERRS -> reply spoken by Vapi

Vapi answers the call, and Vapi — not ERRS — decides whether it is recorded
(the assistant's recording/artifact settings) and from which moment. A
static `firstMessage` configured on the Vapi assistant is spoken before ERRS
receives anything. ERRS therefore cannot guarantee that a disclosure precedes
recording, nor that it precedes a greeting it never sees.

What ERRS *can* guarantee, and does:

- If the Vapi assistant is set to let the model speak first
  (`firstMessageMode: assistant-speaks-first-with-model-generated-message`),
  Vapi asks ERRS for the opening words, and ERRS answers deterministically —
  no model involved — with the greeting and this disclosure. Those are the
  first words of the call that come from anything ERRS controls.
- Otherwise, the first reply ERRS gives on a call begins with this
  disclosure, emitted before a single word of model output, and recorded on
  the call. Nothing the caller says can skip it, because it is not decided
  by the model.

The wording is fixed here, not free text, and never placed in a prompt for
the model to reproduce: the point is that it is said the same way every
time, whatever the conversation does. It makes no legal claim and names no
jurisdiction; a business decides with its own advisers which notices it
needs, and this makes whichever it chooses reliable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

# Business names come from tenant data; a pathological one must not turn the
# disclosure into a minute of speech.
_MAX_NAME_CHARS = 80


@dataclass(frozen=True, slots=True)
class DisclosurePolicy:
    """A business's chosen caller notices.

    Both default ON. A tenant that has never configured anything gets the
    fuller notice: telling a caller too much is recoverable, telling them
    too little is not."""

    #: Tell the caller they are speaking with an automated assistant.
    ai_disclosure: bool = True
    #: Tell the caller the call is recorded. This states the business's own
    #: declaration that its calls are recorded (recording itself is a Vapi
    #: setting ERRS does not control).
    recording_notice: bool = True

    @property
    def says_anything(self) -> bool:
        return self.ai_disclosure or self.recording_notice


#: What a tenant with no saved configuration gets — and what is used when
#: the configuration cannot be read at all.
DEFAULT_DISCLOSURE_POLICY = DisclosurePolicy()


@dataclass(frozen=True, slots=True)
class DisclosureSettings:
    """The stored, Owner-managed policy for one organization."""

    organization_id: uuid.UUID
    policy: DisclosurePolicy
    created_at: datetime | None = None
    updated_at: datetime | None = None


def spoken_business_name(name: str | None) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return "this business"
    return cleaned if len(cleaned) <= _MAX_NAME_CHARS else cleaned[:_MAX_NAME_CHARS].rstrip()


def disclosure_sentence(policy: DisclosurePolicy, business_name: str | None) -> str | None:
    """The notice spoken before ERRS's first reply on a call, or None when
    the business has switched both notices off."""
    name = spoken_business_name(business_name)
    if policy.ai_disclosure and policy.recording_notice:
        return f"You're speaking with an automated assistant for {name}, and this call is recorded."
    if policy.ai_disclosure:
        return f"You're speaking with an automated assistant for {name}."
    if policy.recording_notice:
        return "Please note that this call is recorded."
    return None


def opening_message(policy: DisclosurePolicy, business_name: str | None) -> str:
    """The first words of the call when Vapi lets ERRS speak first:
    greeting, notice, invitation — deterministic, no model involved."""
    name = spoken_business_name(business_name)
    notice = disclosure_sentence(policy, business_name)
    parts = [f"Thanks for calling {name}."]
    if notice:
        parts.append(notice)
    parts.append("How can I help you today?")
    return " ".join(parts)


#: When Vapi asks for opening words again on a call already greeted (a
#: retried request), there is nothing new to disclose.
REPEAT_OPENING_MESSAGE = "How can I help you today?"
