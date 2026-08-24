"""C2-only private authority adapter for the reviewed-baseline replay lane.

The predecessor record is immutable and intentionally retains its canonical
delivery locator strings.  A private runtime cannot rewrite those bytes
without breaking the record SHA join.  This adapter supplies those strings
only as comparison expectations while denying every non-private file read.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from types import SimpleNamespace

from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthority,
    require_repository_asset_authority,
)
from src.autoslice.reviewed_baseline_replay_authority import (
    RecordBoundFinalizerAuthority,
    resolve_record_bound_finalizer_authority,
)


C2_CANDIDATE_ID = "auto_203011_328_389"
C2_RECORDING_DATE = "2026-08-13"
_C2_UNIFORM_HOST_CLOSURE_RELATIVE = Path(
    "assets/lidousha/fastlane_c2_private/"
    "auto_203011_328_389.uniform-host-chat-closure.v1.json"
)
_C2_UNIFORM_HOST_CLOSURE_SCHEMA = "fastlane-c2-uniform-host-chat-closure.v1"
_C2_UNIFORM_HOST_CLOSURE_RECEIPT_SCHEMA = (
    "c2-private-uniform-host-chat-closure-receipt.v1"
)
_C2_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_C2_PROVENANCE_POINTERS = (
    "/final_recut/output_path",
    "/final_recut/source_path",
    "/padded/inputs/0/path",
    "/padded/output_path",
    "/source_piece/output_path",
)
_C2_PATH_UNAVAILABLE_PREFIX = "C2_PRIVATE_REPLAY_PATH_UNAVAILABLE_"
_C2_REGULAR_PATH_ROLES = {
    "STAGE_DOCUMENT": "REGULAR_STAGE_DOCUMENT_PARENT",
    "RECORD": "REGULAR_RECORD_PARENT",
    "PROVENANCE": "REGULAR_PROVENANCE_PARENT",
    "PADDED_SOURCE": "REGULAR_PADDED_SOURCE_PARENT",
    "PADDED_PROVENANCE": "REGULAR_PADDED_PROVENANCE_PARENT",
    "RELEASE_TRUTH": "REGULAR_RELEASE_TRUTH_PARENT",
    "OLD_SPEAKER_MANIFEST": "REGULAR_OLD_SPEAKER_MANIFEST_PARENT",
    "OPERATOR_DECISION_LEDGER": "REGULAR_OPERATOR_DECISION_LEDGER_PARENT",
    "PIPELINE_DIAGNOSTIC": "REGULAR_PIPELINE_DIAGNOSTIC_PARENT",
    "OPERATOR_TRUTH_DIFF": "REGULAR_OPERATOR_TRUTH_DIFF_PARENT",
    "CHAT_AUTHORITY": "REGULAR_CHAT_AUTHORITY_PARENT",
    "CLIP_CONTEXT": "REGULAR_CLIP_CONTEXT_PARENT",
    "PORTABLE_RECORD": "REGULAR_PORTABLE_RECORD_PARENT",
    "PORTABLE_CHAT_AUTHORITY": "REGULAR_PORTABLE_CHAT_AUTHORITY_PARENT",
    "PORTABLE_CLIP_CONTEXT": "REGULAR_PORTABLE_CLIP_CONTEXT_PARENT",
    "PORTABLE_PUBLISH": "REGULAR_PORTABLE_PUBLISH_PARENT",
    "DEPLOYED_MANIFEST": "REGULAR_DEPLOYED_MANIFEST_PARENT",
    "DELIVERY_PROJECTION_RECEIPT": "REGULAR_DELIVERY_PROJECTION_PARENT",
    "COVER": "REGULAR_COVER_PARENT",
    "COVER_CARRY": "REGULAR_COVER_CARRY_PARENT",
    "PREPARED_HANDLE": "REGULAR_PREPARED_HANDLE_PARENT",
}
_C2_SAFE_DIRECTORY_PATH_ROLES = {
    "synthesize_replay_spec_and_finalize_private": "SYNTHESIS_STAGE",
    "_mkdir_private": "PRIVATE_RUNTIME_PARENT",
    "_copy_private_artifact": "PRIVATE_ARTIFACT_PARENT",
    "_copy_deployed_authority": "DEPLOYED_AUTHORITY_RUNTIME",
    "_production_llm_call": "PROVIDER_RUNTIME",
    "replay_exact_final_reviewer": "EXACT_FINAL_RUNTIME",
    "_validated_padded_provenance": "PADDED_PROVENANCE_PARENT",
    "_private_relative_path": "PRIVATE_COVER_PACKAGE_ROOT",
    "_private_carried_cover_generation": "DEPLOYED_ASSETS_RUNTIME",
}


@dataclass(frozen=True, slots=True)
class _C2UniformHostChatClosure:
    """The C2-only sealed representation of one legacy uniform-host closure."""

    document: Mapping[str, Any]
    repository_authority: RepositoryAssetAuthority


def _c2_error(code: str) -> ValueError:
    return ValueError(code)


def _canonical_sha256(value: object) -> str:
    """Return the canonical semantic digest used by sealed C2 documents."""

    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _exact_sha(value: object) -> bool:
    return isinstance(value, str) and _C2_SHA256.fullmatch(value) is not None


def _closure_error(error: Callable[[str], Exception], suffix: str) -> Exception:
    return error(f"C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_{suffix}")


def _closure_mapping(
    value: object, *, keys: frozenset[str], error: Callable[[str], Exception],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise _closure_error(error, "INVALID")
    return value


def _validate_c2_uniform_host_closure_document(
    document: object, *, error: Callable[[str], Exception],
) -> Mapping[str, Any]:
    """Validate the exact legacy representation without changing its bytes.

    This is deliberately a *representation* seal: it neither creates a
    speaker SRT/ASS nor treats a missing value as generally acceptable.  It
    recognizes exactly C2's pre-existing ``uniform_host`` fallback.
    """

    top = _closure_mapping(
        document,
        keys=frozenset({
            "schema_version", "candidate_id", "recording_date", "scope",
            "predecessor_record", "portable_chat_authority",
            "uniform_host_projection", "authority_sha256",
        }),
        error=error,
    )
    body = dict(top)
    declared = body.pop("authority_sha256", None)
    if (
        top.get("schema_version") != _C2_UNIFORM_HOST_CLOSURE_SCHEMA
        or top.get("candidate_id") != C2_CANDIDATE_ID
        or top.get("recording_date") != C2_RECORDING_DATE
        or top.get("scope") != "PRIVATE_NO_UPLOAD"
        or not _exact_sha(declared)
        or declared != _canonical_sha256(body)
    ):
        raise _closure_error(error, "INVALID")

    predecessor = _closure_mapping(
        top.get("predecessor_record"),
        keys=frozenset({"sha256", "speaker_mode", "subtitle_style", "artifact_hashes"}),
        error=error,
    )
    record_hashes = _closure_mapping(
        predecessor.get("artifact_hashes"),
        keys=frozenset({
            "chat_authority_audit_sha256", "publish_draft_sha256",
            "subtitle_sha256", "speaker_review_srt_sha256", "ass_sha256",
        }),
        error=error,
    )
    if (
        not _exact_sha(predecessor.get("sha256"))
        or predecessor.get("speaker_mode") != "uniform_host"
        or predecessor.get("subtitle_style") != "lidousha-final-sapphire72"
        or any(
            not _exact_sha(record_hashes.get(name))
            for name in (
                "chat_authority_audit_sha256", "publish_draft_sha256",
                "subtitle_sha256", "ass_sha256",
            )
        )
        or record_hashes.get("speaker_review_srt_sha256") is not None
    ):
        raise _closure_error(error, "INVALID")

    chat = _closure_mapping(
        top.get("portable_chat_authority"),
        keys=frozenset({
            "sha256", "schema_version", "status", "final_status",
            "final_output_srt_sha256", "final_text_srt_sha256",
            "final_speaker_srt_sha256", "speaker_ass_sha256",
            "final_review_audit", "structured_chat_binding_audit",
        }),
        error=error,
    )
    review = _closure_mapping(
        chat.get("final_review_audit"),
        keys=frozenset({"schema_version", "status", "reviewed_srt_sha256"}),
        error=error,
    )
    structured = _closure_mapping(
        chat.get("structured_chat_binding_audit"),
        keys=frozenset({"schema_version", "status"}),
        error=error,
    )
    subtitle_sha256 = record_hashes["subtitle_sha256"]
    if (
        not _exact_sha(chat.get("sha256"))
        or chat.get("schema_version") != "chat-authority-audit.v2"
        or chat.get("status") not in {"NO_MATCH", "APPLIED_AND_VERIFIED"}
        or chat.get("final_status") != "FINAL_ARTIFACTS_VERIFIED"
        or any(
            chat.get(name) != subtitle_sha256
            for name in (
                "final_output_srt_sha256", "final_text_srt_sha256",
                "final_speaker_srt_sha256",
            )
        )
        or chat.get("speaker_ass_sha256") is not None
        or review.get("schema_version") != "final-review-audit.v2"
        or review.get("status") != "CLEAN"
        or review.get("reviewed_srt_sha256") != subtitle_sha256
        or structured.get("schema_version") != "structured-chat-binding-audit.v1"
        or structured.get("status") != "PASS"
    ):
        raise _closure_error(error, "INVALID")

    projection = _closure_mapping(
        top.get("uniform_host_projection"),
        keys=frozenset({
            "record_ass_role", "record_ass_sha256", "speaker_ass_role",
            "speaker_srt_role",
        }),
        error=error,
    )
    if (
        projection.get("record_ass_role") != "DISPLAY_ASS"
        or projection.get("record_ass_sha256") != record_hashes["ass_sha256"]
        or projection.get("speaker_ass_role") != "NO_SEPARATE_SPEAKER_ASS"
        or projection.get("speaker_srt_role") != "TEXT_SRT_FALLBACK"
    ):
        raise _closure_error(error, "INVALID")
    return top


def _load_c2_uniform_host_chat_closure(
    *, runtime: Path, private_binding: Callable[..., object],
    private_directory: Callable[[Path], Path], load_json: Callable[..., dict[str, Any]],
    error: Callable[[str], Exception],
) -> _C2UniformHostChatClosure:
    """Load C2's committed/deployed closure seal from the private runtime."""

    try:
        repo = private_directory(runtime / "repo")
        binding = private_binding(
            repo / _C2_UNIFORM_HOST_CLOSURE_RELATIVE,
            label="C2_UNIFORM_HOST_CLOSURE",
        )
        path = Path(getattr(binding, "path"))
        payload = path.read_bytes()
    except Exception as exc:
        if str(exc).startswith("C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_"):
            raise
        raise _closure_error(error, "UNAVAILABLE") from exc
    if (
        not path.is_relative_to(repo)
        or "sha256:" + hashlib.sha256(payload).hexdigest()
        != getattr(binding, "sha256", None)
    ):
        raise _closure_error(error, "ASSET_DRIFT")
    try:
        document = _validate_c2_uniform_host_closure_document(
            load_json(binding, label="C2_UNIFORM_HOST_CLOSURE"), error=error,
        )
        repository_authority = require_repository_asset_authority(
            repo_root=repo,
            relative_path=_C2_UNIFORM_HOST_CLOSURE_RELATIVE,
            observed_bytes=payload,
        )
    except Exception as exc:
        if str(exc).startswith("C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_"):
            raise
        raise _closure_error(error, "UNSEALED") from exc
    return _C2UniformHostChatClosure(document, repository_authority)


def _validate_c2_uniform_host_record(
    *, closure: _C2UniformHostChatClosure, record_binding: object,
    record: Mapping[str, object], error: Callable[[str], Exception],
) -> None:
    predecessor = closure.document.get("predecessor_record")
    if not isinstance(predecessor, Mapping):
        raise _closure_error(error, "INVALID")
    expected_hashes = predecessor.get("artifact_hashes")
    if not isinstance(expected_hashes, Mapping):
        raise _closure_error(error, "INVALID")
    hashes = record.get("artifact_hashes")
    if (
        getattr(record_binding, "sha256", None) != predecessor["sha256"]
        or record.get("speaker_mode") != predecessor["speaker_mode"]
        or record.get("subtitle_style") != predecessor["subtitle_style"]
        or not isinstance(hashes, Mapping)
        or any(hashes.get(name) != value for name, value in expected_hashes.items())
    ):
        raise _closure_error(error, "RECORD_DRIFT")


def _validate_c2_uniform_host_chat(
    binding: object, *, closure: _C2UniformHostChatClosure,
    record: Mapping[str, object], load_json: Callable[..., dict[str, Any]],
    error: Callable[[str], Exception],
) -> None:
    """Validate the sealed fallback fields while retaining every other gate."""

    expected = closure.document.get("portable_chat_authority")
    projection = closure.document.get("uniform_host_projection")
    if not isinstance(expected, Mapping) or not isinstance(projection, Mapping):
        raise _closure_error(error, "INVALID")
    hashes = record.get("artifact_hashes")
    try:
        document = load_json(binding, label="CHAT_AUTHORITY")
    except (OSError, UnicodeError, ValueError) as exc:
        raise _closure_error(error, "CHAT_DRIFT") from exc
    if (
        getattr(binding, "sha256", None) != expected["sha256"]
        or not isinstance(hashes, Mapping)
        or hashes.get("speaker_review_srt_sha256") is not None
        or hashes.get("ass_sha256") != projection.get("record_ass_sha256")
        or any(
            document.get(name) != value
            for name, value in expected.items()
            if name != "sha256"
        )
    ):
        raise _closure_error(error, "CHAT_DRIFT")


def _private_relative_binding(
    binding: object, *, runtime: Path, error: Callable[[str], Exception],
) -> str:
    try:
        return Path(getattr(binding, "path")).absolute().relative_to(runtime).as_posix()
    except (AttributeError, TypeError, ValueError) as exc:
        raise _closure_error(error, "RECEIPT_OUTSIDE_RUNTIME") from exc


def _materialize_c2_uniform_host_chat_closure_receipt(
    *, runtime: Path, plan: Any, record_binding: object,
    authority: RecordBoundFinalizerAuthority, closure: _C2UniformHostChatClosure,
    private_binding: Callable[..., object], private_directory: Callable[[Path], Path],
    load_json: Callable[..., dict[str, Any]], error: Callable[[str], Exception],
) -> Path:
    """Create one checked, private-only receipt for the legacy representation."""

    candidate_root = private_directory(
        runtime / "out" / plan.date / plan.candidate_id
    )
    recut_root = private_directory(candidate_root / "replacement_recuts")
    record_relative = _private_relative_binding(record_binding, runtime=runtime, error=error)
    chat_relative = _private_relative_binding(authority.chat, runtime=runtime, error=error)
    if not record_relative.startswith(
        f"out/{C2_RECORDING_DATE}/{C2_CANDIDATE_ID}/replacement_recuts/"
    ) or not record_relative.endswith(".record.json"):
        raise _closure_error(error, "RECEIPT_RECORD_LOCATOR_DRIFT")
    if not chat_relative.startswith("repo/") or not chat_relative.endswith(".chat-authority.json"):
        raise _closure_error(error, "RECEIPT_CHAT_LOCATOR_DRIFT")
    predecessor = closure.document.get("predecessor_record")
    chat = closure.document.get("portable_chat_authority")
    projection = closure.document.get("uniform_host_projection")
    if (
        not isinstance(predecessor, Mapping)
        or not isinstance(chat, Mapping)
        or not isinstance(projection, Mapping)
    ):
        raise _closure_error(error, "INVALID")
    body: dict[str, object] = {
        "schema_version": _C2_UNIFORM_HOST_CLOSURE_RECEIPT_SCHEMA,
        "scope": "PRIVATE_NO_UPLOAD",
        "candidate_id": C2_CANDIDATE_ID,
        "recording_date": C2_RECORDING_DATE,
        "runtime_root_sha256": "sha256:" + hashlib.sha256(
            str(runtime).encode("utf-8")
        ).hexdigest(),
        "closure_authority": {
            "authority_sha256": closure.document["authority_sha256"],
            "repository_mode": closure.repository_authority.mode,
            "repository_commit": closure.repository_authority.commit,
            "relative_path": closure.repository_authority.relative_path,
            "file_sha256": closure.repository_authority.file_sha256,
        },
        "record": {
            "relative_path": record_relative,
            "sha256": predecessor["sha256"],
            "speaker_mode": predecessor["speaker_mode"],
            "subtitle_style": predecessor["subtitle_style"],
            "artifact_hashes": dict(predecessor["artifact_hashes"]),
        },
        "chat": {
            "relative_path": chat_relative,
            "sha256": chat["sha256"],
            "final_output_srt_sha256": chat["final_output_srt_sha256"],
            "final_text_srt_sha256": chat["final_text_srt_sha256"],
            "final_speaker_srt_sha256": chat["final_speaker_srt_sha256"],
            "speaker_ass_sha256": chat["speaker_ass_sha256"],
        },
        "uniform_host_projection": dict(projection),
    }
    document = dict(body)
    document["receipt_sha256"] = _canonical_sha256(body)
    payload = _canonical_json_bytes(document)
    receipt = recut_root / (
        f"{C2_CANDIDATE_ID}.c2-uniform-host-chat-closure-receipt.v1.json"
    )
    if receipt.is_symlink():
        raise _closure_error(error, "RECEIPT_UNSAFE")
    if receipt.exists():
        try:
            binding = private_binding(receipt, label="C2_UNIFORM_HOST_CLOSURE_RECEIPT")
            actual = load_json(binding, label="C2_UNIFORM_HOST_CLOSURE_RECEIPT")
        except (OSError, UnicodeError, ValueError) as exc:
            raise _closure_error(error, "RECEIPT_UNSAFE") from exc
        if (
            actual != document
            or getattr(binding, "sha256", None)
            != "sha256:" + hashlib.sha256(payload).hexdigest()
        ):
            raise _closure_error(error, "RECEIPT_DRIFT")
        return receipt
    try:
        _write_create_only(receipt, payload)
    except FileExistsError:
        return _materialize_c2_uniform_host_chat_closure_receipt(
            runtime=runtime, plan=plan, record_binding=record_binding,
            authority=authority, closure=closure, private_binding=private_binding,
            private_directory=private_directory, load_json=load_json, error=error,
        )
    except OSError as exc:
        raise _closure_error(error, "RECEIPT_WRITE_FAILED") from exc
    return receipt


def classify_c2_private_path_unavailable(exc: BaseException) -> str:
    """Return a closed C2 call-locus code without retaining a filesystem path.

    ``REPLAY_PATH_UNAVAILABLE`` intentionally elides the failed component.
    C2 needs enough bounded information to distinguish its private-runtime
    bridge from the remaining canonical consumers, but must never emit a raw
    traceback, path, provider response, or arbitrary regular-binding label.
    """

    fallback = _C2_PATH_UNAVAILABLE_PREFIX + "CALL_LOCUS_UNCLASSIFIED"
    if not isinstance(exc, Exception) or str(exc) != "REPLAY_PATH_UNAVAILABLE":
        return fallback
    frames = []
    trace = exc.__traceback__
    while trace is not None:
        frames.append(trace.tb_frame)
        trace = trace.tb_next
    for index, frame in enumerate(frames):
        if (
            frame.f_code.co_name != "_safe_directory"
            or not frame.f_code.co_filename.endswith("src/autoslice/reviewed_baseline_replay.py")
            or index == 0
        ):
            continue
        caller = frames[index - 1]
        if caller.f_code.co_filename.endswith("src/autoslice/fastlane_c2_private_authority.py"):
            if caller.f_code.co_name == "private_directory":
                return _C2_PATH_UNAVAILABLE_PREFIX + "C2_AUTHORITY_PRIVATE_RUNTIME"
            return fallback
        if not caller.f_code.co_filename.endswith("src/autoslice/reviewed_baseline_replay.py"):
            return fallback
        if caller.f_code.co_name == "regular_binding":
            role = _C2_REGULAR_PATH_ROLES.get(caller.f_locals.get("label"))
            return _C2_PATH_UNAVAILABLE_PREFIX + (role or "REGULAR_BINDING_UNCLASSIFIED")
        role = _C2_SAFE_DIRECTORY_PATH_ROLES.get(caller.f_code.co_name)
        return _C2_PATH_UNAVAILABLE_PREFIX + (role or "CALL_LOCUS_UNCLASSIFIED")
    return fallback


def _regular(path: Path, *, label: str) -> tuple[Path, str, int]:
    """Bind one C2-private regular file without following a symlink."""

    candidate = Path(path).absolute()
    try:
        observed = os.lstat(candidate)
    except OSError as exc:
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_MISSING") from exc
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (observed.st_dev, observed.st_ino):
                raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_DRIFT")
            digest = hashlib.sha256()
            while block := os.read(descriptor, 1024 * 1024):
                digest.update(block)
        finally:
            os.close(descriptor)
        after = os.lstat(candidate)
    except OSError as exc:
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_UNSAFE") from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        != (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns)
    ):
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_DRIFT")
    return candidate, digest.hexdigest(), observed.st_size


def _json_object(path: Path, *, label: str) -> tuple[dict[str, Any], str, int]:
    bound, digest, size = _regular(path, label=label)
    try:
        value = json.loads(bound.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_INVALID") from exc
    if not isinstance(value, dict):
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_INVALID")
    return value, digest, size


def _private_root(runtime_root: Path) -> Path:
    root = Path(runtime_root).absolute()
    cursor = Path(root.anchor)
    try:
        for part in root.parts[1:]:
            cursor /= part
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError("unsafe runtime component")
    except OSError as exc:
        raise _c2_error("C2_PRIVATE_PROVENANCE_RUNTIME_UNSAFE") from exc
    return root


def _inside(path: Path, root: Path, *, label: str) -> Path:
    candidate = Path(path).absolute()
    if not candidate.is_relative_to(root):
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_ESCAPES_RUNTIME")
    return candidate


def _private_directory(path: Path, root: Path, *, label: str) -> Path:
    candidate = _inside(path, root, label=label)
    cursor = root
    try:
        for part in candidate.relative_to(root).parts:
            cursor /= part
            info = os.lstat(cursor)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise OSError("unsafe private directory")
    except OSError as exc:
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_UNSAFE") from exc
    return candidate


def _sha_field(value: object, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_HASH_INVALID")
    return value


def _path_field(section: Mapping[str, Any], key: str, *, label: str) -> Path:
    value = section.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _c2_error(f"C2_PRIVATE_PROVENANCE_{label}_INVALID")
    return Path(value)


def _write_create_only(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise _c2_error("C2_PRIVATE_PROVENANCE_WRITE_FAILED")
            view = view[count:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def materialize_c2_private_provenance(*, runtime_root: Path, candidate_id: str, date: str) -> Path:
    """Create the C2-only current-runtime provenance projection.

    The copied predecessor provenance is retained under a non-discoverable
    preimage name.  The generic replay sees exactly one ``*.recut.provenance``
    file, while the receipt binds every permitted locator rewrite and its
    concrete private byte binding.
    """

    if candidate_id != C2_CANDIDATE_ID or date != C2_RECORDING_DATE:
        raise _c2_error("C2_PRIVATE_PROVENANCE_CANDIDATE_SCOPE_INVALID")
    runtime = _private_root(runtime_root)
    candidate_root = _private_directory(runtime / "out" / date / candidate_id, runtime, label="CANDIDATE")
    recut_root = _private_directory(candidate_root / "replacement_recuts", candidate_root, label="RECUT")
    target = recut_root / f"{candidate_id}.recut.provenance.json"
    receipt = recut_root / f"{candidate_id}.recut.provenance.c2-private-projection-receipt.json"

    # A completed private projection is immutable.  It must be verified, never
    # overwritten merely because a subsequent invocation has a different root.
    if receipt.exists():
        document, _, _ = _json_object(receipt, label="RECEIPT")
        if (
            document.get("runtime_root") != str(runtime)
            or document.get("candidate_id") != candidate_id
            or document.get("date") != date
            or document.get("allowed_pointers") != list(_C2_PROVENANCE_POINTERS)
        ):
            raise _c2_error("C2_PRIVATE_PROVENANCE_STALE_PREVIOUS_ROOT")
        projected, projected_sha, _ = _json_object(target, label="PROJECTED")
        if document.get("projected_provenance_sha256") != "sha256:" + projected_sha:
            raise _c2_error("C2_PRIVATE_PROVENANCE_PROJECTED_DRIFT")
        return target
    if not target.exists() or target.is_symlink():
        raise _c2_error("C2_PRIVATE_PROVENANCE_PREIMAGE_MISSING")
    provenance, preimage_sha, preimage_bytes = _json_object(target, label="PREIMAGE")
    final, padded, source_piece = (provenance.get(key) for key in ("final_recut", "padded", "source_piece"))
    if not all(isinstance(value, Mapping) for value in (final, padded, source_piece)):
        raise _c2_error("C2_PRIVATE_PROVENANCE_INVALID")
    if set(key for key in final if key.endswith("path")) != {"output_path", "source_path"}:
        raise _c2_error("C2_PRIVATE_PROVENANCE_EXTRA_POINTER")
    if set(key for key in padded if key.endswith("path")) != {"output_path"}:
        raise _c2_error("C2_PRIVATE_PROVENANCE_EXTRA_POINTER")
    inputs = padded.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], Mapping) or set(key for key in inputs[0] if key.endswith("path")) != {"path"}:
        raise _c2_error("C2_PRIVATE_PROVENANCE_EXTRA_POINTER")
    if set(key for key in source_piece if key.endswith("path")) != {"output_path", "source_path"}:
        raise _c2_error("C2_PRIVATE_PROVENANCE_EXTRA_POINTER")
    old_final_output = _path_field(final, "output_path", label="FINAL_OUTPUT_PATH")
    old_padded = _path_field(final, "source_path", label="FINAL_SOURCE_PATH")
    old_input = _path_field(inputs[0], "path", label="PADDED_INPUT_PATH")
    old_padded_output = _path_field(padded, "output_path", label="PADDED_OUTPUT_PATH")
    old_piece = _path_field(source_piece, "output_path", label="PIECE_OUTPUT_PATH")
    if old_padded != old_padded_output or old_input != old_piece:
        raise _c2_error("C2_PRIVATE_PROVENANCE_STALE_PREVIOUS_ROOT")
    final_output = recut_root / f"{candidate_id}.recut.mp4"
    padded_target = candidate_root / old_padded.name
    piece_target = candidate_root / old_piece.name
    if (
        old_final_output.name != final_output.name
        or not old_padded.name.startswith("padded_")
        or not old_piece.name.startswith("piece_")
    ):
        raise _c2_error("C2_PRIVATE_PROVENANCE_STALE_PREVIOUS_ROOT")
    bindings = {
        "/final_recut/output_path": (final_output, _sha_field(final.get("output_sha256"), label="FINAL_OUTPUT")),
        "/final_recut/source_path": (padded_target, _sha_field(final.get("source_sha256"), label="FINAL_SOURCE")),
        "/padded/inputs/0/path": (piece_target, _sha_field(inputs[0].get("sha256"), label="PADDED_INPUT")),
        "/padded/output_path": (padded_target, _sha_field(padded.get("output_sha256"), label="PADDED_OUTPUT")),
        "/source_piece/output_path": (piece_target, _sha_field(source_piece.get("output_sha256"), label="PIECE_OUTPUT")),
    }
    rows = []
    for pointer, (path, expected_sha) in bindings.items():
        _inside(path, candidate_root, label="TARGET")
        bound, actual_sha, bytes_count = _regular(path, label="TARGET")
        if actual_sha != expected_sha:
            raise _c2_error("C2_PRIVATE_PROVENANCE_TARGET_HASH_MISMATCH")
        rows.append({"pointer": pointer, "old": {"path": str({
            "/final_recut/output_path": old_final_output, "/final_recut/source_path": old_padded,
            "/padded/inputs/0/path": old_input, "/padded/output_path": old_padded_output,
            "/source_piece/output_path": old_piece,
        }[pointer])}, "new": {"path": str(bound), "sha256": "sha256:" + actual_sha, "bytes": bytes_count}})
    preimage = recut_root / f"{candidate_id}.recut.provenance.preimage.{preimage_sha}.json"
    if preimage.exists():
        raise _c2_error("C2_PRIVATE_PROVENANCE_PREIMAGE_EXISTS")
    os.replace(target, preimage)
    try:
        final = dict(final); padded = dict(padded); source_piece = dict(source_piece); inputs = [dict(inputs[0])]
        final["output_path"], final["source_path"] = str(final_output), str(padded_target)
        inputs[0]["path"], padded["inputs"], padded["output_path"] = str(piece_target), inputs, str(padded_target)
        source_piece["output_path"] = str(piece_target)
        projected = dict(provenance); projected.update({"final_recut": final, "padded": padded, "source_piece": source_piece})
        projected_bytes = (json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        _write_create_only(target, projected_bytes)
        projected_sha = hashlib.sha256(projected_bytes).hexdigest()
        document = {
            "schema_version": "c2-private-runtime-provenance-projection.v1", "scope": "PRIVATE_NO_UPLOAD",
            "runtime_root": str(runtime), "candidate_id": candidate_id, "date": date,
            "allowed_pointers": list(_C2_PROVENANCE_POINTERS),
            "source_provenance": {"path": str(preimage), "sha256": "sha256:" + preimage_sha, "bytes": preimage_bytes},
            "projected_provenance": {"path": str(target), "sha256": "sha256:" + projected_sha, "bytes": len(projected_bytes)},
            "projected_provenance_sha256": "sha256:" + projected_sha, "padded_sha256": "sha256:" + bindings["/final_recut/source_path"][1],
            "rows": rows,
        }
        _write_create_only(receipt, (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    except BaseException:
        # The preimage remains authoritative and the failed target is never a
        # valid generic input; recovery must inspect this private-only state.
        raise
    return target


def _canonical_locator_root(*, plan: Any, record: Mapping[str, object]) -> Path:
    """Extract the two immutable old-record locator strings without opening them."""

    chat = record.get("chat_authority_audit_path")
    clip = record.get("clip_context_path")
    if not isinstance(chat, str) or not isinstance(clip, str):
        raise ValueError("C2_PRIVATE_AUTHORITY_CANONICAL_LOCATOR_MISSING")
    chat_path, clip_path = Path(chat), Path(clip)
    root = chat_path.parent
    if (
        not chat_path.is_absolute()
        or not clip_path.is_absolute()
        or clip_path.parent != root
        or chat_path.name != f"{plan.candidate_id}.chat-authority.json"
        or clip_path.name != f"{plan.candidate_id}.clip-context.json"
    ):
        raise ValueError("C2_PRIVATE_AUTHORITY_CANONICAL_LOCATOR_DRIFT")
    return root


def resolve_c2_private_record_authority(
    *, plan: Any, record_binding: object, record: Mapping[str, object],
    runtime_authority_root: Path, source_media_sha256: str,
    regular_binding: Callable[..., object], safe_directory: Callable[[Path], Path],
    load_json: Callable[..., dict[str, Any]], error: Callable[[str], Exception],
) -> RecordBoundFinalizerAuthority:
    """Resolve C2 sidecars without ever dereferencing its canonical `/opt` strings."""

    if plan.candidate_id != C2_CANDIDATE_ID or plan.date != C2_RECORDING_DATE:
        raise error("C2_PRIVATE_AUTHORITY_CANDIDATE_SCOPE_INVALID")
    runtime = safe_directory(Path(runtime_authority_root))
    story = record.get("story_contract")
    if not isinstance(story, Mapping) or story.get("candidate_id") != plan.candidate_id:
        raise error("C2_PRIVATE_AUTHORITY_RECORD_CANDIDATE_DRIFT")
    try:
        canonical_root = _canonical_locator_root(plan=plan, record=record)
    except ValueError as exc:
        raise error(str(exc)) from exc
    if canonical_root == plan.package_root or canonical_root.is_relative_to(runtime):
        raise error("C2_PRIVATE_AUTHORITY_CANONICAL_LOCATOR_DRIFT")

    def private_binding(path: Path, *, label: str) -> object:
        candidate = Path(path).absolute()
        if not candidate.is_relative_to(runtime):
            raise error("C2_PRIVATE_AUTHORITY_READ_OUTSIDE_RUNTIME")
        return regular_binding(candidate, label=label)

    def private_directory(path: Path) -> Path:
        candidate = safe_directory(Path(path))
        if not candidate.is_relative_to(runtime):
            raise error("C2_PRIVATE_AUTHORITY_READ_OUTSIDE_RUNTIME")
        return candidate

    closure = _load_c2_uniform_host_chat_closure(
        runtime=runtime, private_binding=private_binding,
        private_directory=private_directory, load_json=load_json, error=error,
    )
    _validate_c2_uniform_host_record(
        closure=closure, record_binding=record_binding, record=record, error=error,
    )

    # The resolver's portable-record check compares immutable locator strings
    # against `plan.package_root`.  Only that comparison gets a proxy; all
    # paths actually passed to `private_binding` remain under `runtime`.
    proxy = (
        replace(plan, package_root=canonical_root)
        if is_dataclass(plan)
        else SimpleNamespace(
            candidate_id=plan.candidate_id, date=plan.date,
            package_root=canonical_root,
        )
    )
    authority = resolve_record_bound_finalizer_authority(
        plan=proxy, record_binding=record_binding, record=record,
        runtime_authority_root=runtime, source_media_sha256=source_media_sha256,
        regular_binding=private_binding, safe_directory=private_directory,
        load_json=load_json, error=error,
        chat_validator=lambda binding, *, record, load_json, error:
            _validate_c2_uniform_host_chat(
                binding, closure=closure, record=record,
                load_json=load_json, error=error,
            ),
    )
    if authority.source != "portable-record-mirror":
        raise error("C2_PRIVATE_AUTHORITY_UNIFORM_HOST_CLOSURE_SOURCE_DRIFT")
    for binding in (authority.chat, authority.clip_context):
        path = Path(getattr(binding, "path"))
        if not path.is_relative_to(runtime):
            raise error("C2_PRIVATE_AUTHORITY_RETURN_OUTSIDE_RUNTIME")
    _materialize_c2_uniform_host_chat_closure_receipt(
        runtime=runtime, plan=plan, record_binding=record_binding,
        authority=authority, closure=closure, private_binding=private_binding,
        private_directory=private_directory, load_json=load_json, error=error,
    )
    return authority
