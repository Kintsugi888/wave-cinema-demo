"""Wave Cinema REST API v1: JSON API for agents, bearer-token authenticated.

All routes live under /api/v1/*. Authentication is via
`Authorization: Bearer <token>` where the token was created on the
Account page (stored hashed, `wc_` prefix).

Rate limit: 60 requests/minute per token (in-memory sliding window).
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

import auth as authmod
import tokens as tokensmod
from backend_client import Backend, bcall

backend = Backend()

router = APIRouter(prefix="/api/v1")

# ------------------------------------------------------------------ auth

RATE_LIMIT = 60  # requests per minute per token
_rate_buckets: dict[str, list[float]] = {}


def _check_rate_limit(token_id: str) -> None:
    now = time.time()
    window_start = now - 60.0
    bucket = _rate_buckets.get(token_id, [])
    bucket = [t for t in bucket if t > window_start]
    if len(bucket) >= RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded: 60 requests/minute per token")
    bucket.append(now)
    _rate_buckets[token_id] = bucket


def get_api_user(authorization: Optional[str]) -> tuple[dict, dict]:
    """Verify bearer token, return (user, token_record). Raises 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401,
                            detail="Missing or malformed Authorization: Bearer <token> header")
    raw = authorization[7:].strip()
    record = tokensmod.verify_token(raw)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid API token")
    user = authmod.get_user(record["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="Token owner no longer exists")
    _check_rate_limit(record["id"])
    return user, record


def _dep(request: Request) -> tuple[dict, dict]:
    """FastAPI dependency: verify bearer token, return (user, token_record)."""
    return get_api_user(request.headers.get("authorization"))


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _project_owned(pid: str, user: dict) -> tuple[bool, object, Optional[str]]:
    """Load project snapshot; enforce ownership. Returns (ok, data, error)."""
    ok, p = bcall(backend.get_project, pid)
    if not ok:
        return False, None, "Project not found"
    if not isinstance(p, dict):
        return False, None, "Project not found"
    owner_id = p.get("owner_id")
    if owner_id and owner_id != user["id"]:
        return False, None, "Project not found"
    return True, p, None


def _account_id(user: dict) -> str:
    return authmod.ledger_account_id(user)


# ------------------------------------------------------------ capabilities

@router.get("/capabilities")
def capabilities(auth: tuple = Depends(_dep)):
    """What this API can do. No auth scope beyond a valid token."""
    ok_aspects, aspects = bcall(backend.aspect_ratios)
    ok_res, resolutions = bcall(backend.resolutions)
    return {
        "service": "wave-cinema",
        "api_version": "v1",
        "endpoints": [
            "GET /api/v1/capabilities",
            "GET /api/v1/projects",
            "POST /api/v1/projects",
            "GET /api/v1/projects/{pid}",
            "POST /api/v1/projects/{pid}/direct",
            "GET /api/v1/projects/{pid}/plan",
            "POST /api/v1/projects/{pid}/generate",
            "GET /api/v1/projects/{pid}/takes",
            "GET /api/v1/billing/balance",
            "GET /api/v1/billing/ledger",
        ],
        "aspect_ratios": aspects if ok_aspects else [],
        "resolutions": resolutions if ok_res else [],
        "generation_lanes": ["byo", "cloud"],
        "mcp": "/mcp",
        "docs": "/api/docs",
    }


# ---------------------------------------------------------------- projects

def _project_summary(pid: str, name: str, p: Optional[dict]) -> dict:
    p = p or {}
    scenes = p.get("scenes") or []
    plan = p.get("plan") or {}
    plan_scenes = plan.get("scenes") or []
    return {
        "id": pid,
        "name": name,
        "stage": p.get("stage") or None,
        "scenes": len(scenes),
        "planned_scenes": len(plan_scenes),
        "has_song": bool(p.get("song")),
        "has_plan": bool(plan_scenes),
        "updated_at": p.get("updated_at"),
    }


@router.get("/projects")
def list_projects(request: Request, auth: tuple = Depends(_dep)):
    user, _ = auth
    # Reuse the web UI's per-user registry.
    import main as mainmod
    items = mainmod.registry_list(user["id"])
    out = []
    for item in items:
        pid = item.get("id")
        ok, p = bcall(backend.get_project, pid)
        out.append(_project_summary(pid, item.get("name", pid),
                                    p if ok and isinstance(p, dict) else None))
    return {"projects": out}


@router.post("/projects")
async def create_project(request: Request, auth: tuple = Depends(_dep)):
    user, _ = auth
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name", "")).strip() or "Untitled film"
    ok, pid = bcall(backend.create_project, name)
    if not ok:
        return _err(500, f"Could not create project: {pid}")
    import main as mainmod
    # Stamp ownership the same way the web UI does.
    ws_ok, ws = bcall(backend.get_project, pid)
    if ws_ok and isinstance(ws, dict):
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


@router.get("/projects/{pid}")
def get_project(pid: str, auth: tuple = Depends(_dep)):
    user, _ = auth
    ok, p, err = _project_owned(pid, user)
    if not ok:
        return _err(404, err or "Project not found")
    plan = p.get("plan") or {}
    return {
        "id": pid,
        "name": p.get("name"),
        "stage": p.get("stage"),
        "owner_id": p.get("owner_id"),
        "song": bool(p.get("song")),
        "cast": [
            {"id": c.get("id"), "name": c.get("name")}
            for c in (p.get("characters") or {}).values()
        ] if isinstance(p.get("characters"), dict) else [],
        "scenes": len(p.get("scenes") or []),
        "plan_scenes": len(plan.get("scenes") or []),
        "aspect_ratio": p.get("aspect_ratio"),
        "resolution": p.get("resolution"),
        "provider_lane": p.get("provider_lane", "byo"),
        "updated_at": p.get("updated_at"),
    }


@router.post("/projects/{pid}/direct")
async def direct_project(request: Request, pid: str, auth: tuple = Depends(_dep)):
    user, _ = auth
    ok, _, err = _project_owned(pid, user)
    if not ok:
        return _err(404, err or "Project not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    treatment = str((body or {}).get("treatment", ""))
    ok, res = bcall(backend.direct, pid, treatment)
    if not ok:
        return _err(400, f"Director failed: {res}")
    plan = (res or {}).get("plan") or {}
    return {
        "ok": True,
        "scenes": len(plan.get("scenes") or []),
        "plan": plan,
    }


@router.get("/projects/{pid}/plan")
def get_plan(pid: str, auth: tuple = Depends(_dep)):
    user, _ = auth
    ok, p, err = _project_owned(pid, user)
    if not ok:
        return _err(404, err or "Project not found")
    plan = p.get("plan") or {}
    scenes = plan.get("scenes") or []
    return {
        "project_id": pid,
        "scenes": [
            {
                "scene_id": s.get("scene_id"),
                "start_ms": s.get("start_ms"),
                "end_ms": s.get("end_ms"),
                "cast": s.get("cast"),
                "prompt": s.get("prompt"),
            }
            for s in scenes
        ],
    }


@router.post("/projects/{pid}/generate")
async def generate(request: Request, pid: str, auth: tuple = Depends(_dep)):
    """Quote + approve + start generation for one scene (paid)."""
    user, _ = auth
    ok, _, err = _project_owned(pid, user)
    if not ok:
        return _err(404, err or "Project not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    scene_id = str((body or {}).get("scene_id", "")).strip()
    if not scene_id:
        return _err(400, "Missing scene_id")
    # 1. Quote (free).
    ok, quote = bcall(backend.quote_scene, pid, scene_id)
    if not ok:
        return _err(400, f"Quote failed: {quote}")
    quote_id = quote.get("quote_id") or quote.get("id")
    if not quote_id:
        return _err(500, "Quote returned no id")
    # 2. Approve (creates the hold against the token owner's account).
    ok, approved = bcall(backend.approve_quote, pid, quote_id,
                         account_id=_account_id(user))
    if not ok:
        return _err(402, f"Approval failed: {approved}")
    # 3. Generate (idempotent on our fresh key).
    key = uuid.uuid4().hex
    ok, job = bcall(backend.generate_scene, pid, scene_id, key)
    if not ok:
        return _err(500, f"Generation submit failed: {job}")
    return {
        "ok": True,
        "scene_id": scene_id,
        "quote_id": quote_id,
        "credits": (approved or {}).get("credits_total"),
        "job": job if isinstance(job, dict) else {"result": job},
        "idempotency_key": key,
    }


@router.get("/projects/{pid}/takes")
def takes(pid: str, scene_id: Optional[str] = None,
          auth: tuple = Depends(_dep)):
    user, _ = auth
    ok, p, err = _project_owned(pid, user)
    if not ok:
        return _err(404, err or "Project not found")
    plan = p.get("plan") or {}
    scenes = plan.get("scenes") or []
    scene_ids = [s.get("scene_id") for s in scenes if s.get("scene_id")]
    if scene_id:
        if scene_id not in scene_ids:
            return _err(404, f"No such scene: {scene_id}")
        scene_ids = [scene_id]
    out = {}
    for sid in scene_ids:
        ok, tk = bcall(backend.list_takes, pid, sid)
        out[sid] = tk if ok and isinstance(tk, list) else []
    return {"project_id": pid, "takes": out}


# ---------------------------------------------------------------- billing

@router.get("/billing/balance")
def balance(auth: tuple = Depends(_dep)):
    user, _ = auth
    ok, bal = bcall(backend.credit_balance, account_id=_account_id(user))
    if not ok:
        return _err(500, f"Could not read balance: {bal}")
    try:
        bal = int(bal)
    except (TypeError, ValueError):
        bal = 0
    return {"account_id": _account_id(user), "balance": bal}


@router.get("/billing/ledger")
def ledger(limit: int = 50, auth: tuple = Depends(_dep)):
    user, _ = auth
    limit = max(1, min(int(limit or 50), 200))
    ok, hist = bcall(backend.ledger_history, _account_id(user), limit)
    if not ok:
        return _err(500, f"Could not read ledger: {hist}")
    return {"account_id": _account_id(user), "entries": hist if isinstance(hist, list) else []}
