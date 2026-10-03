"""Compatibility boundary: candidate-specific private evidence is not distributed."""

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_ID = '__private_qixi_operator_exact_title_source_fact_unavailable__'
RECORDING_DATE = '__private_qixi_operator_exact_title_source_fact_unavailable__'
SCHEMA_VERSION = '__private_qixi_operator_exact_title_source_fact_unavailable__'
CONSUMPTION_SCHEMA_VERSION = '__private_qixi_operator_exact_title_source_fact_unavailable__'
DECISION = '__private_qixi_operator_exact_title_source_fact_unavailable__'
AUTHORITY_STATUS = '__private_qixi_operator_exact_title_source_fact_unavailable__'
ASSET_PATH = '__private_qixi_operator_exact_title_source_fact_unavailable__'

class QixiOperatorExactTitleSourceFactError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

class Authority:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

class _CommittedSuccessorProof:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def canonical_sha256(*args, **kwargs):
    raise QixiOperatorExactTitleSourceFactError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def bytes_sha256(*args, **kwargs):
    raise QixiOperatorExactTitleSourceFactError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def text_sha256(*args, **kwargs):
    raise QixiOperatorExactTitleSourceFactError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_authority_document(*args, **kwargs):
    return False

def load_authority(*args, **kwargs):
    return None

def consume_authority(*args, **kwargs):
    raise QixiOperatorExactTitleSourceFactError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def authorize(*args, **kwargs):
    raise QixiOperatorExactTitleSourceFactError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_receipt(*args, **kwargs):
    return False

def validate_public_surface_receipt(*args, **kwargs):
    return False
