"""Wave Cinema web UI: FastAPI app (Midnight Chrome v1.0).

Serves the AI music-video workstation: dashboard, project pipeline
(01 Ingest → 02 Direct → 03 Review → 04 Export), paywall hook.
Backend calls go through backend_client (safe, degrading).
"""

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, File, UploadFile, Query, Request, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend_client import Backend, bcall
import auth as authmod

# Bundled ffmpeg on PATH (free hosts ship no system ffmpeg/ffprobe).
from wavecinema import ffmpeg_bin  # noqa: F401  (import-time PATH fix)

APP_DIR = Path(__file__).resolve().parent
WC_ROOT = (APP_DIR / "..").resolve()
DATA_DIR = APP_DIR / "data"
REGISTRY = DATA_DIR / "projects.json"
QUOTES = DATA_DIR / "quotes.json"
LOGO = (WC_ROOT / "brand" / "logo.jpg").resolve()

RIGHTS_VERSION = "wc-rights-v1.0 (2026-10-14)"
RIGHTS_TEXT = """WAVE CINEMA RIGHTS STATEMENT (version wc-rights-v1.0, 2026-10-14)

1. This statement covers exactly the song file(s) you upload in this session.
2. Purpose: generating an AI music video for that song inside Wave Cinema
   (scene planning, clip generation, review, assembly, export).
3. You confirm you own or control the rights to the uploaded song, including
   the right to create derivative audiovisual works from it.
4. This assent is recorded per statement version, per session. It is never
   inferred from past sessions.
5. If the statement version changes, you will be asked to assent again.
"""

MAX_UPLOAD_BYTES = 250 * 1024 * 1024
ALLOWED_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".flac"}

app = FastAPI(title="Wave Cinema", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

backend = Backend()

STAGES = [
    {"key": "ingest", "num": "01", "label": "Drop the Track"},
    {"key": "direct", "num": "02", "label": "Direct the Film"},
    {"key": "review", "num": "03", "label": "Pick Your Shots"},
    {"key": "export", "num": "04", "label": "Ship It"},
]
STAGE_KEYS = [s["key"] for s in STAGES]


# ------------------------------------------------------------------ auth

PUBLIC_PATHS = ("/", "/faq", "/terms", "/privacy", "/refunds",
                "/login", "/auth/", "/brand/", "/static/",
                "/tools", "/wiki")


def get_current_user(request: Request) -> Optional[dict]:
    token = request.cookies.get(authmod.SESSION_COOKIE)
    if not token:
        return None
    return authmod.verify_session(token)


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise _LoginRequired()
    return user


class _LoginRequired(Exception):
    pass


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    is_api = path.startswith("/api/v1/") or path == "/mcp" or path.startswith("/mcp")
    is_public = path in PUBLIC_PATHS or any(
        path.startswith(p) for p in PUBLIC_PATHS if p.endswith("/"))
    # /billing page itself is public (shows packs); checkout requires login.
    if path == "/billing" or path.startswith("/billing/"):
        is_public = path in ("/billing", "/billing/success")
    if path.startswith("/webhooks/"):
        is_public = True
    if path == "/api/docs":
        is_public = True
    if not is_public and not get_current_user(request):
        if is_api:
            # API clients get JSON 401s, never a login redirect.
            # (Bearer-token auth happens per-route; this is the no-session case.)
            return JSONResponse(status_code=401,
                                content={"error": "Authentication required: send Authorization: Bearer <api-token>"})
        return RedirectResponse("/login", status_code=303)
    request.state.user = get_current_user(request)
    try:
        return await call_next(request)
    except _LoginRequired:
        return RedirectResponse("/login", status_code=303)


def _set_session_cookie(resp: RedirectResponse, user: dict) -> RedirectResponse:
    token = authmod.create_session(user["id"])
    resp.set_cookie(
        authmod.SESSION_COOKIE, token,
        max_age=authmod.SESSION_MAX_AGE,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return resp


# ---------------------------------------------------------------- helpers

def _g(d: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
    return default


def ms_to_ts(ms: Any) -> str:
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return "-"
    m, rem = divmod(ms, 60000)
    s, mm = divmod(rem, 1000)
    return f"{m:02d}:{s:02d}.{mm:03d}"


def fmt_duration(sec: Any) -> str:
    try:
        total_ms = int(float(sec) * 1000)
    except (TypeError, ValueError):
        return "-"
    return ms_to_ts(total_ms)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2))


def registry_list(user_id: Optional[str] = None) -> list:
    items = _load_json(REGISTRY, [])
    if not isinstance(items, list):
        return []
    if user_id:
        items = [i for i in items
                 if i.get("owner_id") == user_id or "owner_id" not in i]
    return items


def registry_add(pid: str, name: str, user_id: Optional[str] = None) -> None:
    items = [i for i in _load_json(REGISTRY, []) if isinstance(i, dict)
             and i.get("id") != pid]
    entry = {"id": pid, "name": name}
    if user_id:
        entry["owner_id"] = user_id
    items.append(entry)
    _save_json(REGISTRY, items)


def pending_quotes() -> dict:
    q = _load_json(QUOTES, {})
    return q if isinstance(q, dict) else {}


def save_pending_quotes(q: dict) -> None:
    _save_json(QUOTES, q)


def get_credits(user: Optional[dict] = None) -> Optional[int]:
    account_id = authmod.ledger_account_id(user) if user else "owner"
    ok, res = bcall(backend.credit_balance, account_id=account_id)
    if not ok:
        return None
    if isinstance(res, dict):
        res = _g(res, "balance", "credits", "total", default=None)
    try:
        return int(res)
    except (TypeError, ValueError):
        return None


def ctx(request: Optional[Request] = None,
        error: Optional[str] = None,
        nav_active: Optional[str] = None, **kw) -> dict:
    user = getattr(request.state, "user", None) if request else None
    credits = get_credits(user)
    base = {
        "backend_ok": backend.ok,
        "credits": f"{credits:,}" if credits is not None else None,
        "error": error,
        "nav_active": nav_active,
        "rights_version": RIGHTS_VERSION,
        "rights_text": RIGHTS_TEXT,
        "user": user,
    }
    base.update(kw)
    return base


def media_url(value: Any, project_id: str) -> Optional[str]:
    """Turn a backend media reference (URL, /path, or workspace file path)
    into a browser-usable URL. Absolute paths are served through a guarded
    route that only allows files under the wave-cinema root."""
    if not value or not isinstance(value, str):
        return None
    if value.startswith(("http://", "https://", "/")):
        return value
    try:
        p = Path(value).resolve()
    except Exception:
        return None
    if WC_ROOT in p.parents or p == WC_ROOT:
        token = base64.urlsafe_b64encode(str(p).encode()).decode()
        return f"/project/{project_id}/media?path={token}"
    return None


# ------------------------------------------------------- project parsing

def project_of(pid: str) -> tuple[bool, Any, Optional[str]]:
    ok, res = bcall(backend.get_project, pid)
    if not ok:
        return False, None, res
    if not isinstance(res, dict):
        return False, None, f"get_project returned unexpected type {type(res).__name__}"
    return True, res, None


def song_view(p: dict) -> Optional[dict]:
    s = _g(p, "song", "ingest", "audio", default=None)
    if not isinstance(s, dict):
        return None
    if not _g(s, "sha256", "sha", "hash") and not _g(s, "filename", "name"):
        return None
    return {
        "filename": _g(s, "filename", "name", default="song"),
        "duration": fmt_duration(_g(s, "duration_sec", "duration", "seconds")),
        "sha256": _g(s, "sha256", "sha", "hash", default="-"),
        "rights_version": _g(s, "rights_version", "rights_statement_version"),
    }


def bible_text(p: dict) -> Optional[str]:
    b = _g(p, "bible", "production_bible", "plan", default=None)
    if b is None:
        return None
    if isinstance(b, str):
        return b
    if isinstance(b, dict):
        parts = []
        for k in ("visual_language", "camera_grammar", "identity_locks",
                  "continuity_anchors", "forbidden", "story_arc", "treatment"):
            v = b.get(k)
            if v:
                parts.append(f"{k.replace('_', ' ').upper()}\n{v}" if isinstance(v, str)
                             else f"{k.replace('_', ' ').upper()}\n" + json.dumps(v, indent=2))
        return "\n\n".join(parts) if parts else json.dumps(b, indent=2)
    return str(b)


def scene0_selected(p: dict, scenes: list) -> bool:
    if not scenes:
        return False
    first = scenes[0]
    if isinstance(first, dict) and (_g(first, "selected_take_id", "selected_take")):
        return True
    sel = _g(p, "selections", "selected_takes", default=None)
    if isinstance(sel, dict) and sel:
        return True
    return False


def scene_views(p: dict) -> list:
    raw = _g(p, "scenes", "plan_scenes", default=[]) or []
    # Merge director plan data (prompts, cast) into the scene views.
    plan_scenes = {}
    plan = p.get("plan") if isinstance(p.get("plan"), dict) else None
    if plan:
        for ps in plan.get("scenes", []) or []:
            pid = str(_g(ps, "scene_id", "id", default=""))
            if pid:
                plan_scenes[pid] = ps
    first_ok = scene0_selected(p, raw if isinstance(raw, list) else [])
    views = []
    quotes = pending_quotes().get(p.get("id", ""), {})
    for i, s in enumerate(raw):
        sid = str(_g(s, "scene_id", "id", "index", default=i))
        ps = plan_scenes.get(sid, {})
        cast = _g(s, "cast", "characters", default=None)
        if cast is None or cast == "-":
            cast = _g(ps, "cast", default="-")
        if isinstance(cast, list):
            cast = ", ".join(str(c) for c in cast)
        prompt = str(_g(s, "prompt", "scene_prompt", default="") or _g(ps, "prompt", default=""))
        q = quotes.get(sid)
        views.append({
            "id": sid,
            "n": i,
            "start": ms_to_ts(_g(s, "start_ms", "start", "startMs", default=0)),
            "end": ms_to_ts(_g(s, "end_ms", "end", "endMs", default=0)),
            "cast": cast,
            "prompt": prompt,
            "prompt_short": (prompt[:140] + "…") if len(prompt) > 140 else prompt,
            "gated": i > 0 and not first_ok,
            "quote": quote_view(q) if q else None,
        })
    return views


def quote_view(q: dict) -> dict:
    total = _g(q, "total", "credits", "amount", default="-")
    currency = _g(q, "currency", default="credits")
    detail_bits = []
    for k, label in (("model", None), ("resolution", None),
                     ("billable_seconds", "s"), ("job_count", "jobs")):
        v = _g(q, k)
        if v is not None:
            detail_bits.append(f"{v}{label or ''}" if label else str(v))
    return {
        "quote_id": _g(q, "quote_id", "id", default=""),
        "total": total,
        "currency": currency,
        "detail": " · ".join(detail_bits),
    }


def takes_view(pid: str, p: dict) -> list:
    """Per-scene takes for the Review stage. Only scenes with takes are returned."""
    raw = _g(p, "scenes", default=[]) or []
    out = []
    for i, s in enumerate(raw if isinstance(raw, list) else []):
        sid = str(_g(s, "scene_id", "id", "index", default=i))
        sel_id = str(_g(s, "selected_take_id", "selected_take", default="") or "")
        ok, takes = bcall(backend.list_takes, pid, sid)
        if not ok or not takes:
            continue
        cards = []
        for t in takes if isinstance(takes, list) else []:
            tid = str(_g(t, "take_id", "id", default=""))
            sha = str(_g(t, "sha256", "sha", "hash", default="") or "")
            cards.append({
                "take_id": tid,
                "video_url": media_url(_g(t, "video_url", "url", "path", "file"), pid),
                "sha_short": (sha[:12] + "…") if len(sha) > 12 else (sha or "-"),
                "status": str(_g(t, "status", default="ready")),
                "selected": bool(sel_id and tid == sel_id) or bool(_g(t, "selected")),
            })
        out.append({
            "id": sid,
            "n": i,
            "start": ms_to_ts(_g(s, "start_ms", "start", "startMs", default=0)),
            "end": ms_to_ts(_g(s, "end_ms", "end", "endMs", default=0)),
            "takes": cards,
        })
    return out


def master_view(pid: str, p: dict) -> Optional[dict]:
    m = _g(p, "master", "export", default=None)
    if not isinstance(m, dict):
        return None
    url = media_url(_g(m, "url", "video_url", "path", "file"), pid)
    if not url:
        return None
    return {
        "url": url,
        "bytes": _g(m, "bytes", "size", default="-"),
        "sha256": _g(m, "sha256", "sha", "hash", default="-"),
    }


def ready_to_assemble(p: dict) -> bool:
    raw = _g(p, "scenes", default=[]) or []
    if not raw:
        return False
    for s in raw:
        if not _g(s, "selected_take_id", "selected_take"):
            sel = _g(p, "selections", "selected_takes", default={}) or {}
            if isinstance(sel, dict) and sel:
                return True  # selections map present: trust backend shape
            return False
    return True


def detect_stage(p: dict) -> str:
    if not song_view(p):
        return "ingest"
    scenes = _g(p, "scenes", default=[]) or []
    if not scenes or not bible_text(p):
        return "direct"
    if master_view("", p):
        return "export"
    return "review"


# ----------------------------------------------------------------- routes

@app.get("/brand/logo.jpg")
def logo():
    if LOGO.exists():
        return FileResponse(str(LOGO), media_type="image/jpeg")
    return HTMLResponse("logo missing", status_code=404)


def _simple_page(request: Request, template: str, title: str):
    return templates.TemplateResponse(request, template, {
        "request": request, "credits": get_credits(), "backend_ok": True,
    })


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return _simple_page(request, "terms.html", "Terms of Service")


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return _simple_page(request, "privacy.html", "Privacy Policy")


@app.get("/refunds", response_class=HTMLResponse)
def refunds(request: Request):
    return _simple_page(request, "refunds.html", "Refund Policy")


@app.get("/faq", response_class=HTMLResponse)
def faq(request: Request):
    return _simple_page(request, "faq.html", "FAQ")


@app.get("/project/{pid}/media")
def project_media(pid: str, path: str = Query(...)):
    try:
        p = Path(base64.urlsafe_b64decode(path.encode()).decode()).resolve()
    except Exception:
        return HTMLResponse("bad media token", status_code=400)
    if not (WC_ROOT in p.parents or p == WC_ROOT) or not p.is_file():
        return HTMLResponse("not found", status_code=404)
    return FileResponse(str(p))


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, request_error: Optional[str] = Query(None, alias="err")):
    user = get_current_user(request)
    projects = []
    for item in registry_list(user["id"] if user else None):
        pid = item.get("id", "")
        name = item.get("name", pid)
        ok, p, _ = project_of(pid)
        stage_label = detect_stage(p) if ok and p else "unknown"
        projects.append({"id": pid, "name": name, "stage_label": stage_label})
    return templates.TemplateResponse(request, "dashboard.html", ctx(request, error=request_error, projects=projects, nav_active="create"))


@app.post("/projects")
def create_project(request: Request, name: str = Form(...),
                   aspect: str = Form("16:9")):
    user = require_user(request)
    name = (name or "").strip()[:120] or "Untitled film"
    ok, res = bcall(backend.create_project, name)
    if not ok:
        return RedirectResponse(f"/?err={_q(res)}", status_code=303)
    pid = res if isinstance(res, str) else _g(res, "project_id", "id", default="")
    if not pid:
        return RedirectResponse(f"/?err={_q('create_project returned no project id')}", status_code=303)
    registry_add(str(pid), name, user["id"])
    # Apply the chosen aspect ratio (defaults to 16:9 on any failure).
    bcall(backend.set_aspect_ratio, str(pid), aspect or "16:9")
    return RedirectResponse(f"/project/{pid}", status_code=303)


# ------------------------------------------------------------------ auth routes

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request,
               err: Optional[str] = Query(None),
               notice: Optional[str] = Query(None)):
    if get_current_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", ctx(
        request, error=err, notice=notice,
        google_on=authmod.google_configured(),
        x_on=authmod.x_configured(),
    ))


@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(authmod.SESSION_COOKIE, path="/")
    return resp


@app.post("/auth/email")
def auth_email(email: str = Form(...)):
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return RedirectResponse("/login?err=Please+enter+a+valid+email.", status_code=303)
    token = authmod.create_magic_token(email)
    authmod.send_magic_link(email, token)
    return RedirectResponse(
        "/login?notice=Check+your+email+for+the+sign-in+link.", status_code=303)


@app.get("/auth/email/verify")
def auth_email_verify(token: str = Query("")):
    email = authmod.verify_magic_token(token)
    if not email:
        return RedirectResponse(
            "/login?err=That+link+is+invalid+or+expired.+Try+again.", status_code=303)
    user = authmod.create_user(email, provider="email")
    return _set_session_cookie(RedirectResponse("/", status_code=303), user)


@app.get("/auth/google")
def auth_google():
    if not authmod.google_configured():
        return RedirectResponse("/login?err=Google+sign-in+is+not+configured.",
                                status_code=303)
    state = authmod.oauth_state_token({"provider": "google"})
    return RedirectResponse(authmod.google_auth_url(state), status_code=303)


@app.get("/auth/google/callback")
def auth_google_callback(code: str = Query(""), state: str = Query("")):
    if not authmod.oauth_state_verify(state):
        return RedirectResponse("/login?err=Invalid+sign-in+state.", status_code=303)
    try:
        info = authmod.google_exchange(code)
    except Exception:
        return RedirectResponse("/login?err=Google+sign-in+failed.+Try+again.",
                                status_code=303)
    if not info.get("email"):
        return RedirectResponse("/login?err=Google+did+not+share+your+email.",
                                status_code=303)
    user = authmod.create_user(info["email"], name=info.get("name", ""),
                               avatar_url=info.get("avatar_url", ""),
                               provider="google")
    return _set_session_cookie(RedirectResponse("/", status_code=303), user)


@app.get("/auth/x")
def auth_x():
    if not authmod.x_configured():
        return RedirectResponse("/login?err=X+sign-in+is+not+configured.",
                                status_code=303)
    url, verifier = authmod.x_auth_url(
        authmod.oauth_state_token({"provider": "x"}))
    resp = RedirectResponse(url, status_code=303)
    # PKCE verifier: short-lived, httponly cookie (10 min).
    resp.set_cookie("wc_pkce", authmod.oauth_state_token({"v": verifier}),
                    max_age=600, httponly=True, secure=True, samesite="lax",
                    path="/auth/x/callback")
    return resp


@app.get("/auth/x/callback")
def auth_x_callback(request: Request,
                    code: str = Query(""), state: str = Query("")):
    if not authmod.oauth_state_verify(state):
        return RedirectResponse("/login?err=Invalid+sign-in+state.", status_code=303)
    raw = request.cookies.get("wc_pkce", "")
    verifier = (authmod.oauth_state_verify(raw) or {}).get("v", "")
    if not verifier:
        return RedirectResponse("/login?err=Sign-in+expired.+Try+again.",
                                status_code=303)
    try:
        info = authmod.x_exchange(code, verifier)
    except Exception:
        return RedirectResponse("/login?err=X+sign-in+failed.+Try+again.",
                                status_code=303)
    # X may not share email; fall back to a stable placeholder tied to username.
    email = info.get("email") or f"{info.get('x_username', 'x_user')}@x.local"
    user = authmod.create_user(email, name=info.get("name", ""),
                               avatar_url=info.get("avatar_url", ""),
                               provider="x")
    resp = _set_session_cookie(RedirectResponse("/", status_code=303), user)
    resp.delete_cookie("wc_pkce", path="/auth/x/callback")
    return resp


def _q(msg: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(str(msg)[:500])


def cast_view(pid: str, p: dict) -> list:
    """Characters for the Cast panel: name + browser-usable image URL."""
    raw = _g(p, "characters", default={}) or {}
    views = []
    for cid, c in raw.items():
        if not isinstance(c, dict):
            continue
        views.append({
            "character_id": str(_g(c, "character_id", "id", default=cid)),
            "name": str(_g(c, "name", default=cid)),
            "image_url": f"/project/{pid}/characters/{cid}/image",
            "bytes": _g(c, "bytes", default=0),
        })
    return views


def backgrounds_view(pid: str, p: dict) -> list:
    """Backgrounds for the Locations panel: name + browser-usable image URL."""
    raw = _g(p, "backgrounds", default={}) or {}
    views = []
    for bid, b in raw.items():
        if not isinstance(b, dict):
            continue
        views.append({
            "background_id": str(_g(b, "background_id", "id", default=bid)),
            "name": str(_g(b, "name", default=bid)),
            "image_url": f"/project/{pid}/backgrounds/{bid}/image",
            "bytes": _g(b, "bytes", default=0),
        })
    return views


@app.get("/project/{pid}", response_class=HTMLResponse)
def _check_owner(pid: str, p: dict, user: dict) -> Optional[str]:
    """Return an error message if the user may not access this project."""
    owner_id = p.get("owner_id") if isinstance(p, dict) else None
    if owner_id is None:
        for item in registry_list():
            if item.get("id") == pid:
                owner_id = item.get("owner_id")
                break
    if owner_id and owner_id != user["id"]:
        return "Project not found"
    return None


def project_page(request: Request,
                 pid: str,
                 stage: Optional[str] = Query(None),
                 err: Optional[str] = Query(None),
                 status: Optional[str] = Query(None)):
    user = require_user(request)
    ok, p, perr = project_of(pid)
    if not ok:
        return templates.TemplateResponse(
            request, "dashboard.html",
            ctx(request, error=perr or "Project not found", projects=[]),
            status_code=404)
    # Ownership check: users can only see their own projects.
    # Legacy projects without owner_id are visible to all signed-in users.
    owner_err = _check_owner(pid, p, user)
    if owner_err:
        return RedirectResponse("/?err=Project+not+found", status_code=303)
    active = stage if stage in STAGE_KEYS else detect_stage(p)
    order = {k: i for i, k in enumerate(STAGE_KEYS)}
    stages = [{**s, "done": order[s["key"]] < order[active]} for s in STAGES]
    scenes = scene_views(p) if active in ("direct", "review") else []
    ok_lane, lane_res = bcall(backend.get_provider_lane, pid)
    lane = lane_res if ok_lane and lane_res in ("byo", "cloud") else "byo"
    ok_auto, auto_res = bcall(backend.auto_generation_available)
    auto = (auto_res if ok_auto and isinstance(auto_res, dict)
            else {"ok": False, "key_source": "none",
                  "reason": "status unavailable", "label": ""})
    if not auto.get("ok"):
        # Keep the technical reason out of the UI; users just need the gist.
        auto = {**auto, "reason": "coming soon. DIY Clips works right now"}
    ok_ro, ro = bcall(backend.get_render_opts, pid)
    render_opts = ro if ok_ro and isinstance(ro, dict) else {
        "aspect_ratio": "16:9", "resolution": "1080p"}
    ok_ar, ar_list = bcall(backend.aspect_ratios)
    aspects = ar_list if ok_ar and isinstance(ar_list, list) else ["16:9"]
    ok_rs, rs_list = bcall(backend.resolutions)
    resolutions = rs_list if ok_rs and isinstance(rs_list, list) else ["1080p"]
    return templates.TemplateResponse(request, "project.html", ctx(request,
        error=err,
        nav_active="create",
        project={"id": pid, "name": _g(p, "name", default=pid)},
        stages=stages,
        active_stage=active,
        lane=lane,
        cloud=auto,  # lane picker reads cloud.ok / cloud.reason / cloud.key_source
        key_source=auto.get("key_source", "none"),
        cast=cast_view(pid, p),
        backgrounds=backgrounds_view(pid, p),
        song=song_view(p),
        bible=bible_text(p) if active == "direct" else None,
        scenes=scenes,
        scenes_with_takes=takes_view(pid, p) if active == "review" else [],
        master=master_view(pid, p) if active == "export" else None,
        ready_to_assemble=ready_to_assemble(p),
        assemble_status=status if active == "export" else None,
        notice=status if active != "export" else None,
        aspect_ratio=render_opts.get("aspect_ratio", "16:9"),
        resolution=render_opts.get("resolution", "1080p"),
        aspect_ratios=aspects,
        resolutions=resolutions,
    ))


@app.post("/project/{pid}/ingest")
async def ingest(pid: str, song: UploadFile = File(...), assent: Optional[str] = Form(None)):
    if assent != "yes":
        return RedirectResponse(
            f"/project/{pid}?stage=ingest&err={_q('Tick the rights box first: we can’t touch your song without it.')}",
            status_code=303)
    ext = Path(song.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        ext_label = ext or "no extension"
        return RedirectResponse(
            f"/project/{pid}?stage=ingest&err={_q('Audio format not accepted (got ' + ext_label + '). Try MP3, WAV, M4A, AAC, OGG or FLAC.')}",
            status_code=303)
    data = await song.read()
    if len(data) > MAX_UPLOAD_BYTES:
        return RedirectResponse(
            f"/project/{pid}?stage=ingest&err={_q('That file is over 250 MB. Shrink it and try again.')}", status_code=303)
    ok, res = bcall(backend.ingest_song, pid, data, song.filename or "song",
                    RIGHTS_VERSION, True)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=ingest&err={_q(res)}", status_code=303)
    return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)


@app.post("/project/{pid}/direct")
def direct(pid: str, treatment: str = Form("")):
    steps = [
        ("transcribe", lambda: backend.transcribe(pid)),
        ("plan_cuts", lambda: backend.plan_cuts(pid)),
        ("direct", lambda: backend.direct(pid, treatment or "")),
        ("slice_scenes", lambda: backend.slice_scenes(pid)),
    ]
    for label, fn in steps:
        ok, res = bcall(fn)
        if not ok:
            return RedirectResponse(
                f"/project/{pid}?stage=direct&err={_q(f'{label} failed: {res}')}",
                status_code=303)
    return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)


@app.post("/project/{pid}/quote")
def quote(pid: str, scene_id: str = Form(...)):
    ok, res = bcall(backend.quote_scene, pid, scene_id)
    if not ok or not isinstance(res, dict):
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q(res if not ok else 'quote_scene returned no quote')}",
            status_code=303)
    q = pending_quotes()
    q.setdefault(pid, {})[str(scene_id)] = res
    save_pending_quotes(q)
    return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)


@app.post("/project/{pid}/approve")
def approve(pid: str, quote_id: str = Form(...), scene_id: str = Form(...)):
    q = pending_quotes().get(pid, {}).get(str(scene_id))
    if not q or str(_g(q, "quote_id", "id", default="")) != str(quote_id):
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q('Quote expired or missing. Please re-quote this scene.')}",
            status_code=303)
    ok, res = bcall(backend.approve_quote, pid, quote_id)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    key = uuid.uuid4().hex
    ok, res = bcall(backend.generate_scene, pid, scene_id, key)
    if not ok:
        return RedirectResponse(
            f"/project/{pid}?stage=review&err={_q(f'Quote approved, but generation submit failed: {res}. No take created; you can retry without double-spend (idempotency key {key[:8]}…).')}",
            status_code=303)
    qq = pending_quotes()
    qq.get(pid, {}).pop(str(scene_id), None)
    save_pending_quotes(qq)
    return RedirectResponse(f"/project/{pid}?stage=review", status_code=303)


@app.post("/project/{pid}/select")
def select(pid: str, scene_id: str = Form(...), take_id: str = Form(...)):
    ok, res = bcall(backend.select_take, pid, scene_id, take_id)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=review&err={_q(res)}", status_code=303)
    return RedirectResponse(f"/project/{pid}?stage=review", status_code=303)


@app.post("/project/{pid}/reroll")
def reroll(pid: str, scene_id: str = Form(...)):
    key = uuid.uuid4().hex
    ok, res = bcall(backend.reroll_scene, pid, scene_id, key)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=review&err={_q(res)}", status_code=303)
    return RedirectResponse(f"/project/{pid}?stage=review", status_code=303)


@app.post("/project/{pid}/assemble")
def assemble(pid: str):
    ok, res = bcall(backend.assemble, pid)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=export&err={_q(res)}", status_code=303)
    status = _g(res, "status", "progress", default="Assembly submitted.") if isinstance(res, dict) else str(res)
    return RedirectResponse(f"/project/{pid}?stage=export&status={_q(str(status))}", status_code=303)


@app.post("/project/{pid}/lane")
def set_lane(pid: str, lane: str = Form(...)):
    ok, res = bcall(backend.set_provider_lane, pid, lane)
    if not ok:
        return RedirectResponse(f"/project/{pid}?err={_q(res)}", status_code=303)
    return RedirectResponse(f"/project/{pid}", status_code=303)


# ------------------------------------------------- BYO generation (free)
# Bring-your-own generation: testers with their own working video-gen
# account download a job pack (prompts + timing), generate clips on their
# own Muse, and upload the clips back. Zero credits change hands.

MAX_CLIPZIP_BYTES = 500 * 1024 * 1024


@app.get("/project/{pid}/jobs/export")
def jobs_export(pid: str):
    ok, res = bcall(backend.export_job_pack, pid)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    path = Path(str(res))
    if not path.is_file():
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q('Job pack build reported no file.')}",
            status_code=303)
    return FileResponse(str(path), media_type="application/zip",
                        filename=f"wave-cinema-jobpack-{pid}.zip")


@app.post("/project/{pid}/jobs/import")
async def jobs_import(pid: str, clips: UploadFile = File(...)):
    ext = Path(clips.filename or "").suffix.lower()
    if ext != ".zip":
        return RedirectResponse(
            f"/project/{pid}?stage=review&err={_q('Please upload a .zip of scene_XX.mp4 clips.')}",
            status_code=303)
    data = await clips.read()
    if len(data) > MAX_CLIPZIP_BYTES:
        return RedirectResponse(
            f"/project/{pid}?stage=review&err={_q('Clip zip exceeds 500 MB.')}",
            status_code=303)
    ok, res = bcall(backend.import_job_clips, pid, data)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=review&err={_q(res)}", status_code=303)
    n_in = len(res.get("imported", [])) if isinstance(res, dict) else 0
    n_skip = len(res.get("skipped", [])) if isinstance(res, dict) else 0
    if n_in == 0:
        msg = ("No clips found in that zip. It needs files named scene_00.mp4, "
               "scene_01.mp4 and so on. If you uploaded the clip pack, that is the "
               "wrong file: generate the clips first, then zip those and upload them here.")
    else:
        msg = f"{n_in} clip(s) ready to review, {n_skip} file(s) skipped."
    return RedirectResponse(f"/project/{pid}?stage=review&status={_q(msg)}", status_code=303)


# ------------------------------------------------- Cast (character references)

MAX_CHAR_IMAGE_BYTES = 10 * 1024 * 1024
CHAR_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@app.post("/project/{pid}/characters")
async def character_add(pid: str, name: str = Form(...),
                        image: UploadFile = File(...)):
    ext = Path(image.filename or "").suffix.lower()
    if ext not in CHAR_ALLOWED_EXTS:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q('Reference image must be JPG or PNG.')}",
            status_code=303)
    data = await image.read()
    if len(data) > MAX_CHAR_IMAGE_BYTES:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q('That image is over 10 MB. Shrink it and try again.')}",
            status_code=303)
    ok, res = bcall(backend.add_character, pid, name, data,
                    image.filename or "character.png")
    if not ok:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/project/{pid}?stage=direct&status={_q('Character added to the cast.')}",
        status_code=303)


@app.post("/project/{pid}/characters/{cid}/delete")
def character_delete(pid: str, cid: str):
    ok, res = bcall(backend.remove_character, pid, cid)
    if not ok:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)


@app.get("/project/{pid}/characters/{cid}/image")
def character_image(pid: str, cid: str):
    ok, res = bcall(backend.character_image_path, pid, cid)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)
    path = Path(str(res))
    # containment: only serve files from inside this project's cast dir
    try:
        from wavecinema.workspace import project_dir as _pdir
        cast_dir = _pdir(pid).resolve() / "cast"
        if cast_dir not in path.resolve().parents and path.resolve() != cast_dir:
            raise ValueError("path escapes cast dir")
    except Exception:
        return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)
    if not path.is_file():
        return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png", ".webp": "image/webp"}.get(
                 path.suffix.lower(), "image/jpeg")
    return FileResponse(str(path), media_type=media)


# ------------------------------------------------- Backgrounds (location references)

MAX_BG_IMAGE_BYTES = 10 * 1024 * 1024
BG_ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


@app.post("/project/{pid}/backgrounds")
async def background_add(request: Request, pid: str, name: str = Form(...),
                         image: UploadFile = File(...)):
    require_user(request)
    ext = Path(image.filename or "").suffix.lower()
    if ext not in BG_ALLOWED_EXTS:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q('Reference image must be JPG, PNG or WEBP.')}",
            status_code=303)
    data = await image.read()
    if len(data) > MAX_BG_IMAGE_BYTES:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q('That image is over 10 MB. Shrink it and try again.')}",
            status_code=303)
    ok, res = bcall(backend.add_background, pid, name, data,
                    image.filename or "background.png")
    if not ok:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/project/{pid}?stage=direct&status={_q('Background added.')}",
        status_code=303)


@app.post("/project/{pid}/backgrounds/{bid}/delete")
def background_delete(request: Request, pid: str, bid: str):
    require_user(request)
    ok, res = bcall(backend.remove_background, pid, bid)
    if not ok:
        return RedirectResponse(
            f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)


@app.get("/project/{pid}/backgrounds/{bid}/image")
def background_image(request: Request, pid: str, bid: str):
    require_user(request)
    ok, res = bcall(backend.background_image_path, pid, bid)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)
    path = Path(str(res))
    try:
        from wavecinema.workspace import project_dir as _pdir
        bg_dir = _pdir(pid).resolve() / "backgrounds"
        if bg_dir not in path.resolve().parents and path.resolve() != bg_dir:
            raise ValueError("path escapes backgrounds dir")
    except Exception:
        return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)
    if not path.is_file():
        return RedirectResponse(f"/project/{pid}?stage=direct", status_code=303)
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".png": "image/png", ".webp": "image/webp"}.get(
                 path.suffix.lower(), "image/jpeg")
    return FileResponse(str(path), media_type=media)


# ------------------------------------------------- Character sheet + background generator (Nano Banana)

ART_STYLE_PRESETS = ["anime", "photorealistic", "3d", "comic", "pixel"]
ART_STYLE_LABELS = {
    "anime": "Anime", "photorealistic": "Photorealistic", "3d": "3D render",
    "comic": "Comic book", "pixel": "Pixel art",
}


@app.get("/project/{pid}/character-sheet", response_class=HTMLResponse)
def character_sheet_page(request: Request, pid: str,
                         err: Optional[str] = Query(None),
                         status: Optional[str] = Query(None),
                         tab: str = Query("character")):
    user = require_user(request)
    ok, p = project_of(pid)
    if not ok:
        return RedirectResponse("/", status_code=303)
    key_ok, ks = bcall(backend.gemini_status, user["id"])
    gemini = ks if key_ok and isinstance(ks, dict) else {"set": False}
    ok2, chars = bcall(backend.list_characters, pid)
    recent = chars[-4:] if ok2 and isinstance(chars, list) else []
    return templates.TemplateResponse(request, "character_sheet.html", ctx(request,
        error=err, notice=status,
        project={"id": pid, "name": _g(p, "name", default=pid)},
        gemini=gemini,
        tab=tab if tab in ("character", "background") else "character",
        styles=ART_STYLE_LABELS,
        recent=recent,
    ))


@app.post("/project/{pid}/character-sheet")
def character_sheet_generate(request: Request, pid: str,
                             name: str = Form(...),
                             description: str = Form(...),
                             style: str = Form("anime")):
    user = require_user(request)
    if style not in ART_STYLE_PRESETS:
        style = "anime"
    ok, res = bcall(backend.generate_character_sheet, pid,
                    name, description, style, user["id"])
    if not ok:
        return RedirectResponse(
            f"/project/{pid}/character-sheet?err={_q(res)}", status_code=303)
    cname = res.get("name", name) if isinstance(res, dict) else name
    return RedirectResponse(
        f"/project/{pid}/character-sheet?status={_q(f'Character sheet for {cname} generated and added to the cast.')}&tab=character",
        status_code=303)


@app.post("/project/{pid}/character-sheet/background")
def background_generate(request: Request, pid: str,
                        name: str = Form(...),
                        description: str = Form(...)):
    user = require_user(request)
    ok, res = bcall(backend.generate_background_image, pid,
                    name, description, user["id"])
    if not ok:
        return RedirectResponse(
            f"/project/{pid}/character-sheet?tab=background&err={_q(res)}",
            status_code=303)
    bname = res.get("name", name) if isinstance(res, dict) else name
    return RedirectResponse(
        f"/project/{pid}/character-sheet?tab=background&status={_q(f'Background {bname} generated and saved.')}",
        status_code=303)


# ------------------------------------------------- Settings (BYOK)

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request,
                  err: Optional[str] = Query(None),
                  status: Optional[str] = Query(None)):
    user = require_user(request)
    ok, st = bcall(backend.byok_status, user["id"])
    byok = st if ok and isinstance(st, dict) else {"set": False, "masked": None}
    ok_g, gst = bcall(backend.gemini_status, user["id"])
    gemini = gst if ok_g and isinstance(gst, dict) else {"set": False, "masked": None, "source": None}
    ok_auto, auto_res = bcall(backend.auto_generation_available)
    auto = (auto_res if ok_auto and isinstance(auto_res, dict)
            else {"ok": False, "key_source": "none", "label": ""})
    return templates.TemplateResponse(request, "settings.html", ctx(request,
        error=err,
        notice=status,
        byok=byok,
        gemini=gemini,
        auto=auto,
    ))


@app.post("/settings/byok")
def settings_byok_set(request: Request, api_key: str = Form(...)):
    user = require_user(request)
    ok, res = bcall(backend.byok_set_key, api_key, user["id"])
    if not ok:
        return RedirectResponse(f"/settings?err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/settings?status={_q('Your key is saved (encrypted). Automatic generation will use your Higgsfield account — no credits.')}",
        status_code=303)


@app.post("/settings/byok/delete")
def settings_byok_delete(request: Request):
    user = require_user(request)
    ok, res = bcall(backend.byok_clear_key, user["id"])
    if not ok:
        return RedirectResponse(f"/settings?err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/settings?status={_q('Your key was removed.')}", status_code=303)


# ------------------------------------------------- Settings (Gemini / Nano Banana)

@app.post("/settings/gemini")
def settings_gemini_set(request: Request, api_key: str = Form(...)):
    user = require_user(request)
    ok, res = bcall(backend.gemini_set_key, api_key, user["id"])
    if not ok:
        return RedirectResponse(f"/settings?err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/settings?status={_q('Gemini key saved (encrypted). Character sheet and background generation are now enabled.')}",
        status_code=303)


@app.post("/settings/gemini/delete")
def settings_gemini_delete(request: Request):
    user = require_user(request)
    ok, res = bcall(backend.gemini_clear_key, user["id"])
    if not ok:
        return RedirectResponse(f"/settings?err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/settings?status={_q('Gemini key removed.')}",
        status_code=303)


# ------------------------------------------------------------------ studio
# Studio mode: power-user timeline view of a project. Same data as the
# guided flow, presented as an editable scene-by-scene timeline.

def studio_scene_views(pid: str, p: dict) -> list:
    """Scene views enriched with takes for the Studio timeline."""
    views = scene_views(p)
    for v in views:
        sid = v["id"]
        ok, takes = bcall(backend.list_takes, pid, sid)
        cards = []
        if ok and isinstance(takes, list):
            sel_id = ""
            raw = _g(p, "scenes", default=[]) or []
            for s in raw:
                if str(_g(s, "scene_id", "id", default="")) == sid:
                    sel_id = str(_g(s, "selected_take_id", "selected_take", default="") or "")
                    break
            for t in takes:
                tid = str(_g(t, "take_id", "id", default=""))
                cards.append({
                    "take_id": tid,
                    "video_url": media_url(_g(t, "video_url", "url", "path", "file"), pid),
                    "sha_short": (str(_g(t, "sha256", "sha", "hash", default="") or "")[:12] or "-"),
                    "selected": bool(sel_id and tid == sel_id) or bool(_g(t, "selected")),
                })
        v["takes"] = cards
        # Download URL: selected take, or first take.
        dl = None
        for c in cards:
            if c["selected"]:
                dl = c["video_url"]
                break
        v["download_url"] = dl or (cards[0]["video_url"] if cards else None)
        v["status"] = "has takes" if cards else ("planned" if v["prompt"] else "no plan")
    return views


@app.get("/studio", response_class=HTMLResponse)
def studio_index(request: Request):
    user = require_user(request)
    projects = []
    for item in registry_list(user["id"]):
        pid = item.get("id", "")
        name = item.get("name", pid)
        ok, p, _ = project_of(pid)
        if not ok or not p:
            continue
        scenes = _g(p, "scenes", default=[]) or []
        projects.append({
            "id": pid, "name": name,
            "stage_label": detect_stage(p),
            "scene_count": len(scenes) if isinstance(scenes, list) else 0,
        })
    return templates.TemplateResponse(request, "studio.html",
        ctx(request, projects=projects, nav_active="studio"))


@app.get("/studio/{pid}", response_class=HTMLResponse)
def studio_project(request: Request, pid: str,
                   err: Optional[str] = Query(None),
                   status: Optional[str] = Query(None)):
    user = require_user(request)
    ok, p, perr = project_of(pid)
    if not ok:
        return templates.TemplateResponse(request, "studio.html",
            ctx(request, error=perr or "Project not found", projects=[],
                nav_active="studio"), status_code=404)
    # Ownership check (same as project_page).
    owner_id = p.get("owner_id") if isinstance(p, dict) else None
    if owner_id is None:
        for item in registry_list():
            if item.get("id") == pid:
                owner_id = item.get("owner_id")
                break
    if owner_id and owner_id != user["id"]:
        return RedirectResponse("/studio?err=Project+not+found", status_code=303)
    scenes = studio_scene_views(pid, p)
    return templates.TemplateResponse(request, "studio_project.html", ctx(
        request, error=err, notice=status, nav_active="studio",
        project={"id": pid, "name": _g(p, "name", default=pid)},
        scenes=scenes,
    ))


@app.post("/studio/{pid}/scene/{sid}/prompt")
def studio_save_prompt(request: Request, pid: str, sid: str,
                       prompt: str = Form(...)):
    require_user(request)
    ok, res = bcall(backend.update_scene_prompt, pid, sid, prompt)
    if not ok:
        return RedirectResponse(
            f"/studio/{pid}?err={_q(res)}#scene-{sid}", status_code=303)
    return RedirectResponse(
        f"/studio/{pid}?status={_q('Prompt saved.')}#scene-{sid}",
        status_code=303)


# ------------------------------------------------- Producer (AI directing partner)

@app.get("/project/{pid}/producer", response_class=HTMLResponse)
def producer_page(request: Request, pid: str,
                  err: Optional[str] = Query(None)):
    user = require_user(request)
    ok, p, perr = project_of(pid)
    if not ok:
        return RedirectResponse(f"/?err={_q(perr or 'Project not found')}",
                                status_code=303)
    owner_err = _check_owner(pid, p, user)
    if owner_err:
        return RedirectResponse("/?err=Project+not+found", status_code=303)
    ok_h, history = bcall(backend.producer_history, pid)
    return templates.TemplateResponse(request, "producer.html", ctx(
        request, error=err, nav_active="create",
        project={"id": pid, "name": _g(p, "name", default=pid)},
        history=history if ok_h and isinstance(history, list) else [],
    ))


@app.post("/project/{pid}/producer/chat")
async def producer_chat_api(request: Request, pid: str):
    user = require_user(request)
    ok, p, _ = project_of(pid)
    if not ok or _check_owner(pid, p or {}, user):
        return JSONResponse({"error": "Project not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Send JSON {message}"}, status_code=400)
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Message is empty"}, status_code=400)
    ok_c, res = bcall(backend.producer_chat, pid, message)
    if not ok_c:
        return JSONResponse({"error": str(res)[:500]}, status_code=500)
    return JSONResponse({"reply": res.get("reply", ""),
                         "source": res.get("source", "")})


# ------------------------------------------------- Captions (word timing + SRT)

@app.get("/project/{pid}/captions", response_class=HTMLResponse)
def captions_page(request: Request, pid: str,
                  err: Optional[str] = Query(None)):
    user = require_user(request)
    ok, p, perr = project_of(pid)
    if not ok:
        return RedirectResponse(f"/?err={_q(perr or 'Project not found')}",
                                status_code=303)
    owner_err = _check_owner(pid, p, user)
    if owner_err:
        return RedirectResponse("/?err=Project+not+found", status_code=303)
    ok_c, caps = bcall(backend.captions, pid)
    if not ok_c:
        return templates.TemplateResponse(request, "captions.html", ctx(
            request, error=str(caps)[:500], nav_active="create",
            project={"id": pid, "name": _g(p, "name", default=pid)},
            captions=None,
        ))
    return templates.TemplateResponse(request, "captions.html", ctx(
        request, error=err, nav_active="create",
        project={"id": pid, "name": _g(p, "name", default=pid)},
        captions=caps,
    ))


@app.get("/project/{pid}/captions.srt")
def captions_srt_download(request: Request, pid: str):
    user = require_user(request)
    ok, p, _ = project_of(pid)
    if not ok or _check_owner(pid, p or {}, user):
        return HTMLResponse("not found", status_code=404)
    ok_c, srt = bcall(backend.captions_srt, pid)
    if not ok_c:
        return HTMLResponse(str(srt)[:500], status_code=400)
    return HTMLResponse(str(srt), media_type="text/plain",
                        headers={"Content-Disposition":
                                 f"attachment; filename=captions-{pid}.srt"})


# ------------------------------------------------- Remix (video-to-video)

MAX_REMIX_UPLOAD_BYTES = 100 * 1024 * 1024
REMIX_EXTS = {".mp4", ".mov"}


def _remix_view(pid: str, r: dict) -> dict:
    return {
        "remix_id": r.get("remix_id", ""),
        "style_prompt": r.get("style_prompt", ""),
        "status": r.get("status", ""),
        "error": r.get("error"),
        "source_url": media_url(r.get("source_path"), pid),
        "result_url": media_url(r.get("result_path"), pid),
        "created_at": str(r.get("created_at", ""))[:16].replace("T", " "),
    }


@app.get("/project/{pid}/remix", response_class=HTMLResponse)
def remix_page(request: Request, pid: str,
               err: Optional[str] = Query(None),
               status: Optional[str] = Query(None)):
    user = require_user(request)
    ok, p, perr = project_of(pid)
    if not ok:
        return RedirectResponse(f"/?err={_q(perr or 'Project not found')}",
                                status_code=303)
    owner_err = _check_owner(pid, p, user)
    if owner_err:
        return RedirectResponse("/?err=Project+not+found", status_code=303)
    ok_r, remixes = bcall(backend.remix_list, pid)
    views = [_remix_view(pid, r) for r in remixes] if ok_r and isinstance(remixes, list) else []
    return templates.TemplateResponse(request, "remix.html", ctx(
        request, error=err, notice=status, nav_active="create",
        project={"id": pid, "name": _g(p, "name", default=pid)},
        remixes=views,
    ))


@app.post("/project/{pid}/remix")
async def remix_submit(request: Request, pid: str,
                       video: UploadFile = File(...),
                       style_prompt: str = Form("")):
    user = require_user(request)
    ok, p, _ = project_of(pid)
    if not ok or _check_owner(pid, p or {}, user):
        return RedirectResponse("/?err=Project+not+found", status_code=303)
    ext = Path(video.filename or "").suffix.lower()
    if ext not in REMIX_EXTS:
        return RedirectResponse(
            f"/project/{pid}/remix?err={_q('Remix source must be MP4 or MOV.')}",
            status_code=303)
    data = await video.read()
    if len(data) > MAX_REMIX_UPLOAD_BYTES:
        return RedirectResponse(
            f"/project/{pid}/remix?err={_q('That video is over 100 MB.')}",
            status_code=303)
    ok_s, res = bcall(backend.remix_submit, pid, data,
                      video.filename or "clip.mp4", style_prompt)
    if not ok_s:
        return RedirectResponse(
            f"/project/{pid}/remix?err={_q(res)}", status_code=303)
    rid = res.get("remix_id", "") if isinstance(res, dict) else ""
    # Run immediately on the project's lane (mock/byok/cloud).
    # BYO lane: stays queued for the DIY job pack.
    lane = (res.get("lane") if isinstance(res, dict) else "byo") or "byo"
    if lane == "byo":
        return RedirectResponse(
            f"/project/{pid}/remix?status={_q('Remix queued — it is in your DIY job pack. Generate it on your own account and upload the result.')}",
            status_code=303)
    ok_r, run_res = bcall(backend.remix_run, pid, rid)
    if not ok_r:
        return RedirectResponse(
            f"/project/{pid}/remix?err={_q(f'Remix queued but generation failed: {run_res}')}",
            status_code=303)
    return RedirectResponse(
        f"/project/{pid}/remix?status={_q('Remix complete — compare it with the original below.')}",
        status_code=303)


@app.post("/project/{pid}/remix/{rid}/run")
def remix_run(request: Request, pid: str, rid: str):
    require_user(request)
    ok, res = bcall(backend.remix_run, pid, rid)
    if not ok:
        return RedirectResponse(
            f"/project/{pid}/remix?err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/project/{pid}/remix?status={_q('Remix complete.')}", status_code=303)


# ------------------------------------------------- Render options (aspect + resolution)

@app.post("/project/{pid}/aspect")
def set_aspect(request: Request, pid: str, aspect: str = Form(...)):
    require_user(request)
    ok, res = bcall(backend.set_aspect_ratio, pid, aspect)
    if not ok:
        return RedirectResponse(f"/project/{pid}?err={_q(res)}", status_code=303)
    return RedirectResponse(
        f"/project/{pid}?status={_q(f'Aspect ratio set to {aspect}. New generations will use it.')}",
        status_code=303)


@app.post("/project/{pid}/resolution")
def set_resolution(request: Request, pid: str, resolution: str = Form(...)):
    require_user(request)
    ok, res = bcall(backend.set_resolution, pid, resolution)
    if not ok:
        return RedirectResponse(f"/project/{pid}?err={_q(res)}", status_code=303)
    note = ("2K costs 1.5x the 1080p credit rate." if resolution == "2K"
            else "Back to the standard 1080p rate.")
    return RedirectResponse(
        f"/project/{pid}?status={_q(f'Resolution set to {resolution}. {note}')}",
        status_code=303)


# ------------------------------------------------------------------ tools (public)

@app.get("/tools", response_class=HTMLResponse)
def tools_page(request: Request):
    return templates.TemplateResponse(request, "tools.html",
        ctx(request, nav_active="tools"))


# ------------------------------------------------------------------ wiki (public)

@app.get("/wiki", response_class=HTMLResponse)
def wiki_page(request: Request):
    return templates.TemplateResponse(request, "wiki.html",
        ctx(request, nav_active="wiki"))


# ------------------------------------------------------------------ account

def _fmt_ledger_history(entries: list) -> list:
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        ts = str(e.get("created_at", ""))[:16].replace("T", " ")
        out.append({
            "when": ts or "—",
            "kind": str(e.get("kind", "?")),
            "memo": str(e.get("memo", "") or ""),
            "credits": e.get("credits", 0),
        })
    return out


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = require_user(request)
    ok, hist = bcall(backend.ledger_history,
                     authmod.ledger_account_id(user), 50)
    history = _fmt_ledger_history(hist) if ok and isinstance(hist, list) else []
    import tokens as tokensmod
    token_list = tokensmod.list_tokens(user["id"])
    # Format the member-since timestamp.
    if user.get("created_at"):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(int(user["created_at"]), tz=timezone.utc)
            user = {**user, "created_at": dt.strftime("%B %Y")}
        except Exception:
            user = {**user, "created_at": ""}
    return templates.TemplateResponse(request, "account.html",
        ctx(request, nav_active="account", history=history, tokens=token_list))


@app.post("/account/tokens")
def account_token_create(request: Request, name: str = Form("")):
    user = require_user(request)
    import tokens as tokensmod
    public, raw = tokensmod.create_token(user["id"], name)
    # Re-render the account page with the raw token shown once.
    ok, hist = bcall(backend.ledger_history,
                     authmod.ledger_account_id(user), 50)
    history = _fmt_ledger_history(hist) if ok and isinstance(hist, list) else []
    return templates.TemplateResponse(request, "account.html",
        ctx(request, nav_active="account", history=history,
            tokens=tokensmod.list_tokens(user["id"]),
            new_token=raw, new_token_name=public["name"]))


@app.post("/account/tokens/{tid}/revoke")
def account_token_revoke(request: Request, tid: str):
    user = require_user(request)
    import tokens as tokensmod
    tokensmod.revoke_token(user["id"], tid)
    return RedirectResponse("/account", status_code=303)


# ------------------------------------------------------------------ paywall
# paywall.py is built in parallel; include its router only if present AND valid.
# Broadened beyond ImportError so a mid-build paywall.py (e.g. SyntaxError)
# can never take down the studio UI.
try:
    from paywall import router as paywall_router  # type: ignore
    app.include_router(paywall_router)
except Exception as _paywall_err:  # noqa: BLE001
    app.state.paywall_error = str(_paywall_err)


# ------------------------------------------------------------------ API v1 + MCP (Phase 4)
# Same defensive pattern: a broken API module must never take down the UI.
try:
    from api_v1 import router as api_v1_router  # type: ignore
    app.include_router(api_v1_router)
except Exception as _api_err:  # noqa: BLE001
    app.state.api_error = str(_api_err)

try:
    from mcp_server import router as mcp_router  # type: ignore
    app.include_router(mcp_router)
except Exception as _mcp_err:  # noqa: BLE001
    app.state.mcp_error = str(_mcp_err)


@app.get("/api/docs", response_class=HTMLResponse)
def api_docs_page(request: Request):
    return templates.TemplateResponse(request, "api_docs.html",
        ctx(request, nav_active="account"))
