from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import same_bv_repair as legacy_repair
from src.autoslice.same_bv_title_cover_adapter import BilibiliTitleCoverAdapter
from src.autoslice import same_bv_title_cover_authority as authority
from src.autoslice import same_bv_title_cover_journal as journal_binding
from src.autoslice import same_bv_title_cover_repair as repair
from src.autoslice.same_bv_cover_reconciliation import normalise_cover_url


BVID = "BV1s7326qEc9"
AID = 101
CID = 202
SEASON_ID = 8110001
SECTION_ID = 9110001
DISPLAY_NAME = "示例主播"
OLD_TITLE = "【示例主播】旧标题"
NEW_TITLE = "【示例主播】新标题"
OLD_COVER = "https://archive.biliimg.com/bfs/archive/aaaaaaaaaaaaaaaa.jpg"
NEW_COVER = "https://i0.hdslb.com/bfs/archive/bbbbbbbbbbbbbbbb.png"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: dict) -> Path:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _snapshot(*, title=OLD_TITLE, cover=OLD_COVER, section_title=None) -> dict:
    tags = sorted([DISPLAY_NAME, "切片"])
    metadata = {
        "title": title,
        "desc": "desc",
        "tags": tags,
        "tid": 21,
        "copyright": 2,
        "source": "https://live.bilibili.com/",
        "cover": normalise_cover_url(cover),
    }
    return {
        "creator": {
            "available": True,
            "bvid": BVID,
            "aid": AID,
            "state": 0,
            "state_desc": "开放浏览",
            "metadata": metadata,
            "videos": [{"cid": CID, "filename": "old", "title": title}],
        },
        "public": {
            "available": True,
            "bvid": BVID,
            "aid": AID,
            "cid": CID,
            "state": 0,
            "metadata": {k: v for k, v in metadata.items() if k != "source"},
        },
        "section": {
            "available": True,
            "section_id": SECTION_ID,
            "matches": [
                {
                    "bvid": BVID,
                    "aid": AID,
                    "cid": CID,
                    "title": section_title if section_title is not None else title,
                }
            ],
        },
    }


def _materialize_authority(tmp_path: Path) -> tuple[dict, Path]:
    cover = tmp_path / "new.cover.png"
    cover.write_bytes(b"reviewed title-cover bytes")
    manifest = {
        "artifact_id": "sample-title-cover",
        "title": OLD_TITLE,
        "description": "desc",
        "tags": [DISPLAY_NAME, "切片"],
        "publish_policy": {
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "season": {
            "lane": "talk",
            "season_title": "示例切片",
            "source": "talk:title-prefix",
        },
        "video": {"sha256": "v" * 64},
        "cover": {"sha256": "c" * 64},
    }
    manifest_path = _write(tmp_path / "manifest.json", manifest)
    uploaded = {
        "schema_version": "authorized-upload-result.v3",
        "status": "VERIFIED_PUBLIC",
        "artifact_id": "sample-title-cover",
        "manifest_sha256": _sha(manifest_path),
        "bvid": BVID,
        "aid": AID,
        "cid": CID,
        "title": OLD_TITLE,
        "video_sha256": "v" * 64,
        "cover_sha256": "c" * 64,
        "subtitle_sha256": "s" * 64,
    }
    uploaded_path = _write(tmp_path / "uploaded.json", uploaded)
    expected = {
        "title": OLD_TITLE,
        "description": "desc",
        "tags": [DISPLAY_NAME, "切片"],
        "tid": 21,
        "copyright": 2,
        "source": "https://live.bilibili.com/",
        "season_id": SEASON_ID,
        "section_id": SECTION_ID,
    }
    public_verify = {
        "schema_version": "authorized-upload-public-verify.v2",
        "status": "VERIFIED_PUBLIC",
        "problems": [],
        "manifest_title": OLD_TITLE,
        "bvid": BVID,
        "expected": expected,
        "public_view": {"title": OLD_TITLE, "aid": AID, "cid": CID},
        "public_tags": [DISPLAY_NAME, "切片"],
        "member_archive": {"title": OLD_TITLE, "bvid": BVID, "aid": AID},
        "section_api": {"episode_titles": [OLD_TITLE]},
    }
    public_path = _write(tmp_path / "public.json", public_verify)
    season_verify = {
        "schema_version": "authorized-upload-season-verify.v1",
        "status": "IN_SEASON_PUBLIC",
        "bvid": BVID,
        "aid": AID,
        "cid": CID,
        "title": OLD_TITLE,
        "season_id": SEASON_ID,
        "section_id": SECTION_ID,
    }
    season_path = _write(tmp_path / "season.json", season_verify)
    reconciliation = {
        "schema_version": "new-bv-publication-reconciliation-authority.v1",
        "status": "VERIFIED_PUBLIC",
        "candidate_id": "sample-title-cover",
        "recording_date": "2026-09-24",
        "title": OLD_TITLE,
        "bvid": BVID,
        "aid": AID,
        "cid": CID,
        "evidence": {
            "manifest": {"sha256": _sha(manifest_path)},
            "uploaded": {"payload": uploaded},
            "public_verify": {"payload": public_verify},
            "season_verify": {"payload": season_verify},
        },
    }
    reconciliation_path = _write(tmp_path / "reconciliation.json", reconciliation)
    review = {
        "status": "REVIEW_CONFLICT_UNRESOLVED",
        "cover_sha256": _sha(cover),
        "identity": "PASS",
        "single_image_verdict": {"pass": True},
        "comparison_verdict": {"pass": False},
        "originals_preserved": True,
    }
    review_path = _write(tmp_path / "review.json", review)
    fullres = {
        "schema_version": "cpa-frame-witness.v1",
        "status": "OBSERVED",
        "provider": "cpa",
        "model": "gpt-6-astra",
        "image_sha256": _sha(cover),
        "answer": json.dumps(
            {
                "observed_text_lines": ["主标题", "副标题"],
                "glyph_certainty": True,
                "decorations": [],
                "forbidden_elements": [],
                "ui_or_text_residuals": [],
                "identity_features": [],
                "identity_clear": True,
                "single_story_clear": True,
                "pass": True,
                "reason": "pass",
            },
            ensure_ascii=False,
        ),
    }
    fullres_path = _write(tmp_path / "fullres.json", fullres)
    decision = {"selected_title": NEW_TITLE, "pass": True, "single_story": True,
                "unfamiliar_reader_clear": True, "avoids_unproven_causality": True,
                "aligned_with_cover": True}
    prompt_path = tmp_path / "title-prompt.txt"
    prompt_path.write_text("Review the title against the bound source and cover")
    completion_path = _write(tmp_path / "title-completion.json", decision)
    title_review_path = _write(tmp_path / "title-review.json", {
        "schema_version": "synthetic-title-cpa-review.v1", "status": "PASS",
        "provider": "cpa", "model": "gpt-6-astra", "fallback_used": False,
        "candidate_id": "sample-title-cover",
        "public_target": {"bvid": BVID, "same_bv_only": True},
        "source_bindings": {"final_srt_sha256": "s" * 64},
        "decision": decision,
        "prompt_path": str(prompt_path), "prompt_sha256": _sha(prompt_path),
        "completion_path": str(completion_path), "completion_sha256": _sha(completion_path),
    })
    value = authority.build_authority(
        manifest_path=manifest_path,
        uploaded_path=uploaded_path,
        public_verify_path=public_path,
        season_verify_path=season_path,
        reconciliation_path=reconciliation_path,
        review_consumption_path=review_path,
        fullres_cpa_path=fullres_path,
        title_review_path=title_review_path,
        target_title=NEW_TITLE,
        target_cover_path=cover,
        authorized_by="operator",
        authorization_quote="same-BV title and cover revision only",
    )
    authority_path = tmp_path / "authority.json"
    authority.write_authority(authority_path, value)
    return value, authority_path


def _materialize_plan(tmp_path: Path):
    auth, authority_path = _materialize_authority(tmp_path)
    plan = repair.create_plan(
        authority_path=authority_path,
        authority=auth,
        snapshot=_snapshot(),
    )
    plan_path = tmp_path / "plan.json"
    journal = tmp_path / "journal.jsonl"
    repair.write_plan(plan_path, plan)
    repair.initialise_journal(journal, plan_path, plan)
    return auth, plan, plan_path, journal


def _target(*, section_title=NEW_TITLE):
    return _snapshot(title=NEW_TITLE, cover=NEW_COVER, section_title=section_title)


class FakeAdapter:
    def __init__(
        self,
        observations,
        *,
        edit_error: Exception | None = None,
        sync_error: Exception | None = None,
    ):
        self.observations = list(observations)
        self.last = self.observations[-1]
        self.edit_error = edit_error
        self.sync_error = sync_error
        self.prepare_calls = 0
        self.edit_calls = 0
        self.sync_calls = 0

    def observe(self, bvid, section_id):
        assert (bvid, section_id) == (BVID, SECTION_ID)
        if self.observations:
            self.last = self.observations.pop(0)
        return copy.deepcopy(self.last)

    def prepare_cover(self, path):
        assert path.name == "new.cover.png"
        self.prepare_calls += 1
        return NEW_COVER

    def edit_title_cover(self, bvid, *, expected_creator, target_title, cover_url):
        assert bvid == BVID
        assert expected_creator["videos"][0]["cid"] == CID
        assert target_title == NEW_TITLE
        assert cover_url == NEW_COVER
        self.edit_calls += 1
        if self.edit_error:
            raise self.edit_error
        return {"code": 0}

    def sync_section_title(
        self, bvid, section_id, *, expected_current_title, target_title
    ):
        assert (bvid, section_id) == (BVID, SECTION_ID)
        assert (expected_current_title, target_title) == (OLD_TITLE, NEW_TITLE)
        self.sync_calls += 1
        if self.sync_error:
            raise self.sync_error
        return {"code": 0}


def test_authority_preserves_small_comparison_fail_and_fullres_cpa_pass(tmp_path):
    value, path = _materialize_authority(tmp_path)
    assert value["scope"]["allowed_fields"] == ["title", "cover"]
    assert value["baseline"]["identity"]["cid"] == CID
    assert value["quality_evidence"]["old_comparison_preserved_fail"] is True
    assert value["quality_evidence"]["fullres_claims"]["pass"] is True
    assert authority.load_authority(path) == value


def test_authority_rejects_non_cpa_fullres_verdict(tmp_path):
    value, _path = _materialize_authority(tmp_path)
    receipt_path = Path(value["quality_evidence"]["fullres_cpa"]["path"])
    receipt = json.loads(receipt_path.read_text())
    receipt["provider"] = "agy"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(authority.TitleCoverAuthorityError, match="hash drift"):
        authority.validate_authority(value)


def test_run_changes_only_title_cover_keeps_cid_and_syncs_section_once(tmp_path):
    _auth, plan, plan_path, journal = _materialize_plan(tmp_path)
    target_without_section = _target(section_title=OLD_TITLE)
    adapter = FakeAdapter(
        [_snapshot(), _snapshot(), target_without_section, _target()]
    )
    result = repair.run(
        plan_path=plan_path,
        journal=journal,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert result.state == "VERIFIED"
    assert (adapter.prepare_calls, adapter.edit_calls, adapter.sync_calls) == (1, 1, 1)
    assert result.details["unchanged_cid"] == CID
    completed = repair.verify_live(
        plan_path=plan_path,
        journal=journal,
        adapter=FakeAdapter([_target()]),
        out=tmp_path / "completed.json",
    )
    assert completed["unchanged_cid"] == CID
    assert completed["new_title"] == NEW_TITLE
    assert completed["media_identity"] == plan["media_identity"]


def test_ambiguous_archive_edit_is_never_retried(tmp_path):
    _auth, _plan, plan_path, journal = _materialize_plan(tmp_path)
    adapter = FakeAdapter(
        [_snapshot(), _snapshot(), _snapshot()],
        edit_error=TimeoutError("lost response"),
    )
    first = repair.run(
        plan_path=plan_path,
        journal=journal,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert first.state == "PUBLIC_PENDING"
    assert adapter.edit_calls == 1
    second = repair.run(
        plan_path=plan_path,
        journal=journal,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert second.state == "PUBLIC_PENDING"
    assert adapter.edit_calls == 1


def test_ambiguous_section_sync_is_never_retried(tmp_path):
    _auth, _plan, plan_path, journal = _materialize_plan(tmp_path)
    stale = _target(section_title=OLD_TITLE)
    adapter = FakeAdapter(
        [_snapshot(), _snapshot(), stale, stale, stale],
        sync_error=TimeoutError("lost section response"),
    )
    first = repair.run(
        plan_path=plan_path,
        journal=journal,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert first.state == "SECTION_SYNC_AMBIGUOUS"
    assert adapter.sync_calls == 1
    second = repair.run(
        plan_path=plan_path,
        journal=journal,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert second.state == "SECTION_SYNC_AMBIGUOUS"
    assert adapter.sync_calls == 1


def test_live_drift_blocks_before_cover_upload(tmp_path):
    _auth, _plan, plan_path, journal = _materialize_plan(tmp_path)
    drift = _snapshot()
    drift["creator"]["metadata"]["desc"] = "external"
    adapter = FakeAdapter([drift])
    result = repair.run(
        plan_path=plan_path,
        journal=journal,
        adapter=adapter,
        wait_seconds=0,
        poll_seconds=0,
    )
    assert result.state == "BLOCKED_DRIFT"
    assert (adapter.prepare_calls, adapter.edit_calls, adapter.sync_calls) == (0, 0, 0)


def test_journal_tamper_is_rejected(tmp_path):
    _auth, _plan, plan_path, journal = _materialize_plan(tmp_path)
    row = json.loads(journal.read_text().splitlines()[0])
    row["details"]["remote_mutation"] = "tampered"
    journal.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(journal_binding.TitleCoverJournalCorrupt, match="hash/chain"):
        repair.status(plan_path=plan_path, journal=journal)


class FakeSession:
    def __init__(self, view_data):
        self.view_data = view_data
        self.build_calls = []
        self.edit_calls = []

    def archive_view(self, bvid):
        assert bvid == BVID
        return self.view_data

    def build_edit_payload(self, view_data, **kwargs):
        self.build_calls.append(kwargs)
        return {"aid": AID, "videos": view_data["videos"], **kwargs}

    def edit_archive(self, payload):
        self.edit_calls.append(payload)
        return {"code": 0}


def test_production_adapter_changes_only_title_part_title_and_cover():
    raw = {
        "archive": {
            "bvid": BVID,
            "aid": AID,
            "state": 0,
            "state_desc": "开放浏览",
            "title": OLD_TITLE,
            "desc": "desc",
            "tag": f"{DISPLAY_NAME},切片",
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
            "cover": OLD_COVER,
        },
        "videos": [{"cid": CID, "filename": "old", "title": OLD_TITLE}],
    }
    session = FakeSession(raw)
    adapter = BilibiliTitleCoverAdapter(
        session=session,
        http=lambda _url: {},
        view_url="{bvid}",
        tags_url="{bvid}",
        section_url="{section_id}",
    )
    expected = legacy_repair.normalise_creator_snapshot(
        bvid=BVID, creator_data=raw
    )
    assert adapter.edit_title_cover(
        BVID,
        expected_creator=expected,
        target_title=NEW_TITLE,
        cover_url=NEW_COVER,
    ) == {"code": 0}
    assert session.build_calls == [
        {
            "title": NEW_TITLE,
            "video_title": NEW_TITLE,
            "cover_url": NEW_COVER,
        }
    ]
    assert session.edit_calls[0]["videos"] == raw["videos"]


def test_fullres_missing_residual_findings_is_not_a_pass(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    receipt = json.loads(Path(value["quality_evidence"]["fullres_cpa"]["path"]).read_text())
    verdict = json.loads(receipt["answer"])
    verdict.pop("ui_or_text_residuals")
    receipt["answer"] = json.dumps(verdict)
    with pytest.raises(authority.TitleCoverAuthorityError, match="did not pass"):
        authority._parse_fullres_answer(receipt, value["target"]["cover"]["sha256"])


def test_fullres_observed_text_must_be_nonempty_strings(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    receipt = json.loads(Path(value["quality_evidence"]["fullres_cpa"]["path"]).read_text())
    verdict = json.loads(receipt["answer"])
    verdict["observed_text_lines"] = ["主标题", ""]
    receipt["answer"] = json.dumps(verdict, ensure_ascii=False)
    with pytest.raises(authority.TitleCoverAuthorityError, match="observed text is invalid"):
        authority._parse_fullres_answer(receipt, value["target"]["cover"]["sha256"])


def test_fullres_claims_freeze_receipt_model_and_text(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    claims = value["quality_evidence"]["fullres_claims"]
    assert claims["model"] == "gpt-6-astra"
    assert claims["observed_text_lines"] == ["主标题", "副标题"]


def test_authority_rejects_symlink_evidence(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    original = Path(value["target"]["cover"]["path"])
    link = tmp_path / "linked.png"
    link.symlink_to(original)
    descriptor = {**value["target"]["cover"], "path": str(link)}
    with pytest.raises(authority.TitleCoverAuthorityError, match="non-symlink"):
        authority.validate_descriptor(descriptor, "target cover")


def test_second_plan_cannot_corrupt_active_bvid_journal(tmp_path):
    _auth, plan, plan_path, journal = _materialize_plan(tmp_path)
    before = journal.read_bytes()
    second = {**plan, "plan_id": "another-plan"}
    second_path = tmp_path / "second-plan.json"
    repair.write_plan(second_path, second)
    with pytest.raises(journal_binding.TitleCoverJournalCorrupt, match="active"):
        repair.initialise_journal(journal, second_path, second)
    assert journal.read_bytes() == before
    assert repair.status(plan_path=plan_path, journal=journal).state == "PLANNED"


@pytest.mark.parametrize("field", ["title", "candidate_id", "bvid", "subtitle_sha256", "provider"])
def test_title_review_must_match_exact_target_and_source(tmp_path, field):
    value, _ = _materialize_authority(tmp_path)
    review = json.loads(Path(value["quality_evidence"]["title_review"]["path"]).read_text())
    baseline = copy.deepcopy(value["baseline"])
    title = NEW_TITLE
    if field == "title":
        title = "An unreviewed title"
    elif field == "bvid":
        baseline["identity"]["bvid"] = "BV1different"
    elif field == "provider":
        review["provider"] = "agy"
    else:
        baseline[field] = "different"
    with pytest.raises(authority.TitleCoverAuthorityError, match="exact target"):
        authority._validate_title_review(review, baseline, title)


def test_title_review_replays_original_completion(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    review = json.loads(Path(value["quality_evidence"]["title_review"]["path"]).read_text())
    Path(review["completion_path"]).write_text("{}")
    with pytest.raises(authority.TitleCoverAuthorityError, match="completion hash drift"):
        authority._validate_title_review(review, value["baseline"], NEW_TITLE)


def test_title_review_requires_versioned_title_review_schema(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    review = json.loads(Path(value["quality_evidence"]["title_review"]["path"]).read_text())
    review["schema_version"] = "unrelated-review.v1"
    with pytest.raises(authority.TitleCoverAuthorityError, match="exact target"):
        authority._validate_title_review(review, value["baseline"], NEW_TITLE)


def test_title_review_accepts_generic_versioned_schema(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    review = json.loads(Path(value["quality_evidence"]["title_review"]["path"]).read_text())
    assert review["schema_version"] == "synthetic-title-cpa-review.v1"
    authority._validate_title_review(review, value["baseline"], NEW_TITLE)


def test_parent_symlink_is_rejected(tmp_path):
    value, _ = _materialize_authority(tmp_path)
    linked = tmp_path / "parent-link"
    linked.symlink_to(tmp_path, target_is_directory=True)
    descriptor = {**value["target"]["cover"], "path": str(linked / "new.cover.png")}
    with pytest.raises(authority.TitleCoverAuthorityError, match="non-symlink"):
        authority.validate_descriptor(descriptor, "target cover")


def test_creator_part_title_is_candidate_id_not_archive_title(tmp_path):
    """Bilibili's Creator P-title keeps the candidate id during metadata edits."""
    value, _path = _materialize_authority(tmp_path)
    snapshot = _snapshot(title=value["baseline"]["old_title"])
    snapshot["creator"]["videos"][0]["title"] = value["baseline"]["candidate_id"]

    from src.autoslice.same_bv_title_cover_plan import baseline_snapshot_problems

    assert baseline_snapshot_problems(snapshot, value) == []
    snapshot["creator"]["videos"][0]["title"] = "unrelated-part-title"
    assert baseline_snapshot_problems(snapshot, value) == [
        "Creator page identity/title differs from baseline"
    ]
