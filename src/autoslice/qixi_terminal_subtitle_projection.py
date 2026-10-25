# Private operator evidence is not part of the public snapshot.
from __future__ import annotations
from pathlib import Path

class TerminalProjectionError(ValueError):
    reason_code = 'QIXI_TERMINAL_SUBTITLE_PROJECTION_PRIVATE_AUTHORITY_UNAVAILABLE'

class _ProjectionAssets(ValueError):
    reason_code = 'QIXI_TERMINAL_SUBTITLE_PROJECTION_PRIVATE_AUTHORITY_UNAVAILABLE'

TERMINAL_PROJECTION_RELATIVE_PATH = Path("assets/_template/private-authority-unavailable.json")
SCHEMA = 'qixi-reviewed-terminal-subtitle-projection.v1'
_SHA_LEN = 0

def _unavailable(*args, **kwargs):
    raise TerminalProjectionError('QIXI_TERMINAL_SUBTITLE_PROJECTION_PRIVATE_AUTHORITY_UNAVAILABLE')

def _not_applicable(*args, **kwargs):
    return False

_canonical_sha = _unavailable
_sha256 = _unavailable
_normal_sha = _unavailable
_safe_relative = _unavailable
_contained_regular = _unavailable
_sealed_repository_bytes = _unavailable
_srt_bytes = _unavailable
_projection_sha_entry = _unavailable
load_projection = _unavailable
_sealed_json = _unavailable
_truth_lane = _unavailable
_validate_truth_sidecars = _unavailable
_validate_current_extras = _unavailable
_require_current_cue_authorities = _unavailable
_validate_terminal_boundary = _unavailable
_sealed_srt = _unavailable
_validate_preimage = _unavailable
_replay_projection_assets = _unavailable
validate_projection_assets = _unavailable
validate_projection = _unavailable
build_terminal_chat = _unavailable
