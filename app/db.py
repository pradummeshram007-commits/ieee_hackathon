"""Persistence: SQLAlchemy over SQLite by default, PostgreSQL when asked.

Three tables, and deliberately only three:

``users``      one row per account (farmer, driver, buyer, coordinator).
``sessions``   one row per issued bearer token.
``app_state``  a single row holding the whole planning world as JSON --
               farmers, buyers, trucks, the current plan, and every trip and
               stop status -- plus a monotonic ``version`` counter.

Keeping the planning world as one JSON document rather than a dozen normalised
tables is a considered choice: the domain objects are already pydantic models
that round-trip losslessly, every mutation is a whole-world update guarded by
one lock, and a single process serves every session. Normalising it would buy
nothing here and cost the ability to hand the solver a ``Dataset`` directly.

``DATABASE_URL`` switches the backend. A bare ``postgres://`` prefix (what
several hosts still hand out) is rewritten to ``postgresql+psycopg://`` so the
psycopg 3 driver is used.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

#: Where the SQLite file lives when DATABASE_URL is unset.
DEFAULT_SQLITE_PATH = os.environ.get("KISANROUTE_DB_PATH", "kisanroute.db")

#: The single app_state row's primary key.
STATE_ROW_ID = 1


def database_url() -> str:
    """Resolve the database URL, normalising the legacy ``postgres://`` form."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DEFAULT_SQLITE_PATH}"
    if url.startswith("postgres://"):
        # Heroku-style URLs name a driver SQLAlchemy 2 no longer ships.
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: F07 / T2 / B1 for farmer, driver and buyer accounts; NULL for the coordinator.
    ref_id: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    village: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    lat: Mapped[Optional[float]] = mapped_column(nullable=True)
    lng: Mapped[Optional[float]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SessionToken(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AppStateRow(Base):
    __tablename__ = "app_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=STATE_ROW_ID)
    #: Bumped on every change; the frontend polls against it.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


_engine = None
_SessionFactory = None


def engine():
    """Lazily build the engine so importing this module touches no filesystem."""
    global _engine, _SessionFactory
    if _engine is None:
        url = database_url()
        kwargs = {"future": True, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            # One process, several request threads: SQLite needs telling that a
            # connection may cross threads, and a timeout so a concurrent write
            # waits instead of raising "database is locked".
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kwargs)
        _SessionFactory = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


def init_db() -> None:
    """Create tables if they do not exist. Safe to call repeatedly."""
    Base.metadata.create_all(engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on failure."""
    engine()
    assert _SessionFactory is not None
    db = _SessionFactory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Small typed helpers used by auth and the workflow store
# --------------------------------------------------------------------------- #
def get_user(db: Session, username: str) -> Optional[User]:
    return db.scalar(select(User).where(User.username == username))


def get_session_token(db: Session, token: str) -> Optional[SessionToken]:
    return db.get(SessionToken, token)


def load_state_row(db: Session) -> Optional[AppStateRow]:
    return db.get(AppStateRow, STATE_ROW_ID)


def save_state_row(db: Session, payload: str, version: int) -> AppStateRow:
    row = db.get(AppStateRow, STATE_ROW_ID)
    if row is None:
        row = AppStateRow(id=STATE_ROW_ID, version=version, data=payload, updated_at=_utcnow())
        db.add(row)
    else:
        row.version = version
        row.data = payload
        row.updated_at = _utcnow()
    return row


def reset_engine_for_tests() -> None:
    """Drop the cached engine so a test can point DATABASE_URL somewhere else."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


def get_user_by_ref(db: Session, role: str, ref_id: str) -> Optional[User]:
    """The account that owns a truck or a buyer, used when sending them news."""
    return db.scalar(select(User).where(User.role == role, User.ref_id == ref_id))
