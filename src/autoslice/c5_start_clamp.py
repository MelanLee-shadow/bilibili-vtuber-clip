"""Narrow C5 opening-cue clamp, isolated from generic delivery projection."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

CANDIDATE_ID = "auto_113028_1271_1328"
RECORDING_DATE = "2026-08-14"
SCHEMA = "c5-start-clamp-proposal.v1"
ACCEPTANCE_SCHEMA = "c5-start-clamp-acceptance.v1"
ACCEPTED_BY = "IVAN_DELEGATED_ROOT"
PROPOSAL_DOC_SHA256 = "sha256:d8b2074bcc446180135fb3a554aec322f19b90c4a0915d17f3571180c4069d34"
RULING_LINE_SHA256 = "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
HISTORICAL_CHAIN = {
    "record_sha256": "sha256:09646c35fdb2afd6a35cc4cf7c5aa4fbb61461f455a1866e9de3c8f35952ab95",
    "speaker_manifest_sha256": "sha256:cbed92f720d52d4da93f2b3fd58d1acaa02aefae0dc55a3a6cece13c3de248ec",
    "speaker_srt_sha256": "sha256:a014b0ece4a12234be58f08afb52bbb41a9ec7260d737b06a4ebfe8191932d20",
    "speaker_ass_sha256": "sha256:493e6aa6603b7a9dfe7ecb86e8b67d79cddc65ba1666ef2f499afb540106d94d",
    "diagnostic_srt_sha256": "sha256:5da5af9dba5ce3ff2fee7d585bda809eb3a9edb612a18868ace683cb94281043",
    "reviewed_srt_sha256": "sha256:9f33f247deb405b409d50f294bbfab08db7d5f43086241dd736fac0498faf64b",
    "ledger_sha256": "sha256:6fac5913ee143619c840483a8355e6ba63690f9bf47ddd27b3d34b9fcb5ce1a2",
    "truth_diff_sha256": "sha256:48142b7ebf737dde41472757030385794deb43ba9091289d1f4962885fd446b1",
}
BOUNDARY = {"final_start_ms": 9750, "final_end_ms": 67524, "media_sha256": "sha256:ce0142eca7921ddd7ca85412c0b11bf040b32e60021482b3b8cfe234ef36b312"}
CUE = {"source_index": 5, "text": "呃", "speaker_label": "李豆沙", "old_start_ms": 9560, "old_end_ms": 10080, "delivery_start_ms": 0, "delivery_end_ms": 330, "clamp_ms": 190}
EVIDENCE = {"audio_sha256": "sha256:904b77ad9b60c5480872c979796f8ead2449157a858a3814f9f608386ab02342", "frame_9500_sha256": "sha256:ddf0ea59a65b735ea144da0ac298be75127b34dacea448584f5c7e81655401e3", "frame_9750_sha256": "sha256:913ac4067707450560aa50ab3d7feb7f458f92378005c4210f1f71f1286e5079", "frame_10080_sha256": "sha256:bedfe3b21dfe69d438f6baf866c1b5641428ac31982f4c58ba6fd70e4ed25096", "silence_start_ms": 9679, "silence_end_ms": 10028}
_PROPOSAL_FIELDS = frozenset({"schema_version", "candidate_id", "recording_date", "accepted", "upload_allowed", "proposal_doc_sha256", "ruling_line_sha256", "historical_chain", "boundary", "cue", "drop_source_indices", "evidence", "self_sha256"})
_ACCEPTANCE_FIELDS = frozenset({"schema_version", "candidate_id", "recording_date", "accepted", "upload_allowed", "accepted_by", "proposal_self_sha256", "self_sha256"})

class C5StartClampError(RuntimeError): pass

def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
def _sha(value: object) -> str: return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()
def _sealed(value: Mapping[str, object], expected: frozenset[str], code: str) -> None:
    if set(value) != expected: raise C5StartClampError(code)
    unsigned = dict(value); declared = unsigned.pop("self_sha256", None)
    if declared != _sha(unsigned): raise C5StartClampError("C5_CLAMP_SELF_SEAL_DRIFT")

def build_proposal() -> dict[str, object]:
    """Return the immutable review-only proposal; it has no acceptance switch."""
    value: dict[str, object] = {"schema_version": SCHEMA, "candidate_id": CANDIDATE_ID, "recording_date": RECORDING_DATE, "accepted": False, "upload_allowed": False, "proposal_doc_sha256": PROPOSAL_DOC_SHA256, "ruling_line_sha256": RULING_LINE_SHA256, "historical_chain": dict(HISTORICAL_CHAIN), "boundary": dict(BOUNDARY), "cue": dict(CUE), "drop_source_indices": [13, 14, 15], "evidence": dict(EVIDENCE)}
    value["self_sha256"] = _sha(value)
    return value

def validate_proposal(value: Mapping[str, object]) -> dict[str, object]:
    _sealed(value, _PROPOSAL_FIELDS, "C5_CLAMP_IDENTITY_OR_SCHEMA_DRIFT")
    if dict(value) != build_proposal(): raise C5StartClampError("C5_CLAMP_PROVENANCE_OR_GEOMETRY_DRIFT")
    return dict(value)

def load_proposal(path: Path) -> dict[str, object]:
    """Load an exact proposal only from a non-symlink regular file."""
    if path.is_symlink() or not path.is_file():
        raise C5StartClampError("C5_CLAMP_PROPOSAL_PATH_DRIFT")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C5StartClampError("C5_CLAMP_PROPOSAL_PATH_DRIFT") from exc
    if not isinstance(value, Mapping):
        raise C5StartClampError("C5_CLAMP_PROPOSAL_PATH_DRIFT")
    return validate_proposal(value)

def build_accepted_authority(*, proposal_self_sha256: str, accepted_by: str) -> dict[str, object]:
    """Shape a root decision for a fixture/review; it does not sign a decision."""
    value: dict[str, object] = {"schema_version": ACCEPTANCE_SCHEMA, "candidate_id": CANDIDATE_ID, "recording_date": RECORDING_DATE, "accepted": True, "upload_allowed": False, "accepted_by": accepted_by, "proposal_self_sha256": proposal_self_sha256}
    value["self_sha256"] = _sha(value)
    return value

def validate_accepted_authority(proposal: Mapping[str, object], acceptance: Mapping[str, object]) -> None:
    sealed_proposal = validate_proposal(proposal)
    _sealed(acceptance, _ACCEPTANCE_FIELDS, "C5_CLAMP_ACCEPTANCE_SCHEMA_DRIFT")
    if (acceptance.get("schema_version") != ACCEPTANCE_SCHEMA or acceptance.get("candidate_id") != CANDIDATE_ID or acceptance.get("recording_date") != RECORDING_DATE or acceptance.get("accepted") is not True or acceptance.get("upload_allowed") is not False or acceptance.get("accepted_by") != ACCEPTED_BY or acceptance.get("proposal_self_sha256") != sealed_proposal["self_sha256"]):
        raise C5StartClampError("C5_CLAMP_ACCEPTANCE_BINDING_DRIFT")

def load_accepted_authority(path: Path, proposal: Mapping[str, object]) -> dict[str, object]:
    """Read a root-supplied acceptance without following an authority symlink."""
    if path.is_symlink() or not path.is_file():
        raise C5StartClampError("C5_CLAMP_ACCEPTANCE_PATH_DRIFT")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C5StartClampError("C5_CLAMP_ACCEPTANCE_PATH_DRIFT") from exc
    if not isinstance(value, Mapping):
        raise C5StartClampError("C5_CLAMP_ACCEPTANCE_PATH_DRIFT")
    validate_accepted_authority(proposal, value)
    return dict(value)

def accepted_delivery_geometry(*, proposal: Mapping[str, object], acceptance: Mapping[str, object], candidate_id: str, recording_date: str, final_start_ms: int, final_end_ms: int, source_index: int, text: str, speaker_label: str, old_start_ms: int, old_end_ms: int, media_sha256: str) -> tuple[int, int]:
    """Return only C5 cue 5's approved projection; every other row fails closed."""
    validate_accepted_authority(proposal, acceptance)
    if (candidate_id, recording_date, final_start_ms, final_end_ms, source_index, text, speaker_label, old_start_ms, old_end_ms, media_sha256) != (CANDIDATE_ID, RECORDING_DATE, BOUNDARY["final_start_ms"], BOUNDARY["final_end_ms"], CUE["source_index"], CUE["text"], CUE["speaker_label"], CUE["old_start_ms"], CUE["old_end_ms"], BOUNDARY["media_sha256"]):
        raise C5StartClampError("C5_CLAMP_RUNTIME_BINDING_DRIFT")
    return int(CUE["delivery_start_ms"]), int(CUE["delivery_end_ms"])

def materialize_accepted_authority(path: Path, acceptance: Mapping[str, object]) -> None:
    """Create-only persistence for a root-supplied acceptance; never signs it."""
    _sealed(acceptance, _ACCEPTANCE_FIELDS, "C5_CLAMP_ACCEPTANCE_SCHEMA_DRIFT")
    if path.is_symlink() or path.exists(): raise C5StartClampError("C5_CLAMP_ACCEPTANCE_COLLISION")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try: os.write(fd, _canonical(dict(acceptance)) + b"\n")
    finally: os.close(fd)
