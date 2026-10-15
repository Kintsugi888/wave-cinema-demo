"""Wave Cinema backend public API surface.

Thin, UI-facing functions over the backend package. The web layer
(app/main.py via app/backend_client.py, and app/paywall.py) imports only
this module — never the internals directly.

Pipeline functions (create_project … assemble) are the exact contract the
web UI and paywall build against — do not rename them. ``grant_credits`` /
``get_balance`` keep the exact signatures the Stripe paywall's webhook
depends on (idempotent on memo, e.g. ``memo=f"stripe:{session_id}"``).

Money-moving behaviour:
  - quoting is free; approval creates a ledger HOLD (reservation)
  - successful generation CAPTUREs the hold into a DEBIT
  - failed generation RELEASEs the hold (refund)
  - every submission carries an idempotency key: retries never double-spend
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import assemble as _assemble
from . import cuts as _cuts
from . import direct as _direct
from . import export as _export
from . import ingest as _ingest
from . import jobpack as _jobpack
from . import ledger as _ledger
from . import pipeline as _pipeline
from . import providers as _providers
from . import quote as _quote
from . import slice as _slice
from . import takes as _takes
from . import transcribe as _transcribe
from . import workspace as _workspace
from .errors import (
    NoApprovedQuote,
    QuoteExpired,
    RightsRequired,
    SceneNotFound,
    WaveCinemaError,
)

DEFAULT_ACCOUNT = "owner"

# Generation lanes (per project). "byo" = the tester generates on their own
# account via the job-pack loop (zero cost to us). "cloud" = Wave Cinema
# Cloud: our Higgsfield key, debited from the credit balance WITH margin.
LANES = ("byo", "cloud")
CLOUD_MARGIN = 1.5  # pricing lever for $shamz: cloud quotes are base x 1.5

_provider = None


def set_provider(provider: "_providers.ProviderAdapter") -> None:
    """Swap the generation backend (tests, future real provider)."""
    global _provider
    _provider = provider


def get_provider() -> "_providers.ProviderAdapter":
    global _provider
    if _provider is None:
        _provider = _providers.MockProvider(
            _workspace.DATA_DIR / "mock_clips"
        )
    return _provider


def cloud_available() -> dict:
    """Whether the Wave Cinema Cloud lane can be offered right now."""
    return _providers.cloud_available()


def get_provider_lane(project_id: str) -> str:
    """This project's generation lane ('byo' default)."""
    ws = _workspace.ProjectWorkspace.load(project_id)
    lane = ws.state.get("provider_lane", "byo")
    return lane if lane in LANES else "byo"


def set_provider_lane(project_id: str, lane: str) -> dict:
    """Pick this project's generation lane. 'cloud' is refused unless the
    cloud provider is explicitly enabled AND the key is present."""
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r} (expected one of {LANES})")
    if lane == "cloud":
        avail = _providers.cloud_available()
        if not avail["ok"]:
            raise RuntimeError(
                f"cloud lane unavailable: {avail['reason']}")
    ws = _workspace.ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["provider_lane"] = lane
    ws.log_event(f"provider lane set to {lane!r}")
    ws.save(expected_revision=rev)
    return {"project_id": project_id, "provider_lane": lane}


def _provider_for_project(project_id: str) -> "_providers.ProviderAdapter":
    """Lane-aware generation backend.

    Honors an explicit override first: either set_provider() or a
    monkeypatched get_provider (the spec suite's hook) — then the
    project's lane.
    """
    global _provider
    if get_provider is not _ORIGINAL_GET_PROVIDER:
        return get_provider()  # monkeypatched (tests) — honor it
    if _provider is not None:
        return _provider
    if get_provider_lane(project_id) == "cloud":
        return _providers.HiggsfieldProvider(
            _workspace.DATA_DIR / "hf_clips")
    return _providers.MockProvider(
        _workspace.DATA_DIR / "mock_clips")


_ORIGINAL_GET_PROVIDER = get_provider


# ---------------------------------------------------------------- project

def create_project(name: str) -> str:
    """Create a project; returns its project_id."""
    return _workspace.create_project(name)


def get_project(project_id: str) -> dict:
    """Full workspace snapshot for a project."""
    return _workspace.ProjectWorkspace.load(project_id).snapshot()


# ---------------------------------------------------------------- ingest

def ingest_song(project_id: str, file_bytes: bytes, filename: str,
                rights_statement_version: str,
                rights_assented: bool) -> dict:
    """Record rights sign-off, then ingest the master song.

    Raises RightsRequired unless the human explicitly assented.
    """
    if not rights_assented:
        raise RightsRequired(
            "explicit assent to the rights statement is required before upload"
        )
    ws = _workspace.ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["rights"] = {
        "statement_version": rights_statement_version,
        "assented": True,
        "assented_at": datetime.now(timezone.utc).isoformat(),
    }
    ws.log_event(f"rights assented: statement {rights_statement_version}")
    ws.save(expected_revision=rev)
    return _ingest.ingest_song_bytes(project_id, file_bytes, filename)


# ---------------------------------------------------------------- plan (free)

def transcribe(project_id: str) -> dict:
    return _transcribe.transcribe_project(project_id)


def plan_cuts(project_id: str) -> list:
    return _cuts.plan_cuts(project_id)


def direct(project_id: str, treatment: str) -> dict:
    return _direct.direct_project(project_id, treatment)


def slice_scenes(project_id: str) -> list:
    return _slice.slice_scenes(project_id)


# ---------------------------------------------------------------- quote (free)

def quote_scene(project_id: str, scene_id: str) -> dict:
    return _quote.quote_scene(project_id, scene_id)


def approve_quote(project_id: str, quote_id: str) -> dict:
    """Approve a quote: creates a ledger hold for the exact quoted amount."""
    ws = _workspace.ProjectWorkspace.load(project_id)
    quote = ws.state["quotes"].get(quote_id)
    if quote is None:
        raise KeyError(f"no such quote {quote_id!r}")
    if quote["status"] == "approved":
        return dict(quote)  # idempotent re-approval
    if quote["status"] != "quoted":
        raise NoApprovedQuote(f"quote {quote_id} is {quote['status']!r}")
    if datetime.fromisoformat(quote["expires_at"]) <= datetime.now(timezone.utc):
        raise QuoteExpired(f"quote {quote_id} expired at {quote['expires_at']}")

    hold = _ledger.Ledger().hold(
        DEFAULT_ACCOUNT,
        quote["credits_total"],
        idempotency_key=f"quote:{quote_id}",
        memo=f"quote {quote_id} scene {quote['scene_id']} project {project_id}",
    )
    rev = ws.revision
    quote["status"] = "approved"
    quote["hold_id"] = hold["id"]
    quote["approved_at"] = datetime.now(timezone.utc).isoformat()
    ws.state["quotes"][quote_id] = quote
    ws.log_event(
        f"approved quote {quote_id}: hold {hold['id']} "
        f"({quote['credits_total']} credits)"
    )
    ws.save(expected_revision=rev)
    return dict(quote)


# ---------------------------------------------------------------- generate (paid)

def _resolve_quote(ws: "_workspace.ProjectWorkspace", scene_id: str) -> dict:
    """Newest approved, unexpired, unconsumed quote for a scene."""
    cands = [
        q for q in ws.state["quotes"].values()
        if q["scene_id"] == scene_id and q["status"] == "approved"
    ]
    if not cands:
        raise NoApprovedQuote(
            f"scene {scene_id}: no approved quote — quote and approve first"
        )
    cands.sort(key=lambda q: q["issued_at"], reverse=True)
    quote = cands[0]
    if datetime.fromisoformat(quote["expires_at"]) <= datetime.now(timezone.utc):
        raise QuoteExpired(f"quote {quote['quote_id']} expired")
    return quote


def _scene_with_prompt(ws: "_workspace.ProjectWorkspace", scene_id: str) -> dict:
    plan = ws.state.get("plan")
    if not plan:
        raise SceneNotFound("no timed plan — direct the project first")
    scene = next((s for s in plan["scenes"] if s["scene_id"] == scene_id), None)
    if scene is None:
        raise SceneNotFound(f"no scene {scene_id!r}")
    return scene


def generate_scene(project_id: str, scene_id: str,
                   idempotency_key: str) -> dict:
    """Generate one take for a scene against its approved quote.

    Requires an approved, unexpired, unconsumed quote for the scene; the
    first-scene gate applies to every scene after scene 0. On success the
    hold is captured (debit); on failure it is released (refund). Retrying
    with the same idempotency key resumes/returns the same job — never a
    second charge.
    """
    ws = _workspace.ProjectWorkspace.load(project_id)
    scene = _scene_with_prompt(ws, scene_id)
    scene_ids = [s["scene_id"] for s in ws.state["plan"]["scenes"]]
    if scene_id != scene_ids[0]:
        _pipeline.assert_first_scene_gate(project_id)

    jobs = ws.state["jobs"]
    if idempotency_key in jobs:
        job = jobs[idempotency_key]
        if job["status"] == "done":
            return dict(job["result"])
        if job["status"] == "failed":
            raise WaveCinemaError(
                f"job {idempotency_key} already failed ({job.get('error')}); "
                "reroll with a new idempotency key and a fresh approved quote"
            )
        # status == "running": an interrupted run resumes below.

    quote = _resolve_quote(ws, scene_id)
    now = datetime.now(timezone.utc).isoformat()
    rev = ws.revision
    quote["status"] = "consumed"
    quote["consumed_by"] = idempotency_key
    quote["consumed_at"] = now
    ws.state["quotes"][quote["quote_id"]] = quote
    job = {
        "job_id": idempotency_key,
        "scene_id": scene_id,
        "quote_id": quote["quote_id"],
        "hold_id": quote["hold_id"],
        "status": "running",
        "created_at": now,
        "error": None,
        "result": None,
    }
    jobs[idempotency_key] = job
    ws.log_event(f"job {idempotency_key}: generating {scene_id} ({quote['model']})")
    ws.save(expected_revision=rev)

    ledger = _ledger.Ledger()
    try:
        clip_path = _provider_for_project(project_id).generate(
            scene, scene["prompt"],
            duration_s=(scene["end_ms"] - scene["start_ms"]) / 1000.0,
            aspect="16:9",
        )
        take = _takes.register_take(
            project_id, scene_id, clip_path,
            quote_id=quote["quote_id"], job_idempotency_key=idempotency_key,
        )
        debit = ledger.capture(quote["hold_id"])
    except Exception as exc:  # noqa: BLE001 — must release the hold on ANY failure
        ledger.release(quote["hold_id"])
        ws = _workspace.ProjectWorkspace.load(project_id)
        rev = ws.revision
        ws.state["jobs"][idempotency_key]["status"] = "failed"
        ws.state["jobs"][idempotency_key]["error"] = str(exc)
        ws.log_event(f"job {idempotency_key} FAILED: {exc} (hold released)")
        ws.save(expected_revision=rev)
        raise

    result = {
        "job_id": idempotency_key,
        "scene_id": scene_id,
        "take_id": take["take_id"],
        "take_sha256": take["sha256"],
        "debit_id": debit["id"],
        "credits_spent": debit["credits"],
        "status": "done",
    }
    ws = _workspace.ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["jobs"][idempotency_key]["status"] = "done"
    ws.state["jobs"][idempotency_key]["result"] = result
    ws.log_event(
        f"job {idempotency_key} done: take {take['take_id']}, "
        f"debited {debit['credits']} credits"
    )
    ws.save(expected_revision=rev)
    return result


def reroll_scene(project_id: str, scene_id: str, idempotency_key: str) -> dict:
    """Generate another take for a scene. A fresh approved quote is required:
    each quote is consumed by exactly one generation, so a reroll is always
    a new paid job."""
    return generate_scene(project_id, scene_id, idempotency_key)


# ---------------------------------------------------------------- review (free)

def list_takes(project_id: str, scene_id: str) -> list:
    return _takes.list_takes(project_id, scene_id)


def select_take(project_id: str, scene_id: str, take_id: str) -> dict:
    return _takes.select_take(project_id, scene_id, take_id)


def delete_take(project_id: str, scene_id: str, take_id: str) -> dict:
    return _takes.delete_take(project_id, scene_id, take_id)


# ---------------------------------------------------------------- assemble/export (free)

def assemble(project_id: str) -> dict:
    return _assemble.assemble_project(project_id)


def export(project_id: str) -> dict:
    """Verify the master and record bytes + SHA-256; returns download path."""
    return _export.export_master(project_id)


# ---------------------------------------------------------------- BYO (free)

def export_job_pack(project_id: str) -> str:
    """Build the BYO job-pack zip (prompts + timing) for external generation.

    Free — no credits. The tester generates clips on their own account and
    uploads them back via import_job_clips. Returns the zip file path.
    """
    return str(_jobpack.build_pack(project_id))


def import_job_clips(project_id: str, zip_bytes: bytes) -> dict:
    """Register tester-generated clips from an uploaded zip as takes.

    Free — zero credits; the clips were generated on the tester's own
    account. Returns {"imported": [...], "skipped": [...]}.
    """
    return _jobpack.import_clips_zip(project_id, zip_bytes)


# ---------------------------------------------------------------- credits

def credit_balance(account_id: str = DEFAULT_ACCOUNT) -> int:
    """Available (unreserved) credit balance for an account."""
    return _ledger.Ledger().balance(account_id)


def get_balance(account_id: str) -> int:
    """Available (unreserved) credit balance for an account.

    Kept for the Stripe paywall webhook — exact signature preserved.
    """
    return _ledger.Ledger().balance(account_id)


def grant_credits(account_id: str, credits: int, memo: str = "") -> dict:
    """Add prepaid credits to an account.

    Idempotent on the memo: repeating the call with the same memo returns the
    original ledger entry instead of granting twice. The paywall passes
    ``memo=f"stripe:{session_id}"`` so each Stripe Checkout Session grants
    exactly once, even across webhook redeliveries and process restarts.
    """
    return _ledger.Ledger().grant(
        account_id=account_id,
        credits=credits,
        memo=memo,
        idempotency_key=memo or None,
    )


__all__ = [
    "create_project", "ingest_song", "transcribe", "plan_cuts", "direct",
    "slice_scenes", "quote_scene", "approve_quote", "generate_scene",
    "list_takes", "select_take", "reroll_scene", "assemble", "export",
    "get_project", "credit_balance", "get_balance", "grant_credits",
    "set_provider", "get_provider",
    "export_job_pack", "import_job_clips",
    "get_provider_lane", "set_provider_lane", "cloud_available",
]
