"""Small JWT boundary for organizer identity in the local POC."""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import jwt

_ALGORITHM = "HS256"
_AUDIENCE = "where-are-we-eating"
_USER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _secret() -> str:
    secret = os.getenv("GROUP_RESERVATIONS_JWT_SECRET")
    if len(secret) < 32:
        raise RuntimeError("GROUP_RESERVATIONS_JWT_SECRET must be at least 32 characters")
    return secret


def validate_user_id(user_id: str) -> str:
    if not _USER_ID.fullmatch(user_id):
        raise ValueError("user_id must be 1-128 safe identifier characters")
    return user_id


def mint_access_token(user_id: str, *, ttl_minutes: int = 60) -> str:
    """Mint an app token; this token is never sent to OpenTable."""
    validate_user_id(user_id)
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=ttl_minutes),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM)


def verify_access_token(token: str) -> str:
    """Verify and return the organizer ID from an app-issued token."""
    claims = jwt.decode(
        token,
        _secret(),
        algorithms=[_ALGORITHM],
        audience=_AUDIENCE,
        options={"require": ["sub", "aud", "iat", "exp", "jti"]},
    )
    return validate_user_id(str(claims["sub"]))
