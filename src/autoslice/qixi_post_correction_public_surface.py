"""Compatibility boundary: candidate-specific private evidence is not distributed."""

from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
RELATIVE_AUTHORITY_PATH = '__private_qixi_post_correction_public_surface_unavailable__'
CANDIDATE_ID = '__private_qixi_post_correction_public_surface_unavailable__'
RECORDING_DATE = '__private_qixi_post_correction_public_surface_unavailable__'
MANUAL_TITLE = '__private_qixi_post_correction_public_surface_unavailable__'
PUBLIC_TITLE = '__private_qixi_post_correction_public_surface_unavailable__'
PREPARED_SCHEMA_VERSION = '__private_qixi_post_correction_public_surface_unavailable__'

class RuntimeInputs:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

class InstalledSnapshot:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

class PreparedQixiAfterImage:
    def __init__(self, *args, **kwargs):
        raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def validate_authority(*args, **kwargs):
    return False

def load_deployed_authority(*args, **kwargs):
    return None

def validate_runtime(*args, **kwargs):
    return False

def prepare_qixi_after_image(*args, **kwargs):
    raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def commit_qixi_after_image(*args, **kwargs):
    raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")

def finalize(*args, **kwargs):
    raise ValueError("PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE")
