from __future__ import annotations

import re
import uuid

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    slug = _NON_ALNUM.sub("-", value.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]
