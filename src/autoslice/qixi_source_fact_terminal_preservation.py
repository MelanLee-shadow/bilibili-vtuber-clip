# Private operator evidence is not part of the public snapshot.
from __future__ import annotations
from pathlib import Path

class QixiSourceFactTerminalPreservationError(ValueError):
    reason_code = 'QIXI_SOURCE_FACT_TERMINAL_PRESERVATION_PRIVATE_AUTHORITY_UNAVAILABLE'

CANDIDATE_ID = ""
AUTHORITY_RELATIVE_PATH = Path("assets/_template/private-authority-unavailable.json")
FINALIZATION_AUTHORITY_RELATIVE_PATH = Path("assets/_template/private-authority-unavailable.json")
SCHEMA = 'qixi-source-fact-terminal-preservation-authority.v1'
CONSUMPTION_SCHEMA = 'qixi-source-fact-terminal-preservation-consumption.v1'
SOURCE_FACT_SCHEMA = 'lidousha-source-fact-review.v1'
DECISION = 'OPERATOR_TERMINAL_TEXT_PRESERVATION'
_SHA = ""

def _unavailable(*args, **kwargs):
    raise QixiSourceFactTerminalPreservationError('QIXI_SOURCE_FACT_TERMINAL_PRESERVATION_PRIVATE_AUTHORITY_UNAVAILABLE')

def _not_applicable(*args, **kwargs):
    return False

_canonical_sha = _unavailable
_text_sha = _unavailable
_normal_sha = _unavailable
_load_authority = _unavailable
_bound_finalization_authority_sha = _unavailable
validate_terminal_preservation_finalization_authority = _unavailable
_transcript = _unavailable
_cue_rows_match = _unavailable
_validate_authority = _unavailable
build_terminal_preservation_review = _unavailable
validate_terminal_preservation_review = _unavailable
validate_terminal_preservation_source_fact_review = _unavailable
validate_terminal_preservation_finalization_binding = _unavailable
