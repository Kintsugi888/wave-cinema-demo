"""Wave Cinema authentication: users, sessions, OAuth, magic links.

User accounts are stored in app/data/users.json (JSON file store, matching
the existing projects.json / quotes.json pattern). Sessions are signed
cookies via itsdangerous (30-day expiry, httponly).

Supported providers:
- Google OAuth2 (env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
- X OAuth 2.0 with PKCE (env: X_CLIENT_ID, X_CLIENT_SECRET)
- Passwordless email magic link (env: RESEND_API_KEY optional; logs link in dev)
"""

from __future__ import annotations

import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"

SESSION_COOKIE = "wc_session"
SESSION_MAX_AGE = 30 * 24 * 3600  # 30 days
MAGIC_LINK_MAX_AGE = 10 * 60  # 10 minutes

BASE_URL = os.environ.get("WAVE_CINEMA_BASE_URL", "http://localhost:8000").rstrip("/")


def _secret() -> str:
    s = os.environ.get("WAVECINEMA_SESSION_SECRET")
    if s:
        return s
    # Dev fallback: stable per-process secret (sessions won't survive restarts).
    # Production MUST set WAVECINEMA_SESSION_SECRET.
    return "dev-only-session-secret-change-me"


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret(), salt=salt)


# ------------------------------------------------------------------ users

def _load_users() -> list:
    try:
        from store import USE_DB
    except ImportError:
        from .store import USE_DB
    if USE_DB:
        # DB-backed: return all users as dicts
        try:
            from store import get_session
            from db import User as DBUser
        except ImportError:
            from .store import get_session
            from .db import User as DBUser
        s = get_session()
        try:
            return [{"id": u.id, "email": u.email, "name": u.name,
                     "credits": u.credits, "byok_keys": u.byok_keys}
                    for u in s.query(DBUser).all()]
        finally:
            s.close()
    try:
        data = json.loads(USERS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_users(users: list) -> None:
    try:
        from store import USE_DB
    except ImportError:
        from .store import USE_DB
    if USE_DB:
        try:
            from store import save_user
        except ImportError:
            from .store import save_user
        for u in users:
            if isinstance(u, dict) and u.get("id"):
                save_user(u)
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))


def get_user(user_id: str) -> Optional[dict]:
    for u in _load_users():
        if u.get("id") == user_id:
            return u
    return None


def get_user_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    for u in _load_users():
        if (u.get("email") or "").lower() == email:
            return u
    return None


def create_user(email: str, name: str = "", avatar_url: str = "",
                provider: str = "") -> dict:
    users = _load_users()
    existing = get_user_by_email(email)
    if existing:
        # Update profile on re-login; keep the original id.
        existing["name"] = name or existing.get("name", "")
        existing["avatar_url"] = avatar_url or existing.get("avatar_url", "")
        _save_users(users)
        return existing
    user = {
        "id": f"u_{secrets.token_hex(8)}",
        "email": email.strip().lower(),
        "name": name or email.split("@")[0],
        "avatar_url": avatar_url,
        "provider": provider,
        "created_at": int(time.time()),
    }
    users.append(user)
    _save_users(users)
    return user


def ledger_account_id(user: dict) -> str:
    """Per-user ledger account id. Keeps the legacy 'owner' account untouched."""
    return f"user_{user['id']}"


# ---------------------------------------------------------------- sessions

def create_session(user_id: str) -> str:
    return _serializer("wc-session").dumps({"uid": user_id})


def verify_session(token: str) -> Optional[dict]:
    try:
        data = _serializer("wc-session").loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    if not uid:
        return None
    return get_user(uid)


# ------------------------------------------------------------ magic links

def create_magic_token(email: str) -> str:
    return _serializer("wc-magic").dumps({"email": email.strip().lower()})


def verify_magic_token(token: str) -> Optional[str]:
    try:
        data = _serializer("wc-magic").loads(token, max_age=MAGIC_LINK_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email or None


def send_magic_link(email: str, token: str) -> bool:
    """Send the magic link. Returns True if sent (or logged in dev mode)."""
    link = f"{BASE_URL}/auth/email/verify?token={urllib.parse.quote(token)}"
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        # Dev mode: log the link so it can be clicked from server logs.
        print(f"[auth] MAGIC LINK for {email}: {link}", flush=True)
        return True
    try:
        body = json.dumps({
            "from": os.environ.get("RESEND_FROM", "Wave Cinema <noreply@wavecinema.ai>"),
            "to": [email],
            "subject": "Sign in to Wave Cinema",
            "html": (
                "<p>Click the link below to sign in to Wave Cinema. "
                "It expires in 10 minutes.</p>"
                f'<p><a href="{link}">Sign in to Wave Cinema</a></p>'
            ),
        }).encode()
        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=body,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 201, 202)
    except Exception as e:
        print(f"[auth] Resend send failed: {e}", flush=True)
        return False


# ------------------------------------------------------------------ OAuth

def google_configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))


def x_configured() -> bool:
    return bool(os.environ.get("X_CLIENT_ID") and os.environ.get("X_CLIENT_SECRET"))


def google_auth_url(state: str) -> str:
    cid = os.environ["GOOGLE_CLIENT_ID"]
    redirect = f"{BASE_URL}/auth/google/callback"
    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{params}"


def google_exchange(code: str) -> dict:
    """Exchange code for tokens, return userinfo dict."""
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": f"{BASE_URL}/auth/google/callback",
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read())
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {tok['access_token']}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        info = json.loads(r.read())
    return {
        "email": info.get("email", ""),
        "name": info.get("name", ""),
        "avatar_url": info.get("picture", ""),
    }


def _pkce_pair() -> tuple[str, str]:
    import hashlib, base64
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def x_auth_url(state: str) -> tuple[str, str]:
    """Return (url, code_verifier). Verifier must be kept server-side for callback."""
    cid = os.environ["X_CLIENT_ID"]
    redirect = f"{BASE_URL}/auth/x/callback"
    verifier, challenge = _pkce_pair()
    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": "tweet.read users.read offline.access",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"https://twitter.com/i/oauth2/authorize?{params}", verifier


def x_exchange(code: str, verifier: str) -> dict:
    import base64
    cid = os.environ["X_CLIENT_ID"]
    csecret = os.environ["X_CLIENT_SECRET"]
    basic = base64.b64encode(f"{cid}:{csecret}".encode()).decode()
    body = urllib.parse.urlencode({
        "code": code,
        "grant_type": "authorization_code",
        "client_id": cid,
        "redirect_uri": f"{BASE_URL}/auth/x/callback",
        "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(
        "https://api.twitter.com/2/oauth2/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Basic {basic}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read())
    req = urllib.request.Request(
        "https://api.twitter.com/2/users/me?user.fields=profile_image_url",
        headers={"Authorization": f"Bearer {tok['access_token']}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        me = json.loads(r.read()).get("data", {})
    return {
        # X API v2 user lookup has no email without extra scope/approval.
        "email": "",
        "name": me.get("name", me.get("username", "")),
        "avatar_url": (me.get("profile_image_url", "") or "").replace("_normal", ""),
        "x_username": me.get("username", ""),
    }


def oauth_state_token(payload: dict) -> str:
    return _serializer("wc-oauth-state").dumps(payload)


def oauth_state_verify(token: str, max_age: int = 600) -> Optional[dict]:
    try:
        data = _serializer("wc-oauth-state").loads(token, max_age=max_age)
        return data if isinstance(data, dict) else None
    except (BadSignature, SignatureExpired):
        return None
