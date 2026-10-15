"""Quote engine: exact, expiring per-scene generation quotes.

Pricing (Wave Cinema credit economy):
  - 40 credits minimum per generation
  - 8 credits per output second, seconds rounded UP (ceil)
  - quotes expire 15 minutes after issue

Planning, quoting, slicing and assembly are free — only generation debits.
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

from .errors import QuoteExpired, SceneNotFound
from .workspace import ProjectWorkspace

MODEL = "mock-v1"
RESOLUTION = "1280x720"
CURRENCY = "USD"
MIN_CREDITS = 40
CREDITS_PER_SECOND = 8
QUOTE_TTL = timedelta(minutes=15)
CLOUD_MODEL = "higgsfield/seedance-2.5"  # Wave Cinema Cloud model label
CLOUD_MARGIN = 1.5  # pricing lever: cloud quotes are base x margin


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def price_for_duration_ms(duration_ms: int) -> tuple[int, int]:
    """Return (billable_seconds, credits_total) for a scene duration.

    NOTE on the worked example: under the stated rule, 7.2s -> ceil = 8
    billable seconds -> 64 credits (the 40-credit minimum does not engage);
    12s -> 96 credits. A short scene (e.g. 4.5s) -> ceil 5 -> 40 credits
    via the minimum. If the intended behaviour for 7.2s is truly 40,
    that needs a different (kinked) formula — flag before changing.
    """
    billable_seconds = max(1, math.ceil(duration_ms / 1000))
    credits_total = max(MIN_CREDITS, CREDITS_PER_SECOND * billable_seconds)
    return billable_seconds, credits_total


def quote_scene(project_id: str, scene_id: str) -> dict:
    """Issue a fresh expiring quote for one scene. Stored in the workspace."""
    ws = ProjectWorkspace.load(project_id)
    plan = ws.state.get("plan")
    scenes = (plan or {}).get("scenes") or ws.state.get("scenes") or []
    scene = next((s for s in scenes if s["scene_id"] == scene_id), None)
    if scene is None:
        raise SceneNotFound(f"project {project_id} has no scene {scene_id!r}")

    # Timed-plan scenes carry start/end; cut-plan scenes carry duration_ms.
    duration_ms = scene.get("duration_ms")
    if duration_ms is None:
        duration_ms = int(scene["end_ms"]) - int(scene["start_ms"])
    billable_seconds, credits_total = price_for_duration_ms(duration_ms)
    lane = ws.state.get("provider_lane", "byo")
    model = MODEL
    if lane == "cloud":
        # Wave Cinema Cloud: our Higgsfield key, debited WITH margin.
        credits_total = math.ceil(credits_total * CLOUD_MARGIN)
        model = CLOUD_MODEL
    now = _utcnow()
    quote = {
        "quote_id": "q_" + uuid.uuid4().hex[:12],
        "project_id": project_id,
        "scene_id": scene_id,
        "model": model,
        "lane": lane,
        "resolution": RESOLUTION,
        "jobs": 1,
        "billable_seconds": billable_seconds,
        "duration_ms": duration_ms,
        "currency": CURRENCY,
        "credits_total": credits_total,
        "issued_at": now.isoformat(),
        "expires_at": (now + QUOTE_TTL).isoformat(),
        "status": "quoted",          # quoted -> approved -> consumed
        "hold_id": None,
    }
    rev = ws.revision
    ws.state["quotes"][quote["quote_id"]] = quote
    ws.log_event(
        f"quoted scene {scene_id} [{lane}]: {credits_total} {CURRENCY} credits "
        f"({billable_seconds}s billable), expires {quote['expires_at']}"
    )
    ws.save(expected_revision=rev)
    return dict(quote)


def get_quote(project_id: str, quote_id: str) -> dict:
    ws = ProjectWorkspace.load(project_id)
    quote = ws.state["quotes"].get(quote_id)
    if quote is None:
        raise KeyError(f"no such quote {quote_id!r} in project {project_id}")
    return dict(quote)


def assert_quote_usable(project_id: str, quote_id: str) -> dict:
    """Return the quote if it is approved, unexpired and unconsumed."""
    quote = get_quote(project_id, quote_id)
    if quote["status"] != "approved":
        from .errors import NoApprovedQuote
        raise NoApprovedQuote(
            f"quote {quote_id} is {quote['status']!r}, not approved"
        )
    if datetime.fromisoformat(quote["expires_at"]) <= _utcnow():
        raise QuoteExpired(f"quote {quote_id} expired at {quote['expires_at']}")
    return quote
