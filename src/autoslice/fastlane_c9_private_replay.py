"""Fail-closed local replay of C9's watched-video subtitle omission.

This is deliberately a candidate-private adapter.  It cannot call a provider,
read a runtime state tree, or create a publish manifest.  Its only output is a
small, create-only evidence packet that lets a later, separately authorised
canonical materializer consume the sealed reviewed sidecar.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


_CANDIDATE = "auto_143025_1112_1285"
_BASE = Path("assets/lidousha/fastlane_c9_private")
_RECEIPT = _BASE / f"{_CANDIDATE}.foreign-video-source-action.v1.json"
_SOURCE = _BASE / f"{_CANDIDATE}.source.speaker-final.srt"
_REVIEWED = _BASE / f"{_CANDIDATE}.reviewed.srt"
_ENVELOPE = _BASE / f"{_CANDIDATE}.root-acceptance-envelope.v1.json"
_CLASSES = frozenset({"IN_VIDEO", "HOST_LIVE", "MIXED", "UNCERTAIN"})
_DECISION_BASIS = (
    "Root independently reviewed the C9 15-frame contact sheet, retained "
    "speaker-final/cue-table/media hashes, exhaustive 66-row source/action "
    "map, and the exact 25-drop/41-freeze projection; Ivan fastlane ruling #9 "
    "permits omission only of the sealed complete IN_VIDEO source cues, while "
    "all remaining source cue bytes stay frozen."
)


class FastlaneC9PrivateReplayError(ValueError):
    """The candidate-private C9 receipt is incomplete or has drifted."""


def _sha(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read(root: Path, relative: Path) -> bytes:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise FastlaneC9PrivateReplayError("C9_PRIVATE_ASSET_UNSAFE")
    return path.read_bytes()


def _document(root: Path) -> dict[str, Any]:
    try:
        document = json.loads(_read(root, _RECEIPT).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_RECEIPT_INVALID") from exc
    if not isinstance(document, dict):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_RECEIPT_INVALID")
    declared = document.pop("self_sha256", None)
    if declared != _sha(_canonical(document)):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_RECEIPT_HASH_DRIFT")
    document["self_sha256"] = declared
    return document


def _srt_blocks(payload: bytes, *, code: str) -> list[list[str]]:
    try:
        blocks = [block.splitlines() for block in payload.decode("utf-8").strip().split("\n\n") if block]
    except UnicodeDecodeError as exc:
        raise FastlaneC9PrivateReplayError(code) from exc
    if not blocks or any(len(block) < 3 for block in blocks):
        raise FastlaneC9PrivateReplayError(code)
    try:
        indexes = [int(block[0]) for block in blocks]
    except ValueError as exc:
        raise FastlaneC9PrivateReplayError(code) from exc
    if indexes != list(range(1, len(blocks) + 1)):
        raise FastlaneC9PrivateReplayError(code)
    return blocks


def validate_c9_source_action(root: Path) -> dict[str, object]:
    """Validate all source/action rows and return a sealed private projection."""

    root = Path(root).resolve(strict=True)
    receipt = _document(root)
    if (
        receipt.get("schema_version") != "fastlane-c9-foreign-video-source-action.v1"
        or receipt.get("candidate_id") != _CANDIDATE
        or receipt.get("recording_date") != "2026-08-15"
        or receipt.get("upload") is not False
    ):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_SCOPE_INVALID")
    source = _read(root, _SOURCE)
    reviewed = _read(root, _REVIEWED)
    binding = receipt.get("source_binding")
    projection = receipt.get("exhaustive_projection")
    if not isinstance(binding, Mapping) or not isinstance(projection, Mapping):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_BINDING_INVALID")
    if binding.get("speaker_final_srt_sha256") != _sha(source) or projection.get("reviewed_srt_sha256") != _sha(reviewed):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_SRT_HASH_DRIFT")
    source_blocks = _srt_blocks(source, code="C9_SOURCE_SRT_INVALID")
    reviewed_blocks = _srt_blocks(reviewed, code="C9_REVIEWED_SRT_INVALID")
    actions = receipt.get("cue_actions")
    if not isinstance(actions, list) or len(actions) != len(source_blocks):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTIONS_NOT_EXHAUSTIVE")
    by_cue: dict[int, Mapping[str, object]] = {}
    for row in actions:
        if not isinstance(row, Mapping) or set(row) != {"cue", "source_classification", "action", "source_pointer"}:
            raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_ROW_INVALID")
        cue, classification, action, pointer = row.get("cue"), row.get("source_classification"), row.get("action"), row.get("source_pointer")
        if not isinstance(cue, int) or cue in by_cue or classification not in _CLASSES or action not in {"DROP", "RETAIN_FROZEN"} or not isinstance(pointer, str) or not pointer:
            raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_ROW_INVALID")
        if (classification == "IN_VIDEO") != (action == "DROP"):
            raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_DROP_SCOPE_INVALID")
        by_cue[cue] = row
    if sorted(by_cue) != list(range(1, len(source_blocks) + 1)):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTIONS_NOT_EXHAUSTIVE")
    dropped = [cue for cue, row in by_cue.items() if row["action"] == "DROP"]
    frozen = [cue for cue, row in by_cue.items() if row["action"] == "RETAIN_FROZEN"]
    if receipt.get("complete_in_video_source_cues") != dropped or receipt.get("frozen_source_cues") != frozen:
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_SET_DRIFT")
    retained = [block[1:] for block in source_blocks if int(block[0]) not in set(dropped)]
    if [block[1:] for block in reviewed_blocks] != retained:
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_PROJECTION_DRIFT")
    if projection.get("source_cue_count") != len(source_blocks) or projection.get("reviewed_cue_count") != len(reviewed_blocks):
        raise FastlaneC9PrivateReplayError("C9_SOURCE_ACTION_COUNT_DRIFT")
    return {
        "schema_version": "fastlane-c9-private-replay.v1",
        "candidate_id": _CANDIDATE,
        "upload_allowed": False,
        "receipt_sha256": _sha(_read(root, _RECEIPT)),
        "source_srt_sha256": _sha(source),
        "reviewed_srt_sha256": _sha(reviewed),
        "dropped_source_cues": dropped,
        "retained_source_cues": frozen,
    }


def validate_c9_root_acceptance_envelope(root: Path) -> dict[str, object]:
    """Validate the non-authorizing root-review proposal for this exact C9 set.

    Root has accepted the named source/action correction for candidate-private
    replay only.  A public/package authority must never be inferred from it.
    """

    root = Path(root).resolve(strict=True)
    projection = validate_c9_source_action(root)
    try:
        envelope = json.loads(_read(root, _ENVELOPE).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FastlaneC9PrivateReplayError("C9_ACCEPTANCE_ENVELOPE_INVALID") from exc
    if not isinstance(envelope, dict):
        raise FastlaneC9PrivateReplayError("C9_ACCEPTANCE_ENVELOPE_INVALID")
    declared = envelope.pop("self_sha256", None)
    if declared != _sha(_canonical(envelope)):
        raise FastlaneC9PrivateReplayError("C9_ACCEPTANCE_ENVELOPE_HASH_DRIFT")
    envelope["self_sha256"] = declared
    required_keys = {
        "schema_version", "candidate_id", "recording_date", "reviewed_by",
        "reviewed_at", "accepted", "accepted_for_private_replay",
        "decision_basis", "source_action_receipt", "source_srt", "reviewed_srt",
        "activation", "self_sha256",
    }
    if (
        set(envelope) != required_keys
        or envelope.get("schema_version") != "fastlane-c9-root-acceptance-envelope.v1"
        or envelope.get("candidate_id") != _CANDIDATE
        or envelope.get("recording_date") != "2026-08-15"
        or envelope.get("reviewed_by") != "Codex root"
        or envelope.get("reviewed_at") != "2026-08-24T23:40:05Z"
        or envelope.get("accepted") is not True
        or envelope.get("accepted_for_private_replay") is not True
        or envelope.get("decision_basis") != _DECISION_BASIS
    ):
        raise FastlaneC9PrivateReplayError("C9_ACCEPTANCE_ENVELOPE_SCOPE_INVALID")
    expected = {
        "source_action_receipt": (_RECEIPT.as_posix(), projection["receipt_sha256"]),
        "source_srt": (_SOURCE.as_posix(), projection["source_srt_sha256"]),
        "reviewed_srt": (_REVIEWED.as_posix(), projection["reviewed_srt_sha256"]),
    }
    for key, (path, digest) in expected.items():
        binding = envelope.get(key)
        if not isinstance(binding, Mapping) or binding != {"repo_path": path, "sha256": digest}:
            raise FastlaneC9PrivateReplayError("C9_ACCEPTANCE_ENVELOPE_BINDING_DRIFT")
    activation = envelope.get("activation")
    expected_activation = {
        "kind": "ROOT_ACCEPTED_PRIVATE_REPLAY_ONLY",
        "private_replay_allowed": True,
        "canonical_delivery_allowed": False,
        "state_write_allowed": False,
        "provider_allowed": False,
        "ssh_allowed": False,
        "deploy_allowed": False,
        "upload_allowed": False,
    }
    if (
        not isinstance(activation, Mapping)
        or set(activation) != set(expected_activation)
        or dict(activation) != expected_activation
    ):
        raise FastlaneC9PrivateReplayError("C9_ACCEPTANCE_ENVELOPE_ACTIVATION_INVALID")
    return {
        **projection,
        "acceptance_envelope_sha256": _sha(_read(root, _ENVELOPE)),
        "reviewed_at": envelope["reviewed_at"],
        "accepted": True,
        "accepted_for_private_replay": True,
    }


def materialize_c9_private_replay(*, root: Path, output_dir: Path) -> dict[str, object]:
    """Create one create-only local evidence packet; never a delivery package."""

    projection = validate_c9_root_acceptance_envelope(root)
    output_dir = Path(output_dir).absolute()
    if output_dir.exists() or output_dir.is_symlink() or output_dir.name != _CANDIDATE:
        raise FastlaneC9PrivateReplayError("C9_PRIVATE_REPLAY_OUTPUT_NOT_CREATE_ONLY")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.parent.is_symlink():
        raise FastlaneC9PrivateReplayError("C9_PRIVATE_REPLAY_OUTPUT_UNSAFE")
    try:
        os.mkdir(output_dir)
        for name, payload in (
            ("reviewed.srt", _read(Path(root), _REVIEWED)),
            ("source.speaker-final.srt", _read(Path(root), _SOURCE)),
            ("source-action.json", _read(Path(root), _RECEIPT)),
            ("root-acceptance-envelope.json", _read(Path(root), _ENVELOPE)),
        ):
            with (output_dir / name).open("xb") as handle:
                handle.write(payload)
        packet = dict(projection)
        packet["artifacts"] = {
            name: _sha((output_dir / name).read_bytes())
            for name in (
                "reviewed.srt", "source.speaker-final.srt", "source-action.json",
                "root-acceptance-envelope.json",
            )
        }
        packet["packet_sha256"] = _sha(_canonical(packet))
        with (output_dir / "private-replay.json").open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    except FileExistsError as exc:
        raise FastlaneC9PrivateReplayError("C9_PRIVATE_REPLAY_OUTPUT_NOT_CREATE_ONLY") from exc
    return packet
