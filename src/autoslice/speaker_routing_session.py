"""Speaker-routing session authority + collab-evidence queueing.

Extracted verbatim from scripts/free_session_autoslice.py (2026-07-14 屎山
治理第一刀，~880 行内聚块).  Behaviour is unchanged: the runner keeps thin
wrappers with the original names that build a :class:`RunnerContext` from its
module globals **at call time**, so existing tests that monkeypatch
``scripts.free_session_autoslice.BASE`` (etc.) keep working.

The context object carries every runner global the cluster used to reach
implicitly; nothing here imports the runner back (no cycles).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.autoslice.collab_evidence_capture import (
    CollabEvidenceCaptureError,
    WORKER_REQUEST_SCHEMA_VERSION,
    evaluate_trigger as evaluate_collab_capture_trigger,
    validate_worker_request_document,
)
from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.speaker_session_router import (
    FAST_SOLO,
    PROVIDER_SANITIZED_ENVIRONMENT,
    REQUEST_SCHEMA_VERSION as SPEAKER_ROUTING_REQUEST_SCHEMA,
    ROUTER_POLICY_VERSION as SPEAKER_ROUTING_POLICY_VERSION,
    RUN_BINARY_FINALIZER,
    SpeakerRoutingError,
    build_provider_authority,
    generate_speaker_routing,
    routing_runtime_fingerprint,
    routing_policy_fingerprint,
    segment_binding_sha256,
    segment_stat_signature,
    validate_provider_authority,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHANNEL_PROFILE = load_channel_profile(REPO_ROOT)
PROFILE_ID = CHANNEL_PROFILE.profile_id
SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA = (
    f"{PROFILE_ID}-speaker-routing-session-authority.v1"
)
SPEAKER_ROUTING_SESSION_SCHEMA = f"{PROFILE_ID}-speaker-routing-session.v2"
SPEAKER_ROUTING_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


@dataclass(frozen=True)
class RunnerContext:
    """Late-bound runner globals, assembled by the runner per call."""

    base: Path
    repo_root: Path
    room: str
    piece_pre_ms: int
    boundary_repair_retry_cap_ms: int
    final_tail_guard_ms: int
    date_rx: re.Pattern[str]
    log: Callable[[str], None]
    pipeline_fingerprint: Callable[[], str]
    child_env: Callable[[], dict[str, str]]
    atomic_write_json: Callable[[Path, dict], None]


def _clear_speaker_routing_fields(items: list[dict]) -> None:
    for item in items:
        for key in (
            "speaker_routing_claim",
            "speaker_routing_claim_sha256",
            "speaker_routing_candidate",
        ):
            item.pop(key, None)


def _speaker_routing_provider_command(
    *, request_path: Path, output_path: Path
) -> list[str] | None:
    """Parse a shell-free provider argv template from the environment."""

    raw = os.environ.get("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", "").strip()
    if not raw:
        return None
    try:
        template = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"speaker routing provider command JSON is invalid: {exc}") from exc
    if (
        not isinstance(template, list)
        or len(template) != 4
        or any(not isinstance(value, str) or not value for value in template)
        or template[2:] != ["{request}", "{output}"]
    ):
        raise ValueError(
            "speaker routing provider argv must be exactly executable, script, "
            "{request}, {output}; inline code and extra/path args are forbidden"
        )
    executable = Path(template[0]).absolute()
    script = Path(template[1]).absolute()
    for label, path in (("executable", executable), ("script", script)):
        if path.is_symlink() or path.resolve(strict=True) != path or not path.is_file():
            raise ValueError(f"speaker routing provider {label} must be a regular non-symlink path")
    return [str(executable), str(script), str(request_path), str(output_path)]


def _speaker_routing_provider_authority(
    command: list[str] | None = None,
    *,
    require_audited: bool = True,
) -> dict[str, object] | None:
    """Resolve and hash the exact provider executable and inference assets."""

    raw_command = os.environ.get(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_COMMAND_JSON", ""
    ).strip()
    if not raw_command:
        return None
    if command is None:
        try:
            template = json.loads(raw_command)
        except json.JSONDecodeError as exc:
            raise ValueError(f"speaker routing provider command JSON is invalid: {exc}") from exc
        if (
            not isinstance(template, list)
            or len(template) != 4
            or template[2:] != ["{request}", "{output}"]
            or any(not isinstance(value, str) or not value for value in template)
        ):
            raise ValueError("speaker routing provider command is not canonical")
        executable = str(Path(template[0]).absolute())
        executed_script = str(Path(template[1]).absolute())
    else:
        executable = command[0]
        if len(command) != 4:
            raise ValueError("speaker routing provider command is not canonical")
        executed_script = command[1]
    raw_artifacts = os.environ.get(
        "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ARTIFACTS_JSON", ""
    ).strip()
    try:
        artifacts = json.loads(raw_artifacts)
    except json.JSONDecodeError as exc:
        raise ValueError(f"speaker routing provider artifacts JSON is invalid: {exc}") from exc
    if not isinstance(artifacts, dict):
        raise ValueError("speaker routing provider artifacts must be a JSON object")
    artifacts = {str(key): str(value) for key, value in artifacts.items()}
    configured_executable = artifacts.get("executable")
    executable_path = Path(executable).absolute()
    configured_executable_path = Path(str(configured_executable)).absolute()
    if (
        executable_path.is_symlink()
        or executable_path.resolve(strict=True) != executable_path
        or configured_executable_path.is_symlink()
        or configured_executable_path.resolve(strict=True) != configured_executable_path
    ):
        raise ValueError("speaker routing executable authority contains a symlink")
    resolved_executable = str(executable_path)
    if configured_executable and str(configured_executable_path) != resolved_executable:
        raise ValueError("speaker routing command executable disagrees with artifact authority")
    artifacts["executable"] = resolved_executable
    configured_script = artifacts.get("script")
    executed_script_path = Path(executed_script).absolute()
    configured_script_path = Path(str(configured_script)).absolute()
    if (
        executed_script_path.is_symlink()
        or executed_script_path.resolve(strict=True) != executed_script_path
        or configured_script_path.is_symlink()
        or configured_script_path.resolve(strict=True) != configured_script_path
    ):
        raise ValueError("speaker routing script authority contains a symlink")
    resolved_script = str(executed_script_path)
    if not configured_script or str(configured_script_path) != resolved_script:
        raise ValueError("speaker routing executed script disagrees with artifact authority")
    artifacts["script"] = resolved_script
    authority = build_provider_authority(
        name=os.environ.get("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_NAME", ""),
        algorithm_id=os.environ.get(
            "AUTOSLICE_SPEAKER_ROUTING_PROVIDER_ALGORITHM_ID", ""
        ),
        artifact_paths=artifacts,
        executed_argv_template=[
            resolved_executable,
            resolved_script,
            "{request}",
            "{output}",
        ],
        sanitized_environment=PROVIDER_SANITIZED_ENVIRONMENT,
    )
    return validate_provider_authority(
        authority, require_audited=require_audited
    )


def _sealed_speaker_inventory(items: list[dict]) -> list[dict]:
    keys = (
        "cid",
        "segment_path",
        "bcut_srt_path",
        "seg_dur_ms",
        "start_ms",
        "end_ms",
    )
    return [
        {key: item[key] for key in keys if key in item}
        for item in items
    ]


def _attach_speaker_routing_claim(
    *,
    items: list[dict],
    claim_path: Path,
    claim_sha256: str,
    candidates: list[dict[str, object]],
    pipeline: str,
) -> None:
    by_candidate = {str(candidate["candidate_id"]): candidate for candidate in candidates}
    for item in items:
        candidate = by_candidate.get(str(item.get("cid") or ""))
        if candidate is None:
            # A late/unmatched candidate was never classified with the sealed
            # session.  Leaving it unbound forces the binary finalizer.
            continue
        item["speaker_routing_claim"] = str(claim_path)
        item["speaker_routing_claim_sha256"] = claim_sha256
        item["speaker_routing_candidate"] = {
            **candidate,
            "pipeline_fingerprint": pipeline,
        }


def _speaker_inventory_sha256(inventory: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            inventory,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _speaker_session_authority_integrity(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in document.items()
                if key != "authority_integrity_sha256"
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _speaker_date_root(date: str, *, ctx: RunnerContext) -> Path:
    if not SPEAKER_ROUTING_DATE_RE.fullmatch(str(date)):
        raise ValueError("speaker routing date must be strict YYYY-MM-DD")
    root = (ctx.base / "state" / "speaker-routing").resolve()
    date_root = (root / date).resolve()
    try:
        date_root.relative_to(root)
    except ValueError as exc:
        raise ValueError("speaker routing date path escapes its root") from exc
    return date_root


def _speaker_generation_root(date: str, pipeline: str, *, ctx: RunnerContext) -> Path:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", pipeline):
        raise ValueError("speaker routing generation pipeline fingerprint is invalid")
    date_root = _speaker_date_root(date, ctx=ctx)
    generation_root = (
        date_root / "generations" / pipeline.removeprefix("sha256:")
    ).resolve()
    try:
        generation_root.relative_to(date_root)
    except ValueError as exc:
        raise ValueError("speaker routing generation path escapes its date root") from exc
    return generation_root


def _write_speaker_session_authority(path: Path, document: dict, *, ctx: RunnerContext) -> str:
    expected_root = _speaker_date_root(str(document.get("date") or ""), ctx=ctx)
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(expected_root)
    except ValueError as exc:
        raise ValueError("speaker routing authority write escapes its date root") from exc
    payload = dict(document)
    payload["authority_integrity_sha256"] = _speaker_session_authority_integrity(
        payload
    )
    ctx.atomic_write_json(resolved_path, payload)
    return hashlib.sha256(resolved_path.read_bytes()).hexdigest()


def _load_speaker_session_authority(
    path: Path, *, expected_sha256: str | None = None
) -> dict:
    absolute = path.absolute()
    if absolute.is_symlink() or absolute.resolve(strict=True) != absolute:
        raise ValueError("speaker routing session authority path is not canonical")
    actual_sha = hashlib.sha256(absolute.read_bytes()).hexdigest()
    if expected_sha256 is not None and actual_sha != expected_sha256:
        raise ValueError("speaker routing session authority file hash mismatch")
    document = json.loads(absolute.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("speaker routing session authority must be an object")
    if document.get("schema_version") != SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA:
        raise ValueError("speaker routing session authority schema is invalid")
    if document.get("authority_integrity_sha256") != _speaker_session_authority_integrity(
        document
    ):
        raise ValueError("speaker routing session authority integrity mismatch")
    inventory = document.get("sealed_inventory")
    if not isinstance(inventory, list) or document.get(
        "sealed_inventory_sha256"
    ) != _speaker_inventory_sha256(inventory):
        raise ValueError("speaker routing session authority inventory is invalid")
    return document


def _speaker_session_state_from_authority(
    authority_path: Path, authority_sha256: str, authority: dict
) -> dict:
    return {
        "schema_version": SPEAKER_ROUTING_SESSION_SCHEMA,
        "date": authority["date"],
        "generation_pipeline_fingerprint": authority[
            "generation_pipeline_fingerprint"
        ],
        "sealed_inventory": authority["sealed_inventory"],
        "sealed_inventory_sha256": authority["sealed_inventory_sha256"],
        "decision": authority.get("decision"),
        "claim_path": authority.get("claim_path"),
        "claim_sha256": authority.get("claim_sha256"),
        "pipeline_fingerprint": authority.get("claim_pipeline_fingerprint"),
        "provider_classification_sealed": authority.get(
            "provider_classification_sealed"
        )
        is True,
        "authority_path": str(authority_path),
        "authority_sha256": authority_sha256,
    }


def _speaker_session_state_matches_authority(state_value: dict, authority: dict) -> bool:
    expected = _speaker_session_state_from_authority(
        Path(str(state_value.get("authority_path") or "")),
        str(state_value.get("authority_sha256") or ""),
        authority,
    )
    return state_value == expected


def _build_speaker_routing_request(
    *,
    date: str,
    inventory_items: list[dict],
    routing_root: Path,
    request_path: Path,
    provider_authority: dict[str, object],
    ctx: RunnerContext,
) -> tuple[dict, list[dict[str, object]]]:
    """Seal candidate media/SRT bindings and persist one provider request."""

    routing_root.mkdir(parents=True, exist_ok=True)
    segment_hashes: dict[Path, str] = {}
    candidates: list[dict[str, object]] = []
    for item in inventory_items:
        segment = Path(str(item["segment_path"])).resolve(strict=True)
        if segment not in segment_hashes:
            segment_hashes[segment] = segment_binding_sha256(segment)
        bcut_value = item.get("bcut_srt_path")
        bcut_srt = (
            Path(str(bcut_value)).resolve(strict=True)
            if bcut_value
            else (ctx.base / "cache" / date / f"{segment.stem}.bcut.srt").resolve(
                strict=True
            )
        )
        bcut_sha256 = hashlib.sha256(bcut_srt.read_bytes()).hexdigest()
        coverage_start = max(0, int(item["start_ms"]) - ctx.piece_pre_ms)
        coverage_end = (
            int(item["end_ms"])
            + ctx.boundary_repair_retry_cap_ms
            + ctx.final_tail_guard_ms
        )
        segment_duration = int(item.get("seg_dur_ms") or 0)
        if segment_duration > 0:
            coverage_end = min(segment_duration, coverage_end)
        if coverage_end <= coverage_start:
            raise ValueError("speaker routing candidate coverage is empty")
        candidates.append(
            {
                "candidate_id": str(item["cid"]),
                "segment_path": str(segment),
                "segment_binding_sha256": segment_hashes[segment],
                "segment_content_sha256": segment_hashes[segment],
                "segment_stat_signature": segment_stat_signature(segment),
                "bcut_srt_path": str(bcut_srt),
                "bcut_srt_sha256": bcut_sha256,
                "start_ms": coverage_start,
                "end_ms": coverage_end,
            }
        )
    request = {
        "schema_version": SPEAKER_ROUTING_REQUEST_SCHEMA,
        "date": date,
        "room": ctx.room,
        "pipeline_fingerprint": ctx.pipeline_fingerprint(),
        "routing_runtime_fingerprint": routing_runtime_fingerprint(
            provider_authority, repo_root=ctx.repo_root
        ),
        "router_policy_version": SPEAKER_ROUTING_POLICY_VERSION,
        "router_policy_fingerprint": routing_policy_fingerprint(),
        "provider_authority": provider_authority,
        "candidates": candidates,
    }
    ctx.atomic_write_json(request_path, request)
    return request, candidates


def _finalize_speaker_routing_claim(
    *,
    request_path: Path,
    claim_path: Path,
    provider_evidence: Path | None,
    items: list[dict],
    candidates: list[dict[str, object]],
    request: dict,
    routing_session: dict | None,
    session_authority: dict | None,
    session_authority_path: Path | None,
    state: dict | None,
    ctx: RunnerContext,
) -> dict:
    """Generate, attach and durably advance the external session authority."""

    claim = generate_speaker_routing(
        request_path=request_path,
        output_path=claim_path,
        provider_evidence_path=provider_evidence,
    )
    claim_sha256 = hashlib.sha256(claim_path.read_bytes()).hexdigest()
    _attach_speaker_routing_claim(
        items=items,
        claim_path=claim_path,
        claim_sha256=claim_sha256,
        candidates=candidates,
        pipeline=str(request["pipeline_fingerprint"]),
    )
    if (
        routing_session is not None
        and session_authority is not None
        and session_authority_path is not None
    ):
        session_authority.update(
            {
                "decision": claim["decision"],
                "claim_path": str(claim_path),
                "claim_sha256": claim_sha256,
                "claim_pipeline_fingerprint": request["pipeline_fingerprint"],
                # A complete provider classification that found any
                # collab/uncertainty is sticky in the external authority.
                "provider_classification_sealed": (
                    claim["decision"] == RUN_BINARY_FINALIZER
                    and len(claim.get("candidate_results") or []) == len(candidates)
                ),
            }
        )
        authority_sha = _write_speaker_session_authority(
            session_authority_path, session_authority, ctx=ctx
        )
        reloaded_authority = _load_speaker_session_authority(
            session_authority_path, expected_sha256=authority_sha
        )
        routing_session.clear()
        routing_session.update(
            _speaker_session_state_from_authority(
                session_authority_path, authority_sha, reloaded_authority
            )
        )
        if state is not None:
            state.pop("speaker_routing_authority_status", None)
    return claim


def _reuse_sealed_speaker_claim(
    *,
    date: str,
    items: list[dict],
    session_authority: dict,
    ctx: RunnerContext,
) -> dict | None:
    """Reattach a hash-verified sticky claim without rerunning its provider."""

    prior_path_value = session_authority.get("claim_path")
    prior_sha = str(session_authority.get("claim_sha256") or "")
    try:
        prior_path = Path(str(prior_path_value)).resolve(strict=True)
        if hashlib.sha256(prior_path.read_bytes()).hexdigest() != prior_sha:
            raise ValueError("sealed claim hash mismatch")
        prior_claim = json.loads(prior_path.read_text(encoding="utf-8"))
        prior_request = json.loads(
            Path(str(prior_claim["request_path"])).read_text(encoding="utf-8")
        )
        prior_candidates = prior_request["candidates"]
        if not isinstance(prior_candidates, list):
            raise ValueError("sealed claim candidate inventory is invalid")
        _attach_speaker_routing_claim(
            items=items,
            claim_path=prior_path,
            claim_sha256=prior_sha,
            candidates=prior_candidates,
            pipeline=str(prior_request["pipeline_fingerprint"]),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        ctx.log(f"speaker routing sealed claim failed safe for {date}: {exc}")
        return None
    return prior_claim


def prepare_speaker_routing(
    date: str, items: list[dict], *, state: dict | None = None, ctx: RunnerContext
) -> dict | None:
    """Prepare one session claim, only when an explicit provider is configured.

    No provider means no segment hashing and no extra model lane; the producer's
    ``auto`` mode falls back to the full binary finalizer.  When configured,
    one content hash is reused for all selected candidates on the same sealed
    segment.  Provider failure still emits a RUN_BINARY_FINALIZER claim.
    """

    _clear_speaker_routing_fields(items)
    try:
        date_routing_root = _speaker_date_root(date, ctx=ctx)
    except ValueError as exc:
        ctx.log(f"speaker routing forced binary: {exc}")
        return None
    routing_session: dict | None = None
    session_authority: dict | None = None
    session_authority_path: Path | None = None
    inventory_items = items
    if state is not None:
        current_generation = ctx.pipeline_fingerprint()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", current_generation):
            ctx.log(
                f"speaker routing forced binary for {date}: pipeline fingerprint is invalid"
            )
            return None
        history_exists = any(
            path.is_file() for path in date_routing_root.rglob("*")
        ) if date_routing_root.is_dir() else False
        routing_session = state.get("speaker_routing_session")
        if not isinstance(routing_session, dict):
            if history_exists:
                state["speaker_routing_authority_status"] = (
                    "ROLLBACK_STATE_AUTHORITY_MISSING"
                )
                ctx.log(
                    f"speaker routing forced binary for {date}: routing history exists "
                    "but state authority is missing"
                )
                return None
            inventory = _sealed_speaker_inventory(items)
            session_authority_path = (
                _speaker_generation_root(date, current_generation, ctx=ctx)
                / "session-authority.json"
            )
            session_authority = {
                "schema_version": SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA,
                "date": date,
                "generation_pipeline_fingerprint": current_generation,
                "previous_authority_path": None,
                "sealed_inventory": inventory,
                "sealed_inventory_sha256": _speaker_inventory_sha256(inventory),
                "decision": None,
                "claim_path": None,
                "claim_sha256": None,
                "claim_pipeline_fingerprint": None,
                "provider_classification_sealed": False,
            }
            authority_sha = _write_speaker_session_authority(
                session_authority_path, session_authority, ctx=ctx
            )
            session_authority = _load_speaker_session_authority(
                session_authority_path, expected_sha256=authority_sha
            )
            routing_session = _speaker_session_state_from_authority(
                session_authority_path, authority_sha, session_authority
            )
            state["speaker_routing_session"] = routing_session
        else:
            try:
                authority_value = routing_session.get("authority_path")
                authority_sha = str(routing_session.get("authority_sha256") or "")
                if not authority_value or not re.fullmatch(r"[0-9a-f]{64}", authority_sha):
                    if history_exists:
                        raise ValueError(
                            "routing history exists but mutable state lacks external authority"
                        )
                    # Safe one-time migration is possible only when there are
                    # no prior routing artifacts at all.
                    sealed_inventory = routing_session.get("sealed_inventory")
                    if not isinstance(sealed_inventory, list):
                        raise ValueError("legacy sealed inventory is invalid")
                    session_authority_path = (
                        _speaker_generation_root(date, current_generation, ctx=ctx)
                        / "session-authority.json"
                    )
                    session_authority = {
                        "schema_version": SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA,
                        "date": date,
                        "generation_pipeline_fingerprint": current_generation,
                        "previous_authority_path": None,
                        "sealed_inventory": sealed_inventory,
                        "sealed_inventory_sha256": _speaker_inventory_sha256(
                            sealed_inventory
                        ),
                        "decision": None,
                        "claim_path": None,
                        "claim_sha256": None,
                        "claim_pipeline_fingerprint": None,
                        "provider_classification_sealed": False,
                    }
                    authority_sha = _write_speaker_session_authority(
                        session_authority_path, session_authority, ctx=ctx
                    )
                    session_authority = _load_speaker_session_authority(
                        session_authority_path, expected_sha256=authority_sha
                    )
                    routing_session = _speaker_session_state_from_authority(
                        session_authority_path, authority_sha, session_authority
                    )
                    state["speaker_routing_session"] = routing_session
                else:
                    session_authority_path = Path(str(authority_value)).absolute()
                    session_authority = _load_speaker_session_authority(
                        session_authority_path, expected_sha256=authority_sha
                    )
                    if (
                        session_authority.get("date") != date
                        or not _speaker_session_state_matches_authority(
                            routing_session, session_authority
                        )
                    ):
                        raise ValueError(
                            "mutable state does not match external session authority"
                        )
                    previous_generation = str(
                        session_authority["generation_pipeline_fingerprint"]
                    )
                    if previous_generation != current_generation:
                        new_authority_path = (
                            _speaker_generation_root(date, current_generation, ctx=ctx)
                            / "session-authority.json"
                        )
                        if new_authority_path.exists():
                            raise ValueError(
                                "new pipeline authority already exists without matching state"
                            )
                        # Explicit new generation, always from the prior full
                        # authority inventory and never from this retry subset.
                        new_authority = {
                            "schema_version": SPEAKER_ROUTING_SESSION_AUTHORITY_SCHEMA,
                            "date": date,
                            "generation_pipeline_fingerprint": current_generation,
                            "previous_authority_path": str(session_authority_path),
                            "sealed_inventory": session_authority[
                                "sealed_inventory"
                            ],
                            "sealed_inventory_sha256": session_authority[
                                "sealed_inventory_sha256"
                            ],
                            "decision": None,
                            "claim_path": None,
                            "claim_sha256": None,
                            "claim_pipeline_fingerprint": None,
                            "provider_classification_sealed": False,
                        }
                        new_sha = _write_speaker_session_authority(
                            new_authority_path, new_authority, ctx=ctx
                        )
                        session_authority_path = new_authority_path
                        session_authority = _load_speaker_session_authority(
                            new_authority_path, expected_sha256=new_sha
                        )
                        routing_session = _speaker_session_state_from_authority(
                            new_authority_path, new_sha, session_authority
                        )
                        state["speaker_routing_session"] = routing_session
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                state["speaker_routing_authority_status"] = (
                    "ROLLBACK_OR_TAMPER_DETECTED"
                )
                ctx.log(f"speaker routing forced binary for {date}: {exc}")
                return None
        if session_authority is None or session_authority_path is None:
            ctx.log(f"speaker routing forced binary for {date}: external authority unavailable")
            return None
        inventory_items = session_authority["sealed_inventory"]
    if not inventory_items:
        return None
    routing_root = (
        _speaker_generation_root(
            date,
            str(
                session_authority["generation_pipeline_fingerprint"]
                if session_authority is not None
                else ctx.pipeline_fingerprint()
            ),
            ctx=ctx,
        )
        if state is not None
        else date_routing_root
    )
    request_path = routing_root / "request.json"
    provider_path = routing_root / "provider-evidence.json"
    claim_path = routing_root / "claim.json"
    if session_authority is not None and (
        session_authority.get("provider_classification_sealed") is True
        or (
            session_authority.get("decision") == FAST_SOLO
            and session_authority.get("claim_pipeline_fingerprint")
            == ctx.pipeline_fingerprint()
        )
    ):
        return _reuse_sealed_speaker_claim(
            date=date,
            items=items,
            session_authority=session_authority,
            ctx=ctx,
        )
    try:
        command = _speaker_routing_provider_command(
            request_path=request_path,
            output_path=provider_path,
        )
    except ValueError as exc:
        ctx.log(f"speaker routing disabled for {date}: {exc}")
        return None
    if command is None:
        return None
    try:
        provider_authority = _speaker_routing_provider_authority(command)
    except (OSError, TypeError, ValueError, SpeakerRoutingError) as exc:
        ctx.log(f"speaker routing disabled for {date}: provider authority invalid: {exc}")
        return None
    if provider_authority is None:
        ctx.log(f"speaker routing disabled for {date}: provider authority is missing")
        return None

    try:
        request, candidates = _build_speaker_routing_request(
            date=date,
            inventory_items=inventory_items,
            routing_root=routing_root,
            request_path=request_path,
            provider_authority=provider_authority,
            ctx=ctx,
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        ctx.log(f"speaker routing request failed safe for {date}: {type(exc).__name__}: {exc}")
        return None

    provider_path.unlink(missing_ok=True)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("AUTOSLICE_SPEAKER_ROUTING_PROVIDER_TIMEOUT", "1800")),
            cwd=str(ctx.repo_root),
            env=dict(PROVIDER_SANITIZED_ENVIRONMENT),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        completed = None
        ctx.log(f"speaker routing provider failed safe for {date}: {type(exc).__name__}: {exc}")
    provider_evidence = provider_path if completed is not None and completed.returncode == 0 and provider_path.is_file() else None
    if completed is not None and completed.returncode != 0:
        ctx.log(
            f"speaker routing provider rc={completed.returncode} for {date}; "
            "falling back to binary finalization"
        )
    try:
        claim = _finalize_speaker_routing_claim(
            request_path=request_path,
            claim_path=claim_path,
            provider_evidence=provider_evidence,
            items=items,
            candidates=candidates,
            request=request,
            routing_session=routing_session,
            session_authority=session_authority,
            session_authority_path=session_authority_path,
            state=state,
            ctx=ctx,
        )
    except (KeyError, OSError, TypeError, ValueError, SpeakerRoutingError) as exc:
        _clear_speaker_routing_fields(items)
        ctx.log(f"speaker routing claim failed safe for {date}: {type(exc).__name__}: {exc}")
        return None
    ctx.log(
        f"speaker routing {date}: {claim['decision']} "
        f"({','.join(str(value) for value in claim.get('reason_codes', [])) or 'verified solo'})"
    )
    return claim


def _capture_state_from_result(result: dict, *, ctx: RunnerContext) -> dict[str, object]:
    queue = result.get("queue") if isinstance(result.get("queue"), dict) else {}
    summary: dict[str, object] = {
        "schema_version": "collab-evidence-capture-state.v1",
        "status": str(result.get("status") or "UNKNOWN"),
        "reason_codes": list(
            (result.get("trigger") or {}).get("reason_codes") or []
        )
        if isinstance(result.get("trigger"), dict)
        else [],
        "labels_present": False,
        "predictions_present": False,
        "training_ready": False,
        "upload_authorized": False,
    }
    for field in ("capture_id", "manifest_path", "cue_count", "error"):
        if result.get(field) is not None:
            summary[field] = result[field]
    if queue:
        summary.update(
            {
                "queue_path": str(
                    ctx.base / "state" / "collab-evidence" / "queue" / "queue.v1.json"
                ),
                "candidate_session_count": int(
                    queue.get("candidate_session_count") or 0
                ),
                "candidate_session_quota": int(
                    queue.get("candidate_session_quota") or 0
                ),
                "candidate_session_quota_reached": bool(
                    queue.get("candidate_session_quota_reached")
                ),
            }
        )
    return summary


def queue_collab_evidence_capture(
    date: str,
    state: dict,
    candidates: list[dict],
    *,
    routing_claim: dict | None,
    ctx: RunnerContext,
) -> dict[str, object]:
    """Queue a bounded worker only after production is durably finalized.

    The common no-trigger path is pure and touches no filesystem.  A triggered
    worker receives only source/SRT paths plus an opaque session trigger; no
    provider verdict, candidate identity, subtitle text, label, or prediction
    is persisted in the request.
    """

    trigger = evaluate_collab_capture_trigger(routing_claim, candidates)
    if not trigger.triggered:
        summary = _capture_state_from_result(
            {
                "status": "NO_TRIGGER",
                "trigger": {"reason_codes": []},
            },
            ctx=ctx,
        )
        state["collab_evidence_capture"] = summary
        return summary
    if (ctx.base / "DISABLED").exists():
        summary = _capture_state_from_result(
            {
                "status": "SKIPPED_DISABLED",
                "trigger": {"reason_codes": list(trigger.reason_codes)},
            },
            ctx=ctx,
        )
        state["collab_evidence_capture"] = summary
        return summary
    try:
        if not ctx.date_rx.fullmatch(date):
            raise CollabEvidenceCaptureError("capture date is invalid")
        unique_sources: dict[tuple[str, str], dict[str, str]] = {}
        for candidate in candidates:
            source = str(candidate.get("segment_path") or "")
            srt = str(candidate.get("bcut_srt_path") or "")
            if source and srt:
                unique_sources[(source, srt)] = {
                    "segment_path": source,
                    "bcut_srt_path": srt,
                }
        if not unique_sources:
            raise CollabEvidenceCaptureError("capture has no source/SRT inventory")
        intervals = []
        for row in state.get("song_quarantine_intervals", []):
            if not isinstance(row, dict):
                continue
            source = str(row.get("segment_path") or "")
            start_ms = row.get("start_ms")
            end_ms = row.get("end_ms")
            if (
                source
                and isinstance(start_ms, int)
                and not isinstance(start_ms, bool)
                and isinstance(end_ms, int)
                and not isinstance(end_ms, bool)
            ):
                intervals.append(
                    {
                        "segment_path": source,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    }
                )
        request = {
            "schema_version": WORKER_REQUEST_SCHEMA_VERSION,
            "date": date,
            "candidates": [
                unique_sources[key] for key in sorted(unique_sources)
            ],
            "song_intervals": sorted(
                intervals,
                key=lambda row: (
                    row["segment_path"], row["start_ms"], row["end_ms"]
                ),
            ),
            "trigger": {
                "reason_codes": list(trigger.reason_codes),
                "text_signal_classes": list(trigger.text_signal_classes),
            },
        }
        validate_worker_request_document(request)
        capture_root = ctx.base / "state" / "collab-evidence"
        request_path = capture_root / "requests" / f"{date}.json"
        result_path = request_path.with_suffix(".result.json")
        if request_path.is_file():
            existing = json.loads(request_path.read_text(encoding="utf-8"))
            if (
                not isinstance(existing, dict)
                or existing.get("schema_version") != WORKER_REQUEST_SCHEMA_VERSION
                or existing.get("date") != date
            ):
                raise CollabEvidenceCaptureError(
                    "existing capture request authority is invalid"
                )
            validate_worker_request_document(existing)
            # The first sealed full-session request remains authority across
            # title retries whose mutable pending set may be only a subset.
            request = existing
        else:
            ctx.atomic_write_json(request_path, request)
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise CollabEvidenceCaptureError("capture worker result is invalid")
            summary = _capture_state_from_result(result, ctx=ctx)
        else:
            log_path = ctx.base / "logs" / f"collab-evidence-{date}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as sink:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "src.autoslice.collab_evidence_capture",
                        "--request",
                        str(request_path),
                        "--base-dir",
                        str(capture_root),
                        "--alert-dir",
                        str(ctx.base / "reports"),
                    ],
                    cwd=str(ctx.repo_root),
                    env=ctx.child_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            summary = _capture_state_from_result(
                {
                    "status": "CAPTURE_QUEUED",
                    "trigger": {"reason_codes": list(trigger.reason_codes)},
                },
                ctx=ctx,
            )
            summary.update(
                {
                    "worker_pid": process.pid,
                    "request_path": str(request_path),
                    "result_path": str(result_path),
                    "log_path": str(log_path),
                }
            )
    except Exception as exc:  # noqa: BLE001 - capture cannot undo finalized production
        summary = _capture_state_from_result(
            {
                "status": "CAPTURE_QUEUE_FAILED",
                "error": f"{type(exc).__name__}: {exc}"[:500],
                "trigger": {"reason_codes": list(trigger.reason_codes)},
            },
            ctx=ctx,
        )
        ctx.log(f"collab evidence capture failed open for {date}: {summary['error']}")
    state["collab_evidence_capture"] = summary
    return summary
