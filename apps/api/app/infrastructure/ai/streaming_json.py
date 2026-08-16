"""Incremental extraction of one string field from a JSON document that is
still arriving.

The AI Brain asks OpenAI for a strict JSON object, so a streamed response
arrives as JSON syntax, not prose — forwarding raw deltas to Vapi would
make it speak `{"message_to_customer":"...`. This module decodes just the
`message_to_customer` value as its characters arrive, so the caller hears
the sentence while the model is still producing the decision fields that
follow it.

Nothing here assumes a chunk boundary aligns with anything: a chunk may
split the key, the colon, the opening quote, an escape sequence, or a
surrogate pair. The scanner keeps whatever it cannot yet interpret and
waits for more input rather than guessing."""

from __future__ import annotations

_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}

_HIGH_SURROGATE = range(0xD800, 0xDC00)
_LOW_SURROGATE = range(0xDC00, 0xE000)


class StreamingStringFieldExtractor:
    """Feeds JSON text in, gets decoded characters of one field out.

    Usage is `feed(chunk) -> str`, where the return value is only the
    *newly decoded* characters. Once the field's closing quote is seen the
    extractor is finished and every later `feed` returns an empty string —
    the rest of the document is the caller's problem (it is accumulated
    separately for final validation)."""

    def __init__(self, field: str) -> None:
        self._key = f'"{field}"'
        self._buffer = ""
        self._state = "seek_key"
        self.finished = False

    def feed(self, chunk: str) -> str:
        if self.finished:
            return ""

        self._buffer += chunk
        decoded: list[str] = []

        while True:
            if self._state == "seek_key":
                index = self._buffer.find(self._key)
                if index < 0:
                    # The key itself may straddle chunks, so retain just
                    # enough of the tail that a split key can still match.
                    keep = len(self._key) - 1
                    if len(self._buffer) > keep:
                        self._buffer = self._buffer[-keep:]
                    break
                self._buffer = self._buffer[index + len(self._key) :]
                self._state = "seek_colon"

            elif self._state == "seek_colon":
                index = self._buffer.find(":")
                if index < 0:
                    break
                self._buffer = self._buffer[index + 1 :]
                self._state = "seek_open_quote"

            elif self._state == "seek_open_quote":
                index = self._buffer.find('"')
                if index < 0:
                    # Only whitespace can legally precede the quote, so
                    # nothing here is worth keeping.
                    self._buffer = ""
                    break
                self._buffer = self._buffer[index + 1 :]
                self._state = "in_string"

            elif self._state == "in_string":
                consumed, text, closed = _scan_string(self._buffer)
                self._buffer = self._buffer[consumed:]
                if text:
                    decoded.append(text)
                if closed:
                    self._state = "finished"
                    self.finished = True
                # Either the string closed or the remainder is an
                # incomplete escape — both mean stop until more input.
                break

            else:
                break

        return "".join(decoded)


def _scan_string(source: str) -> tuple[int, str, bool]:
    """Decodes JSON string content until the closing quote or an
    unresolvable tail.

    Returns `(characters_consumed, decoded_text, closed)`. Anything the
    scanner cannot finish — a lone backslash, a truncated `\\uXXXX`, half a
    surrogate pair — is left unconsumed so the next chunk completes it."""
    out: list[str] = []
    index = 0
    length = len(source)

    while index < length:
        char = source[index]

        if char == '"':
            return index + 1, "".join(out), True

        if char != "\\":
            out.append(char)
            index += 1
            continue

        # --- escape sequence ---
        if index + 1 >= length:
            break  # lone trailing backslash; wait for the rest
        marker = source[index + 1]

        if marker != "u":
            simple = _ESCAPES.get(marker)
            if simple is None:
                # Not valid JSON, but emitting the raw character is safer
                # for a live caller than aborting a sentence mid-word.
                out.append(marker)
            else:
                out.append(simple)
            index += 2
            continue

        if index + 6 > length:
            break  # \uXXXX not fully arrived
        try:
            code = int(source[index + 2 : index + 6], 16)
        except ValueError:
            out.append(source[index + 1])
            index += 2
            continue

        if code in _HIGH_SURROGATE:
            # Needs its low surrogate partner (another 6 characters) before
            # it can become a real codepoint.
            if index + 12 > length:
                break
            if source[index + 6 : index + 8] == "\\u":
                try:
                    low = int(source[index + 8 : index + 12], 16)
                except ValueError:
                    low = -1
                if low in _LOW_SURROGATE:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    index += 12
                    continue
            # Unpaired high surrogate: skip it rather than emit a value
            # that cannot be encoded to UTF-8 on the way out.
            index += 6
            continue

        if code in _LOW_SURROGATE:
            index += 6  # orphaned low surrogate; same reasoning
            continue

        out.append(chr(code))
        index += 6

    return index, "".join(out), False
