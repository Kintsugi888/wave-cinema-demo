"""Wave Cinema API tokens: scoped bearer tokens for the REST API + MCP server.

Tokens are stored in app/data/api_tokens.json as SHA256 hashes — the raw
token is shown exactly once at creation. Format: `wc_` + 32 hex chars.

Verification uses hmac.compare_digest (constant-time).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
TOKENS_FILE = DATA_DIR / "api_tokens.json"

TOKEN_PREFIX = "wc_"


def _load_tokens() -> list:
    import store as storemod
    if storemod.USE_DB:
        # DB-backed: fetch via store adapter
        from db import ApiToken as DBToken
        s = storemod.get_session()
        try:
            return [{"id": t.id, "user_id": t.user_id, "name": t.name,
                     "token_hash": t.token_hash,
                     "created_at": int(t.created_at.timestamp()) if t.created_at else 0,
                     "last_used_at": int(t.last_used_at.timestamp()) if t.last_used_at else None}
                    for t in s.query(DBToken).filter_by(revoked=0).all()]
        finally:
            s.close()
    try:
        data = json.loads(TOKENS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_tokens(tokens: list) -> None:
    import store as storemod
    if storemod.USE_DB:
        # DB-backed: sync via store adapter (used for last_used_at touches)
        from db import ApiToken as DBToken
        s = storemod.get_session()
        try:
            for t in tokens:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                rec = s.query(DBToken).filter_by(id=t["id"]).first()
                if rec and "last_used_at" in t and t["last_used_at"]:
                    from datetime import datetime, timezone
                    rec.last_used_at = datetime.fromtimestamp(
                        t["last_used_at"], tz=timezone.utc)
            s.commit()
        finally:
            s.close()
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_token(user_id: str, name: str) -> tuple[dict, str]:
    """Create a token. Returns (public_record, raw_token). Raw shown once."""
    raw = TOKEN_PREFIX + secrets.token_hex(16)  # 32 hex chars
    record = {
        "id": f"tok_{secrets.token_hex(8)}",
        "user_id": user_id,
        "name": (name or "").strip()[:60] or "Untitled token",
        "token_hash": _hash(raw),
        "created_at": int(time.time()),
        "last_used_at": None,
    }
    tokens = _load_tokens()
    tokens.append(record)
    _save_tokens(tokens)
    public = {k: v for k, v in record.items() if k != "token_hash"}
    return public, raw


def list_tokens(user_id: str) -> list[dict]:
    """Public token records for a user (no hashes). Newest first."""
    out = [
        {k: v for k, v in t.items() if k != "token_hash"}
        for t in _load_tokens()
        if t.get("user_id") == user_id
    ]
    out.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return out


def revoke_token(user_id: str, token_id: str) -> bool:
    """Revoke a token. Returns True if a token was removed."""
    tokens = _load_tokens()
    kept = [t for t in tokens
            if not (t.get("id") == token_id and t.get("user_id") == user_id)]
    if len(kept) == len(tokens):
        return False
    _save_tokens(kept)
    return True


def verify_token(raw: str) -> Optional[dict]:
    """Verify a raw bearer token. Returns the token record (with user_id)
    or None. Constant-time comparison on the hash."""
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw.startswith(TOKEN_PREFIX):
        return None
    candidate = _hash(raw)
    for t in _load_tokens():
        stored = t.get("token_hash", "")
        if hmac.compare_digest(stored, candidate):
            # Touch last_used_at (best-effort; never fails auth).
            try:
                tokens = _load_tokens()
                for tt in tokens:
                    if tt.get("id") == t.get("id"):
                        tt["last_used_at"] = int(time.time())
                        break
                _save_tokens(tokens)
            except Exception:
                pass
            return t
    return None
