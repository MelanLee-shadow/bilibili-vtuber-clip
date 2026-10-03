"""Compatibility boundary: candidate-specific private evidence is not distributed."""

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CID = '__private_fastlane_c1_formal_adapter_unavailable__'
RECORDING_DATE = '__private_fastlane_c1_formal_adapter_unavailable__'
TITLE = '__private_fastlane_c1_formal_adapter_unavailable__'
FORMAL_AUTHORITY_RELATIVE = '__private_fastlane_c1_formal_adapter_unavailable__'
CORRECTION_AUTHORITY_RELATIVE = '__private_fastlane_c1_formal_adapter_unavailable__'
FORMAL_MANIFEST_SCHEMA = '__private_fastlane_c1_formal_adapter_unavailable__'
FORMAL_RECORD_SCHEMA = '__private_fastlane_c1_formal_adapter_unavailable__'
FORMAL_PUBLISH_SCHEMA = '__private_fastlane_c1_formal_adapter_unavailable__'
FORMAL_RECEIPT_SCHEMA = '__private_fastlane_c1_formal_adapter_unavailable__'
RULING_SEAL_SCHEMA = '__private_fastlane_c1_formal_adapter_unavailable__'
TECHNICAL_TEMPLATE_SCHEMA = '__private_fastlane_c1_formal_adapter_unavailable__'
ROOT_TECHNICAL_STATUS = '__private_fastlane_c1_formal_adapter_unavailable__'
SIX_NAMED_POINTS = '__private_fastlane_c1_formal_adapter_unavailable__'

class FastlaneC1FormalAdapterError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

class C1AuditIssue:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def sha256_file(*args, **kwargs):
    raise FastlaneC1FormalAdapterError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def load_formal_authority(*args, **kwargs):
    return None

def is_fastlane_c1_formal_manifest(*args, **kwargs):
    return False

def validate_formal_package(*args, **kwargs):
    return False

def audit_fastlane_c1_formal_package(*args, **kwargs):
    raise FastlaneC1FormalAdapterError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def audit_fastlane_c1_formal_manifest(*args, **kwargs):
    raise FastlaneC1FormalAdapterError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def materialize_formal_private_package(*args, **kwargs):
    raise FastlaneC1FormalAdapterError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def is_fastlane_formal_manifest(*args, **kwargs):
    return False

def audit_fastlane_formal_package(*args, **kwargs):
    raise FastlaneC1FormalAdapterError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")
