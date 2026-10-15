"""Director: production bible + timed scene plan.

DeterministicDirector is fully deterministic (no network): the same
treatment + cue sheet + scenes always produce the same bible and plan.
AgentDirector is the interface a future LLM-backed director implements;
the pipeline defaults to the deterministic one.
"""
from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod

from .errors import SceneNotFound
from .workspace import ProjectWorkspace

PLAN_SCHEMA = "wavecinema.plan/1"

# ---- Original creative banks (authored for Wave Cinema; nothing copied) ----

_VISUAL_LANGUAGES = [
    "neon-noir stagecraft: deep-navy voids, electric-blue rim light, chrome reflections",
    "midnight chrome dreamscape: glossy black surfaces, drifting silver mist, blue signal flares",
    "ink-and-static anime realism: heavy blacks, paper-grain texture, sudden blue light leaks",
    "Cathedral of bass: vast dark architecture, columns of blue light pulsing with the low end",
]

_CAMERA_MOVES = [
    "slow orbital dolly around the performer, 180 degrees over the scene",
    "locked-off wide shot, subject drifts through frame left to right",
    "low-angle push-in, lens breathing with each downbeat",
    "overhead crane descent settling into a medium close-up",
    "lateral tracking shot, background parallax layers at three depths",
    "static close-up, rack focus between foreground rain and the performer",
]

_LEAD_LOCKS = [
    "LEAD wears a matte-black cropped jacket with a single electric-blue chest stripe; "
    "silver chain at the throat; hair in two tight braids with chrome cuffs; "
    "eyes always rendered with a faint blue catchlight, never brown, never hidden by hair.",
    "LEAD wears an oversized deep-navy bomber, sleeves pushed to the elbow; "
    "chrome-silver hoop earrings; short cropped hair with a hard side part; "
    "a thin blue LED strip traces the jacket collar in every shot.",
    "LEAD wears a black turtleneck under a chrome-silver harness vest; "
    "long straight hair with blue-dipped ends; no facial hair; "
    "skin rendered with soft rim light, never flat-lit.",
]

_ENSEMBLE_LOCKS = [
    "ENSEMBLE figures are faceless silhouettes in deep navy, backlit by electric blue; "
    "they never face the camera and never share the LEAD's wardrobe details.",
]

_SET_LOCKS = [
    "Every set carries one chrome-silver reflective surface and one electric-blue "
    "practical light source; no daylight exteriors, no wooden textures.",
]

_FORBIDDEN = [
    "no on-screen text, captions, logos, or watermarks of any kind",
    "no morphing or transforming faces between shots",
    "no extra limbs, fused hands, or distorted instruments",
    "no photorealistic celebrity likenesses",
    "no daylight or pastel palettes — the film lives at night",
    "no repeated identical compositions across consecutive scenes",
]

_ARC_BEGINNINGS = [
    "A lone figure wakes inside a dark machine-city as the first blue signal fires.",
    "Static on a black screen resolves into a chrome corridor stretching down.",
    "Rain on black glass; behind it, a stage nobody built begins to glow blue.",
]
_ARC_ESCALATIONS = [
    "The signal multiplies — every surface answers, the city starts to move with the beat.",
    "The LEAD walks deeper; silhouettes gather at the edges of the light.",
    "The machine-city inhales: architecture bends toward the rhythm, chrome warps like water.",
]
_ARC_CLIMAXES = [
    "Total overload — every light source fires at once, the LEAD at the exact centre of the blast.",
    "The silhouettes step into the light as one; the LEAD raises both arms and the city holds its breath.",
    "Gravity lets go: debris, rain and light hang frozen while the LEAD delivers the final line.",
]
_ARC_FINAL_IMAGES = [
    "One blue light left burning in the dark. The LEAD walks toward it, out of frame.",
    "The corridor collapses to a single chrome door, closing on the last note.",
    "Black screen. A faint blue catchlight blinks once, then nothing.",
]


def _rng(treatment: str) -> random.Random:
    seed = int.from_bytes(hashlib.sha256(treatment.encode("utf-8")).digest()[:8], "big")
    return random.Random(seed)


def _words_in_scene(cue_sheet: dict, scene: dict) -> list[dict]:
    return [
        w for w in cue_sheet.get("words", [])
        if scene["start_ms"] <= w["start_ms"] < scene["end_ms"]
    ]


def _scene_cast(cue_sheet: dict, scene: dict) -> list[str]:
    words = _words_in_scene(cue_sheet, scene)
    if words:
        return ["LEAD"]
    return ["INSTRUMENTAL — no vocalist on screen"]


class DeterministicDirector:
    """Deterministic director: treatment + cue sheet + scenes -> bible + plan.

    No network, no randomness beyond the treatment hash: identical inputs
    always yield identical outputs.
    """

    def __init__(self, treatment: str, cue_sheet: dict, scenes: list[dict],
                 characters: list[dict] | None = None,
                 backgrounds: list[dict] | None = None):
        self.treatment = treatment
        self.cue_sheet = cue_sheet
        self.scenes = scenes
        self.characters = list(characters or [])
        self.backgrounds = list(backgrounds or [])
        self.rng = _rng(treatment or "")

    # -- bible ---------------------------------------------------------
    def _bible(self) -> dict:
        n = len(self.scenes)
        bible = {
            "visual_language": self.rng.choice(_VISUAL_LANGUAGES),
            "camera_grammar": (
                "One deliberate camera move per scene, chosen from the scene "
                "grammar below; no handheld shake; cuts land on phrase "
                "boundaries, never mid-word."
            ),
            "character_identity_locks": [
                self.rng.choice(_LEAD_LOCKS),
                self.rng.choice(_ENSEMBLE_LOCKS),
                self.rng.choice(_SET_LOCKS),
            ],
            "continuity_anchors": [
                "The LEAD's wardrobe and hair are identical in every scene "
                "(see identity locks); verify before each generation.",
                "The electric-blue practical light appears in every scene, "
                "motivating all blue highlights.",
                "Screen direction is preserved: the LEAD exits frame right "
                "only to enter frame left in the next scene.",
            ],
            "forbidden_elements": list(_FORBIDDEN),
            "story_arc": {
                "beginning": self.rng.choice(_ARC_BEGINNINGS),
                "escalation": self.rng.choice(_ARC_ESCALATIONS),
                "climax": self.rng.choice(_ARC_CLIMAXES),
                "final_image": self.rng.choice(_ARC_FINAL_IMAGES),
            },
            "treatment_echo": (self.treatment or "").strip()[:500],
            "scene_count": n,
        }
        return bible

    # -- timed plan ----------------------------------------------------
    def _cast_reference_line(self) -> str:
        """Character-consistency line from uploaded reference images."""
        if not self.characters:
            return ""
        names = ", ".join(c.get("name", "?") for c in self.characters)
        return (
            f"CHARACTER REFERENCES: the cast ({names}) has uploaded reference "
            f"images — match each named character's face, hair, eyes, skin tone "
            f"and wardrobe to their reference EXACTLY in every shot. No "
            f"morphing between references; the same character looks identical "
            f"across all scenes. "
        )

    def _location_reference_line(self) -> str:
        """Environment-consistency line from uploaded background references."""
        if not self.backgrounds:
            return ""
        names = ", ".join(b.get("name", "?") for b in self.backgrounds)
        return (
            f"LOCATION REFERENCES: the production ({names}) has uploaded "
            f"environment reference images — match each named location's "
            f"architecture, lighting mood, palette and key set dressing to "
            f"its reference EXACTLY in every shot set there. No morphing "
            f"between references; the same location looks identical across "
            f"all scenes. "
        )

    def _prompt_for(self, bible: dict, scene: dict, index: int) -> str:
        words = _words_in_scene(self.cue_sheet, scene)
        cast = _scene_cast(self.cue_sheet, scene)
        dur_s = scene["duration_ms"] / 1000.0
        move = _CAMERA_MOVES[index % len(_CAMERA_MOVES)]
        locks = " ".join(bible["character_identity_locks"])
        if words:
            lyric_bits = " ".join(
                f"{w['word']}@{w['start_ms'] - scene['start_ms']}ms" for w in words[:24]
            )
            lyric_line = (
                f"Vocal line (scene-local ms, LEAD lip-syncs exactly these onsets): "
                f"{lyric_bits}. "
            )
        else:
            lyric_line = (
                "No vocals in this scene: the LEAD's mouth stays closed and "
                "still; pure performance/atmosphere shot. "
            )
        arc = bible["story_arc"]
        beat = arc["beginning"] if index < len(self.scenes) / 4 else (
            arc["escalation"] if index < 3 * len(self.scenes) / 4 else arc["climax"]
        )
        if index == len(self.scenes) - 1:
            beat = arc["final_image"]
        return (
            f"Wave Cinema scene {scene['scene_id']} ({dur_s:.1f}s, 16:9, night). "
            f"VISUAL LANGUAGE: {bible['visual_language']}. "
            f"STORY BEAT: {beat} "
            f"CAST ON SCREEN: {', '.join(cast)}. "
            f"{self._cast_reference_line()}"f"{self._location_reference_line()}"
            f"IDENTITY LOCKS (must hold exactly): {locks} "
            f"CAMERA: {move}. "
            f"{lyric_line}"
            f"PALETTE: deep navy #142B52 shadows, electric blue #1677FF key light, "
            f"chrome silver #C5D0E0 reflections. "
            f"FORBIDDEN: {'; '.join(bible['forbidden_elements'])}."
        )

    def plan(self) -> tuple[dict, dict]:
        """Return (bible, timed_plan)."""
        bible = self._bible()
        plan_scenes = []
        for i, scene in enumerate(self.scenes):
            plan_scenes.append({
                "scene_id": scene["scene_id"],
                "start_ms": scene["start_ms"],
                "end_ms": scene["end_ms"],
                "cast": _scene_cast(self.cue_sheet, scene),
                "prompt": self._prompt_for(bible, scene, i),
            })
        plan = {"schema_version": PLAN_SCHEMA, "scenes": plan_scenes}
        return bible, plan


class AgentDirector(ABC):
    """Interface for a future LLM-backed director.

    Any implementation must accept (treatment, cue_sheet, scenes) and return
    (bible, timed_plan) with the same shapes DeterministicDirector.plan()
    produces: bible carries visual_language, camera_grammar,
    character_identity_locks (list), continuity_anchors, forbidden_elements
    and story_arc {beginning, escalation, climax, final_image}; timed_plan
    carries schema_version and scenes [{scene_id, start_ms, end_ms, cast,
    prompt}] with identity locks restated in every prompt.
    """

    def __init__(self, treatment: str, cue_sheet: dict, scenes: list[dict]):
        self.treatment = treatment
        self.cue_sheet = cue_sheet
        self.scenes = scenes

    @abstractmethod
    def plan(self) -> tuple[dict, dict]:
        """Return (bible, timed_plan)."""
        raise NotImplementedError


def direct_project(project_id: str, treatment: str,
                   director_cls: type = DeterministicDirector) -> dict:
    """Run the director for a project and store bible + timed plan."""
    ws = ProjectWorkspace.load(project_id)
    if not ws.state.get("scenes"):
        raise SceneNotFound(f"project {project_id} has no cut plan — plan cuts first")
    if ws.state.get("cue_sheet") is None:
        raise ValueError(f"project {project_id} has no cue sheet — transcribe first")
    characters = list((ws.state.get("characters") or {}).values())
    backgrounds = list((ws.state.get("backgrounds") or {}).values())
    director = director_cls(treatment, ws.state["cue_sheet"], ws.state["scenes"],
                            characters=characters, backgrounds=backgrounds)
    bible, plan = director.plan()
    rev = ws.revision
    ws.state["bible"] = bible
    ws.state["plan"] = plan
    ws.log_event(
        f"directed with {director_cls.__name__}: "
        f"{len(plan['scenes'])} scene prompts, {len(bible['character_identity_locks'])} identity locks"
    )
    ws.save(expected_revision=rev)
    return {"bible": bible, "plan": plan}
