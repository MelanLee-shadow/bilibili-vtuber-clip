from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from src.autoslice.published_topic_collision import (
    AUTHORITY_KIND,
    AUTHORITY_SCHEMA,
    PAIR_FINGERPRINT_KIND,
    REVIEW_STATE_FIELD,
    REVIEW_STATUS,
    RESOLUTION_DECISION,
    RESOLUTION_SCHEMA,
    STALE_REVIEW_STATUS,
    authority_relative_path,
    canonical_sha256,
    hold_published_topic_collision_reviews,
    resolution_relative_path,
)


CANDIDATE = "auto_213135_806_1068"
PUBLISHED = "auto_213135_469_710"
DISTINCT = "auto_210131_1576_1802"
DATE = "2026-08-08"
BV = "BV18Gu16NEcX"
PUBLIC_TITLE = (
    "【李豆沙】第一次3D Live紧张到手抖，想起五年前看歌姬时的心情，"
    "自己好像正走在实现五年前舞台愿望的路上"
)


def _card(score: float) -> dict:
    return {
        "schema_version": "lidousha-selection-scorecard.v1",
        "status": "VALID",
        "tier": 2,
        "effective_score": score,
        "dimensions": {
            "lidousha_centrality": 4,
            "stance_intensity": 4,
            "audience_salience": 4,
            "relationship_interaction": 3,
            "persona_reversal": 2,
            "comedic_payoff": 4,
            "self_contained": 4,
        },
    }


def _row(
    candidate_id: str,
    hook: str,
    *,
    score: float,
    scene: str = "event",
    status: str | None = None,
    bvid: str | None = None,
) -> dict:
    row = {
        "candidate_id": candidate_id,
        "hook": hook,
        "selection_scorecard": _card(score),
        "segment_scene_context": {
            "recording_date": DATE,
            "scene_kind": scene,
        },
        "segment_path": f"/recordings/{candidate_id}.mp4",
        "session_id": "live-20260808Tunknown",
        "start_ms": 1000,
        "end_ms": 2000,
        "confidence": 0.94,
    }
    if status is not None:
        row["status"] = status
    if bvid is not None:
        row["bvid"] = bvid
        row["publication_reconciliation"] = {"bvid": bvid}
    return row


@pytest.fixture
def rows() -> tuple[dict, dict, dict]:
    candidate = _row(
        CANDIDATE,
        "李豆沙坦白自己羡慕能全职做主播的人，也曾为没时间没钱而自责，"
        "但回头才发现她和观众已经相伴五年、一起走上了理想中的3D舞台。",
        score=89.25,
    )
    published = _row(
        PUBLISHED,
        "第一次3D Live前她紧张得手抖，却在赴约的高铁上突然意识到："
        "自己正走在实现五年前舞台愿望的路上。",
        score=81.25,
        status="published",
        bvid=BV,
    )
    distinct = _row(
        DISTINCT,
        "小李紧张复盘生日舞台，揭开彩排照夹字母V的伏笔、搭档半小时学舞和此前直播埋下的选曲预告。",
        score=81.75,
    )
    return candidate, published, distinct


def _registry_row() -> dict:
    return {
        "candidate_id": PUBLISHED,
        "recording_date": DATE,
        "status": "published",
        "bvid": BV,
        "note": "verified public row",
    }


def _registry() -> dict:
    return {
        "schema_version": "publication-registry.v1",
        "entries": [_registry_row()],
    }


def _candidate_binding(row: dict) -> dict:
    context = row["segment_scene_context"]
    return {
        "candidate_id": row["candidate_id"],
        "recording_date": context["recording_date"],
        "scene_kind": context["scene_kind"],
        "hook": row["hook"],
        "hook_sha256": canonical_sha256(row["hook"]),
        "selection_scorecard_sha256": canonical_sha256(row["selection_scorecard"]),
    }


def _authority(candidate: dict, published: dict, registry: dict) -> dict:
    quote = "event 你说的这个5年后这个切片不是已经上传了吗？"
    candidate_binding = _candidate_binding(candidate)
    published_binding = {
        **_candidate_binding(published),
        "registry_row_sha256": canonical_sha256(registry["entries"][0]),
        "registry_status": "published",
        "bvid": BV,
        "public_title": PUBLIC_TITLE,
        "public_title_sha256": canonical_sha256(PUBLIC_TITLE),
    }
    pair = {
        "kind": PAIR_FINGERPRINT_KIND,
        "authority_quote": quote,
        "candidate": candidate_binding,
        "published": {
            key: published_binding[key]
            for key in (
                "candidate_id",
                "recording_date",
                "scene_kind",
                "hook",
                "hook_sha256",
                "selection_scorecard_sha256",
                "registry_row_sha256",
                "bvid",
                "public_title",
                "public_title_sha256",
            )
        },
    }
    authority = {
        "schema_version": AUTHORITY_SCHEMA,
        "authority_kind": AUTHORITY_KIND,
        "decision": REVIEW_STATUS,
        "asserted_by": "Ivan",
        "asserted_at": "2026-08-13",
        "authority_quote": quote,
        "interpretation": "review required; not a duplicate verdict",
        "candidate": candidate_binding,
        "published": published_binding,
        "topic_review_fingerprint_sha256": canonical_sha256(pair),
        "suppression_authorized": False,
        "score_mutation_authorized": False,
        "upload_authorized": False,
    }
    authority["authority_sha256"] = canonical_sha256(authority)
    return authority


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _resolution(candidate: dict, published: dict, authority: dict, registry: dict) -> dict:
    resolution = {
        "schema_version": RESOLUTION_SCHEMA,
        "decision": RESOLUTION_DECISION,
        "asserted_by": "Ivan",
        "asserted_at": "2026-08-13T14:00:00Z",
        "authority_quote": "806，1576内容没问题可以发。",
        "candidate": _candidate_binding(candidate),
        "published": {
            **_candidate_binding(published),
            "registry_row_sha256": canonical_sha256(registry["entries"][0]),
            "registry_status": "published",
            "bvid": BV,
            "public_title": PUBLIC_TITLE,
            "public_title_sha256": canonical_sha256(PUBLIC_TITLE),
        },
        "original_authority_relative_path": authority_relative_path(CANDIDATE).as_posix(),
        "original_authority_raw_sha256": "",  # set after serializing authority bytes
        "original_authority_sha256": authority["authority_sha256"],
        "upload_authorized": False,
    }
    authority_bytes = (json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    resolution["original_authority_raw_sha256"] = "sha256:" + hashlib.sha256(authority_bytes).hexdigest()
    resolution["resolution_sha256"] = canonical_sha256(resolution)
    return resolution


def _sealed_repo(
    tmp_path: Path, candidate_id: str, authority: dict, resolution: dict | None = None
) -> Path:
    root = tmp_path / "repo"
    path = root / authority_relative_path(candidate_id)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if resolution is not None:
        resolution_path = root / resolution_relative_path(candidate_id)
        resolution_path.parent.mkdir(parents=True)
        resolution_path.write_text(
            json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Autoslice Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal authority")
    return root


def test_469_vs_806_is_held_without_score_or_suppression_and_1576_passes(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    original_scorecard = deepcopy(candidate["selection_scorecard"])
    state = {
        "picks": [published],
        "pending_talk": [candidate, distinct],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]
    assert [row["candidate_id"] for row in holds] == [CANDIDATE]
    hold = holds[0]
    assert hold["disposition"] == REVIEW_STATUS
    assert hold["candidate"]["selection_scorecard"] == original_scorecard
    assert hold["score_mutated"] is False
    assert hold["suppression_authorized"] is False
    assert hold["upload_authorized"] is False
    assert hold["evidence"]["published_candidate_id"] == PUBLISHED
    assert hold["evidence"]["published_bvid"] == BV
    assert hold["evidence"]["published_public_title"] == PUBLIC_TITLE


def test_valid_resolution_releases_sticky_hold_to_its_original_queue_without_upload(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {"picks": [published], "pending_talk": [candidate, distinct], "talk_backlog": []}
    first = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert first[0]["queue_origin"] == "pending_talk"
    resolution = _resolution(candidate, published, authority, registry)
    path = root / resolution_relative_path(CANDIDATE)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal resolution")

    second = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert second == []
    assert state[REVIEW_STATE_FIELD]["holds"] == []
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT, CANDIDATE]
    restored = state["pending_talk"][1]
    assert restored["selection_scorecard"] == candidate["selection_scorecard"]
    assert resolution["upload_authorized"] is False


def test_resolution_accepts_legacy_hold_without_origin_only_after_fresh_requeue(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, _distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {"picks": [published], "pending_talk": [candidate], "talk_backlog": []}
    first = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    legacy_hold = deepcopy(first[0])
    legacy_hold.pop("queue_origin")
    state[REVIEW_STATE_FIELD]["holds"] = [legacy_hold]

    resolution = _resolution(candidate, published, authority, registry)
    path = root / resolution_relative_path(CANDIDATE)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal resolution")

    # A current recovery row is authoritative enough to preserve in place;
    # the old receipt's missing origin alone must not turn it stale.
    state["pending_talk"] = [deepcopy(candidate)]
    assert hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    ) == []
    assert [row["candidate_id"] for row in state["pending_talk"]] == [CANDIDATE]

    # Without that fresh queue row, the same legacy omission is not enough to
    # guess pending vs backlog and therefore stays fail-closed.
    state[REVIEW_STATE_FIELD]["holds"] = [legacy_hold]
    state["pending_talk"] = []
    held = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert held[0]["disposition"] == STALE_REVIEW_STATUS
    assert "no valid queue origin" in held[0]["evidence"]["error"]


@pytest.mark.parametrize(
    "drift",
    ["candidate_hook", "registry", "resolution_bytes", "original_authority_bytes"],
)
def test_resolution_drift_keeps_the_candidate_in_a_sticky_hold(
    tmp_path: Path, rows: tuple[dict, dict, dict], drift: str
) -> None:
    candidate, published, _distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    resolution = _resolution(candidate, published, authority, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {"picks": [published], "pending_talk": [candidate], "talk_backlog": []}

    if drift == "candidate_hook":
        changed = deepcopy(candidate)
        changed["hook"] += "（漂移）"
        state["pending_talk"] = [changed]
    elif drift == "registry":
        registry = deepcopy(registry)
        registry["entries"][0]["note"] = "drift"
    elif drift == "resolution_bytes":
        path = root / resolution_relative_path(CANDIDATE)
        path.write_text(path.read_text() + "\n")
    else:
        path = root / authority_relative_path(CANDIDATE)
        path.write_text(path.read_text() + "\n")

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert len(holds) == 1
    assert holds[0]["disposition"] == STALE_REVIEW_STATUS
    assert holds[0]["upload_authorized"] is False
    assert state["pending_talk"] == []


def test_resolution_for_806_does_not_capture_1576(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    resolution = _resolution(candidate, published, authority, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {"picks": [published], "pending_talk": [candidate, distinct], "talk_backlog": []}

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert holds == []
    assert [row["candidate_id"] for row in state["pending_talk"]] == [CANDIDATE, DISTINCT]


def test_resolution_cannot_grant_upload_even_when_it_is_sealed(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, _distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    resolution = _resolution(candidate, published, authority, registry)
    resolution["upload_authorized"] = True
    resolution["resolution_sha256"] = canonical_sha256(
        {key: value for key, value in resolution.items() if key != "resolution_sha256"}
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {"picks": [published], "pending_talk": [candidate], "talk_backlog": []}

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert holds[0]["disposition"] == STALE_REVIEW_STATUS
    assert holds[0]["upload_authorized"] is False


@pytest.mark.parametrize("drift", ["candidate_hook", "registry_row", "asset_bytes"])
def test_candidate_registry_or_asset_drift_recomputes_to_sticky_stale_hold(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    drift: str,
) -> None:
    candidate, published, _distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {"picks": [published], "pending_talk": [candidate], "talk_backlog": []}
    first = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert first[0]["disposition"] == REVIEW_STATUS

    if drift == "candidate_hook":
        changed = deepcopy(candidate)
        changed["hook"] += "（改写）"
        state["pending_talk"] = [changed]
    elif drift == "registry_row":
        registry = deepcopy(registry)
        registry["entries"][0]["note"] = "changed registry bytes"
        state["pending_talk"] = [candidate]
    else:
        path = root / authority_relative_path(CANDIDATE)
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        state["pending_talk"] = [candidate]

    second = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert second[0]["disposition"] == STALE_REVIEW_STATUS
    assert second[0]["reason_code"] == "PUBLISHED_TOPIC_REVIEW_AUTHORITY_STALE"
    assert state["pending_talk"] == []
    assert state[REVIEW_STATE_FIELD]["holds"] == second


def test_merely_failed_target_is_not_accepted_as_a_published_collision(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    published["status"] = "failed"
    state = {
        "picks": [published],
        "pending_talk": [candidate, distinct],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    # A bad published claim cannot become a valid collision.  Because a sealed
    # review authority exists, validation failure stays parked fail-closed.
    assert holds[0]["disposition"] == STALE_REVIEW_STATUS
    assert "not published" in holds[0]["evidence"]["error"]
    # The merely adjacent candidate has no authority and remains eligible.
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]


def test_event_authority_does_not_capture_talk_scope(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    changed_scope = deepcopy(candidate)
    changed_scope["segment_scene_context"]["scene_kind"] = "talk"
    ordinary_talk = deepcopy(distinct)
    ordinary_talk["segment_scene_context"]["scene_kind"] = "talk"
    state = {
        "picks": [published],
        "pending_talk": [changed_scope, ordinary_talk],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert holds[0]["disposition"] == STALE_REVIEW_STATUS
    assert "scene_kind binding drifted" in holds[0]["evidence"]["error"]
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]


def test_uncommitted_asset_cannot_create_a_new_hold(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, _distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    # A second candidate-specific asset exists in the worktree but not HEAD.
    uncommitted = deepcopy(authority)
    uncommitted["candidate"] = _candidate_binding(
        _row(
            DISTINCT,
            "different topic",
            score=81.75,
        )
    )
    path = root / authority_relative_path(DISTINCT)
    path.write_text(json.dumps(uncommitted), encoding="utf-8")
    distinct = _row(DISTINCT, "different topic", score=81.75)
    state = {
        "picks": [published],
        "pending_talk": [distinct],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert holds == []
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]
