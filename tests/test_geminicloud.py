"""Tests for the Wave Cinema Cloud image generation lane (Nano Banana)."""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wavecinema import api as api_mod
from wavecinema import workspace as ws_mod
from wavecinema import geminicloud as gc_mod
from wavecinema import geminikey as gk_mod
from wavecinema import nanobanana as nb_mod
from wavecinema import byok as byok_mod
from wavecinema import ledger as ledger_mod
from wavecinema import quote as quote_mod

# A minimal valid 1x1 PNG.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Isolate workspace + ledger to a temp dir."""
    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(gk_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(byok_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ledger_mod, "LEDGER_PATH", tmp_path / "ledger.db")
    return tmp_path


@pytest.fixture
def project(ws_env):
    return api_mod.create_project("Sheet Test")


def _mock_nanobanana(monkeypatch):
    """Stub generate_image to return a 1x1 PNG without hitting the API."""
    monkeypatch.setattr(
        nb_mod, "generate_image",
        lambda prompt, api_key, timeout=180: {
            "image_bytes": PNG_1X1, "mime_type": "image/png"})


def _enable_cloud(monkeypatch):
    monkeypatch.setenv("WAVECINEMA_GEMINI_CLOUD_ENABLED", "1")
    monkeypatch.setenv("WAVECINEMA_GEMINI_API_KEY", "server-cloud-key-12345")


# ------------------------------------------------- pricing constant

def test_image_gen_credits_is_10():
    assert quote_mod.IMAGE_GEN_CREDITS == 10
    assert api_mod.image_gen_credits() == 10


# ------------------------------------------------- cloud availability

def test_cloud_disabled_by_default(ws_env, monkeypatch):
    monkeypatch.delenv("WAVECINEMA_GEMINI_CLOUD_ENABLED", raising=False)
    monkeypatch.delenv("WAVECINEMA_GEMINI_API_KEY", raising=False)
    assert gc_mod.cloud_available() is False
    assert api_mod.gemini_cloud_available() is False
    assert gc_mod.get_cloud_key() is None


def test_cloud_needs_both_flag_and_key(ws_env, monkeypatch):
    monkeypatch.setenv("WAVECINEMA_GEMINI_CLOUD_ENABLED", "1")
    monkeypatch.delenv("WAVECINEMA_GEMINI_API_KEY", raising=False)
    assert gc_mod.cloud_available() is False


def test_cloud_enabled(ws_env, monkeypatch):
    _enable_cloud(monkeypatch)
    assert gc_mod.cloud_available() is True
    assert gc_mod.get_cloud_key() == "server-cloud-key-12345"
    assert api_mod.gemini_cloud_available() is True


# ------------------------------------------------- BYOK lane unchanged

def test_byok_lane_zero_credits(ws_env, project, monkeypatch):
    """BYOK: user's own key, no ledger movement."""
    _mock_nanobanana(monkeypatch)
    gk_mod.set_key("AIzaFakeKeyForUnitTests0123456789", "u1")
    acct = "user_u1"
    before = api_mod.credit_balance(acct)
    rec = api_mod.generate_character_sheet(
        project, "Zed", "tall robot", "anime",
        user_id="u1", lane="byok", account_id=acct)
    assert rec["name"] == "Zed"
    assert api_mod.credit_balance(acct) == before


def test_byok_lane_no_key_raises(ws_env, project):
    with pytest.raises(gk_mod.GeminiKeyError):
        api_mod.generate_character_sheet(
            project, "Zed", "tall robot", user_id="nobody", lane="byok")


# ------------------------------------------------- Cloud lane credits

def test_cloud_lane_deducts_credits(ws_env, project, monkeypatch):
    _enable_cloud(monkeypatch)
    _mock_nanobanana(monkeypatch)
    acct = "user_cloud1"
    ledger_mod.Ledger().grant(acct, 100, memo="test grant")
    assert api_mod.credit_balance(acct) == 100
    rec = api_mod.generate_character_sheet(
        project, "Nova", "space pilot", "anime",
        user_id="u1", lane="cloud", account_id=acct)
    assert rec["name"] == "Nova"
    assert api_mod.credit_balance(acct) == 90  # 100 - 10


def test_cloud_lane_background_deducts(ws_env, project, monkeypatch):
    _enable_cloud(monkeypatch)
    _mock_nanobanana(monkeypatch)
    acct = "user_cloud2"
    ledger_mod.Ledger().grant(acct, 50, memo="test grant")
    rec = api_mod.generate_background_image(
        project, "Rooftop", "neon rooftop",
        user_id="u1", lane="cloud", account_id=acct)
    assert rec["name"] == "Rooftop"
    assert api_mod.credit_balance(acct) == 40


def test_cloud_lane_insufficient_balance(ws_env, project, monkeypatch):
    """Insufficient credits -> InsufficientFunds, no generation attempted."""
    _enable_cloud(monkeypatch)
    called = []
    monkeypatch.setattr(
        nb_mod, "generate_image",
        lambda *a, **k: called.append(True) or {"image_bytes": PNG_1X1,
                                                "mime_type": "image/png"})
    acct = "user_broke"
    ledger_mod.Ledger().grant(acct, 5, memo="test grant")  # less than 10
    with pytest.raises(ledger_mod.InsufficientFunds):
        api_mod.generate_character_sheet(
            project, "Zed", "tall robot", user_id="u1",
            lane="cloud", account_id=acct)
    assert called == []  # generation never ran
    assert api_mod.credit_balance(acct) == 5  # nothing moved


def test_cloud_lane_failure_releases_hold(ws_env, project, monkeypatch):
    """Generation failure -> hold released, balance restored."""
    _enable_cloud(monkeypatch)

    def _boom(prompt, api_key, timeout=180):
        raise nb_mod.NanoBananaError("simulated API failure")
    monkeypatch.setattr(nb_mod, "generate_image", _boom)
    acct = "user_fail1"
    ledger_mod.Ledger().grant(acct, 100, memo="test grant")
    with pytest.raises(nb_mod.NanoBananaError):
        api_mod.generate_character_sheet(
            project, "Zed", "tall robot", user_id="u1",
            lane="cloud", account_id=acct)
    assert api_mod.credit_balance(acct) == 100  # hold released


def test_cloud_lane_not_configured_raises(ws_env, project, monkeypatch):
    monkeypatch.delenv("WAVECINEMA_GEMINI_CLOUD_ENABLED", raising=False)
    monkeypatch.delenv("WAVECINEMA_GEMINI_API_KEY", raising=False)
    with pytest.raises(gk_mod.GeminiKeyError, match="not configured"):
        api_mod.generate_character_sheet(
            project, "Zed", "tall robot", user_id="u1",
            lane="cloud", account_id="user_x")


def test_cloud_lane_needs_account_id(ws_env, project, monkeypatch):
    _enable_cloud(monkeypatch)
    _mock_nanobanana(monkeypatch)
    with pytest.raises(ValueError, match="account_id"):
        api_mod.generate_character_sheet(
            project, "Zed", "tall robot", user_id="u1", lane="cloud")


def test_invalid_lane_falls_back_to_byok(ws_env, project, monkeypatch):
    """Unknown lane string is treated as BYOK (no credits moved)."""
    _mock_nanobanana(monkeypatch)
    gk_mod.set_key("AIzaFakeKeyForUnitTests0123456789", "u1")
    acct = "user_u9"
    rec = api_mod.generate_character_sheet(
        project, "Zed", "tall robot", user_id="u1",
        lane="weird", account_id=acct)
    assert rec["name"] == "Zed"
    assert api_mod.credit_balance(acct) == 0
