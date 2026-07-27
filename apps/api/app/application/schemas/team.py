from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class TeamMemberResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    roles: list[str]
    is_active: bool
    is_superuser: bool
    last_login_at: datetime | None
    created_at: datetime


class InviteTeamMemberRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    # bcrypt (see infrastructure/security/password.py) silently truncates
    # beyond 72 bytes and this codebase rejects that instead — capping the
    # request field here keeps that a normal 422, not a raw ValueError.
    temporary_password: str = Field(min_length=8, max_length=72)
    role: Literal["Admin", "Member"]


class UpdateMemberStatusRequest(BaseModel):
    is_active: bool


class UpdateMemberRoleRequest(BaseModel):
    role: Literal["Owner", "Admin", "Member"]
