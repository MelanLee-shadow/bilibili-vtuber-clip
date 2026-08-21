import copy
import hashlib
from pathlib import Path

import pytest

import scripts.repair_reviewed_covers as reviewed
from src.autoslice.surface_canon import CHANNEL_PROFILE


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_invalidate_document_preserves_media_authority_and_resolves_manual_title():
    source = {
        "schema_version": "shadow-publish-draft.v1",
        "candidate_id": "auto_test",
        "title": f"{CHANNEL_PROFILE.talk_title_prefix}错误标题",
        "title_source": "llm+style_asset",
        "cover_status": "AI_COVER_READY",
        "cover_path": "/delivery/old.cover.png",
        "cover_generation": {"title": f"{CHANNEL_PROFILE.talk_title_prefix}错误标题"},
        "cover_repair_binding": {"path": "/old.binding.json"},
        "public_text_surface_authority_consumption": {"stale": True},
        "artifact_hashes": {
            "burned_video_sha256": "sha256:" + "1" * 64,
            "subtitle_sha256": "sha256:" + "2" * 64,
            "cover_sha256": "sha256:" + "3" * 64,
        },
        "upload_enabled": False,
        "reason_codes": [],
    }
    original = copy.deepcopy(source)

    updated = reviewed._invalidate_document(
        source,
        title=f"{CHANNEL_PROFILE.talk_title_prefix}去彩排前连问三遍：你们还要来找我玩，好不好？",
    )

    assert source == original
    assert updated["title"] == f"{CHANNEL_PROFILE.talk_title_prefix}去彩排前连问三遍：你们还要来找我玩，好不好？"
    assert updated["title_source"] == "job_title"
    assert updated["title_authority_status"] == "RESOLVED_MANUAL"
    assert updated["cover_text"] == "去彩排前连问三遍\n你们还要来找我玩，好不好？"
    assert updated["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert updated["upload_enabled"] is False
    assert "cover_path" not in updated
    assert "cover_generation" not in updated
    assert "cover_repair_binding" not in updated
    assert "public_text_surface_authority_consumption" not in updated
    assert "cover_sha256" not in updated["artifact_hashes"]
    assert updated["artifact_hashes"]["burned_video_sha256"] == original["artifact_hashes"]["burned_video_sha256"]
    assert updated["artifact_hashes"]["subtitle_sha256"] == original["artifact_hashes"]["subtitle_sha256"]


def test_invalidate_document_accepts_hash_bound_short_cover_copy():
    source = {
        "schema_version": "shadow-publish-draft.v1",
        "artifact_hashes": {"burned_video_sha256": "sha256:" + "1" * 64},
        "reason_codes": [],
    }

    updated = reviewed._invalidate_document(
        source,
        title=f"{CHANNEL_PROFILE.talk_title_prefix}完整归档标题保留上下文",
        cover_text="短梗字\n保留问号？",
    )

    assert updated["title"] == f"{CHANNEL_PROFILE.talk_title_prefix}完整归档标题保留上下文"
    assert updated["cover_text"] == "短梗字\n保留问号？"


def test_manual_title_repair_plan_requires_the_sealed_public_text_authority() -> None:
    plan_path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/cover_repair_plans/2026-08-11-auto-173005-934-1166-manual-title-cover.v1.json"
    )
    plan, _sha = reviewed.load_plan(plan_path)
    authority = plan["title_repair_authorities"][0]
    assert authority["candidate_id"] == "auto_173005_934_1166"
    assert authority["title"] == "【李豆沙】经小李判断，薇欧拉对阿拉蕾就是铁暗恋！"
    assert authority["subtitle"]["sha256"] == (
        "sha256:1a668a899257407685c91629cc254918a594663af8b55cccb0932db16d0aa5d1"
    )
    route = plan["cover_route_authorities"][0]
    assert route["selected_treatment"] == "cpa_redraw"
    assert route["screenshot_direct_rejected"] is True
    assert route["screenshot_polish_rejected"] is True


def test_cover_route_authority_reads_all_three_active_publish_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate_id = "auto_test"
    title = f"{CHANNEL_PROFILE.talk_title_prefix}旧标题"
    mp4 = tmp_path / "candidate.mp4"
    reference = tmp_path / "reference.png"
    mp4.write_bytes(b"frozen-media")
    reference.write_bytes(b"reference-pixels")
    reference_sha = "sha256:" + _sha(reference.read_bytes())
    witness_sha = "sha256:" + "4" * 64
    generation = {
        "route_decision": {
            "selected_treatment": "cpa_redraw",
            "source_composition_witness_sha256": witness_sha,
            "alternatives": [
                {"treatment": "screenshot_direct", "status": "REJECTED"},
                {"treatment": "screenshot_polish", "status": "REJECTED"},
            ],
        },
        "source_composition_verification": {
            "reference_path": str(reference),
            "reference_sha256": reference_sha,
            "witness_receipt_sha256": witness_sha,
        },
    }

    def active_documents(value):
        return [
            (tmp_path / "delivery.record.json", {"publish_staging": {"cover_generation": value}}),
            (tmp_path / "source.record.json", {"publish_staging": {"cover_generation": copy.deepcopy(value)}}),
            (
                tmp_path / "candidate.publish.json",
                {
                    "schema_version": "shadow-publish-draft.v1",
                    "cover_generation": copy.deepcopy(value),
                },
            ),
        ]

    plan = {
        "cover_route_authorities": [
            {
                "candidate_id": candidate_id,
                "source_reference_path": str(reference),
                "source_reference_sha256": reference_sha,
                "source_composition_witness_sha256": witness_sha,
                "selected_treatment": "cpa_redraw",
                "screenshot_direct_rejected": True,
                "screenshot_polish_rejected": True,
            }
        ]
    }
    monkeypatch.setattr(reviewed, "delivered_paths", lambda _date, _record: (mp4, reference))
    monkeypatch.setattr(
        reviewed,
        "_active_cover_documents",
        lambda **_kwargs: active_documents(generation),
    )
    kwargs = {
        "plan": plan,
        "date": "2026-08-11",
        "records": {candidate_id: {"title": title}},
    }
    reviewed._validate_cover_route_authorities(**kwargs)

    missing_shadow = active_documents(generation)
    missing_shadow[2][1].pop("cover_generation")
    monkeypatch.setattr(reviewed, "_active_cover_documents", lambda **_kwargs: missing_shadow)
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="route comparison evidence is missing"):
        reviewed._validate_cover_route_authorities(**kwargs)

    drifted_shadow = active_documents(generation)
    drifted_shadow[2][1]["cover_generation"]["route_decision"]["selected_treatment"] = (
        "screenshot_direct"
    )
    monkeypatch.setattr(reviewed, "_active_cover_documents", lambda **_kwargs: drifted_shadow)
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="route evidence drifted"):
        reviewed._validate_cover_route_authorities(**kwargs)


def test_invalidate_state_record_persists_reviewed_cover_diversity_slot():
    record = {
        "candidate_id": "auto_test",
        "title": f"{CHANNEL_PROFILE.talk_title_prefix}旧标题",
        "cover_status": "AI_COVER_READY",
    }

    reviewed._invalidate_state_record(
        record,
        row={
            "title": f"{CHANNEL_PROFILE.talk_title_prefix}新标题",
            "cover_diversity_slot": 4,
        },
        plan_sha256="a" * 64,
    )

    assert record["title"] == f"{CHANNEL_PROFILE.talk_title_prefix}新标题"
    assert record["cover_diversity_slot"] == 4
    assert record["cover_status"] == "BLOCKED_AI_COVER_REQUIRED"


def _transaction_fixture(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    base = tmp_path / "base"
    date = "2026-07-10"
    candidate = "auto_test"
    delivery = root / "lidousha" / date
    active = base / "out" / date / candidate
    delivery.mkdir(parents=True)
    active.mkdir(parents=True)
    target = delivery / "clip.record.json"
    target.write_bytes(b"old\n")
    blob = active / "transaction" / "intended.bin"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"new\n")
    journal_path = active / "transaction" / "journal.json"
    plan = {
        "date": date,
        "invalidations": [
            {
                "candidate_id": candidate,
                "documents": [{"path": str(target)}],
            }
        ],
    }
    journal = {
        "schema_version": reviewed.TRANSACTION_SCHEMA,
        "status": "PREPARED",
        "date": date,
        "plan_sha256": "a" * 64,
        "code_fingerprint": "sha256:" + "b" * 64,
        "upload_enabled": False,
        "entries": [
            {
                "target": str(target),
                "intended_blob": str(blob),
                "intended_sha256": _sha(blob.read_bytes()),
            }
        ],
    }
    monkeypatch.setattr(reviewed, "ROOT", root)
    monkeypatch.setattr(reviewed, "BASE", base)
    return plan, journal, journal_path, target, blob


def test_invalidation_transaction_rolls_forward_and_is_idempotent(tmp_path, monkeypatch):
    plan, journal, journal_path, target, _blob = _transaction_fixture(tmp_path, monkeypatch)
    reviewed._atomic_write_json_file(journal_path, journal)

    committed = reviewed._commit_transaction(
        journal_path=journal_path,
        journal=journal,
        plan=plan,
        plan_sha256="a" * 64,
        code_fingerprint="sha256:" + "b" * 64,
        state_is_bound=False,
    )
    assert committed["status"] == "COMMITTED"
    assert target.read_bytes() == b"new\n"
    reviewed._commit_transaction(
        journal_path=journal_path,
        journal=committed,
        plan=plan,
        plan_sha256="a" * 64,
        code_fingerprint="sha256:" + "b" * 64,
        state_is_bound=False,
    )

    target.write_bytes(b"new bound cover document\n")
    reviewed._commit_transaction(
        journal_path=journal_path,
        journal=committed,
        plan=plan,
        plan_sha256="a" * 64,
        code_fingerprint="sha256:" + "b" * 64,
        state_is_bound=True,
    )
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="drifted before state"):
        reviewed._commit_transaction(
            journal_path=journal_path,
            journal=committed,
            plan=plan,
            plan_sha256="a" * 64,
            code_fingerprint="sha256:" + "b" * 64,
            state_is_bound=False,
        )


def test_invalidation_transaction_rejects_blob_drift_and_target_escape(tmp_path, monkeypatch):
    plan, journal, journal_path, target, blob = _transaction_fixture(tmp_path, monkeypatch)
    blob.write_bytes(b"tampered\n")
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="invalid invalidation"):
        reviewed._commit_transaction(
            journal_path=journal_path,
            journal=journal,
            plan=plan,
            plan_sha256="a" * 64,
            code_fingerprint="sha256:" + "b" * 64,
            state_is_bound=False,
        )

    blob.write_bytes(b"new\n")
    escaped = tmp_path / "escaped.json"
    escaped.write_bytes(b"old\n")
    plan["invalidations"][0]["documents"][0]["path"] = str(escaped)
    journal["entries"][0]["target"] = str(escaped)
    with pytest.raises(reviewed.ReviewedCoverRepairError, match="escapes active roots"):
        reviewed._commit_transaction(
            journal_path=journal_path,
            journal=journal,
            plan=plan,
            plan_sha256="a" * 64,
            code_fingerprint="sha256:" + "b" * 64,
            state_is_bound=False,
        )


@pytest.mark.parametrize(
    "name",
    [
        "2026-07-10.reviewed.v1.json",
        "2026-07-22.background-diversity-and-title.v1.json",
        "2026-07-22.full-replay-rerun-reviewed.v1.json",
        "2026-07-22.full-replay-title-scale.v1.json",
    ],
)
def test_checked_in_historical_cover_plans_cannot_be_executed(name):
    path = (
        Path(__file__).resolve().parents[1]
        / "assets/lidousha/cover_repair_plans/history"
        / name
    )

    with pytest.raises(
        reviewed.ReviewedCoverRepairError,
        match="historical evidence only",
    ):
        reviewed.load_plan(path)


def test_run_preflights_every_selected_candidate_before_state_or_transaction_mutation(
    monkeypatch,
):
    date = "2026-08-08"
    plan = {
        "date": date,
        "repair_candidates": [
            {"candidate_id": "auto_b"},
            {"candidate_id": "auto_a"},
        ],
        "invalidations": [{"candidate_id": "auto_a"}],
    }
    calls = []

    monkeypatch.setattr(reviewed, "load_plan", lambda _path: (plan, "a" * 64))
    monkeypatch.setattr(reviewed, "_assert_ledger", lambda _plan: None)

    def publication_block(candidate_id, *, recording_date):
        calls.append((candidate_id, recording_date))
        return "published candidate" if candidate_id == "auto_a" else None

    monkeypatch.setattr(
        reviewed,
        "cover_maintenance_block_reason",
        publication_block,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("publication preflight must run before state or transaction work")

    for name in (
        "read_state",
        "pipeline_fingerprint",
        "_prepare_transaction",
        "_commit_transaction",
        "write_state",
        "write_reports",
        "_atomic_write_json_file",
        "repair_covers",
    ):
        monkeypatch.setattr(reviewed, name, forbidden)

    with pytest.raises(
        reviewed.ReviewedCoverRepairError,
        match="publication preflight refused before invalidation",
    ):
        reviewed.run(Path("unused.json"))

    assert calls == [("auto_a", date), ("auto_b", date)]


def test_run_publication_preflight_includes_repair_only_candidates(monkeypatch):
    date = "2026-08-08"
    plan = {
        "date": date,
        "repair_candidates": [
            {"candidate_id": "auto_a"},
            {"candidate_id": "auto_b"},
        ],
        "invalidations": [{"candidate_id": "auto_a"}],
    }
    calls = []

    monkeypatch.setattr(reviewed, "load_plan", lambda _path: (plan, "a" * 64))
    monkeypatch.setattr(reviewed, "_assert_ledger", lambda _plan: None)

    def publication_block(candidate_id, *, recording_date):
        calls.append((candidate_id, recording_date))
        return "held candidate" if candidate_id == "auto_b" else None

    monkeypatch.setattr(
        reviewed,
        "cover_maintenance_block_reason",
        publication_block,
    )
    monkeypatch.setattr(
        reviewed,
        "read_state",
        lambda _date: (_ for _ in ()).throw(
            AssertionError("repair-only candidate must block before state read")
        ),
    )

    with pytest.raises(
        reviewed.ReviewedCoverRepairError,
        match="publication preflight refused before invalidation",
    ):
        reviewed.run(Path("unused.json"))

    assert calls == [("auto_a", date), ("auto_b", date)]
