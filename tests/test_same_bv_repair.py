from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.authorized_upload import UploadLockBusy, exclusive_upload_lock
from src.autoslice.same_bv_repair import (
    DuplicateBvid,
    JournalCorrupt,
    PlanInvalid,
    RemoteMutationError,
    append_journal,
    create_plan,
    initialise_journal,
    load_plan,
    normalise_snapshot,
    read_journal,
    repair_status,
    repair_step,
    run_repair,
    sha256_file,
    write_plan,
)

BVID = "BV1Mug46EEQz"
OLD_CID = 101
NEW_CID = 202
COVER_URL = "https://img.example/new-cover.png"


def _before_snapshot() -> dict:
    creator_metadata = {
        "title": "旧标题",
        "desc": "https://live.bilibili.com/\n简介",
        "tags": ["李豆沙", "直播切片"],
        "tid": 21,
        "copyright": 2,
        "source": "https://live.bilibili.com/",
        "cover": "https://img.example/old-cover.png",
    }
    public_metadata = {
        key: value for key, value in creator_metadata.items() if key != "source"
    }
    return {
        "creator": {
            "available": True,
            "bvid": BVID,
            "aid": 42,
            "state": 0,
            "state_desc": "开放浏览",
            "metadata": creator_metadata,
            "videos": [
                {"cid": OLD_CID, "filename": "old-file", "title": "旧标题"}
            ],
        },
        "public": {
            "available": True,
            "bvid": BVID,
            "aid": 42,
            "cid": OLD_CID,
            "state": 0,
            "metadata": public_metadata,
        },
        "section": {
            "available": True,
            "section_id": 9320779,
            "matches": [
                {
                    "bvid": BVID,
                    "aid": 42,
                    "cid": OLD_CID,
                    "title": "旧标题",
                }
            ],
        },
    }


def _manifest(tmp_path: Path) -> tuple[Path, dict]:
    video = tmp_path / "new.mp4"
    cover = tmp_path / "new.png"
    video.write_bytes(b"new exact video")
    cover.write_bytes(b"new exact cover")
    manifest = {
        "manifest_version": 3,
        "schema_version": "authorized-upload-manifest.v3",
        "video": {
            "path": str(video.resolve()),
            "sha256": sha256_file(video),
            "bytes": video.stat().st_size,
        },
        "cover": {
            "path": str(cover.resolve()),
            "sha256": sha256_file(cover),
            "bytes": cover.stat().st_size,
        },
        "title": "【李豆沙】新标题",
        "description": "https://live.bilibili.com/\n简介",
        "tags": ["李豆沙", "直播切片"],
        "publish_policy": {
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
        },
        "season": {
            "season_id": 8383206,
            "section_id": 9320779,
            "season_title": "小李切片",
        },
        "authorization": {"by": "Ivan", "quote": "尽量上传"},
    }
    path = tmp_path / "new.upload_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False) + "\n", encoding="utf-8")
    return path, manifest


def _plan_authority(tmp_path: Path, *, initialise: bool = True):
    manifest_path, manifest = _manifest(tmp_path)
    plan = create_plan(
        manifest_path=manifest_path,
        manifest=manifest,
        bvid=BVID,
        snapshot=_before_snapshot(),
    )
    plan_path = tmp_path / "repair.plan.json"
    write_plan(plan_path, plan)
    journal = tmp_path / "repair.journal.jsonl"
    if initialise:
        initialise_journal(journal, plan_path, plan)
    return manifest, plan, plan_path, journal


class FakeAdapter:
    def __init__(
        self,
        plan: dict,
        *,
        append_mode: str = "success",
        swap_mode: str = "success",
        publish_immediately: bool = True,
    ) -> None:
        self.plan = plan
        self.snapshot = copy.deepcopy(plan["before"])
        self.append_mode = append_mode
        self.swap_mode = swap_mode
        self.publish_immediately = publish_immediately
        self.append_calls = 0
        self.cover_calls = 0
        self.swap_calls = 0

    def observe(self, bvid: str, section_id: int) -> dict:
        assert bvid == BVID
        assert section_id == 9320779
        return copy.deepcopy(self.snapshot)

    def _append_new(self) -> None:
        self.snapshot["creator"]["videos"].append(
            {"cid": NEW_CID, "filename": "new-file", "title": "new-file"}
        )

    def append_existing(self, bvid: str, media_path: Path) -> None:
        self.append_calls += 1
        assert bvid == BVID
        assert media_path.read_bytes() == b"new exact video"
        if self.append_mode == "raise_before":
            raise RuntimeError("transport failed before known response")
        self._append_new()
        if self.append_mode == "crash_after":
            raise SystemExit("simulated process death after remote append")

    def prepare_cover(self, cover_path: Path) -> str:
        self.cover_calls += 1
        assert cover_path.read_bytes() == b"new exact cover"
        return COVER_URL

    def _make_creator_target(self, target_metadata: dict) -> None:
        target = copy.deepcopy(target_metadata)
        target["cover"] = "//img.example/new-cover.png"
        self.snapshot["creator"]["metadata"] = target
        self.snapshot["creator"]["videos"] = [
            {
                "cid": NEW_CID,
                "filename": "new-file",
                "title": target["title"],
            }
        ]
        self.snapshot["creator"]["state"] = -30
        self.snapshot["creator"]["state_desc"] = "审核中"

    def make_public_target(self) -> None:
        target = copy.deepcopy(self.plan["target_metadata"])
        target["cover"] = "//img.example/new-cover.png"
        self.snapshot["public"] = {
            "available": True,
            "bvid": BVID,
            "aid": 42,
            "cid": NEW_CID,
            "state": 0,
            "metadata": {
                key: value for key, value in target.items() if key != "source"
            },
        }
        self.snapshot["section"]["matches"] = [
            {
                "bvid": BVID,
                "aid": 42,
                "cid": NEW_CID,
                "title": target["title"],
            }
        ]

    def swap_keep_only(
        self,
        bvid: str,
        *,
        keep_cid: int,
        target_metadata: dict,
        cover_url: str,
    ) -> dict:
        self.swap_calls += 1
        assert bvid == BVID
        assert keep_cid == NEW_CID
        assert cover_url in {COVER_URL, "//img.example/new-cover.png"}
        if self.swap_mode == "21540_once" and self.swap_calls == 1:
            raise RemoteMutationError("稿件编辑中", code=21540)
        if self.swap_mode == "fatal":
            raise RemoteMutationError("permission denied", code=-403)
        self._make_creator_target(target_metadata)
        if self.publish_immediately:
            self.make_public_target()
        if self.swap_mode == "timeout_after_success":
            raise TimeoutError("response lost after successful edit")
        return {"code": 0}


def test_happy_path_verifies_exact_new_cid_metadata_and_section(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
    )

    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1
    assert adapter.swap_calls == 1
    states = [row["state"] for row in read_journal(journal)]
    assert states == [
        "PLANNED",
        "APPEND_INTENT",
        "APPEND_AMBIGUOUS",
        "TWO_P_READY",
        "SWAP_RETRYABLE",
        "CREATOR_SINGLE_NEW",
        "VERIFIED",
    ]
    verified = read_journal(journal)[-1]["details"]["live_snapshot"]
    assert verified["creator"]["videos"][0]["cid"] == NEW_CID
    assert verified["public"]["cid"] == NEW_CID
    assert verified["section"]["matches"][0]["cid"] == NEW_CID


def test_crash_after_append_response_resumes_by_polling_without_reappend(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, append_mode="crash_after")

    with pytest.raises(SystemExit, match="process death"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
    assert read_journal(journal)[-1]["state"] == "APPEND_INTENT"
    assert adapter.append_calls == 1

    adapter.append_mode = "success"
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=1,
        poll_seconds=0,
    )
    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1


def test_crash_before_append_effect_is_permanently_ambiguous_and_never_retries(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, append_mode="raise_before")

    first = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert first.state == "APPEND_AMBIGUOUS"
    assert adapter.append_calls == 1
    adapter.append_mode = "success"

    second = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    third = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert second.state == third.state == "APPEND_AMBIGUOUS"
    assert adapter.append_calls == 1
    assert adapter.snapshot["creator"]["videos"][0]["cid"] == OLD_CID


def test_edit_21540_retries_the_same_cid_and_payload(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, swap_mode="21540_once")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=1,
        poll_seconds=0,
    )

    assert result.state == "VERIFIED"
    assert adapter.append_calls == 1
    assert adapter.swap_calls == 2
    retry_rows = [
        row
        for row in read_journal(journal)
        if row["state"] == "SWAP_RETRYABLE"
    ]
    assert any("21540" in str(row["details"].get("reason")) for row in retry_rows)
    assert {row["details"]["new_video"]["cid"] for row in retry_rows} == {NEW_CID}
    assert {row["details"]["cover_url"] for row in retry_rows} == {COVER_URL}


def test_swap_timeout_but_live_success_advances_without_second_edit(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, swap_mode="timeout_after_success")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "VERIFIED"
    assert adapter.swap_calls == 1
    creator_row = next(
        row
        for row in read_journal(journal)
        if row["state"] == "CREATOR_SINGLE_NEW"
    )
    assert creator_row["details"]["swap_observation"] == (
        "live success after ambiguous error"
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda snapshot: snapshot["creator"]["videos"].append(
            {"cid": 303, "filename": "unknown", "title": "unknown"}
        ),
        lambda snapshot: snapshot["creator"]["videos"].__setitem__(
            1, {"cid": OLD_CID, "filename": "dup", "title": "dup"}
        ),
        lambda snapshot: snapshot["section"]["matches"].append(
            {
                "bvid": BVID,
                "aid": 42,
                "cid": OLD_CID,
                "title": "重复 BV",
            }
        ),
    ],
)
def test_unknown_three_p_duplicate_cid_or_duplicate_section_bvid_blocks(
    tmp_path, mutate
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    adapter._append_new()
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )
    mutate(adapter.snapshot)

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "BLOCKED_DRIFT"
    assert adapter.append_calls == 0
    assert adapter.swap_calls == 0


def test_wrong_public_cid_after_creator_swap_blocks(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
        wait_seconds=0,
    )
    assert result.state == "PUBLIC_PENDING"
    adapter.snapshot["public"]["cid"] = 999999

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "BLOCKED_DRIFT"


@pytest.mark.parametrize("field", ["title", "desc", "tid", "copyright", "source"])
def test_creator_metadata_drift_after_swap_blocks(tmp_path, field):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)
    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "PUBLIC_PENDING"
    adapter.snapshot["creator"]["metadata"][field] = "DRIFT"

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state == "BLOCKED_DRIFT"


def test_public_pending_then_verified_without_more_mutation(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, publish_immediately=False)

    first = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert first.state == "PUBLIC_PENDING"
    assert adapter.append_calls == adapter.swap_calls == 1

    adapter.make_public_target()
    second = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert second.state == "VERIFIED"
    assert adapter.append_calls == adapter.swap_calls == 1


def test_verified_terminal_is_idempotent_even_if_adapter_would_fail(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    assert (
        run_repair(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "VERIFIED"
    )
    counts = (adapter.append_calls, adapter.cover_calls, adapter.swap_calls)
    adapter.snapshot = {"corrupt": True}

    again = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert again.state == "VERIFIED"
    assert (adapter.append_calls, adapter.cover_calls, adapter.swap_calls) == counts
    assert read_journal(journal)[-1]["state"] == "VERIFIED"


def test_non_retryable_swap_error_blocks_without_a_second_edit(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan, swap_mode="fatal")

    result = run_repair(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "BLOCKED_DRIFT"
    assert adapter.swap_calls == 1


def test_journal_partial_row_hash_edit_and_invalid_transition_are_corruption(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    original = journal.read_bytes()
    journal.write_bytes(original.rstrip(b"\n"))
    with pytest.raises(JournalCorrupt, match="partial"):
        read_journal(journal)

    journal.write_bytes(original.replace(b"PLANNED", b"VERIFIED", 1))
    with pytest.raises(JournalCorrupt, match="hash mismatch"):
        read_journal(journal)

    journal.write_bytes(original)
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
    )
    # A syntactically valid but illegal VERIFIED transition must also fail.
    rows = read_journal(journal)
    body = {
        key: value for key, value in rows[-1].items() if key != "row_sha256"
    }
    body["seq"] = 3
    body["previous_row_sha256"] = rows[-1]["row_sha256"]
    body["state"] = "VERIFIED"
    import hashlib

    payload = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    body["row_sha256"] = hashlib.sha256(payload).hexdigest()
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
    with pytest.raises(JournalCorrupt, match="transition"):
        read_journal(journal)


def test_manifest_or_artifact_drift_invalidates_plan_before_remote_action(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    Path(plan["replacement"]["video"]["path"]).write_bytes(b"tampered")
    adapter = FakeAdapter(plan)

    with pytest.raises(Exception, match="hash drift"):
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        )
    assert adapter.append_calls == adapter.swap_calls == 0


def test_duplicate_bvid_plan_is_rejected_in_separate_repair_ledger(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    other = copy.deepcopy(plan)
    other["plan_id"] = "different-plan"
    other_path = tmp_path / "other.plan.json"
    write_plan(other_path, other)

    with pytest.raises(DuplicateBvid, match=BVID):
        initialise_journal(journal, other_path, other)


def test_shared_upload_lock_refuses_a_second_repair_owner(tmp_path):
    lock = tmp_path / "upload.lock"
    with exclusive_upload_lock(lock):
        with pytest.raises(UploadLockBusy, match="busy"):
            with exclusive_upload_lock(lock):
                pass


def test_status_and_dry_read_do_not_append_journal(tmp_path):
    manifest, _plan, plan_path, journal = _plan_authority(tmp_path)
    before = journal.read_bytes()

    result = repair_status(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
    )

    assert result.state == "PLANNED"
    assert result.details["next_action"] == "APPEND_EXISTING_ONCE"
    assert journal.read_bytes() == before


def test_plan_and_journal_files_are_create_only(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    assert load_plan(plan_path)["plan_id"] == plan["plan_id"]
    with pytest.raises(Exception, match="already exists"):
        write_plan(plan_path, plan)
    assert initialise_journal(journal, plan_path, plan)["state"] == "PLANNED"
    assert len(read_journal(journal)) == 1


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda snapshot: snapshot["public"]["metadata"].__setitem__(
                "title", "另一标题"
            ),
            "Creator and public metadata disagree",
        ),
        (
            lambda snapshot: snapshot["section"]["matches"][0].__setitem__(
                "title", "另一标题"
            ),
            "section title disagrees",
        ),
    ],
)
def test_planning_refuses_preexisting_cross_surface_metadata_drift(
    tmp_path, mutate, match
):
    manifest_path, manifest = _manifest(tmp_path)
    snapshot = _before_snapshot()
    mutate(snapshot)

    with pytest.raises(PlanInvalid, match=match):
        create_plan(
            manifest_path=manifest_path,
            manifest=manifest,
            bvid=BVID,
            snapshot=snapshot,
        )


def test_public_tags_failure_is_unavailable_not_an_empty_tag_observation():
    creator = {
        "archive": {
            "bvid": BVID,
            "aid": 42,
            "state": 0,
            "title": "旧标题",
            "desc": "简介",
            "tag": "李豆沙,直播切片",
            "tid": 21,
            "copyright": 2,
            "source": "https://live.bilibili.com/",
            "cover": "https://img.example/old-cover.png",
        },
        "videos": [{"cid": OLD_CID, "filename": "old", "title": "旧标题"}],
    }
    public = {
        "code": 0,
        "data": {
            "bvid": BVID,
            "aid": 42,
            "cid": OLD_CID,
            "state": 0,
            "title": "旧标题",
            "desc": "简介",
            "tid": 21,
            "copyright": 2,
            "pic": "https://img.example/old-cover.png",
        },
    }
    section = {
        "code": 0,
        "data": {
            "id": 9320779,
            "episodes": [
                {
                    "bvid": BVID,
                    "aid": 42,
                    "cid": OLD_CID,
                    "title": "旧标题",
                }
            ],
        },
    }

    snapshot = normalise_snapshot(
        bvid=BVID,
        section_id=9320779,
        creator_data=creator,
        public_payload=public,
        public_tags_payload={"code": -500},
        section_payload=section,
    )

    assert snapshot["public"]["available"] is False
    assert snapshot["public"]["metadata"]["tags"] == []


def test_transient_public_read_failure_after_append_polls_without_block_or_reappend(
    tmp_path,
):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    adapter._append_new()
    adapter.snapshot["public"]["available"] = False
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )

    assert result.state == "APPEND_INTENT"
    assert adapter.append_calls == 0
    assert adapter.swap_calls == 0
    assert read_journal(journal)[-1]["state"] == "APPEND_INTENT"


def test_protocol_relative_cover_url_survives_journal_resume(tmp_path):
    manifest, plan, plan_path, journal = _plan_authority(tmp_path)
    adapter = FakeAdapter(plan)
    adapter._append_new()
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="APPEND_INTENT",
        details={"remote_mutation": "simulated"},
    )
    assert (
        repair_step(
            plan_path=plan_path,
            journal=journal,
            manifest=manifest,
            adapter=adapter,
        ).state
        == "TWO_P_READY"
    )
    append_journal(
        journal,
        plan_path=plan_path,
        plan=plan,
        state="SWAP_RETRYABLE",
        details={
            "new_video": {
                "cid": NEW_CID,
                "filename": "new-file",
                "title": "new-file",
            },
            "cover_url": "//img.example/new-cover.png",
            "remote_mutation": False,
        },
    )

    result = repair_step(
        plan_path=plan_path,
        journal=journal,
        manifest=manifest,
        adapter=adapter,
    )
    assert result.state in {"CREATOR_SINGLE_NEW", "SWAP_RETRYABLE"}
    assert adapter.append_calls == 0
