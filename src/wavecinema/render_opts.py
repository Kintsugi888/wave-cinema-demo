"""Project render options: aspect ratio + output resolution.

Aspect ratios (SLOPCANNON parity): 16:9 default, 9:16 vertical, 1:1 square,
21:9 ultrawide, 4:3, 3:4 portrait.

Resolutions: 1080p default, 2K option. 2K costs 1.5x the 1080p credit rate
(see quote.RESOLUTION_MULTIPLIERS).
"""
from __future__ import annotations

from .workspace import ProjectWorkspace

ASPECT_RATIOS = ("16:9", "9:16", "1:1", "21:9", "4:3", "3:4")
DEFAULT_ASPECT = "16:9"

RESOLUTIONS = ("1080p", "2K")
DEFAULT_RESOLUTION = "1080p"

# Credit multiplier per resolution (applied in quote.price_for_duration_ms).
RESOLUTION_MULTIPLIERS = {"1080p": 1.0, "2K": 1.5}

# Render dimensions per aspect (width, height) at 1080p baseline.
ASPECT_DIMS = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "21:9": (2560, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
}


def get_aspect_ratio(project_id: str) -> str:
    ws = ProjectWorkspace.load(project_id)
    a = ws.state.get("aspect_ratio", DEFAULT_ASPECT)
    return a if a in ASPECT_RATIOS else DEFAULT_ASPECT


def set_aspect_ratio(project_id: str, aspect: str) -> dict:
    if aspect not in ASPECT_RATIOS:
        raise ValueError(
            f"unknown aspect ratio {aspect!r} (expected one of {ASPECT_RATIOS})")
    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["aspect_ratio"] = aspect
    ws.log_event(f"aspect ratio set to {aspect}")
    ws.save(expected_revision=rev)
    return {"project_id": project_id, "aspect_ratio": aspect}


def get_resolution(project_id: str) -> str:
    ws = ProjectWorkspace.load(project_id)
    r = ws.state.get("resolution", DEFAULT_RESOLUTION)
    return r if r in RESOLUTIONS else DEFAULT_RESOLUTION


def set_resolution(project_id: str, resolution: str) -> dict:
    if resolution not in RESOLUTIONS:
        raise ValueError(
            f"unknown resolution {resolution!r} (expected one of {RESOLUTIONS})")
    ws = ProjectWorkspace.load(project_id)
    rev = ws.revision
    ws.state["resolution"] = resolution
    ws.log_event(f"resolution set to {resolution}")
    ws.save(expected_revision=rev)
    return {"project_id": project_id, "resolution": resolution}


def get_render_opts(project_id: str) -> dict:
    """Both options at once, for prompts and job packs."""
    return {
        "aspect_ratio": get_aspect_ratio(project_id),
        "resolution": get_resolution(project_id),
        "dimensions": ASPECT_DIMS[get_aspect_ratio(project_id)],
        "credit_multiplier": RESOLUTION_MULTIPLIERS[get_resolution(project_id)],
    }
