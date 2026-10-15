"""Tests for Wave Cinema Phase 4: REST API, scoped tokens, MCP server."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

import main as appmod
import tokens as tokensmod
import auth as authmod
from wavecinema import workspace as ws_mod


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Isolate JSON stores + workspace to a temp dir."""
    monkeypatch.setattr(ws_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ws_mod, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(authmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(authmod, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(tokensmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(tokensmod, "TOKENS_FILE", tmp_path / "api_tokens.json")
    monkeypatch.setattr(appmod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(appmod, "REGISTRY", tmp_path / "projects.json")
    monkeypatch.setattr(appmod, "QUOTES", tmp_path / "quotes.json")
    return tmp_path


@pytest.fixture
def user(tmp_data):
    return authmod.create_user("api-test@example.com", "API Tester", provider="test")


@pytest.fixture
def token(user):
    public, raw = tokensmod.create_token(user["id"], "test-token")
    return public, raw


@pytest.fixture
def client():
    return TestClient(appmod.app, raise_server_exceptions=False)


def _auth(raw):
    return {"Authorization": f"Bearer {raw}"}


# ------------------------------------------------------------- tokens

def test_token_create_format(user):
    public, raw = tokensmod.create_token(user["id"], "my agent")
    assert raw.startswith("wc_")
    assert len(raw) == 3 + 32
    assert public["name"] == "my agent"
    assert public["user_id"] == user["id"]
    assert "token_hash" not in public


def test_token_verify_roundtrip(user):
    public, raw = tokensmod.create_token(user["id"], "rt")
    rec = tokensmod.verify_token(raw)
    assert rec is not None
    assert rec["id"] == public["id"]
    assert rec["user_id"] == user["id"]


def test_token_verify_rejects_bad(user, token):
    _, raw = token
    assert tokensmod.verify_token("wc_" + "0" * 32) is None
    assert tokensmod.verify_token("not-a-token") is None
    assert tokensmod.verify_token("") is None
    assert tokensmod.verify_token(None) is None


def test_token_revoke(user):
    public, raw = tokensmod.create_token(user["id"], "gone")
    assert tokensmod.revoke_token(user["id"], public["id"]) is True
    assert tokensmod.verify_token(raw) is None
    assert tokensmod.revoke_token(user["id"], public["id"]) is False


def test_token_list_scoped(user, tmp_data):
    other = authmod.create_user("other@example.com", "Other", provider="test")
    tokensmod.create_token(user["id"], "mine")
    tokensmod.create_token(other["id"], "theirs")
    mine = tokensmod.list_tokens(user["id"])
    assert len(mine) == 1
    assert mine[0]["name"] == "mine"
    assert "token_hash" not in mine[0]


def test_revoke_cannot_touch_other_users_token(user, tmp_data):
    other = authmod.create_user("other2@example.com", "Other2", provider="test")
    public, raw = tokensmod.create_token(other["id"], "theirs")
    assert tokensmod.revoke_token(user["id"], public["id"]) is False
    assert tokensmod.verify_token(raw) is not None


# ------------------------------------------------------------- API auth

def test_api_no_token_401(client):
    r = client.get("/api/v1/projects")
    assert r.status_code == 401


def test_api_bad_token_401(client):
    r = client.get("/api/v1/projects", headers=_auth("wc_" + "0" * 32))
    assert r.status_code == 401


def test_api_malformed_header_401(client, token):
    _, raw = token
    r = client.get("/api/v1/projects", headers={"Authorization": raw})
    assert r.status_code == 401


def test_capabilities(client, token):
    _, raw = token
    r = client.get("/api/v1/capabilities", headers=_auth(raw))
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "wave-cinema"
    assert "/api/v1/projects" in " ".join(body["endpoints"])
    assert "mcp" in body


# ------------------------------------------------------------- projects API

def test_project_crud_cycle(client, token, user):
    _, raw = token
    h = _auth(raw)
    # create
    r = client.post("/api/v1/projects", json={"name": "API Film"}, headers=h)
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    # list contains it
    r = client.get("/api/v1/projects", headers=h)
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["projects"]]
    assert pid in ids
    # get snapshot
    r = client.get(f"/api/v1/projects/{pid}", headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "API Film"
    # missing project -> 404
    r = client.get("/api/v1/projects/wc_nope", headers=h)
    assert r.status_code == 404


def test_projects_scoped_to_owner(client, tmp_data):
    u1 = authmod.create_user("u1@example.com", "U1", provider="test")
    u2 = authmod.create_user("u2@example.com", "U2", provider="test")
    _, raw1 = tokensmod.create_token(u1["id"], "t1")
    _, raw2 = tokensmod.create_token(u2["id"], "t2")
    r = client.post("/api/v1/projects", json={"name": "U1 film"},
                    headers=_auth(raw1))
    pid = r.json()["id"]
    # u2 cannot see u1's project
    r = client.get(f"/api/v1/projects/{pid}", headers=_auth(raw2))
    assert r.status_code == 404
    r = client.get("/api/v1/projects", headers=_auth(raw2))
    assert all(p["id"] != pid for p in r.json()["projects"])


def test_plan_empty_before_direct(client, token):
    _, raw = token
    r = client.post("/api/v1/projects", json={"name": "Plan Test"},
                    headers=_auth(raw))
    pid = r.json()["id"]
    r = client.get(f"/api/v1/projects/{pid}/plan", headers=_auth(raw))
    assert r.status_code == 200
    assert r.json()["scenes"] == []


# ------------------------------------------------------------- billing API

def test_balance_and_ledger(client, token, user):
    _, raw = token
    h = _auth(raw)
    r = client.get("/api/v1/billing/balance", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["account_id"] == authmod.ledger_account_id(user)
    assert isinstance(body["balance"], int)
    r = client.get("/api/v1/billing/ledger", headers=h)
    assert r.status_code == 200
    assert "entries" in r.json()


# ------------------------------------------------------------- rate limit

def test_rate_limit(client, token):
    from api_v1 import _rate_buckets
    _rate_buckets.clear()
    _, raw = token
    h = _auth(raw)
    statuses = set()
    for _ in range(65):
        r = client.get("/api/v1/capabilities", headers=h)
        statuses.add(r.status_code)
    assert 429 in statuses
    _rate_buckets.clear()


# ------------------------------------------------------------- MCP

def test_mcp_info_no_auth(client):
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["service"] == "wave-cinema"


def test_mcp_initialize(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["protocolVersion"] == "2024-11-05"
    assert body["result"]["serverInfo"]["name"] == "wave-cinema"


def test_mcp_tools_list_requires_auth(client):
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert r.status_code == 200
    assert r.json()["error"]["code"] == -32001


def test_mcp_tools_list_and_call(client, token, user):
    _, raw = token
    h = _auth(raw)
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=h)
    assert r.status_code == 200
    names = [t["name"] for t in r.json()["result"]["tools"]]
    for expected in ("list_projects", "create_project", "get_plan",
                     "generate_scene", "get_balance", "get_ledger"):
        assert expected in names
    # call create_project then list_projects
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "create_project", "arguments": {"name": "MCP Film"}}},
        headers=h)
    assert r.status_code == 200
    created = json.loads(r.json()["result"]["content"][0]["text"])
    assert created["name"] == "MCP Film"
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "list_projects", "arguments": {}}},
        headers=h)
    listed = json.loads(r.json()["result"]["content"][0]["text"])
    assert any(p["id"] == created["id"] for p in listed["projects"])
    # unknown tool
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}}},
        headers=h)
    assert r.json()["error"]["code"] == -32602


def test_mcp_malformed_jsonrpc(client):
    r = client.post("/mcp", json={"nope": True})
    assert r.json()["error"]["code"] == -32600
    r = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 9, "method": "bogus/method", "params": {}})
    assert r.json()["error"]["code"] == -32601


# ------------------------------------------------------------- docs + account

def test_api_docs_public(client):
    r = client.get("/api/docs")
    assert r.status_code == 200
    assert "DEVELOPER API" in r.text
    assert "/api/v1/projects" in r.text


def test_account_requires_login(client):
    # No session cookie -> redirect to /login (not JSON).
    r = client.get("/account", follow_redirects=False)
    assert r.status_code in (303, 307)
