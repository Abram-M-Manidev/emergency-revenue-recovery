"""Password hashing via bcrypt, isolated behind two functions so the hashing
scheme can change later (e.g. to argon2) without touching call sites."""

from __future__ import annotations

import bcrypt

_BCRYPT_MAX_BYTES = 72  # bcrypt silently truncates beyond this; reject instead


def hash_password(plain_password: str) -> str:
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > _BCRYPT_MAX_BYTES:
        raise ValueError("Password exceeds maximum supported length.")
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    password_bytes = plain_password.encode("utf-8")
    if len(password_bytes) > _BCRYPT_MAX_BYTES:
        return False
    return bcrypt.checkpw(password_bytes, hashed_password.encode("utf-8"))
