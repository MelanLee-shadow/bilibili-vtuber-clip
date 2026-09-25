"""Fail-closed verification for package-carried private boundary authority."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from src.autoslice.boundary_semantic_review import boundary_search_scope_is_valid
from src.autoslice.clip_context import ClipContextError, validate_clip_context
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_boundary_owner_contract import (
    frozen_boundary_owner_contract_sha256,
    validate_frozen_boundary_owner_contract,
)
from src.autoslice.review_package_portable_evidence import contained_package_artifact
from src.autoslice.structured_chat_payoff import assessment_sha256, validate_assessment
from src.autoslice.subtitle_validation import validate_srt_text

RESULT_SCHEMA = "surgery-formal-boundary-result.v1"
RESULT_STATUS = "PASS_FORMAL_TYPED_BOUNDARY_AUTHORITY_CONSUMED_BY_PRIVATE_PACKAGE"
AUTHORITY_SCHEMA = "surgery-boundary-package-authority.v1"
CONSUMER_SCHEMA = "surgery-boundary-package-consumer.v1"
MANIFEST_SCHEMA = "surgery-private-boundary-package.v1"
MANIFEST_STATUS = "PRIVATE_BOUNDARY_PACKAGE_READY"
VERIFICATION_SCHEMA = "private-boundary-package-verification.v1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_JSON_BYTES = 16 * 1024 * 1024
_MAX_SRT_BYTES = 32 * 1024 * 1024

MediaProbe = Callable[[Path], Mapping[str, object]]


class PrivateBoundaryPackageError(ValueError):
    """A typed package-authority verification failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _fail(code: str, detail: str) -> None:
    raise PrivateBoundaryPackageError(code, detail)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _normalized_sha256(value: object, *, label: str) -> str:
    text = str(value or "")
    if not _SHA256_RE.fullmatch(text):
        _fail("PRIVATE_BOUNDARY_SHA_INVALID", f"{label}: {text!r}")
    return text


def _int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("PRIVATE_BOUNDARY_INTEGER_INVALID", f"{label}: {value!r}")
    return value


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("PRIVATE_BOUNDARY_OBJECT_INVALID", label)
    return value


def _reject_symlink_components(path: Path, *, label: str) -> Path:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            cursor /= part
            if stat.S_ISLNK(os.lstat(cursor).st_mode):
                _fail("PRIVATE_BOUNDARY_SYMLINK_FORBIDDEN", f"{label}: {cursor}")
    except FileNotFoundError as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_PATH_MISSING", f"{label}: {path}"
        ) from exc
    return absolute


def _stable_regular_binding(path: Path, *, label: str) -> dict[str, object]:
    absolute = _reject_symlink_components(path, label=label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(absolute, flags)
    except OSError as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_FILE_OPEN_FAILED", f"{label}: {absolute}: {exc}"
        ) from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _fail("PRIVATE_BOUNDARY_FILE_NOT_REGULAR", f"{label}: {absolute}")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    named = os.stat(absolute, follow_symlinks=False)
    def identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            stat.S_IMODE(value.st_mode),
            value.st_uid,
            value.st_gid,
            value.st_nlink,
        )
    if identity(before) != identity(after) or identity(after) != identity(named):
        _fail("PRIVATE_BOUNDARY_FILE_DRIFT", f"{label}: {absolute}")
    if total != after.st_size:
        _fail("PRIVATE_BOUNDARY_FILE_SHORT_READ", f"{label}: {absolute}")
    return {
        "path": str(absolute),
        "sha256": "sha256:" + digest.hexdigest(),
        "bytes": total,
        "device": after.st_dev,
        "inode": after.st_ino,
        "nlink": after.st_nlink,
        "mode": stat.S_IMODE(after.st_mode),
    }


def _read_small_regular(path: Path, *, label: str, maximum: int) -> tuple[bytes, dict[str, object]]:
    binding = _stable_regular_binding(path, label=label)
    if int(binding["bytes"]) > maximum:
        _fail("PRIVATE_BOUNDARY_FILE_TOO_LARGE", f"{label}: {binding['bytes']} > {maximum}")
    absolute = Path(str(binding["path"]))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute, flags)
    try:
        payload = b""
        while len(payload) <= maximum:
            chunk = os.read(fd, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(fd)
    if len(payload) != binding["bytes"] or "sha256:" + hashlib.sha256(payload).hexdigest() != binding["sha256"]:
        _fail("PRIVATE_BOUNDARY_FILE_DRIFT", f"{label}: {absolute}")
    return payload, binding


def _load_json(path: Path, *, label: str) -> tuple[dict[str, object], dict[str, object]]:
    payload, binding = _read_small_regular(path, label=label, maximum=_MAX_JSON_BYTES)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_JSON_INVALID", f"{label}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        _fail("PRIVATE_BOUNDARY_JSON_INVALID", f"{label}: root is not an object")
    return value, binding


def _package_root(path: Path) -> Path:
    absolute = _reject_symlink_components(path, label="package root")
    if not absolute.is_dir():
        _fail("PRIVATE_BOUNDARY_PACKAGE_ROOT_INVALID", str(absolute))
    return absolute.resolve(strict=True)


def _package_artifact(root: Path, value: object, *, label: str) -> Path:
    try:
        return contained_package_artifact(root, value, label=label)
    except ValueError as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_ARTIFACT_UNSAFE", f"{label}: {exc}"
        ) from exc


def _verify_declared_file(
    entry: object,
    *,
    label: str,
    package_root: Path | None = None,
    cache: dict[Path, dict[str, object]],
) -> tuple[Path, dict[str, object]]:
    item = _mapping(entry, label=label)
    value = item.get("path")
    path = (
        _package_artifact(package_root, value, label=label)
        if package_root is not None
        else Path(str(value or ""))
    )
    resolved = path.resolve(strict=True)
    binding = cache.get(resolved)
    if binding is None:
        binding = _stable_regular_binding(path, label=label)
        cache[resolved] = binding
    if binding["sha256"] != _normalized_sha256(item.get("sha256"), label=f"{label}.sha256"):
        _fail("PRIVATE_BOUNDARY_HASH_MISMATCH", label)
    declared_bytes = item.get("bytes")
    if declared_bytes is not None and binding["bytes"] != _int(declared_bytes, label=f"{label}.bytes"):
        _fail("PRIVATE_BOUNDARY_SIZE_MISMATCH", label)
    return path, binding


def _ffprobe_media(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        _fail("PRIVATE_BOUNDARY_MEDIA_PROBE_FAILED", completed.stderr[-400:])
    try:
        raw = json.loads(completed.stdout)
        streams = raw.get("streams") or []
        duration_ms = round(float(raw["format"]["duration"]) * 1000)
        size_bytes = int(raw["format"]["size"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_MEDIA_PROBE_INVALID", str(exc)
        ) from exc
    return {
        "duration_ms": duration_ms,
        "size_bytes": size_bytes,
        "video_stream_count": sum(
            1
            for stream in streams
            if stream.get("codec_type") == "video"
            and int(stream.get("width") or 0) > 0
            and int(stream.get("height") or 0) > 0
        ),
        "audio_stream_count": sum(
            1
            for stream in streams
            if stream.get("codec_type") == "audio"
            and int(stream.get("sample_rate") or 0) > 0
            and int(stream.get("channels") or 0) > 0
        ),
        "streams": streams,
    }


def _verify_hardlink_source(
    artifact: Mapping[str, object],
    *,
    target: dict[str, object],
    label: str,
) -> dict[str, object]:
    source_value = artifact.get("hardlinked_from")
    if not isinstance(source_value, str) or not source_value:
        _fail("PRIVATE_BOUNDARY_HARDLINK_SOURCE_MISSING", label)
    source = _stable_regular_binding(Path(source_value), label=f"{label}.hardlinked_from")
    if (
        source["sha256"] != target["sha256"]
        or source["bytes"] != target["bytes"]
        or source["device"] != target["device"]
        or source["inode"] != target["inode"]
    ):
        _fail("PRIVATE_BOUNDARY_HARDLINK_PROVENANCE_MISMATCH", label)
    return source


def _verify_zero_mutation_surfaces(
    result: Mapping[str, object],
    authority: Mapping[str, object],
    consumer: Mapping[str, object],
    manifest: Mapping[str, object],
) -> None:
    result_acceptance = _mapping(result.get("acceptance"), label="result.acceptance")
    authority_acceptance = _mapping(authority.get("acceptance"), label="authority.acceptance")
    for key in ("provider_calls", "media_mutations", "subtitle_mutations"):
        if _int(result_acceptance.get(key), label=f"result.acceptance.{key}") != 0:
            _fail("PRIVATE_BOUNDARY_MUTATION_FORBIDDEN", f"result.{key}")
        if _int(authority_acceptance.get(key), label=f"authority.acceptance.{key}") != 0:
            _fail("PRIVATE_BOUNDARY_MUTATION_FORBIDDEN", f"authority.{key}")
        if _int(consumer.get(key), label=f"consumer.{key}") != 0:
            _fail("PRIVATE_BOUNDARY_MUTATION_FORBIDDEN", f"consumer.{key}")
    publication = _mapping(result.get("publication"), label="result.publication")
    forbidden_true = {
        "result.upload_attempted": publication.get("upload_attempted"),
        "result.public_release": publication.get("public_release"),
        "result.production_state_mutated": publication.get("production_state_mutated"),
        "authority.upload_allowed": authority_acceptance.get("upload_allowed"),
        "consumer.upload_allowed": consumer.get("upload_allowed"),
        "manifest.upload_allowed": manifest.get("upload_allowed"),
        "manifest.public": manifest.get("public"),
        "manifest.production_adopted": manifest.get("production_adopted"),
    }
    if any(value is not False for value in forbidden_true.values()):
        _fail("PRIVATE_BOUNDARY_RELEASE_AUTHORITY_FORBIDDEN", repr(forbidden_true))


def _verify_source_identity(
    clip_context_path: Path,
    *,
    candidate_id: str,
    recording_date: str,
    authority: Mapping[str, object],
    result: Mapping[str, object],
    manifest: Mapping[str, object],
    consumer: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    context, binding = _load_json(Path(clip_context_path), label="clip context")
    try:
        context = validate_clip_context(
            context, candidate_id=candidate_id, recording_date=recording_date
        )
    except ClipContextError as exc:
        raise PrivateBoundaryPackageError("PRIVATE_BOUNDARY_CLIP_CONTEXT_INVALID", str(exc)) from exc
    # validate_clip_context checks the context digest and source hashes; validate
    # every piece's interval and recording locator before sealing its identity.
    for index, value in enumerate(context["pieces"]):
        piece = _mapping(value, label=f"clip_context.pieces[{index}]")
        start = _int(piece.get("start_ms"), label="piece.start_ms")
        _int(piece.get("end_ms"), label="piece.end_ms", minimum=start + 1)
        _normalized_sha256(piece.get("source_media_sha256"), label="piece.source_media_sha256")
        basename = piece.get("recording_basename")
        if not isinstance(basename, str) or not basename or Path(basename).name != basename:
            _fail("PRIVATE_BOUNDARY_SOURCE_PIECE_INVALID", f"piece {index}: recording_basename")
    identity = {
        "schema_version": "private-boundary-source-identity.v1",
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "clip_context_file_sha256": binding["sha256"],
        "context_sha256": context["context_sha256"],
        "pieces": context["pieces"],
    }
    identity["source_identity_sha256"] = _canonical_sha256(identity)
    for label, entries in (
        ("authority.artifacts", authority.get("artifacts")),
        ("result.inputs", result.get("inputs")),
    ):
        entry = _mapping(_mapping(entries, label=label).get("clip_context"), label=f"{label}.clip_context")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            _fail("PRIVATE_BOUNDARY_CLIP_CONTEXT_BINDING_MISMATCH", f"{label}.path")
        declared_path = _reject_symlink_components(Path(path), label=f"{label}.clip_context")
        if declared_path.resolve(strict=True) != Path(str(binding["path"])).resolve(strict=True):
            _fail("PRIVATE_BOUNDARY_CLIP_CONTEXT_BINDING_MISMATCH", f"{label}.path")
        if entry.get("sha256") != binding["sha256"]:
            _fail("PRIVATE_BOUNDARY_CLIP_CONTEXT_BINDING_MISMATCH", f"{label}.sha256")
        if "bytes" in entry and _int(entry["bytes"], label=f"{label}.bytes") != binding["bytes"]:
            _fail("PRIVATE_BOUNDARY_SIZE_MISMATCH", f"{label}.clip_context")
    if _canonical_sha256(authority.get("source_identity")) != _canonical_sha256(identity):
        _fail("PRIVATE_BOUNDARY_SOURCE_IDENTITY_MISMATCH", "authority.source_identity")
    if manifest.get("source_identity_sha256") != identity["source_identity_sha256"]:
        _fail("PRIVATE_BOUNDARY_SOURCE_IDENTITY_MISMATCH", "manifest.source_identity_sha256")
    for key, expected in (
        ("verified_source_identity_sha256", identity["source_identity_sha256"]),
        ("verified_clip_context_file_sha256", binding["sha256"]),
        ("verified_context_sha256", context["context_sha256"]),
    ):
        if consumer.get(key) != expected:
            _fail("PRIVATE_BOUNDARY_CONSUMER_BINDING_MISMATCH", key)
    return identity, binding


def verify_private_boundary_package(
    package_root: Path,
    *,
    clip_context_path: Path,
    result_path: Path | None = None,
    probe_media: MediaProbe | None = None,
) -> dict[str, object]:
    """Verify one private package without granting ordinary-package or upload status."""

    root = _package_root(Path(package_root))
    result_path = Path(result_path) if result_path is not None else root.parent / "RESULT.json"
    result, result_binding = _load_json(result_path, label="formal result")
    authority_path = root / "boundary-authority.json"
    consumer_path = root / "consumer-receipt.json"
    manifest_path = root / "package-manifest.json"
    authority, authority_binding = _load_json(authority_path, label="boundary authority")
    consumer, consumer_binding = _load_json(consumer_path, label="consumer receipt")
    manifest, manifest_binding = _load_json(manifest_path, label="package manifest")

    headers = (
        (result, RESULT_SCHEMA, RESULT_STATUS, "result"),
        (authority, AUTHORITY_SCHEMA, "PASS", "authority"),
        (consumer, CONSUMER_SCHEMA, "PASS", "consumer"),
        (manifest, MANIFEST_SCHEMA, MANIFEST_STATUS, "manifest"),
    )
    for document, schema, status_value, label in headers:
        if document.get("schema_version") != schema or document.get("status") != status_value:
            _fail("PRIVATE_BOUNDARY_SCHEMA_OR_STATUS_INVALID", label)
    candidate_id = str(result.get("candidate_id") or "")
    recording_date = str(result.get("recording_date") or "")
    if not candidate_id or not recording_date:
        _fail("PRIVATE_BOUNDARY_IDENTITY_MISSING", "candidate_id/recording_date")
    if any(document.get("candidate_id") != candidate_id for document in (authority, consumer, manifest)):
        _fail("PRIVATE_BOUNDARY_CANDIDATE_MISMATCH", candidate_id)
    if any(document.get("recording_date") != recording_date for document in (authority, manifest)):
        _fail("PRIVATE_BOUNDARY_RECORDING_DATE_MISMATCH", recording_date)

    cache: dict[Path, dict[str, object]] = {
        Path(str(authority_binding["path"])).resolve(strict=True): authority_binding,
        Path(str(consumer_binding["path"])).resolve(strict=True): consumer_binding,
        Path(str(manifest_binding["path"])).resolve(strict=True): manifest_binding,
    }
    result_outputs = _mapping(result.get("outputs"), label="result.outputs")
    output_bindings = {
        "boundary_authority": authority_binding,
        "consumer_receipt": consumer_binding,
        "package_manifest": manifest_binding,
    }
    for name, expected in output_bindings.items():
        _, observed = _verify_declared_file(
            result_outputs.get(name), label=f"result.outputs.{name}", package_root=root, cache=cache
        )
        if observed["sha256"] != expected["sha256"]:
            _fail("PRIVATE_BOUNDARY_RESULT_OUTPUT_MISMATCH", name)

    authority_body = {key: value for key, value in authority.items() if key != "authority_sha256"}
    authority_sha = _normalized_sha256(authority.get("authority_sha256"), label="authority.authority_sha256")
    if authority_sha != _canonical_sha256(authority_body):
        _fail("PRIVATE_BOUNDARY_AUTHORITY_SELF_SEAL_MISMATCH", str(authority_path))
    if result_outputs["boundary_authority"].get("authority_sha256") != authority_sha:
        _fail("PRIVATE_BOUNDARY_RESULT_AUTHORITY_SEAL_MISMATCH", candidate_id)

    source_identity, context_binding = _verify_source_identity(
        clip_context_path, candidate_id=candidate_id, recording_date=recording_date,
        authority=authority, result=result, manifest=manifest, consumer=consumer,
    )
    cache[Path(str(context_binding["path"])).resolve(strict=True)] = context_binding

    artifacts = _mapping(manifest.get("artifacts"), label="manifest.artifacts")
    media_entry = _mapping(artifacts.get("media"), label="manifest.artifacts.media")
    subtitle_entry = _mapping(artifacts.get("subtitle"), label="manifest.artifacts.subtitle")
    media_path = _package_artifact(root, media_entry.get("path"), label="media")
    subtitle_path = _package_artifact(root, subtitle_entry.get("path"), label="subtitle")
    media_binding = _stable_regular_binding(media_path, label="media")
    subtitle_payload, subtitle_binding = _read_small_regular(
        subtitle_path, label="subtitle", maximum=_MAX_SRT_BYTES
    )
    cache[media_path.resolve(strict=True)] = media_binding
    cache[subtitle_path.resolve(strict=True)] = subtitle_binding
    for label, entry, binding in (
        ("media", media_entry, media_binding),
        ("subtitle", subtitle_entry, subtitle_binding),
    ):
        if binding["sha256"] != _normalized_sha256(entry.get("sha256"), label=f"{label}.sha256"):
            _fail("PRIVATE_BOUNDARY_HASH_MISMATCH", label)
        if binding["bytes"] != _int(entry.get("bytes"), label=f"{label}.bytes"):
            _fail("PRIVATE_BOUNDARY_SIZE_MISMATCH", label)

    authority_artifacts = _mapping(authority.get("artifacts"), label="authority.artifacts")
    for name, entry in authority_artifacts.items():
        package = root if name in {"media", "subtitle", "package_manifest"} else None
        _verify_declared_file(entry, label=f"authority.artifacts.{name}", package_root=package, cache=cache)
    for name, entry in _mapping(result.get("inputs"), label="result.inputs").items():
        _verify_declared_file(entry, label=f"result.inputs.{name}", cache=cache)

    if manifest.get("large_media_copied") is not False:
        _fail("PRIVATE_BOUNDARY_COPY_MODE_INVALID", "large_media_copied must be false")
    hardlinks = {
        "media": _verify_hardlink_source(media_entry, target=media_binding, label="media"),
        "subtitle": _verify_hardlink_source(subtitle_entry, target=subtitle_binding, label="subtitle"),
    }

    if _package_artifact(root, consumer.get("authority_path"), label="consumer authority") != authority_path:
        _fail("PRIVATE_BOUNDARY_CONSUMER_AUTHORITY_PATH_MISMATCH", candidate_id)
    expected_consumer = {
        "authority_file_sha256": authority_binding["sha256"],
        "authority_sha256": authority_sha,
        "verified_media_sha256": media_binding["sha256"],
        "verified_subtitle_sha256": subtitle_binding["sha256"],
        "verified_package_manifest_sha256": manifest_binding["sha256"],
    }
    for key, expected in expected_consumer.items():
        if consumer.get(key) != expected:
            _fail("PRIVATE_BOUNDARY_CONSUMER_BINDING_MISMATCH", key)

    try:
        contract = validate_frozen_boundary_owner_contract(
            authority.get("frozen_boundary_owner_contract")
        )
    except RuntimeError as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_OWNER_CONTRACT_INVALID", str(exc)
        ) from exc
    if contract.get("contract_sha256") != frozen_boundary_owner_contract_sha256(contract):
        _fail("PRIVATE_BOUNDARY_OWNER_CONTRACT_HASH_MISMATCH", candidate_id)
    scope = contract.get("boundary_search_scope")
    if not boundary_search_scope_is_valid(scope):
        _fail("PRIVATE_BOUNDARY_SEARCH_SCOPE_INVALID", candidate_id)
    try:
        assessment = validate_assessment(contract.get("structured_chat_payoff_assessment"))
    except RuntimeError as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_PAYOFF_ASSESSMENT_INVALID", str(exc)
        ) from exc
    if assessment.get("assessment_sha256") != assessment_sha256(assessment):
        _fail("PRIVATE_BOUNDARY_PAYOFF_HASH_MISMATCH", candidate_id)
    excluded = assessment.get("excluded_row_refs")
    story_end_ms = _int(contract.get("story_end_ms"), label="contract.story_end_ms")
    if (
        assessment.get("effective_story_ms") is not None
        or assessment.get("scope_ms") is not None
        or not isinstance(excluded, list)
        or not excluded
        or any(
            not isinstance(row, Mapping)
            or row.get("reason_code") != "OUTSIDE_IMMUTABLE_STORY_SCOPE"
            or _int(row.get("matched_start_ms"), label="excluded.matched_start_ms") < story_end_ms
            for row in excluded
        )
    ):
        _fail("PRIVATE_BOUNDARY_PAYOFF_EXCLUSION_INVALID", candidate_id)

    owners = contract.get("owners")
    if not isinstance(owners, list) or not owners:
        _fail("PRIVATE_BOUNDARY_REQUIRED_OWNER_MISSING", candidate_id)
    owner_end_ms = max(
        _int(window.get("end_ms"), label="owner.end_ms")
        for owner in owners
        if isinstance(owner, Mapping) and owner.get("required") is True
        for window in (owner.get("local_windows") or [])
        if isinstance(window, Mapping)
    )
    authority_acceptance = _mapping(authority.get("acceptance"), label="authority.acceptance")
    required_owner_count = _int(authority_acceptance.get("required_owner_count"), label="required_owner_count")
    if required_owner_count != len(owners) or contract.get("required_owner_count") != len(owners):
        _fail("PRIVATE_BOUNDARY_REQUIRED_OWNER_COUNT_MISMATCH", candidate_id)
    if owner_end_ms != _int(authority_acceptance.get("required_owner_end_ms"), label="required_owner_end_ms"):
        _fail("PRIVATE_BOUNDARY_REQUIRED_OWNER_END_MISMATCH", candidate_id)
    if consumer.get("verified_frozen_contract_sha256") != contract.get("contract_sha256"):
        _fail("PRIVATE_BOUNDARY_CONSUMER_BINDING_MISMATCH", "verified_frozen_contract_sha256")
    if consumer.get("verified_owner_set_sha256") != contract.get("owner_set_sha256"):
        _fail("PRIVATE_BOUNDARY_CONSUMER_BINDING_MISMATCH", "verified_owner_set_sha256")

    timeline = _mapping(authority.get("timeline"), label="authority.timeline")
    source_start_ms = _int(timeline.get("source_local_start_ms"), label="source_local_start_ms")
    formal_end_ms = _int(timeline.get("formal_boundary_source_local_ms"), label="formal_boundary_source_local_ms")
    subtitle_end_ms = _int(timeline.get("package_subtitle_end_ms"), label="package_subtitle_end_ms")
    media_end_ms = _int(timeline.get("package_media_end_ms"), label="package_media_end_ms")
    tail_pad_ms = _int(timeline.get("delivery_tail_pad_ms"), label="delivery_tail_pad_ms")
    if formal_end_ms != source_start_ms + subtitle_end_ms:
        _fail("PRIVATE_BOUNDARY_TIMELINE_MAPPING_INVALID", candidate_id)
    if media_end_ms != subtitle_end_ms + tail_pad_ms or tail_pad_ms != 400:
        _fail("PRIVATE_BOUNDARY_TAIL_PAD_INVALID", candidate_id)
    if owner_end_ms > formal_end_ms:
        _fail("PRIVATE_BOUNDARY_OWNER_EXCEEDS_FORMAL_END", candidate_id)
    if any(
        value != formal_end_ms
        for value in (
            contract.get("story_end_ms"),
            contract.get("owner_discovery_end_ms"),
            contract.get("boundary_review_target_ms"),
            _mapping(scope, label="boundary_search_scope").get("review_target_ms"),
        )
    ):
        _fail("PRIVATE_BOUNDARY_FORMAL_END_BINDING_INVALID", candidate_id)
    if consumer.get("verified_timeline") != dict(timeline):
        _fail("PRIVATE_BOUNDARY_CONSUMER_BINDING_MISMATCH", "verified_timeline")

    try:
        subtitle_text = subtitle_payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PrivateBoundaryPackageError("PRIVATE_BOUNDARY_SRT_INVALID", str(exc)) from exc
    srt_verdict = validate_srt_text(subtitle_text, media_duration_ms=media_end_ms)
    cues = parse_srt_cues(subtitle_text)
    if srt_verdict.get("status") != "PASS" or not cues:
        _fail("PRIVATE_BOUNDARY_SRT_INVALID", json.dumps(srt_verdict, ensure_ascii=False))
    if len(cues) != _int(subtitle_entry.get("cue_count"), label="subtitle.cue_count"):
        _fail("PRIVATE_BOUNDARY_SRT_CUE_COUNT_MISMATCH", candidate_id)
    if cues[-1].end_ms != subtitle_end_ms:
        _fail("PRIVATE_BOUNDARY_SRT_END_MISMATCH", candidate_id)

    probe = dict((probe_media or _ffprobe_media)(media_path))
    if _int(probe.get("video_stream_count"), label="video_stream_count") < 1:
        _fail("PRIVATE_BOUNDARY_VIDEO_STREAM_MISSING", candidate_id)
    if _int(probe.get("audio_stream_count"), label="audio_stream_count") < 1:
        _fail("PRIVATE_BOUNDARY_AUDIO_STREAM_MISSING", candidate_id)
    if _int(probe.get("duration_ms"), label="media.duration_ms") != media_end_ms:
        _fail("PRIVATE_BOUNDARY_MEDIA_DURATION_MISMATCH", candidate_id)
    if _int(probe.get("size_bytes"), label="media.size_bytes") != media_binding["bytes"]:
        _fail("PRIVATE_BOUNDARY_MEDIA_SIZE_MISMATCH", candidate_id)
    media_after = _stable_regular_binding(media_path, label="media after probe")
    if media_after != media_binding:
        _fail("PRIVATE_BOUNDARY_MEDIA_DRIFT_DURING_PROBE", candidate_id)

    _verify_zero_mutation_surfaces(result, authority, consumer, manifest)
    result_acceptance = _mapping(result.get("acceptance"), label="result.acceptance")
    if (
        result_acceptance.get("typed_authority") is not True
        or result_acceptance.get("actual_private_package_consumer") is not True
        or result_acceptance.get("large_media_copied") is not False
        or _int(result_acceptance.get("delivery_tail_pad_ms"), label="result.delivery_tail_pad_ms") != 400
    ):
        _fail("PRIVATE_BOUNDARY_RESULT_ACCEPTANCE_INVALID", candidate_id)

    receipt: dict[str, object] = {
        "schema_version": VERIFICATION_SCHEMA,
        "status": "PASS_PRIVATE_BOUNDARY_PACKAGE_VERIFIED",
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "package_root": str(root),
        "source_identity": source_identity,
        "source_identity_sha256": source_identity["source_identity_sha256"],
        "clip_context": context_binding,
        "result": result_binding,
        "authority": {
            **authority_binding,
            "authority_sha256": authority_sha,
            "frozen_contract_sha256": contract["contract_sha256"],
            "owner_set_sha256": contract["owner_set_sha256"],
        },
        "consumer": consumer_binding,
        "manifest": manifest_binding,
        "media": {**media_binding, "probe": probe, "hardlinked_from": hardlinks["media"]},
        "subtitle": {
            **subtitle_binding,
            "cue_count": len(cues),
            "last_end_ms": cues[-1].end_ms,
            "release_verdict": srt_verdict,
            "hardlinked_from": hardlinks["subtitle"],
        },
        "boundary": {
            "source_local_start_ms": source_start_ms,
            "formal_boundary_source_local_ms": formal_end_ms,
            "required_owner_count": required_owner_count,
            "required_owner_end_ms": owner_end_ms,
            "package_subtitle_end_ms": subtitle_end_ms,
            "package_media_end_ms": media_end_ms,
            "delivery_tail_pad_ms": tail_pad_ms,
            "structured_payoff_ms": assessment.get("scope_ms"),
            "excluded_payoff_row_count": len(excluded),
        },
        "delivery_level": {
            "formal_boundary_authority_verified": True,
            "private_boundary_consumer_verified": True,
            "canonical_review_package_integrated": False,
            "canonical_package_audit_passed": False,
            "title_cover_joint_qc_passed": False,
            "production_adopted": False,
            "upload_authorized": False,
            "public": False,
        },
        "actions": {
            "provider_calls": 0,
            "media_mutations": 0,
            "subtitle_mutations": 0,
            "uploads_or_publications": 0,
            "production_or_state_mutations": 0,
        },
    }
    receipt["verification_sha256"] = _canonical_sha256(receipt)
    return receipt


def write_verification_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    """Persist one create-only private receipt outside the verified package."""

    destination = Path(path).absolute()
    parent = _reject_symlink_components(destination.parent, label="receipt parent")
    if not parent.is_dir():
        _fail("PRIVATE_BOUNDARY_RECEIPT_PARENT_INVALID", str(parent))
    payload = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(destination, flags, 0o600)
    except OSError as exc:
        raise PrivateBoundaryPackageError(
            "PRIVATE_BOUNDARY_RECEIPT_CREATE_FAILED", f"{destination}: {exc}"
        ) from exc
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
