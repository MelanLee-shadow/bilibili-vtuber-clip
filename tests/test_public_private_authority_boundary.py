"""Synthetic checks for the public boundary around omitted private authority."""
from pathlib import Path

import pytest

from src.autoslice import deterministic_text_surface_resolution as text_authority
from src.autoslice.final_review_contract import (
    FinalReviewContractError,
    validate_final_review_release,
)


@pytest.mark.parametrize('schema', [
    'b2-caption-final-review-successor.v1',
    'e353-content-final-review-successor.v1',
])
def test_claimed_private_successor_cannot_grant_release(schema):
    with pytest.raises(FinalReviewContractError, match='PRIVATE_CANDIDATE_AUTHORITY_UNAVAILABLE'):
        validate_final_review_release({'schema_version': schema, 'status': 'PASS'})


def test_absent_candidate_authority_does_not_override_normal_review(tmp_path: Path):
    assert text_authority.load_deterministic_text_surface_authority('synthetic', root=tmp_path) is None
    with pytest.raises(text_authority.DeterministicTextSurfaceResolutionError):
        text_authority.consume_deterministic_text_surface_authority({'status': 'PASS'})


def test_invented_private_authority_is_rejected(tmp_path: Path):
    authority_dir = tmp_path / 'assets/lidousha/deterministic_text_surface_resolutions'
    authority_dir.mkdir(parents=True)
    (authority_dir / 'synthetic.json').write_text('{"status":"PASS"}')
    with pytest.raises(text_authority.DeterministicTextSurfaceResolutionError):
        text_authority.load_deterministic_text_surface_authority('synthetic', root=tmp_path)
