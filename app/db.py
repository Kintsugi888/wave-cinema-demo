"""Wave Cinema persistent storage — SQLAlchemy models over Postgres.

Replaces the ephemeral JSON file storage (app/data/*.json) and the
backend's workspace.json files. All tables are created on startup
if they don't exist.

Uses DATABASE_URL env var (Render Postgres internal URL). Falls back
to SQLite for local development.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (Column, DateTime, Integer, LargeBinary, String, Text,
                        create_engine, JSON)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

Base = declarative_base()


def _utcnow():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True)  # Google sub or email-based ID
    email = Column(String, unique=True, index=True)
    name = Column(String)
    credits = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    # Encrypted BYOK keys stored as JSON: {"higgsfield": "...", "gemini": "..."}
    byok_keys = Column(Text, default="{}")


class Project(Base):
    __tablename__ = "projects"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    owner_id = Column(String, index=True)  # User.id
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    # Full project workspace state as JSONB-compatible dict
    workspace = Column(JSON, default=dict)


class ApiToken(Base):
    __tablename__ = "api_tokens"
    id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    name = Column(String)
    token_hash = Column(String, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked = Column(Integer, default=0)  # 0 = active, 1 = revoked


class Quote(Base):
    __tablename__ = "quotes"
    id = Column(String, primary_key=True)
    project_id = Column(String, index=True)
    data = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    id = Column(String, primary_key=True)
    project_id = Column(String, index=True)
    kind = Column(String)  # song, character, background
    filename = Column(String)
    content_type = Column(String)
    data = Column(LargeBinary)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


def get_engine():
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url.startswith("postgres://"):
        # Render uses postgres://, SQLAlchemy needs postgresql://
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    if not db_url:
        # Local fallback: SQLite
        local_db = Path(__file__).parent / "data" / "wavecinema.db"
        local_db.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{local_db}"
    return create_engine(db_url, pool_pre_ping=True)


_engine = None
_Session = None


def get_session():
    global _engine, _Session
    if _engine is None:
        _engine = get_engine()
        Base.metadata.create_all(_engine)
        _Session = sessionmaker(bind=_engine)
    return _Session()


def init_db():
    """Create all tables. Called on app startup."""
    engine = get_engine()
    Base.metadata.create_all(engine)
