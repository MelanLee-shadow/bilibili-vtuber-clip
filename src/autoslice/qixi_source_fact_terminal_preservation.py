"""Compatibility boundary: candidate-specific private evidence is not distributed."""

CANDIDATE_ID = '__private_qixi_source_fact_terminal_preservation_unavailable__'
AUTHORITY_RELATIVE_PATH = '__private_qixi_source_fact_terminal_preservation_unavailable__'
FINALIZATION_AUTHORITY_RELATIVE_PATH = '__private_qixi_source_fact_terminal_preservation_unavailable__'
SCHEMA = '__private_qixi_source_fact_terminal_preservation_unavailable__'
CONSUMPTION_SCHEMA = '__private_qixi_source_fact_terminal_preservation_unavailable__'
SOURCE_FACT_SCHEMA = '__private_qixi_source_fact_terminal_preservation_unavailable__'
DECISION = '__private_qixi_source_fact_terminal_preservation_unavailable__'

class QixiSourceFactTerminalPreservationError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def validate_terminal_preservation_finalization_authority(*args, **kwargs):
    return False

def build_terminal_preservation_review(*args, **kwargs):
    raise QixiSourceFactTerminalPreservationError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_terminal_preservation_review(*args, **kwargs):
    return False

def validate_terminal_preservation_source_fact_review(*args, **kwargs):
    return False

def validate_terminal_preservation_finalization_binding(*args, **kwargs):
    return False
