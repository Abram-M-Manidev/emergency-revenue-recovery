from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

_BCRYPT_MAX_BYTES = 72


def _fits_bcrypt(value: str) -> str:
    """`max_length=72` counts characters; bcrypt's limit is 72 BYTES. A
    password of 40 accented characters passed the field check and then made
    `hash_password` raise a bare `ValueError`, which surfaced as a 500 on
    registration and on inviting a teammate or technician."""
    if len(value.encode("utf-8")) > _BCRYPT_MAX_BYTES:
        raise ValueError("Password is too long (at most 72 bytes).")
    return value


# A password being SET (registration, invitations). Login deliberately does
# not use this: an over-long attempt there is simply a wrong password.
NewPassword = Annotated[str, Field(min_length=8, max_length=72), AfterValidator(_fits_bcrypt)]


class RegisterRequest(BaseModel):
    organization_name: str = Field(min_length=2, max_length=255)
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    password: NewPassword


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    organization_id: uuid.UUID
    is_superuser: bool
    roles: list[str]
    permissions: list[str]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class AuthResponse(BaseModel):
    tokens: TokenResponse
    user: UserProfileResponse
