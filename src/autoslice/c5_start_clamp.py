"""Forensic validator for the revoked C5 start-clamp proposal.

The proposal mistook a delivery-local cue for a piece-local cue.  Historical
seals remain readable for incident evidence, but no runtime path may authorize
or apply its geometry.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CANDIDATE_ID = "auto_113028_1271_1328"
RECORDING_DATE = "2026-08-14"
SCHEMA = "c5-start-clamp-proposal.v1"
ACCEPTANCE_SCHEMA = "c5-start-clamp-acceptance.v2"
PERMITTED_OPERATION = "C5_EXACT_START_CLAMP_PRIVATE_PROJECTION"
REVOKED_ERROR = "C5_START_CLAMP_REVOKED_TIME_DOMAIN_MISMATCH"
PROPOSAL_DOC_SHA256 = "sha256:d8b2074bcc446180135fb3a554aec322f19b90c4a0915d17f3571180c4069d34"
RULING_LINE_SHA256 = "sha256:e64d4409aaf36193c27f3d67cd8e3fae69a6d3ae543a29a6c26f57c77d61c2aa"
HISTORICAL_CHAIN = {"record_sha256":"sha256:09646c35fdb2afd6a35cc4cf7c5aa4fbb61461f455a1866e9de3c8f35952ab95", "speaker_manifest_sha256":"sha256:cbed92f720d52d4da93f2b3fd58d1acaa02aefae0dc55a3a6cece13c3de248ec", "speaker_srt_sha256":"sha256:a014b0ece4a12234be58f08afb52bbb41a9ec7260d737b06a4ebfe8191932d20", "speaker_ass_sha256":"sha256:493e6aa6603b7a9dfe7ecb86e8b67d79cddc65ba1666ef2f499afb540106d94d", "diagnostic_srt_sha256":"sha256:5da5af9dba5ce3ff2fee7d585bda809eb3a9edb612a18868ace683cb94281043", "reviewed_srt_sha256":"sha256:9f33f247deb405b409d50f294bbfab08db7d5f43086241dd736fac0498faf64b", "ledger_sha256":"sha256:6fac5913ee143619c840483a8355e6ba63690f9bf47ddd27b3d34b9fcb5ce1a2", "truth_diff_sha256":"sha256:48142b7ebf737dde41472757030385794deb43ba9091289d1f4962885fd446b1"}
BOUNDARY = {"final_start_ms":9750, "final_end_ms":67524, "media_sha256":"sha256:ce0142eca7921ddd7ca85412c0b11bf040b32e60021482b3b8cfe234ef36b312"}
CUE = {"source_index":5, "text":"呃", "speaker_label":"李豆沙", "old_start_ms":9560, "old_end_ms":10080, "delivery_start_ms":0, "delivery_end_ms":330, "clamp_ms":190}
EVIDENCE = {"audio_sha256":"sha256:904b77ad9b60c5480872c979796f8ead2449157a858a3814f9f608386ab02342", "frame_9500_sha256":"sha256:ddf0ea59a65b735ea144da0ac298be75127b34dacea448584f5c7e81655401e3", "frame_9750_sha256":"sha256:913ac4067707450560aa50ab3d7feb7f458f92378005c4210f1f71f1286e5079", "frame_10080_sha256":"sha256:bedfe3b21dfe69d438f6baf866c1b5641428ac31982f4c58ba6fd70e4ed25096", "silence_start_ms":9679, "silence_end_ms":10028}
_PROPOSAL_FIELDS = frozenset({"schema_version","candidate_id","recording_date","accepted","upload_allowed","proposal_doc_sha256","ruling_line_sha256","historical_chain","boundary","cue","drop_source_indices","evidence","self_sha256"})
_ACCEPTANCE_FIELDS = frozenset({"schema_version","candidate_id","recording_date","accepted","upload_allowed","permitted_private_operation","proposal_path","proposal_file_sha256","proposal_self_sha256","reviewer_kind","reviewer_by","reviewed_at","decision_basis","self_sha256"})

class C5StartClampError(RuntimeError): pass

@dataclass(frozen=True)
class AcceptanceExpectations:
    reviewer_kind: str | None = None
    reviewer_by: str | None = None
    reviewed_at: str | None = None
    decision_basis: str | None = None

C5_ACCEPTANCE_EXPECTATIONS = AcceptanceExpectations(
    reviewer_kind="delegated_root_agent", reviewer_by="Codex root",
    reviewed_at="2026-08-24T23:00:13Z",
    decision_basis="Root accepted the exact C5 cue-5 private start-clamp proposal after verifying that the frozen 9750 ms boundary lies inside the detected silence interval and that Ivan's named C5 content change remains limited to dropping humming cues 13-15; private projection only, with no text, speaker, title, cover, boundary, media, deploy, manifest, or upload approval.",
)

def runtime_authority_paths(runtime_root: Path) -> tuple[Path, Path]:
    """Return the sole private authority location; callers never accept a flag."""
    root = Path(runtime_root).absolute() / ".private-c5-start-clamp-authority"
    return root / "c5-start-clamp-proposal.v1.json", root / "c5-start-clamp-acceptance.v2.json"


def finalizer_authority_kwargs(
    *, candidate_id: str, recording_date: str, runtime_root: Path
) -> dict[str, object]:
    """Return no runtime fields: the former private clamp is revoked."""
    del candidate_id, recording_date, runtime_root
    return {}

def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
def _sha_bytes(value: bytes) -> str: return "sha256:" + hashlib.sha256(value).hexdigest()
def _sha(value: object) -> str: return _sha_bytes(_canonical(value))
def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev,a.st_ino,a.st_mode,a.st_size,a.st_mtime_ns,a.st_ctime_ns) == (b.st_dev,b.st_ino,b.st_mode,b.st_size,b.st_mtime_ns,b.st_ctime_ns)

def _parent_fd(path: Path, *, label: str) -> tuple[int, str, Path]:
    absolute = Path(path).absolute()
    if absolute.name in {"", ".", ".."}: raise C5StartClampError(f"C5_CLAMP_{label}_UNSAFE")
    flags = os.O_RDONLY | getattr(os,"O_DIRECTORY",0) | getattr(os,"O_NOFOLLOW",0) | getattr(os,"O_CLOEXEC",0)
    try: fd = os.open(absolute.anchor, flags)
    except OSError as exc: raise C5StartClampError(f"C5_CLAMP_{label}_UNSAFE") from exc
    try:
        for part in absolute.parts[1:-1]:
            try: next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError as exc: raise C5StartClampError(f"C5_CLAMP_{label}_MISSING") from exc
            except OSError as exc: raise C5StartClampError(f"C5_CLAMP_{label}_UNSAFE") from exc
            os.close(fd); fd = next_fd
        return fd, absolute.name, absolute
    except BaseException:
        os.close(fd); raise

def _read_regular(path: Path, *, label: str) -> bytes:
    parent, name, _absolute = _parent_fd(path, label=label)
    flags = os.O_RDONLY | getattr(os,"O_NOFOLLOW",0) | getattr(os,"O_CLOEXEC",0)
    try:
        try: fd = os.open(name, flags, dir_fd=parent)
        except FileNotFoundError as exc: raise C5StartClampError(f"C5_CLAMP_{label}_MISSING") from exc
        except OSError as exc: raise C5StartClampError(f"C5_CLAMP_{label}_UNSAFE") from exc
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode): raise C5StartClampError(f"C5_CLAMP_{label}_UNSAFE")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk: break
                chunks.append(chunk)
            after = os.fstat(fd)
            try: named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError as exc: raise C5StartClampError(f"C5_CLAMP_{label}_DRIFT") from exc
            if not _same(before, after) or not _same(before, named) or not stat.S_ISREG(named.st_mode):
                raise C5StartClampError(f"C5_CLAMP_{label}_DRIFT")
            return b"".join(chunks)
        finally: os.close(fd)
    finally: os.close(parent)

def _sealed(value: Mapping[str, object], fields: frozenset[str], code: str) -> None:
    if set(value) != fields: raise C5StartClampError(code)
    unsigned = dict(value); declared = unsigned.pop("self_sha256", None)
    if declared != _sha(unsigned): raise C5StartClampError("C5_CLAMP_SELF_SEAL_DRIFT")

def build_proposal() -> dict[str, object]:
    value: dict[str, object] = {"schema_version":SCHEMA,"candidate_id":CANDIDATE_ID,"recording_date":RECORDING_DATE,"accepted":False,"upload_allowed":False,"proposal_doc_sha256":PROPOSAL_DOC_SHA256,"ruling_line_sha256":RULING_LINE_SHA256,"historical_chain":dict(HISTORICAL_CHAIN),"boundary":dict(BOUNDARY),"cue":dict(CUE),"drop_source_indices":[13,14,15],"evidence":dict(EVIDENCE)}
    value["self_sha256"] = _sha(value); return value
def validate_proposal(value: Mapping[str, object]) -> dict[str, object]:
    _sealed(value, _PROPOSAL_FIELDS, "C5_CLAMP_IDENTITY_OR_SCHEMA_DRIFT")
    if dict(value) != build_proposal(): raise C5StartClampError("C5_CLAMP_PROVENANCE_OR_GEOMETRY_DRIFT")
    return dict(value)
def load_proposal(path: Path) -> tuple[dict[str, object], str]:
    raw = _read_regular(path, label="PROPOSAL")
    try: value = json.loads(raw.decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise C5StartClampError("C5_CLAMP_PROPOSAL_INVALID") from exc
    if not isinstance(value, Mapping): raise C5StartClampError("C5_CLAMP_PROPOSAL_INVALID")
    return validate_proposal(value), _sha_bytes(raw)

def build_accepted_authority(*, proposal_path: Path, proposal_file_sha256: str, proposal_self_sha256: str, expectations: AcceptanceExpectations) -> dict[str, object]:
    value: dict[str, object] = {"schema_version":ACCEPTANCE_SCHEMA,"candidate_id":CANDIDATE_ID,"recording_date":RECORDING_DATE,"accepted":True,"upload_allowed":False,"permitted_private_operation":PERMITTED_OPERATION,"proposal_path":str(Path(proposal_path).absolute()),"proposal_file_sha256":proposal_file_sha256,"proposal_self_sha256":proposal_self_sha256,"reviewer_kind":expectations.reviewer_kind,"reviewer_by":expectations.reviewer_by,"reviewed_at":expectations.reviewed_at,"decision_basis":expectations.decision_basis}
    value["self_sha256"] = _sha(value); return value
def _require_expectations(expected: AcceptanceExpectations | None) -> AcceptanceExpectations:
    if expected is None or any(not isinstance(v,str) or not v for v in (expected.reviewer_kind,expected.reviewer_by,expected.reviewed_at,expected.decision_basis)):
        raise C5StartClampError("C5_CLAMP_ACCEPTANCE_EXPECTATIONS_UNSET")
    return expected
def validate_accepted_authority(*, proposal_path: Path, proposal: Mapping[str, object], proposal_file_sha256: str, acceptance: Mapping[str, object], expectations: AcceptanceExpectations | None) -> None:
    expected = _require_expectations(expectations); sealed = validate_proposal(proposal)
    _sealed(acceptance, _ACCEPTANCE_FIELDS, "C5_CLAMP_ACCEPTANCE_SCHEMA_DRIFT")
    if (acceptance.get("schema_version") != ACCEPTANCE_SCHEMA or acceptance.get("candidate_id") != CANDIDATE_ID or acceptance.get("recording_date") != RECORDING_DATE or acceptance.get("accepted") is not True or acceptance.get("upload_allowed") is not False or acceptance.get("permitted_private_operation") != PERMITTED_OPERATION or acceptance.get("proposal_path") != str(Path(proposal_path).absolute()) or acceptance.get("proposal_file_sha256") != proposal_file_sha256 or acceptance.get("proposal_self_sha256") != sealed["self_sha256"] or tuple(acceptance.get(k) for k in ("reviewer_kind","reviewer_by","reviewed_at","decision_basis")) != (expected.reviewer_kind,expected.reviewer_by,expected.reviewed_at,expected.decision_basis)):
        raise C5StartClampError("C5_CLAMP_ACCEPTANCE_BINDING_DRIFT")
def load_accepted_authority(path: Path, *, proposal_path: Path, expectations: AcceptanceExpectations | None) -> dict[str, object]:
    proposal, proposal_sha = load_proposal(proposal_path)
    raw = _read_regular(path, label="ACCEPTANCE")
    try: value = json.loads(raw.decode())
    except (UnicodeDecodeError,json.JSONDecodeError) as exc: raise C5StartClampError("C5_CLAMP_ACCEPTANCE_INVALID") from exc
    if not isinstance(value, Mapping): raise C5StartClampError("C5_CLAMP_ACCEPTANCE_INVALID")
    validate_accepted_authority(proposal_path=proposal_path, proposal=proposal, proposal_file_sha256=proposal_sha, acceptance=value, expectations=expectations)
    return dict(value)

def accepted_delivery_geometry(*, proposal_path: Path, proposal: Mapping[str, object], proposal_file_sha256: str, acceptance: Mapping[str, object], expectations: AcceptanceExpectations | None, candidate_id: str, recording_date: str, final_start_ms: int, final_end_ms: int, source_index: int, text: str, speaker_label: str, old_start_ms: int, old_end_ms: int, media_sha256: str) -> tuple[int,int]:
    validate_accepted_authority(proposal_path=proposal_path, proposal=proposal, proposal_file_sha256=proposal_file_sha256, acceptance=acceptance, expectations=expectations)
    del candidate_id, recording_date, final_start_ms, final_end_ms
    del source_index, text, speaker_label, old_start_ms, old_end_ms, media_sha256
    raise C5StartClampError(REVOKED_ERROR)

def _write_all(fd: int, payload: bytes) -> None:
    position = 0
    while position < len(payload):
        written = os.write(fd, payload[position:])
        if written <= 0: raise OSError("short write")
        position += written
def materialize_accepted_authority(path: Path, *, proposal_path: Path, proposal: Mapping[str, object], proposal_file_sha256: str, acceptance: Mapping[str, object], expectations: AcceptanceExpectations | None) -> None:
    """Persist an already-authorized decision, create-only; never manufacture one."""
    validate_accepted_authority(proposal_path=proposal_path, proposal=proposal, proposal_file_sha256=proposal_file_sha256, acceptance=acceptance, expectations=expectations)
    parent,name,_absolute = _parent_fd(path,label="ACCEPTANCE_OUTPUT")
    flags = os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_CLOEXEC",0)
    fd = -1
    try:
        try: fd=os.open(name,flags,0o600,dir_fd=parent)
        except FileExistsError as exc: raise C5StartClampError("C5_CLAMP_ACCEPTANCE_COLLISION") from exc
        except OSError as exc: raise C5StartClampError("C5_CLAMP_ACCEPTANCE_OUTPUT_UNSAFE") from exc
        try:
            _write_all(fd,_canonical(dict(acceptance))+b"\n"); os.fsync(fd)
        except OSError as exc:
            # Delete only the inode we created; a write error never leaves a usable authority.
            try:
                opened=os.fstat(fd); named=os.stat(name,dir_fd=parent,follow_symlinks=False)
                if _same(opened,named): os.unlink(name,dir_fd=parent); os.fsync(parent)
            except OSError: pass
            raise C5StartClampError("C5_CLAMP_ACCEPTANCE_OUTPUT_WRITE_FAILED") from exc
        finally: os.close(fd); fd=-1
        os.fsync(parent)
    finally:
        if fd >= 0: os.close(fd)
        os.close(parent)
