"""Compatibility boundary: candidate-specific private evidence is not distributed."""
from pathlib import Path

CANDIDATE_ID = '__private_qixi_terminal_evidence_refresh_unavailable__'
RECORDING_DATE = '__private_qixi_terminal_evidence_refresh_unavailable__'
ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = '__private_qixi_terminal_evidence_refresh_unavailable__'
HUMAN_TRUTH_AUTHORITY_PATH = '__private_qixi_terminal_evidence_refresh_unavailable__'

class QixiTerminalEvidenceRefreshError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def full_dry_run_failure_result(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def load_authority(*args, **kwargs):
    return None

def validate_runtime(*args, **kwargs):
    return False

def refresh_terminal_evidence(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def project_evidence_mirrors(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def apply_projection(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_committed_refresh(*args, **kwargs):
    return False

def committed_successor_snapshot(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def committed_successor_snapshot_with_before(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def build_staged_refresh(*args, **kwargs):
    raise QixiTerminalEvidenceRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")
