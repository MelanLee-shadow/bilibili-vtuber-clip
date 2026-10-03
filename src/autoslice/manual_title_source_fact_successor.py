"""Compatibility boundary: candidate-specific private evidence is not distributed."""

CANDIDATE_ID = '__private_manual_title_source_fact_successor_unavailable__'
RECORDING_DATE = '__private_manual_title_source_fact_successor_unavailable__'
SCHEMA_VERSION = '__private_manual_title_source_fact_successor_unavailable__'
RELATIVE_PATH = '__private_manual_title_source_fact_successor_unavailable__'

class ManualTitleSourceFactSuccessorError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def accepts_manual_title_source_fact_successor(*args, **kwargs):
    return False
