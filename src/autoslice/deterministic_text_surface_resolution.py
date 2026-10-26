"""Public compatibility boundary: sealed candidate-specific text plans are private."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ID = ""
EXACT_COMBINED_SHA256 = ""
EXACT_HOOK = ""
EXACT_TITLE = ""
CONSUMPTION_SCHEMA_VERSION = "deterministic-text-narrowing-consumption.v1"
ASSET_DIRECTORY = Path("assets/lidousha/deterministic_text_surface_resolutions")

class DeterministicTextSurfaceResolutionError(ValueError):
    pass

def _unavailable(*_args, **_kwargs):
    raise DeterministicTextSurfaceResolutionError("DETERMINISTIC_TEXT_PRIVATE_AUTHORITY_UNAVAILABLE")

@dataclass(frozen=True, slots=True)
class DeterministicTextSurfaceAuthorityV1:
    document: dict[str, object]
    failed_source_fact_receipt: dict[str, object]
    adjudication_prompt: str
    repo_root: Path
    repo_path: Path
    file_sha256: str
    failed_receipt_repo_path: Path
    failed_receipt_file_sha256: str
    adjudication_prompt_repo_path: Path
    adjudication_prompt_file_sha256: str

@dataclass(frozen=True, slots=True)
class RenderedTextSurfaces:
    title: str
    selection_hook: str

def load_deterministic_text_surface_authority(candidate_id, *, root=ROOT):
    if not isinstance(candidate_id, str) or not candidate_id or Path(candidate_id).name != candidate_id:
        return None
    path = Path(root) / ASSET_DIRECTORY / (candidate_id + ".deterministic-text-narrowing.v1.json")
    if path.exists() or path.is_symlink():
        _unavailable()
    return None

validate_deterministic_text_surface_document = _unavailable
render_closed_surface_plan = _unavailable
consume_deterministic_text_surface_authority = _unavailable
