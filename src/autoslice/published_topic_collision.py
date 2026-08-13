"""Human-bound review holds for candidates that may repeat a published topic.

This is deliberately *not* a fuzzy duplicate detector.  The current selector's
``event_key`` is local to one semantic-recall call and selection-metric v2 only
consumes an opaque caller-provided ``topic_fingerprint``; neither is a safe
cross-publication identity.  A candidate is therefore parked only when the
active repository contains a committed/deployed, candidate-scoped authority
which says that the exact candidate/published pair requires human review.

The authority does not lower a score, delete a candidate, or declare the two
clips duplicates.  It binds the current hooks, scorecards, scene/date, the
committed publication-registry row, public title and BVID.  Any drift leaves
the candidate parked with a stale-authority reason rather than silently
returning it to production.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from src.autoslice.publication_registry import (
    DEFAULT_REGISTRY_PATH,
    load_publication_registry,
)
from src.autoslice.repository_asset_authority import (
    RepositoryAssetAuthorityError,
    repository_authority_expects_asset,
    require_repository_asset_authority,
)


AUTHORITY_SCHEMA = "published-topic-collision-authority.v1"
AUTHORITY_KIND = "HUMAN_ASSERTED_REVIEW_REQUIRED"
PAIR_FINGERPRINT_KIND = "PAIR_IDENTITY_ONLY_NOT_DUPLICATE_VERDICT"
REVIEW_STATE_SCHEMA = "published-topic-dedup-review-state.v1"
REVIEW_STATUS = "HUMAN_TOPIC_DEDUP_REVIEW"
STALE_REVIEW_STATUS = "HUMAN_TOPIC_DEDUP_REVIEW_AUTHORITY_STALE"
REVIEW_STATE_FIELD = "published_topic_dedup_review"
AUTHORITY_DIRECTORY = Path("assets/lidousha/published_topic_collision_authorities")
AUTHORITY_SUFFIX = ".published-topic-collision-authority.v1.json"
RESOLUTION_SCHEMA = "published-topic-dedup-resolution.v1"
RESOLUTION_DECISION = "RELEASE_FOR_PRODUCTION"
RESOLUTION_DIRECTORY = Path("assets/lidousha/published_topic_dedup_resolutions")
RESOLUTION_SUFFIX = ".published-topic-dedup-resolution.v1.json"
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]

_CANDIDATE_ID_RE = re.compile(r"auto_[0-9]+_[0-9]+_[0-9]+\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCENE_KINDS = frozenset({"event", "talk", "game"})


class PublishedTopicCollisionError(ValueError):
    """The review authority or one of its current bindings is invalid."""


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def authority_relative_path(candidate_id: str) -> Path:
    if _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None:
        raise PublishedTopicCollisionError("candidate_id is invalid")
    return AUTHORITY_DIRECTORY / f"{candidate_id}{AUTHORITY_SUFFIX}"


def resolution_relative_path(candidate_id: str) -> Path:
    """Return the one candidate-scoped human resolution path.

    The original collision authority is deliberately retained.  A resolution
    is an additional, sealed statement that may release production only after
    the original pair has been revalidated against current state.
    """

    if _CANDIDATE_ID_RE.fullmatch(str(candidate_id or "")) is None:
        raise PublishedTopicCollisionError("candidate_id is invalid")
    return RESOLUTION_DIRECTORY / f"{candidate_id}{RESOLUTION_SUFFIX}"


def _raw_sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _candidate_id(row: Mapping[str, object]) -> str:
    return str(row.get("candidate_id") or row.get("cid") or "")


def _scene_identity(row: Mapping[str, object]) -> tuple[str, str]:
    context = row.get("segment_scene_context")
    if not isinstance(context, Mapping):
        raise PublishedTopicCollisionError("candidate scene context is missing")
    recording_date = str(context.get("recording_date") or "")
    scene_kind = str(context.get("scene_kind") or "")
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", recording_date) is None:
        raise PublishedTopicCollisionError("candidate recording date is invalid")
    if scene_kind not in _SCENE_KINDS:
        raise PublishedTopicCollisionError("candidate scene kind is invalid")
    return recording_date, scene_kind


def _require_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublishedTopicCollisionError(f"{label} is missing")
    return value


def _require_sha(value: object, *, label: str) -> str:
    digest = str(value or "")
    if _SHA256_RE.fullmatch(digest) is None:
        raise PublishedTopicCollisionError(f"{label} is not a SHA-256 digest")
    return digest


def _require_bound_candidate(
    binding: object,
    row: Mapping[str, object],
    *,
    label: str,
) -> dict[str, object]:
    if not isinstance(binding, Mapping):
        raise PublishedTopicCollisionError(f"{label} binding is invalid")
    candidate_id = _candidate_id(row)
    recording_date, scene_kind = _scene_identity(row)
    hook = _require_text(row.get("hook"), label=f"{label} current hook")
    scorecard = row.get("selection_scorecard")
    if not isinstance(scorecard, Mapping):
        raise PublishedTopicCollisionError(f"{label} current scorecard is missing")
    expected = {
        "candidate_id": candidate_id,
        "recording_date": recording_date,
        "scene_kind": scene_kind,
        "hook": hook,
        "hook_sha256": canonical_sha256(hook),
        "selection_scorecard_sha256": canonical_sha256(scorecard),
    }
    for key, value in expected.items():
        if binding.get(key) != value:
            raise PublishedTopicCollisionError(f"{label} {key} binding drifted")
    return expected


def _registry_row(
    registry: Mapping[str, object], candidate_id: str, recording_date: str
) -> Mapping[str, object]:
    entries = registry.get("entries")
    if not isinstance(entries, list):
        raise PublishedTopicCollisionError("publication registry entries are invalid")
    matches = [
        row
        for row in entries
        if isinstance(row, Mapping)
        and str(row.get("candidate_id") or "") == candidate_id
        and str(row.get("recording_date") or "") == recording_date
    ]
    if len(matches) != 1:
        raise PublishedTopicCollisionError(
            "published candidate has no unique publication registry row"
        )
    row = matches[0]
    if row.get("status") != "published" or not str(row.get("bvid") or ""):
        raise PublishedTopicCollisionError("review target is not registered as published")
    return row


def validate_authority(
    value: object,
    *,
    candidate_row: Mapping[str, object],
    published_row: Mapping[str, object],
    publication_registry: Mapping[str, object],
) -> dict[str, object]:
    """Validate one exact pair against current state and registry bytes."""

    if not isinstance(value, Mapping):
        raise PublishedTopicCollisionError("authority is not an object")
    authority = dict(value)
    declared_sha = authority.pop("authority_sha256", None)
    if (
        authority.get("schema_version") != AUTHORITY_SCHEMA
        or authority.get("authority_kind") != AUTHORITY_KIND
        or authority.get("decision") != REVIEW_STATUS
        or authority.get("suppression_authorized") is not False
        or authority.get("score_mutation_authorized") is not False
        or authority.get("upload_authorized") is not False
        or canonical_sha256(authority) != declared_sha
    ):
        raise PublishedTopicCollisionError("authority envelope is invalid")
    _require_text(authority.get("authority_quote"), label="authority quote")
    _require_text(authority.get("asserted_by"), label="asserted_by")

    candidate = _require_bound_candidate(
        authority.get("candidate"), candidate_row, label="candidate"
    )
    published = _require_bound_candidate(
        authority.get("published"), published_row, label="published candidate"
    )
    if candidate["candidate_id"] == published["candidate_id"]:
        raise PublishedTopicCollisionError("candidate cannot review against itself")
    if candidate["recording_date"] != published["recording_date"]:
        raise PublishedTopicCollisionError("candidate and published date differ")
    if candidate["scene_kind"] != published["scene_kind"]:
        raise PublishedTopicCollisionError("candidate and published scene differ")
    if published_row.get("status") != "published":
        raise PublishedTopicCollisionError("published state row is not published")

    registry_row = _registry_row(
        publication_registry,
        str(published["candidate_id"]),
        str(published["recording_date"]),
    )
    published_binding = authority.get("published")
    assert isinstance(published_binding, Mapping)
    if (
        published_binding.get("registry_row_sha256") != canonical_sha256(registry_row)
        or published_binding.get("registry_status") != "published"
        or published_binding.get("bvid") != registry_row.get("bvid")
    ):
        raise PublishedTopicCollisionError("publication registry binding drifted")
    bvid = _require_text(registry_row.get("bvid"), label="published BVID")
    public_title = _require_text(
        published_binding.get("public_title"), label="published public title"
    )
    if published_binding.get("public_title_sha256") != canonical_sha256(public_title):
        raise PublishedTopicCollisionError("published public title hash is invalid")
    state_bvid = str(published_row.get("bvid") or "")
    reconciliation = published_row.get("publication_reconciliation")
    reconciliation_bvid = (
        str(reconciliation.get("bvid") or "") if isinstance(reconciliation, Mapping) else ""
    )
    if bvid not in {state_bvid, reconciliation_bvid}:
        raise PublishedTopicCollisionError("published state BVID binding drifted")

    pair_fingerprint = {
        "kind": PAIR_FINGERPRINT_KIND,
        "authority_quote": authority["authority_quote"],
        "candidate": candidate,
        "published": {
            **published,
            "registry_row_sha256": canonical_sha256(registry_row),
            "bvid": bvid,
            "public_title": public_title,
            "public_title_sha256": canonical_sha256(public_title),
        },
    }
    if authority.get("topic_review_fingerprint_sha256") != canonical_sha256(pair_fingerprint):
        raise PublishedTopicCollisionError("topic review fingerprint drifted")
    return {
        **authority,
        "authority_sha256": declared_sha,
        "validated_pair": pair_fingerprint,
    }


def validate_resolution(
    value: object,
    *,
    candidate_row: Mapping[str, object],
    published_row: Mapping[str, object],
    validated_authority: Mapping[str, object],
    authority_path: Path,
    authority_bytes: bytes,
) -> dict[str, object]:
    """Validate a human production release without widening upload authority.

    ``validated_authority`` is the result of :func:`validate_authority`, so it
    already proves the live candidate, the published state row, registry row,
    BVID and public title.  The resolution deliberately repeats those exact
    bindings and pins both the original raw bytes and its self-seal; a stale
    pair therefore cannot be released by merely retaining an old resolution.
    """

    if not isinstance(value, Mapping):
        raise PublishedTopicCollisionError("resolution is not an object")
    resolution = dict(value)
    declared_sha = resolution.pop("resolution_sha256", None)
    expected_keys = {
        "schema_version",
        "decision",
        "asserted_by",
        "asserted_at",
        "authority_quote",
        "candidate",
        "published",
        "original_authority_relative_path",
        "original_authority_raw_sha256",
        "original_authority_sha256",
        "upload_authorized",
    }
    if (
        set(resolution) != expected_keys
        or resolution.get("schema_version") != RESOLUTION_SCHEMA
        or resolution.get("decision") != RESOLUTION_DECISION
        or resolution.get("upload_authorized") is not False
        or canonical_sha256(resolution) != declared_sha
    ):
        raise PublishedTopicCollisionError("resolution envelope is invalid")
    _require_text(resolution.get("asserted_by"), label="resolution asserted_by")
    _require_text(resolution.get("asserted_at"), label="resolution asserted_at")
    _require_text(resolution.get("authority_quote"), label="resolution authority quote")

    candidate = _require_bound_candidate(
        resolution.get("candidate"), candidate_row, label="resolution candidate"
    )
    published = _require_bound_candidate(
        resolution.get("published"), published_row, label="resolution published candidate"
    )
    original_candidate = validated_authority.get("candidate")
    original_published = validated_authority.get("published")
    if not isinstance(original_candidate, Mapping) or not isinstance(original_published, Mapping):
        raise PublishedTopicCollisionError("validated original authority is incomplete")
    if dict(resolution["candidate"]) != dict(original_candidate) or candidate != dict(original_candidate):
        raise PublishedTopicCollisionError("resolution candidate binding drifted")
    # _require_bound_candidate intentionally checks the moving source fields;
    # the original authority carries the additional registry/public fields.
    required_published = {
        key: original_published.get(key)
        for key in (
            "candidate_id",
            "recording_date",
            "scene_kind",
            "hook",
            "hook_sha256",
            "selection_scorecard_sha256",
            "registry_row_sha256",
            "registry_status",
            "bvid",
            "public_title",
            "public_title_sha256",
        )
    }
    if dict(resolution["published"]) != required_published or any(
        published.get(key) != required_published.get(key)
        for key in ("candidate_id", "recording_date", "scene_kind", "hook", "hook_sha256", "selection_scorecard_sha256")
    ):
        raise PublishedTopicCollisionError("resolution published binding drifted")
    if resolution.get("original_authority_relative_path") != authority_path.as_posix():
        raise PublishedTopicCollisionError("resolution original authority path drifted")
    if resolution.get("original_authority_raw_sha256") != _raw_sha256(authority_bytes):
        raise PublishedTopicCollisionError("resolution original authority bytes drifted")
    if resolution.get("original_authority_sha256") != validated_authority.get("authority_sha256"):
        raise PublishedTopicCollisionError("resolution original authority self-seal drifted")
    return {**resolution, "resolution_sha256": declared_sha}


def _current_rows_by_id(state: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    rows: dict[str, Mapping[str, object]] = {}
    # A freshly requeued row is newer than the historical failed pick.  Read
    # picks first, then queues so a changed hook/scorecard is checked rather
    # than shadowed by its old failure record.
    for field in ("picks", "talk_backlog", "pending_talk"):
        values = state.get(field)
        if not isinstance(values, list):
            continue
        for row in values:
            if isinstance(row, Mapping) and _candidate_id(row):
                rows[_candidate_id(row)] = row
    return rows


def _held_candidates(state: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    result: dict[str, Mapping[str, object]] = {}
    if not isinstance(holds, list):
        return result
    for hold in holds:
        candidate = hold.get("candidate") if isinstance(hold, Mapping) else None
        if isinstance(candidate, Mapping) and _candidate_id(candidate):
            result[_candidate_id(candidate)] = candidate
    return result


def _prior_hold_records(state: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    current = state.get(REVIEW_STATE_FIELD)
    holds = current.get("holds") if isinstance(current, Mapping) else None
    result: dict[str, Mapping[str, object]] = {}
    if not isinstance(holds, list):
        return result
    for hold in holds:
        if isinstance(hold, Mapping) and _candidate_id(hold):
            result[_candidate_id(hold)] = hold
    return result


def _remove_from_queue(state: dict, candidate_id: str) -> None:
    for field in ("pending_talk", "talk_backlog"):
        values = state.get(field)
        if isinstance(values, list):
            state[field] = [
                row
                for row in values
                if not (isinstance(row, Mapping) and _candidate_id(row) == candidate_id)
            ]


def _queue_origin(state: Mapping[str, object], candidate_id: str) -> str | None:
    """Return the unique queue currently owning a candidate, if any."""

    origins = [
        field
        for field in ("pending_talk", "talk_backlog")
        if any(
            isinstance(row, Mapping) and _candidate_id(row) == candidate_id
            for row in (state.get(field) or [])
        )
    ]
    return origins[0] if len(origins) == 1 else None


def _restore_resolved_hold(
    state: dict,
    *,
    candidate_id: str,
    prior_hold: Mapping[str, object] | None,
) -> None:
    """Put a validly released sticky hold back exactly where it was parked."""

    if prior_hold is None:
        return
    for field in ("pending_talk", "talk_backlog"):
        rows = state.get(field)
        if isinstance(rows, list) and any(
            isinstance(row, Mapping) and _candidate_id(row) == candidate_id for row in rows
        ):
            # Delivery recovery may already have restored an older sticky hold
            # whose pre-v1 receipt predates ``queue_origin``.  The current
            # queue row is stronger evidence than that legacy omission: keep
            # it in place, but never infer an origin when no fresh row exists.
            return
    origin = prior_hold.get("queue_origin")
    candidate = prior_hold.get("candidate")
    if origin not in {"pending_talk", "talk_backlog"} or not isinstance(candidate, Mapping):
        raise PublishedTopicCollisionError("resolved sticky hold has no valid queue origin")
    state.setdefault(origin, []).append(dict(candidate))


def hold_published_topic_collision_reviews(
    state: dict,
    *,
    repo_root: Path = DEFAULT_REPO_ROOT,
    publication_registry: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Park every candidate with an active pair-review authority.

    Only an asset declared by ``HEAD`` or the commit-bound deployed manifest
    can create a new hold.  Once created, a hold is sticky: missing, malformed,
    or drifted authority bytes become ``...AUTHORITY_STALE`` and never silently
    release the candidate.
    """

    rows = _current_rows_by_id(state)
    prior_held = _held_candidates(state)
    prior_hold_records = _prior_hold_records(state)
    rows = {**prior_held, **rows}

    active_ids: set[str] = set(prior_held)
    for candidate_id in rows:
        try:
            relative = authority_relative_path(candidate_id)
            if repository_authority_expects_asset(repo_root=repo_root, relative_path=relative):
                active_ids.add(candidate_id)
        except (PublishedTopicCollisionError, RepositoryAssetAuthorityError):
            if candidate_id in prior_held:
                active_ids.add(candidate_id)

    if not active_ids:
        state[REVIEW_STATE_FIELD] = {
            "schema_version": REVIEW_STATE_SCHEMA,
            "holds": [],
        }
        return []

    registry = publication_registry
    registry_error: Exception | None = None
    if registry is None:
        # Passing the committed path intentionally excludes a runtime overlay:
        # the pair authority binds the exact repository row, not a mutable
        # post-publication cache projection.  An unreadable registry is
        # converted into a sticky stale hold for the scoped candidate rather
        # than crashing unrelated selection work.
        try:
            registry = load_publication_registry(DEFAULT_REGISTRY_PATH)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            registry = {"entries": []}
            registry_error = exc

    holds: list[dict[str, object]] = []
    for candidate_id in sorted(active_ids):
        candidate = rows.get(candidate_id)
        if candidate is None:
            # The prior row is retained inside the hold, so this is reachable
            # only for a corrupt pre-v1 state.  Keep a typed stale tombstone.
            holds.append(
                {
                    "candidate_id": candidate_id,
                    "disposition": STALE_REVIEW_STATUS,
                    "reason_code": "CANDIDATE_ROW_MISSING",
                    "candidate": {},
                }
            )
            continue
        relative = authority_relative_path(candidate_id)
        path = repo_root / relative
        disposition = REVIEW_STATUS
        reason_code = "HUMAN_ASSERTED_PUBLISHED_TOPIC_REVIEW_REQUIRED"
        evidence: dict[str, object] = {}
        try:
            observed = path.read_bytes()
            repository_binding = require_repository_asset_authority(
                repo_root=repo_root,
                relative_path=relative,
                observed_bytes=observed,
            )
            raw = json.loads(observed.decode("utf-8"))
            if not isinstance(raw, Mapping):
                raise PublishedTopicCollisionError("authority is not an object")
            published_binding = raw.get("published")
            published_id = (
                str(published_binding.get("candidate_id") or "")
                if isinstance(published_binding, Mapping)
                else ""
            )
            published = rows.get(published_id)
            if published is None:
                raise PublishedTopicCollisionError("published candidate state row is missing")
            if registry_error is not None:
                raise PublishedTopicCollisionError(
                    f"publication registry is unreadable: {registry_error}"
                )
            validated = validate_authority(
                raw,
                candidate_row=candidate,
                published_row=published,
                publication_registry=registry,
            )
            resolution_relative = resolution_relative_path(candidate_id)
            resolution_path = repo_root / resolution_relative
            if resolution_path.exists():
                resolution_bytes = resolution_path.read_bytes()
                require_repository_asset_authority(
                    repo_root=repo_root,
                    relative_path=resolution_relative,
                    observed_bytes=resolution_bytes,
                )
                resolution = json.loads(resolution_bytes.decode("utf-8"))
                validate_resolution(
                    resolution,
                    candidate_row=candidate,
                    published_row=published,
                    validated_authority=validated,
                    authority_path=relative,
                    authority_bytes=observed,
                )
                _restore_resolved_hold(
                    state,
                    candidate_id=candidate_id,
                    prior_hold=prior_hold_records.get(candidate_id),
                )
                # This statement only releases selection/production.  Its
                # explicit upload_authorized=false is deliberately not
                # translated into a publication or manifest permission.
                continue
            evidence = {
                "authority": {
                    "relative_path": repository_binding.relative_path,
                    "file_sha256": repository_binding.file_sha256,
                    "commit": repository_binding.commit,
                    "mode": repository_binding.mode,
                    "authority_sha256": validated["authority_sha256"],
                },
                "authority_quote": validated["authority_quote"],
                "topic_review_fingerprint_sha256": validated["topic_review_fingerprint_sha256"],
                "published_candidate_id": published_id,
                "published_bvid": published_binding.get("bvid"),
                "published_public_title": published_binding.get("public_title"),
                "candidate_hook_sha256": validated["candidate"]["hook_sha256"],
                "candidate_scorecard_sha256": validated["candidate"]["selection_scorecard_sha256"],
            }
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            PublishedTopicCollisionError,
            RepositoryAssetAuthorityError,
        ) as exc:
            disposition = STALE_REVIEW_STATUS
            reason_code = "PUBLISHED_TOPIC_REVIEW_AUTHORITY_STALE"
            evidence = {"error": f"{type(exc).__name__}: {exc}"}

        origin = _queue_origin(state, candidate_id)
        _remove_from_queue(state, candidate_id)
        candidate_copy = dict(candidate)
        holds.append(
            {
                "candidate_id": candidate_id,
                "disposition": disposition,
                "reason_code": reason_code,
                "score_mutated": False,
                "suppression_authorized": False,
                "upload_authorized": False,
                "queue_origin": origin,
                "candidate": candidate_copy,
                "evidence": evidence,
            }
        )

    state[REVIEW_STATE_FIELD] = {
        "schema_version": REVIEW_STATE_SCHEMA,
        "holds": holds,
    }
    return holds
