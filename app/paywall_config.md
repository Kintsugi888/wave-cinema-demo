# Wave Cinema Paywall — Configuration

Server code: `app/paywall.py` (a FastAPI `APIRouter`; include it in the main app with `app.include_router(router)`).

**Test mode only.** The paywall refuses to run with live Stripe keys (`sk_live_*` / `pk_live_*` → the billing page shows "not configured" and `/billing/checkout` returns 503). No real charge can ever be created from this code path.

## Environment variables

| Variable | Required | Example | Notes |
|---|---|---|---|
| `STRIPE_SECRET_KEY` | yes | `sk_test_51AbC...` | Test-mode secret key. Read **server-side only** (per-call `api_key=` on Checkout creation). Never commit it, never log it, never render it into a page. |
| `STRIPE_PUBLISHABLE_KEY` | yes | `pk_test_51AbC...` | Test-mode publishable key. Reserved for future client-side Stripe.js use; the current flow redirects to Stripe-hosted Checkout so this key is not strictly required at runtime, but it must be a test key if set. |
| `STRIPE_WEBHOOK_SECRET` | yes | `whsec_...` | Signing secret for the `/webhooks/stripe` endpoint. Get it from the Stripe Dashboard webhook endpoint (test mode) or from `stripe listen` output. |
| `STRIPE_PRICE_500` | yes | `price_1AbC...` | Stripe **Price ID** (test mode) for the 500-credit / $10 pack. |
| `STRIPE_PRICE_1500` | yes | `price_1DeF...` | Stripe **Price ID** (test mode) for the 1500-credit / $25 pack. |
| `STRIPE_PRICE_5000` | yes | `price_1GhI...` | Stripe **Price ID** (test mode) for the 5000-credit / $75 pack. |
| `WAVE_CINEMA_BASE_URL` | no | `http://localhost:8000` | Public base URL used for Checkout `success_url`/`cancel_url`. Defaults to `http://localhost:8000`. Set to your tunnel URL (e.g. ngrok) when testing webhooks against a public endpoint. |

If any required variable is missing, `GET /billing` renders a branded **"billing not configured"** page listing exactly what is missing — the server never crashes and never half-starts a checkout.

Keep secrets out of files: export them in the shell that starts the server (or a process manager's env), never in the repo.

```bash
export STRIPE_SECRET_KEY='sk_test_...'
export STRIPE_PUBLISHABLE_KEY='pk_test_...'
export STRIPE_WEBHOOK_SECRET='whsec_...'
export STRIPE_PRICE_500='price_...'
export STRIPE_PRICE_1500='price_...'
export STRIPE_PRICE_5000='price_...'
# optional:
export WAVE_CINEMA_BASE_URL='http://localhost:8000'
```

## Creating the test-mode prices

Prices are one-time setup, done in **test mode** (orange "TEST MODE" banner in the Dashboard, or `stripe` CLI which defaults to test mode after `stripe login`).

### Option A — Stripe CLI

```bash
stripe login   # opens the browser; pairs the CLI with your account (test mode)

stripe prices create --unit-amount 1000 --currency usd \
  --product-data name="Wave Cinema — 500 credits"
# → copy the returned id (price_...) into STRIPE_PRICE_500

stripe prices create --unit-amount 2500 --currency usd \
  --product-data name="Wave Cinema — 1500 credits"
# → STRIPE_PRICE_1500

stripe prices create --unit-amount 7500 --currency usd \
  --product-data name="Wave Cinema — 5000 credits"
# → STRIPE_PRICE_5000
```

### Option B — Dashboard

1. Switch to **Test mode** (toggle in the left nav).
2. Product catalog → **Create product** → name it e.g. `Wave Cinema — 500 credits`.
3. Add a **one-time** price: 10.00 USD. Save and copy the Price ID (`price_...`) into `STRIPE_PRICE_500`.
4. Repeat for 1500 credits / $25.00 and 5000 credits / $75.00.

The USD amounts shown on the billing page live in `PACKS` in `paywall.py`; keep them in sync with the Price objects you create.

## Pointing Stripe test webhooks at the local server

### Local development (recommended)

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
# → prints a webhook signing secret like whsec_... ; export it as STRIPE_WEBHOOK_SECRET
```

Then, in another terminal, run the app and complete a test Checkout on `/billing`
with card `4242 4242 4242 4242` (any future expiry, any CVC). `stripe listen` prints
each forwarded event so you can watch the grant happen.

Note: `stripe trigger checkout.session.completed` fires an event whose session
carries **no pack metadata** (it isn't created by our endpoint). That exercises the
price-ID fallback path; if the fixture's price doesn't match your test prices the
endpoint returns `needs_manual_review` and logs loudly — that is the intended
behaviour for unmappable sessions, not a bug.

### Persistent test endpoint (Dashboard)

1. Developers → Webhooks → **Add endpoint** (in test mode).
2. Endpoint URL: `https://<your-host>/webhooks/stripe` (must be HTTPS; use a tunnel for local).
3. Events to send: select `checkout.session.completed` only.
4. Copy the endpoint's **Signing secret** into `STRIPE_WEBHOOK_SECRET` and restart the server.

## How the money → credits flow works

1. `POST /billing/checkout {pack_id}` creates a Stripe Checkout Session in **test
   mode** with the pack's Price ID, `client_reference_id=owner`, and
   `metadata={pack_id, credits, account_id}`. Returns `{checkout_url}`; the buyer
   pays on Stripe's hosted page (test card `4242…`).
2. Stripe sends `checkout.session.completed` to `POST /webhooks/stripe`.
3. The endpoint verifies the `stripe-signature` header with `STRIPE_WEBHOOK_SECRET`
   (wrong/missing signature → 400, nothing happens).
4. The pack is resolved from session metadata (fallback: line-item Price ID).
5. Credits are granted via `wavecinema.api.grant_credits(account_id="owner", credits=<pack>, memo=f"stripe:<session_id>")`.

**Exactly-once:** the idempotency key is the Stripe session id. A local in-memory
guard rejects duplicate deliveries before the ledger is touched; the ledger's own
memo idempotency (`stripe:<session_id>`) is the durable backstop across restarts.
`GET /billing/success` deliberately grants **nothing** — credits arrive only via
the webhook. If the ledger is unreachable the webhook returns 503 so Stripe
retries instead of dropping a paid purchase.

## Credit math

Owned by the backend quote engine; the paywall only sells packs.

- 40 credits minimum per generation, 8 credits per output second.
- Planning, quoting, assembly: free. Failed jobs release reserved credit.
- Pack equivalences (at 8 credits/s): 500 ≈ 62s of generation · 1500 ≈ 187s · 5000 ≈ 625s.

## Running locally / tests

```bash
# minimal app for local testing (main app wires this in Phase 3)
python - <<'EOF'
from fastapi import FastAPI
from app.paywall import router
app = FastAPI()
app.include_router(router)
import uvicorn; uvicorn.run(app, host="127.0.0.1", port=8000)
EOF

# tests (stripe is mocked; no network, no real charges)
cd ~/workspace/wave-cinema
python -m pytest tests/test_paywall.py -v
```
