import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import plan_recovery_review_rerun as planner
from scripts import free_session_autoslice as runner
import src.autoslice.delivery_recovery as delivery_recovery
import src.autoslice.published_cover_carry as published_cover_carry
from src.autoslice.delivery_recovery import (
    RecoveryReviewRerunError,
    plan_current_talk_recovery_rerun,
)


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
QIXI_SAME_BV_ASSET = (
    ROOT / "assets/lidousha/daily_same_bv_publication_authority.v1.json"
)
QIXI_SAME_BV_ASSET_SHA256 = (
    "sha256:88fd8a35607f9d2e46999bd5fcc7044e2c547e08edf5aa711c9e32965828f3b4"
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
            "participants": ["主播", "嘉宾"],
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


def _without_suppression_or_replacement_options(args: list[str]) -> list[str]:
    return args[: args.index("--suppress-candidate-id")] + args[
        args.index("--publication-authority-asset") :
    ]


def _bcut_copy_fixture(tmp_path: Path) -> tuple[Path, Path, dict, Path, str]:
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    date = "2026-08-17"
    source_bcut = source_base / "cache" / date / "official.bcut.srt"
    source_bcut.parent.mkdir(parents=True)
    source_bcut.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    target_base.mkdir()
    record = {"segment": "official.mp4"}
    return (
        source_base,
        target_base,
        record,
        source_bcut,
        "sha256:" + hashlib.sha256(source_bcut.read_bytes()).hexdigest(),
    )


def _qixi_single_published_main_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], Path, Path, Path]:
    date = "2026-08-17"
    candidate_id = "auto_113022_354_496"
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    recording_root = tmp_path / "recordings"
    (source_base / "state").mkdir(parents=True)
    (source_base / "cache" / date).mkdir(parents=True)
    (source_base / "cpa.env").write_text("CPA_API_KEY=test\n", encoding="utf-8")
    source_bcut = source_base / "cache" / date / "official.bcut.srt"
    source_bcut.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    target_base.mkdir()
    (target_base / "repo").symlink_to(ROOT, target_is_directory=True)
    (recording_root / date).mkdir(parents=True)
    (recording_root / date / "official.mp4").write_bytes(b"official-media")
    target = _record(
        candidate_id,
        start_ms=354_630,
        end_ms=496_420,
        status="published",
    )
    target["prepublication_status"] = "review_ready"
    state = {
        "date": date,
        "run_mode": "DAILY",
        "upload_allowed": False,
        "picks": [target],
        "pending_talk": [],
        "talk_backlog": [],
        "talk_superseded_attempts": [],
    }
    source_state = source_base / "state" / f"{date}.json"
    source_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    source_state_sha256 = "sha256:" + hashlib.sha256(source_state.read_bytes()).hexdigest()
    source_bcut_sha256 = "sha256:" + hashlib.sha256(source_bcut.read_bytes()).hexdigest()

    monkeypatch.setattr(runner, "BASE", target_base)
    monkeypatch.setattr(runner, "REC_ROOT", recording_root)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: NEW_FINGERPRINT)
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
    return (
        [
            "--source-base", str(source_base), "--target-base", str(target_base),
            "--target-recordings-root", str(recording_root), "--project-single-published-repair",
            "--expected-source-bcut-sha256", source_bcut_sha256,
            "--date", date, "--expected-source-state-sha256", source_state_sha256,
            "--expected-old-fingerprint", OLD_FINGERPRINT, "--expected-new-fingerprint", NEW_FINGERPRINT,
            "--candidate-id", candidate_id, "--publication-authority-asset", str(QIXI_SAME_BV_ASSET),
            "--expected-publication-authority-sha256", QIXI_SAME_BV_ASSET_SHA256,
        ],
        source_state,
        target_base,
        source_bcut,
    )


def _sealed_qixi_cover_carry_main_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[str], Path, Path, str]:
    """A clean temporary Git authority, not an uncommitted test override.

    The real carry asset is deliberately only usable after its own commit.  This
    fixture keeps that property while testing planner/main end-to-end with tiny
    synthetic bytes: the authority asset itself is committed to a fresh Git
    repository and its state/public/cover graph is fully hash bound.
    """

    date = "2026-08-17"
    candidate_id = "auto_113022_354_496"
    title = "【李豆沙】李豆沙公布七夕安排，中午甜甜甜晚上苦苦苦，一套PUA直播要让kmx集体分号！"
    repo_root = tmp_path / "sealed-repo"
    (repo_root / "assets/lidousha").mkdir(parents=True)
    (repo_root / "scripts").mkdir()
    (repo_root / "reports/authorized_uploads/qixi").mkdir(parents=True)
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    recording_root = tmp_path / "recordings"
    (source_base / "state").mkdir(parents=True)
    (source_base / "cache" / date).mkdir(parents=True)
    (source_base / "cpa.env").write_text("CPA_API_KEY=test\n", encoding="utf-8")
    source_bcut = source_base / "cache" / date / "official.bcut.srt"
    source_bcut.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    source_artifacts = tmp_path / "public-source-artifacts"
    source_artifacts.mkdir()
    payloads = {
        "cover": b"8c-public-cover-fixture",
        "pre_overlay": b"pre-overlay",
        "title_mask": b"title-mask",
        "route_background": b"route-background",
        "reference": b"reference",
        "host_witness": b"host-witness",
        "source_composition_receipt": b"composition-receipt",
    }
    paths = {}
    for name, payload in payloads.items():
        path = source_artifacts / f"{name}.bin"
        path.write_bytes(payload)
        paths[name] = path
    def digest(payload: bytes) -> str:
        return "sha256:" + hashlib.sha256(payload).hexdigest()
    bindings = {
        name: {
            "source_path": str(paths[name]),
            "sha256": digest(payload),
            "bytes": len(payload),
        }
        for name, payload in payloads.items()
    }
    generation = {
        "final_cover": str(paths["cover"]),
        "final_cover_sha256": bindings["cover"]["sha256"],
        "pre_overlay_path": str(paths["pre_overlay"]),
        "pre_overlay_sha256": bindings["pre_overlay"]["sha256"],
        "rendered_text_pixels": {
            "mask_path": str(paths["title_mask"]),
            "mask_sha256": bindings["title_mask"]["sha256"],
        },
        "screenshot_graphic_poster": {
            "output_path": str(paths["route_background"]),
            "output_sha256": bindings["route_background"]["sha256"],
        },
        "ai_background": str(paths["route_background"]),
        "ai_background_sha256": bindings["route_background"]["sha256"],
        "reference_image": str(paths["reference"]),
        "reference_sha256": bindings["reference"]["sha256"],
        "final_host_identity_verification": {
            "comparison_path": str(paths["host_witness"]),
            "comparison_sha256": bindings["host_witness"]["sha256"],
        },
        "source_composition_receipt": {
            "path": str(paths["source_composition_receipt"]),
            "sha256": bindings["source_composition_receipt"]["sha256"],
        },
        "route_decision": {"host_identity_required": True},
    }
    target = _record(candidate_id, start_ms=354_630, end_ms=496_420, status="published")
    target.update(
        {
            "prepublication_status": "review_ready",
            "bvid": "BV1Ud8F6fECS",
            "aid": 117126140527747,
            "published_cid": 41087534673,
            "title": title,
            "cover_path": str(paths["cover"]),
            "cover_sha256": bindings["cover"]["sha256"],
            "cover_generation": generation,
        }
    )
    state = {
        "date": date,
        "run_mode": "DAILY",
        "upload_allowed": False,
        "picks": [target],
        "pending_talk": [],
        "talk_backlog": [],
        "talk_superseded_attempts": [],
    }
    source_state = source_base / "state" / f"{date}.json"
    source_state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_sha = digest(source_state.read_bytes())
    public_verify = {
        "schema_version": "authorized-upload-public-verify.v2",
        "status": "VERIFIED_PUBLIC",
        "bvid": target["bvid"],
        "manifest_title": title,
        "public_view": {"aid": target["aid"], "cid": target["published_cid"], "title": title},
        "member_archive": {"bvid": target["bvid"], "aid": target["aid"], "title": title},
        "expected": {"title": title},
        "section_api": {"episode_titles": [title]},
    }
    public_verify_path = repo_root / "reports/authorized_uploads/qixi/public.json"
    public_verify_path.write_text(json.dumps(public_verify, ensure_ascii=False), encoding="utf-8")
    authority = {
        "schema_version": "daily-same-bv-published-cover-carry-authority.v1",
        "authority": "fixture",
        "entries": [{
            "candidate_id": candidate_id,
            "recording_date": date,
            "source_state_sha256": source_sha,
            "published_identity": {
                "bvid": target["bvid"], "aid": target["aid"], "published_cid": target["published_cid"],
                "title": title, "public_verify_repo_path": "reports/authorized_uploads/qixi/public.json",
                "public_verify_sha256": digest(public_verify_path.read_bytes()),
            },
            "cover_generation_sha256": digest(json.dumps(generation, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
            "artifacts": bindings,
        }],
    }
    authority_path = repo_root / "assets/lidousha/daily_same_bv_published_cover_carry_authority.v1.json"
    authority_path.write_text(json.dumps(authority, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for command in (("git", "init", "-q", str(repo_root)), ("git", "-C", str(repo_root), "add", "."),
                    ("git", "-C", str(repo_root), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "sealed carry fixture")):
        subprocess.run(command, check=True)
    assert subprocess.run(("git", "-C", str(repo_root), "status", "--porcelain"), check=True, capture_output=True).stdout == b""
    target_base.mkdir()
    (target_base / "repo").symlink_to(repo_root, target_is_directory=True)
    (recording_root / date).mkdir(parents=True)
    (recording_root / date / "official.mp4").write_bytes(b"official-media")
    monkeypatch.setattr(planner, "__file__", str(repo_root / "scripts/plan_recovery_review_rerun.py"))
    monkeypatch.setattr(runner, "BASE", target_base)
    monkeypatch.setattr(runner, "REC_ROOT", recording_root)
    monkeypatch.setattr(runner, "talk_pipeline_fingerprint", lambda _cid: NEW_FINGERPRINT)
    monkeypatch.setattr(runner, "ffprobe_ms", lambda _segment: 1_800_000)
    monkeypatch.setattr(runner, "find_danmaku_xml", lambda _segment: None)
    monkeypatch.setattr(runner, "find_chat_jsonl", lambda _segment: None)
    monkeypatch.setattr(runner, "resolve_structured_chat_binding", lambda *_args, **_kwargs: {"chat_jsonl": None, "structured_chat_required": False, "chat_binding_status": "OPTIONAL_ABSENT"})
    monkeypatch.setattr(published_cover_carry, "validate_cover_route_decision", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(published_cover_carry, "validate_final_host_identity_verification", lambda *_args, **_kwargs: True)
    real_load = planner._load_recovery_publication_contract
    publication = real_load(queued_candidate_ids={candidate_id}, publication_asset=QIXI_SAME_BV_ASSET, expected_publication_authority_sha256=QIXI_SAME_BV_ASSET_SHA256, repo_root=ROOT)
    monkeypatch.setattr(planner, "_load_recovery_publication_contract", lambda **_kwargs: publication)
    return (
        [
            "--source-base", str(source_base), "--target-base", str(target_base),
            "--target-recordings-root", str(recording_root), "--project-single-published-repair",
            "--preserve-published-cover", "--expected-source-bcut-sha256", digest(source_bcut.read_bytes()),
            "--date", date, "--expected-source-state-sha256", source_sha,
            "--expected-old-fingerprint", OLD_FINGERPRINT, "--expected-new-fingerprint", NEW_FINGERPRINT,
            "--candidate-id", candidate_id, "--publication-authority-asset", str(QIXI_SAME_BV_ASSET),
            "--expected-publication-authority-sha256", QIXI_SAME_BV_ASSET_SHA256,
        ],
        target_base,
        source_state,
        bindings["cover"]["sha256"],
    )
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


@pytest.mark.parametrize(
    ("publication_asset", "expected_sha256", "match"),
    [
        (
            ASSET,
            ASSET_SHA256,
            "RECOVERY_PUBLICATION_CANDIDATE_MISSING",
        ),
        (
            QIXI_SAME_BV_ASSET,
            "sha256:" + "0" * 64,
            "RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH",
        ),
    ],
    ids=["missing-qixi-entry", "wrong-dedicated-registry-hash"],
)
def test_qixi_single_published_contract_rejects_missing_or_wrong_authority(
    publication_asset: Path, expected_sha256: str, match: str
):
    with pytest.raises(SystemExit, match=match):
        planner._load_recovery_publication_contract(
            queued_candidate_ids={"auto_113022_354_496"},
            publication_asset=publication_asset,
            expected_publication_authority_sha256=expected_sha256,
            repo_root=ROOT,
        )


def test_single_published_bcut_copy_rejects_wrong_source_hash(tmp_path: Path):
    source_base, target_base, record, _source_bcut, _source_sha256 = _bcut_copy_fixture(
        tmp_path
    )

    with pytest.raises(SystemExit, match="BCUT source hash mismatch"):
        planner._copy_single_published_bcut_authority(
            source_base=source_base,
            target_base=target_base,
            date="2026-08-17",
            record=record,
            expected_sha256="sha256:" + "0" * 64,
        )

    assert not (target_base / "cache" / "2026-08-17" / "official.bcut.srt").exists()


@pytest.mark.parametrize("source_kind", ["symlink", "empty"])
def test_single_published_bcut_copy_rejects_non_authoritative_source(
    tmp_path: Path, source_kind: str
):
    source_base, target_base, record, source_bcut, source_sha256 = _bcut_copy_fixture(
        tmp_path
    )
    if source_kind == "symlink":
        source_target = tmp_path / "source-authority.srt"
        source_target.write_bytes(source_bcut.read_bytes())
        source_bcut.unlink()
        source_bcut.symlink_to(source_target)
    else:
        source_bcut.write_bytes(b"")

    with pytest.raises(SystemExit, match="BCUT source"):
        planner._copy_single_published_bcut_authority(
            source_base=source_base,
            target_base=target_base,
            date="2026-08-17",
            record=record,
            expected_sha256=source_sha256,
        )

    assert not (target_base / "cache" / "2026-08-17" / "official.bcut.srt").exists()


@pytest.mark.parametrize("target_kind", ["regular", "symlink"])
def test_single_published_bcut_copy_rejects_preexisting_target(
    tmp_path: Path, target_kind: str
):
    source_base, target_base, record, _source_bcut, source_sha256 = _bcut_copy_fixture(
        tmp_path
    )
    target = target_base / "cache" / "2026-08-17" / "official.bcut.srt"
    target.parent.mkdir(parents=True)
    if target_kind == "regular":
        target.write_text("existing\n", encoding="utf-8")
    else:
        target.symlink_to(target_base / "missing.srt")

    with pytest.raises(SystemExit, match="BCUT target already exists"):
        planner._copy_single_published_bcut_authority(
            source_base=source_base,
            target_base=target_base,
            date="2026-08-17",
            record=record,
            expected_sha256=source_sha256,
        )

    assert target.is_symlink() if target_kind == "symlink" else target.is_file()


def test_single_published_plan_failure_rolls_back_created_bcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args, source_state, target_base, _source_bcut = _qixi_single_published_main_fixture(
        tmp_path, monkeypatch
    )
    source_bytes = source_state.read_bytes()

    def fail_plan(*_args: object, **_kwargs: object) -> dict:
        raise RecoveryReviewRerunError("INJECTED_PLAN_FAILURE")

    monkeypatch.setattr(delivery_recovery, "plan_current_talk_recovery_rerun", fail_plan)
    with pytest.raises(SystemExit, match="INJECTED_PLAN_FAILURE"):
        planner.main(args)

    assert source_state.read_bytes() == source_bytes
    assert not (target_base / "cache" / "2026-08-17" / "official.bcut.srt").exists()
    assert not (target_base / "state" / "2026-08-17.json").exists()
    assert not (target_base / "reports" / "recovery-review-rerun-plan-2026-08-17.json").exists()
    assert not (target_base / "cpa.env").exists()


def test_single_published_cpa_failure_rolls_back_created_bcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args, source_state, target_base, _source_bcut = _qixi_single_published_main_fixture(
        tmp_path, monkeypatch
    )
    source_bytes = source_state.read_bytes()
    monkeypatch.setattr(
        planner,
        "_bind_external_cpa_env",
        lambda **_kwargs: (_ for _ in ()).throw(SystemExit("INJECTED_CPA_FAILURE")),
    )

    with pytest.raises(SystemExit, match="INJECTED_CPA_FAILURE"):
        planner.main(args)

    assert source_state.read_bytes() == source_bytes
    assert not (target_base / "cache" / "2026-08-17" / "official.bcut.srt").exists()
    assert not (target_base / "state" / "2026-08-17.json").exists()
    assert not (target_base / "reports" / "recovery-review-rerun-plan-2026-08-17.json").exists()
    assert not (target_base / "cpa.env").exists()


def test_single_published_receipt_failure_keeps_bcut_after_state_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args, _source_state, target_base, source_bcut = _qixi_single_published_main_fixture(
        tmp_path, monkeypatch
    )
    atomic_create = planner._atomic_create

    def fail_receipt(path: Path, payload: bytes) -> None:
        if path.parent.name == "reports":
            raise OSError("INJECTED_RECEIPT_FAILURE")
        atomic_create(path, payload)

    monkeypatch.setattr(planner, "_atomic_create", fail_receipt)
    with pytest.raises(OSError, match="INJECTED_RECEIPT_FAILURE"):
        planner.main(args)

    assert (target_base / "state" / "2026-08-17.json").is_file()
    assert (target_base / "cache" / "2026-08-17" / "official.bcut.srt").read_bytes() == (
        source_bcut.read_bytes()
    )


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


def test_single_published_projection_accepts_published_review_ready_source():
    target = _record(
        "auto_113022_354_496",
        start_ms=354_630,
        end_ms=496_420,
        status="published",
    )
    target["prepublication_status"] = "review_ready"
    state = {
        "run_mode": "DAILY",
        "upload_allowed": False,
        "picks": [target],
        "pending_talk": [],
    }

    projected = planner._project_single_published_repair_state(
        state,
        candidate_id="auto_113022_354_496",
        source_state_sha256="sha256:" + "a" * 64,
        delivered_statuses=runner.DELIVERED_TALK_STATUSES,
    )

    assert state["picks"][0]["status"] == "published"
    assert projected["picks"][0]["status"] == "review_ready"
    assert projected["picks"][0]["prepublication_status"] == "review_ready"
    assert projected["single_published_repair_projection"]["source_target_status"] == (
        "published"
    )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("prepublication_status", "candidate_rejected", "prepublication_status"),
        ("rc", 1, "CURRENT\\+COMPLIANT"),
        ("bundle_lifecycle", "SUPERSEDED", "CURRENT\\+COMPLIANT"),
        ("bundle_compliance", "STALE_PIPELINE", "CURRENT\\+COMPLIANT"),
    ],
)
def test_single_published_projection_rejects_unqualified_published_source(
    field: str, value: object, match: str
):
    target = _record(
        "auto_113022_354_496",
        start_ms=354_630,
        end_ms=496_420,
        status="published",
    )
    target["prepublication_status"] = "review_ready"
    target[field] = value
    state = {
        "run_mode": "DAILY",
        "upload_allowed": False,
        "picks": [target],
        "pending_talk": [],
    }

    with pytest.raises(SystemExit, match=match):
        planner._project_single_published_repair_state(
            state,
            candidate_id="auto_113022_354_496",
            source_state_sha256="sha256:" + "a" * 64,
            delivered_statuses=runner.DELIVERED_TALK_STATUSES,
        )

    assert state["picks"][0]["status"] == "published"


def test_normal_recovery_path_rejects_published_qixi_without_isolation():
    authorities, given_ends, given_authority = (
        planner._load_recovery_publication_contract(
            queued_candidate_ids={"auto_113022_354_496"},
            publication_asset=QIXI_SAME_BV_ASSET,
            expected_publication_authority_sha256=QIXI_SAME_BV_ASSET_SHA256,
            repo_root=ROOT,
        )
    )
    published = _record(
        "auto_113022_354_496",
        start_ms=354_630,
        end_ms=496_420,
        status="published",
    )
    published["prepublication_status"] = "review_ready"
    state = {
        "run_mode": "RECOVERY_REVIEW",
        "upload_allowed": False,
        "picks": [published],
        "pending_talk": [],
        "talk_backlog": [],
        "talk_superseded_attempts": [],
    }

    with pytest.raises(
        RecoveryReviewRerunError,
        match="RECOVERY_RERUN_ALLOWLIST_MUST_EQUAL_ALL_CURRENT_DELIVERIES",
    ):
        plan_current_talk_recovery_rerun(
            "2026-08-17",
            state,
            candidate_ids=["auto_113022_354_496"],
            expected_source_state_sha256="sha256:" + "a" * 64,
            expected_old_fingerprint=OLD_FINGERPRINT,
            expected_new_fingerprint=NEW_FINGERPRINT,
            given_end_ms_by_candidate=given_ends,
            given_end_authority=given_authority,
            recovery_publication_authorities_by_candidate=authorities,
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


def test_single_published_main_requires_source_bcut_authority(
    tmp_path, monkeypatch
):
    args, target_base, _source_state = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=["auto_193450_3573_3665"],
    )
    args = _without_suppression_or_replacement_options(args)
    args.extend(
        [
            "--project-single-published-repair",
            "--target-recordings-root",
            str(target_base / "recordings"),
        ]
    )

    with pytest.raises(SystemExit, match="requires a valid --expected-source-bcut-sha256"):
        planner.main(args)

    assert not (target_base / "state" / f"{DATE}.json").exists()


def test_source_bcut_sha_argument_is_rejected_without_single_published_flag(
    tmp_path, monkeypatch
):
    args, target_base, _source_state = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=["auto_193450_3573_3665"],
    )
    args.extend(["--expected-source-bcut-sha256", "sha256:" + "a" * 64])

    with pytest.raises(SystemExit, match="requires --project-single-published-repair"):
        planner.main(args)

    assert not (target_base / "state" / f"{DATE}.json").exists()


@pytest.mark.parametrize(
    (
        "date,candidate_id,start_ms,end_ms,status,publication_asset,publication_sha256"
    ),
    [
        (
            "2026-07-24",
            "auto_193129_850_940",
            850_020,
            940_490,
            "review_ready",
            DAILY_850_ASSET,
            DAILY_850_ASSET_SHA256,
        ),
        (
            "2026-08-17",
            "auto_113022_354_496",
            354_630,
            496_420,
            "published",
            QIXI_SAME_BV_ASSET,
            QIXI_SAME_BV_ASSET_SHA256,
        ),
    ],
    ids=["historical-delivered", "qixi-published"],
)
def test_single_published_projection_main_uses_external_recording_tree(
    tmp_path,
    monkeypatch,
    date: str,
    candidate_id: str,
    start_ms: int,
    end_ms: int,
    status: str,
    publication_asset: Path,
    publication_sha256: str,
):
    source_base = tmp_path / "source"
    target_base = tmp_path / "target"
    recording_root = tmp_path / "canonical-recordings"
    (source_base / "state").mkdir(parents=True)
    (source_base / "cpa.env").write_text(
        "CPA_BASE_URL=https://example.invalid/v1\nCPA_API_KEY=test\n",
        encoding="utf-8",
    )
    (source_base / "cache" / date).mkdir(parents=True)
    source_bcut = source_base / "cache" / date / "official.bcut.srt"
    source_bcut.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n字幕\n",
        encoding="utf-8",
    )
    source_bcut_sha256 = "sha256:" + hashlib.sha256(source_bcut.read_bytes()).hexdigest()
    target_base.mkdir()
    (target_base / "repo").symlink_to(ROOT, target_is_directory=True)
    (recording_root / date).mkdir(parents=True)
    (recording_root / date / "official.mp4").write_bytes(b"official-media")
    target = _record(
        candidate_id,
        start_ms=start_ms,
        end_ms=end_ms,
        status=status,
    )
    if status == "published":
        target["prepublication_status"] = "review_ready"
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
                "--expected-source-bcut-sha256",
                source_bcut_sha256,
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
                str(publication_asset),
                "--expected-publication-authority-sha256",
                publication_sha256,
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
    assert target_state["single_published_repair_projection"][
        "source_target_status"
    ] == status
    copied_bcut = target_base / "cache" / date / "official.bcut.srt"
    assert copied_bcut.read_bytes() == source_bcut.read_bytes()
    assert receipt["target_recordings_root"] == str(recording_root)
    assert receipt["external_cpa_env"] == {
        "schema_version": "recovery-external-cpa-env-binding.v1",
        "status": "BOUND",
        "binding": "SYMLINK_EXTERNAL_AUTHORITY",
        "source_path": str(source_base / "cpa.env"),
        "resolved_authority_path": str(source_base / "cpa.env"),
        "target_path": str(target_base / "cpa.env"),
    }
    assert receipt["source_bcut_authority"] == {
        "schema_version": "recovery-source-bcut-binding.v1",
        "status": "BOUND",
        "binding": "COPIED_HASH_BOUND_SOURCE_BCUT",
        "source_path": str(source_bcut),
        "target_path": str(copied_bcut),
        "sha256": source_bcut_sha256,
        "bytes": len(source_bcut.read_bytes()),
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
            "--expected-source-bcut-sha256",
            "sha256:" + "a" * 64,
            "--target-recordings-root",
            str(recording_root),
        ]
    )

    with pytest.raises(
        SystemExit,
        match="target recordings root must expose only",
    ):
        planner.main(args)


def test_single_published_main_rejects_multiple_candidates_before_projection(
    tmp_path, monkeypatch
):
    args, target_base, _source_state = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=[
            "auto_193450_3573_3665",
            "auto_193450_672_945",
        ],
    )
    args = _without_suppression_or_replacement_options(args)
    args.extend(
        [
            "--project-single-published-repair",
            "--expected-source-bcut-sha256",
            "sha256:" + "a" * 64,
            "--target-recordings-root",
            str(target_base / "recordings"),
        ]
    )

    with pytest.raises(
        SystemExit,
        match="requires exactly one --candidate-id",
    ):
        planner.main(args)

    assert not (target_base / "state" / f"{DATE}.json").exists()


def test_single_published_main_rejects_wrong_authority_before_writing(
    tmp_path, monkeypatch
):
    args, target_base, _source_state = _planner_fixture(
        tmp_path,
        monkeypatch,
        requested_candidate_ids=["auto_193450_3573_3665"],
    )
    args = _without_suppression_or_replacement_options(args)
    source_base = _source_state.parent.parent
    source_bcut = source_base / "cache" / DATE / "official.bcut.srt"
    source_bcut.parent.mkdir(parents=True)
    source_bcut.write_text("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8")
    source_bcut_sha256 = "sha256:" + hashlib.sha256(source_bcut.read_bytes()).hexdigest()
    target_bcut = target_base / "cache" / DATE / "official.bcut.srt"
    target_bcut.unlink()
    authority_hash_index = args.index("--expected-publication-authority-sha256") + 1
    args[authority_hash_index] = "sha256:" + "0" * 64
    args.extend(
        [
            "--project-single-published-repair",
            "--expected-source-bcut-sha256",
            source_bcut_sha256,
            "--target-recordings-root",
            str(target_base / "recordings"),
        ]
    )

    with pytest.raises(
        SystemExit,
        match="RECOVERY_PUBLICATION_REGISTRY_SHA_MISMATCH",
    ):
        planner.main(args)

    assert not (target_base / "state" / f"{DATE}.json").exists()
    assert not (target_base / "cache" / DATE / "official.bcut.srt").exists()


def test_qixi_published_cover_carry_main_uses_only_sealed_fixture_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, target_base, source_state, public_cover_sha = _sealed_qixi_cover_carry_main_fixture(
        tmp_path, monkeypatch
    )

    assert planner.main(args) == 0

    state = json.loads((target_base / "state" / "2026-08-17.json").read_text(encoding="utf-8"))
    receipt = json.loads(
        (target_base / "reports/recovery-review-rerun-plan-2026-08-17.json").read_text(encoding="utf-8")
    )
    item = state["pending_talk"]
    assert len(item) == 1
    marker = item[0]["published_cover_carry"]
    assert item[0]["published_cover_carry_required"] is True
    assert item[0]["reuse_cover"] is True
    assert marker["cover_sha256"] == public_cover_sha
    assert Path(marker["cover_path"]).read_bytes() == b"8c-public-cover-fixture"
    assert receipt["published_cover_carries_by_candidate"] == {
        "auto_113022_354_496": {
            "recording_date": "2026-08-17",
            "cover_sha256": public_cover_sha,
            "generation_sha256": marker["generation_sha256"],
        }
    }
    assert source_state.exists()  # source authority is read-only throughout.


@pytest.mark.parametrize("mutation", ["source", "public", "artifact", "symlink", "target_exists"])
def test_qixi_published_cover_carry_rejects_before_state_when_authority_graph_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    args, target_base, source_state, _public_cover_sha = _sealed_qixi_cover_carry_main_fixture(
        tmp_path, monkeypatch
    )
    if mutation == "source":
        source_state.write_bytes(source_state.read_bytes() + b" ")
    elif mutation == "public":
        repo_root = (target_base / "repo").resolve()
        (repo_root / "reports/authorized_uploads/qixi/public.json").write_text("{}", encoding="utf-8")
    elif mutation == "artifact":
        (tmp_path / "public-source-artifacts/cover.bin").write_bytes(b"not-8c")
    elif mutation == "symlink":
        artifact = tmp_path / "public-source-artifacts/reference.bin"
        replacement = tmp_path / "replacement.bin"
        replacement.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(replacement)
    else:
        target = target_base / "out/2026-08-17/auto_113022_354_496/replacement_recuts/covers/auto_113022_354_496.published-carry.cover.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"preexisting")

    with pytest.raises(SystemExit):
        planner.main(args)

    assert not (target_base / "state/2026-08-17.json").exists()
    assert not (target_base / "reports/recovery-review-rerun-plan-2026-08-17.json").exists()
    assert not (target_base / "cache/2026-08-17/official.bcut.srt").exists()
    if mutation != "target_exists":
        assert not (target_base / "out/2026-08-17/auto_113022_354_496/replacement_recuts/covers/auto_113022_354_496.published-carry.cover.png").exists()


def test_qixi_published_cover_carry_plan_failure_rolls_back_all_owned_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, target_base, _source_state, _public_cover_sha = _sealed_qixi_cover_carry_main_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        delivery_recovery,
        "plan_current_talk_recovery_rerun",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RecoveryReviewRerunError("INJECTED_PLAN_FAILURE")),
    )

    with pytest.raises(SystemExit, match="INJECTED_PLAN_FAILURE"):
        planner.main(args)

    package = target_base / "out/2026-08-17/auto_113022_354_496/replacement_recuts"
    # mkdir parents are harmless and are not invocation-owned files; every
    # create-only evidence file and the sidecar must be gone.
    assert not [path for path in package.rglob("*") if path.is_file() or path.is_symlink()]
    assert not (target_base / "cache/2026-08-17/official.bcut.srt").exists()
    assert not (target_base / "state/2026-08-17.json").exists()


def test_qixi_published_cover_carry_receipt_failure_never_leaves_a_consumable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, target_base, _source_state, _public_cover_sha = _sealed_qixi_cover_carry_main_fixture(
        tmp_path, monkeypatch
    )
    atomic_create = planner._atomic_create

    def fail_receipt(path: Path, payload: bytes) -> None:
        if path.parent.name == "reports":
            raise OSError("INJECTED_RECEIPT_FAILURE")
        atomic_create(path, payload)

    monkeypatch.setattr(planner, "_atomic_create", fail_receipt)
    with pytest.raises(OSError, match="INJECTED_RECEIPT_FAILURE"):
        planner.main(args)

    # State is the commit point.  Its typed marker is valid and exact even when
    # operator reporting fails afterwards; a later invocation sees the state
    # and refuses to overwrite it instead of creating a second plan.
    state = json.loads((target_base / "state/2026-08-17.json").read_text(encoding="utf-8"))
    marker = state["pending_talk"][0]["published_cover_carry"]
    assert published_cover_carry.validate_materialized_marker(
        marker, base=target_base, date="2026-08-17", candidate_id="auto_113022_354_496"
    )
    assert not (target_base / "reports/recovery-review-rerun-plan-2026-08-17.json").exists()
