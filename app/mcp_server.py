"""Wave Cinema MCP server: Model Context Protocol over streamable HTTP.

Mounted at /mcp. Minimal JSON-RPC 2.0 implementation (the `mcp` package
is not installed): handles initialize, notifications/initialized,
tools/list, and tools/call. Authenticated with the same bearer API
tokens as /api/v1/*.

Tools: list_projects, create_project, get_plan, generate_scene,
        get_balance, get_ledger.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import auth as authmod
import tokens as tokensmod
from backend_client import Backend, bcall

backend = Backend()

router = APIRouter()

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "wave-cinema"
SERVER_VERSION = "1.0.0"


def _rpc_ok(rpc_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def _rpc_err(rpc_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                         "error": {"code": code, "message": message}})


def _api_user_from_header(authorization: Optional[str]) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    record = tokensmod.verify_token(authorization[7:].strip())
    if not record:
        raise HTTPException(status_code=401, detail="Invalid API token")
    user = authmod.get_user(record["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="Token owner not found")
    return user


def _text_result(data: Any) -> dict:
    import json
    return {"content": [{"type": "text",
                         "text": json.dumps(data, indent=2, default=str)}]}


def _owned_project(pid: str, user: dict) -> tuple[Optional[dict], Optional[str]]:
    ok, p = bcall(backend.get_project, pid)
    if not ok or not isinstance(p, dict):
        return None, "Project not found"
    owner_id = p.get("owner_id")
    if owner_id and owner_id != user["id"]:
        return None, "Project not found"
    return p, None


# ------------------------------------------------------------------ tools

def tool_list_projects(user: dict, args: dict) -> dict:
    import main as mainmod
    items = mainmod.registry_list(user["id"])
    return {"projects": [{"id": i.get("id"), "name": i.get("name")} for i in items]}


def tool_create_project(user: dict, args: dict) -> dict:
    import main as mainmod
    name = str(args.get("name", "")).strip() or "Untitled film"
    ok, pid = bcall(backend.create_project, name)
    if not ok:
        return {"error": f"Could not create project: {pid}"}
    try:
        from wavecinema import workspace as _wsm
        w = _wsm.ProjectWorkspace.load(pid)
        rev = w.revision
        w.state["owner_id"] = user["id"]
        w.save(expected_revision=rev)
    except Exception:
        pass
    mainmod.registry_add(pid, name, user["id"])
    return {"id": pid, "name": name}


def tool_get_plan(user: dict, args: dict) -> dict:
    pid = str(args.get("project_id", ""))
    p, err = _owned_project(pid, user)
    if err:
        return {"error": err}
    scenes = ((p.get("plan") or {}).get("scenes")) or []
    return {"project_id": pid, "scenes": [
        {"scene_id": s.get("scene_id"), "start_ms": s.get("start_ms"),
         "end_ms": s.get("end_ms"), "cast": s.get("cast"),
         "prompt": s.get("prompt")}
        for s in scenes]}


def tool_generate_scene(user: dict, args: dict) -> dict:
    pid = str(args.get("project_id", ""))
    scene_id = str(args.get("scene_id", ""))
    if not pid or not scene_id:
        return {"error": "project_id and scene_id are required"}
    _, err = _owned_project(pid, user)
    if err:
        return {"error": err}
    ok, quote = bcall(backend.quote_scene, pid, scene_id)
    if not ok:
        return {"error": f"Quote failed: {quote}"}
    quote_id = quote.get("quote_id") or quote.get("id")
    if not quote_id:
        return {"error": "Quote returned no id"}
    ok, approved = bcall(backend.approve_quote, pid, quote_id,
                         account_id=authmod.ledger_account_id(user))
    if not ok:
        return {"error": f"Approval failed: {approved}"}
    key = uuid.uuid4().hex
    ok, job = bcall(backend.generate_scene, pid, scene_id, key)
    if not ok:
        return {"error": f"Generation submit failed: {job}"}
    return {"ok": True, "scene_id": scene_id, "quote_id": quote_id,
            "credits": (approved or {}).get("credits_total"),
            "idempotency_key": key}


def tool_get_balance(user: dict, args: dict) -> dict:
    acct = authmod.ledger_account_id(user)
    ok, bal = bcall(backend.credit_balance, account_id=acct)
    try:
        bal = int(bal) if ok else 0
    except (TypeError, ValueError):
        bal = 0
    return {"account_id": acct, "balance": bal}


def tool_get_ledger(user: dict, args: dict) -> dict:
    acct = authmod.ledger_account_id(user)
    try:
        limit = max(1, min(int(args.get("limit", 50)), 200))
    except (TypeError, ValueError):
        limit = 50
    ok, hist = bcall(backend.ledger_history, acct, limit)
    return {"account_id": acct,
            "entries": hist if ok and isinstance(hist, list) else []}


TOOLS = {
    "list_projects": (
        "List the authenticated user's Wave Cinema projects.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        tool_list_projects,
    ),
    "create_project": (
        "Create a new Wave Cinema film project.",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "Film title"}},
         "additionalProperties": False},
        tool_create_project,
    ),
    "get_plan": (
        "Get the director's shot plan (scenes with prompts) for a project.",
        {"type": "object",
         "properties": {"project_id": {"type": "string"}},
         "required": ["project_id"], "additionalProperties": False},
        tool_get_plan,
    ),
    "generate_scene": (
        "Quote, approve, and start generation for one scene (spends credits).",
        {"type": "object",
         "properties": {"project_id": {"type": "string"},
                        "scene_id": {"type": "string"}},
         "required": ["project_id", "scene_id"], "additionalProperties": False},
        tool_generate_scene,
    ),
    "get_balance": (
        "Get the authenticated user's credit balance.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        tool_get_balance,
    ),
    "get_ledger": (
        "Get the authenticated user's credit usage history.",
        {"type": "object",
         "properties": {"limit": {"type": "string",
                                 "description": "Max entries (default 50)"}},
         "additionalProperties": False},
        tool_get_ledger,
    ),
}


# ------------------------------------------------------------------ router

@router.get("/mcp")
def mcp_info():
    return {
        "service": SERVER_NAME,
        "transport": "streamable-http",
        "endpoint": "/mcp",
        "protocol": "json-rpc-2.0",
        "auth": "Authorization: Bearer <api-token>",
    }


@router.post("/mcp")
async def mcp_endpoint(request: Request,
                       authorization: Optional[str] = Header(None)):
    try:
        body = await request.json()
    except Exception:
        return _rpc_err(None, -32700, "Parse error: body must be JSON")
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _rpc_err(body.get("id") if isinstance(body, dict) else None,
                        -32600, "Invalid Request: expected JSON-RPC 2.0")
    method = body.get("method")
    rpc_id = body.get("id")
    params = body.get("params") or {}

    # Notifications (no id) get 202 with no body.
    if rpc_id is None:
        return JSONResponse(status_code=202, content={})

    if method == "initialize":
        return _rpc_ok(rpc_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "tools/list":
        try:
            _api_user_from_header(authorization)
        except HTTPException as e:
            return _rpc_err(rpc_id, -32001, f"Unauthorized: {e.detail}")
        return _rpc_ok(rpc_id, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (desc, schema, _) in TOOLS.items()
        ]})
    if method == "tools/call":
        try:
            user = _api_user_from_header(authorization)
        except HTTPException as e:
            return _rpc_err(rpc_id, -32001, f"Unauthorized: {e.detail}")
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return _rpc_err(rpc_id, -32602, f"Unknown tool: {name}")
        _, _, fn = TOOLS[name]
        try:
            result = fn(user, args if isinstance(args, dict) else {})
        except Exception as e:  # noqa: BLE001
            return _rpc_ok(rpc_id, _text_result({"error": f"{type(e).__name__}: {e}"}))
        return _rpc_ok(rpc_id, _text_result(result))
    if method == "ping":
        return _rpc_ok(rpc_id, {})
    return _rpc_err(rpc_id, -32601, f"Method not found: {method}")
