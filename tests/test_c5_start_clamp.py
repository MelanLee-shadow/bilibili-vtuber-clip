from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.autoslice import c5_start_clamp as c5


def _proposal_path() -> Path:
    return Path(__file__).parents[1] / "docs" / "reviews" / "auto_113028_1271_1328-c5-start-clamp-proposal.v1.json"


def _fixture() -> tuple[Path, dict[str, object], str, c5.AcceptanceExpectations, dict[str, object]]:
    path = _proposal_path()
    proposal, proposal_sha = c5.load_proposal(path)
    expected = c5.C5_ACCEPTANCE_EXPECTATIONS
    acceptance = c5.build_accepted_authority(proposal_path=path, proposal_file_sha256=proposal_sha, proposal_self_sha256=str(proposal["self_sha256"]), expectations=expected)
    return path, proposal, proposal_sha, expected, acceptance


def _reseal(value: dict[str, object]) -> None:
    unsigned = dict(value); unsigned.pop("self_sha256")
    value["self_sha256"] = c5._sha(unsigned)


def test_checked_in_proposal_is_exact_and_stays_unaccepted() -> None:
    path, proposal, _sha, _expected, _acceptance = _fixture()
    assert path.read_text(encoding="utf-8")
    assert proposal == c5.build_proposal()
    assert proposal["accepted"] is False and proposal["upload_allowed"] is False


def test_exact_acceptance_allows_only_the_190ms_geometry() -> None:
    path, proposal, proposal_sha, expected, acceptance = _fixture()
    assert c5.accepted_delivery_geometry(proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=expected, candidate_id=c5.CANDIDATE_ID, recording_date=c5.RECORDING_DATE, final_start_ms=9750, final_end_ms=67524, source_index=5, text="呃", speaker_label="李豆沙", old_start_ms=9560, old_end_ms=10080, media_sha256=str(c5.BOUNDARY["media_sha256"])) == (0, 330)


@pytest.mark.parametrize("mutator", [
    lambda a: a.__setitem__("candidate_id", "other"),
    lambda a: a.__setitem__("proposal_path", "/wrong/proposal.json"),
    lambda a: a.__setitem__("proposal_file_sha256", "sha256:" + "0" * 64),
    lambda a: a.__setitem__("proposal_self_sha256", "sha256:" + "0" * 64),
    lambda a: a.__setitem__("reviewer_by", "other"),
    lambda a: a.__setitem__("reviewed_at", "never"),
    lambda a: a.__setitem__("decision_basis", "other"),
    lambda a: a.__setitem__("upload_allowed", True),
])
def test_resealed_wrong_acceptance_still_fails(mutator) -> None:
    path, proposal, proposal_sha, expected, acceptance = _fixture()
    mutator(acceptance); _reseal(acceptance)
    with pytest.raises(c5.C5StartClampError):
        c5.validate_accepted_authority(proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=expected)


@pytest.mark.parametrize("mutator", [
    lambda p: p["historical_chain"].__setitem__("record_sha256", "sha256:" + "0" * 64),
    lambda p: p.__setitem__("drop_source_indices", [13, 14]),
    lambda p: p["cue"].__setitem__("text", "改词"),
    lambda p: p["cue"].__setitem__("speaker_label", "猜测"),
    lambda p: p["cue"].__setitem__("old_end_ms", 10081),
    lambda p: p["boundary"].__setitem__("final_start_ms", 9751),
    lambda p: p["boundary"].__setitem__("media_sha256", "sha256:" + "0" * 64),
])
def test_proposal_bindings_fail_even_when_resealed(mutator) -> None:
    proposal = c5.build_proposal(); mutator(proposal); _reseal(proposal)
    with pytest.raises(c5.C5StartClampError): c5.validate_proposal(proposal)


def test_missing_leaf_and_ancestor_link_fail_separately(tmp_path: Path) -> None:
    with pytest.raises(c5.C5StartClampError, match="PROPOSAL_MISSING"):
        c5.load_proposal(tmp_path / "missing.json")
    linked = tmp_path / "linked"; linked.symlink_to(tmp_path)
    with pytest.raises(c5.C5StartClampError, match="PROPOSAL_UNSAFE"):
        c5.load_proposal(linked / "anything.json")
    leaf = tmp_path / "leaf.json"; leaf.symlink_to(_proposal_path())
    with pytest.raises(c5.C5StartClampError, match="PROPOSAL_UNSAFE"):
        c5.load_proposal(leaf)


def test_fd_loop_handles_short_reads_and_rejects_truncated_reads(tmp_path: Path, monkeypatch) -> None:
    source = _proposal_path(); copy = tmp_path / "proposal.json"; copy.write_bytes(source.read_bytes())
    original = c5.os.read
    def one_byte(fd: int, size: int) -> bytes: return original(fd, 1)
    monkeypatch.setattr(c5.os, "read", one_byte)
    assert c5.load_proposal(copy)[0] == c5.build_proposal()
    monkeypatch.undo()
    calls = iter([b"{", b""])
    monkeypatch.setattr(c5.os, "read", lambda _fd, _size: next(calls))
    with pytest.raises(c5.C5StartClampError, match="PROPOSAL_INVALID"):
        c5.load_proposal(copy)


def test_create_only_write_validates_then_handles_short_zero_and_collision(tmp_path: Path, monkeypatch) -> None:
    path, proposal, proposal_sha, expected, acceptance = _fixture()
    output = tmp_path / "acceptance.json"
    c5.materialize_accepted_authority(output, proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=expected)
    assert json.loads(output.read_text()) == acceptance
    with pytest.raises(c5.C5StartClampError, match="COLLISION"):
        c5.materialize_accepted_authority(output, proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=expected)
    short = tmp_path / "short.json"; original = c5.os.write
    monkeypatch.setattr(c5.os, "write", lambda fd, data: original(fd, data[:1]))
    c5.materialize_accepted_authority(short, proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=expected)
    zero = tmp_path / "zero.json"; monkeypatch.setattr(c5.os, "write", lambda _fd, _data: 0)
    with pytest.raises(c5.C5StartClampError, match="WRITE_FAILED"):
        c5.materialize_accepted_authority(zero, proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=expected)
    assert not zero.exists()


def test_materializer_refuses_unset_expectation_before_creating(tmp_path: Path) -> None:
    path, proposal, proposal_sha, _expected, acceptance = _fixture()
    with pytest.raises(c5.C5StartClampError, match="EXPECTATIONS_UNSET"):
        c5.materialize_accepted_authority(tmp_path / "no.json", proposal_path=path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=acceptance, expectations=None)
