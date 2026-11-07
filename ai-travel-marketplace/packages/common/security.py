from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from fastapi import Header, HTTPException, status

ALGORITHM = "HS256"
TOKEN_LIFETIME = timedelta(hours=2)


def _secret() -> str:
    secret = os.getenv("JWT_SECRET", "local-only-insecure-secret")
    if secret == "local-only-insecure-secret":
        raise RuntimeError("JWT_SECRET must be configured before starting a service")
    return secret


def create_access_token(user_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": user_id, "iat": now, "exp": now + TOKEN_LIFETIME},
        _secret(),
        algorithm=ALGORITHM,
    )


def current_user_id(authorization: str | None = Header(default=None)) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _secret(),
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except (jwt.InvalidTokenError, RuntimeError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token subject",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return subject
