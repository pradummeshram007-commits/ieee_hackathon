"""Accounts, password hashing and bearer tokens -- stdlib only.

Passwords are stored as ``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``
using :func:`hashlib.pbkdf2_hmac`, and compared with
:func:`secrets.compare_digest` so verification takes the same time whatever the
input. Tokens are :func:`secrets.token_urlsafe` values kept in the ``sessions``
table and sent back as ``Authorization: Bearer <token>``.

The iteration count is deliberately high enough to be defensible and
overridable through ``KISANROUTE_PBKDF2_ITERATIONS`` so the test suite, which
creates 33 accounts from scratch, does not spend a minute on key derivation.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Header, HTTPException
from sqlalchemy.orm import Session

from . import db as database
from .models import Role, UserPublic

PBKDF2_ALGORITHM = "pbkdf2_sha256"
PBKDF2_ITERATIONS = int(os.environ.get("KISANROUTE_PBKDF2_ITERATIONS", "200000"))
SALT_BYTES = 16
TOKEN_BYTES = 32

#: How long a bearer token stays usable.
SESSION_TTL = timedelta(days=7)


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PBKDF2_ALGORITHM}${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != PBKDF2_ALGORITHM:
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(derived.hex(), expected_hex)


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(
    db: Session,
    *,
    username: str,
    password: str,
    role: str,
    ref_id: Optional[str],
    display_name: str,
    village: Optional[str] = None,
    phone: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
) -> database.User:
    user = database.User(
        username=username.strip().lower(),
        password_hash=hash_password(password),
        role=role,
        ref_id=ref_id,
        display_name=display_name,
        village=village,
        phone=phone,
        lat=lat,
        lng=lng,
    )
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, username: str, password: str) -> Optional[database.User]:
    user = database.get_user(db, username.strip().lower())
    if user is None:
        # Spend the same work on an unknown username so timing does not leak
        # which accounts exist.
        verify_password(password, hash_password("x", iterations=PBKDF2_ITERATIONS))
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def public_user(user: database.User) -> UserPublic:
    return UserPublic(
        username=user.username,
        role=Role(user.role),
        ref_id=user.ref_id,
        display_name=user.display_name,
        village=user.village,
        phone=user.phone,
        lat=user.lat,
        lng=user.lng,
    )


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def issue_token(db: Session, username: str) -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.add(database.SessionToken(token=token, username=username))
    return token


def revoke_token(db: Session, token: str) -> bool:
    row = database.get_session_token(db, token)
    if row is None:
        return False
    db.delete(row)
    return True


def user_for_token(db: Session, token: str) -> Optional[database.User]:
    """Resolve a bearer token, refreshing ``last_seen`` and expiring old ones."""
    row = database.get_session_token(db, token)
    if row is None:
        return None

    created = row.created_at
    if created is not None:
        if created.tzinfo is None:            # SQLite hands back naive datetimes
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created > SESSION_TTL:
            db.delete(row)
            return None

    row.last_seen = datetime.now(timezone.utc)
    return database.get_user(db, row.username)


# --------------------------------------------------------------------------- #
# FastAPI plumbing
# --------------------------------------------------------------------------- #
def bearer_token(authorization: Optional[str] = Header(default=None)) -> str:
    """Pull the token out of an Authorization header, or 401."""
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="expected 'Authorization: Bearer <token>'")
    return token.strip()
