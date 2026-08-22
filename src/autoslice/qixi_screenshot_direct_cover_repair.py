"""Sealed, screenshot-only cover repair inputs for one Qixi candidate.

This module deliberately owns no renderer or runtime mutation.  It validates
the immutable predecessor evidence that a fixed wrapper must replay before it
can invoke the normal screenshot composition path.  Keeping this boundary
small prevents a cover repair from becoming a subtitle/title/source-fact lane.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import base64
import re
from contextlib import contextmanager, nullcontext
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.repository_asset_authority import require_repository_asset_authority
from src.autoslice.cover_punch_semantics import (
    review_cover_punch_semantics,
    validate_cover_punch_semantic_review,
)
from src.autoslice.llm_client import LlmConfig, build_llm_call
from src.autoslice.qixi_transaction_core import (
    InstallCallbacks,
    QixiTransactionCoreError,
    create_staged_inode,
    exclusive_runner_lock,
    install_checkpointed_inode,
    journal_before_snapshot,
    restore_owned_inode,
    safe_parent,
    stable_regular_snapshot,
)


ROOT = Path(__file__).resolve().parents[2]
AUTHORITY_PATH = Path(
    "assets/lidousha/qixi_screenshot_direct_cover_repair/"
    "auto_123655_771_844.v1.json"
)
CANDIDATE_ID = "auto_123655_771_844"
RECORDING_DATE = "2026-08-17"
PUNCH_CANDIDATES = ("有女友感吗？", "宿敌有点亲密")
PREFLIGHT_SCHEMA = "qixi-screenshot-direct-cover-repair-preflight.v1"
PREFLIGHT_RECEIPT_SCHEMA = "qixi-screenshot-direct-cover-repair-preflight-receipt.v1"
JOURNAL_SCHEMA = "qixi-screenshot-direct-cover-repair-journal.v1"


class QixiScreenshotDirectCoverRepairError(ValueError):
    """The fixed screenshot-only repair cannot establish its sealed inputs."""


def canonical_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def load_authority(repo_root: Path = ROOT) -> dict[str, object]:
    """Load only a committed/deployed fixed authority asset."""

    path = repo_root / AUTHORITY_PATH
    try:
        raw = path.read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=AUTHORITY_PATH, observed_bytes=raw
        )
        value = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_UNSEALED") from exc
    if not isinstance(value, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_INVALID")
    normalized = validate_authority(value)
    terminal = normalized["terminal_refresh_authority"]
    assert isinstance(terminal, Mapping)
    try:
        terminal_path = Path(str(terminal["relative_path"]))
        if terminal_path.is_absolute() or ".." in terminal_path.parts:
            raise ValueError("terminal authority path escapes")
        terminal_raw = (repo_root / terminal_path).read_bytes()
        require_repository_asset_authority(
            repo_root=repo_root, relative_path=terminal_path, observed_bytes=terminal_raw
        )
        terminal_document = json.loads(terminal_raw)
        if (
            not isinstance(terminal_document, Mapping)
            or terminal_document.get("authority_sha256") != terminal["authority_sha256"]
        ):
            raise ValueError("terminal authority does not bind")
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_TERMINAL_SUCCESSOR_DRIFT") from exc
    return normalized


def validate_authority(value: Mapping[str, object]) -> dict[str, object]:
    """Validate shape and the narrow scope before any runtime/provider work."""

    authority = dict(value)
    claimed = authority.pop("authority_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "runtime_root",
        "upload_enabled", "title", "punch_candidates", "terminal_refresh_authority",
        "legacy_cover", "immutable_media", "title_projection_sha256",
        "allowed_mutations",
    }
    if (
        set(authority) != required
        or authority.get("schema_version")
        != "qixi-screenshot-direct-cover-repair-authority.v1"
        or authority.get("candidate_id") != CANDIDATE_ID
        or authority.get("recording_date") != RECORDING_DATE
        or authority.get("upload_enabled") is not False
        or authority.get("punch_candidates") != list(PUNCH_CANDIDATES)
        or not isinstance(claimed, str)
        or canonical_sha256(authority) != claimed
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_INVALID")
    legacy = authority.get("legacy_cover")
    if not isinstance(legacy, Mapping) or set(legacy) != {
        "final_cover", "failed_joint_qc", "generation_sha256", "subtree_sha256"
    }:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_SCOPE_INVALID")
    for role in ("final_cover", "failed_joint_qc"):
        descriptor = legacy.get(role)
        if (
            not isinstance(descriptor, Mapping)
            or set(descriptor) != {"path", "sha256", "bytes"}
            or not isinstance(descriptor.get("path"), str)
            or not isinstance(descriptor.get("sha256"), str)
            or isinstance(descriptor.get("bytes"), bool)
            or not isinstance(descriptor.get("bytes"), int)
        ):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_DESCRIPTOR_INVALID")
    if not isinstance(legacy.get("subtree_sha256"), Mapping) or not all(
        isinstance(key, str) and isinstance(digest, str) and digest.startswith("sha256:")
        for key, digest in legacy["subtree_sha256"].items()
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_SUBTREE_INVALID")
    title = authority.get("title")
    terminal = authority.get("terminal_refresh_authority")
    allowed = authority.get("allowed_mutations")
    if (
        not isinstance(title, Mapping)
        or set(title) != {"value", "sha256"}
        or not isinstance(title.get("value"), str)
        or title.get("sha256") != _sha256_bytes(title["value"].encode("utf-8"))
        or not isinstance(terminal, Mapping)
        or set(terminal) != {"relative_path", "authority_sha256"}
        or not isinstance(terminal.get("relative_path"), str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(terminal.get("authority_sha256")))
        or not isinstance(allowed, Mapping)
        or set(allowed) != {"record", "delivery_record", "publish", "state"}
        or any(not _valid_mutation_scope(scope) for scope in allowed.values())
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AUTHORITY_SCOPE_INVALID")
    authority["authority_sha256"] = claimed
    return authority


def _valid_mutation_scope(value: object) -> bool:
    """Validate the authority's exact allowed-versus-required pointer contract."""
    if not isinstance(value, Mapping) or set(value) != {"allowed", "required"}:
        return False
    allowed, required = value.get("allowed"), value.get("required")
    if not isinstance(allowed, list) or not isinstance(required, list):
        return False
    if len(allowed) != len(set(allowed)) or len(required) != len(set(required)):
        return False
    if any(not isinstance(item, str) or not item.startswith("/") for item in allowed + required):
        return False
    return set(required).issubset(allowed)


def validate_legacy_cover_inputs(
    authority: Mapping[str, object], *, generation: Mapping[str, object],
    old_cover: bytes, old_qc: bytes,
) -> None:
    """Replay every stable old-cover fact without pinning a mutable record."""

    normalized = validate_authority(authority)
    legacy = normalized["legacy_cover"]
    assert isinstance(legacy, Mapping)
    if (
        _sha256_bytes(old_cover) != legacy["final_cover"]["sha256"]
        or len(old_cover) != legacy["final_cover"]["bytes"]
        or _sha256_bytes(old_qc) != legacy["failed_joint_qc"]["sha256"]
        or len(old_qc) != legacy["failed_joint_qc"]["bytes"]
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LEGACY_BYTES_DRIFT")
    if generation.get("method") != "screenshot_direct" or generation.get("image_generation_used") is not False:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ROUTE_DRIFT")
    subtrees = legacy["subtree_sha256"]
    assert isinstance(subtrees, Mapping)
    for key, expected in subtrees.items():
        if canonical_sha256(generation.get(key)) != expected:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_GENERATION_SUBTREE_DRIFT")
    if canonical_sha256(generation) != legacy["generation_sha256"]:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_GENERATION_DRIFT")


def snapshot_fixed_runtime(authority: Mapping[str, object], *, repo_root: Path = ROOT) -> dict[str, bytes]:
    """Read the terminal successor first, then every mutable cover preimage."""

    normalized = validate_authority(authority)
    try:
        from src.autoslice import qixi_terminal_evidence_refresh as terminal

        terminal_authority = terminal.load_authority(repo_root)
        binding = normalized["terminal_refresh_authority"]
        assert isinstance(binding, Mapping)
        if terminal_authority["authority_sha256"] != binding["authority_sha256"]:
            raise ValueError("terminal authority mismatch")
        runtime = terminal.committed_successor_snapshot(repo_root=repo_root)
        legacy = normalized["legacy_cover"]
        assert isinstance(legacy, Mapping)
        cover = stable_regular_snapshot(Path(str(legacy["final_cover"]["path"])), label="cover repair legacy cover")
        qc = stable_regular_snapshot(Path(str(legacy["failed_joint_qc"]["path"])), label="cover repair legacy qc")
        if cover is None or qc is None:
            raise ValueError("legacy input absent")
        record = json.loads(runtime["record"])
        staging = record.get("publish_staging") if isinstance(record, Mapping) else None
        generation = staging.get("cover_generation") if isinstance(staging, Mapping) else None
        if not isinstance(generation, Mapping):
            raise ValueError("cover generation absent")
        validate_legacy_cover_inputs(normalized, generation=generation, old_cover=cover.payload, old_qc=qc.payload)
        runtime["cover"] = cover.payload
        runtime["qc"] = qc.payload
        return runtime
    except (OSError, ValueError, QixiTransactionCoreError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_RUNTIME_DRIFT") from exc


def build_cover_projection(
    *, authority: Mapping[str, object], runtime: Mapping[str, bytes],
    cover_bytes: bytes, qc_bytes: bytes, generation: Mapping[str, object],
) -> dict[Path, bytes]:
    """Patch only cover fields into terminal-validated JSON preimages."""

    normalized = validate_authority(authority)
    if _sha256_bytes(cover_bytes) != generation.get("final_cover_sha256"):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_COVER_DRIFT")
    try:
        record = json.loads(runtime["record"])
        delivery = json.loads(runtime["delivery_record"])
        publish = json.loads(runtime["publish"])
        state = json.loads(runtime["state"])
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PROJECTION_INVALID") from exc
    if not all(isinstance(value, dict) for value in (record, delivery, publish, state)) or record != delivery:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_RECORD_MIRROR_DRIFT")
    cover_sha = _sha256_bytes(cover_bytes)
    for document in (record, delivery):
        staging = document.get("publish_staging")
        if not isinstance(staging, dict):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUBLISH_STAGING_DRIFT")
        for key in ("cover_path", "cover_status", "cover_text"):
            if key not in staging:
                raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUBLISH_STAGING_DRIFT")
        prior_generation = staging.get("cover_generation")
        if not isinstance(prior_generation, Mapping):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUBLISH_STAGING_DRIFT")
        if prior_generation.get("ai_background") != generation.get("ai_background") or prior_generation.get("ai_background_sha256") != generation.get("ai_background_sha256"):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_AI_BACKGROUND_DRIFT")
        staging["cover_generation"] = dict(generation)
        staging["cover_path"] = str(normalized["legacy_cover"]["final_cover"]["path"])
        staging["cover_status"] = "AI_COVER_READY"
        hashes = document.get("artifact_hashes")
        if not isinstance(hashes, dict):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ARTIFACT_HASH_DRIFT")
        hashes["cover_sha256"] = cover_sha
    publish["cover_generation"] = dict(generation)
    publish["cover_path"] = str(normalized["legacy_cover"]["final_cover"]["path"])
    qc_target = cover_repair_qc_target(normalized)
    publish["title_cover_joint_qc"] = json.loads(qc_bytes)
    publish["title_cover_joint_qc"]["cover_path"] = str(normalized["legacy_cover"]["final_cover"]["path"])
    hashes = publish.get("artifact_hashes")
    if isinstance(hashes, dict):
        hashes["cover_sha256"] = cover_sha
    rows = state.get("picks")
    matches = [row for row in rows if isinstance(row, dict) and row.get("candidate_id") == CANDIDATE_ID] if isinstance(rows, list) else []
    if len(matches) != 1:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STATE_CANDIDATE_DRIFT")
    matches[0].update({"cover_path": str(normalized["legacy_cover"]["final_cover"]["path"]), "cover_generation": dict(generation), "cover_sha256": cover_sha})
    for value in (record, delivery, publish, state):
        _assert_no_stage_locator(value)
    _assert_projection_mutation_contract(
        normalized, before={
            "record": json.loads(runtime["record"]),
            "delivery_record": json.loads(runtime["delivery_record"]),
            "publish": json.loads(runtime["publish"]),
            "state": json.loads(runtime["state"]),
        }, after={
            "record": record, "delivery_record": delivery, "publish": publish, "state": state,
        },
    )
    terminal_paths = {role: Path(str(json.loads(runtime[role]).get("_unused", ""))) for role in ()}
    del terminal_paths
    # The caller obtains these exact paths from the terminal authority, not from a provider response.
    terminal = json.loads((ROOT / str(normalized["terminal_refresh_authority"]["relative_path"])).read_text())
    preimage = terminal["preimage"]
    targets = {
        Path(str(preimage["record"]["path"])): _json_bytes(record),
        Path(str(preimage["delivery_record"]["path"])): _json_bytes(delivery),
        Path(str(preimage["publish"]["path"])): _json_bytes(publish),
        Path(str(preimage["state"]["path"])): _json_bytes(state),
        Path(str(normalized["legacy_cover"]["final_cover"]["path"])): cover_bytes,
        qc_target: qc_bytes,
    }
    return targets


def _assert_no_stage_locator(value: object) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _assert_no_stage_locator(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_stage_locator(child)
    elif isinstance(value, str) and ".qixi-screenshot-cover-stage-" in value:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_LOCATOR_DRIFT")


def _changed_leaf_pointers(before: object, after: object, pointer: str = "") -> set[str]:
    """Return only semantic leaf changes; mapping/list containers are not leaves."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changed: set[str] = set()
        for key in set(before) | set(after):
            child = f"{pointer}/{key}".replace("~", "~0").replace("/", "~1")
            # Reconstruct the separator because only the key itself is escaped.
            child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                changed.add(child)
            else:
                changed |= _changed_leaf_pointers(before[key], after[key], child)
        return changed
    if isinstance(before, list) and isinstance(after, list):
        changed = set()
        for index in range(max(len(before), len(after))):
            child = f"{pointer}/{index}"
            if index >= len(before) or index >= len(after):
                changed.add(child)
            else:
                changed |= _changed_leaf_pointers(before[index], after[index], child)
        return changed
    return set() if before == after else {pointer or "/"}


def _pointer_is_within(pointer: str, root: str) -> bool:
    parts = pointer.strip("/").split("/")
    roots = root.strip("/").split("/")
    return len(parts) >= len(roots) and all(a == "*" or a == b for a, b in zip(roots, parts))


def _assert_projection_mutation_contract(
    authority: Mapping[str, object], *, before: Mapping[str, object], after: Mapping[str, object],
) -> None:
    """Reject extra leaves and require the exact authority-owned mutation roots."""
    allowed = authority["allowed_mutations"]
    assert isinstance(allowed, Mapping)
    for role in ("record", "delivery_record", "publish", "state"):
        scope = allowed[role]
        assert isinstance(scope, Mapping)
        changed = _changed_leaf_pointers(before[role], after[role])
        allowed_roots = scope["allowed"]
        required_roots = scope["required"]
        if any(not any(_pointer_is_within(pointer, root) for root in allowed_roots) for pointer in changed):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ALLOWLIST_DRIFT")
        if any(not any(_pointer_is_within(pointer, root) for pointer in changed) for root in required_roots):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_REQUIRED_MUTATION_MISSING")


def require_repaired_punch(value: object) -> tuple[str, ...]:
    """Prevent a caller from turning a bounded candidate pool into free text."""

    if not isinstance(value, (list, tuple)) or not (1 <= len(value) <= 2):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_INVALID")
    lines = tuple(value)
    if any(not isinstance(line, str) or line not in PUNCH_CANDIDATES for line in lines):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_OUTSIDE_POOL")
    return lines


def _cpa_env(path: Path = Path("/opt/bilive/autoslice/cpa.env")) -> dict[str, str]:
    """Read only the two credential presence values; never expose them."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_CPA_ENV_UNAVAILABLE") from exc
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip().removeprefix("export ")
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"CPA_BASE_URL", "CPA_API_KEY"}:
                values[key.strip()] = value.strip().strip('"').strip("'")
    if not values.get("CPA_BASE_URL") or not values.get("CPA_API_KEY"):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_CPA_CREDENTIALS_MISSING")
    return values


@contextmanager
def _scoped_cpa_environment(path: Path = Path("/opt/bilive/autoslice/cpa.env")):
    """Temporarily expose fixed CPA credentials only to canonical adapters."""
    values = _cpa_env(path)
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _production_review_punch(
    *, story_hook: str, cover_text: str, llm_factory=build_llm_call,
    env_path: Path = Path("/opt/bilive/autoslice/cpa.env"),
) -> tuple[tuple[str, ...], dict[str, object]]:
    """Run the canonical CPA semantic punch gate for this fixed candidate."""
    _cpa_env(env_path)  # Presence gate only; secrets never enter a result/prompt.
    if not story_hook or not cover_text:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_INPUT_INVALID")
    try:
        llm = llm_factory(LlmConfig(
            transport="command",
            command_template="bash scripts/llm_via_cpa.sh {prompt_file} {completion_file} 'gpt-5.6-luna gpt-5.5 gpt-5.4' medium",
            timeout_seconds=600.0,
        ))
        lines, receipt = review_cover_punch_semantics(
            title="【李豆沙】小李有女友感吗？宿敌是否有点亲密了",
            cover_text=cover_text,
            story_hook=story_hook,
            punch=PUNCH_CANDIDATES,
            llm_call=llm,
        )
    except Exception as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_PROVIDER_FAILED") from exc
    lines = require_repaired_punch(lines)
    if not validate_cover_punch_semantic_review(
        receipt, rendered_lines=list(lines), cover_text=cover_text, story_hook=story_hook
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_REVIEW_INVALID")
    return lines, receipt


def _production_joint_qc(
    *, cover_path: Path, logical_cover_path: str, image_probe,
):
    """Build a PASS-only canonical QC receipt for the exact staged bytes."""
    from scripts.run_title_cover_joint_qc import build_joint_qc_receipt
    try:
        receipt = build_joint_qc_receipt(
            cover_path=cover_path,
            title="【李豆沙】小李有女友感吗？宿敌是否有点亲密了",
            candidate_id=CANDIDATE_ID,
            image_probe=image_probe,
            logical_cover_path=logical_cover_path,
        )
    except Exception as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOINT_QC_PROVIDER_FAILED") from exc
    if not isinstance(receipt, Mapping) or receipt.get("status") != "PASS" or receipt.get("pass") is not True:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOINT_QC_INVALID")
    return dict(receipt)


def _production_stage_cover(
    *, authority: Mapping[str, object], root: Path, runtime: Mapping[str, bytes], approved_punch: tuple[str, ...],
    semantic_receipt: Mapping[str, object], cover_mode_override: str,
    require_screenshot_direct: bool,
) -> dict[str, object]:
    """Run the canonical screenshot stage only inside the private root."""
    from src.autoslice.publish_staging import _stage_lidousha_ai_cover
    from src.autoslice.cover_host_identity_gate import (
        validate_final_host_identity_verification,
        verify_lidousha_final_host_identity,
    )
    from src.autoslice.cover_route_evidence import (
        validate_cover_route_decision,
        validate_final_participant_verification,
        validate_rendered_text_pixel_evidence,
    )
    from src.autoslice.cover_source_composition import (
        validate_source_composition_verification,
        verify_lidousha_source_composition,
    )

    try:
        record = json.loads(runtime["record"])
        story = record["story_contract"]
        media_path = Path(str(record["media_path"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INPUT_INVALID") from exc
    if not isinstance(story, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INPUT_INVALID")
    def image_edit_trap(**_kwargs: object) -> dict[str, object]:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_IMAGE_EDIT_FORBIDDEN")
    result = _stage_lidousha_ai_cover(
        record, media_path=media_path, candidate_id=CANDIDATE_ID,
        title="【李豆沙】小李有女友感吗？宿敌是否有点亲密了",
        cover_text=str(record.get("publish_staging", {}).get("cover_text") or ""),
        run_ffmpeg=True, private_artifact_root=root, punch_allowed=True,
        cover_mode_override=cover_mode_override,
        require_screenshot_direct=require_screenshot_direct,
        approved_punch=approved_punch, approved_punch_receipt=semantic_receipt,
        image_edit=image_edit_trap,
        enforce_final_host_identity=True,
        final_host_identity_verifier=verify_lidousha_final_host_identity,
        source_composition_verifier=verify_lidousha_source_composition,
    )
    if not isinstance(result, Mapping) or result.get("status") != "AI_COVER_READY":
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
    generation = result.get("cover_generation")
    cover_path = result.get("cover_path")
    if (
        not isinstance(generation, Mapping) or not isinstance(cover_path, str)
        or generation.get("method") != "screenshot_direct"
        or generation.get("image_generation_used") is not False
        or generation.get("image_generation_attempted") is not False
        or generation.get("route_decision", {}).get("actual_treatment") != "screenshot_direct"
        or not validate_cover_route_decision(generation, allow_legacy_v1=False)
        or not validate_final_host_identity_verification(generation)
        or not validate_rendered_text_pixel_evidence(generation)
        or not isinstance(generation.get("thumbnail_text_gate"), Mapping)
        or generation["thumbnail_text_gate"].get("status") != "PASS"
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
    route = generation["route_decision"]
    required_participants = route.get("required_participant_ids")
    if not isinstance(required_participants, list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
    if required_participants and not validate_final_participant_verification(generation):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
    reference_sha = generation.get("reference_sha256")
    if not isinstance(reference_sha, str) or not validate_source_composition_verification(
        generation.get("source_composition_verification"), reference_sha256=reference_sha
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
    staged = Path(cover_path)
    if not staged.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_ESCAPE")
    try:
        snapshot = stable_regular_snapshot(staged, label="cover repair private output")
    except QixiTransactionCoreError as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_ESCAPE") from exc
    if snapshot is None or snapshot.payload != staged.read_bytes():
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_ESCAPE")
    relocated, sidecars = _relocate_private_generation(
        authority=authority, root=root, generation=generation, cover_path=staged,
    )
    logical_cover = str(authority_final_cover_path(authority))
    try:
        from src.autoslice.qixi_post_correction_public_surface import replayed_cover_gate_callables

        after = dict(sidecars)
        after[Path(logical_cover)] = snapshot.payload
        for _predicate, check in replayed_cover_gate_callables(
            relocated, story=story, package_root=Path(logical_cover).parent, after=after,
        ):
            check()
    except Exception as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_RELOCATED_GATE_BLOCKED") from exc
    return {"generation": relocated, "cover_path": staged, "sidecars": sidecars,
            "logical_cover_path": logical_cover,
            "logical_qc_path": str(cover_repair_qc_target(authority))}


def authority_final_cover_path(authority: Mapping[str, object]) -> Path:
    """Return the one official cover target named by the sealed authority."""
    normalized = validate_authority(authority)
    legacy = normalized["legacy_cover"]
    assert isinstance(legacy, Mapping)
    final = legacy.get("final_cover")
    value = final.get("path") if isinstance(final, Mapping) else None
    if not isinstance(value, str) or not value.startswith("/opt/bilive/autoslice/"):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_LOGICAL_TARGET_DRIFT")
    return Path(value)


def cover_repair_qc_target(authority: Mapping[str, object]) -> Path:
    """Create the one authority-bound, create-only PASS QC locator."""
    normalized = validate_authority(authority)
    cover = authority_final_cover_path(normalized)
    return cover.with_name(
        f"{CANDIDATE_ID}.qixi-cover-repair-"
        f"{str(normalized['authority_sha256'])[7:23]}.title-cover-joint-qc.json"
    )


def _relocate_private_generation(
    *, authority: Mapping[str, object], root: Path, generation: Mapping[str, object], cover_path: Path,
) -> tuple[dict[str, object], dict[Path, bytes]]:
    """Freeze every private evidence file and replace only its exact locator.

    A stage directory is deliberately deleted after full dry-run, so no path
    occurring in the public generation may still refer to it.  The actual
    final cover is the authority's fixed target; every other stage artifact is
    put in a distinct authority-keyed namespace and committed create-only.
    """
    from src.autoslice.qixi_post_correction_projection_paths import public_artifact_path, public_artifact_root

    final_cover = authority_final_cover_path(authority)
    namespace = public_artifact_root(final_cover.parent, authority)
    root = root.resolve(strict=True)
    targets: dict[Path, bytes] = {}

    def convert(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): convert(child) for key, child in value.items()}
        if isinstance(value, list):
            return [convert(child) for child in value]
        if not isinstance(value, str) or not value.startswith(str(root) + "/"):
            return value
        stage = Path(value)
        try:
            snapshot = stable_regular_snapshot(stage, label="cover repair stage sidecar")
        except QixiTransactionCoreError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_SIDECAR_INVALID") from exc
        if snapshot is None:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_SIDECAR_INVALID")
        if stage.resolve(strict=True) == cover_path.resolve(strict=True):
            target = final_cover
        else:
            try:
                relative = stage.resolve(strict=True).relative_to(root).as_posix()
            except ValueError as exc:
                raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_ESCAPE") from exc
            target = public_artifact_path(namespace, relative)
        previous = targets.get(target)
        if previous is not None and previous != snapshot.payload:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_SIDECAR_COLLISION")
        targets[target] = snapshot.payload
        return str(target)

    relocated = convert(generation)
    if not isinstance(relocated, dict) or relocated.get("final_cover") != str(final_cover):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_SIDECAR_INVALID")
    _assert_no_stage_locator(relocated)
    return relocated, {path: payload for path, payload in targets.items() if path != final_cover}


def run_canonical_full_dry(
    *, authority: Mapping[str, object], stage_root_parent: Path, runtime: Mapping[str, bytes],
    review_punch: object, stage_cover: object, joint_qc: object,
) -> dict[str, object]:
    """Execute the fixed review→screenshot→QC gates before storing success.

    Adapters are injected so the CLI has one canonical route while tests never
    contact a provider.  The stage adapter must already enforce forced
    screenshot_direct, route/pixel/host/participant gates and return its
    verified generation plus private cover bytes.
    """
    normalized = validate_authority(authority)
    if not callable(review_punch) or not callable(stage_cover) or not callable(joint_qc):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_CANONICAL_ADAPTER_INVALID")
    record = json.loads(runtime["record"])
    story = record.get("story_contract") if isinstance(record, Mapping) else None
    staging = record.get("publish_staging") if isinstance(record, Mapping) else None
    if not isinstance(story, Mapping) or not isinstance(staging, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_RUNTIME_DRIFT")
    title = normalized["title"]["value"]
    cover_text = staging.get("cover_text")
    semantic = review_punch(
        title=title, story=story, cover_text=cover_text, candidates=PUNCH_CANDIDATES
    )
    if not isinstance(semantic, Mapping) or semantic.get("status") != "PASS":
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_SEMANTIC_BLOCKED")
    punch = require_repaired_punch(semantic.get("final_punch"))
    def stage(root: Path) -> Mapping[str, object]:
        output = stage_cover(
            authority=normalized, root=root, runtime=runtime, approved_punch=punch, semantic_receipt=semantic,
            cover_mode_override="screenshot", require_screenshot_direct=True,
        )
        if not isinstance(output, Mapping) or output.get("generation", {}).get("method") != "screenshot_direct" or output.get("generation", {}).get("image_generation_used") is not False:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
        cover_path = output.get("cover_path")
        if not isinstance(cover_path, Path):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_SCREENSHOT_GATE_BLOCKED")
        qc = joint_qc(cover_path=cover_path, title=title, candidate_id=CANDIDATE_ID, logical_cover_path=output.get("logical_cover_path"))
        if not isinstance(qc, Mapping) or qc.get("status") != "PASS" or qc.get("pass") is not True:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOINT_QC_BLOCKED")
        return {**output, "qc_bytes": _json_bytes(dict(qc))}
    return run_private_preflight(
        authority=normalized, stage_root_parent=stage_root_parent, stage=stage,
        runtime_preimage=runtime,
    )


def run_fixed_full_dry(*, repo_root: Path = ROOT, stage_root_parent: Path | None = None) -> dict[str, object]:
    """Execute the only real fixed full-dry route; no official target is writable."""
    authority = load_authority(repo_root)
    runtime = snapshot_fixed_runtime(authority, repo_root=repo_root)
    parent = stage_root_parent or Path(str(authority["runtime_root"])) / "reports"
    from src.autoslice.cpa_frame_witness import image_vision_probe

    def review_punch(*, title: object, story: object, cover_text: object, candidates: object) -> Mapping[str, object]:
        if title != authority["title"]["value"] or candidates != PUNCH_CANDIDATES or not isinstance(story, Mapping):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_INPUT_INVALID")
        lines, receipt = _production_review_punch(
            story_hook=str(story.get("selection_hook") or ""), cover_text=str(cover_text or "")
        )
        if tuple(receipt.get("final_punch") or ()) != lines:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PUNCH_REVIEW_INVALID")
        return receipt

    def joint_qc(*, cover_path: Path, title: object, candidate_id: object, logical_cover_path: object) -> dict[str, object]:
        if title != authority["title"]["value"] or candidate_id != CANDIDATE_ID or not isinstance(logical_cover_path, str):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOINT_QC_INVALID")
        return _production_joint_qc(
            cover_path=cover_path, logical_cover_path=logical_cover_path,
            image_probe=lambda path, question: image_vision_probe(
                path, question, api_base=os.environ.get("CPA_BASE_URL", ""),
                api_key=os.environ.get("CPA_API_KEY", ""),
            ),
        )

    with _scoped_cpa_environment():
        return run_canonical_full_dry(
            authority=authority, stage_root_parent=parent, runtime=runtime,
            review_punch=review_punch, stage_cover=_production_stage_cover, joint_qc=joint_qc,
        )


def _unique_stored_preflight(parent: Path, authority: Mapping[str, object]) -> tuple[Path, dict[str, object]]:
    root = _preflight_store_root(parent, authority)
    candidates: list[tuple[Path, dict[str, object]]] = []
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink():
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
        candidates.append((child, _load_preflight_store(child, authority=authority)))
    if len(candidates) != 1:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_AMBIGUOUS")
    return candidates[0]


def run_fixed_apply(*, repo_root: Path = ROOT, stage_root_parent: Path | None = None) -> dict[str, object]:
    """Replay exactly one sealed full-dry store without provider access."""
    authority = load_authority(repo_root)
    parent = stage_root_parent or Path(str(authority["runtime_root"])) / "reports"
    with exclusive_runner_lock(Path(str(authority["runtime_root"]))):
        _store, stored = _unique_stored_preflight(parent, authority)
        manifest = stored["manifest"]
        assert isinstance(manifest, Mapping)
        transaction = _transaction_root(parent, authority, str(manifest["preflight_sha256"]))
        journal_path = transaction / "journal.json"
        if os.path.lexists(journal_path):
            journal = _read_journal(journal_path, authority, str(manifest["preflight_sha256"]))
            replay = _sealed_journal_targets(
                authority=authority, journal=journal, stored=stored,
            )
            return apply_preflight_targets(
                authority=authority, stage_root_parent=parent,
                preflight_sha256=str(manifest["preflight_sha256"]), targets=replay,
                apply=True, lock_held=True, strict_targets=True,
            )
        runtime = snapshot_fixed_runtime(authority, repo_root=repo_root)
        expected = manifest.get("runtime_preimage")
        observed = {role: _sha256_bytes(payload) for role, payload in runtime.items()}
        if not isinstance(expected, Mapping) or expected != observed:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_RUNTIME_DRIFT")
        targets = build_cover_projection(
            authority=authority, runtime=runtime, cover_bytes=stored["cover"],
            qc_bytes=stored["joint_qc"], generation=stored["generation"],
        )
        sidecars = stored["sidecars"]
        assert isinstance(sidecars, Mapping)
        for path, payload in sidecars.items():
            if path in targets:
                raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_SIDECAR_COLLISION")
            targets[path] = payload
        return apply_preflight_targets(
            authority=authority, stage_root_parent=parent,
            preflight_sha256=str(manifest["preflight_sha256"]), targets=targets, apply=True,
            lock_held=True, strict_targets=True,
        )


def _sealed_journal_targets(
    *, authority: Mapping[str, object], journal: Mapping[str, object], stored: Mapping[str, object],
) -> dict[Path, bytes]:
    """Resume only a journal whose postimages are bound to its sealed store."""
    entries = journal.get("entries")
    manifest, generation = stored.get("manifest"), stored.get("generation")
    if not isinstance(entries, list) or not isinstance(manifest, Mapping) or not isinstance(generation, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    sidecars = stored.get("sidecars")
    if not isinstance(sidecars, Mapping) or any(not isinstance(path, Path) or not isinstance(payload, bytes) for path, payload in sidecars.items()):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    terminal = json.loads((ROOT / str(authority["terminal_refresh_authority"]["relative_path"])).read_text())
    preimage = terminal.get("preimage")
    if not isinstance(preimage, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    json_paths = {Path(str(preimage[role]["path"])) for role in ("record", "delivery_record", "publish", "state")}
    expected = json_paths | {authority_final_cover_path(authority), cover_repair_qc_target(authority)} | set(sidecars)
    targets: dict[Path, bytes] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
        try:
            target = Path(str(entry["target"]))
            payload = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT") from exc
        if target in targets or target not in expected:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
        targets[target] = payload
    if set(targets) != expected:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    if targets[authority_final_cover_path(authority)] != stored.get("cover") or targets[cover_repair_qc_target(authority)] != stored.get("joint_qc"):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    if any(targets[path] != payload for path, payload in sidecars.items()):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    try:
        record = json.loads(targets[Path(str(preimage["record"]["path"]))])
        delivery = json.loads(targets[Path(str(preimage["delivery_record"]["path"]))])
        publish = json.loads(targets[Path(str(preimage["publish"]["path"]))])
        state = json.loads(targets[Path(str(preimage["state"]["path"]))])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT") from exc
    if (
        not isinstance(record, Mapping) or not isinstance(delivery, Mapping)
        or record != delivery
        or record.get("publish_staging", {}).get("cover_generation") != generation
        or publish.get("cover_generation") != generation
        or publish.get("title_cover_joint_qc") != json.loads(stored["joint_qc"])
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    rows = state.get("picks") if isinstance(state, Mapping) else None
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("candidate_id") == CANDIDATE_ID] if isinstance(rows, list) else []
    if len(matches) != 1 or matches[0].get("cover_generation") != generation:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    return targets


def build_preflight_manifest(
    *, authority: Mapping[str, object], cover_bytes: bytes, qc_bytes: bytes,
    generation: Mapping[str, object], logical_cover_path: str,
    logical_qc_path: str, runtime_preimage: Mapping[str, bytes] | None = None,
    sidecars: Mapping[Path, bytes] | None = None,
) -> dict[str, object]:
    """Freeze a successful private-stage result without writing any target.

    The caller owns staging/provider execution.  This small pure primitive
    deliberately accepts only byte images plus their intended final logical
    paths, so APPLY can consume the exact preflight rather than rerunning a
    provider or silently choosing a new route.
    """
    normalized = validate_authority(authority)
    if runtime_preimage is not None and any(
        not isinstance(role, str) or not isinstance(payload, bytes)
        for role, payload in runtime_preimage.items()
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_INVALID")
    if sidecars is not None and any(
        not isinstance(path, Path) or not isinstance(payload, bytes)
        for path, payload in sidecars.items()
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_INVALID")
    if (
        generation.get("method") != "screenshot_direct"
        or generation.get("image_generation_used") is not False
        or not isinstance(logical_cover_path, str)
        or not logical_cover_path
        or not isinstance(logical_qc_path, str)
        or not logical_qc_path
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_INVALID")
    if _sha256_bytes(cover_bytes) != generation.get("final_cover_sha256"):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_COVER_DRIFT")
    try:
        qc = json.loads(qc_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_QC_INVALID") from exc
    if (
        not isinstance(qc, Mapping)
        or qc.get("schema_version") != "lidousha-title-cover-joint-qc.v1"
        or qc.get("status") != "PASS"
        or qc.get("pass") is not True
        or qc.get("cover_path") != logical_cover_path
        or qc.get("cover_sha256") != _sha256_bytes(cover_bytes)
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_QC_INVALID")
    manifest: dict[str, object] = {
        "schema_version": PREFLIGHT_SCHEMA,
        "candidate_id": CANDIDATE_ID,
        "recording_date": RECORDING_DATE,
        "upload_enabled": False,
        "authority_sha256": normalized["authority_sha256"],
        "route": "screenshot_direct",
        "image_generation_used": False,
        "cover": {"logical_path": logical_cover_path, "sha256": _sha256_bytes(cover_bytes), "bytes": len(cover_bytes)},
        "joint_qc": {"logical_path": logical_qc_path, "sha256": _sha256_bytes(qc_bytes), "bytes": len(qc_bytes)},
        "generation_sha256": canonical_sha256(generation),
        "runtime_preimage": {
            role: _sha256_bytes(payload)
            for role, payload in sorted((runtime_preimage or {}).items())
        },
        "sidecars": {
            str(path): {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            for path, payload in sorted((sidecars or {}).items(), key=lambda item: str(item[0]))
        },
    }
    manifest["preflight_sha256"] = canonical_sha256(manifest)
    return manifest


def validate_preflight_manifest(value: Mapping[str, object], *, authority: Mapping[str, object]) -> dict[str, object]:
    """Reject hand-edited or cross-authority preflight data before APPLY."""
    manifest = dict(value)
    claimed = manifest.pop("preflight_sha256", None)
    required = {
        "schema_version", "candidate_id", "recording_date", "upload_enabled",
        "authority_sha256", "route", "image_generation_used", "cover", "joint_qc",
        "generation_sha256", "runtime_preimage", "sidecars",
    }
    normalized = validate_authority(authority)
    if (
        set(manifest) != required
        or claimed != canonical_sha256(manifest)
        or manifest.get("schema_version") != PREFLIGHT_SCHEMA
        or manifest.get("candidate_id") != CANDIDATE_ID
        or manifest.get("recording_date") != RECORDING_DATE
        or manifest.get("upload_enabled") is not False
        or manifest.get("authority_sha256") != normalized["authority_sha256"]
        or manifest.get("route") != "screenshot_direct"
        or manifest.get("image_generation_used") is not False
        or not isinstance(manifest.get("runtime_preimage"), Mapping)
        or any(
            not isinstance(role, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest))
            for role, digest in manifest.get("runtime_preimage", {}).items()
        )
        or not isinstance(manifest.get("sidecars"), Mapping)
        or any(
            not isinstance(path, str) or not path.startswith("/opt/bilive/autoslice/")
            or not isinstance(row, Mapping) or set(row) != {"sha256", "bytes"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(row.get("sha256")))
            or isinstance(row.get("bytes"), bool) or not isinstance(row.get("bytes"), int)
            for path, row in manifest.get("sidecars", {}).items()
        )
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_DRIFT")
    for role in ("cover", "joint_qc"):
        row = manifest.get(role)
        if not isinstance(row, Mapping) or set(row) != {"logical_path", "sha256", "bytes"}:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_DRIFT")
    manifest["preflight_sha256"] = claimed
    return manifest


def run_private_preflight(
    *, authority: Mapping[str, object], stage_root_parent: Path,
    stage: object, runtime_preimage: Mapping[str, bytes] | None = None,
) -> dict[str, object]:
    """Run one injected canonical stage wholly below a private directory.

    The stage callable returns ``generation``, ``cover_path``, ``qc_bytes`` and
    ``logical_cover_path``/``logical_qc_path``.  This core owns only private
    filesystem hygiene and manifest freezing; it deliberately has no access
    to official record/state/journal paths.
    """
    normalized = validate_authority(authority)
    if not callable(stage):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_UNAVAILABLE")
    _require_private_parent(stage_root_parent)
    root = Path(tempfile.mkdtemp(prefix=".qixi-screenshot-cover-stage-", dir=stage_root_parent))
    root_identity = os.lstat(root)
    try:
        os.chmod(root, 0o700)
        output = stage(root)
        if not isinstance(output, Mapping):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INVALID")
        generation = output.get("generation")
        cover_path = output.get("cover_path")
        qc_bytes = output.get("qc_bytes")
        logical_cover = output.get("logical_cover_path")
        logical_qc = output.get("logical_qc_path")
        sidecars = output.get("sidecars", {})
        if not isinstance(generation, Mapping) or not isinstance(cover_path, Path) or not isinstance(qc_bytes, bytes) or not isinstance(sidecars, Mapping):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INVALID")
        if any(not isinstance(path, Path) or not isinstance(payload, bytes) for path, payload in sidecars.items()):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_INVALID")
        resolved_root = root.resolve(strict=True)
        resolved_cover = cover_path.resolve(strict=True)
        if not resolved_cover.is_relative_to(resolved_root):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_ESCAPE")
        try:
            cover_snapshot = stable_regular_snapshot(resolved_cover, label="cover private stage")
        except QixiTransactionCoreError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_COVER_INVALID") from exc
        if cover_snapshot is None:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_COVER_INVALID")
        cover_bytes = cover_snapshot.payload
        manifest = build_preflight_manifest(
            authority=normalized, cover_bytes=cover_bytes, qc_bytes=qc_bytes,
            generation=generation, logical_cover_path=logical_cover,
            logical_qc_path=logical_qc, runtime_preimage=runtime_preimage, sidecars=sidecars,
        )
        store = _preflight_store_root(stage_root_parent, normalized)
        digest = str(manifest["preflight_sha256"])[7:]
        destination = store / digest
        if os.path.lexists(destination):
            reusable = _load_preflight_store(destination, authority=normalized)
            if reusable["manifest"] != manifest:
                raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_COLLISION")
            return {
                "status": "FULL_DRY_RUN_REUSED", "upload_enabled": False,
                "manifest": manifest, "preflight_store": str(destination), "target_writes": 0,
            }
        try:
            destination.mkdir(mode=0o700)
        except OSError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
        files = {
            "cover.png": cover_bytes,
            "joint-qc.json": qc_bytes,
            "generation.json": json.dumps(generation, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "manifest.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            "sidecars.json": _json_bytes({
                str(path): base64.b64encode(payload).decode("ascii")
                for path, payload in sorted(sidecars.items(), key=lambda item: str(item[0]))
            }),
        }
        for name, payload in files.items():
            _write_private_new(destination / name, payload)
        receipt = _preflight_receipt(manifest, files)
        _write_private_new(destination / "receipt.json", _json_bytes(receipt))
        _fsync_directory(destination)
        _load_preflight_store(destination, authority=normalized)
        return {
            "status": "FULL_DRY_RUN_PASS", "upload_enabled": False,
            "manifest": manifest, "preflight_store": str(destination), "target_writes": 0,
        }
    finally:
        # Successful preflight returns only sealed bytes/hashes, never a
        # provider raw response or private artifact locator.
        _remove_owned_private_stage(root, root_identity)


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _require_private_parent(parent: Path) -> None:
    try:
        safe_parent(parent / ".qixi-private-probe")
        info = os.lstat(parent)
    except (OSError, QixiTransactionCoreError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE")


def _private_directory(path: Path) -> None:
    _require_private_parent(path.parent)
    if not os.path.lexists(path):
        try:
            path.mkdir(mode=0o700)
        except OSError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE")


def _preflight_store_root(parent: Path, authority: Mapping[str, object]) -> Path:
    namespace = parent / "qixi_screenshot_direct_cover_preflights"
    _private_directory(namespace)
    root = namespace / str(authority["authority_sha256"])[7:23]
    _private_directory(root)
    return root


def _write_private_new(path: Path, payload: bytes) -> None:
    try:
        snapshot = create_staged_inode(path, payload=payload, mode=0o600, label="cover preflight store")
    except QixiTransactionCoreError as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_UNSAFE") from exc
    if snapshot.payload != payload:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")


def _preflight_receipt(manifest: Mapping[str, object], files: Mapping[str, bytes]) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": PREFLIGHT_RECEIPT_SCHEMA,
        "status": "STORED",
        "authority_sha256": manifest["authority_sha256"],
        "preflight_sha256": manifest["preflight_sha256"],
        "files": {
            name: {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
            for name, payload in files.items()
        },
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def _load_preflight_store(destination: Path, *, authority: Mapping[str, object]) -> dict[str, object]:
    _private_directory(destination)
    expected_names = {"cover.png", "joint-qc.json", "generation.json", "manifest.json", "sidecars.json", "receipt.json"}
    if {child.name for child in destination.iterdir()} != expected_names:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
    payloads: dict[str, bytes] = {}
    for name in expected_names:
        try:
            snapshot = stable_regular_snapshot(destination / name, label="cover preflight store")
        except QixiTransactionCoreError as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT") from exc
        if snapshot is None or snapshot.mode != 0o600:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
        payloads[name] = snapshot.payload
    try:
        manifest = json.loads(payloads["manifest.json"])
        receipt = json.loads(payloads["receipt.json"])
        generation = json.loads(payloads["generation.json"])
        encoded_sidecars = json.loads(payloads["sidecars.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT") from exc
    manifest = validate_preflight_manifest(manifest, authority=authority)
    if not isinstance(encoded_sidecars, Mapping):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
    sidecars: dict[Path, bytes] = {}
    for path, encoded in encoded_sidecars.items():
        if not isinstance(path, str) or not isinstance(encoded, str):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
        try:
            sidecars[Path(path)] = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT") from exc
    expected_sidecars = manifest["sidecars"]
    if {
        str(path): {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
        for path, payload in sidecars.items()
    } != expected_sidecars:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
    files = {key: value for key, value in payloads.items() if key != "receipt.json"}
    if (
        not isinstance(receipt, Mapping)
        or receipt != _preflight_receipt(manifest, files)
        or canonical_sha256(generation) != manifest["generation_sha256"]
    ):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_STORE_DRIFT")
    return {"manifest": manifest, "cover": payloads["cover.png"], "joint_qc": payloads["joint-qc.json"], "generation": generation, "sidecars": sidecars}


def _remove_owned_private_stage(root: Path, owner: os.stat_result) -> None:
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_CLEANUP_FAILED") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (owner.st_dev, owner.st_ino):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_CLEANUP_FAILED")
    for child in root.rglob("*"):
        if stat.S_ISLNK(os.lstat(child).st_mode):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_STAGE_CLEANUP_FAILED")
    shutil.rmtree(root)


def apply_preflight_targets(
    *, authority: Mapping[str, object], stage_root_parent: Path,
    preflight_sha256: str, targets: Mapping[Path, bytes], apply: bool,
    lock_held: bool = False, strict_targets: bool = False,
) -> dict[str, object]:
    """Install an already-stored cover projection; it never invokes a provider.

    The caller must derive ``targets`` from the sealed candidate projection.
    This boundary freezes target bytes into a durable CAS journal before the
    first rename, and is deliberately useful to the fixed CLI and test seams
    without accepting arbitrary provider data at apply time.
    """

    normalized = validate_authority(authority)
    if not isinstance(preflight_sha256, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", preflight_sha256):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PREFLIGHT_DRIFT")
    if not targets or any(not isinstance(path, Path) or not isinstance(payload, bytes) for path, payload in targets.items()):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_PROJECTION_INVALID")
    if not apply:
        return {"status": "PLAN_PASS", "target_writes": 0, "upload_enabled": False}
    lock = nullcontext() if lock_held else exclusive_runner_lock(Path(str(normalized["runtime_root"])))
    with lock:
        root = _transaction_root(stage_root_parent, normalized, preflight_sha256)
        journal_path = root / "journal.json"
        if os.path.lexists(journal_path):
            journal = _read_journal(journal_path, normalized, preflight_sha256)
            if journal["status"] == "COMMITTED":
                _verify_committed(journal)
                _write_transaction_receipt(root, journal)
                return {"status": "ALREADY_COMMITTED", "target_writes": 0, "upload_enabled": False}
        else:
            entries: list[dict[str, object]] = []
            for index, (target, payload) in enumerate(sorted(targets.items(), key=lambda item: str(item[0]))):
                try:
                    snapshot = stable_regular_snapshot(target, label="cover repair preimage")
                except QixiTransactionCoreError as exc:
                    raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_TARGET_UNSAFE") from exc
                if strict_targets and target not in _replaceable_targets(normalized) and snapshot is not None:
                    raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_CREATE_ONLY_COLLISION")
                entries.append({
                    "target": str(target), "before_bytes_b64": (base64.b64encode(snapshot.payload).decode() if snapshot else None),
                    "before_sha256": (snapshot.sha256 if snapshot else None), "before_mode": (snapshot.mode if snapshot else None),
                    "before_device": (snapshot.device if snapshot else None), "before_inode": (snapshot.inode if snapshot else None),
                    "after_bytes_b64": base64.b64encode(payload).decode(),
                    "after_sha256": _sha256_bytes(payload), "after_mode": (snapshot.mode if snapshot else 0o600),
                    "staged_name": f".{target.name}.qixi-cover-{index}.tmp",
                    "staged_device": None, "staged_inode": None,
                    "installed_device": None, "installed_inode": None, "phase": "PREPARED",
                })
            journal = {"schema_version": JOURNAL_SCHEMA, "status": "PREPARED", "authority_sha256": normalized["authority_sha256"], "preflight_sha256": preflight_sha256, "entries": entries}
            _write_journal_new(journal_path, journal)
        try:
            _commit_journal(root, journal)
        except BaseException:
            try:
                _rollback_journal(root, journal)
            except BaseException:
                journal["status"] = "ROLLBACK_REQUIRED"
                _write_journal(journal_path, journal)
            raise
        _write_transaction_receipt(root, journal)
        return {"status": "COMMITTED", "target_writes": len(targets), "upload_enabled": False}


def _replaceable_targets(authority: Mapping[str, object]) -> set[Path]:
    """Only the terminal JSON mirrors and sealed predecessor cover may replace."""
    try:
        terminal = json.loads((ROOT / str(authority["terminal_refresh_authority"]["relative_path"])).read_text())
        preimage = terminal["preimage"]
        targets = {Path(str(preimage[role]["path"])) for role in ("record", "delivery_record", "publish", "state")}
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_TARGET_SCHEMA_INVALID") from exc
    targets.add(authority_final_cover_path(authority))
    return targets


def _transaction_root(parent: Path, authority: Mapping[str, object], preflight_sha256: str) -> Path:
    root = parent / "qixi_screenshot_direct_cover_transactions" / str(authority["authority_sha256"])[7:23] / preflight_sha256[7:23]
    for directory in (root.parent.parent, root.parent, root):
        _private_directory(directory)
    return root


def _journal_digest(journal: Mapping[str, object]) -> str:
    return canonical_sha256({key: value for key, value in journal.items() if key != "journal_sha256"})


def _write_journal_new(path: Path, journal: dict[str, object]) -> None:
    journal["journal_sha256"] = _journal_digest(journal)
    _write_private_new(path, _json_bytes(journal))


def _write_journal(path: Path, journal: dict[str, object]) -> None:
    journal["journal_sha256"] = _journal_digest(journal)
    old = stable_regular_snapshot(path, label="cover repair journal")
    if old is None:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    staged = create_staged_inode(path.with_name(".journal.qixi-cover.tmp"), payload=_json_bytes(journal), mode=0o600, label="cover repair journal")
    install_checkpointed_inode(staged, target=path, expected_before=old, callbacks=InstallCallbacks(checkpoint_installed=lambda _snap: None, verify_installed=lambda _snap: None), label="cover repair journal")


def _read_journal(path: Path, authority: Mapping[str, object], preflight_sha256: str) -> dict[str, object]:
    snapshot = stable_regular_snapshot(path, label="cover repair journal")
    if snapshot is None:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    try:
        journal = json.loads(snapshot.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT") from exc
    if not isinstance(journal, dict) or journal.get("schema_version") != JOURNAL_SCHEMA or journal.get("authority_sha256") != authority["authority_sha256"] or journal.get("preflight_sha256") != preflight_sha256 or journal.get("journal_sha256") != _journal_digest(journal):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    return journal


def _commit_journal(root: Path, journal: dict[str, object]) -> None:
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
        target = Path(str(entry["target"]))
        payload = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        if entry["phase"] == "INSTALLED":
            continue
        staged = create_staged_inode(target.with_name(str(entry["staged_name"])), payload=payload, mode=int(entry["after_mode"]), label="cover repair stage")
        entry.update({"phase": "INSTALLING", "staged_device": staged.device, "staged_inode": staged.inode})
        _write_journal(root / "journal.json", journal)
        before = journal_before_snapshot(entry, target=target)
        installed = install_checkpointed_inode(staged, target=target, expected_before=before, callbacks=InstallCallbacks(checkpoint_installed=lambda snap: (entry.update({"phase": "INSTALLED", "installed_device": snap.device, "installed_inode": snap.inode}), _write_journal(root / "journal.json", journal)), verify_installed=lambda _snap: None), label="cover repair install")
        entry.update({"phase": "INSTALLED", "installed_device": installed.device, "installed_inode": installed.inode})
        _write_journal(root / "journal.json", journal)
    journal["status"] = "COMMITTED"
    _write_journal(root / "journal.json", journal)
    _verify_committed(journal)


def _rollback_journal(root: Path, journal: dict[str, object]) -> None:
    entries = journal.get("entries")
    if not isinstance(entries, list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_JOURNAL_DRIFT")
    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("phase") != "INSTALLED":
            continue
        target = Path(str(entry["target"]))
        after = base64.b64decode(str(entry["after_bytes_b64"]), validate=True)
        current = stable_regular_snapshot(target, label="cover repair rollback")
        if current is None or current.payload != after or (current.device, current.inode) != (entry["installed_device"], entry["installed_inode"]):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ROLLBACK_OWNERSHIP_DRIFT")
        restored = restore_owned_inode(current, before=journal_before_snapshot(entry, target=target), label="cover repair rollback")
        before_payload = entry["before_bytes_b64"]
        if (before_payload is None and restored is not None) or (before_payload is not None and (restored is None or restored.payload != base64.b64decode(str(before_payload), validate=True))):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_ROLLBACK_DRIFT")
        entry["phase"] = "PREPARED"
    journal["status"] = "ROLLED_BACK"
    _write_journal(root / "journal.json", journal)


def _verify_committed(journal: Mapping[str, object]) -> None:
    if journal.get("status") != "COMMITTED" or not isinstance(journal.get("entries"), list):
        raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_COMMIT_MISSING")
    for entry in journal["entries"]:
        if not isinstance(entry, Mapping) or entry.get("phase") != "INSTALLED":
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_COMMITTED_DRIFT")
        snap = stable_regular_snapshot(Path(str(entry["target"])), label="cover repair committed target")
        if snap is None or snap.payload != base64.b64decode(str(entry["after_bytes_b64"]), validate=True) or (snap.device, snap.inode) != (entry["installed_device"], entry["installed_inode"]):
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_COMMITTED_DRIFT")


def _write_transaction_receipt(root: Path, journal: Mapping[str, object]) -> None:
    receipt = {"schema_version": "qixi-screenshot-direct-cover-repair-receipt.v1", "status": "COMMITTED", "authority_sha256": journal["authority_sha256"], "journal_sha256": journal["journal_sha256"]}
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    path = root / "receipt.json"
    payload = _json_bytes(receipt)
    if os.path.lexists(path):
        existing = stable_regular_snapshot(path, label="cover repair receipt")
        if existing is None or existing.payload != payload:
            raise QixiScreenshotDirectCoverRepairError("COVER_REPAIR_RECEIPT_DRIFT")
    else:
        _write_private_new(path, payload)
