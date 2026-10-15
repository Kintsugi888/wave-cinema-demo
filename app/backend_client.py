"""Wave Cinema web UI — safe adapter over the backend package.

The backend package lives in ~/workspace/wave-cinema/src and exposes module
`wavecinema.api` (built in parallel with this UI). The UI must never crash on
import when the backend is absent or incomplete: this adapter degrades to a
branded "backend wiring in progress" state and every backend call surfaces its
exception as a user-facing Sharingan Red banner via bcall().
"""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SRC_DIR = (APP_DIR / ".." / "src").resolve()
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    import wavecinema.api as _api  # type: ignore
    BACKEND_OK = True
    IMPORT_ERROR: Exception | None = None
except Exception as e:  # backend being built in parallel — degrade, don't crash
    _api = None
    BACKEND_OK = False
    IMPORT_ERROR = e


class Backend:
    """Attribute-style proxy: backend.create_project(...) etc.

    Raises RuntimeError when the backend package is not importable, and
    AttributeError (with a friendly message) when a named function is missing
    from wavecinema.api. Callers must use bcall() and never let these escape.
    """

    def __init__(self) -> None:
        self.ok = BACKEND_OK
        self.import_error = IMPORT_ERROR

    def _call(self, name, *args, **kwargs):
        if not self.ok:
            raise RuntimeError(
                "Backend wiring in progress: wavecinema.api is not importable yet "
                f"({self.import_error}). The pipeline will activate once the backend lands."
            )
        try:
            fn = getattr(_api, name)
        except AttributeError:
            raise AttributeError(
                f"wavecinema.api has no function '{name}' yet — backend build in progress."
            )
        return fn(*args, **kwargs)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: self._call(name, *a, **k)


def bcall(fn, *args, **kwargs):
    """Call a backend function; return (True, result) or (False, message)."""
    try:
        return True, fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — surfaced to the user as a banner
        return False, f"{type(e).__name__}: {e}"
