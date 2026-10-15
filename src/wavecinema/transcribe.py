"""Transcription: whisper word-level timestamps -> cue sheet JSON.

Cue sheet shape:
    {
      "schema_version": "wavecinema.cuesheet/1",
      "generated_by": "whisper" | "heuristic",
      "whisper_model": "tiny" | ... | null,
      "duration_ms": int,
      "words": [{"word": str, "start_ms": int, "end_ms": int}],
      "phrases": [{"phrase_id": str, "start_ms": int, "end_ms": int,
                   "owner": "LEAD"|"ENSEMBLE"|"INSTRUMENTAL", "words": [...]}],
      "created_at": iso,
    }

Speaker owners: whisper has no diarization, so every sung phrase is owned
by "LEAD" and word-free stretches >= 4s are marked "INSTRUMENTAL". The
"ENSEMBLE" owner exists in the schema for caller-supplied or future
diarized cue sheets. This limitation is documented rather than faked.

If whisper is unavailable, a clearly-marked heuristic segmenter is used
(equal 4s phrases, all INSTRUMENTAL) and the fallback is logged.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .ingest import master_path
from .workspace import ProjectWorkspace

PHRASE_GAP_MS = 1200          # silence >= this starts a new phrase
INSTRUMENTAL_MIN_MS = 4000    # word-free stretch >= this is marked instrumental
HEURISTIC_PHRASE_MS = 4000    # fallback segmenter phrase length

DEFAULT_MODEL = os.environ.get("WAVECINEMA_WHISPER_MODEL", "tiny")


def _s(ms_float: float) -> int:
    return int(round(ms_float * 1000))


def _group_phrases(words: list[dict], duration_ms: int) -> list[dict]:
    """Group words into phrases; mark instrumental stretches."""
    sung: list[dict] = []
    current: list[dict] = []
    for w in words:
        if current and w["start_ms"] - current[-1]["end_ms"] >= PHRASE_GAP_MS:
            sung.append(current)
            current = []
        current.append(w)
    if current:
        sung.append(current)

    phrases: list[dict] = []
    for grp in sung:
        phrases.append({
            "start_ms": grp[0]["start_ms"],
            "end_ms": grp[-1]["end_ms"],
            "owner": "LEAD",
            "words": grp,
        })

    # Mark word-free stretches as INSTRUMENTAL (intro, gaps, outro).
    covered = [(p["start_ms"], p["end_ms"]) for p in phrases]
    instrumentals: list[tuple[int, int]] = []
    cursor = 0
    for start, end in sorted(covered):
        if start - cursor >= INSTRUMENTAL_MIN_MS:
            instrumentals.append((cursor, start))
        cursor = max(cursor, end)
    if duration_ms - cursor >= INSTRUMENTAL_MIN_MS:
        instrumentals.append((cursor, duration_ms))
    for start, end in instrumentals:
        phrases.append({
            "start_ms": start, "end_ms": end,
            "owner": "INSTRUMENTAL", "words": [],
        })

    phrases.sort(key=lambda p: p["start_ms"])
    for i, p in enumerate(phrases):
        p["phrase_id"] = f"ph{i:02d}"
    return phrases


def _whisper_library_available() -> bool:
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _whisper_via_library(audio_path: Path, model_name: str) -> list[dict]:
    import whisper
    model = whisper.load_model(model_name)
    result = model.transcribe(str(audio_path), word_timestamps=True, verbose=False)
    words: list[dict] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []) or []:
            words.append({
                "word": str(w["word"]).strip(),
                "start_ms": _s(w["start"]),
                "end_ms": _s(w["end"]),
            })
    words = [w for w in words if w["word"]]
    words.sort(key=lambda w: w["start_ms"])
    return words


def _whisper_cli_path() -> str | None:
    return shutil.which("whisper")


def _pip_shows_whisper() -> bool:
    try:
        r = subprocess.run(
            ["pip", "show", "openai-whisper"],
            capture_output=True, text=True, timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def _whisper_via_cli(audio_path: Path, model_name: str) -> list[dict]:
    binary = _whisper_cli_path()
    assert binary, "whisper CLI not found"
    with tempfile.TemporaryDirectory(prefix="wc-whisper-") as tmp:
        subprocess.run(
            [binary, str(audio_path), "--model", model_name,
             "--word_timestamps", "True", "--output_format", "json",
             "--output_dir", tmp, "--fp16", "False"],
            capture_output=True, text=True, check=True, timeout=3600,
        )
        outputs = list(Path(tmp).glob("*.json"))
        if not outputs:
            raise RuntimeError("whisper CLI produced no JSON output")
        data = json.loads(outputs[0].read_text(encoding="utf-8"))
    words: list[dict] = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []) or []:
            words.append({
                "word": str(w["word"]).strip(),
                "start_ms": _s(w["start"]),
                "end_ms": _s(w["end"]),
            })
    words = [w for w in words if w["word"]]
    words.sort(key=lambda w: w["start_ms"])
    return words


def _heuristic_cue_sheet(duration_ms: int, reason: str) -> dict:
    """Clearly-marked fallback: equal 4s phrases, all INSTRUMENTAL."""
    phrases: list[dict] = []
    start = 0
    i = 0
    while start < duration_ms:
        end = min(start + HEURISTIC_PHRASE_MS, duration_ms)
        phrases.append({
            "phrase_id": f"ph{i:02d}",
            "start_ms": start, "end_ms": end,
            "owner": "INSTRUMENTAL", "words": [],
        })
        start = end
        i += 1
    return {
        "schema_version": "wavecinema.cuesheet/1",
        "generated_by": "heuristic",
        "fallback_reason": reason,
        "whisper_model": None,
        "duration_ms": duration_ms,
        "words": [],
        "phrases": phrases,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def transcribe_project(project_id: str, model_name: str = DEFAULT_MODEL) -> dict:
    """Transcribe the project's master song and store the cue sheet.

    Uses whisper (library first, CLI second) when available; otherwise falls
    back to the marked heuristic segmenter and logs the fallback.
    """
    audio = master_path(project_id)
    ws = ProjectWorkspace.load(project_id)
    duration_ms = int(ws.state["song"]["duration_ms"])

    words: list[dict] | None = None
    used = "whisper"
    fallback_reason = ""

    if _whisper_library_available():
        words = _whisper_via_library(audio, model_name)
    else:
        # Secondary checks before giving up on whisper.
        if _pip_shows_whisper() and _whisper_library_available():
            words = _whisper_via_library(audio, model_name)
        elif _whisper_cli_path():
            words = _whisper_via_cli(audio, model_name)

    if words is None:
        fallback_reason = (
            "whisper unavailable (no importable openai-whisper module, "
            "`pip show openai-whisper` negative, no `whisper` binary); "
            "using heuristic segmenter"
        )
        cue = _heuristic_cue_sheet(duration_ms, fallback_reason)
        used = "heuristic"
    else:
        phrases = _group_phrases(words, duration_ms)
        cue = {
            "schema_version": "wavecinema.cuesheet/1",
            "generated_by": "whisper",
            "whisper_model": model_name,
            "duration_ms": duration_ms,
            "words": words,
            "phrases": phrases,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["cue_sheet"] = cue
    if used == "heuristic":
        ws.log_event(f"FALLBACK: heuristic cue sheet used — {fallback_reason}")
    else:
        ws.log_event(
            f"transcribed with whisper ({model_name}): "
            f"{len(words)} words, {len(cue['phrases'])} phrases"
        )
    ws.save(expected_revision=rev)
    return cue
