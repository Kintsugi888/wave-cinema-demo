"""Tests for Wave Cinema Phase 3: Producer, captions, remix, aspect ratio, 2K."""

import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import main as appmod
from wavecinema import workspace as ws_mod
from wavecinema import producer as producer_mod
from wavecinema import captions as captions_mod
from wavecinema import remix as remix_mod
from wavecinema import render_opts as ro_mod
from wavecinema import quote as quote_mod


@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Isolate the workspace to a temp dir."""
    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    return tmp_path


@pytest.fixture
def project(ws_env):
    pid = ws_mod.create_project("Phase3 Test")
    return pid


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


# ------------------------------------------------------------- producer

def test_producer_chat_greeting(project):
    res = producer_mod.chat(project, "hello")
    assert res["reply"]
    assert "Producer" in res["reply"] or "film" in res["reply"].lower()
    assert res["source"] == "producer"  # no LLM keys in test env


def test_producer_chat_empty_rejected(project):
    with pytest.raises(ValueError):
        producer_mod.chat(project, "   ")


def test_producer_chat_too_long_rejected(project):
    with pytest.raises(ValueError):
        producer_mod.chat(project, "x" * 2001)


def test_producer_history_stored(project):
    pid = ws_mod.create_project("Hist Test")
    producer_mod.chat(pid, "what next?")
    producer_mod.chat(pid, "tell me about pacing")
    hist = producer_mod.get_history(pid)
    assert len(hist) == 4  # user+assistant x2
    assert hist[0]["role"] == "user"
    assert hist[1]["role"] == "assistant"


def test_producer_what_next_no_song(project):
    res = producer_mod.chat(project, "what should I do next?")
    assert "upload" in res["reply"].lower() or "ingest" in res["reply"].lower()


def test_producer_scene_question(project):
    pid = project
    # Seed a plan with one scene.
    ws = ws_mod.ProjectWorkspace.load(pid)
    rev = ws.revision
    ws.state["plan"] = {"scenes": [
        {"scene_id": "s00", "start_ms": 0, "end_ms": 9000,
         "cast": ["Nova"], "prompt": "Nova walks through neon rain."}]}
    ws.save(expected_revision=rev)
    res = producer_mod.chat(pid, "what about scene 0?")
    assert "9.0s" in res["reply"] or "Scene 0" in res["reply"]
    assert "Nova" in res["reply"]


def test_producer_unknown_scene(project):
    res = producer_mod.chat(project, "what about scene 5?")
    assert "only have 0" in res["reply"]


# ------------------------------------------------------------- captions

def _seed_cue_sheet(pid, words):
    ws = ws_mod.ProjectWorkspace.load(pid)
    rev = ws.revision
    ws.state["cue_sheet"] = {
        "schema_version": "wavecinema.cuesheet/1",
        "generated_by": "whisper",
        "duration_ms": 10000,
        "words": words,
        "phrases": [],
    }
    ws.save(expected_revision=rev)


def test_captions_from_words(project):
    words = [
        {"word": "hello", "start_ms": 0, "end_ms": 500},
        {"word": "world", "start_ms": 600, "end_ms": 1100},
    ]
    _seed_cue_sheet(project, words)
    caps = captions_mod.build_captions(project)
    assert caps["word_count"] == 2
    assert caps["cue_count"] == 1
    assert not caps["estimated"]
    assert "hello world" in caps["srt"]
    assert "00:00:00,000 --> 00:00:01,100" in caps["srt"]


def test_captions_groups_max_words(project):
    words = [{"word": f"w{i}", "start_ms": i * 100, "end_ms": i * 100 + 50}
             for i in range(20)]
    _seed_cue_sheet(project, words)
    caps = captions_mod.build_captions(project)
    assert caps["cue_count"] == 3  # 8 + 8 + 4


def test_captions_heuristic_estimated(project):
    ws = ws_mod.ProjectWorkspace.load(project)
    rev = ws.revision
    ws.state["cue_sheet"] = {
        "schema_version": "wavecinema.cuesheet/1",
        "generated_by": "heuristic",
        "duration_ms": 8000,
        "words": [],
        "phrases": [
            {"phrase_id": "ph00", "start_ms": 0, "end_ms": 4000,
             "owner": "INSTRUMENTAL", "words": []},
            {"phrase_id": "ph01", "start_ms": 4000, "end_ms": 8000,
             "owner": "INSTRUMENTAL", "words": []},
        ],
    }
    ws.save(expected_revision=rev)
    caps = captions_mod.build_captions(project)
    assert caps["estimated"]
    assert caps["cue_count"] == 2
    assert "estimated" in caps["srt"].lower()


def test_captions_no_cue_sheet(project):
    with pytest.raises(ValueError, match="no cue sheet"):
        captions_mod.build_captions(project)


# ------------------------------------------------------------- remix

def _mp4_bytes():
    # Minimal ftyp box so _looks_like_video passes.
    return b"\x00\x00\x00\x18ftypisom" + b"\x00" * 2048


def test_remix_submit_ok(project):
    job = remix_mod.submit_remix(project, _mp4_bytes(), "clip.mp4",
                                 "make it night, neon city")
    assert job["remix_id"].startswith("rx_")
    assert job["status"] == "queued"
    assert "neon city" in job["style_prompt"]


def test_remix_submit_bad_ext(project):
    with pytest.raises(ValueError, match="MP4 or MOV"):
        remix_mod.submit_remix(project, b"x" * 2048, "clip.avi", "style it")


def test_remix_submit_empty_prompt(project):
    with pytest.raises(ValueError, match="describe"):
        remix_mod.submit_remix(project, _mp4_bytes(), "clip.mp4", "  ")


def test_remix_submit_too_big(project):
    with pytest.raises(ValueError, match="100 MB"):
        remix_mod.submit_remix(project, b"\x00" * (101 * 1024 * 1024),
                               "clip.mp4", "style")


def test_remix_submit_not_video(project):
    with pytest.raises(ValueError, match="not a valid"):
        remix_mod.submit_remix(project, b"\x00" * 2048, "clip.mp4", "style")


def test_remix_list(project):
    pid = ws_mod.create_project("Remix List")
    remix_mod.submit_remix(pid, _mp4_bytes(), "a.mp4", "night")
    remix_mod.submit_remix(pid, _mp4_bytes(), "b.mp4", "day")
    jobs = remix_mod.list_remixes(pid)
    assert len(jobs) == 2
    assert jobs[0]["style_prompt"] == "day"  # newest first


# ------------------------------------------------------------- render options

def test_aspect_ratio_default(project):
    assert ro_mod.get_aspect_ratio(project) == "16:9"


def test_aspect_ratio_set(project):
    ro_mod.set_aspect_ratio(project, "9:16")
    assert ro_mod.get_aspect_ratio(project) == "9:16"


def test_aspect_ratio_invalid(project):
    with pytest.raises(ValueError, match="unknown aspect"):
        ro_mod.set_aspect_ratio(project, "5:7")


def test_resolution_default(project):
    assert ro_mod.get_resolution(project) == "1080p"


def test_resolution_set_2k(project):
    ro_mod.set_resolution(project, "2K")
    assert ro_mod.get_resolution(project) == "2K"


def test_resolution_invalid(project):
    with pytest.raises(ValueError, match="unknown resolution"):
        ro_mod.set_resolution(project, "8K")


def test_render_opts(project):
    ro_mod.set_aspect_ratio(project, "1:1")
    ro_mod.set_resolution(project, "2K")
    opts = ro_mod.get_render_opts(project)
    assert opts["aspect_ratio"] == "1:1"
    assert opts["resolution"] == "2K"
    assert opts["dimensions"] == (1080, 1080)
    assert opts["credit_multiplier"] == 1.5


# ------------------------------------------------------------- quote 2K pricing

def test_quote_1080p_base():
    secs, credits = quote_mod.price_for_duration_ms(7200, resolution="1080p")
    assert secs == 8
    assert credits == 64  # 8s x 8


def test_quote_2k_multiplier():
    secs, credits = quote_mod.price_for_duration_ms(7200, resolution="2K")
    assert secs == 8
    assert credits == 96  # 64 x 1.5


def test_quote_2k_minimum():
    # 4.5s -> ceil 5 -> 40 min -> 60 at 2K
    _, credits = quote_mod.price_for_duration_ms(4500, resolution="2K")
    assert credits == 60


# ------------------------------------------------------------- providers multi-aspect

def test_mock_provider_aspects(tmp_path):
    from wavecinema import providers as prov_mod
    p = prov_mod.MockProvider(tmp_path / "clips")
    for aspect in ("16:9", "9:16", "1:1", "21:9", "4:3", "3:4"):
        path = p.generate({"scene_id": "s00"}, "a test prompt", 1.0,
                          aspect=aspect)
        assert Path(path).is_file()
        Path(path).unlink()


def test_mock_provider_bad_aspect(tmp_path):
    from wavecinema import providers as prov_mod
    p = prov_mod.MockProvider(tmp_path / "clips")
    with pytest.raises(ValueError, match="unsupported aspect"):
        p.generate({"scene_id": "s00"}, "prompt", 1.0, aspect="5:7")


# ------------------------------------------------------------- jobpack remix + aspect

def test_jobpack_includes_render_opts(project):
    from wavecinema import jobpack as jp_mod
    ro_mod.set_aspect_ratio(project, "9:16")
    ro_mod.set_resolution(project, "2K")
    ws = ws_mod.ProjectWorkspace.load(project)
    rev = ws.revision
    ws.state["plan"] = {"scenes": [
        {"scene_id": "s00", "start_ms": 0, "end_ms": 3000,
         "prompt": "test prompt", "cast": ""}]}
    ws.save(expected_revision=rev)
    zpath = jp_mod.build_pack(project)
    with zipfile.ZipFile(zpath) as z:
        pack = z.read("pack.json").decode()
    import json
    data = json.loads(pack)
    assert data["aspect_ratio"] == "9:16"
    assert data["resolution"] == "2K"


# ------------------------------------------------------------- route auth gates

def test_producer_redirects_when_logged_out(client):
    r = client.get("/project/wc_fake/producer", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_captions_redirects_when_logged_out(client):
    r = client.get("/project/wc_fake/captions", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_captions_srt_redirects_when_logged_out(client):
    r = client.get("/project/wc_fake/captions.srt", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")


def test_remix_redirects_when_logged_out(client):
    r = client.get("/project/wc_fake/remix", follow_redirects=False)
    assert r.status_code in (303, 307)
    assert "/login" in r.headers.get("location", "")
