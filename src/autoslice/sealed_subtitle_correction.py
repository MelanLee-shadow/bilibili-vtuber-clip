"""Compatibility boundary: candidate-specific private evidence is not distributed."""

SCHEMA_VERSION = '__private_sealed_subtitle_correction_unavailable__'
CANDIDATE_ID = '__private_sealed_subtitle_correction_unavailable__'
RECORDING_DATE = '__private_sealed_subtitle_correction_unavailable__'
RELATIVE_PATH = '__private_sealed_subtitle_correction_unavailable__'

class SealedSubtitleCorrectionError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

class SealedSubtitleCorrectionTransactionContext:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def canonical_authority_path(*args, **kwargs):
    raise SealedSubtitleCorrectionError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_authority(*args, **kwargs):
    return False

def load_deployed_authority(*args, **kwargs):
    return None

def validate_diagnostic_assets(*args, **kwargs):
    return False

def expected_output_srt(*args, **kwargs):
    raise SealedSubtitleCorrectionError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_runtime(*args, **kwargs):
    return False

def validate_post_transaction(*args, **kwargs):
    return False
