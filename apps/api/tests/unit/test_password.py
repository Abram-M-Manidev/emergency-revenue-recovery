import pytest

from app.infrastructure.security.password import hash_password, verify_password


def test_hash_password_is_not_plaintext():
    hashed = hash_password("correct-horse-battery-staple")
    assert hashed != "correct-horse-battery-staple"


def test_verify_password_accepts_correct_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_verify_password_rejects_wrong_password():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", hashed) is False


def test_hash_password_rejects_overlong_password():
    with pytest.raises(ValueError):
        hash_password("x" * 100)
