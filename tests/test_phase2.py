"""Tests for Wave Cinema Phase 2: Studio, Tools, Wiki, Account routes."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import main as appmod


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


# ------------------------------------------------------------------ public routes

def test_tools_page_public(client):
    r = client.get("/tools")
    assert r.status_code == 200
    assert "Song Trimmer" in r.text
    assert "Video Clipper" in r.text
    assert "Aspect Cropper" in r.text


def test_wiki_page_public(client):
    r = client.get("/wiki")
    assert r.status_code == 200
    assert "Planning your music video" in r.text
    assert "Aspect ratios explained" in r.text
    assert "Credits and billing explained" in r.text


def test_nav_present_on_tools(client):
    r = client.get("/tools")
    assert r.status_code == 200
    for label in ("Create", "Studio", "Tools", "Wiki"):
        assert label in r.text


# ------------------------------------------------------------------ auth-gated routes

def test_studio_redirects_when_logged_out(client):
    r = client.get("/studio", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_studio_project_redirects_when_logged_out(client):
    r = client.get("/studio/wc_fake123", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_account_redirects_when_logged_out(client):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_studio_prompt_save_redirects_when_logged_out(client):
    r = client.post("/studio/wc_fake123/scene/s0/prompt",
                    data={"prompt": "test"}, follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


# ------------------------------------------------------------------ backend: update_scene_prompt

def test_update_scene_prompt(tmp_path, monkeypatch):
    """update_scene_prompt edits the stored plan via the workspace."""
    from wavecinema import api as api_mod
    from wavecinema import workspace as ws_mod

    # Point the workspace at a temp dir.
    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")

    pid = api_mod.create_project("Prompt Test")
    # Inject a plan directly into the workspace.
    ws = ws_mod.ProjectWorkspace.load(pid)
    ws.state["plan"] = {
        "schema_version": 1,
        "scenes": [
            {"scene_id": "s0", "start_ms": 0, "end_ms": 3000,
             "prompt": "original prompt"},
            {"scene_id": "s1", "start_ms": 3000, "end_ms": 6000,
             "prompt": "second prompt"},
        ],
    }
    ws.save(expected_revision=ws.revision)

    res = api_mod.update_scene_prompt(pid, "s0", "edited prompt here")
    assert res["scene_id"] == "s0"
    assert res["prompt"] == "edited prompt here"

    # Verify persistence.
    ws2 = ws_mod.ProjectWorkspace.load(pid)
    scenes = ws2.state["plan"]["scenes"]
    assert scenes[0]["prompt"] == "edited prompt here"
    assert scenes[0]["prompt_edited"] is True
    assert scenes[1]["prompt"] == "second prompt"  # untouched


def test_update_scene_prompt_rejects_empty(tmp_path, monkeypatch):
    from wavecinema import api as api_mod
    from wavecinema import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    pid = api_mod.create_project("Prompt Test 2")
    ws = ws_mod.ProjectWorkspace.load(pid)
    ws.state["plan"] = {"scenes": [{"scene_id": "s0", "prompt": "x"}]}
    ws.save(expected_revision=ws.revision)

    with pytest.raises(ValueError, match="empty"):
        api_mod.update_scene_prompt(pid, "s0", "   ")


def test_update_scene_prompt_missing_scene(tmp_path, monkeypatch):
    from wavecinema import api as api_mod
    from wavecinema import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    pid = api_mod.create_project("Prompt Test 3")
    ws = ws_mod.ProjectWorkspace.load(pid)
    ws.state["plan"] = {"scenes": [{"scene_id": "s0", "prompt": "x"}]}
    ws.save(expected_revision=ws.revision)

    with pytest.raises(ValueError, match="not found"):
        api_mod.update_scene_prompt(pid, "s99", "new prompt")


def test_update_scene_prompt_no_plan(tmp_path, monkeypatch):
    from wavecinema import api as api_mod
    from wavecinema import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    pid = api_mod.create_project("Prompt Test 4")
    # No plan injected — should raise.

    with pytest.raises(ValueError, match="no director plan"):
        api_mod.update_scene_prompt(pid, "s0", "new prompt")


# ------------------------------------------------------------------ backend: ledger_history

def test_ledger_history_wrapper():
    from wavecinema import api as api_mod
    # Should not raise; returns a list (possibly empty).
    hist = api_mod.ledger_history("user_nonexistent_test_acct", limit=5)
    assert isinstance(hist, list)
