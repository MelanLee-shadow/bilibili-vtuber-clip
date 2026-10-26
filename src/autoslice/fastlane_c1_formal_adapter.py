# Private operator evidence is not part of the public snapshot.
from __future__ import annotations
from pathlib import Path

class FastlaneC1FormalAdapterError(ValueError):
    reason_code = 'FASTLANE_C1_FORMAL_ADAPTER_PRIVATE_AUTHORITY_UNAVAILABLE'

class C1AuditIssue(ValueError):
    reason_code = 'FASTLANE_C1_FORMAL_ADAPTER_PRIVATE_AUTHORITY_UNAVAILABLE'

ROOT = Path("assets/_template/private-authority-unavailable.json")
CID = ""
RECORDING_DATE = ""
TITLE = ""
FORMAL_AUTHORITY_RELATIVE = Path("assets/_template/private-authority-unavailable.json")
CORRECTION_AUTHORITY_RELATIVE = Path("assets/_template/private-authority-unavailable.json")
FORMAL_MANIFEST_SCHEMA = 'fastlane-c1-formal-private-review-manifest.v1'
FORMAL_RECORD_SCHEMA = 'fastlane-c1-formal-private-record.v1'
FORMAL_PUBLISH_SCHEMA = 'fastlane-c1-formal-private-publish.v1'
FORMAL_RECEIPT_SCHEMA = 'fastlane-c1-formal-private-adapter-receipt.v1'
RULING_SEAL_SCHEMA = 'fastlane-c1-ruling-seal.v1'
TECHNICAL_TEMPLATE_SCHEMA = 'fastlane-c1-delegated-root-technical-review-template.v1'
ROOT_TECHNICAL_STATUS = ""
SIX_NAMED_POINTS = ()

def _unavailable(*args, **kwargs):
    raise FastlaneC1FormalAdapterError('FASTLANE_C1_FORMAL_ADAPTER_PRIVATE_AUTHORITY_UNAVAILABLE')

def _not_applicable(*args, **kwargs):
    return False

_canonical_bytes = _unavailable
_sha256_bytes = _unavailable
sha256_file = _unavailable
_require_regular = _unavailable
_contained = _unavailable
_read_json = _unavailable
_expect_sha256 = _unavailable
_require_exact_keys = _unavailable
_authority_sha256 = _unavailable
_expected_constraint = _unavailable
_validate_formal_authority = _unavailable
load_formal_authority = _unavailable
_verify_input = _unavailable
_public_identity_binding = _unavailable
_read_line_947 = _unavailable
_validate_ruling_inputs = _unavailable
_write_json = _unavailable
_copy_checked = _unavailable
_successor_cue_contract = _unavailable
_artifact_hashes = _unavailable
_build_record = _unavailable
_build_publish = _unavailable
_build_manifest = _unavailable
is_fastlane_c1_formal_manifest = _not_applicable
_validate_manifest_shape = _unavailable
_validate_root_receipt = _unavailable
_validate_mirrors = _unavailable
_validate_ruling_seal = _unavailable
_validate_materializer_receipt = _unavailable
_validate_technical_template = _unavailable
validate_formal_package = _unavailable
audit_fastlane_c1_formal_package = _unavailable
audit_fastlane_c1_formal_manifest = _unavailable
materialize_formal_private_package = _unavailable
is_fastlane_formal_manifest = _not_applicable
audit_fastlane_formal_package = _unavailable
