"""Timed captions: cue sheet words -> SRT download + display model.

The cue sheet already carries word-level timestamps (whisper) or a
clearly-marked heuristic fallback (no words). This module turns either
into captions:
  - whisper words  -> word-grouped caption cues (up to ~8 words / cue)
  - heuristic      -> one cue per instrumental phrase, clearly labeled
                     as estimated timing
"""
from __future__ import annotations

from .workspace import ProjectWorkspace

MAX_WORDS_PER_CUE = 8


def _srt_ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, mm = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{mm:03d}"


def build_captions(project_id: str) -> dict:
    """Return {cues, srt, estimated, word_count}.

    cues: [{n, start_ms, end_ms, text}] for display.
    srt: full SRT file text for download.
    estimated: True when timing is heuristic (no word timestamps).
    """
    ws = ProjectWorkspace.load(project_id)
    cue = ws.state.get("cue_sheet")
    if not cue:
        raise ValueError(f"project {project_id} has no cue sheet — transcribe first")

    words = [w for w in (cue.get("words") or []) if w.get("word")]
    cues: list[dict] = []
    estimated = False

    if words:
        # Group words into caption cues (max N words each).
        cur: list[dict] = []
        for w in words:
            cur.append(w)
            if len(cur) >= MAX_WORDS_PER_CUE:
                cues.append({
                    "start_ms": cur[0]["start_ms"],
                    "end_ms": cur[-1]["end_ms"],
                    "text": " ".join(x["word"] for x in cur),
                })
                cur = []
        if cur:
            cues.append({
                "start_ms": cur[0]["start_ms"],
                "end_ms": cur[-1]["end_ms"],
                "text": " ".join(x["word"] for x in cur),
            })
    else:
        # Heuristic fallback: one cue per phrase, clearly estimated.
        estimated = True
        for p in cue.get("phrases") or []:
            owner = p.get("owner", "")
            label = {"LEAD": "[vocals]", "ENSEMBLE": "[ensemble]",
                     "INSTRUMENTAL": "[instrumental]"}.get(owner, "")
            cues.append({
                "start_ms": int(p.get("start_ms", 0)),
                "end_ms": int(p.get("end_ms", 0)),
                "text": f"{label} (timing estimated)".strip(),
            })

    out = []
    for i, c in enumerate(cues, 1):
        c["n"] = i
        out.append(f"{i}\n{_srt_ts(c['start_ms'])} --> "
                   f"{_srt_ts(c['end_ms'])}\n{c['text']}\n")
    return {
        "cues": cues,
        "srt": "\n".join(out),
        "estimated": estimated,
        "word_count": len(words),
        "cue_count": len(cues),
        "generated_by": cue.get("generated_by", "unknown"),
    }
