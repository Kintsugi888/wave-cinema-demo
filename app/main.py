"""Wave Cinema web UI — FastAPI app (Midnight Chrome v1.0).

Serves the AI music-video workstation: dashboard, project pipeline
(01 Ingest → 02 Direct → 03 Review → 04 Export), paywall hook.
Backend calls go through backend_client (safe, degrading).
"""

import base64
import json
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, File, UploadFile, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend_client import Backend, bcall

# Bundled ffmpeg on PATH (free hosts ship no system ffmpeg/ffprobe).
from wavecinema import ffmpeg_bin  # noqa: F401  (import-time PATH fix)

APP_DIR = Path(__file__).resolve().parent
WC_ROOT = (APP_DIR / "..").resolve()
DATA_DIR = APP_DIR / "data"
REGISTRY = DATA_DIR / "projects.json"
QUOTES = DATA_DIR / "quotes.json"
LOGO = (WC_ROOT / "brand" / "logo.jpg").resolve()

RIGHTS_VERSION = "wc-rights-v1.0 (2026-10-14)"
RIGHTS_TEXT = """WAVE CINEMA — RIGHTS STATEMENT (version wc-rights-v1.0, 2026-10-14)

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
    {"key": "ingest", "num": "01", "label": "Ingest"},
    {"key": "direct", "num": "02", "label": "Direct"},
    {"key": "review", "num": "03", "label": "Review"},
    {"key": "export", "num": "04", "label": "Export"},
]
STAGE_KEYS = [s["key"] for s in STAGES]


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
        return "—"
    m, rem = divmod(ms, 60000)
    s, mm = divmod(rem, 1000)
    return f"{m:02d}:{s:02d}.{mm:03d}"


def fmt_duration(sec: Any) -> str:
    try:
        total_ms = int(float(sec) * 1000)
    except (TypeError, ValueError):
        return "—"
    return ms_to_ts(total_ms)


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2))


def registry_list() -> list:
    items = _load_json(REGISTRY, [])
    return items if isinstance(items, list) else []


def registry_add(pid: str, name: str) -> None:
    items = [i for i in registry_list() if i.get("id") != pid]
    items.append({"id": pid, "name": name})
    _save_json(REGISTRY, items)


def pending_quotes() -> dict:
    q = _load_json(QUOTES, {})
    return q if isinstance(q, dict) else {}


def save_pending_quotes(q: dict) -> None:
    _save_json(QUOTES, q)


def get_credits() -> Optional[int]:
    ok, res = bcall(backend.credit_balance, account_id="owner")
    if not ok:
        return None
    if isinstance(res, dict):
        res = _g(res, "balance", "credits", "total", default=None)
    try:
        return int(res)
    except (TypeError, ValueError):
        return None


def ctx(error: Optional[str] = None, **kw) -> dict:
    credits = get_credits()
    base = {
        "backend_ok": backend.ok,
        "credits": f"{credits:,}" if credits is not None else None,
        "error": error,
        "rights_version": RIGHTS_VERSION,
        "rights_text": RIGHTS_TEXT,
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
        "sha256": _g(s, "sha256", "sha", "hash", default="—"),
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
    first_ok = scene0_selected(p, raw if isinstance(raw, list) else [])
    views = []
    quotes = pending_quotes().get(p.get("id", ""), {})
    for i, s in enumerate(raw):
        sid = str(_g(s, "scene_id", "id", "index", default=i))
        cast = _g(s, "cast", "characters", default="—")
        if isinstance(cast, list):
            cast = ", ".join(str(c) for c in cast)
        prompt = str(_g(s, "prompt", "scene_prompt", default=""))
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
    total = _g(q, "total", "credits", "amount", default="—")
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
                "sha_short": (sha[:12] + "…") if len(sha) > 12 else (sha or "—"),
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
        "bytes": _g(m, "bytes", "size", default="—"),
        "sha256": _g(m, "sha256", "sha", "hash", default="—"),
    }


def ready_to_assemble(p: dict) -> bool:
    raw = _g(p, "scenes", default=[]) or []
    if not raw:
        return False
    for s in raw:
        if not _g(s, "selected_take_id", "selected_take"):
            sel = _g(p, "selections", "selected_takes", default={}) or {}
            if isinstance(sel, dict) and sel:
                return True  # selections map present — trust backend shape
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
    projects = []
    for item in registry_list():
        pid = item.get("id", "")
        name = item.get("name", pid)
        ok, p, _ = project_of(pid)
        stage_label = detect_stage(p) if ok and p else "unknown"
        projects.append({"id": pid, "name": name, "stage_label": stage_label})
    return templates.TemplateResponse(request, "dashboard.html", ctx(error=request_error, projects=projects))


@app.post("/projects")
def create_project(name: str = Form(...)):
    name = (name or "").strip()[:120] or "Untitled film"
    ok, res = bcall(backend.create_project, name)
    if not ok:
        return RedirectResponse(f"/?err={_q(res)}", status_code=303)
    pid = res if isinstance(res, str) else _g(res, "project_id", "id", default="")
    if not pid:
        return RedirectResponse(f"/?err={_q('create_project returned no project id')}", status_code=303)
    registry_add(str(pid), name)
    return RedirectResponse(f"/project/{pid}", status_code=303)


def _q(msg: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(str(msg)[:500])


@app.get("/project/{pid}", response_class=HTMLResponse)
def project_page(request: Request,
                 pid: str,
                 stage: Optional[str] = Query(None),
                 err: Optional[str] = Query(None),
                 status: Optional[str] = Query(None)):
    ok, p, perr = project_of(pid)
    if not ok:
        return templates.TemplateResponse(
            request, "dashboard.html",
            ctx(error=perr or "Project not found", projects=[]),
            status_code=404)
    active = stage if stage in STAGE_KEYS else detect_stage(p)
    order = {k: i for i, k in enumerate(STAGE_KEYS)}
    stages = [{**s, "done": order[s["key"]] < order[active]} for s in STAGES]
    scenes = scene_views(p) if active in ("direct", "review") else []
    ok_lane, lane_res = bcall(backend.get_provider_lane, pid)
    lane = lane_res if ok_lane and lane_res in ("byo", "cloud") else "byo"
    ok_cloud, cloud_res = bcall(backend.cloud_available)
    cloud = (cloud_res if ok_cloud and isinstance(cloud_res, dict)
             else {"ok": False, "reason": "cloud status unavailable"})
    return templates.TemplateResponse(request, "project.html", ctx(
        error=err,
        project={"id": pid, "name": _g(p, "name", default=pid)},
        stages=stages,
        active_stage=active,
        lane=lane,
        cloud=cloud,
        song=song_view(p),
        bible=bible_text(p) if active == "direct" else None,
        scenes=scenes,
        scenes_with_takes=takes_view(pid, p) if active == "review" else [],
        master=master_view(pid, p) if active == "export" else None,
        ready_to_assemble=ready_to_assemble(p),
        assemble_status=status,
    ))


@app.post("/project/{pid}/ingest")
async def ingest(pid: str, song: UploadFile = File(...), assent: Optional[str] = Form(None)):
    if assent != "yes":
        return RedirectResponse(
            f"/project/{pid}?stage=ingest&err={_q('Upload refused: rights assent checkbox is required.')}",
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
            f"/project/{pid}?stage=ingest&err={_q('File exceeds 250 MB limit.')}", status_code=303)
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
                f"/project/{pid}?stage=direct&err={_q(f'{label} failed — {res}')}",
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
            f"/project/{pid}?stage=direct&err={_q('Quote expired or missing — please re-quote this scene.')}",
            status_code=303)
    ok, res = bcall(backend.approve_quote, pid, quote_id)
    if not ok:
        return RedirectResponse(f"/project/{pid}?stage=direct&err={_q(res)}", status_code=303)
    key = uuid.uuid4().hex
    ok, res = bcall(backend.generate_scene, pid, scene_id, key)
    if not ok:
        return RedirectResponse(
            f"/project/{pid}?stage=review&err={_q(f'Quote approved, but generation submit failed — {res}. No take created; you can retry without double-spend (idempotency key {key[:8]}…).')}",
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
    msg = f"BYO import: {n_in} takes registered, {n_skip} files skipped."
    return RedirectResponse(f"/project/{pid}?stage=review&status={_q(msg)}", status_code=303)


# ------------------------------------------------------------------ paywall
# paywall.py is built in parallel; include its router only if present AND valid.
# Broadened beyond ImportError so a mid-build paywall.py (e.g. SyntaxError)
# can never take down the studio UI.
try:
    from paywall import router as paywall_router  # type: ignore
    app.include_router(paywall_router)
except Exception as _paywall_err:  # noqa: BLE001
    app.state.paywall_error = str(_paywall_err)
