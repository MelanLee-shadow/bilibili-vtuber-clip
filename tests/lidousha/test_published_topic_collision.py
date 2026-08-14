from __future__ import annotations

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice import candidate_selection, historical_failed_talk_scope
from src.autoslice.published_topic_collision import (
    AUTHORITY_KIND,
    AUTHORITY_SCHEMA,
    PAIR_FINGERPRINT_KIND,
    REFRESHED_RESOLUTION_SCHEMA,
    CURRENT_REFRESH_PROVIDER_CONTRACT_SHA256,
    REVIEW_STATE_FIELD,
    REVIEW_STATUS,
    RECOVERY_BLOCKED,
    RECOVERY_CONVERGED,
    RECOVERY_MARKER_FIELD,
    RECOVERY_READY_TO_RELEASE,
    RECOVERY_REBOUND_SESSION_ANNOTATION,
    RECOVERY_RELEASED_QUEUED,
    RECOVERY_RELEASED_RETRY_PENDING,
    RESOLUTION_DECISION,
    RESOLUTION_SCHEMA,
    SEMANTIC_CHAT_POLICY_SHA256,
    STALE_REVIEW_STATUS,
    authority_relative_path,
    advance_published_topic_resolution_recovery,
    canonical_sha256,
    refreshed_resolution_relative_path,
    hold_published_topic_collision_reviews,
    inspect_published_topic_resolution_recovery,
    published_topic_resolution_recovery_outstanding,
    release_resolved_published_topic_hold,
    resolution_relative_path,
    seal_published_topic_resolution_production_transition,
    seal_published_topic_resolution_row_rebounds,
)
from src.autoslice.operator_processing_scope import (
    FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
    FINAL_REVIEW_RECOVERY_INTENT,
    TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
    TOPIC_HOLD_RECOVERY_INTENT,
    operator_talk_scope,
)
from src.autoslice.published_topic_final_review_handoff import (
    HANDOFF_READY,
    HANDOFF_TRANSITION_KIND,
    inspect_initial_final_review_handoff,
    seal_published_topic_final_review_handoff,
)
from src.autoslice.selected_final_review_recovery import (
    PICK_TO_QUEUE_TRANSITION,
    QUEUE_TO_PICK_TRANSITION,
    RECOVERY_RECEIPT_FIELD as FINAL_REVIEW_RECEIPT_FIELD,
    advance_selected_final_review_recovery_receipt,
    build_selected_final_review_recovery_receipt,
)
from src.autoslice.runner_proxy import RunnerProxy
from src.autoslice.talk_quota_policy import TalkQuotaPolicy


CANDIDATE = "auto_213135_806_1068"
PUBLISHED = "auto_213135_469_710"
DISTINCT = "auto_210131_1576_1802"
DATE = "2026-08-08"
BV = "BV18Gu16NEcX"
PUBLIC_TITLE = (
    "【李豆沙】第一次3D Live紧张到手抖，想起五年前看歌姬时的心情，"
    "自己好像正走在实现五年前舞台愿望的路上"
)
REPO_ROOT = Path(__file__).resolve().parents[2]


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
        "original_authority_relative_path": authority_relative_path(
            candidate["candidate_id"]
        ).as_posix(),
        "original_authority_raw_sha256": "",  # set after serializing authority bytes
        "original_authority_sha256": authority["authority_sha256"],
        "upload_authorized": False,
    }
    authority_bytes = (
        json.dumps(authority, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    resolution["original_authority_raw_sha256"] = (
        "sha256:" + hashlib.sha256(authority_bytes).hexdigest()
    )
    resolution["resolution_sha256"] = canonical_sha256(resolution)
    return resolution


def _refreshed_candidate(candidate: dict) -> dict:
    refreshed = deepcopy(candidate)
    candidate_id = refreshed["candidate_id"]
    old_scorecard_sha256 = canonical_sha256(refreshed["selection_scorecard"])
    refreshed["selection_scorecard"] = _card(91.25)
    refreshed.update(
        {
            "lane": "semantic_recall",
            "bcut_srt_path": "/cache/2026-08-08/source.bcut.srt",
            "xml": "/recordings/2026-08-08/source.xml",
        }
    )
    candidate_binding = {
        "candidate_id": candidate_id,
        "lane": refreshed["lane"],
        "hook": refreshed["hook"],
        "start_ms": refreshed["start_ms"],
        "end_ms": refreshed["end_ms"],
        "segment_path": refreshed["segment_path"],
        "bcut_srt_path": refreshed["bcut_srt_path"],
        "xml_path": refreshed["xml"],
    }
    input_provenance = {
        "source_media": {
            "path": refreshed["segment_path"],
            "size_bytes": 123456,
            "mtime_ns": 10,
            "ctime_ns": 11,
            "device": 67,
            "inode": 88,
            "mode": 511,
        },
        "bcut_srt": {
            "path": refreshed["bcut_srt_path"],
            "size_bytes": 4567,
            "mtime_ns": 12,
            "ctime_ns": 13,
            "device": 2049,
            "inode": 99,
            "mode": 420,
            "sha256": "sha256:" + "b" * 64,
        },
        "candidate_binding": candidate_binding,
        "candidate_binding_sha256": canonical_sha256(candidate_binding),
        "old_scorecard_sha256": old_scorecard_sha256,
        "prompt_sha256": "sha256:" + "c" * 64,
        "response_sha256": "sha256:" + "d" * 64,
        "semantic_chat": {
            "schema_version": "semantic-recall-chat-evidence.v1",
            "algorithm_id": "bounded-request-reaction.v1",
            "status": "LOADED",
            "policy_sha256": SEMANTIC_CHAT_POLICY_SHA256,
            "source_sha256": "sha256:" + "f" * 64,
            "evidence_sha256": "sha256:" + "1" * 64,
            "window_start_ms": refreshed["start_ms"],
            "window_end_ms": refreshed["end_ms"],
        },
        "semantic_chat_source": {
            "path": refreshed["xml"],
            "size_bytes": 7654,
            "mtime_ns": 14,
            "ctime_ns": 15,
            "device": 67,
            "inode": 89,
            "mode": 511,
            "sha256": "sha256:" + "f" * 64,
        },
    }
    refreshed["selection_scorecard"]["semantic_recall_chat_evidence"] = deepcopy(
        input_provenance["semantic_chat"]
    )
    provider_contract = {
        "schema_version": "semantic-evidence-scorecard-refresh-provider.v1",
        "transport": "command",
        "command": "scripts/llm_via_cpa.sh",
        "model_chain": ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4"],
        "reasoning_effort": "medium",
        "timeout_seconds": 600,
        "prompt_schema": "semantic-evidence-scorecard-refresh-prompt.v1",
    }
    assert canonical_sha256(provider_contract) == CURRENT_REFRESH_PROVIDER_CONTRACT_SHA256
    attempt_fingerprint = "sha256:" + "2" * 64
    receipt = {
        "schema_version": "semantic-evidence-scorecard-refresh-receipt.v1",
        "candidate_id": candidate_id,
        "recording_date": DATE,
        "scope_grant_id": "test-refresh-scope",
        "scope_grant_sha256": "sha256:" + "3" * 64,
        "status": "REFRESHED",
        "reason_code": "REFRESHED_WITH_CURRENT_CHAT_EVIDENCE",
        "attempt_fingerprint": attempt_fingerprint,
        "attempts": [
            {
                "attempted_at": "2026-08-13T20:18:35Z",
                "attempt_fingerprint": attempt_fingerprint,
                "attempt_number": 1,
                "outcome": "REFRESHED",
                "reason_code": "REFRESHED_WITH_CURRENT_CHAT_EVIDENCE",
                "prompt_sha256": input_provenance["prompt_sha256"],
                "response_sha256": input_provenance["response_sha256"],
            }
        ],
        "old_scorecard_sha256": old_scorecard_sha256,
        "new_scorecard_sha256": canonical_sha256(refreshed["selection_scorecard"]),
        "provider_contract": provider_contract,
        "provider_contract_sha256": canonical_sha256(provider_contract),
        "input_provenance": input_provenance,
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    refreshed["semantic_evidence_scorecard_refresh"] = receipt
    return refreshed


def _refreshed_resolution(
    original_candidate: dict,
    refreshed_candidate: dict,
    published: dict,
    authority: dict,
    registry: dict,
) -> dict:
    resolution = _resolution(original_candidate, published, authority, registry)
    receipt = refreshed_candidate["semantic_evidence_scorecard_refresh"]
    provenance = receipt["input_provenance"]
    resolution["schema_version"] = REFRESHED_RESOLUTION_SCHEMA
    resolution["candidate"] = _candidate_binding(refreshed_candidate)
    resolution["scorecard_refresh"] = {
        "receipt_schema_version": receipt["schema_version"],
        "receipt_sha256": receipt["receipt_sha256"],
        "candidate_id": receipt["candidate_id"],
        "recording_date": receipt["recording_date"],
        "old_selection_scorecard_sha256": receipt["old_scorecard_sha256"],
        "new_selection_scorecard_sha256": receipt["new_scorecard_sha256"],
        "candidate_binding_sha256": provenance["candidate_binding_sha256"],
        "input_provenance_sha256": canonical_sha256(provenance),
        "provider_contract_sha256": receipt["provider_contract_sha256"],
        "semantic_chat_policy_sha256": provenance["semantic_chat"]["policy_sha256"],
        "semantic_chat_source_sha256": provenance["semantic_chat"]["source_sha256"],
        "semantic_chat_evidence_sha256": provenance["semantic_chat"]["evidence_sha256"],
    }
    resolution["resolution_sha256"] = canonical_sha256(
        {key: value for key, value in resolution.items() if key != "resolution_sha256"}
    )
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
        resolution_path = root / (
            refreshed_resolution_relative_path(candidate_id)
            if resolution.get("schema_version") == REFRESHED_RESOLUTION_SCHEMA
            else resolution_relative_path(candidate_id)
        )
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


def _released_retry_state_without_refresh_receipt(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
) -> tuple[dict, Path, dict]:
    """Build the exact live v5 shape: a sealed retry row lost its old receipt."""

    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(
        candidate, refreshed, published, authority, registry
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {
        "upload_allowed": False,
        "picks": [deepcopy(published)],
        "pending_talk": [deepcopy(refreshed), deepcopy(distinct)],
        "talk_backlog": [],
        "pending_song": [{"candidate_id": "song-pending"}],
        "song_backlog": [{"candidate_id": "song-backlog"}],
        "song_selection_backlog": [{"candidate_id": "song-selection"}],
        "songs": [{"candidate_id": "song-old", "status": "blocked"}],
        "song_superseded_attempts": [{"candidate_id": "song-superseded"}],
    }
    held = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert [row["candidate_id"] for row in held] == [CANDIDATE]

    resolution_path = root / refreshed_resolution_relative_path(CANDIDATE)
    resolution_path.parent.mkdir(parents=True, exist_ok=True)
    resolution_path.write_text(
        json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal refreshed resolution")
    assert release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )

    production_preimage = deepcopy(state)
    released = next(
        row for row in state["pending_talk"] if row["candidate_id"] == CANDIDATE
    )
    state["pending_talk"] = [
        row for row in state["pending_talk"] if row["candidate_id"] != CANDIDATE
    ]
    failed = deepcopy(released)
    failed.pop("semantic_evidence_scorecard_refresh")
    failed.update(
        {
            "status": "failed",
            "rc": 1,
            "failure_kind": "provider_transient",
            "failure_recoverable": True,
        }
    )
    state["picks"].append(failed)
    assert seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=production_preimage,
        repo_root=root,
        publication_registry=registry,
    )

    retry_preimage = deepcopy(state)
    retry_row = deepcopy(failed)
    for field in ("status", "rc", "failure_kind", "failure_recoverable"):
        retry_row.pop(field)
    retry_row.update(
        {
            "selected_repair": True,
            "retry_reason": "transient_infrastructure_failure",
            "talk_repair_retry_count": 1,
            "talk_transient_retry_count": 1,
            "recovery_source_record_sha256": canonical_sha256(failed),
        }
    )
    state["picks"].remove(failed)
    state["pending_talk"].append(retry_row)
    assert advance_published_topic_resolution_recovery(
        state,
        CANDIDATE,
        pre_state=retry_preimage,
        from_collection="picks",
        from_row=failed,
        to_collection="pending_talk",
        to_row=retry_row,
        repo_root=root,
        publication_registry=registry,
    )
    assert "semantic_evidence_scorecard_refresh" not in retry_row
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_QUEUED
    )
    state["operator_processing_scope"] = {
        "schema_version": TOPIC_HOLD_RECOVERY_GRANT_SCHEMA,
        "grant_id": "retry-one-resolved-topic-hold",
        "recording_date": DATE,
        "reason": "只恢复已由 canonical ledger 约束的单个 topic hold。",
        "candidate_ids": [CANDIDATE],
        "user_authorization": {
            "quote": "806，1576内容没问题可以发。",
            "timestamp": "2026-08-13T18:00:00Z",
        },
        "expires_at": "2099-08-14T06:00:00Z",
        "intent": TOPIC_HOLD_RECOVERY_INTENT,
        "upload_allowed": False,
    }
    return state, root, registry


def _selected_authority_rejection_with_redundant_stale_hold(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    *,
    rejection_overrides: dict[str, object] | None = None,
) -> tuple[dict, Path, dict, dict]:
    """Build the observed 806 v5 marker head plus its generic stale re-hold."""

    state, root, registry = _released_retry_state_without_refresh_receipt(
        tmp_path, rows
    )
    production_preimage = deepcopy(state)
    rejected = next(
        row
        for row in state["pending_talk"]
        if row["candidate_id"] == CANDIDATE
    )
    state["pending_talk"] = [
        row
        for row in state["pending_talk"]
        if row["candidate_id"] != CANDIDATE
    ]
    rejected.update(
        {
            "status": "candidate_rejected",
            "rejected_status": "failed",
            "selected_repair": True,
            "rc": 1,
            "failure_kind": "subtitle_authority",
            "failure_stage": "chat_authority_final_artifact",
            "failure_recoverable": False,
            "failure_recovery_fingerprint": "sha256:" + "1" * 64,
            "rejection_reason": "subtitle_authority_unresolved_backfilled",
            **(rejection_overrides or {}),
        }
    )
    state["picks"].append(rejected)
    assert seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=production_preimage,
        repo_root=root,
        publication_registry=registry,
    )
    holds = hold_published_topic_collision_reviews(
        state,
        repo_root=root,
        publication_registry=registry,
    )
    assert len(holds) == 1
    assert holds[0] == {
        "candidate_id": CANDIDATE,
        "disposition": STALE_REVIEW_STATUS,
        "reason_code": "PUBLISHED_TOPIC_REVIEW_AUTHORITY_STALE",
        "score_mutated": False,
        "suppression_authorized": False,
        "upload_authorized": False,
        "queue_origin": None,
        "candidate": rejected,
        "evidence": {
            "error": (
                "PublishedTopicCollisionError: current scorecard refresh "
                "receipt is missing"
            )
        },
    }
    return state, root, registry, rejected


def _annotated_selected_authority_rejection_with_stale_hold(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, Path, dict, dict]:
    """Apply the real scoped session annotation to the observed stale re-hold."""

    from src.autoslice import published_topic_collision as topic_collision

    state, root, registry, rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(
            tmp_path,
            rows,
            rejection_overrides={"session_relation_authority": None},
        )
    )
    rejected_preimage = deepcopy(rejected)
    segment_stem = Path(rejected["segment_path"]).stem
    state["segment_sessions"] = {segment_stem: rejected["session_id"]}
    state["segment_relation_authorities"] = {}
    state["segment_scene_contexts"] = {
        segment_stem: deepcopy(rejected["segment_scene_context"])
    }
    original_seal = seal_published_topic_resolution_row_rebounds

    def seal_scoped(value: dict, **kwargs) -> bool:
        return original_seal(
            value,
            **kwargs,
            repo_root=root,
            publication_registry=registry,
        )

    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(
        topic_collision,
        "seal_published_topic_resolution_row_rebounds",
        seal_scoped,
    )
    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(CANDIDATE,),
    ) == 1
    return state, root, registry, rejected_preimage


def _v7_grant(candidate_id: str = CANDIDATE) -> dict:
    return {
        "schema_version": FINAL_REVIEW_RECOVERY_GRANT_SCHEMA,
        "grant_id": f"recover-{candidate_id}-final-review",
        "recording_date": DATE,
        "reason": "只重试点名的 final-review 失败及 provider-budget continuation。",
        "candidate_ids": [candidate_id],
        "user_authorization": {
            "quote": "806，1576内容没问题可以发。",
            "timestamp": "2026-08-13T20:17:34Z",
        },
        "expires_at": "2099-08-14T04:00:00Z",
        "intent": FINAL_REVIEW_RECOVERY_INTENT,
        "upload_allowed": False,
    }


def _initial_v5_to_v7_handoff_state(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
) -> tuple[dict, dict, Path, dict]:
    state, root, registry, rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(
            tmp_path,
            rows,
            rejection_overrides={
                "failure_stage": "final_review_findings",
                "talk_repair_retry_count": 5,
            },
        )
    )
    state[REVIEW_STATE_FIELD]["holds"] = []
    state["operator_processing_scope"] = _v7_grant()
    state.setdefault("talk_below_confidence_threshold", [])
    state.setdefault("talk_superseded_attempts", [])
    queue = deepcopy(rejected)
    for key in (
        "status",
        "rejected_status",
        "rc",
        "failure_kind",
        "failure_stage",
        "failure_recoverable",
        "failure_recovery_fingerprint",
        "rejection_reason",
    ):
        queue.pop(key, None)
    queue.update(
        {
            "talk_repair_retry_count": 6,
            "retry_reason": "pipeline_fingerprint_changed",
            "recovery_source_record_sha256": canonical_sha256(rejected),
        }
    )
    receipt = build_selected_final_review_recovery_receipt(
        old_row=rejected,
        queued_row=queue,
        candidate_id=CANDIDATE,
        grant_id=_v7_grant()["grant_id"],
        current_fingerprint="sha256:" + "2" * 64,
    )
    queue[FINAL_REVIEW_RECEIPT_FIELD] = receipt
    post = deepcopy(state)
    post["picks"] = [
        row for row in post["picks"] if row.get("candidate_id") != CANDIDATE
    ]
    post["pending_talk"].append(queue)
    post["talk_superseded_attempts"].append(
        {
            "candidate_id": CANDIDATE,
            FINAL_REVIEW_RECEIPT_FIELD: deepcopy(receipt),
        }
    )
    return state, post, root, registry


def test_v7_initial_handoff_session_annotation_reseals_v5_pick_head(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 806 null cleanup must be sealed before its one-way handoff."""

    from src.autoslice import published_topic_collision as topic_collision
    from src.autoslice import published_topic_final_review_handoff as handoff

    state, root, registry, rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(
            tmp_path,
            rows,
            rejection_overrides={
                "failure_stage": "final_review_findings",
                "talk_repair_retry_count": 5,
                "session_relation_authority": None,
            },
        )
    )
    state[REVIEW_STATE_FIELD]["holds"] = []
    state["operator_processing_scope"] = _v7_grant()
    state.setdefault("talk_below_confidence_threshold", [])
    state.setdefault("talk_superseded_attempts", [])
    segment_stem = Path(rejected["segment_path"]).stem
    state["segment_sessions"] = {segment_stem: rejected["session_id"]}
    state["segment_relation_authorities"] = {}
    state["segment_scene_contexts"] = {
        segment_stem: deepcopy(rejected["segment_scene_context"])
    }
    marker_before = deepcopy(
        state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]
    )

    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", CANDIDATE)
        else pytest.fail("unrelated recovery fingerprint requested"),
        raising=False,
    )
    original_seal = seal_published_topic_resolution_row_rebounds

    def seal_scoped(value: dict, **kwargs) -> bool:
        return original_seal(
            value,
            **kwargs,
            repo_root=root,
            publication_registry=registry,
        )

    original_inspect = inspect_initial_final_review_handoff

    def inspect_scoped(value: dict, **kwargs) -> str:
        return original_inspect(
            value,
            **kwargs,
            repo_root=root,
            publication_registry=registry,
        )

    monkeypatch.setattr(runner, "list_segments", lambda _date: [])
    monkeypatch.setattr(
        topic_collision,
        "seal_published_topic_resolution_row_rebounds",
        seal_scoped,
    )
    monkeypatch.setattr(
        handoff,
        "inspect_initial_final_review_handoff",
        inspect_scoped,
    )
    assert (
        inspect_initial_final_review_handoff(
            state,
            candidate_id=CANDIDATE,
            recording_date=DATE,
            repo_root=root,
            publication_registry=registry,
        )
        == HANDOFF_READY
    )

    assert historical_failed_talk_scope.annotate_sessions(
        DATE,
        state,
        include_song_rows=False,
        candidate_ids=(CANDIDATE,),
    ) == 1

    rebound = next(
        row for row in state["picks"] if row.get("candidate_id") == CANDIDATE
    )
    assert "session_relation_authority" not in rebound
    marker_after = state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]
    assert marker_after != marker_before
    assert marker_after["transitions"][-1]["transition_kind"] == (
        "CURRENT_ROW_REBOUND"
    )
    assert marker_after["transitions"][-1]["transition_phase"] == (
        "SESSION_ANNOTATION"
    )
    assert marker_after["transitions"][-1]["changed_fields"] == [
        "session_relation_authority"
    ]
    assert (
        inspect_initial_final_review_handoff(
            state,
            candidate_id=CANDIDATE,
            recording_date=DATE,
            repo_root=root,
            publication_registry=registry,
        )
        == HANDOFF_READY
    )
    assert "operator_processing_scope_runtime_block" not in state


def _terminal_v5_to_v7_handoff_state(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict, Path, dict]:
    pre, state, root, registry = _initial_v5_to_v7_handoff_state(tmp_path, rows)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, kind, candidate_id: "sha256:" + "2" * 64
        if (kind, candidate_id) == ("subtitle_authority", CANDIDATE)
        else pytest.fail("unrelated recovery fingerprint requested"),
        raising=False,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            pre,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )
    assert seal_published_topic_final_review_handoff(
        state,
        CANDIDATE,
        pre_state=pre,
        recording_date=DATE,
        repo_root=root,
        publication_registry=registry,
    )
    return state, root, registry


def test_v5_terminal_handoff_converges_without_permanent_double_write(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import exact_talk_recovery_scope
    from src.autoslice import published_topic_collision as topic_collision

    state, root, registry = _terminal_v5_to_v7_handoff_state(
        tmp_path, rows, monkeypatch
    )
    marker = state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]
    frozen_marker = deepcopy(marker)
    assert marker["transitions"][-1]["transition_kind"] == HANDOFF_TRANSITION_KIND
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )
    persisted = json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True))
    assert (
        inspect_published_topic_resolution_recovery(
            persisted,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )

    original_inspect = inspect_published_topic_resolution_recovery
    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        lambda value, candidate_id, **_kwargs: original_inspect(
            value,
            candidate_id,
            repo_root=root,
            publication_registry=registry,
        ),
    )
    assert exact_talk_recovery_scope._marker_bound_topic_candidate_ids(state) == set()
    assert operator_talk_scope(state, date=DATE) == (CANDIDATE,)

    rebound_preimage = deepcopy(state)
    target = next(
        row for row in state["pending_talk"] if row["candidate_id"] == CANDIDATE
    )
    target["session_id"] = "session-after-terminal-handoff"
    assert seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=rebound_preimage,
        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
        candidate_ids=(CANDIDATE,),
        repo_root=root,
        publication_registry=registry,
    )
    assert state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE] == frozen_marker

    state["operator_processing_scope"] = _v7_grant(DISTINCT)
    assert (
        original_inspect(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )


def test_terminal_handoff_accepts_linear_queue_pick_queue_continuation(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, root, registry = _terminal_v5_to_v7_handoff_state(
        tmp_path, rows, monkeypatch
    )
    queue = next(
        row for row in state["pending_talk"] if row["candidate_id"] == CANDIDATE
    )
    initial = queue[FINAL_REVIEW_RECEIPT_FIELD]
    pick_body = {
        key: deepcopy(value)
        for key, value in queue.items()
        if key != FINAL_REVIEW_RECEIPT_FIELD
    }
    pick_body.update(
        {
            "status": "failed",
            "failure_kind": "provider_transient",
            "failure_stage": "external_provider",
            "failure_recoverable": True,
        }
    )
    pick_receipt = advance_selected_final_review_recovery_receipt(
        initial,
        from_row=queue,
        to_row=pick_body,
        candidate_id=CANDIDATE,
        grant_id=_v7_grant()["grant_id"],
        transition_kind=QUEUE_TO_PICK_TRANSITION,
        allow_queue_rebound=True,
    )
    pick = {**pick_body, FINAL_REVIEW_RECEIPT_FIELD: pick_receipt}
    state["pending_talk"] = [
        row for row in state["pending_talk"] if row is not queue
    ]
    state["picks"].append(pick)
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )

    next_queue_body = {
        key: deepcopy(value)
        for key, value in queue.items()
        if key != FINAL_REVIEW_RECEIPT_FIELD
    }
    next_queue_body["retry_reason"] = "final_review_provider_budget"
    queue_receipt = advance_selected_final_review_recovery_receipt(
        pick_receipt,
        from_row=pick,
        to_row=next_queue_body,
        candidate_id=CANDIDATE,
        grant_id=_v7_grant()["grant_id"],
        transition_kind=PICK_TO_QUEUE_TRANSITION,
    )
    next_queue = {
        **next_queue_body,
        FINAL_REVIEW_RECEIPT_FIELD: queue_receipt,
    }
    state["picks"] = [row for row in state["picks"] if row is not pick]
    state["pending_talk"].append(next_queue)
    state["talk_superseded_attempts"].append(
        {
            "candidate_id": CANDIDATE,
            FINAL_REVIEW_RECEIPT_FIELD: deepcopy(queue_receipt),
        }
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )


@pytest.mark.parametrize(
    "drift",
    [
        "marker",
        "grant",
        "grant_hash",
        "receipt_hash",
        "receipt_old",
        "receipt_queue",
        "receipt_fingerprint",
        "receipt_first_transition",
        "candidate_id",
        "recording_date",
        "intent",
        "upload",
        "state_upload",
        "current_history_fork",
        "current_receipt_missing",
        "target_hold",
        "song_target",
    ],
)
def test_terminal_handoff_tamper_matrix_fails_closed(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    state, root, registry = _terminal_v5_to_v7_handoff_state(
        tmp_path, rows, monkeypatch
    )
    ledger = state[RECOVERY_MARKER_FIELD]
    marker = ledger["entries"][CANDIDATE]
    transition = marker["transitions"][-1]
    embedded = transition["initial_recovery_receipt"]
    current = next(
        row for row in state["pending_talk"] if row["candidate_id"] == CANDIDATE
    )
    if drift == "marker":
        marker["marker_sha256"] = "sha256:" + "f" * 64
    elif drift == "grant":
        transition["operator_scope_grant"]["grant_id"] = "other-grant"
    elif drift == "grant_hash":
        transition["operator_scope_grant_sha256"] = "sha256:" + "f" * 64
    elif drift == "receipt_hash":
        transition["initial_recovery_receipt_sha256"] = "sha256:" + "f" * 64
    elif drift == "receipt_old":
        embedded["old_row_sha256"] = "sha256:" + "f" * 64
    elif drift == "receipt_queue":
        embedded["initial_queue_row_sha256"] = "sha256:" + "f" * 64
    elif drift == "receipt_fingerprint":
        embedded["current_failure_recovery_fingerprint"] = "sha256:" + "f" * 64
    elif drift == "receipt_first_transition":
        embedded["transitions"][0]["kind"] = "QUEUE_TO_PICK"
    elif drift == "candidate_id":
        transition["candidate_id"] = DISTINCT
    elif drift == "recording_date":
        transition["recording_date"] = "2026-08-09"
    elif drift == "intent":
        transition["operator_scope_intent"] = "RECOVER_EVERYTHING"
    elif drift == "upload":
        transition["upload_authorized"] = True
    elif drift == "state_upload":
        state["upload_allowed"] = True
    elif drift == "current_history_fork":
        fork_body = deepcopy(embedded["initial_queue_row"])
        fork_body["hook"] += " fork"
        fork = build_selected_final_review_recovery_receipt(
            old_row=embedded["old_row"],
            queued_row=fork_body,
            candidate_id=CANDIDATE,
            grant_id=_v7_grant()["grant_id"],
            current_fingerprint="sha256:" + "2" * 64,
        )
        state["talk_superseded_attempts"].append(
            {"candidate_id": CANDIDATE, FINAL_REVIEW_RECEIPT_FIELD: fork}
        )
    elif drift == "current_receipt_missing":
        current.pop(FINAL_REVIEW_RECEIPT_FIELD)
    elif drift == "target_hold":
        state[REVIEW_STATE_FIELD]["holds"].append(
            {"candidate_id": CANDIDATE, "candidate": deepcopy(current)}
        )
    else:
        state["pending_song"].append({"candidate_id": CANDIDATE})

    receipt_semantic_drift = {
        "receipt_old",
        "receipt_queue",
        "receipt_fingerprint",
        "receipt_first_transition",
    }
    if drift in receipt_semantic_drift:
        embedded["receipt_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in embedded.items()
                if key != "receipt_sha256"
            }
        )
        transition["initial_recovery_receipt_sha256"] = canonical_sha256(embedded)
    if drift == "grant":
        transition["operator_scope_grant_sha256"] = canonical_sha256(
            transition["operator_scope_grant"]
        )
    sealed_transition_drift = {
        "grant",
        "grant_hash",
        "receipt_hash",
        *receipt_semantic_drift,
        "candidate_id",
        "recording_date",
        "intent",
        "upload",
    }
    if drift in sealed_transition_drift:
        transition["transition_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in transition.items()
                if key != "transition_sha256"
            }
        )
        marker["marker_sha256"] = canonical_sha256(
            {key: value for key, value in marker.items() if key != "marker_sha256"}
        )
    if drift == "marker" or drift in sealed_transition_drift:
        ledger["ledger_sha256"] = canonical_sha256(
            {key: value for key, value in ledger.items() if key != "ledger_sha256"}
        )

    before = deepcopy(state)
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == before
    assert not seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=deepcopy(state),
        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
        candidate_ids=(CANDIDATE,),
        repo_root=root,
        publication_registry=registry,
    )
    assert state == before


def test_nonconverged_v5_head_cannot_be_handed_to_v7(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pre, state, root, registry = _initial_v5_to_v7_handoff_state(tmp_path, rows)
    target = next(
        row for row in pre["picks"] if row["candidate_id"] == CANDIDATE
    )
    pre[REVIEW_STATE_FIELD]["holds"].append(
        {"candidate_id": CANDIDATE, "candidate": deepcopy(target)}
    )
    state[REVIEW_STATE_FIELD] = deepcopy(pre[REVIEW_STATE_FIELD])
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: "sha256:" + "2" * 64,
        raising=False,
    )
    before = deepcopy(state)
    assert not seal_published_topic_final_review_handoff(
        state,
        CANDIDATE,
        pre_state=pre,
        recording_date=DATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert state == before


@pytest.mark.parametrize(
    "drift",
    [
        "pre_upload",
        "post_upload",
        "post_scope",
        "non_target_talk",
        "non_target_history",
        "non_target_song",
    ],
)
def test_terminal_handoff_seal_rejects_state_envelope_drift(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    pre, state, root, registry = _initial_v5_to_v7_handoff_state(tmp_path, rows)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: "sha256:" + "2" * 64,
        raising=False,
    )
    if drift == "pre_upload":
        pre["upload_allowed"] = True
    elif drift == "post_upload":
        state["upload_allowed"] = True
    elif drift == "post_scope":
        state["operator_processing_scope"] = _v7_grant(DISTINCT)
    elif drift == "non_target_talk":
        next(
            row
            for row in state["picks"]
            if row.get("candidate_id") != CANDIDATE
        )["hook"] = "unrelated talk drift"
    elif drift == "non_target_history":
        state["talk_superseded_attempts"].append(
            {"candidate_id": DISTINCT, "status": "unrelated history drift"}
        )
    else:
        state["pending_song"].append({"candidate_id": "song_unrelated_drift"})
    before = deepcopy(state)

    assert not seal_published_topic_final_review_handoff(
        state,
        CANDIDATE,
        pre_state=pre,
        recording_date=DATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert state == before


@pytest.mark.parametrize(
    ("current_fingerprint", "expected"),
    [
        ("sha256:" + "2" * 64, RECOVERY_RELEASED_RETRY_PENDING),
        ("sha256:" + "1" * 64, RECOVERY_CONVERGED),
        ("sha256:bad", RECOVERY_BLOCKED),
    ],
)
def test_v5_selected_subtitle_authority_rejection_uses_exact_recovery_fingerprint(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    current_fingerprint: str,
    expected: str,
) -> None:
    state, root, registry, _rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(tmp_path, rows)
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, kind, candidate_id: (
            current_fingerprint
            if (kind, candidate_id) == ("subtitle_authority", CANDIDATE)
            else pytest.fail("fingerprint probe widened beyond the exact target")
        ),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == expected
    )
    assert state == preimage


def test_v5_selected_subtitle_authority_rejection_blocks_fingerprint_errors(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, root, registry, _rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(tmp_path, rows)
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("current fingerprint failed")),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == preimage


def test_v5_selected_subtitle_authority_rejection_ignores_unrelated_holds(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, root, registry, _rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(tmp_path, rows)
    )
    state[REVIEW_STATE_FIELD]["holds"].append(
        {"candidate_id": DISTINCT, "candidate": {"candidate_id": DISTINCT}}
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, _kind, _candidate_id: "sha256:" + "2" * 64,
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_RETRY_PENDING
    )
    assert state == preimage


@pytest.mark.parametrize("recorded", [None, "sha256:bad"])
def test_v5_selected_subtitle_authority_rejection_blocks_malformed_recorded_fingerprint(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    recorded: object,
) -> None:
    state, root, registry, _rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(
            tmp_path,
            rows,
            rejection_overrides={"failure_recovery_fingerprint": recorded},
        )
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: pytest.fail("malformed recorded fingerprint must short-circuit"),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == preimage


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rejected_status", "boundary_unrepairable"),
        ("selected_repair", False),
        ("rc", 0),
        ("failure_kind", "content_boundary"),
        ("failure_kind", "speaker_evidence"),
        ("failure_stage", "chat_authority_finalization"),
        ("failure_stage", "final_review_findings"),
        ("failure_stage", "speaker_finalization"),
        ("failure_recoverable", True),
        ("rejection_reason", "story_contract_unresolved_backfilled"),
    ],
)
def test_v5_selected_subtitle_authority_rejection_shape_does_not_widen(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    state, root, registry, _rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(
            tmp_path, rows, rejection_overrides={field: value}
        )
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: pytest.fail("non-exact rejection must not probe fingerprint"),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == preimage


@pytest.mark.parametrize(
    "drift",
    [
        "candidate",
        "candidate_identity",
        "score_flag",
        "suppression_flag",
        "upload_flag",
        "queue_origin",
        "evidence",
        "marker_head_hash",
        "review_schema",
        "review_extra",
        "missing_holds",
        "extra_field",
        "missing_field",
        "duplicate",
    ],
)
def test_v5_selected_subtitle_authority_rejection_blocks_stale_hold_drift(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    state, root, registry, _rejected = (
        _selected_authority_rejection_with_redundant_stale_hold(tmp_path, rows)
    )
    holds = state[REVIEW_STATE_FIELD]["holds"]
    hold = holds[0]
    if drift == "candidate":
        hold["candidate"]["hook"] += "（漂移）"
    elif drift == "candidate_identity":
        hold["candidate_id"] = DISTINCT
    elif drift == "score_flag":
        hold["score_mutated"] = True
    elif drift == "suppression_flag":
        hold["suppression_authorized"] = True
    elif drift == "upload_flag":
        hold["upload_authorized"] = True
    elif drift == "queue_origin":
        hold["queue_origin"] = "pending_talk"
    elif drift == "evidence":
        hold["evidence"]["error"] += "（漂移）"
    elif drift == "marker_head_hash":
        marker = state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]
        marker["current_row_binding"]["row_sha256"] = "sha256:" + "9" * 64
        marker["marker_sha256"] = canonical_sha256(
            {key: value for key, value in marker.items() if key != "marker_sha256"}
        )
        ledger = state[RECOVERY_MARKER_FIELD]
        ledger["ledger_sha256"] = canonical_sha256(
            {key: value for key, value in ledger.items() if key != "ledger_sha256"}
        )
    elif drift == "review_schema":
        state[REVIEW_STATE_FIELD]["schema_version"] = "tampered-review-state.v1"
    elif drift == "review_extra":
        state[REVIEW_STATE_FIELD]["unsealed"] = True
    elif drift == "missing_holds":
        state[REVIEW_STATE_FIELD].pop("holds")
    elif drift == "extra_field":
        hold["unsealed"] = True
    elif drift == "missing_field":
        hold.pop("score_mutated")
    else:
        holds.append(deepcopy(hold))
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: pytest.fail("drifted hold must block before fingerprint probe"),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == preimage


def test_v5_selected_subtitle_authority_stale_rehold_is_removed_then_requeued_with_lineage(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import published_topic_collision as topic_collision

    state, root, registry, rejected_preimage = (
        _annotated_selected_authority_rejection_with_stale_hold(
            tmp_path, rows, monkeypatch
        )
    )
    annotated_preimage = deepcopy(
        next(row for row in state["picks"] if row.get("candidate_id") == CANDIDATE)
    )
    song_preimage = deepcopy(
        {
            key: state[key]
            for key in (
                "pending_song",
                "song_backlog",
                "song_selection_backlog",
                "songs",
                "song_superseded_attempts",
            )
        }
    )
    non_target_preimage = {
        collection: [
            deepcopy(row)
            for row in state.get(collection, [])
            if (row.get("candidate_id") or row.get("cid")) != CANDIDATE
        ]
        for collection in (
            "pending_talk",
            "talk_backlog",
            "picks",
            "talk_below_confidence_threshold",
        )
    }
    original_inspect = inspect_published_topic_resolution_recovery
    original_advance = advance_published_topic_resolution_recovery

    def inspect_scoped(value: dict, candidate_id: str) -> str:
        return original_inspect(
            value,
            candidate_id,
            repo_root=root,
            publication_registry=registry,
        )

    def advance_scoped(value: dict, candidate_id: str, **kwargs) -> bool:
        return original_advance(
            value,
            candidate_id,
            **kwargs,
            repo_root=root,
            publication_registry=registry,
        )

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        inspect_scoped,
    )
    monkeypatch.setattr(
        topic_collision,
        "advance_published_topic_resolution_recovery",
        advance_scoped,
    )
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, kind, candidate_id: (
            "sha256:" + "2" * 64
            if (kind, candidate_id) == ("subtitle_authority", CANDIDATE)
            else pytest.fail("fingerprint probe widened beyond the exact target")
        ),
        raising=False,
    )
    assert "session_relation_authority" not in annotated_preimage
    assert state[REVIEW_STATE_FIELD]["holds"][0]["candidate"] == rejected_preimage
    annotation = state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]["transitions"][-1]
    assert annotation["transition_kind"] == "CURRENT_ROW_REBOUND"
    assert annotation["transition_phase"] == "SESSION_ANNOTATION"
    assert annotation["changed_fields"] == ["session_relation_authority"]
    assert annotation["previous_row_binding"]["row_sha256"] == canonical_sha256(
        rejected_preimage
    )
    assert annotation["next_row_binding"]["row_sha256"] == canonical_sha256(
        annotated_preimage
    )
    assert inspect_scoped(state, CANDIDATE) == RECOVERY_RELEASED_RETRY_PENDING

    def generic_requeue(
        _date: str,
        value: dict,
        *,
        automatic_maintenance: bool,
        talk_candidate_ids: tuple[str, ...],
    ) -> tuple[int, int, int, int, bool]:
        assert automatic_maintenance is True
        assert tuple(talk_candidate_ids) == (CANDIDATE,)
        assert value[REVIEW_STATE_FIELD]["holds"] == []
        failed = next(
            row for row in value["picks"] if row.get("candidate_id") == CANDIDATE
        )
        value["picks"].remove(failed)
        queued = deepcopy(failed)
        queued["recovery_source_record_sha256"] = canonical_sha256(failed)
        value["pending_talk"].append(queued)
        return 0, 0, 1, 0, False

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        generic_requeue,
    )

    result = historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(CANDIDATE,),
    )

    assert result == (0, 0, 1, 0, False)
    assert state[REVIEW_STATE_FIELD]["holds"] == []
    assert not any(
        row.get("candidate_id") == CANDIDATE for row in state["picks"]
    )
    queued = next(
        row
        for row in state["pending_talk"]
        if row.get("candidate_id") == CANDIDATE
    )
    assert queued["recovery_source_record_sha256"] == canonical_sha256(
        annotated_preimage
    )
    marker = state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]
    assert marker["current_row_binding"] == {
        "collection": "pending_talk",
        "row_sha256": canonical_sha256(queued),
    }
    assert [item["transition_kind"] for item in marker["transitions"][-2:]] == [
        "CURRENT_ROW_REBOUND",
        "RECOVERABLE_FAILED_PICK_REQUEUED",
    ]
    assert marker["transitions"][-1]["failed_pick_sha256"] == canonical_sha256(
        annotated_preimage
    )
    assert inspect_scoped(state, CANDIDATE) == RECOVERY_RELEASED_QUEUED
    assert {key: state[key] for key in song_preimage} == song_preimage
    assert {
        collection: [
            row
            for row in state.get(collection, [])
            if (row.get("candidate_id") or row.get("cid")) != CANDIDATE
        ]
        for collection in non_target_preimage
    } == non_target_preimage


def test_v5_annotated_stale_hold_blocks_resealed_changed_field_claim(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid self-seal cannot substitute for the exact old/new row diff."""

    state, root, registry, _rejected = (
        _annotated_selected_authority_rejection_with_stale_hold(
            tmp_path, rows, monkeypatch
        )
    )
    ledger = state[RECOVERY_MARKER_FIELD]
    marker = ledger["entries"][CANDIDATE]
    transition = marker["transitions"][-1]
    transition["changed_fields"] = ["session_id"]
    transition["transition_sha256"] = canonical_sha256(
        {
            key: value
            for key, value in transition.items()
            if key != "transition_sha256"
        }
    )
    marker["marker_sha256"] = canonical_sha256(
        {key: value for key, value in marker.items() if key != "marker_sha256"}
    )
    ledger["ledger_sha256"] = canonical_sha256(
        {key: value for key, value in ledger.items() if key != "ledger_sha256"}
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: pytest.fail("nonexact rebound must block before fingerprint"),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == preimage


def test_v5_annotated_stale_hold_blocks_when_it_is_two_rebounds_old(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the immediately previous annotation head may explain a stale hold."""

    state, root, registry, _rejected = (
        _annotated_selected_authority_rejection_with_stale_hold(
            tmp_path, rows, monkeypatch
        )
    )
    rebound_preimage = deepcopy(state)
    current = next(
        row for row in state["picks"] if row.get("candidate_id") == CANDIDATE
    )
    current["session_id"] = "live-20260808Treannotated"
    assert seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=rebound_preimage,
        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
        repo_root=root,
        publication_registry=registry,
    )
    preimage = deepcopy(state)
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda *_args: pytest.fail("non-immediate hold must block before fingerprint"),
        raising=False,
    )

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == preimage


def test_v5_annotated_stale_hold_reconciliation_rolls_back_with_failed_requeue(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the proven old hold remains atomic with retry and lineage."""

    from src.autoslice import published_topic_collision as topic_collision

    state, root, registry, _rejected = (
        _annotated_selected_authority_rejection_with_stale_hold(
            tmp_path, rows, monkeypatch
        )
    )
    preimage = deepcopy(state)

    def inspect_scoped(value: dict, candidate_id: str) -> str:
        return inspect_published_topic_resolution_recovery(
            value,
            candidate_id,
            repo_root=root,
            publication_registry=registry,
        )

    monkeypatch.setattr(
        topic_collision,
        "inspect_published_topic_resolution_recovery",
        inspect_scoped,
    )
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, kind, candidate_id: (
            "sha256:" + "2" * 64
            if (kind, candidate_id) == ("subtitle_authority", CANDIDATE)
            else pytest.fail("fingerprint probe widened beyond the exact target")
        ),
        raising=False,
    )

    def failed_requeue(
        _date: str, value: dict, **_kwargs
    ) -> tuple[int, int, int, int, bool]:
        assert value[REVIEW_STATE_FIELD]["holds"] == []
        value["picks"] = [
            row
            for row in value["picks"]
            if row.get("candidate_id") != CANDIDATE
        ]
        raise RuntimeError("retry failed after mutating the pick")

    monkeypatch.setattr(
        historical_failed_talk_scope,
        "maintain_delivery_recovery_scope",
        failed_requeue,
    )

    assert historical_failed_talk_scope.maintain(
        DATE,
        state,
        automatic_maintenance=True,
        candidate_ids=(CANDIDATE,),
    ) == (0, 0, 0, 0, False)
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state[REVIEW_STATE_FIELD]["holds"] == preimage[REVIEW_STATE_FIELD][
        "holds"
    ]
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_RETRY_TRANSITION_BLOCKED"
    )


def _patch_frozen_retry_selection_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    root: Path,
    registry: dict,
    hold_calls: list[str],
) -> None:
    policy = TalkQuotaPolicy(
        kind="talk",
        scope_key="talk:live-20260808Tunknown",
        cap=5,
        extra_slot_min_score=None,
        recording_date=DATE,
    )
    monkeypatch.setattr(candidate_selection, "_talk_quota_policy", lambda _row: policy)
    monkeypatch.setattr(
        runner,
        "exclude_session_edge_bgm_candidates",
        lambda _state: None,
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "quarantine_overlapping_talk_candidates",
        lambda _state: None,
        raising=False,
    )
    monkeypatch.setattr(
        candidate_selection,
        "_assign_cover_diversity_slots",
        lambda _state: None,
    )
    monkeypatch.setattr(
        runner,
        "refill_songs",
        lambda _state: None,
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "prioritize",
        candidate_selection.prioritize,
        raising=False,
    )

    def real_hold(value: dict, **kwargs) -> list[dict[str, object]]:
        hold_calls.append("called")
        return hold_published_topic_collision_reviews(
            value,
            repo_root=root,
            publication_registry=registry,
            **kwargs,
        )

    real_seal = seal_published_topic_resolution_row_rebounds

    def real_scoped_seal(value: dict, **kwargs) -> bool:
        return real_seal(
            value,
            **kwargs,
            repo_root=root,
            publication_registry=registry,
        )

    monkeypatch.setattr(
        candidate_selection,
        "hold_published_topic_collision_reviews",
        real_hold,
    )
    monkeypatch.setattr(
        "src.autoslice.published_topic_collision."
        "seal_published_topic_resolution_row_rebounds",
        real_scoped_seal,
    )


def test_v5_marker_bound_retry_skips_duplicate_generic_topic_hold(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, root, registry = _released_retry_state_without_refresh_receipt(
        tmp_path, rows
    )
    hold_calls: list[str] = []
    _patch_frozen_retry_selection_dependencies(
        monkeypatch,
        root=root,
        registry=registry,
        hold_calls=hold_calls,
    )
    non_target_preimage = deepcopy(
        next(
            row
            for row in state["pending_talk"]
            if row["candidate_id"] == DISTINCT
        )
    )
    song_preimage = deepcopy(
        {
            key: state[key]
            for key in (
                "pending_song",
                "song_backlog",
                "song_selection_backlog",
                "songs",
                "song_superseded_attempts",
            )
        }
    )

    captured = historical_failed_talk_scope.prioritize_and_capture(
        DATE, state, (CANDIDATE,)
    )

    assert captured is not None
    assert [row["candidate_id"] for row in captured] == [CANDIDATE]
    assert hold_calls == []
    target_rows = [
        (collection, row)
        for collection in ("pending_talk", "talk_backlog")
        for row in state[collection]
        if row["candidate_id"] == CANDIDATE
    ]
    assert len(target_rows) == 1
    assert target_rows[0][0] == "pending_talk"
    assert "semantic_evidence_scorecard_refresh" not in target_rows[0][1]
    assert state[REVIEW_STATE_FIELD]["holds"] == []
    assert next(
        row for row in state["pending_talk"] if row["candidate_id"] == DISTINCT
    ) == non_target_preimage
    assert {key: state[key] for key in song_preimage} == song_preimage
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_QUEUED
    )


@pytest.mark.parametrize("marker_damage", ["missing", "tampered"])
def test_v5_generic_topic_bypass_still_fails_closed_on_invalid_marker(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
    marker_damage: str,
) -> None:
    state, root, registry = _released_retry_state_without_refresh_receipt(
        tmp_path, rows
    )
    if marker_damage == "missing":
        state.pop(RECOVERY_MARKER_FIELD)
    else:
        state[RECOVERY_MARKER_FIELD]["ledger_sha256"] = "sha256:" + "0" * 64
    preimage = deepcopy(state)
    hold_calls: list[str] = []
    _patch_frozen_retry_selection_dependencies(
        monkeypatch,
        root=root,
        registry=registry,
        hold_calls=hold_calls,
    )

    assert (
        historical_failed_talk_scope.prioritize_and_capture(
            DATE, state, (CANDIDATE,)
        )
        is None
    )
    assert hold_calls == []
    assert {
        key: value
        for key, value in state.items()
        if key != "operator_processing_scope_runtime_block"
    } == preimage
    assert state["operator_processing_scope_runtime_block"]["reason_code"] == (
        "TOPIC_DEDUP_PRIORITIZE_TRANSITION_BLOCKED"
    )


def test_ordinary_unmarked_candidate_still_enters_sticky_stale_topic_hold(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, published, _distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(
        candidate, refreshed, published, authority, registry
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    no_receipt = deepcopy(refreshed)
    no_receipt.pop("semantic_evidence_scorecard_refresh")
    state = {
        "picks": [deepcopy(published)],
        "pending_talk": [no_receipt],
        "talk_backlog": [],
        "pending_song": [],
        "songs": [],
    }
    hold_calls: list[str] = []
    _patch_frozen_retry_selection_dependencies(
        monkeypatch,
        root=root,
        registry=registry,
        hold_calls=hold_calls,
    )

    candidate_selection.prioritize(state)

    assert hold_calls == ["called"]
    assert all(
        row.get("candidate_id") != CANDIDATE
        for row in state["pending_talk"] + state["talk_backlog"]
    )
    hold = next(
        row
        for row in state[REVIEW_STATE_FIELD]["holds"]
        if row["candidate_id"] == CANDIDATE
    )
    assert hold["disposition"] == STALE_REVIEW_STATUS


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


def test_scoped_hold_mutates_only_target_and_preserves_non_target_hold_bytes(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    non_target, published, target = rows
    registry = _registry()
    non_target_authority = _authority(non_target, published, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, non_target_authority)
    state = {
        "picks": [deepcopy(published)],
        "pending_talk": [deepcopy(non_target), deepcopy(target)],
        "talk_backlog": [],
    }
    first = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert [hold["candidate_id"] for hold in first] == [CANDIDATE]
    sentinel_id = "auto_existing_non_target_hold"
    sentinel_hold = {
        "candidate_id": sentinel_id,
        "disposition": STALE_REVIEW_STATUS,
        "reason_code": "PRESERVE_EXACTLY",
        "candidate": {"candidate_id": sentinel_id, "opaque": [3, 1, 2]},
        "evidence": {"opaque": {"order": ["b", "a"]}},
    }
    state[REVIEW_STATE_FIELD]["holds"].insert(0, sentinel_hold)
    state[REVIEW_STATE_FIELD]["opaque_non_target_state"] = {
        "order": ["second", "first"]
    }
    non_target_holds = deepcopy(state[REVIEW_STATE_FIELD]["holds"])
    opaque_review_state = deepcopy(
        state[REVIEW_STATE_FIELD]["opaque_non_target_state"]
    )

    target_authority = _authority(target, published, registry)
    target_path = root / authority_relative_path(DISTINCT)
    target_path.write_text(
        json.dumps(target_authority, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    ignored_id = "auto_200000_100_200"
    ignored = {**deepcopy(target), "candidate_id": ignored_id, "hook": "ignored"}
    state["pending_talk"].append(ignored)
    ignored_path = root / authority_relative_path(ignored_id)
    ignored_path.write_text(
        json.dumps(
            _authority(ignored, published, registry),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal target authority")

    holds = hold_published_topic_collision_reviews(
        state,
        repo_root=root,
        publication_registry=registry,
        candidate_ids=(DISTINCT,),
    )

    assert holds[:2] == non_target_holds
    assert [hold["candidate_id"] for hold in holds] == [
        sentinel_id,
        CANDIDATE,
        DISTINCT,
    ]
    assert state[REVIEW_STATE_FIELD]["holds"][:2] == non_target_holds
    assert (
        state[REVIEW_STATE_FIELD]["opaque_non_target_state"]
        == opaque_review_state
    )
    assert state["pending_talk"] == [ignored]
    assert all(hold["candidate_id"] != ignored_id for hold in holds)
    assert holds[2]["evidence"]["published_candidate_id"] == PUBLISHED


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


def test_valid_scorecard_refresh_receipt_bridges_old_review_to_current_card(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(candidate, refreshed, published, authority, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert holds == [], holds[0]["evidence"]
    assert [row["candidate_id"] for row in state["pending_talk"]] == [
        CANDIDATE,
        DISTINCT,
    ]
    assert resolution["upload_authorized"] is False


def test_806_refreshed_resolution_asset_pins_the_observed_live_receipt() -> None:
    path = REPO_ROOT / refreshed_resolution_relative_path(CANDIDATE)
    resolution = json.loads(path.read_text(encoding="utf-8"))
    declared = resolution.pop("resolution_sha256")

    assert declared == canonical_sha256(resolution)
    assert resolution["schema_version"] == REFRESHED_RESOLUTION_SCHEMA
    assert resolution["candidate"]["selection_scorecard_sha256"] == (
        "sha256:f8e9e348dc09f3140a2e7abcf47ceafdf433e879477757dd18c4098d9b88333a"
    )
    assert resolution["scorecard_refresh"] == {
        "candidate_binding_sha256": (
            "sha256:a4527a2b40a7a222967faef71413e36e105130c5b08d7c3576e9f196bcf00b3b"
        ),
        "candidate_id": CANDIDATE,
        "input_provenance_sha256": (
            "sha256:c5faf5278cf78044fc525328e4d8a4a4314d626ba12883cf03ca70c5fa818e07"
        ),
        "new_selection_scorecard_sha256": (
            "sha256:f8e9e348dc09f3140a2e7abcf47ceafdf433e879477757dd18c4098d9b88333a"
        ),
        "old_selection_scorecard_sha256": (
            "sha256:5cd263fbf5d936fa4a6db372198d00a91f7a1792c44bfe7fd5d67a0ae2afa4fd"
        ),
        "provider_contract_sha256": (
            "sha256:1fa451d58e262f7aa5ca7e59708fb2edf8d02f4df1d5a61ace32bcbd99b27be6"
        ),
        "receipt_schema_version": "semantic-evidence-scorecard-refresh-receipt.v1",
        "receipt_sha256": (
            "sha256:26e0d7fb08624e411c3ca8981dd21401309e0b0f55d8cdf9431dd27851425729"
        ),
        "recording_date": DATE,
        "semantic_chat_evidence_sha256": (
            "sha256:767aa721676e0e56409556d05b666a684a5b2d90adcb3eae3484f73bc9b7b676"
        ),
        "semantic_chat_policy_sha256": (
            "sha256:dcddbafbb37693f270adbab5ecb8d7ebbe3c79c9a8bd0d332aa13dcc61750044"
        ),
        "semantic_chat_source_sha256": (
            "sha256:f86aa60d02d1ced94de49685a0ee37a3004d8d40bcd36d212a94e57dd60d63fb"
        ),
    }
    assert resolution["upload_authorized"] is False


@pytest.mark.parametrize(
    "drift",
    [
        "receipt_unsealed",
        "receipt_resealed",
        "candidate_window",
        "candidate_date",
        "candidate_scene",
        "candidate_hook",
        "current_scorecard",
        "resolution_refresh_binding",
        "published_bvid",
        "published_public_title",
        "registry_row",
        "upload_true",
    ],
)
def test_refreshed_resolution_drift_stays_candidate_scoped_and_fail_closed(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    drift: str,
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(candidate, refreshed, published, authority, registry)
    receipt = refreshed["semantic_evidence_scorecard_refresh"]

    if drift == "receipt_unsealed":
        receipt["attempts"][-1]["attempted_at"] = "2026-08-13T20:19:00Z"
    elif drift == "receipt_resealed":
        receipt["input_provenance"]["bcut_srt"]["sha256"] = "sha256:" + "a" * 64
        receipt["receipt_sha256"] = canonical_sha256(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    elif drift == "candidate_window":
        refreshed["start_ms"] += 1
    elif drift == "candidate_date":
        refreshed["segment_scene_context"]["recording_date"] = "2026-08-09"
    elif drift == "candidate_scene":
        refreshed["segment_scene_context"]["scene_kind"] = "talk"
    elif drift == "candidate_hook":
        refreshed["hook"] += "（漂移）"
    elif drift == "current_scorecard":
        refreshed["selection_scorecard"]["effective_score"] += 1
    elif drift == "resolution_refresh_binding":
        resolution["scorecard_refresh"]["receipt_sha256"] = "sha256:" + "9" * 64
    elif drift == "published_bvid":
        published["bvid"] = "BV1drifted111"
        published["publication_reconciliation"]["bvid"] = "BV1drifted111"
    elif drift == "published_public_title":
        resolution["published"]["public_title"] += "（漂移）"
        resolution["published"]["public_title_sha256"] = canonical_sha256(
            resolution["published"]["public_title"]
        )
    elif drift == "registry_row":
        registry["entries"][0]["note"] = "drifted"
    else:
        resolution["upload_authorized"] = True
    if drift in {
        "resolution_refresh_binding",
        "published_public_title",
        "upload_true",
    }:
        resolution["resolution_sha256"] = canonical_sha256(
            {key: value for key, value in resolution.items() if key != "resolution_sha256"}
        )

    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert len(holds) == 1
    assert holds[0]["candidate_id"] == CANDIDATE
    assert holds[0]["disposition"] == STALE_REVIEW_STATUS
    assert holds[0]["upload_authorized"] is False
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]


@pytest.mark.parametrize("tamper", ["provider_contract", "semantic_chat_policy"])
def test_resealed_refresh_contract_or_policy_tamper_stays_fail_closed(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    tamper: str,
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(
        candidate, refreshed, published, authority, registry
    )
    receipt = refreshed["semantic_evidence_scorecard_refresh"]
    provenance = receipt["input_provenance"]
    if tamper == "provider_contract":
        receipt["provider_contract"]["transport"] = "tampered"
        receipt["provider_contract_sha256"] = canonical_sha256(
            receipt["provider_contract"]
        )
        resolution["scorecard_refresh"]["provider_contract_sha256"] = receipt[
            "provider_contract_sha256"
        ]
    else:
        provenance["semantic_chat"]["policy_sha256"] = "sha256:" + "9" * 64
        refreshed["selection_scorecard"]["semantic_recall_chat_evidence"] = deepcopy(
            provenance["semantic_chat"]
        )
        receipt["new_scorecard_sha256"] = canonical_sha256(
            refreshed["selection_scorecard"]
        )
        resolution["candidate"]["selection_scorecard_sha256"] = receipt[
            "new_scorecard_sha256"
        ]
        resolution["scorecard_refresh"]["new_selection_scorecard_sha256"] = receipt[
            "new_scorecard_sha256"
        ]
        resolution["scorecard_refresh"]["semantic_chat_policy_sha256"] = provenance[
            "semantic_chat"
        ]["policy_sha256"]
        resolution["scorecard_refresh"]["input_provenance_sha256"] = canonical_sha256(
            provenance
        )
    receipt["receipt_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    resolution["scorecard_refresh"]["receipt_sha256"] = receipt["receipt_sha256"]
    resolution["resolution_sha256"] = canonical_sha256(
        {key: value for key, value in resolution.items() if key != "resolution_sha256"}
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
    }

    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )

    assert len(holds) == 1
    assert holds[0]["candidate_id"] == CANDIDATE
    assert holds[0]["disposition"] == STALE_REVIEW_STATUS
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]


def test_refreshed_resolution_can_wake_and_restore_one_already_parked_candidate(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(candidate, refreshed, published, authority, registry)
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
        "talk_superseded_attempts": [
            {**deepcopy(candidate), "status": "SUPERSEDED_BY_REQUEUE"}
        ],
    }
    held = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert held[0]["candidate_id"] == CANDIDATE
    assert [row["candidate_id"] for row in state["pending_talk"]] == [DISTINCT]

    path = root / refreshed_resolution_relative_path(CANDIDATE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal refreshed resolution")
    before_probe = deepcopy(state)

    assert published_topic_resolution_recovery_outstanding(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_READY_TO_RELEASE
    )
    assert state == before_probe
    assert release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert state[REVIEW_STATE_FIELD]["holds"] == []
    assert [row["candidate_id"] for row in state["pending_talk"]] == [
        DISTINCT,
        CANDIDATE,
    ]
    assert state["pending_talk"][-1]["selection_scorecard"] == refreshed["selection_scorecard"]
    assert state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]["upload_authorized"] is False
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_QUEUED
    )
    after_release = deepcopy(state)
    assert release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert state == after_release

    still_held = deepcopy(state)
    still_held[REVIEW_STATE_FIELD]["holds"] = [deepcopy(held[0])]
    assert (
        inspect_published_topic_resolution_recovery(
            still_held,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )

    production_preimage = deepcopy(state)
    state["pending_talk"] = [distinct]
    failed = deepcopy(refreshed)
    failed.update(
        {
            "status": "failed",
            "rc": 1,
            "failure_kind": "provider_transient",
            "failure_recoverable": True,
        }
    )
    state["picks"].append(failed)
    # Same immutable identity is not production authority: until the exact
    # queue->pick boundary is sealed, even a plausible recoverable row blocks.
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    drifted_preimage = deepcopy(production_preimage)
    drifted_preimage["pending_talk"][-1]["song_name_candidates"] = ["伪造输入"]
    assert not seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=drifted_preimage,
        repo_root=root,
        publication_registry=registry,
    )
    assert seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=production_preimage,
        repo_root=root,
        publication_registry=registry,
    )
    for corruption in ("wrong_kind", "wrong_order", "extra_field", "wrong_pick_sha"):
        invalid = deepcopy(state)
        ledger = invalid[RECOVERY_MARKER_FIELD]
        marker = ledger["entries"][CANDIDATE]
        transition = marker["transitions"][0]
        if corruption == "wrong_kind":
            transition["transition_kind"] = "RECOVERABLE_FAILED_PICK_REQUEUED"
        elif corruption == "wrong_order":
            transition["previous_row_binding"]["collection"] = "picks"
        elif corruption == "extra_field":
            transition["failed_pick_sha256"] = canonical_sha256(failed)
        else:
            transition["produced_pick_sha256"] = "sha256:" + "9" * 64
        transition["transition_sha256"] = canonical_sha256(
            {
                key: value
                for key, value in transition.items()
                if key != "transition_sha256"
            }
        )
        marker["marker_sha256"] = canonical_sha256(
            {key: value for key, value in marker.items() if key != "marker_sha256"}
        )
        ledger["ledger_sha256"] = canonical_sha256(
            {key: value for key, value in ledger.items() if key != "ledger_sha256"}
        )
        assert (
            inspect_published_topic_resolution_recovery(
                invalid,
                CANDIDATE,
                repo_root=root,
                publication_registry=registry,
            )
            == RECOVERY_BLOCKED
        )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_RETRY_PENDING
    )
    assert published_topic_resolution_recovery_outstanding(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )

    retry_preimage = deepcopy(state)
    retry_row = deepcopy(refreshed)
    retry_row.update(
        {
            "selected_repair": True,
            "retry_reason": "transient_infrastructure_failure",
            "talk_repair_retry_count": 0,
            "talk_transient_retry_count": 1,
            "recovery_source_record_sha256": canonical_sha256(failed),
        }
    )
    state["picks"] = [published]
    state["pending_talk"] = [distinct, retry_row]
    # The successor's self-declared source hash is insufficient by itself: the
    # old marker head still binds the original released queue row.
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    arbitrary = deepcopy(state)
    assert not advance_published_topic_resolution_recovery(
        arbitrary,
        CANDIDATE,
        pre_state=arbitrary,
        from_collection="picks",
        from_row=failed,
        to_collection="pending_talk",
        to_row=retry_row,
        repo_root=root,
        publication_registry=registry,
    )
    assert advance_published_topic_resolution_recovery(
        state,
        CANDIDATE,
        pre_state=retry_preimage,
        from_collection="picks",
        from_row=failed,
        to_collection="pending_talk",
        to_row=retry_row,
        repo_root=root,
        publication_registry=registry,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_QUEUED
    )
    entry = state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE]
    assert entry["current_row_binding"] == {
        "collection": "pending_talk",
        "row_sha256": canonical_sha256(retry_row),
    }
    assert entry["transitions"][1]["failed_pick_sha256"] == canonical_sha256(
        failed
    )

    nonrecoverable = deepcopy(state)
    nonrecoverable["pending_talk"] = [distinct]
    rejected_failure = deepcopy(retry_row)
    rejected_failure.update(
        {
            "status": "failed",
            "rc": 1,
            "failure_kind": "content_boundary",
            "failure_recoverable": False,
            "failure_recovery_fingerprint": "sha256:" + "4" * 64,
        }
    )
    nonrecoverable["picks"].append(rejected_failure)
    assert seal_published_topic_resolution_production_transition(
        nonrecoverable,
        CANDIDATE,
        pre_state=state,
        repo_root=root,
        publication_registry=registry,
    )
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, *_args: "sha256:" + "4" * 64,
        raising=False,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            nonrecoverable,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, *_args: "sha256:" + "5" * 64,
        raising=False,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            nonrecoverable,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_RETRY_PENDING
    )
    for invalid in (None, "sha256:bad"):
        invalid_fingerprint = deepcopy(state)
        invalid_fingerprint["pending_talk"] = [distinct]
        invalid_row = deepcopy(retry_row)
        invalid_row.update(
            {
                "status": "failed",
                "rc": 1,
                "failure_kind": "content_boundary",
                "failure_recoverable": False,
                "failure_recovery_fingerprint": invalid,
            }
        )
        invalid_fingerprint["picks"].append(invalid_row)
        assert seal_published_topic_resolution_production_transition(
            invalid_fingerprint,
            CANDIDATE,
            pre_state=state,
            repo_root=root,
            publication_registry=registry,
        )
        assert (
            inspect_published_topic_resolution_recovery(
                invalid_fingerprint,
                CANDIDATE,
                repo_root=root,
                publication_registry=registry,
            )
            == RECOVERY_BLOCKED
        )
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, *_args: "sha256:bad",
        raising=False,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            nonrecoverable,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    monkeypatch.setattr(
        RunnerProxy,
        "talk_failure_recovery_fingerprint",
        lambda _self, *_args: (_ for _ in ()).throw(
            ValueError("fingerprint unavailable")
        ),
        raising=False,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            nonrecoverable,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    rejected = deepcopy(state)
    rejected["pending_talk"] = [distinct]
    rejected_row = deepcopy(retry_row)
    rejected_row["segment"] = Path(str(rejected_row.pop("segment_path"))).name
    rejected_row.update(
        {
            "status": "candidate_rejected",
            "rc": 0,
            "rejection_reason": "talk_effective_duration_too_short",
        }
    )
    rejected["picks"].append(rejected_row)
    assert seal_published_topic_resolution_production_transition(
        rejected,
        CANDIDATE,
        pre_state=state,
        repo_root=root,
        publication_registry=registry,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            rejected,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )

    delivered_preimage = deepcopy(state)
    state["pending_talk"] = [distinct]
    delivered = deepcopy(retry_row)
    delivered.update({"status": "review_ready", "rc": 0})
    state["picks"].append(delivered)
    assert seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=delivered_preimage,
        repo_root=root,
        publication_registry=registry,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_CONVERGED
    )


def test_recovery_ledger_blocks_parallel_release_then_preserves_a_while_releasing_b(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate_a, published, candidate_b = rows
    registry = _registry()
    authority_a = _authority(candidate_a, published, registry)
    authority_b = _authority(candidate_b, published, registry)
    refreshed_a = _refreshed_candidate(candidate_a)
    refreshed_b = _refreshed_candidate(candidate_b)
    resolution_a = _refreshed_resolution(
        candidate_a, refreshed_a, published, authority_a, registry
    )
    resolution_b = _refreshed_resolution(
        candidate_b, refreshed_b, published, authority_b, registry
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority_a)
    authority_b_path = root / authority_relative_path(DISTINCT)
    authority_b_path.write_text(
        json.dumps(authority_b, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal second authority")
    state = {
        "picks": [published],
        "pending_talk": [refreshed_a, refreshed_b],
        "talk_backlog": [],
    }
    holds = hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    assert {hold["candidate_id"] for hold in holds} == {CANDIDATE, DISTINCT}
    for candidate_id, resolution in (
        (CANDIDATE, resolution_a),
        (DISTINCT, resolution_b),
    ):
        path = root / refreshed_resolution_relative_path(candidate_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal both refreshed resolutions")

    assert release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    production_preimage = deepcopy(state)
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            DISTINCT,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )

    state["pending_talk"] = []
    failed_a = deepcopy(refreshed_a)
    failed_a.update(
        {
            "status": "failed",
            "rc": 1,
            "failure_kind": "provider_transient",
            "failure_recoverable": True,
        }
    )
    state["picks"].append(failed_a)
    assert seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=production_preimage,
        repo_root=root,
        publication_registry=registry,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            DISTINCT,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )

    state = deepcopy(production_preimage)
    state["pending_talk"] = []
    delivered_a = deepcopy(refreshed_a)
    delivered_a.update({"status": "review_ready", "rc": 0})
    state["picks"].append(delivered_a)
    assert seal_published_topic_resolution_production_transition(
        state,
        CANDIDATE,
        pre_state=production_preimage,
        repo_root=root,
        publication_registry=registry,
    )
    annotation_preimage = deepcopy(state)
    delivered_a["session_id"] = "session-annotated-after-terminal"
    assert seal_published_topic_resolution_row_rebounds(
        state,
        pre_state=annotation_preimage,
        phase=RECOVERY_REBOUND_SESSION_ANNOTATION,
        repo_root=root,
        publication_registry=registry,
    )
    terminal_entry = deepcopy(state[RECOVERY_MARKER_FIELD]["entries"][CANDIDATE])
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            DISTINCT,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_READY_TO_RELEASE
    )
    assert release_resolved_published_topic_hold(
        state,
        DISTINCT,
        repo_root=root,
        publication_registry=registry,
    )
    ledger = state[RECOVERY_MARKER_FIELD]
    assert set(ledger["entries"]) == {CANDIDATE, DISTINCT}
    assert ledger["entries"][CANDIDATE] == terminal_entry
    assert (
        ledger["ledger_sha256"]
        == canonical_sha256(
            {key: value for key, value in ledger.items() if key != "ledger_sha256"}
        )
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            DISTINCT,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_QUEUED
    )


@pytest.mark.parametrize(
    "drift",
    [
        "arbitrary_queue_without_marker",
        "marker_queue_origin",
        "marker_original_hold_candidate",
        "marker_refresh_receipt",
        "marker_upload_authority",
        "queued_row",
        "duplicate_queue",
        "publication_registry",
        "published_state_bvid",
        "resolution_asset",
        "original_authority_asset",
    ],
)
def test_released_recovery_marker_is_exact_and_fail_closed(
    tmp_path: Path,
    rows: tuple[dict, dict, dict],
    drift: str,
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(
        candidate, refreshed, published, authority, registry
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
    }
    hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    resolution_path = root / refreshed_resolution_relative_path(CANDIDATE)
    resolution_path.parent.mkdir(parents=True, exist_ok=True)
    resolution_path.write_text(
        json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal refreshed resolution")
    assert release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_RELEASED_QUEUED
    )

    ledger = state[RECOVERY_MARKER_FIELD]
    marker = ledger["entries"][CANDIDATE]
    if drift == "arbitrary_queue_without_marker":
        state.pop(RECOVERY_MARKER_FIELD)
    elif drift == "marker_queue_origin":
        marker["queue_origin"] = "talk_backlog"
    elif drift == "marker_original_hold_candidate":
        marker["original_hold"]["candidate"]["hook"] += "（漂移）"
        marker["original_hold_sha256"] = canonical_sha256(marker["original_hold"])
    elif drift == "marker_refresh_receipt":
        marker["scorecard_refresh"]["receipt_sha256"] = "sha256:" + "9" * 64
    elif drift == "marker_upload_authority":
        marker["upload_authorized"] = True
    elif drift == "queued_row":
        state["pending_talk"][-1]["hook"] += "（漂移）"
    elif drift == "duplicate_queue":
        state["talk_backlog"].append(deepcopy(state["pending_talk"][-1]))
    elif drift == "publication_registry":
        registry["entries"][0]["note"] = "drifted after release"
    elif drift == "published_state_bvid":
        state["picks"][0]["bvid"] = "BV1drifted111"
        state["picks"][0]["publication_reconciliation"]["bvid"] = (
            "BV1drifted111"
        )
    elif drift == "resolution_asset":
        changed = json.loads(resolution_path.read_text(encoding="utf-8"))
        changed["asserted_at"] = "2026-08-13T14:00:01Z"
        changed["resolution_sha256"] = canonical_sha256(
            {key: value for key, value in changed.items() if key != "resolution_sha256"}
        )
        resolution_path.write_text(
            json.dumps(changed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "drift resolution")
    else:
        authority_path = root / authority_relative_path(CANDIDATE)
        changed = json.loads(authority_path.read_text(encoding="utf-8"))
        changed["interpretation"] = "drifted after release"
        changed["authority_sha256"] = canonical_sha256(
            {key: value for key, value in changed.items() if key != "authority_sha256"}
        )
        authority_path.write_text(
            json.dumps(changed, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "drift authority")
    if drift.startswith("marker_"):
        marker["marker_sha256"] = canonical_sha256(
            {key: value for key, value in marker.items() if key != "marker_sha256"}
        )
        ledger["ledger_sha256"] = canonical_sha256(
            {key: value for key, value in ledger.items() if key != "ledger_sha256"}
        )
    before_probe = deepcopy(state)

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert state == before_probe


def test_malformed_origin_queue_cannot_partially_release_a_valid_hold(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(
        candidate, refreshed, published, authority, registry
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
    }
    hold_published_topic_collision_reviews(
        state, repo_root=root, publication_registry=registry
    )
    resolution_path = root / refreshed_resolution_relative_path(CANDIDATE)
    resolution_path.parent.mkdir(parents=True, exist_ok=True)
    resolution_path.write_text(
        json.dumps(resolution, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "seal refreshed resolution")
    state["pending_talk"] = {"malformed": True}
    before = deepcopy(state)

    assert (
        inspect_published_topic_resolution_recovery(
            state,
            CANDIDATE,
            repo_root=root,
            publication_registry=registry,
        )
        == RECOVERY_BLOCKED
    )
    assert not release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert state == before


def test_drifted_refreshed_resolution_does_not_wake_or_mutate_parked_candidate(
    tmp_path: Path, rows: tuple[dict, dict, dict]
) -> None:
    candidate, published, distinct = rows
    registry = _registry()
    authority = _authority(candidate, published, registry)
    refreshed = _refreshed_candidate(candidate)
    resolution = _refreshed_resolution(candidate, refreshed, published, authority, registry)
    resolution["scorecard_refresh"]["candidate_binding_sha256"] = "sha256:" + "9" * 64
    resolution["resolution_sha256"] = canonical_sha256(
        {key: value for key, value in resolution.items() if key != "resolution_sha256"}
    )
    root = _sealed_repo(tmp_path, CANDIDATE, authority, resolution)
    state = {
        "picks": [published],
        "pending_talk": [refreshed, distinct],
        "talk_backlog": [],
    }
    hold_published_topic_collision_reviews(state, repo_root=root, publication_registry=registry)
    before = deepcopy(state)

    assert not published_topic_resolution_recovery_outstanding(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert not release_resolved_published_topic_hold(
        state,
        CANDIDATE,
        repo_root=root,
        publication_registry=registry,
    )
    assert state == before


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
    assert (
        hold_published_topic_collision_reviews(state, repo_root=root, publication_registry=registry)
        == []
    )
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
