import hashlib
import json
from pathlib import Path

import pytest

from scripts import plan_recovery_review_rerun as planner
from scripts import free_session_autoslice as runner


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "assets/lidousha/recovery_publication_authority.v1.json"
ASSET_SHA256 = (
    "sha256:0bbb26c63c30b1e30af13e33d5513c49aa10b98afa8730ee9761f59865317e30"
)
DAILY_850_ASSET = (
    ROOT
    / "assets/lidousha/recovery_publication_authority_2026-07-24_850.v1.json"
)
DAILY_850_ASSET_SHA256 = (
    "sha256:d22b34c3365a8daa71fb10bf54cf1207971d9c52e26142b840c02321f6baabcd"
)
DAILY_1493_ASSET = (
    ROOT
    / "assets/lidousha/recovery_publication_authority_2026-07-25_1493.v1.json"
)
DAILY_1493_ASSET_SHA256 = (
    "sha256:9f84633d9db12f37159030a104ff0d3b6876646e393da99f78684182f005dc5f"
)
JAPANESE_PRONOUN_ASSET = (
    ROOT
    / "assets/lidousha/recovery_publication_authority_2026-07-30_japanese_pronoun.v1.json"
)
CANDIDATE_IDS = {
    "auto_193450_3573_3665",
    "auto_193450_672_945",
    "auto_193450_1863_2056",
    "auto_193450_1573_1672",
    "auto_193450_1475_1543",
}
EXPECTED_ENDS = {
    "auto_193450_3573_3665": 3_665_850,
    "auto_193450_672_945": 951_900,
    "auto_193450_1863_2056": 2_056_480,
    "auto_193450_1573_1672": 1_672_970,
    "auto_193450_1475_1543": 1_543_760,
}
OLD_FINGERPRINT = "sha256:" + "1" * 64
NEW_FINGERPRINT = "sha256:" + "2" * 64
DATE = "2026-07-22"


def _load(candidate_ids: set[str]):
    return planner._load_recovery_publication_contract(
        queued_candidate_ids=candidate_ids,
        publication_asset=ASSET,
        expected_publication_authority_sha256=ASSET_SHA256,
        repo_root=ROOT,
    )


def _record(
    candidate_id: str,
    *,
    start_ms: int,
    end_ms: int,
    status: str = "review_ready",
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "status": status,
        "rc": 0,
        "bundle_lifecycle": "CURRENT",
        "bundle_compliance": "COMPLIANT",
        "pipeline_fingerprint": OLD_FINGERPRINT,
        "segment": "official.mp4",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "hook": candidate_id,
        "confidence": 0.9,
        "selection_scorecard": {
            "status": "VALID",
            "tier": 1,
            "effective_score": 80.0,
        },
        "session_relation_authority": {
            "state": "CONFIRMED",
            "participants": ["李豆沙", "南町"],
        },
        "session_id": "fixture-20260722",
    }


def _planner_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    requested_candidate_ids: list[str],
) -> tuple[list[str], Path, Path]:
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    (source_base / "state").mkdir(parents=True)
    (target_base / "recordings" / DATE).mkdir(parents=True)
    (target_base / "cache" / DATE).mkdir(parents=True)
    (target_base / "repo").symlink_to(ROOT, target_is_directory=True)
    (target_base / "recordings" / DATE / "official.mp4").write_bytes(
        b"official-media"
    )
    (target_base / "cache" / DATE / "official.bcut.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n字幕\n",
        encoding="utf-8",
    )
    current = [
        _record(
            "auto_193450_3573_3665",
            start_ms=3_573_000,
            end_ms=3_665_000,
        ),
        _record(
            "auto_193450_672_945",
            start_ms=672_000,
            end_ms=945_000,
        ),
        _record(
            "auto_193450_1863_2056",
            start_ms=1_863_000,
            end_ms=2_056_000,
        ),
        _record(
            "auto_193450_1573_1672",
            start_ms=1_573_000,
            end_ms=1_672_000,
        ),
        _record(
            "auto_193450_6577_6695",
            start_ms=6_577_000,
            end_ms=6_695_000,
        ),
    ]
    backlog = [
        _record(
            "auto_193450_1475_1543",
            start_ms=1_475_000,
            end_ms=1_543_000,
            status="reserve",
        )
    ]
    state = {
        "date": DATE,
        "status": "review_ready",
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "pending_talk": [],
        "picks": current,
        "talk_backlog": backlog,
        "talk_superseded_attempts": [],
    }
    source_state = source_base / "state" / f"{DATE}.json"
    source_state.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_sha = (
        "sha256:" + hashlib.sha256(source_state.read_bytes()).hexdigest()
    )

    monkeypatch.setenv("AUTOSLICE_BASE", "planner-test-original-base")
    monkeypatch.setenv("AUTOSLICE_REC_ROOT", "planner-test-original-rec")
    monkeypatch.setattr(runner, "BASE", target_base)
    monkeypatch.setattr(
        runner,
        "REC_ROOT",
        target_base / "recordings",
    )
    monkeypatch.setattr(
        runner,
        "talk_pipeline_fingerprint",
        lambda _candidate_id: NEW_FINGERPRINT,
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 7_000_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda _segment, source_sha256=None: {
            "chat_jsonl": None,
            "structured_chat_required": False,
            "chat_binding_status": "OPTIONAL_ABSENT",
        },
    )

    args = [
        "--source-base",
        str(source_base),
        "--target-base",
        str(target_base),
        "--date",
        DATE,
        "--expected-source-state-sha256",
        source_sha,
        "--expected-old-fingerprint",
        OLD_FINGERPRINT,
        "--expected-new-fingerprint",
        NEW_FINGERPRINT,
    ]
    for candidate_id in requested_candidate_ids:
        args.extend(["--candidate-id", candidate_id])
    args.extend(
        [
            "--suppress-candidate-id",
            "auto_193450_6577_6695",
            "--suppression-authority",
            "Ivan: 已有同题材视频，不再制作连线谜题",
            "--replacement-candidate-id",
            "auto_193450_1475_1543",
            "--replacement-selection-authority",
            "Ivan: 明确要求制作脑瓜崩切片",
            "--publication-authority-asset",
            str(ASSET),
            "--expected-publication-authority-sha256",
            ASSET_SHA256,
        ]
    )
    return args, target_base, source_state


def test_v10_contract_binds_exact_five_candidates_and_reviewed_ends():
    authorities, ends, end_authority = _load(CANDIDATE_IDS)

    assert set(authorities) == CANDIDATE_IDS
    assert ends == EXPECTED_ENDS
    assert end_authority == next(
        iter(
            {
                str(authority["registry_authority"])
                for authority in authorities.values()
            }
        )
    )
    assert all(
        authority["required_given_end_ms"] == EXPECTED_ENDS[candidate_id]
        for candidate_id, authority in authorities.items()
    )


def test_single_published_850_contract_binds_existing_bv_and_exact_end():
    authorities, ends, end_authority = (
        planner._load_recovery_publication_contract(
            queued_candidate_ids={"auto_193129_850_940"},
            publication_asset=DAILY_850_ASSET,
            expected_publication_authority_sha256=(
                DAILY_850_ASSET_SHA256
            ),
            repo_root=ROOT,
        )
    )

    authority = authorities["auto_193129_850_940"]
    assert ends == {"auto_193129_850_940": 940_490}
    assert end_authority == authority["registry_authority"]
    assert authority["boundary_end_mode"] == "exact_source_pin"
    assert authority["bvid"] == "BV1ec3A6bEWF"
    assert authority["cid"] == 40_357_990_267


def test_single_published_1493_contract_binds_existing_bv_and_exact_end():
    authorities, ends, end_authority = (
        planner._load_recovery_publication_contract(
            queued_candidate_ids={"auto_195000_1493_1579"},
            publication_asset=DAILY_1493_ASSET,
            expected_publication_authority_sha256=(
                DAILY_1493_ASSET_SHA256
            ),
            repo_root=ROOT,
        )
    )

    authority = authorities["auto_195000_1493_1579"]
    assert ends == {"auto_195000_1493_1579": 1_579_550}
    assert end_authority == authority["registry_authority"]
    assert authority["boundary_end_mode"] == "exact_source_pin"
    assert authority["bvid"] == "BV1zzgd6JEHe"
    assert authority["cid"] == 40_331_906_310


def test_japanese_pronoun_contract_pins_complete_reviewed_public_interval():
    raw = JAPANESE_PRONOUN_ASSET.read_bytes()
    authorities, ends, end_authority = (
        planner._load_recovery_publication_contract(
            queued_candidate_ids={"auto_142942_496_618"},
            publication_asset=JAPANESE_PRONOUN_ASSET,
            expected_publication_authority_sha256=(
                "sha256:" + hashlib.sha256(raw).hexdigest()
            ),
            repo_root=ROOT,
        )
    )

    authority = authorities["auto_142942_496_618"]
    assert ends == {"auto_142942_496_618": 627_010}
    assert end_authority == authority["registry_authority"]
    assert authority["boundary_end_mode"] == "exact_source_pin"
    assert authority["bvid"] == "BV1zk386LEjC"
    assert authority["cid"] == 40_453_148_097


def test_single_published_projection_isolates_target_without_suppressing_others():
    target = _record(
        "auto_193129_850_940",
        start_ms=850_020,
        end_ms=940_490,
    )
    other = _record(
        "auto_183122_1209_1410",
        start_ms=1_209_000,
        end_ms=1_410_000,
    )
    pending_other = {"cid": "auto_190124_1571_1804"}
    state = {
        "run_mode": "DAILY",
        "upload_allowed": False,
        "status": "review_ready_retry_wait",
        "picks": [target, other],
        "pending_talk": [pending_other],
        "talk_backlog": [{"candidate_id": "reserve"}],
        "talk_superseded_attempts": [
            {"candidate_id": "auto_193129_850_940", "status": "failed"},
            {"candidate_id": "other-history", "status": "failed"},
        ],
        "songs": [{"candidate_id": "song_1"}],
        "pending_song": [{"candidate_id": "song_2"}],
        "song_backlog": [{"candidate_id": "song_3"}],
        "song_selection_backlog": [{"candidate_id": "song_4"}],
    }

    projected = planner._project_single_published_repair_state(
        state,
        candidate_id="auto_193129_850_940",
        source_state_sha256="sha256:" + "a" * 64,
        delivered_statuses=runner.DELIVERED_TALK_STATUSES,
    )

    assert state["picks"] == [target, other]
    assert state["pending_talk"] == [pending_other]
    assert projected["run_mode"] == "RECOVERY_REVIEW"
    assert projected["upload_allowed"] is False
    assert [row["candidate_id"] for row in projected["picks"]] == [
        "auto_193129_850_940"
    ]
    assert projected["pending_talk"] == []
    assert projected["talk_backlog"] == []
    assert projected["songs"] == []
    assert projected["pending_song"] == []
    assert projected["song_backlog"] == []
    assert projected["song_selection_backlog"] == []
    assert projected.get("talk_user_suppressions") is None
    assert [
        row["candidate_id"]
        for row in projected["talk_superseded_attempts"]
    ] == ["auto_193129_850_940"]
    projection = projected["single_published_repair_projection"]
    assert projection["excluded_pick_candidate_ids"] == [
        "auto_183122_1209_1410"
    ]
    assert projection["excluded_pending_candidate_ids"] == [
        "auto_190124_1571_1804"
    ]
    assert projection["excluded_rows_disposition"] == (
        "SOURCE_STATE_UNCHANGED_OUTSIDE_REPAIR_TARGET"
    )


def test_single_published_projection_rejects_candidate_already_pending():
    state = {
        "run_mode": "DAILY",
        "upload_allowed": False,
        "picks": [
            _record(
                "auto_193129_850_940",
                start_ms=850_020,
                end_ms=940_490,
            )
        ],
        "pending_talk": [{"cid": "auto_193129_850_940"}],
    }

    with pytest.raises(
        SystemExit,
        match="candidate is already pending",
    ):
        planner._project_single_published_repair_state(
            state,
            candidate_id="auto_193129_850_940",
            source_state_sha256="sha256:" + "a" * 64,
            delivered_statuses=runner.DELIVERED_TALK_STATUSES,
        )


def test_single_published_projection_accepts_valid_exact_recovery_source():
    target = _record(
        "auto_193450_1863_2056",
        start_ms=1_863_760,
        end_ms=2_056_480,
    )
    other = _record(
        "auto_193450_3573_3665",
        start_ms=3_573_000,
        end_ms=3_665_000,
    )
    selection_contract = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": [
            "auto_193450_1863_2056",
            "auto_193450_3573_3665",
        ],
    }
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "picks": [target, other],
        "pending_talk": [],
        "talk_selection_contract": selection_contract,
        "delivery_rerun_plan": {
            "schema_version": "recovery-review-talk-rerun-plan.v5",
            "talk_selection_contract": selection_contract,
        },
    }

    projected = planner._project_single_published_repair_state(
        state,
        candidate_id="auto_193450_1863_2056",
        source_state_sha256="sha256:" + "a" * 64,
        delivered_statuses=runner.DELIVERED_TALK_STATUSES,
    )

    assert [row["candidate_id"] for row in projected["picks"]] == [
        "auto_193450_1863_2056"
    ]
    assert projected.get("talk_selection_contract") is None
    assert projected.get("delivery_rerun_plan") is None
    projection = projected["single_published_repair_projection"]
    assert projection["source_state_kind"] == "EXACT_RECOVERY_REVIEW"
    assert projection["source_talk_selection_contract_sha256"].startswith(
        "sha256:"
    )
    assert projection["excluded_pick_candidate_ids"] == [
        "auto_193450_3573_3665"
    ]


def test_single_published_projection_rejects_mismatched_recovery_plan():
    selection_contract = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": ["auto_193450_1863_2056"],
    }
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "picks": [
            _record(
                "auto_193450_1863_2056",
                start_ms=1_863_760,
                end_ms=2_056_480,
            )
        ],
        "pending_talk": [],
        "talk_selection_contract": selection_contract,
        "delivery_rerun_plan": {
            "schema_version": "recovery-review-talk-rerun-plan.v7",
            "talk_selection_contract": {
                **selection_contract,
                "candidate_ids": [],
            },
        },
    }

    with pytest.raises(
        SystemExit,
        match="source recovery contract is invalid",
    ):
        planner._project_single_published_repair_state(
            state,
            candidate_id="auto_193450_1863_2056",
            source_state_sha256="sha256:" + "a" * 64,
            delivered_statuses=runner.DELIVERED_TALK_STATUSES,
        )


def test_single_published_projection_rejects_unknown_recovery_plan_schema():
    selection_contract = {
        "schema_version": "talk-selection-contract.v1",
        "mode": "EXACT_CANDIDATE_SET_NO_BACKFILL",
        "candidate_ids": ["auto_193450_1863_2056"],
    }
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "picks": [
            _record(
                "auto_193450_1863_2056",
                start_ms=1_863_760,
                end_ms=2_056_480,
            )
        ],
        "pending_talk": [],
        "talk_selection_contract": selection_contract,
        "delivery_rerun_plan": {
            "schema_version": "recovery-review-talk-rerun-plan.v4",
            "talk_selection_contract": selection_contract,
        },
    }

    with pytest.raises(
        SystemExit,
        match="source recovery contract is invalid",
    ):
        planner._project_single_published_repair_state(
            state,
            candidate_id="auto_193450_1863_2056",
            source_state_sha256="sha256:" + "a" * 64,
            delivered_statuses=runner.DELIVERED_TALK_STATUSES,
        )


@pytest.mark.parametrize(
    "candidate_ids",
    [
        CANDIDATE_IDS - {"auto_193450_1475_1543"},
        CANDIDATE_IDS | {"auto_unexpected_sixth"},
    ],
)
def test_v10_contract_rejects_missing_or_extra_candidate(candidate_ids):
    with pytest.raises(
        SystemExit,
        match=(
            "RECOVERY_PUBLICATION_CANDIDATE_SET_MISMATCH"
            "|RECOVERY_PUBLICATION_CANDIDATE_MISSING"
        ),
    ):
        _load(candidate_ids)


def test_v10_planner_main_writes_exact_hash_bound_state_and_receipt(
    tmp_path, monkeypatch
):
    requested = [
        "auto_193450_3573_3665",
        "auto_193450_672_945",
        "auto_193450_1863_2056",
        "auto_193450_1573_1672",
    ]
    args, target_base, _ = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=requested,
    )

    assert planner.main(args) == 0

    target_state_path = target_base / "state" / f"{DATE}.json"
    receipt_path = (
        target_base
        / "reports"
        / f"recovery-review-rerun-plan-{DATE}.json"
    )
    state = json.loads(target_state_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    exact_ids = [
        *requested,
        "auto_193450_1475_1543",
    ]
    assert state["upload_allowed"] is False
    assert state["talk_selection_contract"]["candidate_ids"] == exact_ids
    assert receipt["talk_selection_contract"]["candidate_ids"] == exact_ids
    assert receipt["given_end_ms_by_candidate"] == EXPECTED_ENDS
    assert (
        set(receipt["recovery_publication_authorities_by_candidate"])
        == CANDIDATE_IDS
    )
    assert [row["cid"] for row in state["pending_talk"]] == exact_ids
    assert {
        row["cid"]: row["given_end_ms"]
        for row in state["pending_talk"]
    } == EXPECTED_ENDS


def test_v10_planner_main_missing_candidate_creates_no_target_state(
    tmp_path, monkeypatch
):
    args, target_base, _ = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=[
            "auto_193450_3573_3665",
            "auto_193450_672_945",
            "auto_193450_1863_2056",
        ],
    )

    with pytest.raises(
        SystemExit,
        match="RECOVERY_PUBLICATION_CANDIDATE_SET_MISMATCH",
    ):
        planner.main(args)

    assert not (target_base / "state" / f"{DATE}.json").exists()
    assert not (
        target_base
        / "reports"
        / f"recovery-review-rerun-plan-{DATE}.json"
    ).exists()


def test_single_published_projection_main_uses_external_recording_tree(
    tmp_path, monkeypatch
):
    date = "2026-07-24"
    candidate_id = "auto_193129_850_940"
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    recording_root = tmp_path / "canonical-recordings"
    (source_base / "state").mkdir(parents=True)
    (source_base / "cpa.env").write_text(
        "CPA_BASE_URL=https://example.invalid/v1\nCPA_API_KEY=test\n",
        encoding="utf-8",
    )
    (target_base / "cache" / date).mkdir(parents=True)
    (target_base / "repo").symlink_to(ROOT, target_is_directory=True)
    (recording_root / date).mkdir(parents=True)
    (recording_root / date / "official.mp4").write_bytes(b"official-media")
    (target_base / "cache" / date / "official.bcut.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n字幕\n",
        encoding="utf-8",
    )
    target = _record(
        candidate_id,
        start_ms=850_020,
        end_ms=940_490,
    )
    other = _record(
        "auto_183122_1209_1410",
        start_ms=1_209_000,
        end_ms=1_410_000,
    )
    state = {
        "date": date,
        "status": "review_ready_retry_wait",
        "run_mode": "DAILY",
        "upload_allowed": False,
        "pending_talk": [{"cid": "auto_190124_1571_1804"}],
        "picks": [target, other],
        "talk_backlog": [],
        "talk_superseded_attempts": [],
        "songs": [{"candidate_id": "song_unrelated"}],
    }
    source_state = source_base / "state" / f"{date}.json"
    source_state.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    source_sha = (
        "sha256:" + hashlib.sha256(source_state.read_bytes()).hexdigest()
    )

    monkeypatch.setattr(runner, "BASE", target_base)
    monkeypatch.setattr(runner, "REC_ROOT", recording_root)
    monkeypatch.setattr(
        runner,
        "talk_pipeline_fingerprint",
        lambda _candidate_id: NEW_FINGERPRINT,
    )
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 1_800_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: None)
    monkeypatch.setattr(
        runner,
        "resolve_structured_chat_binding",
        lambda _segment, source_sha256=None: {
            "chat_jsonl": None,
            "structured_chat_required": False,
            "chat_binding_status": "OPTIONAL_ABSENT",
        },
    )

    assert (
        planner.main(
            [
                "--source-base",
                str(source_base),
                "--target-base",
                str(target_base),
                "--target-recordings-root",
                str(recording_root),
                "--project-single-published-repair",
                "--date",
                date,
                "--expected-source-state-sha256",
                source_sha,
                "--expected-old-fingerprint",
                OLD_FINGERPRINT,
                "--expected-new-fingerprint",
                NEW_FINGERPRINT,
                "--candidate-id",
                candidate_id,
                "--publication-authority-asset",
                str(DAILY_850_ASSET),
                "--expected-publication-authority-sha256",
                DAILY_850_ASSET_SHA256,
            ]
        )
        == 0
    )

    target_state = json.loads(
        (target_base / "state" / f"{date}.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = json.loads(
        (
            target_base
            / "reports"
            / f"recovery-review-rerun-plan-{date}.json"
        ).read_text(encoding="utf-8")
    )
    assert target_state["talk_selection_contract"]["candidate_ids"] == [
        candidate_id
    ]
    assert [row["cid"] for row in target_state["pending_talk"]] == [
        candidate_id
    ]
    assert target_state["picks"] == []
    assert target_state["songs"] == []
    assert target_state["single_published_repair_projection"][
        "excluded_pick_candidate_ids"
    ] == ["auto_183122_1209_1410"]
    assert receipt["target_recordings_root"] == str(recording_root)
    assert receipt["external_cpa_env"] == {
        "schema_version": "recovery-external-cpa-env-binding.v1",
        "status": "BOUND",
        "binding": "SYMLINK_EXTERNAL_AUTHORITY",
        "source_path": str(source_base / "cpa.env"),
        "resolved_authority_path": str(source_base / "cpa.env"),
        "target_path": str(target_base / "cpa.env"),
    }
    assert (target_base / "cpa.env").is_symlink()
    assert (target_base / "cpa.env").resolve() == (
        source_base / "cpa.env"
    ).resolve()
    assert json.loads(source_state.read_text(encoding="utf-8")) == state


def test_single_published_projection_follows_existing_external_cpa_binding(
    tmp_path,
):
    authority = tmp_path / "production-cpa.env"
    authority.write_text(
        "CPA_BASE_URL=https://example.invalid/v1\nCPA_API_KEY=test\n",
        encoding="utf-8",
    )
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    source_base.mkdir()
    target_base.mkdir()
    (source_base / "cpa.env").symlink_to(authority)

    receipt = planner._bind_external_cpa_env(
        source_base=source_base,
        target_base=target_base,
    )

    assert receipt == {
        "schema_version": "recovery-external-cpa-env-binding.v1",
        "status": "BOUND",
        "binding": "SYMLINK_EXTERNAL_AUTHORITY",
        "source_path": str(source_base / "cpa.env"),
        "resolved_authority_path": str(authority),
        "target_path": str(target_base / "cpa.env"),
    }
    assert (target_base / "cpa.env").is_symlink()
    assert (target_base / "cpa.env").resolve() == authority


def test_external_cpa_binding_rejects_dangling_source_symlink(tmp_path):
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    source_base.mkdir()
    (source_base / "cpa.env").symlink_to(tmp_path / "missing-cpa.env")

    with pytest.raises(
        SystemExit,
        match="source CPA environment symlink is invalid",
    ):
        planner._bind_external_cpa_env(
            source_base=source_base,
            target_base=target_base,
        )

    assert not (target_base / "cpa.env").exists()


def test_single_published_projection_rejects_multi_date_recording_root(
    tmp_path, monkeypatch
):
    args, _target_base, _source_state = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=[
            "auto_193450_3573_3665",
            "auto_193450_672_945",
            "auto_193450_1863_2056",
            "auto_193450_1573_1672",
        ],
    )
    recording_root = tmp_path / "multi-date-recordings"
    (recording_root / DATE).mkdir(parents=True)
    (recording_root / "2026-07-24").mkdir()
    args.extend(
        [
            "--project-single-published-repair",
            "--target-recordings-root",
            str(recording_root),
        ]
    )

    with pytest.raises(
        SystemExit,
        match="target recordings root must expose only",
    ):
        planner.main(args)
