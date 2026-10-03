"""Compatibility boundary: candidate-specific private evidence is not distributed."""

SOURCE_FACT_SCHEMA = '__private_candidate_source_fact_refresh_unavailable__'
AUTHORITY_SCHEMA = '__private_candidate_source_fact_refresh_unavailable__'
CONSUMPTION_SCHEMA = '__private_candidate_source_fact_refresh_unavailable__'
DECISION = '__private_candidate_source_fact_refresh_unavailable__'
CANDIDATE_ID = '__private_candidate_source_fact_refresh_unavailable__'
AUTHORITY_RELATIVE_PATH = '__private_candidate_source_fact_refresh_unavailable__'

class CandidateSourceFactRefreshError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def load_candidate_source_fact_refresh_preimages(*args, **kwargs):
    return None

def build_candidate_public_text_source_fact_refresh_review(*args, **kwargs):
    raise CandidateSourceFactRefreshError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_candidate_public_text_source_fact_refresh_review(*args, **kwargs):
    return False

def validate_candidate_public_text_source_fact_refresh_from_source_fact(*args, **kwargs):
    return False
