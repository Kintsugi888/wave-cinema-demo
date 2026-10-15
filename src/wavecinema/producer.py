"""In-project AI Producer: a project-aware creative directing partner.

The Producer answers directing questions with full knowledge of the
project's current state: song, cue sheet, scenes, characters, plan, and
pipeline stage. It uses an LLM when one is configured (ANTHROPIC_API_KEY
or OPENAI_API_KEY), and falls back to a smart rule-based engine that
gives genuinely useful, state-grounded directing advice.
"""
from __future__ import annotations

import os
import re
import urllib.request
import json
from datetime import datetime, timezone

from .workspace import ProjectWorkspace

CHAT_KEY = "producer_chat"  # workspace state key for chat history
MAX_HISTORY = 100  # bounded chat history per project


# ------------------------------------------------------------- LLM backends

def _llm_available() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return None


def _project_context(project_id: str) -> str:
    """Compact project summary for the Producer's system prompt."""
    ws = ProjectWorkspace.load(project_id)
    s = ws.state
    song = s.get("song") or {}
    scenes = (s.get("plan") or {}).get("scenes") or s.get("scenes") or []
    chars = s.get("characters") or {}
    cue = s.get("cue_sheet") or {}

    lines = [
        f"Project: {s.get('name', project_id)}",
        f"Song: {song.get('filename', 'none')} "
        f"({int(song.get('duration_ms', 0)) // 1000}s)",
        f"Scenes: {len(scenes)}",
        f"Characters: {', '.join(c.get('name', k) for k, c in chars.items()) or 'none'}",
        f"Transcription: {cue.get('generated_by', 'none')} "
        f"({len(cue.get('words', []))} words)",
        f"Aspect ratio: {s.get('aspect_ratio', '16:9')}",
        f"Resolution: {s.get('resolution', '1080p')}",
    ]
    # Stage detection (mirrors the UI).
    if not song:
        lines.append("Stage: ingest (no song yet)")
    elif not scenes:
        lines.append("Stage: direct (no scenes planned)")
    else:
        takes = sum(len(v) for v in (s.get("takes") or {}).values())
        lines.append(f"Stage: review/export ({takes} takes generated)")
    for i, sc in enumerate(scenes[:12]):
        cast = sc.get("cast", "")
        if isinstance(cast, list):
            cast = ", ".join(str(c) for c in cast)
        prompt = str(sc.get("prompt", ""))[:120]
        lines.append(
            f"  Scene {i} ({sc.get('scene_id', i)}): "
            f"{sc.get('start_ms', 0)}-{sc.get('end_ms', 0)}ms, "
            f"cast=[{cast}], prompt={prompt!r}"
        )
    if len(scenes) > 12:
        lines.append(f"  ... and {len(scenes) - 12} more scenes")
    return "\n".join(lines)


_SYSTEM_PROMPT = """You are the Producer, the in-house creative director for \
Wave Cinema, an AI music-video workstation. A filmmaker is directing their \
music video and asks you questions. Answer with concrete, actionable \
directing advice — shot language, pacing, character blocking, visual \
continuity. Be concise (under 150 words unless they ask for detail). \
Reference their actual scenes, characters, and song timing when relevant. \
Never invent scenes or characters that are not in the project context. \
If they ask about something outside filmmaking, answer briefly and steer \
back to the film."""


def _llm_reply(message: str, context: str, history: list[dict]) -> str | None:
    """Try an LLM backend; return None on any failure (caller falls back)."""
    which = _llm_available()
    if not which:
        return None
    try:
        if which == "anthropic":
            return _anthropic_reply(message, context, history)
        return _openai_reply(message, context, history)
    except Exception:
        return None


def _anthropic_reply(message: str, context: str, history: list[dict]) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    msgs = []
    for h in history[-8:]:
        msgs.append({"role": h["role"], "content": h["message"]})
    msgs.append({"role": "user", "content": message})
    payload = {
        "model": os.environ.get("WAVECINEMA_PRODUCER_MODEL", "claude-haiku-4-5"),
        "max_tokens": 400,
        "system": _SYSTEM_PROMPT + "\n\nPROJECT CONTEXT:\n" + context,
        "messages": msgs,
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    blocks = data.get("content", [])
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(texts).strip() or "I could not form a reply just now."


def _openai_reply(message: str, context: str, history: list[dict]) -> str:
    key = os.environ["OPENAI_API_KEY"]
    msgs = [{"role": "system",
             "content": _SYSTEM_PROMPT + "\n\nPROJECT CONTEXT:\n" + context}]
    for h in history[-8:]:
        msgs.append({"role": h["role"], "content": h["message"]})
    msgs.append({"role": "user", "content": message})
    payload = {
        "model": os.environ.get("WAVECINEMA_PRODUCER_MODEL", "gpt-4o-mini"),
        "max_tokens": 400,
        "messages": msgs,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"authorization": f"Bearer {key}",
                 "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return (data["choices"][0]["message"]["content"] or "").strip()


# ------------------------------------------------------------- rule-based engine

def _scene_stats(project_id: str) -> dict:
    ws = ProjectWorkspace.load(project_id)
    s = ws.state
    scenes = (s.get("plan") or {}).get("scenes") or s.get("scenes") or []
    durations = []
    for sc in scenes:
        try:
            durations.append((int(sc["end_ms"]) - int(sc["start_ms"])) / 1000.0)
        except (KeyError, TypeError, ValueError):
            continue
    chars = s.get("characters") or {}
    song = s.get("song") or {}
    cue = s.get("cue_sheet") or {}
    takes = s.get("takes") or {}
    return {
        "scene_count": len(scenes),
        "durations": durations,
        "avg_dur": sum(durations) / len(durations) if durations else 0,
        "characters": [c.get("name", k) for k, c in chars.items()],
        "song_name": song.get("filename", ""),
        "song_s": int(song.get("duration_ms", 0)) // 1000,
        "words": len(cue.get("words", [])),
        "transcribed": bool(cue.get("words")),
        "takes": sum(len(v) for v in takes.values()),
        "has_plan": bool((s.get("plan") or {}).get("scenes")),
        "aspect": s.get("aspect_ratio", "16:9"),
    }


def _rule_reply(project_id: str, message: str) -> str:
    """Genuinely useful directing advice grounded in live project state."""
    st = _scene_stats(project_id)
    msg = message.lower()
    n = st["scene_count"]

    def scene_word():
        return "scene" if n == 1 else "scenes"

    # --- greetings / orientation -------------------------------------
    if re.search(r"\b(hi|hello|hey|yo|sup)\b", msg) and len(msg.split()) < 6:
        bits = [
            f"Hey — Producer here. Your film has {n} {scene_word()}",
        ]
        if st["characters"]:
            bits.append(f"starring {', '.join(st['characters'])}")
        if st["song_name"]:
            bits.append(f"cut to {st['song_name']} ({st['song_s']}s)")
        bits.append("Ask me about pacing, a specific scene, character blocking, "
                    "or what to do next.")
        return " ".join(bits) + "."

    # --- what next / stage guidance ----------------------------------
    if re.search(r"what.{0,12}(next|now|do)|where.{0,8}(start|begin)|stuck|help", msg):
        if not st["song_name"]:
            return ("First move: upload your track on the Ingest stage and tick the "
                    "rights box. Everything downstream — transcription, scene planning, "
                    "generation — keys off the song.")
        if not st["has_plan"]:
            return ("Next: hit 'Build my game plan' on the Direct stage. I'll transcribe "
                    "the track, cut it into timed scenes, and write a shot prompt per "
                    "scene with your cast baked in. Then come back and we'll talk "
                    "through the plan scene by scene.")
        if st["takes"] == 0:
            return (f"The plan is locked with {n} {scene_word()}. Next: quote scene 0 "
                    "and approve it to generate your first take — that unlocks the rest. "
                    "Start with your strongest visual idea; the first take sets the "
                    "film's look.")
        return ("Takes are rolling. Next: review each scene's takes, pick winners, "
                "reroll anything off-model, then Assemble. Want notes on a specific "
                "scene? Tell me the scene number.")

    # --- pacing -------------------------------------------------------
    if re.search(r"pac(e|ing)|rhythm|too (fast|slow|long|short)|boring|drag", msg):
        if not st["durations"]:
            return ("Pacing lives in the cut plan — run the director first and I'll "
                    "break down your scene rhythm.")
        avg = st["avg_dur"]
        longest = max(st["durations"])
        shortest = min(st["durations"])
        advice = [
            f"Your {n} {scene_word()} average {avg:.1f}s "
            f"(range {shortest:.1f}s–{longest:.1f}s)."
        ]
        if avg > 8:
            advice.append("That's on the slow side for a music video — consider "
                          "splitting the longest scenes so cuts land on beat hits.")
        elif avg < 4:
            advice.append("That's cutty — great for high-energy sections, but give "
                          "emotional beats room to breathe (6-8s holds).")
        else:
            advice.append("Healthy middle ground. Vary it deliberately: quick cuts "
                          "for verses, longer holds for the hook.")
        if longest > 2 * avg:
            advice.append(f"One scene runs {longest:.1f}s — that's your drag risk; "
                          "either split it or give it a strong internal camera move.")
        return " ".join(advice)

    # --- specific scene ------------------------------------------------
    m = re.search(r"scene\s*(\d+)", msg)
    if m:
        idx = int(m.group(1))
        ws = ProjectWorkspace.load(project_id)
        scenes = (ws.state.get("plan") or {}).get("scenes") or ws.state.get("scenes") or []
        if idx < len(scenes):
            sc = scenes[idx]
            cast = sc.get("cast", "")
            if isinstance(cast, list):
                cast = ", ".join(str(c) for c in cast)
            dur = (int(sc.get("end_ms", 0)) - int(sc.get("start_ms", 0))) / 1000.0
            prompt = str(sc.get("prompt", ""))[:200]
            notes = [f"Scene {idx} runs {dur:.1f}s"]
            notes.append(f"cast: {cast or 'no one on screen'}")
            if dur > 8:
                notes.append("it's long — make sure the prompt has an internal "
                             "camera move or a two-beat action so it doesn't sit still")
            if not cast or cast == "-":
                notes.append("no cast listed — if someone should be on screen, "
                             "add a character and re-run the director")
            if prompt:
                notes.append(f"prompt: {prompt}…")
            takes = (ws.state.get("takes") or {}).get(sc.get("scene_id", ""), [])
            if takes:
                notes.append(f"{len(takes)} take(s) generated — review them before rerolling")
            else:
                notes.append("no takes yet — quote and approve to generate")
            return ". ".join(notes) + "."
        return (f"You only have {n} {scene_word()} (0–{n - 1}). "
                "Ask about one of those.")

    # --- characters ----------------------------------------------------
    if re.search(r"character|cast|actor|who.{0,10}(in|on screen)|blocking", msg):
        if not st["characters"]:
            return ("No cast yet — add character references on the Direct stage "
                    "(up to 10). The director writes them into every shot prompt, "
                    "and the generator gets up to 9 reference images per scene. "
                    "Consistent faces are what separate amateur from pro here.")
        return (f"Cast: {', '.join(st['characters'])}. Keep them on screen for "
                f"the hook sections — faces carry choruses. For verses, you can "
                f"cut away to environment or detail shots for contrast. If a take "
                f"comes back off-model, reroll it; never ship a take where the "
                f"face drifts.")

    # --- song / lyrics -------------------------------------------------
    if re.search(r"lyric|word|song|track|music|beat|chorus|verse|hook", msg):
        bits = []
        if st["transcribed"]:
            bits.append(f"Transcribed {st['words']} words with timing")
        if st["song_name"]:
            bits.append(f"{st['song_name']} ({st['song_s']}s)")
        head = " ".join(bits) + ". " if bits else ""
        return (head + "Cut on the beat: scene boundaries should land on bar lines "
                "or lyric phrases, never mid-word. Put your biggest visual swing on "
                "the first chorus hit — that's the moment viewers decide to keep "
                "watching. Verses can be visually sparser; save the spectacle for "
                "the hooks.")

    # --- visual style / prompts ----------------------------------------
    if re.search(r"style|look|visual|aesthetic|prompt|camera|light|color|colour", msg):
        return ("Prompt craft: lead with the subject and action, then camera "
                "(lens, move), then light and palette. Your brand palette is deep "
                "navy shadows, electric blue key light, chrome silver reflections — "
                "the director already bakes that in. One strong idea per scene beats "
                "three weak ones. And keep the identity locks verbatim; that's what "
                "holds faces together across takes.")

    # --- generation / takes --------------------------------------------
    if re.search(r"take|reroll|generate|render|quote|credit", msg):
        return ("Generation loop: quote a scene (exact price, 15-min expiry), approve "
                "to generate one take. Reroll for alternates — each reroll is a fresh "
                "quote. Only the takes you select get assembled, so generate freely "
                "on the scenes that matter and be frugal on connective tissue. "
                "BYOK lane: your own key, zero credits.")

    # --- aspect ratio ----------------------------------------------------
    if re.search(r"aspect|vertical|square|ultrawide|9:16|16:9|1:1", msg):
        return (f"You're cutting {st['aspect']}. 16:9 is the cinema default; 9:16 "
                "vertical is for Shorts/Reels/TikTok — compose with the subject "
                "center-frame since the sides get cropped on every other surface. "
                "1:1 works for feed posts. Pick the ratio for where this film will "
                "live most.")

    # --- fallback: grounded orientation ----------------------------------
    return ("Here's where you stand: "
            f"{n} {scene_word()}, "
            f"{'starring ' + ', '.join(st['characters']) if st['characters'] else 'no cast yet'}, "
            f"{st['takes']} takes generated. "
            "Ask me about pacing, a specific scene ('what about scene 2'), "
            "character blocking, or what to do next.")


# ------------------------------------------------------------- public API

def get_history(project_id: str) -> list[dict]:
    ws = ProjectWorkspace.load(project_id)
    return list(ws.state.get(CHAT_KEY, []))


def chat(project_id: str, message: str) -> dict:
    """Send a message to the Producer; returns {reply, history}."""
    message = (message or "").strip()
    if not message:
        raise ValueError("message cannot be empty")
    if len(message) > 2000:
        raise ValueError("message too long (max 2000 characters)")

    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    history = list(ws.state.get(CHAT_KEY, []))

    # Try LLM first, fall back to the rule engine.
    context = _project_context(project_id)
    reply = _llm_reply(message, context, history)
    source = "llm"
    if not reply:
        reply = _rule_reply(project_id, message)
        source = "producer"

    now = datetime.now(timezone.utc).isoformat()
    history.append({"role": "user", "message": message, "at": now})
    history.append({"role": "assistant", "message": reply, "at": now,
                    "source": source})
    if len(history) > MAX_HISTORY:
        del history[: len(history) - MAX_HISTORY]
    ws.state[CHAT_KEY] = history
    ws.log_event(f"producer chat: user asked ({len(message)} chars), "
                 f"replied via {source}")
    ws.save(expected_revision=rev)
    return {"reply": reply, "source": source,
            "history": history[-20:]}
