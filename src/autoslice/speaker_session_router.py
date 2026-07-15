"""Hash-bound acoustic session routing for channel speaker finalization.

The router never performs speaker inference itself.  It accepts a claim only
from a sealed, explicitly configured acoustic provider and binds the decision
to the current code, provider artifacts, source recording, subtitle inventory,
and analyzed audio coverage.  Anything incomplete or inconsistent falls back
to the ordinary binary finalizer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from scripts.apply_speaker_turn_overrides import atomic_write_text, sha256_file
from src.autoslice.channel_profile import load_channel_profile


REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
REQUEST_SCHEMA_VERSION = f"{PROFILE_ID}-speaker-routing-request.v3"
PROVIDER_SCHEMA_VERSION = f"{PROFILE_ID}-speaker-routing-provider-evidence.v3"
ACOUSTIC_EVIDENCE_SCHEMA_VERSION = (
    f"{PROFILE_ID}-speaker-routing-acoustic-evidence.v1"
)
CLAIM_SCHEMA_VERSION = f"{PROFILE_ID}-speaker-routing-claim.v3"
ROUTER_POLICY_VERSION = "audited-bundle-acoustic-all-candidates-solo-host.v3"
FAST_SOLO = "FAST_SOLO"
RUN_BINARY_FINALIZER = "RUN_BINARY_FINALIZER"
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
PIPELINE_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
PROVIDER_ARTIFACT_ROLES = ("executable", "script", "config", "model", "profile")
PROVIDER_SANITIZED_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONNOUSERSITE": "1",
    "TZ": "UTC",
}
# Repo-controlled production authority.  Intentionally empty until a real
# acoustic provider bundle is implemented, audited, and committed.  Tests may
# inject one fingerprint pair in-process; environment/config cannot populate it.
AUDITED_PROVIDER_BUNDLES: dict[str, str] = {}
RUNTIME_CODE_PATHS = (
    "src/autoslice/speaker_session_router.py",
    "src/autoslice/speaker_common.py",
    "src/autoslice/speaker_context.py",
    "src/autoslice/speaker_evidence.py",
    "src/autoslice/speaker_finalizer.py",
    "scripts/produce_slice_package.py",
    "scripts/free_session_autoslice.py",
)


class SpeakerRoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedSpeakerRoute:
    """Current-state authority for one candidate, including non-FAST outcomes."""

    claim_sha256: str
    candidate_id: str
    pipeline_fingerprint: str
    routing_runtime_fingerprint: str
    request_sha256: str
    provider_evidence_sha256: str
    decision: str
    candidate_result: dict[str, object]
    segment_path: str
    segment_binding_sha256: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class VerifiedFastSoloRoute(VerifiedSpeakerRoute):
    """Opaque authority returned only after a current-state FAST verification."""


def _canonical_json_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def routing_policy() -> dict[str, object]:
    return {
        "version": ROUTER_POLICY_VERSION,
        "provider_coverage_rule": "every_candidate_exactly_once",
        "provider_input_modality": "audio",
        "transcript_or_llm_may_approve_identity": False,
        "fast_verdict": "SOLO_HOST",
        "uncertainty_fallback": RUN_BINARY_FINALIZER,
        "mixed_or_overlap_requires_review": True,
        "provider_bundle_must_be_repo_allowlisted": True,
        "production_fast_provider_available": False,
    }


def routing_policy_fingerprint() -> str:
    return _canonical_json_sha256(routing_policy())


def _require_sha256(value: object, *, field: str) -> str:
    digest = str(value or "")
    if not SHA256_RE.fullmatch(digest):
        raise SpeakerRoutingError(f"{field} must be a SHA-256 digest")
    return digest


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpeakerRoutingError(f"{field} must be an integer")
    return value


def _require_exact_keys(raw: Mapping[str, object], keys: set[str], *, field: str) -> None:
    actual = set(raw)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise SpeakerRoutingError(f"{field} schema mismatch: missing={missing} extra={extra}")


def _stat_tuple(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def segment_stat_signature(path: Path) -> dict[str, int]:
    resolved = path.resolve(strict=True)
    dev, ino, size, mtime_ns, ctime_ns = _stat_tuple(resolved)
    return {
        "dev": dev,
        "ino": ino,
        "size": size,
        "mtime_ns": mtime_ns,
        "ctime_ns": ctime_ns,
    }


def segment_binding_sha256(path: Path) -> str:
    """Hash a sealed recording segment while detecting concurrent writes."""

    resolved = path.resolve(strict=True)
    before = _stat_tuple(resolved)
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    if before != _stat_tuple(resolved):
        raise SpeakerRoutingError("recording segment changed while hashing")
    return digest.hexdigest()


def _artifact_digest(path: Path) -> tuple[str, str]:
    """Return deterministic kind/digest for a regular file or directory tree."""

    absolute = path.absolute()
    if absolute.is_symlink():
        raise SpeakerRoutingError(f"provider artifact path is a symlink: {absolute}")
    resolved = absolute.resolve(strict=True)
    if resolved != absolute:
        raise SpeakerRoutingError(f"provider artifact path traverses a symlink: {absolute}")
    if resolved.is_file():
        return "file", segment_binding_sha256(resolved)
    if not resolved.is_dir():
        raise SpeakerRoutingError(f"provider artifact is not a regular file/tree: {resolved}")
    entries = list(resolved.rglob("*"))
    symlinks = [entry for entry in entries if entry.is_symlink()]
    if symlinks:
        raise SpeakerRoutingError(
            f"provider artifact tree contains a symlink: {symlinks[0]}"
        )
    digest = hashlib.sha256()
    files = sorted(
        (entry for entry in entries if entry.is_file()),
        key=lambda entry: entry.relative_to(resolved).as_posix(),
    )
    if not files:
        raise SpeakerRoutingError(f"provider artifact tree is empty: {resolved}")
    for entry in files:
        relative = entry.relative_to(resolved).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(segment_binding_sha256(entry)))
        digest.update(b"\0")
    return "directory", digest.hexdigest()


def build_provider_authority(
    *,
    name: str,
    algorithm_id: str,
    artifact_paths: Mapping[str, str | Path],
    executed_argv_template: Sequence[str] | None = None,
    sanitized_environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Seal one canonical provider execution closure without trusting it."""

    clean_name = str(name or "").strip()
    clean_algorithm = str(algorithm_id or "").strip()
    if not clean_name or not clean_algorithm:
        raise SpeakerRoutingError("provider name and algorithm_id are required")
    if set(artifact_paths) != set(PROVIDER_ARTIFACT_ROLES):
        raise SpeakerRoutingError("provider artifacts must include executable/script/config/model/profile")
    artifacts: dict[str, dict[str, str]] = {}
    for role in PROVIDER_ARTIFACT_ROLES:
        supplied_path = Path(str(artifact_paths[role])).absolute()
        kind, digest = _artifact_digest(supplied_path)
        path = supplied_path.resolve(strict=True)
        artifacts[role] = {"path": str(path), "kind": kind, "sha256": digest}
    argv = list(
        executed_argv_template
        or (
            artifacts["executable"]["path"],
            artifacts["script"]["path"],
            "{request}",
            "{output}",
        )
    )
    expected_argv = [
        artifacts["executable"]["path"],
        artifacts["script"]["path"],
        "{request}",
        "{output}",
    ]
    if argv != expected_argv:
        raise SpeakerRoutingError(
            "provider argv must be exactly executable, script, {request}, {output}"
        )
    environment = dict(sanitized_environment or PROVIDER_SANITIZED_ENVIRONMENT)
    if environment != PROVIDER_SANITIZED_ENVIRONMENT:
        raise SpeakerRoutingError("provider environment is not the canonical sanitized environment")
    bundle_input = {
        "artifacts": artifacts,
        "executed_argv_template": argv,
        "sanitized_environment": environment,
    }
    bundle_fingerprint = _canonical_json_sha256(bundle_input)
    algorithm_input = {
        "name": clean_name,
        "algorithm_id": clean_algorithm,
        "provider_bundle_fingerprint": bundle_fingerprint,
    }
    return {
        **algorithm_input,
        **bundle_input,
        "algorithm_fingerprint": _canonical_json_sha256(algorithm_input),
    }


def validate_provider_authority(
    authority: object, *, require_audited: bool = True
) -> dict[str, object]:
    if not isinstance(authority, Mapping):
        raise SpeakerRoutingError("provider authority is missing")
    _require_exact_keys(
        authority,
        {
            "name",
            "algorithm_id",
            "algorithm_fingerprint",
            "provider_bundle_fingerprint",
            "artifacts",
            "executed_argv_template",
            "sanitized_environment",
        },
        field="provider authority",
    )
    artifacts = authority.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(PROVIDER_ARTIFACT_ROLES):
        raise SpeakerRoutingError("provider artifact authority is incomplete")
    artifact_paths: dict[str, str] = {}
    for role in PROVIDER_ARTIFACT_ROLES:
        raw = artifacts[role]
        if not isinstance(raw, Mapping):
            raise SpeakerRoutingError(f"provider {role} artifact is invalid")
        _require_exact_keys(raw, {"path", "kind", "sha256"}, field=f"provider {role} artifact")
        artifact_paths[role] = str(raw.get("path") or "")
    current = build_provider_authority(
        name=str(authority.get("name") or ""),
        algorithm_id=str(authority.get("algorithm_id") or ""),
        artifact_paths=artifact_paths,
        executed_argv_template=authority.get("executed_argv_template"),
        sanitized_environment=authority.get("sanitized_environment"),
    )
    if current != dict(authority):
        raise SpeakerRoutingError("provider artifact/config/model/profile bytes drifted")
    if require_audited and AUDITED_PROVIDER_BUNDLES.get(
        str(current["algorithm_fingerprint"])
    ) != str(current["provider_bundle_fingerprint"]):
        raise SpeakerRoutingError("provider bundle/algorithm is not repo-audited")
    return current


def routing_runtime_fingerprint(
    provider_authority: Mapping[str, object], *, repo_root: Path | None = None
) -> str:
    """Fingerprint live router/finalizer/producer/runner and provider bytes."""

    current = validate_provider_authority(provider_authority)
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve(strict=True)
    digest = hashlib.sha256()
    digest.update(f"{PROFILE_ID}-speaker-routing-runtime.v2\0".encode("utf-8"))
    for relative in RUNTIME_CODE_PATHS:
        path = (root / relative).resolve(strict=True)
        if not path.is_file():
            raise SpeakerRoutingError(f"routing runtime file is missing: {relative}")
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(
        json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    return "sha256:" + digest.hexdigest()


def _validate_candidate(raw: object, *, verify_segment_content: bool) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise SpeakerRoutingError("routing candidate must be an object")
    candidate_id = str(raw.get("candidate_id") or "").strip()
    if not candidate_id:
        raise SpeakerRoutingError("routing candidate_id is missing")
    segment_path = Path(str(raw.get("segment_path") or "")).resolve(strict=True)
    segment_binding = _require_sha256(
        raw.get("segment_binding_sha256"), field=f"{candidate_id} segment binding"
    )
    segment_content = _require_sha256(
        raw.get("segment_content_sha256"), field=f"{candidate_id} segment content"
    )
    if segment_binding != segment_content:
        raise SpeakerRoutingError(f"{candidate_id} segment hashes disagree")
    if verify_segment_content and segment_binding_sha256(segment_path) != segment_binding:
        raise SpeakerRoutingError(f"{candidate_id} segment content drifted")
    stat_signature = raw.get("segment_stat_signature")
    if not isinstance(stat_signature, Mapping) or dict(stat_signature) != segment_stat_signature(
        segment_path
    ):
        raise SpeakerRoutingError(f"{candidate_id} segment stat binding drifted")
    bcut_srt_path = Path(str(raw.get("bcut_srt_path") or "")).resolve(strict=True)
    bcut_srt_sha256 = _require_sha256(
        raw.get("bcut_srt_sha256"), field=f"{candidate_id} BCUT SRT"
    )
    if sha256_file(bcut_srt_path) != bcut_srt_sha256:
        raise SpeakerRoutingError(f"{candidate_id} BCUT SRT drifted")
    start_ms = _require_int(raw.get("start_ms"), field=f"{candidate_id} start_ms")
    end_ms = _require_int(raw.get("end_ms"), field=f"{candidate_id} end_ms")
    if start_ms < 0 or end_ms <= start_ms:
        raise SpeakerRoutingError(f"{candidate_id} interval is invalid")
    return {
        "candidate_id": candidate_id,
        "segment_path": str(segment_path),
        "segment_binding_sha256": segment_binding,
        "segment_content_sha256": segment_content,
        "segment_stat_signature": dict(stat_signature),
        "bcut_srt_path": str(bcut_srt_path),
        "bcut_srt_sha256": bcut_srt_sha256,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def validate_routing_request_document(
    document: object, *, verify_segment_content: bool = True
) -> list[dict[str, object]]:
    if not isinstance(document, Mapping):
        raise SpeakerRoutingError("routing request must be an object")
    if document.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise SpeakerRoutingError("routing request schema is invalid")
    if not DATE_RE.fullmatch(str(document.get("date") or "")):
        raise SpeakerRoutingError("routing request date must be strict YYYY-MM-DD")
    if not PIPELINE_FINGERPRINT_RE.fullmatch(str(document.get("pipeline_fingerprint") or "")):
        raise SpeakerRoutingError("routing request pipeline fingerprint is invalid")
    if document.get("router_policy_version") != ROUTER_POLICY_VERSION:
        raise SpeakerRoutingError("routing request policy version is stale")
    if document.get("router_policy_fingerprint") != routing_policy_fingerprint():
        raise SpeakerRoutingError("routing request policy fingerprint mismatch")
    authority = validate_provider_authority(document.get("provider_authority"))
    current_runtime = routing_runtime_fingerprint(authority)
    if document.get("routing_runtime_fingerprint") != current_runtime:
        raise SpeakerRoutingError("routing request runtime/provider fingerprint is stale")
    raw_candidates = document.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SpeakerRoutingError("routing request candidates are missing")
    candidates = [
        _validate_candidate(raw, verify_segment_content=verify_segment_content)
        for raw in raw_candidates
    ]
    ids = [str(row["candidate_id"]) for row in candidates]
    if len(set(ids)) != len(ids):
        raise SpeakerRoutingError("routing request candidate_ids are not unique")
    return candidates


def _validate_acoustic_evidence(
    raw: object, *, candidate: Mapping[str, object], candidate_id: str
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise SpeakerRoutingError("routing provider acoustic evidence is missing")
    keys = {
        "schema_version",
        "source_media_sha256",
        "window_start_ms",
        "window_end_ms",
        "audio_window_sha256",
        "analyzed_speech_ms",
        "coverage_unit_count",
        "covered_unit_count",
        "unresolved_unit_count",
        "speaker_count_lower_bound",
        "overlap_detected",
        "mixed_speaker_within_unit_detected",
        "acoustic_observation_sha256",
    }
    _require_exact_keys(raw, keys, field=f"{candidate_id} acoustic evidence")
    if raw.get("schema_version") != ACOUSTIC_EVIDENCE_SCHEMA_VERSION:
        raise SpeakerRoutingError("routing provider acoustic evidence schema is invalid")
    if raw.get("source_media_sha256") != candidate["segment_content_sha256"]:
        raise SpeakerRoutingError("routing provider acoustic source hash mismatch")
    if (
        _require_int(raw.get("window_start_ms"), field="acoustic window_start_ms")
        != candidate["start_ms"]
        or _require_int(raw.get("window_end_ms"), field="acoustic window_end_ms")
        != candidate["end_ms"]
    ):
        raise SpeakerRoutingError("routing provider acoustic window mismatch")
    _require_sha256(raw.get("audio_window_sha256"), field="acoustic window")
    _require_sha256(raw.get("acoustic_observation_sha256"), field="acoustic observation")
    analyzed = _require_int(raw.get("analyzed_speech_ms"), field="analyzed_speech_ms")
    unit_count = _require_int(raw.get("coverage_unit_count"), field="coverage_unit_count")
    covered = _require_int(raw.get("covered_unit_count"), field="covered_unit_count")
    unresolved = _require_int(raw.get("unresolved_unit_count"), field="unresolved_unit_count")
    speakers = _require_int(
        raw.get("speaker_count_lower_bound"), field="speaker_count_lower_bound"
    )
    if analyzed <= 0 or analyzed > int(candidate["end_ms"]) - int(candidate["start_ms"]):
        raise SpeakerRoutingError("routing provider analyzed speech duration is invalid")
    if unit_count <= 0 or covered < 0 or covered > unit_count or unresolved < 0:
        raise SpeakerRoutingError("routing provider acoustic coverage counts are invalid")
    if covered + unresolved != unit_count or speakers < 1:
        raise SpeakerRoutingError("routing provider acoustic coverage is self-inconsistent")
    for key in ("overlap_detected", "mixed_speaker_within_unit_detected"):
        if not isinstance(raw.get(key), bool):
            raise SpeakerRoutingError(f"routing provider {key} must be boolean")
    return dict(raw)


def validate_provider_evidence_document(
    document: object,
    *,
    request: Mapping[str, object],
    request_sha256: str,
    candidates: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    if not isinstance(document, Mapping):
        raise SpeakerRoutingError("routing provider evidence must be an object")
    if document.get("schema_version") != PROVIDER_SCHEMA_VERSION:
        raise SpeakerRoutingError("routing provider evidence schema is invalid")
    if document.get("status") != "READY":
        raise SpeakerRoutingError("routing provider evidence is not READY")
    if document.get("request_sha256") != request_sha256:
        raise SpeakerRoutingError("routing provider request hash mismatch")
    if document.get("pipeline_fingerprint") != request.get("pipeline_fingerprint"):
        raise SpeakerRoutingError("routing provider pipeline fingerprint mismatch")
    if document.get("routing_runtime_fingerprint") != request.get(
        "routing_runtime_fingerprint"
    ):
        raise SpeakerRoutingError("routing provider runtime fingerprint mismatch")
    if document.get("input_modality") != "audio":
        raise SpeakerRoutingError("routing provider is not acoustic-only")
    if document.get("transcript_or_llm_used") is not False:
        raise SpeakerRoutingError("routing provider used transcript/LLM identity evidence")
    provider = validate_provider_authority(document.get("provider"))
    if provider != dict(request.get("provider_authority") or {}):
        raise SpeakerRoutingError("routing provider authority is not the requested provider")
    results_raw = document.get("candidate_results")
    if not isinstance(results_raw, list):
        raise SpeakerRoutingError("routing provider candidate_results must be a list")
    expected = {str(row["candidate_id"]): row for row in candidates}
    if len(results_raw) != len(expected):
        raise SpeakerRoutingError("routing provider coverage is incomplete")
    results: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in results_raw:
        if not isinstance(raw, Mapping):
            raise SpeakerRoutingError("routing provider candidate result is invalid")
        keys = {
            "candidate_id",
            "segment_binding_sha256",
            "bcut_srt_sha256",
            "verdict",
            "complete_coverage",
            "unresolved_cue_count",
            "mixed_or_overlap_detected",
            "evidence",
            "evidence_sha256",
        }
        _require_exact_keys(raw, keys, field="provider candidate result")
        candidate_id = str(raw.get("candidate_id") or "")
        candidate = expected.get(candidate_id)
        if candidate is None or candidate_id in seen:
            raise SpeakerRoutingError("routing provider candidate coverage is invalid")
        verdict = raw.get("verdict")
        if verdict not in {"SOLO_HOST", "MULTI_SPEAKER", "UNCERTAIN"}:
            raise SpeakerRoutingError("routing provider verdict is invalid")
        if raw.get("segment_binding_sha256") != candidate["segment_binding_sha256"]:
            raise SpeakerRoutingError("routing provider segment binding mismatch")
        if raw.get("bcut_srt_sha256") != candidate["bcut_srt_sha256"]:
            raise SpeakerRoutingError("routing provider subtitle binding mismatch")
        if not isinstance(raw.get("complete_coverage"), bool):
            raise SpeakerRoutingError("routing provider complete_coverage must be boolean")
        unresolved_cues = _require_int(
            raw.get("unresolved_cue_count"), field=f"{candidate_id} unresolved_cue_count"
        )
        if unresolved_cues < 0 or not isinstance(raw.get("mixed_or_overlap_detected"), bool):
            raise SpeakerRoutingError("routing provider result disposition is invalid")
        evidence = _validate_acoustic_evidence(
            raw.get("evidence"), candidate=candidate, candidate_id=candidate_id
        )
        evidence_sha256 = _require_sha256(
            raw.get("evidence_sha256"), field=f"{candidate_id} provider evidence"
        )
        if evidence_sha256 != _canonical_json_sha256(evidence):
            raise SpeakerRoutingError("routing provider acoustic evidence digest mismatch")
        complete = raw.get("complete_coverage") is True
        mixed = bool(
            evidence["overlap_detected"]
            or evidence["mixed_speaker_within_unit_detected"]
        )
        if bool(raw.get("mixed_or_overlap_detected")) != mixed:
            raise SpeakerRoutingError("routing provider mixed/overlap disposition disagrees")
        acoustic_unresolved = int(evidence["unresolved_unit_count"])
        if complete != (acoustic_unresolved == 0 and int(evidence["covered_unit_count"]) == int(evidence["coverage_unit_count"])):
            raise SpeakerRoutingError("routing provider completeness disagrees with acoustic coverage")
        if verdict == "SOLO_HOST" and (
            not complete
            or unresolved_cues
            or mixed
            or int(evidence["speaker_count_lower_bound"]) != 1
        ):
            raise SpeakerRoutingError("SOLO_HOST evidence is self-inconsistent")
        if verdict == "MULTI_SPEAKER" and int(evidence["speaker_count_lower_bound"]) < 2:
            raise SpeakerRoutingError("MULTI_SPEAKER evidence has fewer than two speakers")
        if verdict == "UNCERTAIN" and complete and not unresolved_cues and not mixed:
            # An explicit uncertain result must expose what was unresolved.
            raise SpeakerRoutingError("UNCERTAIN evidence has no unresolved disposition")
        results.append({key: (dict(evidence) if key == "evidence" else raw[key]) for key in keys})
        seen.add(candidate_id)
    if seen != set(expected):
        raise SpeakerRoutingError("routing provider coverage is incomplete")
    return results


def _claim_integrity_sha256(claim: Mapping[str, object]) -> str:
    return _canonical_json_sha256(
        {key: value for key, value in claim.items() if key != "claim_integrity_sha256"}
    )


def _fallback_claim(
    *,
    request_path: Path,
    request: Mapping[str, object],
    request_sha256: str | None,
    candidates: Sequence[Mapping[str, object]],
    reason_codes: Sequence[str],
) -> dict[str, object]:
    claim: dict[str, object] = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "status": "READY",
        "decision": RUN_BINARY_FINALIZER,
        "fast_solo_authorized": False,
        "date": str(request.get("date") or ""),
        "pipeline_fingerprint": request.get("pipeline_fingerprint"),
        "routing_runtime_fingerprint": request.get("routing_runtime_fingerprint"),
        "router_policy_version": ROUTER_POLICY_VERSION,
        "router_policy_fingerprint": routing_policy_fingerprint(),
        "policy": routing_policy(),
        "request_path": str(request_path),
        "request_sha256": request_sha256,
        "provider_evidence_path": None,
        "provider_evidence_sha256": None,
        "provider": None,
        "candidates": [dict(row) for row in candidates],
        "candidate_results": [],
        "reason_codes": list(dict.fromkeys(reason_codes)),
    }
    claim["claim_integrity_sha256"] = _claim_integrity_sha256(claim)
    return claim


def generate_speaker_routing(
    *, request_path: Path, output_path: Path, provider_evidence_path: Path | None = None
) -> dict[str, object]:
    request_path = request_path.resolve(strict=True)
    request_sha256 = sha256_file(request_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, Mapping):
        raise SpeakerRoutingError("routing request must be an object")
    candidates = validate_routing_request_document(request)
    if provider_evidence_path is None:
        claim = _fallback_claim(
            request_path=request_path,
            request=request,
            request_sha256=request_sha256,
            candidates=candidates,
            reason_codes=["ROUTER_PROVIDER_UNAVAILABLE"],
        )
    else:
        try:
            provider_evidence_path = provider_evidence_path.resolve(strict=True)
            provider_sha256 = sha256_file(provider_evidence_path)
            provider_document = json.loads(provider_evidence_path.read_text(encoding="utf-8"))
            results = validate_provider_evidence_document(
                provider_document,
                request=request,
                request_sha256=request_sha256,
                candidates=candidates,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, SpeakerRoutingError) as exc:
            claim = _fallback_claim(
                request_path=request_path,
                request=request,
                request_sha256=request_sha256,
                candidates=candidates,
                reason_codes=[f"ROUTER_PROVIDER_INVALID:{type(exc).__name__}"],
            )
            claim["error"] = str(exc)
            claim["claim_integrity_sha256"] = _claim_integrity_sha256(claim)
        else:
            blocking = [
                f"PROVIDER_{row['verdict']}:{row['candidate_id']}"
                for row in results
                if row["verdict"] != "SOLO_HOST"
            ]
            decision = FAST_SOLO if not blocking else RUN_BINARY_FINALIZER
            claim = {
                "schema_version": CLAIM_SCHEMA_VERSION,
                "status": "READY",
                "decision": decision,
                "fast_solo_authorized": decision == FAST_SOLO,
                "date": str(request.get("date") or ""),
                "pipeline_fingerprint": request["pipeline_fingerprint"],
                "routing_runtime_fingerprint": request["routing_runtime_fingerprint"],
                "router_policy_version": ROUTER_POLICY_VERSION,
                "router_policy_fingerprint": routing_policy_fingerprint(),
                "policy": routing_policy(),
                "request_path": str(request_path),
                "request_sha256": request_sha256,
                "provider_evidence_path": str(provider_evidence_path),
                "provider_evidence_sha256": provider_sha256,
                "provider": provider_document["provider"],
                "candidates": candidates,
                "candidate_results": results,
                "reason_codes": blocking,
            }
            claim["claim_integrity_sha256"] = _claim_integrity_sha256(claim)
    atomic_write_text(
        output_path,
        json.dumps(claim, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
    return claim


def verify_speaker_routing_claim_for_candidate(
    claim: Mapping[str, object],
    *,
    claim_path: Path,
    expected_claim_sha256: str,
    expected_date: str,
    expected_candidate_id: str,
    expected_segment_binding_sha256: str,
    expected_start_ms: int,
    expected_end_ms: int,
    expected_pipeline_fingerprint: str,
    expected_segment_path: Path,
    expected_segment_stat_signature: Mapping[str, int],
    expected_bcut_srt_path: Path,
    expected_bcut_srt_sha256: str,
) -> VerifiedSpeakerRoute:
    """Verify any provider-backed decision against current bytes and runtime."""

    claim_path = claim_path.resolve(strict=True)
    actual_claim_sha256 = sha256_file(claim_path)
    if actual_claim_sha256 != _require_sha256(expected_claim_sha256, field="expected routing claim"):
        raise SpeakerRoutingError("speaker routing claim hash mismatch")
    loaded_claim = json.loads(claim_path.read_text(encoding="utf-8"))
    if loaded_claim != dict(claim):
        raise SpeakerRoutingError("speaker routing claim object differs from bound bytes")
    if claim.get("schema_version") != CLAIM_SCHEMA_VERSION or claim.get("status") != "READY":
        raise SpeakerRoutingError("speaker routing claim schema/status is invalid")
    if claim.get("claim_integrity_sha256") != _claim_integrity_sha256(claim):
        raise SpeakerRoutingError("speaker routing claim integrity mismatch")
    if claim.get("date") != expected_date:
        raise SpeakerRoutingError("speaker routing date mismatch")
    if (
        claim.get("pipeline_fingerprint") != expected_pipeline_fingerprint
        or claim.get("router_policy_version") != ROUTER_POLICY_VERSION
        or claim.get("router_policy_fingerprint") != routing_policy_fingerprint()
        or claim.get("policy") != routing_policy()
    ):
        raise SpeakerRoutingError("speaker routing policy/pipeline authority mismatch")
    request_path = Path(str(claim.get("request_path") or "")).resolve(strict=True)
    request_sha256 = _require_sha256(claim.get("request_sha256"), field="routing request")
    if sha256_file(request_path) != request_sha256:
        raise SpeakerRoutingError("speaker routing request bytes drifted")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    candidates = validate_routing_request_document(request, verify_segment_content=False)
    current_runtime = routing_runtime_fingerprint(request["provider_authority"])
    if (
        request.get("pipeline_fingerprint") != expected_pipeline_fingerprint
        or request.get("routing_runtime_fingerprint") != current_runtime
        or claim.get("routing_runtime_fingerprint") != current_runtime
    ):
        raise SpeakerRoutingError("speaker routing runtime/provider authority is stale")
    if claim.get("candidates") != candidates:
        raise SpeakerRoutingError("speaker routing candidate inventory mismatch")
    provider_path = Path(str(claim.get("provider_evidence_path") or "")).resolve(strict=True)
    provider_sha256 = _require_sha256(
        claim.get("provider_evidence_sha256"), field="routing provider evidence"
    )
    if sha256_file(provider_path) != provider_sha256:
        raise SpeakerRoutingError("speaker routing provider evidence drifted")
    provider_document = json.loads(provider_path.read_text(encoding="utf-8"))
    results = validate_provider_evidence_document(
        provider_document,
        request=request,
        request_sha256=request_sha256,
        candidates=candidates,
    )
    if claim.get("provider") != provider_document.get("provider"):
        raise SpeakerRoutingError("speaker routing provider authority mismatch")
    if claim.get("candidate_results") != results:
        raise SpeakerRoutingError("speaker routing provider results drifted")
    match = next((row for row in candidates if row["candidate_id"] == expected_candidate_id), None)
    result = next((row for row in results if row["candidate_id"] == expected_candidate_id), None)
    if match is None or result is None:
        raise SpeakerRoutingError("candidate is not covered by speaker routing claim")
    segment_path = expected_segment_path.resolve(strict=True)
    bcut_path = expected_bcut_srt_path.resolve(strict=True)
    if (
        match["segment_binding_sha256"] != expected_segment_binding_sha256
        or match["start_ms"] != expected_start_ms
        or match["end_ms"] != expected_end_ms
        or match["segment_path"] != str(segment_path)
        or match["segment_stat_signature"] != dict(expected_segment_stat_signature)
        or match["segment_stat_signature"] != segment_stat_signature(segment_path)
        or segment_binding_sha256(segment_path) != expected_segment_binding_sha256
        or match["bcut_srt_path"] != str(bcut_path)
        or match["bcut_srt_sha256"] != expected_bcut_srt_sha256
        or sha256_file(bcut_path) != expected_bcut_srt_sha256
    ):
        raise SpeakerRoutingError("speaker routing candidate binding mismatch")
    return VerifiedSpeakerRoute(
        claim_sha256=actual_claim_sha256,
        candidate_id=expected_candidate_id,
        pipeline_fingerprint=expected_pipeline_fingerprint,
        routing_runtime_fingerprint=current_runtime,
        request_sha256=request_sha256,
        provider_evidence_sha256=provider_sha256,
        decision=str(claim.get("decision") or ""),
        candidate_result=dict(result),
        segment_path=str(segment_path),
        segment_binding_sha256=expected_segment_binding_sha256,
        start_ms=expected_start_ms,
        end_ms=expected_end_ms,
    )


def verify_speaker_routing_claim(
    claim: Mapping[str, object], **kwargs: object
) -> VerifiedFastSoloRoute:
    verified = verify_speaker_routing_claim_for_candidate(claim, **kwargs)
    if (
        claim.get("decision") != FAST_SOLO
        or claim.get("fast_solo_authorized") is not True
        or claim.get("reason_codes") != []
        or verified.candidate_result.get("verdict") != "SOLO_HOST"
    ):
        raise SpeakerRoutingError("speaker routing claim does not authorize FAST_SOLO")
    return VerifiedFastSoloRoute(**verified.__dict__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--provider-evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        claim = generate_speaker_routing(
            request_path=args.request,
            output_path=args.output,
            provider_evidence_path=args.provider_evidence,
        )
    except Exception as exc:
        claim = {
            "schema_version": CLAIM_SCHEMA_VERSION,
            "status": "READY",
            "decision": RUN_BINARY_FINALIZER,
            "fast_solo_authorized": False,
            "reason_codes": [f"ROUTER_ERROR:{type(exc).__name__}"],
            "error": str(exc),
        }
        claim["claim_integrity_sha256"] = _claim_integrity_sha256(claim)
        atomic_write_text(
            args.output,
            json.dumps(claim, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
    print(json.dumps({"decision": claim["decision"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
