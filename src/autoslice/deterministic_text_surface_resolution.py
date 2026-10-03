"""Public compatibility boundary for omitted candidate-specific authority."""
from pathlib import Path

CANDIDATE_ID = "__private_authority_unavailable__"
CONSUMPTION_SCHEMA_VERSION = "deterministic-text-narrowing-consumption.v1"
EXACT_COMBINED_SHA256 = ""
EXACT_HOOK = ""
EXACT_TITLE = ""

class DeterministicTextSurfaceResolutionError(ValueError):
    pass

def load_deterministic_text_surface_authority(candidate_id, *, root=None):
    repo = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    authority_dir = repo / "assets/lidousha/deterministic_text_surface_resolutions"
    if authority_dir.is_symlink() or any(authority_dir.glob("*.json")):
        raise DeterministicTextSurfaceResolutionError("PRIVATE_TEXT_SURFACE_AUTHORITY_UNAVAILABLE")
    return None

def consume_deterministic_text_surface_authority(*args, **kwargs):
    raise DeterministicTextSurfaceResolutionError("PRIVATE_TEXT_SURFACE_AUTHORITY_UNAVAILABLE")
