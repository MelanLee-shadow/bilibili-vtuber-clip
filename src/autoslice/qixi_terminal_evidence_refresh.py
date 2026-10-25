# Private operator evidence is not part of the public snapshot.
from __future__ import annotations
from pathlib import Path

class QixiTerminalEvidenceRefreshError(ValueError):
    reason_code = 'QIXI_TERMINAL_EVIDENCE_REFRESH_PRIVATE_AUTHORITY_UNAVAILABLE'

CANDIDATE_ID = ""
RECORDING_DATE = ""
ROOT = Path("assets/_template/private-authority-unavailable.json")
AUTHORITY_PATH = Path("assets/_template/private-authority-unavailable.json")
HUMAN_TRUTH_AUTHORITY_PATH = Path("assets/_template/private-authority-unavailable.json")
_SOURCE_FINAL_START_MS = 0
_SOURCE_FINAL_END_MS = 0
_JOURNAL_ROLES = ()
_ENTRY_PHASES = ""
_JOURNAL_STATUSES = ""
_LIVE_CORRECTION_KEYS = ""
_LIVE_DELIVERY_AUTHORITY_KEYS = ""
_DIAGNOSTIC_REASON_RX = ""
_DIAGNOSTIC_STATUS_RX = ""
_DIAGNOSTIC_MAX_REASON_CODES = 0
_PUBLISH_STORY_BOUNDARY_ROOT = ""

def _unavailable(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError('QIXI_TERMINAL_EVIDENCE_REFRESH_PRIVATE_AUTHORITY_UNAVAILABLE')

def _not_applicable(*args, **kwargs):
    return False

_diagnostic_token = _unavailable
_diagnostic_reason_codes = _unavailable
_diagnostic_finding_reason_codes = _unavailable
_diagnostic_cue_indices = _unavailable
_diagnostic_sha256 = _unavailable
_final_review_failure_diagnostic = _unavailable
full_dry_run_failure_result = _unavailable
_sha = _unavailable
_canonical_sha = _unavailable
_read_regular = _unavailable
load_authority = _unavailable
validate_runtime = _unavailable
_require_mapping = _unavailable
_bare_sha256 = _unavailable
_normalize_live_correction = _unavailable
_assert_text_and_grid_immutable = _unavailable
_srt_time_ms = _unavailable
_require_qixi_source_boundary = _unavailable
_correction_pass_for_terminal_review = _unavailable
_live_human_truth_seal = _unavailable
_operator_truth_row = _unavailable
_human_truth_from_authority = _unavailable
_load_qixi_human_truth = _unavailable
_validate_operator_truth_provenance = _unavailable
refresh_terminal_evidence = _unavailable
project_evidence_mirrors = _unavailable
_changed_pointers = _unavailable
_changed_within_allowlisted_subtrees = _unavailable
_candidate_state_story_contracts = _unavailable
_verify_committed_optional_mirrors = _unavailable
_json_bytes = _unavailable
_refresh_root = _unavailable
_create_refresh_root = _unavailable
_write_new = _unavailable
_replace_exact = _unavailable
_sealed_snapshot = _unavailable
_write_journal = _unavailable
_journal_sha256 = _unavailable
_receipt_for = _unavailable
_terminal_matrix = _unavailable
_validate_journal = _unavailable
_verify_committed_projection = _unavailable
_checkpoint = _unavailable
_backup_path = _unavailable
_ensure_backup = _unavailable
_entry_staged_path = _unavailable
_rollback_entries = _unavailable
_stage_marker = _unavailable
_write_stage_marker = _unavailable
_adopt_staged = _unavailable
apply_projection = _unavailable
validate_committed_refresh = _unavailable
_committed_successor_snapshot = _unavailable
committed_successor_snapshot = _unavailable
committed_successor_snapshot_with_before = _unavailable
build_staged_refresh = _unavailable
_json_document = _unavailable
_canonical_chat_from_applied = _unavailable
