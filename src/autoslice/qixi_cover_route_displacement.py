"""Compatibility boundary: candidate-specific private evidence is not distributed."""

AUTHORITY_RELATIVE_PATH = '__private_qixi_cover_route_displacement_unavailable__'
SCHEMA = '__private_qixi_cover_route_displacement_unavailable__'
CANDIDATE_ID = '__private_qixi_cover_route_displacement_unavailable__'

class QixiCoverRouteDisplacementError(ValueError):
    reason_code = "PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE"

def validate_authority_document(*args, **kwargs):
    return False

def load_authority(*args, **kwargs):
    return None

def validate_provider_evidence(*args, **kwargs):
    return False
