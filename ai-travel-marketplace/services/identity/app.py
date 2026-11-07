from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Generator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.types import JSON

from packages.common.database import service_lifespan, session_dependency
from packages.common.security import create_access_token, current_user_id

PASSWORD_ITERATIONS = 310_000


class IdentityBase(DeclarativeBase):
    pass


class User(IdentityBase):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(120))
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class PreferencesRequest(BaseModel):
    preferences: dict[str, Any]


class UserView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    display_name: str
    preferences: dict[str, Any]


def _password_hash(password: str, salt: bytes | None = None) -> str:
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), actual_salt, PASSWORD_ITERATIONS
    )
    return f"{actual_salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", maxsplit=1)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            PASSWORD_ITERATIONS,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _seed(session: Session) -> None:
    del session


def _session(request: Request) -> Generator[Session, None, None]:
    yield from session_dependency(request)


app = FastAPI(
    title="Identity Service",
    version="0.1.0",
    lifespan=service_lifespan(
        base=IdentityBase,
        seed=_seed,
        default_database_url="sqlite:///./identity.db",
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"service": "identity", "status": "ok"}


@app.post("/auth/register", response_model=UserView, status_code=201)
def register(
    request: RegisterRequest,
    session: Session = Depends(_session),
) -> User:
    email = request.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="A valid email address is required")
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=_password_hash(request.password),
        display_name=request.display_name.strip(),
        preferences={},
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Email is already registered") from error
    session.refresh(user)
    return user


@app.post("/auth/login")
def login(
    request: LoginRequest,
    session: Session = Depends(_session),
) -> dict[str, str]:
    email = request.email.strip().lower()
    user = session.scalar(select(User).where(User.email == email))
    if user is None or not _password_matches(request.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"access_token": create_access_token(user.id), "token_type": "bearer"}


@app.get("/users/me", response_model=UserView)
def get_me(
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return user


@app.put("/users/me/preferences", response_model=UserView)
def update_preferences(
    request: PreferencesRequest,
    user_id: str = Depends(current_user_id),
    session: Session = Depends(_session),
) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    user.preferences = request.preferences
    session.commit()
    session.refresh(user)
    return user
