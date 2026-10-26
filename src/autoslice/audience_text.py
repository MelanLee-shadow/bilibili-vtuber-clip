"""Reject development disclosures at actual audience-text consumers."""

from .review_errors import ContractRejection
import re

from src.autoslice.subtitle_acceptability_policy import load_policy


def assert_audience_text(text: str, *, surface: str):
    policy = load_policy()['disclosure']
    if surface not in policy['audience_surfaces']:
        raise ContractRejection('UNKNOWN_AUDIENCE_SURFACE')
    for pattern in policy['developer_marker_patterns']:
        # These ordinary spoken phrases can be verbatim dialogue. Only their
        # annotation form is a development disclosure, not the original speech.
        if pattern in {'听不清', '无法确定'}:
            pattern = r'[\[（(]\s*' + pattern + r'\s*[\]）)]'
        if re.search(pattern, text, re.I):
            raise ContractRejection('AUDIENCE_INTERNAL_DISCLOSURE_FORBIDDEN:' + surface)
    return text


def assert_audience_surfaces(*, subtitle='', title='', cover='', description=''):
    for surface, text in locals().copy().items():
        assert_audience_text(text, surface=surface)
