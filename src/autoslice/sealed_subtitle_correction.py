# Private operator evidence is not part of the public snapshot.
from __future__ import annotations
from pathlib import Path

class SealedSubtitleCorrectionError(ValueError):
    reason_code = 'SEALED_SUBTITLE_CORRECTION_PRIVATE_AUTHORITY_UNAVAILABLE'

class SealedSubtitleCorrectionTransactionContext(ValueError):
    reason_code = 'SEALED_SUBTITLE_CORRECTION_PRIVATE_AUTHORITY_UNAVAILABLE'

SCHEMA_VERSION = 'sealed-subtitle-correction-authority.v1'
CANDIDATE_ID = ""
RECORDING_DATE = ""
RELATIVE_PATH = Path("assets/_template/private-authority-unavailable.json")
_AUTHORITY_FIELDS = ""
_DESCRIPTOR_FIELDS = ""
_ARTIFACT_ROLES = ""
_DELIVERY_FIELDS = ""
_BRANDING_FIELDS = ""
_CORRECTION_FIELDS = ""
_REPLACEMENT_FIELDS = ""
_ASSERTION_FIELDS = ""
_IMMUTABLE_ARTIFACT_ROLES = ""
_SPEAKER_SIDECAR_FIELDS = ""
_CORRECTION_RECEIPT_FIELDS = ""
_DIAGNOSTIC_FIELDS = ""
_REPO_DESCRIPTOR_FIELDS = ""
_CORRECTION_RECEIPT_V2_FIELDS = ""

def _unavailable(*args, **kwargs):
    raise SealedSubtitleCorrectionError('SEALED_SUBTITLE_CORRECTION_PRIVATE_AUTHORITY_UNAVAILABLE')

def _not_applicable(*args, **kwargs):
    return False

_canonical_sha256 = _unavailable
_file_sha256 = _unavailable
canonical_authority_path = _unavailable
_require_mapping = _unavailable
_require_sha = _unavailable
_validate_descriptor = _unavailable
_validate_repo_descriptor = _unavailable
validate_authority = _unavailable
load_deployed_authority = _unavailable
_check_descriptor = _unavailable
_regular_file = _unavailable
_repository_asset_bytes = _unavailable
_required_prefixed_sha = _unavailable
_record_sha = _unavailable
_cue_rows = _unavailable
_validate_diagnostic_decision_receipt = _unavailable
validate_diagnostic_assets = _unavailable
_validate_exact_cue_transform = _unavailable
expected_output_srt = _unavailable
validate_runtime = _unavailable
_json_pointer_differences = _unavailable
_validate_correction_receipt = _unavailable
validate_post_transaction = _unavailable
