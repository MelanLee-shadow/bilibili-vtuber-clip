"""Pure reconciliation for an already-public native manual Talk package.

This lane is intentionally separate from the queued manual intake.  A package
which has already been published can be recorded in a missing daily-state
``picks`` row, but it cannot acquire a pre-publication review verdict or a new
publication permission from this module.
"""

from __future__ import annotations

from copy import deepcopy
import datetime as _datetime
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import manual_publication_intake as _manual
from . import publication_reconciliation as _reconciliation
from .publication_state_projection import (
    PublicationReconciliationError,
    project_publication_closure,
)


SCHEMA = "detached-published-manual-intake.v1"
ACTIVE_COLLECTIONS = (
    "picks",
    "songs",
    "pending_talk",
    "talk_backlog",
    "pending_song",
    "song_backlog",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DetachedPublicationIntakeError(PublicationReconciliationError):
    """A detached public package cannot be safely projected into daily state."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


def _canonical_sha(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _identity(row: Mapping[str, object]) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def _normal_sha(value: object) -> str:
    return str(value or "").removeprefix("sha256:").lower()


def _require_sha_entry(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise DetachedPublicationIntakeError("AUTHORITY_INCOMPLETE", f"{label} is missing")
    path = str(value.get("path") or "")
    sha = _normal_sha(value.get("sha256"))
    size = value.get("bytes")
    if not path or not _SHA256.fullmatch(sha) or type(size) is not int or size < 0:
        raise DetachedPublicationIntakeError(
            "AUTHORITY_INCOMPLETE", f"{label} is not a complete hash binding"
        )
    return {"path": path, "sha256": sha, "bytes": size}


def _interval(value: object, label: str) -> list[int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(type(item) is not int for item in value)
        or not 0 <= value[0] < value[1]
    ):
        raise DetachedPublicationIntakeError("SOURCE_BINDING_INVALID", f"{label} is invalid")
    return [int(value[0]), int(value[1])]


def _validate_verified(verified: Mapping[str, object]) -> dict[str, object]:
    if verified.get("manual_package_validated") is not True:
        raise DetachedPublicationIntakeError(
            "NON_MANUAL_PACKAGE",
            "detached intake requires the existing native manual-package validator",
        )
    if verified.get("source_provenance_validated") is not True:
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID",
            "source provenance was not validated",
        )
    if verified.get("detached_source") is not True:
        raise DetachedPublicationIntakeError(
            "NON_MANUAL_PACKAGE", "detached source marker is missing"
        )
    if verified.get("prepublication_verdict_synthesized") is not False:
        raise DetachedPublicationIntakeError(
            "PREPUBLICATION_VERDICT",
            "detached intake cannot synthesize a pre-publication verdict",
        )
    publication = verified.get("publication")
    if not isinstance(publication, Mapping) or publication.get("status") != "VERIFIED_PUBLIC":
        raise DetachedPublicationIntakeError(
            "PUBLIC_AUTHORITY_INVALID", "public authority is not VERIFIED_PUBLIC"
        )
    candidate = str(publication.get("candidate_id") or "")
    date = str(publication.get("recording_date") or "")
    bvid = str(publication.get("bvid") or "")
    if not candidate or not _DATE.fullmatch(date) or not bvid:
        raise DetachedPublicationIntakeError(
            "PUBLIC_AUTHORITY_INVALID", "public authority identity is incomplete"
        )
    for key in ("manifest", "record", "review_manifest", "provenance", "publication_authority"):
        _require_sha_entry(verified.get(key), key)
    if verified["publication_authority"] != publication.get("authority"):
        raise DetachedPublicationIntakeError(
            "PUBLIC_AUTHORITY_INVALID",
            "publication authority binding differs from its receipt",
        )
    source = verified.get("source_binding")
    checked_source = verified.get("validated_source_binding")
    if (
        not isinstance(source, Mapping)
        or not isinstance(checked_source, Mapping)
        or source != checked_source
    ):
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID",
            "source binding is missing or drifted from validated provenance",
        )
    source_path = str(source.get("source_path") or "")
    source_basename = str(source.get("source_basename") or "")
    source_sha = _normal_sha(source.get("source_media_sha256"))
    media_sha = _normal_sha(source.get("media_sha256"))
    if (
        not source_path
        or not source_basename
        or not _SHA256.fullmatch(source_sha)
        or not _SHA256.fullmatch(media_sha)
    ):
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "source media binding is incomplete"
        )
    source_interval = _interval(source.get("source_interval_ms"), "source interval")
    record_interval = _interval(source.get("record_interval_ms"), "record interval")
    final_interval = _interval(source.get("final_recut_interval_ms"), "final recut interval")
    final_path = str(source.get("final_recut_path") or "")
    if not final_path or _normal_sha(source.get("final_recut_sha256")) != media_sha:
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "final recut does not bind to media"
        )
    if source.get("media_path") != final_path:
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "record media and recut output differ"
        )
    if (
        verified.get("source_interval_ms") != source_interval
        or _normal_sha(verified.get("source_media_sha256")) != source_sha
    ):
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "top-level source binding drifted"
        )
    if not isinstance(verified.get("selection_scorecard"), (dict, type(None))):
        raise DetachedPublicationIntakeError(
            "SELECTION_BINDING_INVALID", "selection scorecard is not an object"
        )
    return {
        "publication": deepcopy(dict(publication)),
        "candidate_id": candidate,
        "recording_date": date,
        "bvid": bvid,
        "source_binding": deepcopy(dict(source)),
        "source_interval_ms": source_interval,
        "source_media_sha256": source_sha,
        "record_interval_ms": record_interval,
        "final_recut_interval_ms": final_interval,
        "selection_scorecard": deepcopy(verified.get("selection_scorecard")),
        "session_relation_authority": deepcopy(verified.get("session_relation_authority")),
    }


def verified_detached_manual_inputs(manifest_path: Path, bvid: str) -> dict[str, object]:
    """Run the existing native manual/public validators and expose bindings.

    The existing queue validator remains the authority for the package itself;
    this wrapper only adds the source/record fields needed by the detached
    projection and marks that the manual validation actually ran.
    """

    values = _manual.verified_manual_inputs(manifest_path, bvid)
    record_path = Path(str(values["record"]["path"]))
    provenance_path = Path(str(values["provenance"]["path"]))
    record = _reconciliation._load_object(record_path, "manual record")
    provenance = _reconciliation._load_object(provenance_path, "source recut provenance")
    recut = provenance.get("final_recut")
    piece = provenance.get("source_piece")
    if not isinstance(recut, Mapping) or not isinstance(piece, Mapping):
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "source provenance lacks final recut/piece"
        )
    media = Path(str(record.get("media_path") or ""))
    if not media.is_file():
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", f"record media is missing: {media}"
        )
    media_sha = _reconciliation.sha256_file(media)
    recut_sha = _normal_sha(recut.get("output_sha256"))
    artifact_sha = _normal_sha((record.get("artifact_hashes") or {}).get("video_sha256"))
    if (
        recut_sha != media_sha
        or artifact_sha != media_sha
        or str(recut.get("output_path")) != str(media)
    ):
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "record/provenance/media hashes disagree"
        )
    source_interval = _interval(values.get("source_interval_ms"), "validated source interval")
    record_interval = _interval([record.get("start_ms"), record.get("end_ms")], "record interval")
    final_interval = _interval(
        [recut.get("absolute_source_start_ms"), recut.get("absolute_source_end_ms")],
        "final recut source interval",
    )
    source_path = str(piece.get("source_path") or "")
    source_sha = _normal_sha(piece.get("source_sha256"))
    if not source_path or not _SHA256.fullmatch(source_sha):
        raise DetachedPublicationIntakeError(
            "SOURCE_BINDING_INVALID", "source piece path/hash is incomplete"
        )
    authority = _require_sha_entry(values["publication"]["authority"], "publication authority")
    source_binding = {
        "source_path": source_path,
        "source_basename": Path(source_path).name,
        "source_media_sha256": source_sha,
        "source_interval_ms": source_interval,
        "final_recut_path": str(media),
        "final_recut_sha256": media_sha,
        "final_recut_interval_ms": final_interval,
        "media_path": str(media),
        "media_sha256": media_sha,
        "record_interval_ms": record_interval,
    }
    output = deepcopy(values)
    output.update(
        {
            "publication_authority": authority,
            "source_binding": source_binding,
            "validated_source_binding": deepcopy(source_binding),
            "source_interval_ms": source_interval,
            "source_media_sha256": source_sha,
            "record_interval_ms": record_interval,
            "detached_source": True,
            "manual_package_validated": True,
            "source_provenance_validated": True,
            "prepublication_verdict_synthesized": False,
            "selection_scorecard": deepcopy(record.get("selection_scorecard"))
            if isinstance(record.get("selection_scorecard"), Mapping)
            else None,
            "session_relation_authority": deepcopy(record.get("session_relation_authority"))
            if isinstance(record.get("session_relation_authority"), Mapping)
            else None,
        }
    )
    return output


def _source_facts(row: Mapping[str, object]) -> tuple[set[str], set[str], list[list[int]]]:
    paths: set[str] = set()
    hashes: set[str] = set()
    intervals: list[list[int]] = []

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if (
                    key in {"source_path", "source_media_path", "segment_path"}
                    and isinstance(item, str)
                    and item
                ):
                    paths.add(os.path.normpath(item))
                elif key in {"source_media_sha256", "source_sha256", "source_media_hash"}:
                    sha = _normal_sha(item)
                    if _SHA256.fullmatch(sha):
                        hashes.add(sha)
                elif key == "source_interval_ms":
                    try:
                        intervals.append(_interval(item, "existing source interval"))
                    except DetachedPublicationIntakeError:
                        pass
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(row)
    return paths, hashes, intervals


def _overlap(left: list[int], right: list[int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _same_delivery(row: Mapping[str, object], source: Mapping[str, object]) -> bool:
    paths, hashes, intervals = _source_facts(row)
    target_path = os.path.normpath(str(source["source_path"]))
    target_hash = _normal_sha(source["source_media_sha256"])
    identity_match = target_path in paths or target_hash in hashes
    if not identity_match:
        return False
    target_interval = list(source["source_interval_ms"])
    return not intervals or any(_overlap(interval, target_interval) for interval in intervals)


def _row_bvids(row: Mapping[str, object]) -> set[str]:
    bvids: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"bvid", "published_bvid"} and isinstance(item, str) and item:
                    bvids.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(row)
    return bvids


def _receipt_binding(verified: Mapping[str, object]) -> dict[str, object]:
    return {
        "publication": deepcopy(verified["publication"]),
        "candidate_id": verified["candidate_id"],
        "recording_date": verified["recording_date"],
        "manifest": deepcopy(verified["manifest"]),
        "record": deepcopy(verified["record"]),
        "review_manifest": deepcopy(verified["review_manifest"]),
        "provenance": deepcopy(verified["provenance"]),
        "publication_authority": deepcopy(verified["publication_authority"]),
        "source_binding": deepcopy(verified["source_binding"]),
        "selection_scorecard": deepcopy(verified.get("selection_scorecard")),
        "session_relation_authority": deepcopy(verified.get("session_relation_authority")),
    }


def _make_receipt(
    verified: Mapping[str, object], state_sha256: str, *, created_at: str
) -> dict[str, object]:
    binding = _receipt_binding(verified)
    return {
        "schema_version": SCHEMA,
        "source_kind": "detached-native-manual-package",
        "detached_source": True,
        "already_public": True,
        "candidate_id": verified["candidate_id"],
        "recording_date": verified["recording_date"],
        "authority": {
            "manifest": deepcopy(verified["manifest"]),
            "record": deepcopy(verified["record"]),
            "review_manifest": deepcopy(verified["review_manifest"]),
            "provenance": deepcopy(verified["provenance"]),
            "publication_authority": deepcopy(verified["publication_authority"]),
        },
        "publication": deepcopy(verified["publication"]),
        "source_binding": deepcopy(verified["source_binding"]),
        "selection_scorecard": deepcopy(verified.get("selection_scorecard")),
        "session_relation_authority": deepcopy(verified.get("session_relation_authority")),
        "binding_sha256": _canonical_sha(binding),
        "state_before_sha256": state_sha256,
        "prepublication_quality_synthesized": False,
        "prepublication_verdict_synthesized": False,
        "publication_permission_granted": False,
        "created_at": created_at,
    }


def _row_matches_receipt(row: Mapping[str, object], verified: Mapping[str, object]) -> bool:
    receipt = row.get("detached_manual_publication")
    if not isinstance(receipt, Mapping):
        return False
    expected = _receipt_binding(verified)
    return (
        row.get("status") == "published"
        and row.get("bvid") == verified["publication"]["bvid"]
        and row.get("published_cid") == verified["publication"]["cid"]
        and row.get("publication_reconciliation") == verified["publication"]
        and row.get("source_path") == verified["source_binding"]["source_path"]
        and _normal_sha(row.get("source_media_sha256")) == verified["source_media_sha256"]
        and row.get("source_interval_ms") == verified["source_interval_ms"]
        and receipt.get("schema_version") == SCHEMA
        and receipt.get("detached_source") is True
        and receipt.get("prepublication_verdict_synthesized") is False
        and receipt.get("publication_permission_granted") is False
        and receipt.get("source_binding") == verified["source_binding"]
        and receipt.get("binding_sha256") == _canonical_sha(expected)
        and receipt.get("authority")
        == {
            "manifest": verified["manifest"],
            "record": verified["record"],
            "review_manifest": verified["review_manifest"],
            "provenance": verified["provenance"],
            "publication_authority": verified["publication_authority"],
        }
    )


def _published_row(
    verified: Mapping[str, object], receipt: Mapping[str, object]
) -> dict[str, object]:
    publication = verified["publication"]
    source = verified["source_binding"]
    row: dict[str, object] = {
        "candidate_id": verified["candidate_id"],
        "date": verified["recording_date"],
        "lane": "talk",
        "start_ms": verified["record_interval_ms"][0],
        "end_ms": verified["record_interval_ms"][1],
        "status": "published",
        "rc": 0,
        "bvid": publication["bvid"],
        "aid": publication["aid"],
        "published_cid": publication["cid"],
        "publication_reconciliation": deepcopy(publication),
        "detached_manual_publication": deepcopy(receipt),
        "video_sha256": verified["video_sha256"],
        "cover_path": verified["cover_path"],
        "cover_sha256": verified["cover_sha256"],
        "source_path": source["source_path"],
        "source_interval_ms": deepcopy(source["source_interval_ms"]),
        "source_media_sha256": source["source_media_sha256"],
        "summary": {
            "candidate_id": verified["candidate_id"],
            "delivery": verified["video_path"],
            "subtitle": verified["subtitle_path"],
            "title": publication["title"],
            "record": verified["record"]["path"],
        },
    }
    if verified.get("selection_scorecard") is not None:
        row["selection_scorecard"] = deepcopy(verified["selection_scorecard"])
    if verified.get("session_relation_authority") is not None:
        row["session_relation_authority"] = deepcopy(verified["session_relation_authority"])
    return row


def plan_detached_publication_intake(
    state: Mapping[str, object],
    verified: Mapping[str, object],
    *,
    state_sha256: str | None = None,
    created_at: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return a pure state projection and receipt plan; never writes state."""

    checked = _validate_verified(verified)
    if not isinstance(state, Mapping):
        raise DetachedPublicationIntakeError("STATE_INVALID", "daily state is not an object")
    state_sha256 = state_sha256 or _canonical_sha(state)
    if not _SHA256.fullmatch(str(state_sha256)):
        raise DetachedPublicationIntakeError("STATE_INVALID", "original state SHA is required")
    if state.get("date") not in (None, checked["recording_date"]):
        raise DetachedPublicationIntakeError("DATE_CONFLICT", "daily state date differs")
    found = [
        (lane, index, row)
        for lane in ACTIVE_COLLECTIONS
        for index, row in enumerate(state.get(lane) if isinstance(state.get(lane), list) else [])
        if isinstance(row, Mapping) and _identity(row) == checked["candidate_id"]
    ]
    if len(found) > 1:
        raise DetachedPublicationIntakeError(
            "DUPLICATE_CANDIDATE", "candidate occurs in multiple active rows"
        )
    if found:
        lane, _index, row = found[0]
        if lane == "picks" and _row_matches_receipt(row, {**verified, **checked}):
            return deepcopy(dict(state)), {
                "status": "ALREADY_APPLIED",
                "state_changed": False,
                "candidate_id": checked["candidate_id"],
                "receipt": deepcopy(row["detached_manual_publication"]),
            }
        raise DetachedPublicationIntakeError(
            "EXISTING_CANDIDATE_CONFLICT", "candidate row is not the same detached receipt"
        )

    target_bvid = str(checked["publication"]["bvid"])
    for lane in ACTIVE_COLLECTIONS:
        rows = state.get(lane) if isinstance(state.get(lane), list) else []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if target_bvid in _row_bvids(row):
                raise DetachedPublicationIntakeError(
                    "BVID_CONFLICT", f"BVID is already bound in {lane}"
                )
            if _same_delivery(row, checked["source_binding"]):
                raise DetachedPublicationIntakeError(
                    "SOURCE_DELIVERY_CONFLICT", f"source delivery overlaps an existing {lane} row"
                )

    now = created_at or _datetime.datetime.now(_datetime.timezone.utc).isoformat()
    receipt = _make_receipt({**verified, **checked}, str(state_sha256), created_at=now)
    published = _published_row({**verified, **checked}, receipt)
    after = deepcopy(dict(state))
    after.setdefault("picks", [])
    if not isinstance(after["picks"], list):
        raise DetachedPublicationIntakeError("STATE_INVALID", "picks is not a list")
    after["picks"].append(published)
    if "publication_closure" in state:
        after["publication_closure"] = project_publication_closure(after)
    for key in set(state) | set(after):
        if key not in {"picks", "publication_closure"} and after.get(key) != state.get(key):
            raise DetachedPublicationIntakeError(
                "STATE_PROJECTION_INVALID", f"unrelated state changed: {key}"
            )
    return after, {
        "status": "PLANNED",
        "state_changed": True,
        "candidate_id": checked["candidate_id"],
        "bvid": target_bvid,
        "receipt": deepcopy(receipt),
    }
