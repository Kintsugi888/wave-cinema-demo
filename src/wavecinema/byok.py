"""Bring Your Own Key: the user's own Higgsfield API key for generation.

No user accounts exist yet, so this is a single server-side setting: one
stored key for the whole installation. The key is encrypted at rest with
Fernet; the encryption secret comes from the WAVECINEMA_BYOK_SECRET env
var, or is generated once and stored (0600) under the data dir.

When a BYOK key is set, automatic generation uses the user's key and
their Higgsfield account is billed directly — Wave Cinema moves zero
credits (quotes are 0, no ledger holds). When no BYOK key is set, the
Cloud lane falls back to our HIGGSFIELD_API_KEY with credit billing.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .errors import WaveCinemaError
from .workspace import DATA_DIR

KEY_FILE = DATA_DIR / "byok.json"
SECRET_FILE = DATA_DIR / ".byok_secret"


class BYOKError(WaveCinemaError):
    """BYOK key storage / validation failure."""


def _fernet() -> Fernet:
    secret = (os.environ.get("WAVECINEMA_BYOK_SECRET") or "").strip()
    if secret:
        try:
            # Accept a raw Fernet key, or derive one from any passphrase.
            key = secret.encode()
            Fernet(key)  # validates
        except Exception:
            digest = __import__("hashlib").sha256(secret.encode()).digest()
            key = base64.urlsafe_b64encode(digest)
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if SECRET_FILE.exists():
            key = SECRET_FILE.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            SECRET_FILE.write_bytes(key)
            try:
                os.chmod(SECRET_FILE, 0o600)
            except OSError:
                pass
    return Fernet(key)


def _validate_shape(api_key: str) -> str:
    key = (api_key or "").strip()
    if not key:
        raise BYOKError("API key is empty")
    if len(key) < 16:
        raise BYOKError("API key looks too short to be real")
    if ":" not in key:
        # Higgsfield keys are "<key_id>:<key_secret>"; warn-shaped but
        # accept anything plausible so other providers work too.
        pass
    return key


def set_key(api_key: str) -> dict:
    """Encrypt and store the user's API key. Returns a masked status."""
    key = _validate_shape(api_key)
    token = _fernet().encrypt(key.encode()).decode()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps({"token": token}), encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return status()


def get_key() -> str | None:
    """Decrypt and return the stored key, or None if unset/unreadable."""
    if not KEY_FILE.exists():
        return None
    try:
        token = json.loads(KEY_FILE.read_text(encoding="utf-8"))["token"]
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, KeyError, ValueError, OSError):
        return None


def clear_key() -> dict:
    """Remove the stored key."""
    KEY_FILE.unlink(missing_ok=True)
    return status()


def _masked(key: str) -> str:
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def status() -> dict:
    """{"set": bool, "masked": str|None} — never exposes the key."""
    key = get_key()
    return {"set": key is not None,
            "masked": _masked(key) if key else None}
