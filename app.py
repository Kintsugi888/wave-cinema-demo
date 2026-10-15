"""Wave Cinema — Hugging Face Spaces entry point (Gradio SDK, free tier).

Docker Spaces need a paid plan, so this Space uses the Gradio SDK: the
Wave Cinema FastAPI app is served at the Space root, with a minimal Gradio
stub mounted at /gradio. The Space listens on $PORT (7860).

Notes:
- Storage is ephemeral on the free tier: projects vanish when the Space
  sleeps/restarts. The BYO job-pack flow (download pack, generate
  elsewhere, upload clips) is the intended tester loop.
- The Stripe paywall ships in TEST MODE (no live keys on the Space).
  Real payments need $shamz's Stripe live onboarding.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))

# ffmpeg shim: the mock provider shells out to `ffmpeg`, which HF's base
# image may not ship. imageio-ffmpeg bundles a static binary; expose it
# under the plain `ffmpeg` name.
try:
    import imageio_ffmpeg  # noqa: E402

    _exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
    _shim = Path.home() / ".wc-shim-bin"
    _shim.mkdir(parents=True, exist_ok=True)
    _link = _shim / "ffmpeg"
    try:
        if _link.is_symlink() or _link.exists():
            _link.unlink()
        _link.symlink_to(_exe)
    except OSError:
        pass
    os.environ["PATH"] = str(_shim) + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

from main import app as fastapi_app  # noqa: E402

import gradio as gr  # noqa: E402

demo = gr.Interface(
    fn=lambda: (
        "Wave Cinema is live — open the Space URL root in your browser.\n"
        "The full studio UI is served there; this stub only satisfies the SDK."
    ),
    inputs=[],
    outputs="text",
    title="Wave Cinema",
    description="AI music-video studio (Midnight Chrome v1.0).",
)

# Mount the stub under /gradio; the product UI stays at /.
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
