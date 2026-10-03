"""Compatibility boundary: candidate-specific private evidence is not distributed."""

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
CANDIDATE_ID = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
RECORDING_DATE = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
PUNCH_ORIGINAL = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
PUNCH_CANDIDATES = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
PREFLIGHT_SCHEMA = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
PREFLIGHT_RECEIPT_SCHEMA = '__private_qixi_screenshot_direct_cover_repair_unavailable__'
JOURNAL_SCHEMA = '__private_qixi_screenshot_direct_cover_repair_unavailable__'

class QixiScreenshotDirectCoverRepairError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def canonical_sha256(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def load_authority(*args, **kwargs):
    return None

def validate_authority(*args, **kwargs):
    return False

def validate_legacy_cover_inputs(*args, **kwargs):
    return False

def snapshot_fixed_runtime(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def committed_cover_successor_snapshot(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def build_cover_projection(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def require_repaired_punch(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def sealed_punch_semantic_receipt(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def authority_final_cover_path(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def cover_repair_qc_target(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def run_canonical_full_dry(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def run_fixed_full_dry(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def run_fixed_apply(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def build_preflight_manifest(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_preflight_manifest(*args, **kwargs):
    return False

def run_private_preflight(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def apply_preflight_targets(*args, **kwargs):
    raise QixiScreenshotDirectCoverRepairError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")
