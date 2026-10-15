"""Wave Cinema paywall: Stripe credit-pack billing.

TEST MODE ONLY. This module refuses to operate with live Stripe keys and never
creates a real charge. Credit packs are sold via Stripe Checkout (test mode);
credits are granted by the ``checkout.session.completed`` webhook, exactly once
per Stripe session id.

Include in the main app::

    from fastapi import FastAPI
    from app.paywall import router

    app = FastAPI()
    app.include_router(router)

Configuration is documented in ``paywall_config.md`` next to this file.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import stripe

try:  # stripe-python v8+
    from stripe._error import SignatureVerificationError, StripeError
except ImportError:  # pragma: no cover - older stripe-python
    from stripe.error import SignatureVerificationError, StripeError  # type: ignore

logger = logging.getLogger("wavecinema.paywall")

router = APIRouter()

# Single-operator phase: all purchases land on the owner's ledger account.
# Multi-account support arrives in Phase 3.
ACCOUNT_ID = "owner"

# --- Credit math (mirrors ARCHITECTURE.md; the quote engine owns the math,
#     the paywall only sells packs) -------------------------------------------
CREDITS_PER_SECOND = 8
MIN_CREDITS_PER_GENERATION = 40

# Credit packs. USD labels are informational; the authoritative price lives in
# the Stripe Price object referenced by each STRIPE_PRICE_* env var. Keep them
# in sync when creating the test-mode prices (see paywall_config.md).
PACKS: Dict[str, Dict[str, Any]] = {
    "500": {
        "pack_id": "500",
        "credits": 500,
        "usd": 10,
        "name": "Spark",
        "env": "STRIPE_PRICE_500",
        "blurb": "For trying the director workflow on a short track.",
    },
    "1500": {
        "pack_id": "1500",
        "credits": 1500,
        "usd": 25,
        "name": "Cut",
        "env": "STRIPE_PRICE_1500",
        "blurb": "A full song, scenes and rerolls included.",
    },
    "5000": {
        "pack_id": "5000",
        "credits": 5000,
        "usd": 75,
        "name": "Feature",
        "env": "STRIPE_PRICE_5000",
        "blurb": "Album runs, long tracks, heavy reroll sessions.",
    },
}


# --- configuration -----------------------------------------------------------

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _looks_live(key: str) -> bool:
    return key.startswith("sk_live_") or key.startswith("rk_live_") or key.startswith("pk_live_")


def billing_status() -> Dict[str, Any]:
    """Check Stripe test-mode configuration without touching the network."""
    secret = _env("STRIPE_SECRET_KEY")
    publishable = _env("STRIPE_PUBLISHABLE_KEY")
    webhook_secret = _env("STRIPE_WEBHOOK_SECRET")
    prices = {pid: _env(p["env"]) for pid, p in PACKS.items()}

    problems = []
    if not secret:
        problems.append("STRIPE_SECRET_KEY is not set")
    elif _looks_live(secret):
        problems.append("STRIPE_SECRET_KEY looks like a LIVE key: test mode only, refusing to run")
    elif not (secret.startswith("sk_test_") or secret.startswith("rk_test_")):
        problems.append("STRIPE_SECRET_KEY does not look like a Stripe test secret key (expected sk_test_... or rk_test_...)")
    if publishable and _looks_live(publishable):
        problems.append("STRIPE_PUBLISHABLE_KEY looks like a LIVE key: test mode only, refusing to run")
    for pid, price_id in prices.items():
        if not price_id:
            problems.append(f"{PACKS[pid]['env']} is not set (Stripe Price ID for the {pid}-credit pack)")
    if not webhook_secret:
        problems.append("STRIPE_WEBHOOK_SECRET is not set (webhook grants will be rejected)")

    return {
        "configured": not problems,
        "problems": problems,
        "prices": prices,
        "webhook_secret_set": bool(webhook_secret),
    }


# --- ledger bridge -----------------------------------------------------------

class LedgerUnavailable(RuntimeError):
    """Raised when the credit ledger cannot be reached. Webhook returns 5xx
    so Stripe retries delivery instead of silently dropping a paid purchase."""


def _grant_via_ledger(pack: Dict[str, Any], session_id: str) -> Any:
    """Grant pack credits to the owner ledger, idempotent on the memo key.

    The backend ``wavecinema.api.grant_credits`` is itself idempotent on the
    memo (``stripe:<session_id>``); the webhook also guards locally so a
    duplicate delivery never reaches the ledger twice.
    """
    memo = f"stripe:{session_id}"
    try:
        from wavecinema import api as ledger_api
    except ImportError as exc:
        raise LedgerUnavailable("wavecinema.api ledger package is not installed") from exc
    grant = getattr(ledger_api, "grant_credits", None)
    if grant is None:
        raise LedgerUnavailable("wavecinema.api has no grant_credits function")
    return grant(account_id=ACCOUNT_ID, credits=pack["credits"], memo=memo)


def _get_balance() -> Optional[int]:
    try:
        from wavecinema import api as ledger_api
    except ImportError:
        return None
    get_balance = getattr(ledger_api, "get_balance", None)
    if get_balance is None:
        return None
    try:
        return int(get_balance(account_id=ACCOUNT_ID))
    except Exception:
        logger.exception("get_balance failed")
        return None


def _so(obj: Any, key: str, default: Any = None) -> Any:
    """Safe read from a Stripe object (v16 StripeObject has no .get())."""
    try:
        value = obj[key]
    except (KeyError, TypeError, AttributeError):
        return default
    return value


# --- webhook idempotency ------------------------------------------------------
# Local guard so a duplicate webhook delivery never grants twice, even before
# the ledger is consulted. The ledger's own memo idempotency is the durable
# backstop across process restarts.

_grant_lock = threading.Lock()
_granted_sessions: set = set()


def _already_granted(session_id: str) -> bool:
    with _grant_lock:
        return session_id in _granted_sessions


def _mark_granted(session_id: str) -> None:
    with _grant_lock:
        _granted_sessions.add(session_id)


def _resolve_pack(session_obj: Any) -> Optional[Dict[str, Any]]:
    """Resolve the purchased pack from the Checkout Session.

    Primary path: the metadata we set at session creation. Fallback: match the
    session's line-item Price ID against the configured STRIPE_PRICE_* values
    (covers sessions created outside this endpoint, e.g. `stripe trigger`).
    """
    metadata = _so(session_obj, "metadata") or {}
    pack = PACKS.get(str(_so(metadata, "pack_id") or ""))
    if pack:
        return pack

    # Fallback: look up the session's price id via the API (test mode only).
    try:
        full = stripe.checkout.Session.retrieve(
            _so(session_obj, "id"),
            expand=["line_items"],
            api_key=_env("STRIPE_SECRET_KEY"),
        )
        items = _so(_so(full, "line_items") or {}, "data") or []
        if items:
            price_id = _so(_so(items[0], "price") or {}, "id")
            status = billing_status()
            for pid, pack in PACKS.items():
                if status["prices"].get(pid) == price_id:
                    return pack
    except Exception:
        logger.exception("price-id fallback lookup failed")
    return None


# --- brand CSS (Midnight Chrome v1.0) -----------------------------------------

_CSS = """
:root{
  --cinema-black:#080D18; --deep-navy:#142B52; --electric-blue:#1677FF;
  --chrome-silver:#C5D0E0; --ice-white:#F4F8FF; --sharingan-red:#E63946;
}
*{box-sizing:border-box}
body{margin:0;background:var(--cinema-black);color:var(--ice-white);
  font-family:'Inter',system-ui,-apple-system,sans-serif;min-height:100vh}
.wrap{max-width:1080px;margin:0 auto;padding:48px 24px 80px}
.wordmark{font-family:'Space Grotesk',sans-serif;font-weight:700;letter-spacing:.35em;
  font-size:15px;color:var(--chrome-silver)}
h1{font-family:'Space Grotesk',sans-serif;font-weight:700;font-size:42px;margin:14px 0 8px}
.sub{color:var(--chrome-silver);font-size:16px;margin:0 0 36px;max-width:640px}
.panel{background:var(--deep-navy);border:1px solid rgba(197,208,224,.22);
  border-radius:14px;padding:28px}
.balance-label{font-size:12px;letter-spacing:.25em;color:var(--chrome-silver)}
.balance{font-family:'JetBrains Mono',monospace;font-size:52px;font-weight:700;margin:6px 0 4px}
.balance-note{color:var(--chrome-silver);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:20px;margin-top:28px}
.card{background:var(--deep-navy);border:1px solid rgba(197,208,224,.22);border-radius:14px;
  padding:28px;display:flex;flex-direction:column}
.card h2{font-family:'Space Grotesk',sans-serif;margin:0 0 4px;font-size:22px}
.credits{font-family:'JetBrains Mono',monospace;font-size:40px;font-weight:700;margin:12px 0 2px}
.price{font-size:20px;color:var(--chrome-silver);margin-bottom:6px}
.per{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--chrome-silver);margin-bottom:14px}
.blurb{font-size:14px;color:var(--chrome-silver);flex:1;margin-bottom:20px}
button.buy{background:var(--electric-blue);color:#fff;border:0;border-radius:9px;
  padding:14px;font-size:16px;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
button.buy:hover{filter:brightness(1.12)}
button.buy:disabled{opacity:.55;cursor:wait}
.math{margin-top:28px;font-size:13px;color:var(--chrome-silver)}
.math code{font-family:'JetBrains Mono',monospace;color:var(--ice-white)}
.alert{border:1px solid var(--sharingan-red);border-radius:14px;padding:28px;background:rgba(230,57,70,.08)}
.alert h2{font-family:'Space Grotesk',sans-serif;margin-top:0}
.alert ul{color:var(--chrome-silver)}
.notice{background:rgba(22,119,255,.12);border:1px solid var(--electric-blue);
  border-radius:14px;padding:20px 24px;margin-bottom:24px}
a{color:var(--electric-blue)}
.footer{margin-top:56px;color:var(--chrome-silver);font-size:12px}
.mono{font-family:'JetBrains Mono',monospace}
.center{text-align:center}
.big{font-size:64px}
"""

_FONTS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=JetBrains+Mono:wght@400;700&family=Space+Grotesk:wght@700&display=swap" rel="stylesheet">
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} | Wave Cinema</title>{_FONTS}<style>{_CSS}</style></head>
<body><div class="wrap">
<div class="wordmark">WAVE CINEMA</div>
{body}
<div class="footer">WAVE CINEMA · AI-Powered Film Studio · Prepaid credits · Test mode · Credits have no cash value and are non-transferable.</div>
</div></body></html>"""


def _billing_page(balance: Optional[int], canceled: bool) -> str:
    cards = []
    for pid in ("500", "1500", "5000"):
        p = PACKS[pid]
        seconds = p["credits"] // CREDITS_PER_SECOND
        cards.append(f"""
        <div class="card">
          <h2>{p['name']}</h2>
          <div class="credits">{p['credits']:,}</div>
          <div class="price">${p['usd']} USD</div>
          <div class="per">≈ {seconds:,}s of generation · {p['usd']/p['credits']*100:.1f}¢/credit</div>
          <div class="blurb">{p['blurb']}</div>
          <button class="buy" data-pack="{pid}">Buy {p['credits']:,} credits</button>
        </div>""")
    notice = ""
    if canceled:
        notice = '<div class="notice">Checkout canceled. No charge was made. Your balance is unchanged.</div>'
    balance_html = f"{balance:,}" if balance is not None else "-"
    balance_note = (
        "Available to spend on generations."
        if balance is not None
        else "Ledger connecting. Balance will appear once the credit ledger is online."
    )
    body = f"""
<h1>Billing</h1>
<p class="sub">Top up prepaid credits for Wave Cinema. Every generation shows its exact
credit quote before it runs. You approve the amount, every time.</p>
{notice}
<div class="panel">
  <div class="balance-label">CREDIT BALANCE</div>
  <div class="balance">{balance_html}</div>
  <div class="balance-note">{balance_note}</div>
</div>
<div class="grid">{''.join(cards)}</div>
<div class="math">
  Credit math: <code>{MIN_CREDITS_PER_GENERATION} credits</code> minimum per generation,
  <code>{CREDITS_PER_SECOND} credits</code> per output second. Planning, quoting and
  assembly are free. Failed jobs release their reserved credit automatically.
</div>
<script>
document.querySelectorAll('button.buy').forEach(b=>b.addEventListener('click',async()=>{{
  b.disabled=true; b.textContent='Opening secure checkout…';
  try{{
    const r=await fetch('/billing/checkout',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{pack_id:b.dataset.pack}}) }});
    const j=await r.json();
    if(j.checkout_url){{ window.location=j.checkout_url; return; }}
    alert('Checkout failed: '+(j.error||r.status));
  }}catch(e){{ alert('Checkout failed: '+e); }}
  b.disabled=false; b.textContent='Buy '+b.dataset.pack+' credits';
}}));
</script>"""
    return _page("Billing", body)


def _not_configured_page(problems) -> str:
    items = "".join(f"<li>{p}</li>" for p in problems)
    body = f"""
<h1>Billing</h1>
<div class="alert">
  <h2>Billing is not configured</h2>
  <p>Credit packs can't be sold until Stripe test-mode credentials are in place.
  Nothing is broken. The server just needs its environment variables.</p>
  <ul>{items}</ul>
  <p>See <span class="mono">app/paywall_config.md</span> for setup instructions
  (test mode only. Live keys are refused).</p>
</div>"""
    return _page("Billing: not configured", body)


def _success_page() -> str:
    body = """
<h1>Payment received</h1>
<div class="panel center">
  <div class="big">◉</div>
  <h2 style="font-family:'Space Grotesk',sans-serif">Thank you. Your credits are on their way</h2>
  <p class="sub" style="margin:0 auto;max-width:560px">
  Credits are added automatically by our payment webhook, usually within a minute.
  <strong>This page does not grant credits itself</strong>. There's no need to refresh
  or pay again. Your balance updates on the <a href="/billing">billing page</a> once
  the webhook lands.</p>
</div>"""
    return _page("Payment successful", body)


# --- endpoints ----------------------------------------------------------------

@router.get("/billing", response_class=HTMLResponse)
async def billing_page(canceled: Optional[int] = None):
    status = billing_status()
    if not status["configured"]:
        return HTMLResponse(_not_configured_page(status["problems"]))
    return HTMLResponse(_billing_page(_get_balance(), canceled=bool(canceled)))


class CheckoutRequest(BaseModel):
    pack_id: str


@router.post("/billing/checkout")
async def create_checkout(req: CheckoutRequest):
    """Create a Stripe Checkout Session (TEST MODE) and return its redirect URL."""
    status = billing_status()
    if not status["configured"]:
        return JSONResponse(
            {"error": "billing_not_configured", "problems": status["problems"]},
            status_code=503,
        )
    pack = PACKS.get(req.pack_id)
    if pack is None:
        return JSONResponse(
            {"error": "unknown_pack", "valid_pack_ids": sorted(PACKS)},
            status_code=400,
        )
    base = _env("WAVE_CINEMA_BASE_URL") or "http://localhost:8000"
    try:
        session = stripe.checkout.Session.create(
            api_key=_env("STRIPE_SECRET_KEY"),
            mode="payment",
            line_items=[{"price": status["prices"][pack["pack_id"]], "quantity": 1}],
            client_reference_id=ACCOUNT_ID,
            metadata={
                "pack_id": pack["pack_id"],
                "credits": str(pack["credits"]),
                "account_id": ACCOUNT_ID,
            },
            success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{base}/billing?canceled=1",
        )
    except StripeError as exc:
        logger.exception("Stripe Checkout Session.create failed")
        return JSONResponse({"error": "stripe_error", "detail": str(exc)}, status_code=502)
    return {
        "checkout_url": session.url,
        "session_id": session.id,
        "pack_id": pack["pack_id"],
        "credits": pack["credits"],
        "usd": pack["usd"],
        "mode": "test",
    }


@router.get("/billing/success", response_class=HTMLResponse)
async def billing_success():
    # Deliberately grants nothing: credits arrive via the webhook below.
    return HTMLResponse(_success_page())


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    """Stripe webhook receiver. Verifies the signature, then grants credits for
    ``checkout.session.completed`` exactly once per Stripe session id."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    webhook_secret = _env("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        logger.error("webhook received but STRIPE_WEBHOOK_SECRET is not set")
        return JSONResponse({"error": "webhook_not_configured"}, status_code=503)

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, SignatureVerificationError):
        logger.warning("rejected webhook with invalid signature")
        return JSONResponse({"error": "invalid_signature"}, status_code=400)

    event_type = _so(event, "type")
    if event_type != "checkout.session.completed":
        return {"received": True, "handled": False, "type": event_type}

    session_obj = event["data"]["object"]
    session_id = _so(session_obj, "id")

    # Local idempotency guard: duplicate deliveries never grant twice.
    if _already_granted(session_id):
        logger.info("duplicate webhook for session %s ignored", session_id)
        return {"received": True, "duplicate": True, "session_id": session_id}

    if _so(session_obj, "payment_status") not in (None, "paid"):
        logger.warning("session %s completed without paid status (%s); no grant",
                       session_id, _so(session_obj, "payment_status"))
        return {"received": True, "handled": False, "reason": "not_paid"}

    pack = _resolve_pack(session_obj)
    if pack is None:
        # Money moved but we cannot map it to a pack. Do NOT 500/retry forever:
        # log loudly for manual reconciliation and acknowledge.
        logger.critical(
            "PAID session %s could not be mapped to a credit pack. Manual review required",
            session_id,
        )
        _mark_granted(session_id)  # don't retry-grant blindly; a human fixes this
        return JSONResponse(
            {"received": True, "error": "unknown_pack", "needs_manual_review": True,
             "session_id": session_id},
            status_code=200,
        )

    try:
        _grant_via_ledger(pack, session_id)
    except LedgerUnavailable:
        logger.exception("ledger unavailable for session %s; Stripe will retry", session_id)
        return JSONResponse({"error": "ledger_unavailable"}, status_code=503)

    _mark_granted(session_id)
    logger.info("granted %d credits for session %s", pack["credits"], session_id)
    return {
        "received": True,
        "granted": pack["credits"],
        "pack_id": pack["pack_id"],
        "session_id": session_id,
    }
