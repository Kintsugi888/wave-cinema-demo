"""Tests for Wave Cinema authentication: users, sessions, magic links."""

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import auth as authmod


@pytest.fixture
def tmp_users(tmp_path, monkeypatch):
    """Isolate the user store to a temp file."""
    users_file = tmp_path / "users.json"
    monkeypatch.setattr(authmod, "USERS_FILE", users_file)
    monkeypatch.setattr(authmod, "DATA_DIR", tmp_path)
    return users_file


def test_create_and_get_user(tmp_users):
    u = authmod.create_user("test@example.com", name="Test", provider="google")
    assert u["id"].startswith("u_")
    assert u["email"] == "test@example.com"
    assert authmod.get_user(u["id"])["email"] == "test@example.com"
    assert authmod.get_user_by_email("TEST@example.com")["id"] == u["id"]


def test_create_user_idempotent(tmp_users):
    u1 = authmod.create_user("a@b.com", provider="email")
    u2 = authmod.create_user("a@b.com", name="New Name", provider="email")
    assert u1["id"] == u2["id"]
    assert u2["name"] == "New Name"  # profile updated on re-login


def test_ledger_account_id(tmp_users):
    u = authmod.create_user("x@y.com")
    acct = authmod.ledger_account_id(u)
    assert acct == f"user_{u['id']}"
    assert acct != "owner"  # legacy account untouched


def test_session_roundtrip(tmp_users):
    u = authmod.create_user("s@t.com")
    token = authmod.create_session(u["id"])
    assert isinstance(token, str) and len(token) > 20
    verified = authmod.verify_session(token)
    assert verified["id"] == u["id"]


def test_session_rejects_bad_token(tmp_users):
    assert authmod.verify_session("garbage") is None
    assert authmod.verify_session("") is None


def test_session_rejects_deleted_user(tmp_users):
    u = authmod.create_user("gone@x.com")
    token = authmod.create_session(u["id"])
    # Delete the user file content
    authmod._save_users([])
    assert authmod.verify_session(token) is None


def test_magic_link_roundtrip(tmp_users):
    token = authmod.create_magic_token("m@x.com")
    assert authmod.verify_magic_token(token) == "m@x.com"


def test_magic_link_rejects_bad(tmp_users):
    assert authmod.verify_magic_token("nope") is None


def test_magic_link_dev_mode_logs(tmp_users, capsys):
    # No RESEND_API_KEY -> logs link, returns True
    import os
    os.environ.pop("RESEND_API_KEY", None)
    assert authmod.send_magic_link("dev@x.com", "tok123") is True
    out = capsys.readouterr().out
    assert "MAGIC LINK" in out
    assert "tok123" in out


def test_oauth_state_roundtrip(tmp_users):
    tok = authmod.oauth_state_token({"provider": "google"})
    assert authmod.oauth_state_verify(tok) == {"provider": "google"}
    assert authmod.oauth_state_verify("bad") is None


def test_google_auth_url_shape(tmp_users, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid123")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csec")
    monkeypatch.setenv("WAVE_CINEMA_BASE_URL", "https://example.com")
    # Re-read BASE_URL
    import importlib
    url = authmod.google_auth_url("state123")
    assert "accounts.google.com" in url
    assert "cid123" in url
    assert "state123" in url


def test_x_auth_url_pkce(tmp_users, monkeypatch):
    monkeypatch.setenv("X_CLIENT_ID", "xcid")
    monkeypatch.setenv("X_CLIENT_SECRET", "xsec")
    url, verifier = authmod.x_auth_url("st")
    assert "twitter.com/i/oauth2/authorize" in url
    assert "code_challenge" in url
    assert "S256" in url
    assert len(verifier) > 20
