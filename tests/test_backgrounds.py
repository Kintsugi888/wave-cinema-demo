"""Tests for Wave Cinema backgrounds, Gemini key storage, and Nano Banana prompts."""

import base64
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import main as appmod
from wavecinema import workspace as ws_mod
from wavecinema import backgrounds as bg_mod
from wavecinema import geminikey as gk_mod
from wavecinema import nanobanana as nb_mod
from wavecinema import byok as byok_mod
from wavecinema import direct as direct_mod


# A minimal valid 1x1 PNG.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Isolate the workspace to a temp dir."""
    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(bg_mod, "project_dir", ws_mod.project_dir)
    monkeypatch.setattr(gk_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(byok_mod, "DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def project(ws_env):
    return ws_mod.create_project("Backgrounds Test")


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


# ------------------------------------------------- backgrounds module

def test_add_background_ok(project):
    rec = bg_mod.add_background(project, "Neon Rooftop", PNG_1X1, "roof.png")
    assert rec["name"] == "Neon Rooftop"
    assert rec["background_id"].startswith("bg_")
    assert bg_mod.background_image_path(project, rec["background_id"]).is_file()


def test_add_background_rejects_bad_ext(project):
    with pytest.raises(bg_mod.BackgroundRejected):
        bg_mod.add_background(project, "X", b"data", "notes.txt")


def test_add_background_rejects_non_image(project):
    with pytest.raises(bg_mod.BackgroundRejected):
        bg_mod.add_background(project, "X", b"not an image at all", "x.png")


def test_add_background_rejects_oversize(project):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
    with pytest.raises(bg_mod.BackgroundRejected):
        bg_mod.add_background(project, "Big", big, "big.png")


def test_add_background_rejects_duplicate_name(project):
    bg_mod.add_background(project, "Rooftop", PNG_1X1, "a.png")
    with pytest.raises(bg_mod.BackgroundRejected):
        bg_mod.add_background(project, "rooftop", PNG_1X1, "b.png")


def test_background_cap(project):
    # shrink the cap via monkeypatch-free approach: fill to MAX
    for i in range(bg_mod.MAX_BACKGROUNDS):
        bg_mod.add_background(project, f"BG{i}", PNG_1X1, f"{i}.png")
    with pytest.raises(bg_mod.BackgroundRejected):
        bg_mod.add_background(project, "OneMore", PNG_1X1, "x.png")


def test_remove_background(project):
    rec = bg_mod.add_background(project, "Gone", PNG_1X1, "g.png")
    path = bg_mod.background_image_path(project, rec["background_id"])
    assert path.is_file()
    out = bg_mod.remove_background(project, rec["background_id"])
    assert out["deleted"] is True
    assert not path.exists()
    assert bg_mod.list_backgrounds(project) == []


def test_background_reference_paths_capped(project):
    for i in range(3):
        bg_mod.add_background(project, f"BG{i}", PNG_1X1, f"{i}.png")
    paths = bg_mod.background_reference_paths(project)
    assert len(paths) == 3
    assert all(Path(p).is_file() for p in paths)


# ------------------------------------------------- director LOCATION REFERENCES

def test_director_location_block(project):
    bg_mod.add_background(project, "Neon Rooftop", PNG_1X1, "r.png")
    ws = ws_mod.ProjectWorkspace.load(project)
    ws.state["cue_sheet"] = {"words": []}
    ws.state["scenes"] = [{"scene_id": "s0", "start_ms": 0, "end_ms": 3000,
                           "duration_ms": 3000}]
    ws.save(expected_revision=ws.revision)
    backgrounds = [b for b in bg_mod.list_backgrounds(project)]
    d = direct_mod.DeterministicDirector("test", {"words": []},
                                         ws.state["scenes"], backgrounds=backgrounds)
    line = d._location_reference_line()
    assert "LOCATION REFERENCES" in line
    assert "Neon Rooftop" in line


def test_director_no_backgrounds_no_block():
    d = direct_mod.DeterministicDirector("test", {"words": []}, [])
    assert d._location_reference_line() == ""


# ------------------------------------------------- gemini key storage

def test_gemini_key_roundtrip(ws_env):
    assert gk_mod.get_key("u1") is None
    st = gk_mod.set_key("AIzaFakeKey1234567890abcdef", "u1")
    assert st["set"] is True
    assert st["masked"].startswith("AIza")
    assert "AIzaFakeKey1234567890abcdef" not in st["masked"]
    assert gk_mod.get_key("u1") == "AIzaFakeKey1234567890abcdef"
    gk_mod.clear_key("u1")
    assert gk_mod.get_key("u1") is None


def test_gemini_key_rejects_short(ws_env):
    with pytest.raises(gk_mod.GeminiKeyError):
        gk_mod.set_key("short", "u1")


def test_gemini_env_fallback(ws_env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaEnvKey1234567890")
    assert gk_mod.get_key("nou") == "AIzaEnvKey1234567890"
    assert gk_mod.status("nou")["source"] == "env"


def test_gemini_status_unset(ws_env):
    st = gk_mod.status("nobody")
    assert st == {"set": False, "masked": None, "source": None}


# ------------------------------------------------- nanobanana prompts

def test_character_sheet_prompt():
    p = nb_mod.character_sheet_prompt("Shamz", "golden braids, red eyes", "anime")
    assert "Shamz" in p
    assert "FRONT VIEW" in p and "SIDE VIEW" in p and "BACK VIEW" in p
    assert "anime" in p


def test_background_prompt():
    p = nb_mod.background_prompt("neon rooftop at night")
    assert "neon rooftop at night" in p
    assert "no characters" in p


def test_generate_image_no_key():
    with pytest.raises(nb_mod.NanoBananaError):
        nb_mod.generate_image("a cat", "")


def test_generate_image_parses_inline_data(monkeypatch):
    fake_png_b64 = base64.b64encode(PNG_1X1).decode()
    payload = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/png", "data": fake_png_b64}}]}}]}

    class FakeResp:
        def read(self):
            return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(nb_mod.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp())
    out = nb_mod.generate_image("a cat", "fake-key")
    assert out["image_bytes"] == PNG_1X1
    assert out["mime_type"] == "image/png"


def test_generate_image_no_image_raises(monkeypatch):
    payload = {"candidates": [{"content": {"parts": [{"text": "sorry"}]}}]}

    class FakeResp:
        def read(self):
            return json.dumps(payload).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(nb_mod.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp())
    with pytest.raises(nb_mod.NanoBananaError):
        nb_mod.generate_image("a cat", "fake-key")


# ------------------------------------------------- web routes (auth)

def _login(client):
    # Use the test helper if present, else craft a session directly.
    import main as m
    if hasattr(m, "_test_login"):
        return m._test_login(client)
    return None


def test_background_routes_require_login(client):
    r = client.post("/project/nope/backgrounds",
                    files={"image": ("x.png", PNG_1X1, "image/png")},
                    data={"name": "X"}, follow_redirects=False)
    assert r.status_code in (303, 307, 401, 403)


def test_character_sheet_requires_login(client):
    r = client.get("/project/nope/character-sheet", follow_redirects=False)
    assert r.status_code in (303, 307, 401, 403)


def test_settings_page_renders(client):
    # public check: settings requires login
    r = client.get("/settings", follow_redirects=False)
    assert r.status_code in (200, 303, 307, 401, 403)
