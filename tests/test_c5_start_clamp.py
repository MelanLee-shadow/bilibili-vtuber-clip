from __future__ import annotations

from pathlib import Path

import pytest

from src.autoslice.c5_start_clamp import (
    BOUNDARY, C5StartClampError, accepted_delivery_geometry,
    build_accepted_authority, build_proposal, materialize_accepted_authority,
    load_proposal, validate_proposal,
)


def _accepted() -> tuple[dict[str, object], dict[str, object]]:
    proposal = build_proposal()
    return proposal, build_accepted_authority(
        proposal_self_sha256=str(proposal["self_sha256"]),
        accepted_by="IVAN_DELEGATED_ROOT",
    )


def _geometry(proposal: dict[str, object], acceptance: dict[str, object]) -> tuple[int, int]:
    return accepted_delivery_geometry(
        proposal=proposal, acceptance=acceptance,
        candidate_id="auto_113028_1271_1328", recording_date="2026-08-14",
        final_start_ms=9750, final_end_ms=67524, source_index=5, text="呃",
        speaker_label="李豆沙", old_start_ms=9560, old_end_ms=10080,
        media_sha256=str(BOUNDARY["media_sha256"]),
    )


def test_exact_root_accepted_fixture_projects_only_cue_five() -> None:
    proposal, acceptance = _accepted()
    assert _geometry(proposal, acceptance) == (0, 330)


def test_checked_in_unaccepted_proposal_is_the_exact_sealed_fixture() -> None:
    proposal_path = Path(__file__).parents[1] / "docs" / "reviews" / "auto_113028_1271_1328-c5-start-clamp-proposal.v1.json"
    assert load_proposal(proposal_path) == build_proposal()


@pytest.mark.parametrize("field,value", [
    ("candidate_id", "other"), ("recording_date", "2026-08-15"),
    ("accepted", True), ("upload_allowed", True),
    ("proposal_doc_sha256", "sha256:" + "0" * 64),
])
def test_proposal_rejects_identity_and_self_acceptance_drift(field: str, value: object) -> None:
    proposal = build_proposal(); proposal[field] = value
    with pytest.raises(C5StartClampError): validate_proposal(proposal)


@pytest.mark.parametrize("container,field,value", [
    ("boundary", "final_start_ms", 9751), ("boundary", "final_end_ms", 67523),
    ("cue", "clamp_ms", 191), ("cue", "text", "改词"),
    ("cue", "speaker_label", "猜测者"), ("cue", "old_end_ms", 10081),
    ("evidence", "silence_end_ms", 10029),
    ("historical_chain", "record_sha256", "sha256:" + "0" * 64),
])
def test_proposal_rejects_every_bound_provenance_drift(container: str, field: str, value: object) -> None:
    proposal = build_proposal(); proposal[container][field] = value  # type: ignore[index]
    with pytest.raises(C5StartClampError): validate_proposal(proposal)


def test_drop_set_and_self_seal_drift_fail_closed() -> None:
    proposal = build_proposal(); proposal["drop_source_indices"] = [13, 14]
    with pytest.raises(C5StartClampError): validate_proposal(proposal)
    proposal = build_proposal(); proposal["self_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(C5StartClampError, match="SELF_SEAL"): validate_proposal(proposal)


@pytest.mark.parametrize("mutation", ["acceptance_candidate", "acceptance_proposal", "acceptance_upload", "runtime_label", "runtime_end", "runtime_media", "runtime_cue"])
def test_acceptance_and_runtime_binding_fail_closed(mutation: str) -> None:
    proposal, acceptance = _accepted()
    kwargs = dict(proposal=proposal, acceptance=acceptance, candidate_id="auto_113028_1271_1328", recording_date="2026-08-14", final_start_ms=9750, final_end_ms=67524, source_index=5, text="呃", speaker_label="李豆沙", old_start_ms=9560, old_end_ms=10080, media_sha256=str(BOUNDARY["media_sha256"]))
    if mutation == "acceptance_candidate": acceptance["candidate_id"] = "other"
    elif mutation == "acceptance_proposal": acceptance["proposal_self_sha256"] = "sha256:" + "0" * 64
    elif mutation == "acceptance_upload": acceptance["upload_allowed"] = True
    elif mutation == "runtime_label": kwargs["speaker_label"] = "other"
    elif mutation == "runtime_end": kwargs["old_end_ms"] = 10081
    elif mutation == "runtime_media": kwargs["media_sha256"] = "sha256:" + "0" * 64
    else: kwargs["source_index"] = 6
    with pytest.raises(C5StartClampError): accepted_delivery_geometry(**kwargs)  # type: ignore[arg-type]


def test_create_only_materializer_rejects_symlink_and_never_overwrites(tmp_path) -> None:
    _proposal, acceptance = _accepted()
    target = tmp_path / "acceptance.json"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(C5StartClampError, match="COLLISION"):
        materialize_accepted_authority(target, acceptance)
    target.unlink(); target.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(C5StartClampError, match="COLLISION"):
        materialize_accepted_authority(target, acceptance)


def test_proposal_loader_rejects_symlink_drift(tmp_path) -> None:
    canonical = tmp_path / "proposal.json"
    canonical.write_text(__import__("json").dumps(build_proposal()), encoding="utf-8")
    assert load_proposal(canonical)["accepted"] is False
    linked = tmp_path / "proposal-link.json"; linked.symlink_to(canonical)
    with pytest.raises(C5StartClampError, match="PATH_DRIFT"):
        load_proposal(linked)
