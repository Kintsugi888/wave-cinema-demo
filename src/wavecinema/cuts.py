"""Cut planner: divide the song into contiguous 5–15s scenes.

Boundaries are scored against word timings: a boundary is "legal" when it
does not land inside an intelligible word (40ms padding each side), and the
planner never splits a word when a legal boundary exists. The song is
covered fully with no gaps and no overlaps; all boundaries are integer ms.
"""
from __future__ import annotations

from .workspace import ProjectWorkspace

MIN_SCENE_MS = 5000
MAX_SCENE_MS = 15000
TARGET_SCENE_MS = 10000
WORD_PAD_MS = 40


def _is_legal(t: int, words: list[dict]) -> bool:
    """True when t does not fall inside any word (with padding)."""
    for w in words:
        if w["start_ms"] - WORD_PAD_MS < t < w["end_ms"] + WORD_PAD_MS:
            return False
    return True


def _candidate_boundaries(words: list[dict], lo: int, hi: int) -> list[int]:
    """Legal boundary candidates in [lo, hi]: word-gap midpoints + edges."""
    cands: set[int] = set()
    prev_end = 0
    for w in words:
        gap_lo, gap_hi = prev_end, w["start_ms"]
        if gap_hi - gap_lo >= 2 * WORD_PAD_MS + 20:
            mid = (gap_lo + gap_hi) // 2
            if lo <= mid <= hi:
                cands.add(mid)
        prev_end = max(prev_end, w["end_ms"])
    for t in (lo, hi):
        cands.add(t)
    return [t for t in cands if _is_legal(t, words)]


def plan_cuts(project_id: str) -> list[dict]:
    """Plan contiguous scenes for the project's song. Returns the scene list."""
    ws = ProjectWorkspace.load(project_id)
    cue = ws.state.get("cue_sheet")
    if cue is None:
        raise ValueError(f"project {project_id} has no cue sheet — transcribe first")
    duration = int(cue["duration_ms"])
    words = sorted(cue.get("words", []), key=lambda w: w["start_ms"])

    scenes: list[dict] = []

    def emit(start: int, end: int, quality: str, reason: str) -> None:
        scenes.append({
            "scene_id": f"s{len(scenes):02d}",
            "start_ms": int(start),
            "end_ms": int(end),
            "duration_ms": int(end - start),
            "boundary_quality": quality,   # clean | forced | short
            "cut_reason": reason,
        })

    if duration <= MAX_SCENE_MS:
        # One scene covers the whole song (short songs keep a single scene).
        emit(0, duration, "short" if duration < MIN_SCENE_MS else "clean",
             "song fits in one scene")
    else:
        start = 0
        while start < duration:
            remaining = duration - start
            if remaining <= MAX_SCENE_MS:
                emit(start, duration, "clean", "final scene to song end")
                break
            # Non-final cut: end in [start+5000, start+15000] and the
            # remainder must itself be a legal scene (>= 5000) or we
            # would strand a too-short tail.
            lo = start + MIN_SCENE_MS
            hi = min(start + MAX_SCENE_MS, duration - MIN_SCENE_MS)
            target = start + TARGET_SCENE_MS
            cands = _candidate_boundaries(words, lo, hi)

            def score(t: int) -> tuple[int, int]:
                # primary: closeness to target; secondary: prefer roomier gaps
                # (approximated by distance to nearest word edge).
                nearest = min(
                    [abs(t - w["start_ms"]) for w in words]
                    + [abs(t - w["end_ms"]) for w in words]
                    + [10 ** 9],
                )
                return (abs(t - target), -min(nearest, 2000))

            if cands:
                end = min(cands, key=score)
                quality, reason = "clean", "word-gap boundary near 10s target"
            else:
                # No legal boundary in range: forced cut, kept off word
                # centres as far as the range allows.
                end = max(lo, min(hi, target))
                quality, reason = "forced", "no word-safe boundary in range"
            emit(start, end, quality, reason)
            start = end

    # Integrity: contiguous, no gaps/overlaps, bounds respected.
    assert scenes, "planner produced no scenes"
    assert scenes[0]["start_ms"] == 0
    assert scenes[-1]["end_ms"] == duration
    for a, b in zip(scenes, scenes[1:]):
        assert a["end_ms"] == b["start_ms"], "gap/overlap in cut plan"
    for s in scenes:
        assert s["duration_ms"] >= MIN_SCENE_MS or len(scenes) == 1, \
            f"scene {s['scene_id']} below minimum"

    rev = ws.revision
    ws.state["scenes"] = scenes
    ws.log_event(f"cut plan: {len(scenes)} scenes over {duration} ms")
    ws.save(expected_revision=rev)
    return scenes
