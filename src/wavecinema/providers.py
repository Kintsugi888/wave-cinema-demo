"""Video generation providers.

ProviderAdapter is the interface every generation backend implements.
MockProvider renders a clearly-marked placeholder clip with ffmpeg (free,
offline, deterministic shape) so the whole pipeline runs end-to-end today.
HiggsfieldProvider ("Wave Cinema Cloud") drives the Higgsfield API
(async submit -> poll -> download) with the key read ONLY from the
HIGGSFIELD_API_KEY environment variable. RealProvider is a stub
documenting the planned real-provider contract — it never calls any
paid API.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

MONO_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"  # JetBrains-mono-ish stand-in

# Midnight Chrome brand tokens used in the placeholder frame.
NAVY = "0x142B52"
ELECTRIC_BLUE = "0x1677FF"
ICE_WHITE = "0xF4F8FF"


class ProviderAdapter(ABC):
    """Interface for video-generation backends."""

    @abstractmethod
    def generate(self, scene: dict, prompt: str, duration_s: float,
                 aspect: str = "16:9") -> str:
        """Render a clip for a scene. Returns the local clip file path."""
        raise NotImplementedError


class MockProvider(ProviderAdapter):
    """Free offline placeholder: navy frame, blue accents, text + timecode.

    Not a real generation — a stand-in so quoting, takes, review, assembly
    and export all run end-to-end. Every clip is stamped "MOCK TAKE".
    """

    def __init__(self, out_dir: str | Path):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, scene: dict, prompt: str, duration_s: float,
                 aspect: str = "16:9") -> str:
        if aspect != "16:9":
            raise ValueError(f"MockProvider renders 16:9 only, got {aspect!r}")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        scene_id = scene.get("scene_id", "s??")
        clip_name = f"{scene_id}-mock-{uuid.uuid4().hex[:6]}.mp4"
        clip_path = self.out_dir / clip_name

        font_opt = f":fontfile={MONO_FONT}" if Path(MONO_FONT).exists() else ""
        prompt_line = prompt.replace("\n", " ").strip()[:64]

        with tempfile.TemporaryDirectory(prefix="wc-mock-") as tmp:
            overlay = Path(tmp) / "overlay.txt"
            overlay.write_text(
                f"SCENE {scene_id}\n{prompt_line}\nWAVE CINEMA // MOCK TAKE\n",
                encoding="utf-8",
            )
            vf = (
                "format=yuv420p,"
                "noise=alls=8:allf=t,"
                "crop=1280:720:x='(in_w-out_w)/2+80*sin(2*PI*t/12)':y='(in_h-out_h)/2',"
                f"drawbox=x=0:y=0:w=iw:h=10:c={ELECTRIC_BLUE}:t=fill,"
                f"drawbox=x=0:y=ih-10:w=iw:h=10:c={ELECTRIC_BLUE}:t=fill,"
                f"drawbox=x=0:y=0:w=10:h=ih:c={ELECTRIC_BLUE}:t=fill,"
                f"drawbox=x=iw-10:y=0:w=10:h=ih:c={ELECTRIC_BLUE}:t=fill,"
                f"drawbox=x='(t*90)':y=0:w=6:h=ih:c={ELECTRIC_BLUE}@0.35:t=fill,"
                f"drawtext=textfile='{overlay}':fontsize=40:fontcolor={ICE_WHITE}"
                f"{font_opt}:x=(w-text_w)/2:y=140:line_spacing=12,"
                "drawtext=text='%{pts\\:hms}':fontsize=34"
                f":fontcolor={ELECTRIC_BLUE}{font_opt}:x=w-text_w-40:y=h-90"
            )
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error",
                 "-f", "lavfi", "-i",
                 f"color=c={NAVY}:s=2560x1440:d={duration_s:.3f}:r=24",
                 "-vf", vf,
                 "-t", f"{duration_s:.3f}",
                 "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-an", str(clip_path)],
                check=True,
            )
        return str(clip_path)


def cloud_available() -> dict:
    """Whether the Wave Cinema Cloud lane (Higgsfield) can be offered.

    Disabled by default: requires the explicit WAVECINEMA_CLOUD_ENABLED=1
    flag AND a HIGGSFIELD_API_KEY secret. The key is never read from any
    file — environment only.
    """
    if os.environ.get("WAVECINEMA_CLOUD_ENABLED") != "1":
        return {"ok": False,
                "reason": "cloud lane is off (set WAVECINEMA_CLOUD_ENABLED=1 to unlock)"}
    key = (os.environ.get("HIGGSFIELD_API_KEY") or "").strip()
    if not key or ":" not in key:
        return {"ok": False,
                "reason": "HIGGSFIELD_API_KEY is not set (expected <key_id>:<key_secret>)"}
    return {"ok": True, "reason": ""}


class HiggsfieldProvider(ProviderAdapter):
    """Wave Cinema Cloud: Higgsfield API (submit -> poll -> download).

    Contract (official docs 2026-09-24):
      POST {base}/{model}  {"prompt","duration","aspect_ratio"}
        -> {"status":"queued","request_id","status_url",...}
      GET  {status_url}    (poll until completed / failed)
      download the result video URL to the take store.

    Auth: HIGGSFIELD_API_KEY env var holding "<key_id>:<key_secret>",
    sent as `Authorization: Key <id>:<secret>`. The key value is never
    logged, never written to disk, never shipped in any repo.
    """

    DEFAULT_MODEL = "bytedance/seedance-2.5/text-to-video"
    TERMINAL_OK = {"completed", "done", "success"}
    TERMINAL_FAIL = {"failed", "error", "cancelled"}

    def __init__(self, out_dir: str | Path, api_key: str | None = None,
                 model: str | None = None, api_base: str | None = None,
                 poll_interval_s: float = 10.0, timeout_s: float = 900.0):
        key = (api_key if api_key is not None
               else os.environ.get("HIGGSFIELD_API_KEY", "")).strip()
        if not key or ":" not in key:
            raise RuntimeError(
                "HIGGSFIELD_API_KEY is not set (expected <key_id>:<key_secret>); "
                "the cloud provider refuses to run without it")
        self._key = key  # never logged
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.model = (model or os.environ.get("HIGGSFIELD_MODEL")
                      or self.DEFAULT_MODEL).strip("/")
        self.api_base = (api_base or os.environ.get("HIGGSFIELD_API_BASE")
                         or "https://api.higgsfield.ai").rstrip("/")
        self.poll_interval_s = poll_interval_s
        self.timeout_s = timeout_s

    # ------------------------------------------------------------- internals

    def _request(self, method: str, url: str,
                 payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Key " + self._key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(
                f"Higgsfield API HTTP {e.code} on {method} "
                f"{url.split('api.higgsfield.ai')[-1] or '/'}: {body}")

    def _submit(self, prompt: str, duration_s: int, aspect: str) -> tuple[str, str]:
        body = {"prompt": prompt, "duration": duration_s,
                "aspect_ratio": aspect}
        res = self._request("POST", f"{self.api_base}/{self.model}", body)
        request_id = res.get("request_id") or res.get("id")
        status_url = (res.get("status_url")
                      or f"{self.api_base}/requests/{request_id}/status")
        if not request_id:
            raise RuntimeError(
                f"Higgsfield submit returned no request_id: {str(res)[:200]}")
        return str(request_id), str(status_url)

    @staticmethod
    def _result_url(res: dict) -> str | None:
        for jobs in (res.get("jobs"),):
            if isinstance(jobs, list) and jobs:
                j = jobs[0]
                for path in (("results", "raw", "url"), ("results", "url")):
                    node = j
                    for k in path:
                        node = (node or {}).get(k) if isinstance(node, dict) else None
                    if node:
                        return str(node)
        outputs = res.get("outputs")
        if isinstance(outputs, list) and outputs:
            o = outputs[0]
            if isinstance(o, dict):
                for k in ("url", "file", "video_url"):
                    if o.get(k):
                        return str(o[k])
            elif isinstance(o, str):
                return o
        for k in ("url", "video_url", "file_url", "result_url"):
            if res.get(k):
                return str(res[k])
        return None

    def _poll(self, status_url: str) -> str:
        deadline = time.time() + self.timeout_s
        last = ""
        while time.time() < deadline:
            res = self._request("GET", status_url)
            status = str(res.get("status", "")).lower()
            last = status
            if status in self.TERMINAL_OK:
                url = self._result_url(res)
                if not url:
                    raise RuntimeError(
                        "Higgsfield job completed but returned no video URL")
                return url
            if status in self.TERMINAL_FAIL:
                raise RuntimeError(
                    f"Higgsfield job failed (status={status}): "
                    f"{str(res.get('error') or res.get('message') or res)[:200]}")
            time.sleep(self.poll_interval_s)
        raise RuntimeError(
            f"Higgsfield job did not complete within {self.timeout_s:.0f}s "
            f"(last status={last!r})")

    # ------------------------------------------------------------- interface

    def generate(self, scene: dict, prompt: str, duration_s: float,
                 aspect: str = "16:9") -> str:
        if aspect not in ("16:9", "9:16", "1:1"):
            raise ValueError(f"HiggsfieldProvider: unsupported aspect {aspect!r}")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        seconds = max(2, min(15, int(round(duration_s))))
        scene_id = scene.get("scene_id", "s??")

        request_id, status_url = self._submit(prompt, seconds, aspect)
        video_url = self._poll(status_url)

        clip_name = f"{scene_id}-hf-{uuid.uuid4().hex[:6]}.mp4"
        clip_path = self.out_dir / clip_name
        tmp = clip_path.with_suffix(".mp4.part")
        try:
            urllib.request.urlretrieve(video_url, str(tmp))
        except Exception as e:
            raise RuntimeError(f"Higgsfield result download failed: {e}")
        if tmp.stat().st_size < 1024:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("Higgsfield result download too small — aborting")
        tmp.replace(clip_path)
        return str(clip_path)


class RealProvider(ProviderAdapter):
    """Stub for the future real video-generation backend.

    Planned contract (MiniMax Hailuo-3-shaped API):
      - generate(scene, prompt, duration_s, aspect) -> local clip file path
      - scene: dict with scene_id, start_ms, end_ms, duration_ms (+ cast)
      - prompt: self-contained scene prompt string (identity locks restated)
      - duration_s: float, provider bounds 5–15 s per scene
      - aspect: one of "16:9", "9:16", "1:1" (render target 1280x720 for 16:9)
      - up to 9 reference images per scene (character/set consistency)
      - returns the path of the downloaded clip (bytes + SHA-256 verified
        by the caller before it becomes a take)

    NEVER call any paid API from this codebase without an explicit,
    human-approved provider configuration.
    """

    def generate(self, scene: dict, prompt: str, duration_s: float,
                 aspect: str = "16:9") -> str:
        raise NotImplementedError(
            "Real provider not configured — planned for MiniMax Hailuo-3-shaped "
            "API: 5-15s, 16:9/9:16/1:1, up to 9 reference images"
        )
