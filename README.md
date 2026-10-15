---
title: Wave Cinema
emoji: 🌊
colorFrom: blue
colorTo: indigo
sdk: gradio
sdk_version: 6.30.0
app_file: app.py
pinned: false
---

# Wave Cinema 🌊 🎦 — public demo (Midnight Chrome v1.0)

AI music-video studio: song in → agent director plans timed scenes → clip
generation → take review → assembly → master export.

## How testers use this demo (BYO generation — free)

1. **Create a project**, upload your song (MP3/WAV), tick the rights
   checkbox, and run **Direct** to generate the scene plan. All free.
2. In **Direct**, hit **Download job pack (.zip)**: every scene prompt,
   exact timings and a README for your generating side.
3. **Generate one clip per scene on your own Muse / video-gen account**
   (name them `scene_00.mp4`, `scene_01.mp4`, …), zip them.
4. In **Review**, **upload the clip zip** — each clip registers as a take
   (bytes + SHA-256). Review, select winners per scene, then **Assemble**
   in **Export** to build the master. Zero credits change hands — the
   clips were generated on your own account.

## Wave Cinema Cloud (disabled on this demo)

Each project can pick a **Generation lane**: the BYO loop above (default,
zero cost), or **Wave Cinema Cloud** — generation on our Higgsfield key,
debited from the credit balance with margin. The cloud lane is disabled on
this free demo; it unlocks when the app moves to paid hosting and the
`HIGGSFIELD_API_KEY` secret is set. See DEPLOY_NOTES.md.

## Payments — TEST MODE

The Stripe paywall is included but runs in **test mode**: no live keys are
configured on this Space, so no real charges are possible. Real payments
require the owner's Stripe live onboarding. Do not enter real card details.

## Free-tier limits (honest)

- **Ephemeral storage**: projects, takes and masters vanish when the Space
  sleeps or restarts. Download anything you want to keep.
- The Space **sleeps when idle** (cold start on wake).
- Mock (server-side) generation needs ffmpeg — a static binary is bundled
  via `imageio-ffmpeg`; if it ever fails, the BYO flow above is the
  supported path.
