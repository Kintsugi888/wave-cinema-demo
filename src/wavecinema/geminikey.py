"""Gemini API key storage for Nano Banana image generation.

Mirrors byok.py's encrypted-at-rest pattern: per-user Fernet-encrypted
key files under the data dir (gemini_<user_id>.json), falling back to the
legacy global gemini.json. The encryption secret is shared with byok.py
(WAVECINEMA_BYOK_SECRET env var or the generated .byok_secret file).

Resolution order for an API key:
  1. per-user stored key (Settings page),
  2. legacy global stored key,
  3. GEMINI_API_KEY / GOOGLE_AI_API_KEY env var.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import byok as _byok
from .errors import WaveCinemaError
from .workspace import DATA_DIR


def _key_file(user_id: Optional[str] = None) -> Path:
    if user_id:
        safe = "".join(c for c in user_id if c.isalnum() or c in "_-")[:32]
        return DATA_DIR / f"gemini_{safe}.json"
    return DATA_DIR / "gemini.json"


class GeminiKeyError(WaveCinemaError):
    """Gemini key storage / validation failure."""


def _validate_shape(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        raise GeminiKeyError("API key is empty")
    if len(key) < 16:
        raise GeminiKeyError("API key looks too short to be real")
    return key


def set_key(api_key: str, user_id: Optional[str] = None) -> dict:
    """Encrypt and store the user's Gemini API key. Returns masked status."""
    key = _validate_shape(api_key)
    token = _byok._fernet().encrypt(key.encode()).decode()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    kf = _key_file(user_id)
    kf.write_text(json.dumps({"token": token}), encoding="utf-8")
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass
    return status(user_id)


def get_key(user_id: Optional[str] = None) -> str | None:
    """Decrypt and return the stored key, or None if unset/unreadable."""
    kf = _key_file(user_id)
    # Fall back to the legacy global key file for backwards compat.
    if not kf.exists() and user_id:
        kf = DATA_DIR / "gemini.json"
    if kf.exists():
        try:
            token = json.loads(kf.read_text(encoding="utf-8"))["token"]
            return _byok._fernet().decrypt(token.encode()).decode()
        except Exception:
            pass
    # Env var fallback (server-wide key).
    for var in ("GEMINI_API_KEY", "GOOGLE_AI_API_KEY"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return None


def clear_key(user_id: Optional[str] = None) -> dict:
    """Remove the stored key."""
    _key_file(user_id).unlink(missing_ok=True)
    return status(user_id)


def _masked(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def status(user_id: Optional[str] = None) -> dict:
    """{"set": bool, "masked": str|None, "source": ...} — never exposes the key."""
    key = get_key(user_id)
    if key is None:
        return {"set": False, "masked": None, "source": None}
    # Figure out where it came from (for display only).
    kf = _key_file(user_id)
    if kf.exists():
        source = "settings"
    elif user_id and (DATA_DIR / "gemini.json").exists():
        source = "settings"
    else:
        source = "env"
    return {"set": True, "masked": _masked(key), "source": source}
