import copy
import json
from pathlib import Path

import pytest

from src.autoslice import qixi_cover_route_displacement as displacement


ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / displacement.AUTHORITY_RELATIVE_PATH


def _authority():
    return json.loads(AUTHORITY.read_text(encoding="utf-8"))


def test_qixi_displacement_is_single_candidate_and_private():
    authority = displacement.validate_authority_document(_authority())
    assert authority["candidate_id"] == "auto_113022_354_496"
    assert authority["upload_enabled"] is False
    assert authority["replacement"]["actual_treatment"] == "cpa_redraw"


def test_qixi_displacement_rejects_any_unsealed_scope_change():
    authority = _authority()
    authority["candidate_id"] = "auto_other"
    with pytest.raises(displacement.QixiCoverRouteDisplacementError):
        displacement.validate_authority_document(authority)


def test_qixi_displacement_replays_only_its_committed_redraw_evidence():
    authority = displacement.validate_authority_document(_authority())
    checked = displacement.validate_provider_evidence(authority=authority, bundle_root=ROOT)
    assert checked["final_cover_sha256"] == authority["replacement"]["final_cover_sha256"]
    drifted = copy.deepcopy(authority)
    drifted["replacement"]["final_cover_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(displacement.QixiCoverRouteDisplacementError):
        displacement.validate_provider_evidence(authority=drifted, bundle_root=ROOT)
