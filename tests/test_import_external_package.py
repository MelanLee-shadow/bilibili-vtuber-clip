"""外部包导入链的单测：路径规整 / 完整性 / 持锁 / 幂等 / 白名单拒绝。

全部在 tmp_path 上跑：不碰 free，不碰真 state，不发网络请求。门链（manifest /
audit / 联合质检）以可注入的 GateRunner seam 打桩——本地造不出一个能过真 audit
的包，所以这里验的是"编排与绑定"，不是"审计本身"。
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import import_external_package as cli
from src.autoslice import package_import as pi
from src.autoslice import repository_asset_authority as raa
from src.autoslice.surface_canon import CHANNEL_PROFILE


CANDIDATE = "auto_220747_488_680"
DATE = "2026-08-07"
STEM = f"{CANDIDATE}.recut.burned-final-speaker"
SRC_WORKSPACE = "/home/ivan/Project"
SRC_REPO = "/home/ivan/Project/repo-87"
SRC_CANDIDATE = f"/home/ivan/Project/vtuber-reproduce/out/{DATE}/{CANDIDATE}"
SRC_PACKAGE = f"{SRC_CANDIDATE}/replacement_recuts"
TITLE = "【李豆沙】她说“熊猫头”那句，我笑了半天"
RELEASE_QUOTE = (
    "说起来刚刚通过的这两个可以发布了，只要你根据我的人工真值改完后，"
    "可以直接去快车道发布，和你的通用车道修复并行进行"
)
OTHER_FAILED_CANDIDATE = "auto_other_failed"
DEPLOYED_TEST_COMMIT = "a" * 40


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = pi.json_bytes(document)
    path.write_bytes(payload)
    return _sha256(payload)


def _write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return _sha256(payload)


@dataclass
class Fixture:
    base: Path
    repo_root: Path
    staging_candidate: Path
    staging_package: Path
    destination_package: Path
    state_path: Path
    runner_lock: Path
    deployed_commit_file: Path


def _build_external_package(tmp_path: Path) -> Fixture:
    """One realistic wsl-produced talk package, still declaring wsl paths."""

    base = tmp_path / "autoslice"
    repo_root = base / "repo"
    staging_candidate = tmp_path / "staging" / CANDIDATE
    staging_package = staging_candidate / "replacement_recuts"

    # repo-side asset the speaker manifest points at (repo role: never copied,
    # only required to exist at the destination).
    _write_json(
        repo_root / "assets" / "lidousha" / "speaker_profile.json",
        {"host": "lidousha"},
    )
    _write_bytes(
        repo_root / "DEPLOYED_COMMIT",
        f"{DEPLOYED_TEST_COMMIT} 2026-08-10T00:00:00Z\n".encode("ascii"),
    )

    burned_sha = _write_bytes(staging_package / f"{STEM}.mp4", b"burned-video-bytes")
    subtitle_sha = _write_bytes(
        staging_package / f"{CANDIDATE}.recut.srt",
        "1\n00:00:00,000 --> 00:00:02,000\n你好\n\n".encode("utf-8"),
    )
    _write_bytes(staging_package / f"{CANDIDATE}.recut.speaker-final.srt", b"srt")
    _write_bytes(staging_package / f"{CANDIDATE}.recut.speaker-final.ass", b"ass")
    cover_sha = _write_bytes(
        staging_package / "covers" / "final-cover.png", b"final-cover-bytes"
    )
    _write_bytes(
        staging_package / "covers_ai_original" / "background.png", b"background"
    )
    _write_bytes(staging_package / "covers" / "mask.png", b"mask")
    _write_bytes(staging_package / "cover_refs" / "reference.png", b"reference")
    _write_bytes(staging_package / f"{CANDIDATE}.redelivery-baseline.json", b"{}")
    # candidate-root artifacts: the record points at them outside the package.
    filler_sha = _write_json(
        staging_candidate / f"{CANDIDATE}.talk-filler.json", {"removals": []}
    )
    chat_sha = _write_json(
        staging_candidate / f"{CANDIDATE}.chat-authority.json",
        {
            "final_status": "FINAL_ARTIFACTS_VERIFIED",
            "final_text_srt_sha256": subtitle_sha,
            "final_speaker_srt_sha256": _sha256(b"srt"),
            "speaker_ass_path": f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.ass",
            "speaker_ass_sha256": _sha256(b"ass"),
            "speaker_manifest_sha256": "0" * 64,
        },
    )
    context_sha = _write_json(
        staging_candidate / f"{CANDIDATE}.clip-context.json", {"prompt": "x"}
    )

    speaker = {
        "schema_version": "speaker-finalization.v1",
        "status": "SPEAKER_FINALIZED",
        "source_media": f"{SRC_PACKAGE}/{STEM}.mp4",
        "text_final_srt": f"{SRC_PACKAGE}/{CANDIDATE}.recut.srt",
        "output_review_srt": f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.srt",
        "output_ass": f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.ass",
        # 真实 wsl 产线以 free 为计算臂，repo 侧资产本来就写成 free 的路径
        # （87finish 实包如此）：这类定位符不需要投影。
        "profile": f"{repo_root}/assets/lidousha/speaker_profile.json",
        "speaker_override": f"{SRC_CANDIDATE}/localized_speaker_overrides/o.json",
        "analysis": {"host_turns": 29, "produced_on": SRC_PACKAGE},
    }
    override_sha = _write_json(
        staging_candidate / "localized_speaker_overrides" / "o.json", {"cues": []}
    )
    speaker_sha = _write_json(
        staging_package / f"{CANDIDATE}.recut.speaker-final.json", speaker
    )

    cover_generation = {
        "method": "cpa_repaint",
        "route_decision": "REPAINT",
        "reference_authority": "live_frame",
        "final_cover": f"{SRC_PACKAGE}/covers/final-cover.png",
        "final_cover_sha256": "sha256:" + cover_sha,
        "pre_overlay_path": f"{SRC_PACKAGE}/covers_ai_original/background.png",
        "pre_overlay_sha256": "sha256:" + _sha256(b"background"),
        "ai_background": f"{SRC_PACKAGE}/covers_ai_original/background.png",
        "ai_background_sha256": "sha256:" + _sha256(b"background"),
        "reference_image": f"{SRC_PACKAGE}/cover_refs/reference.png",
        "reference_sha256": "sha256:" + _sha256(b"reference"),
        "rendered_text_pixels": {
            "mask_path": f"{SRC_PACKAGE}/covers/mask.png",
            "mask_sha256": "sha256:" + _sha256(b"mask"),
            "pre_overlay_path": (
                f"{SRC_PACKAGE}/covers_ai_original/background.png"
            ),
        },
    }
    publish = {
        "schema_version": "slice-publish.v3",
        "candidate_id": CANDIDATE,
        "title": TITLE,
        "title_source": "pipeline",
        "title_authority_status": "RESOLVED",
        "title_authority_error": None,
        "cover_status": "AI_COVER_READY",
        "cover_path": f"{SRC_PACKAGE}/covers/final-cover.png",
        "cover_generation": cover_generation,
        "video_path": f"{SRC_PACKAGE}/{STEM}.mp4",
        "artifact_hashes": {"burned_video_sha256": "sha256:" + burned_sha},
    }
    publish_sha = _write_json(
        staging_package / f"{CANDIDATE}.recut.publish.json", publish
    )

    publish_staging = {
        key: copy.deepcopy(value)
        for key, value in publish.items()
        if key
        not in {"artifact_hashes", "candidate_id", "schema_version", "video_path"}
    }
    publish_staging["publish_json_path"] = (
        f"{SRC_PACKAGE}/{CANDIDATE}.recut.publish.json"
    )
    publish_staging["status"] = "STAGED"

    record = {
        "candidate_id": CANDIDATE,
        "start_ms": 488000,
        "end_ms": 680000,
        "duration_ms": 192000,
        "status": "delivered",
        "media_path": f"{SRC_PACKAGE}/{STEM}.mp4",
        "subtitle_path": f"{SRC_PACKAGE}/{CANDIDATE}.recut.srt",
        "subtitle_ass_path": (
            f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.ass"
        ),
        "speaker_review_srt_path": (
            f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.srt"
        ),
        "speaker_finalization_manifest_path": (
            f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.json"
        ),
        "speaker_finalization_manifest_sha256": "sha256:" + speaker_sha,
        "speaker_finalization": copy.deepcopy(speaker),
        "chat_authority_audit_path": (
            f"{SRC_CANDIDATE}/{CANDIDATE}.chat-authority.json"
        ),
        "clip_context_path": f"{SRC_CANDIDATE}/{CANDIDATE}.clip-context.json",
        "talk_filler_audit_path": f"{SRC_CANDIDATE}/{CANDIDATE}.talk-filler.json",
        "redelivery_baseline_audit_path": (
            f"{SRC_PACKAGE}/{CANDIDATE}.redelivery-baseline.json"
        ),
        "burned_preview": {
            "path": f"{SRC_PACKAGE}/{STEM}.mp4",
            "ass_path": f"{SRC_PACKAGE}/{CANDIDATE}.recut.speaker-final.ass",
            "command": ["ffmpeg", "-i", f"{SRC_PACKAGE}/{CANDIDATE}.recut.mp4"],
        },
        "boundary_audit": {
            "verdict": "CLEAN",
            "final_end_ms": 680000,
            "closure_sentence": "就这样吧",
            "red_flags": [],
            "boundary_repairs": [],
        },
        "subtitle_timing_qa": {"counts": {"cues": 61}},
        "redelivery_baseline": {"status": "BASELINE_BOUND"},
        "story_contract": {
            "candidate_id": CANDIDATE,
            "selection_hook": "她说熊猫头那句",
            "selection_scorecard": {"total": 91.0},
        },
        "selection_scorecard": {"total": 91.0},
        "session_relation_authority": {"status": "RESOLVED"},
        "publish_staging": publish_staging,
        "artifact_hashes": {
            "burned_video_sha256": "sha256:" + burned_sha,
            "subtitle_sha256": "sha256:" + subtitle_sha,
            "cover_sha256": "sha256:" + cover_sha,
            "chat_authority_audit_sha256": "sha256:" + chat_sha,
            "clip_context_file_sha256": "sha256:" + context_sha,
            "publish_draft_sha256": "sha256:" + publish_sha,
        },
    }
    assert filler_sha and override_sha  # written above; hashes unused downstream
    _write_json(staging_package / f"{CANDIDATE}.record.json", record)

    state_path = base / "state" / f"{DATE}.json"
    _write_json(
        state_path,
        {
            "date": DATE,
            "status": "ready_unpublished",
            "picks": [],
            "pending_talk": [],
            "pending_song": [],
            "songs": [],
        },
    )
    return Fixture(
        base=base,
        repo_root=repo_root,
        staging_candidate=staging_candidate,
        staging_package=staging_package,
        destination_package=base / "out" / DATE / CANDIDATE / "replacement_recuts",
        state_path=state_path,
        runner_lock=base / "runner.lock",
        deployed_commit_file=repo_root / "DEPLOYED_COMMIT",
    )


def _failed_pick_closure(state: dict) -> dict:
    target = next(
        row
        for row in state["picks"]
        if row.get("candidate_id") == CANDIDATE
    )
    target_ready = target.get("status") == "review_ready" and target.get("rc") == 0
    return {
        "schema_version": "daily-publication-closure.v1",
        "status": (
            "ready_unpublished_with_failures"
            if target_ready
            else "published_with_failures"
        ),
        "published_candidate_ids": ["auto_already_published"],
        "ready_unpublished_candidate_ids": [CANDIDATE] if target_ready else [],
        "unresolved_candidate_ids": (
            [OTHER_FAILED_CANDIDATE]
            if target_ready
            else [OTHER_FAILED_CANDIDATE, CANDIDATE]
        ),
    }


def _configure_authorized_failed_pick(
    fixture: Fixture,
    *,
    quote: str = RELEASE_QUOTE,
) -> tuple[dict, dict, Path]:
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    target = {
        "candidate_id": CANDIDATE,
        "status": "failed",
        "rc": 1,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_findings",
        "selected_repair": True,
        "failure_message": "FINAL_REVIEW_RELEASE_BLOCKED",
        "failure_evidence": {"release_gate": "BLOCK"},
        "failure_fingerprint": "sha256:" + "1" * 64,
        "failure_recoverable": True,
        "failure_recovery_fingerprint": "sha256:" + "2" * 64,
        "rejected_status": "failed",
        "rejection_reason": "subtitle_authority_unresolved_backfilled",
        "retry_reason": "sanctioned_candidate_revival",
        "sanctioned_revival_retry": {"status": "PENDING"},
        "revivals": [{"schema_version": "candidate-revival.v1"}],
        "talk_repair_retry_count": 1,
        "hook": "historical selection hook",
    }
    other = {
        "candidate_id": OTHER_FAILED_CANDIDATE,
        "status": "failed",
        "rc": 1,
    }
    state.update(
        {
            "status": "published_with_failures",
            "picks": [target, other],
            "pending_talk": [],
            "pending_song": [],
            "songs": [],
        }
    )
    state["publication_closure"] = _failed_pick_closure(state)
    _write_json(fixture.state_path, state)

    authority = {
        "schema_version": pi.FAILED_PICK_IMPORT_AUTHORITY_SCHEMA_VERSION,
        "scope": pi.FAILED_PICK_IMPORT_SCOPE,
        "candidate_id": CANDIDATE,
        "recording_date": DATE,
        "state_row_canonical_sha256": (
            "sha256:" + pi.canonical_json_sha256(target)
        ),
        "required_pick_state": {
            "status": "failed",
            "rc": 1,
            "failure_kind": "subtitle_authority",
            "failure_stage": "final_review_findings",
            "selected_repair": True,
        },
        "required_batch_state": {
            "status": "published_with_failures",
            "publication_closure_status": "published_with_failures",
        },
        "authorized_transition": {
            "status": "ready_unpublished_with_failures",
            "publication_closure_status": "ready_unpublished_with_failures",
        },
    }
    entry = {
        "candidate_id": CANDIDATE,
        "recording_date": DATE,
        "status": "released_for_upload",
        "released_by": "Ivan",
        "released_at": "2026-08-11",
        "release_quote": quote,
        "failed_pick_import_authority": authority,
    }
    registry_path = fixture.repo_root / cli.PUBLICATION_REGISTRY_RELATIVE
    _write_json(
        registry_path,
        {
            "schema_version": "publication-registry.v1",
            "entries": [entry],
        },
    )
    _seal_deployed_authority(fixture, registry_path)
    return state, entry, registry_path


def _seal_deployed_authority(fixture: Fixture, registry_path: Path) -> None:
    manifest = raa.build_deployed_authority_manifest(
        repo_root=fixture.repo_root,
        deployed_commit=DEPLOYED_TEST_COMMIT,
        relative_paths=[registry_path.relative_to(fixture.repo_root)],
    )
    _write_json(
        fixture.repo_root / raa.DEPLOYED_AUTHORITY_MANIFEST,
        manifest,
    )


class StubGateRunner:
    """Stand in for the real gate scripts; records every argv it was given."""

    def __init__(self, fixture: Fixture, *, qc_title: str | None = None,
                 qc_cover: str | None = None, audit_passed: bool = True) -> None:
        self.fixture = fixture
        self.calls: list[list[str]] = []
        self.qc_title = qc_title
        self.qc_cover = qc_cover
        self.audit_passed = audit_passed
        self.audit_stdout: str | None = None

    def run(self, argv):  # noqa: ANN001 - mirrors GateRunner.run
        argv = [str(value) for value in argv]
        self.calls.append(argv)
        script = Path(argv[1]).name
        if script == "build_lidousha_daily_review_manifest.py":
            title = argv[argv.index("--candidate") + 1] and TITLE
            _write_json(
                self.fixture.destination_package / "review_manifest.json",
                {
                    "schema_version": "lidousha-daily-review-manifest.v1",
                    "candidate_id": CANDIDATE,
                    "status": "review_ready",
                    "items": [
                        {
                            "candidate_id": CANDIDATE,
                            "title": title,
                            "cover": f"{STEM}.cover.png",
                            "video": f"{STEM}.mp4",
                        }
                    ],
                },
            )
            return cli.GateResult(0, "WROTE review_manifest.json", "")
        if script == "audit_lidousha_review_package.py":
            report = {
                "passed": self.audit_passed,
                "blocking_issue_count": 0 if self.audit_passed else 1,
                "issue_count": 0 if self.audit_passed else 1,
                "issues": []
                if self.audit_passed
                else [{"severity": "BLOCK", "code": "COVER_TEXT_OVERCROWDED"}],
            }
            self.audit_stdout = json.dumps(report)
            return cli.GateResult(0, self.audit_stdout, "")
        if script == "run_title_cover_joint_qc.py":
            package_root, title, out_path = argv[2], argv[3], Path(argv[4])
            bound_title = self.qc_title if self.qc_title is not None else title
            cover = self.qc_cover or f"{package_root}/{STEM}.cover.png"
            _write_json(
                out_path,
                {
                    "schema_version": "lidousha-title-cover-joint-qc.v1",
                    "candidate_id": CANDIDATE,
                    "title": bound_title,
                    "title_sha256": "sha256:"
                    + hashlib.sha256(bound_title.encode("utf-8")).hexdigest(),
                    "cover_path": cover,
                    "cover_sha256": "sha256:" + _sha256(b"final-cover-bytes"),
                    "status": "PASS",
                    "pass": True,
                    "verdict": {"pass": True},
                },
            )
            return cli.GateResult(0, "status: PASS", "")
        raise AssertionError(f"unexpected gate script: {script}")


def _run(fixture: Fixture, *, apply: bool = True, gate=None, **overrides):
    kwargs = dict(
        source=fixture.staging_package,
        date=DATE,
        candidate_id=CANDIDATE,
        base=fixture.base,
        repo_root=fixture.repo_root,
        state_path=fixture.state_path,
        runner_lock=fixture.runner_lock,
        deployed_commit_file=fixture.deployed_commit_file,
        apply=apply,
        allow_new_pick=True,
        skip_qc=False,
        source_repo_root=None,
        source_workspace_root=None,
        gate_runner=gate or StubGateRunner(fixture),
    )
    kwargs.update(overrides)
    return cli.run_import(**kwargs)


def _step(receipt: dict, name: str) -> dict:
    rows = [row for row in receipt["steps"] if row["step"] == name]
    assert rows, f"{name} not in receipt: {[r['step'] for r in receipt['steps']]}"
    return rows[-1]


def _run_authorized_failed_pick(
    fixture: Fixture,
    *,
    apply: bool = True,
    quote: str = RELEASE_QUOTE,
    **overrides,
):
    return _run(
        fixture,
        apply=apply,
        allow_new_pick=False,
        adopt_failed_pick=CANDIDATE,
        release_quote=quote,
        closure_projector=_failed_pick_closure,
        **overrides,
    )


# ---------------------------------------------------------------- path regularization


def test_relocation_projects_locators_and_freezes_evidence(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture)
    assert code == 0, receipt
    assert receipt["status"] == "REVIEW_READY"

    destination = fixture.destination_package
    record = json.loads(
        (destination / f"{CANDIDATE}.record.json").read_text(encoding="utf-8")
    )
    publish = json.loads(
        (destination / f"{CANDIDATE}.recut.publish.json").read_text(encoding="utf-8")
    )
    speaker = json.loads(
        (destination / f"{CANDIDATE}.recut.speaker-final.json").read_text(
            encoding="utf-8"
        )
    )
    # every runtime locator now names the free copy ...
    assert record["media_path"] == f"{destination}/{STEM}.mp4"
    assert record["subtitle_path"] == f"{destination}/{CANDIDATE}.recut.srt"
    assert publish["cover_generation"]["final_cover"] == (
        f"{destination}/covers/final-cover.png"
    )
    assert speaker["output_ass"] == (
        f"{destination}/{CANDIDATE}.recut.speaker-final.ass"
    )
    assert speaker["profile"] == (
        f"{fixture.repo_root}/assets/lidousha/speaker_profile.json"
    )
    # candidate-role locator keeps its candidate root, not the package root
    assert record["talk_filler_audit_path"] == (
        f"{fixture.base}/out/{DATE}/{CANDIDATE}/{CANDIDATE}.talk-filler.json"
    )
    # ... the frozen candidate evidence is staged into the package ...
    assert record["chat_authority_audit_path"] == (
        f"{destination}/{CANDIDATE}.chat-authority.json"
    )
    assert (destination / f"{CANDIDATE}.chat-authority.json").is_file()
    # ... and frozen evidence keeps the producing host's own words verbatim.
    assert speaker["analysis"]["produced_on"] == SRC_PACKAGE
    assert record["burned_preview"]["command"][-1].startswith(SRC_PACKAGE)
    # the record binds both other documents' post-relocation hashes
    assert record["artifact_hashes"]["publish_draft_sha256"] == (
        "sha256:" + _sha256((destination / f"{CANDIDATE}.recut.publish.json").read_bytes())
    )
    assert record["speaker_finalization_manifest_sha256"] == (
        "sha256:"
        + _sha256(
            (destination / f"{CANDIDATE}.recut.speaker-final.json").read_bytes()
        )
    )
    assert record["speaker_finalization"]["output_ass"] == speaker["output_ass"]


def test_same_stem_cover_alias_is_assembled_byte_identical(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture)
    assert code == 0
    alias = fixture.destination_package / f"{STEM}.cover.png"
    declared = fixture.destination_package / "covers" / "final-cover.png"
    assert alias.read_bytes() == declared.read_bytes()
    assert _step(receipt, "RELOCATE")["same_stem_cover"]["status"] == "ASSEMBLED"


def test_relocation_journal_records_speaker_manifest_lineage(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _run(fixture)
    journal = json.loads(
        (
            fixture.destination_package
            / f".{CANDIDATE}.package-relocation.json"
        ).read_text(encoding="utf-8")
    )
    assert journal["status"] == "COMMITTED"
    assert journal["commit_order"] == ["speaker", "publish", "record"]
    lineage = journal["speaker_manifest_lineage"]
    # 冻结的 chat authority 保留产出主机的 manifest sha，不被改写
    assert lineage["chat_speaker_manifest_sha256"] == "0" * 64
    assert lineage["before_sha256"] != lineage["after_sha256"]


def test_unknown_external_host_path_in_frozen_field_refuses(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    publish_path = fixture.staging_package / f"{CANDIDATE}.recut.publish.json"
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    publish["operator_note"] = f"{SRC_PACKAGE}/scratch/notes.txt"
    _write_json(publish_path, publish)
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "RELOCATE")
    assert step["code"] == "UNRELOCATED_EXTERNAL_PATH"


# ---------------------------------------------------------------- integrity


def test_copy_integrity_mismatch_refuses(tmp_path: Path, monkeypatch) -> None:
    fixture = _build_external_package(tmp_path)
    real_write = pi.atomic_write_bytes

    def corrupting_write(path: Path, payload: bytes) -> None:
        if path.name == f"{STEM}.mp4":
            payload = payload + b"corruption"
        real_write(path, payload)

    monkeypatch.setattr(pi, "atomic_write_bytes", corrupting_write)
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "COPY")
    assert step["status"] == "REFUSED"
    assert step["code"] == "COPY_INTEGRITY_MISMATCH"


def test_declared_artifact_sha_drift_refuses_before_copying(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    (fixture.staging_package / "covers" / "final-cover.png").write_bytes(
        b"tampered-cover"
    )
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "PREFLIGHT")
    assert step["code"] == "DECLARED_ARTIFACT_SHA_DRIFT"
    assert not fixture.destination_package.exists()


def test_preflight_reports_every_planned_file_hash(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture, apply=False)
    assert code == 0
    files = _step(receipt, "COPY")["files"]
    by_relative = {row["relative"]: row for row in files}
    assert by_relative[f"{STEM}.mp4"]["sha256"] == _sha256(b"burned-video-bytes")
    # candidate-root artifacts travel too, under their candidate-relative name
    assert f"{CANDIDATE}.talk-filler.json" in by_relative
    assert all(row["status"] == "WOULD_COPY" for row in files)


# ---------------------------------------------------------------- state binding


def test_state_bind_holds_the_runner_lock(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    observed: list[bool] = []
    real_build = pi.build_bound_state

    def probing_build(*args, **kwargs):
        probe = open(fixture.runner_lock, "a+", encoding="utf-8")
        try:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.append(True)
            else:
                observed.append(False)
                fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        finally:
            probe.close()
        return real_build(*args, **kwargs)

    original = pi.build_bound_state
    pi.build_bound_state = probing_build
    try:
        receipt, code = _run(fixture)
    finally:
        pi.build_bound_state = original
    assert code == 0, receipt
    assert observed == [True], "state was mutated without holding runner.lock"


def test_state_bind_backs_up_and_writes_review_ready_row(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture)
    assert code == 0
    step = _step(receipt, "STATE_BIND")
    assert step["status"] == "PASS"
    assert Path(step["state_backup_path"]).is_file()
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    rows = [row for row in state["picks"] if row["candidate_id"] == CANDIDATE]
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "review_ready"
    assert row["rc"] == 0
    assert row["bundle_lifecycle"] == "CURRENT"
    assert row["bundle_compliance"] == "COMPLIANT"
    assert row["title"] == TITLE
    # 封面必须名称+sha 双绑，且指向包内已落地的字节
    assert row["cover_path"] == (
        f"{fixture.destination_package}/covers/final-cover.png"
    )
    assert row["cover_sha256"] == "sha256:" + _sha256(b"final-cover-bytes")
    assert Path(row["cover_path"]).read_bytes() == b"final-cover-bytes"
    assert row["video_sha256"] == "sha256:" + _sha256(b"burned-video-bytes")
    # 新建行的选择窗只来自 record，不许凭空造
    assert (row["start_ms"], row["end_ms"]) == (488000, 680000)
    assert row["hook"] == "她说熊猫头那句"
    assert row["external_package_import"]["created_pick_row"] is True


def test_new_pick_row_requires_explicit_permission(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture, allow_new_pick=False)
    assert code == 2
    step = _step(receipt, "PREFLIGHT")
    assert step["code"] == "PICK_ROW_ABSENT"
    assert not fixture.destination_package.exists()


def test_non_reviewable_batch_status_refuses_before_copy(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["status"] = "processing"
    _write_json(fixture.state_path, state)
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "PREFLIGHT")
    assert step["code"] == "BATCH_STATUS_NOT_REVIEWABLE"
    assert "runner tick" in step["hint"]
    assert not fixture.destination_package.exists()


def test_failed_pick_row_is_not_a_revival_channel(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["picks"] = [
        {"candidate_id": CANDIDATE, "status": "candidate_rejected", "rc": 2}
    ]
    _write_json(fixture.state_path, state)
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "PREFLIGHT")
    assert step["code"] == "PICK_STATUS_NOT_REBINDABLE"
    assert "revive_rejected_candidates.py" in step["hint"]


def test_authorized_failed_pick_is_adopted_once_with_preimage_provenance(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    before_state, entry, registry_path = _configure_authorized_failed_pick(
        fixture
    )
    before_bytes = fixture.state_path.read_bytes()
    untouched_other = copy.deepcopy(before_state["picks"][1])
    untouched_collections = {
        key: copy.deepcopy(before_state[key])
        for key in ("pending_talk", "pending_song", "songs")
    }

    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 0, receipt
    assert receipt["status"] == "REVIEW_READY"
    assert receipt["upload_allowed"] is False
    bind = _step(receipt, "STATE_BIND")
    assert bind["adopted_failed_pick"] is True
    assert bind["closure_before"]["status"] == "published_with_failures"
    assert (
        bind["closure_after"]["status"]
        == "ready_unpublished_with_failures"
    )
    backup = Path(bind["state_backup_path"])
    assert backup.read_bytes() == before_bytes
    assert bind["state_preimage_sha256"] == (
        "sha256:" + _sha256(before_bytes)
    )

    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    assert state["status"] == "ready_unpublished_with_failures"
    assert (
        state["publication_closure"]["status"]
        == "ready_unpublished_with_failures"
    )
    assert state["picks"][1] == untouched_other
    for key, value in untouched_collections.items():
        assert state[key] == value
    row = state["picks"][0]
    assert row["candidate_id"] == CANDIDATE
    assert row["status"] == "review_ready"
    assert row["rc"] == 0
    for key in pi._ACTIVE_FAILURE_PICK_KEYS:
        assert key not in row
    # Historical retry evidence is retained; the exact full preimage remains
    # in the state backup and is hash-bound by the one-shot consumption row.
    assert row["revivals"] == before_state["picks"][0]["revivals"]
    assert row["talk_repair_retry_count"] == 1
    adoption = row["external_package_import"]["failed_pick_adoption"]
    assert adoption["schema_version"] == (
        pi.FAILED_PICK_IMPORT_CONSUMPTION_SCHEMA_VERSION
    )
    assert adoption["status"] == "CONSUMED"
    assert adoption["released_by"] == "Ivan"
    assert adoption["release_quote"] == RELEASE_QUOTE
    assert adoption["preimage_pick_row_canonical_sha256"] == (
        entry["failed_pick_import_authority"][
            "state_row_canonical_sha256"
        ]
    )
    registry_binding = adoption["publication_registry"]
    assert registry_binding["path"] == str(registry_path.absolute())
    assert registry_binding["sha256"] == (
        "sha256:" + _sha256(registry_path.read_bytes())
    )

    # The permit cannot be consumed twice: the exact preimage and batch state
    # are gone.  The second authorized attempt is read-only at PREFLIGHT.
    state_after_adoption = fixture.state_path.read_bytes()
    second, code = _run_authorized_failed_pick(fixture)
    assert code == 2
    assert _step(second, "PREFLIGHT")["code"] in {
        "FAILED_PICK_BATCH_PREIMAGE_MISMATCH",
        "FAILED_PICK_ROW_SHAPE_MISMATCH",
    }
    assert fixture.state_path.read_bytes() == state_after_adoption


def test_ordinary_reimport_preserves_failed_pick_consumption_idempotently(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    first, code = _run_authorized_failed_pick(fixture)
    assert code == 0, first
    first_state = fixture.state_path.read_bytes()
    first_backup = Path(_step(first, "STATE_BIND")["state_backup_path"])
    preimage_bytes = first_backup.read_bytes()

    second, code = _run(
        fixture,
        allow_new_pick=False,
        closure_projector=_failed_pick_closure,
    )

    assert code == 0, second
    assert fixture.state_path.read_bytes() == first_state
    assert first_backup.read_bytes() == preimage_bytes
    row = json.loads(first_state)["picks"][0]
    assert (
        row["external_package_import"]["failed_pick_adoption"]["status"]
        == "CONSUMED"
    )


@pytest.mark.parametrize(
    ("adopt_failed_pick", "release_quote", "allow_new_pick", "expected"),
    [
        (CANDIDATE, None, False, "FAILED_PICK_IMPORT_FLAGS_INCOMPLETE"),
        (None, RELEASE_QUOTE, False, "FAILED_PICK_IMPORT_FLAGS_INCOMPLETE"),
        (
            "auto_wrong_candidate",
            RELEASE_QUOTE,
            False,
            "FAILED_PICK_IMPORT_CANDIDATE_MISMATCH",
        ),
        (
            CANDIDATE,
            RELEASE_QUOTE,
            True,
            "FAILED_PICK_IMPORT_MODE_CONFLICT",
        ),
    ],
)
def test_failed_pick_flags_are_exact_and_fail_before_target_writes(
    tmp_path: Path,
    adopt_failed_pick: str | None,
    release_quote: str | None,
    allow_new_pick: bool,
    expected: str,
) -> None:
    fixture = _build_external_package(tmp_path)
    before = fixture.state_path.read_bytes()
    receipt, code = _run(
        fixture,
        allow_new_pick=allow_new_pick,
        adopt_failed_pick=adopt_failed_pick,
        release_quote=release_quote,
    )
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == expected
    assert fixture.state_path.read_bytes() == before
    assert not fixture.destination_package.exists()


def test_failed_pick_release_quote_must_match_registry_exactly(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    before = fixture.state_path.read_bytes()

    receipt, code = _run_authorized_failed_pick(
        fixture, quote=RELEASE_QUOTE.removeprefix("说起来")
    )

    assert code == 2
    assert (
        _step(receipt, "PREFLIGHT")["code"]
        == "FAILED_PICK_RELEASE_QUOTE_MISMATCH"
    )
    assert fixture.state_path.read_bytes() == before
    assert not fixture.destination_package.exists()


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda registry: registry["entries"][0].__setitem__(
                "status", "hold_pending_review"
            ),
            "FAILED_PICK_NOT_RELEASED",
        ),
        (
            lambda registry: registry["entries"][0].__setitem__(
                "released_by", "Claude"
            ),
            "FAILED_PICK_RELEASE_ACTOR_INVALID",
        ),
        (
            lambda registry: registry["entries"].append(
                copy.deepcopy(registry["entries"][0])
            ),
            "FAILED_PICK_RELEASE_NOT_UNIQUE",
        ),
        (
            lambda registry: registry["entries"][0][
                "failed_pick_import_authority"
            ]["required_pick_state"].__setitem__(
                "failure_kind", "speaker_evidence"
            ),
            "FAILED_PICK_IMPORT_AUTHORITY_INVALID",
        ),
    ],
)
def test_failed_pick_registry_authority_is_fail_closed(
    tmp_path: Path,
    mutation,
    expected: str,
) -> None:
    fixture = _build_external_package(tmp_path)
    _state, _entry, registry_path = _configure_authorized_failed_pick(
        fixture
    )
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    mutation(registry)
    _write_json(registry_path, registry)
    _seal_deployed_authority(fixture, registry_path)

    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == expected
    assert not fixture.destination_package.exists()


def test_failed_pick_registry_worktree_drift_refuses_even_if_json_is_valid(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _state, _entry, registry_path = _configure_authorized_failed_pick(fixture)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["entries"][0]["released_at"] = "2026-08-11T23:59:59Z"
    _write_json(registry_path, registry)

    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 2
    assert (
        _step(receipt, "PREFLIGHT")["code"]
        == "FAILED_PICK_REGISTRY_AUTHORITY_INVALID"
    )
    assert not fixture.destination_package.exists()


def test_failed_pick_shape_cannot_be_reauthorized_by_only_rehashing(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _state, _entry, registry_path = _configure_authorized_failed_pick(
        fixture
    )
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["picks"][0]["selected_repair"] = False
    state["publication_closure"] = _failed_pick_closure(state)
    _write_json(fixture.state_path, state)
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["entries"][0]["failed_pick_import_authority"][
        "state_row_canonical_sha256"
    ] = "sha256:" + pi.canonical_json_sha256(state["picks"][0])
    _write_json(registry_path, registry)
    _seal_deployed_authority(fixture, registry_path)

    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 2
    assert (
        _step(receipt, "PREFLIGHT")["code"]
        == "FAILED_PICK_ROW_SHAPE_MISMATCH"
    )
    assert not fixture.destination_package.exists()


def test_failed_pick_row_canonical_drift_refuses_before_copy(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["picks"][0]["hook"] = "drift after Ivan release"
    state["publication_closure"] = _failed_pick_closure(state)
    _write_json(fixture.state_path, state)

    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 2
    assert (
        _step(receipt, "PREFLIGHT")["code"]
        == "FAILED_PICK_ROW_PREIMAGE_DRIFT"
    )
    assert not fixture.destination_package.exists()


@pytest.mark.parametrize("collection", ["pending_talk", "pending_song", "songs"])
def test_failed_pick_adoption_refuses_candidate_in_other_collections(
    tmp_path: Path,
    collection: str,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state[collection].append({"candidate_id": CANDIDATE})
    _write_json(fixture.state_path, state)

    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] in {
        "CANDIDATE_STILL_QUEUED",
        "FAILED_PICK_ADOPTION_SCOPE_CONFLICT",
    }
    assert not fixture.destination_package.exists()


def test_failed_pick_adoption_dry_run_has_zero_target_writes(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    before = fixture.state_path.read_bytes()

    receipt, code = _run_authorized_failed_pick(fixture, apply=False)

    assert code == 0, receipt
    assert receipt["status"] == "DRY_RUN_OK"
    assert receipt["upload_allowed"] is False
    assert fixture.state_path.read_bytes() == before
    assert not fixture.destination_package.exists()
    assert not list(fixture.state_path.parent.glob("*.pre-import-*"))


def test_committed_solo_failed_pick_release_is_exactly_bound() -> None:
    registry_path = (
        Path(__file__).resolve().parents[1]
        / cli.PUBLICATION_REGISTRY_RELATIVE
    )
    authorization = pi.load_failed_pick_import_authorization(
        registry_path=registry_path,
        candidate_id="auto_230125_1157_1229",
        date="2026-08-08",
        release_quote=RELEASE_QUOTE,
    )
    assert authorization.released_by == "Ivan"
    assert authorization.state_row_canonical_sha256 == (
        "66e4d8a48a60d015e5dae3f5fd1de92a02b78c78299e945d983115f8e399a12a"
    )
    assert authorization.authority["required_pick_state"] == {
        "status": "failed",
        "rc": 1,
        "failure_kind": "subtitle_authority",
        "failure_stage": "final_review_findings",
        "selected_repair": True,
    }


def test_published_pick_row_refuses(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["picks"] = [
        {"candidate_id": CANDIDATE, "status": "published", "rc": 0}
    ]
    _write_json(fixture.state_path, state)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "PICK_STATUS_NOT_REBINDABLE"


def test_closure_reported_publication_refuses() -> None:
    """Defense in depth: a published candidate is refused even if its row looks
    rebindable (e.g. the publication lives on the song lane surface)."""

    state = {
        "date": DATE,
        "status": "ready_unpublished",
        "picks": [{"candidate_id": CANDIDATE, "status": "review_ready", "rc": 0}],
    }
    with pytest.raises(pi.PackageImportError) as excinfo:
        pi.check_state_preconditions(
            state,
            candidate_id=CANDIDATE,
            date=DATE,
            allow_new_pick=False,
            project_closure=lambda _state: {
                "status": "ready_unpublished",
                "published_candidate_ids": [CANDIDATE],
            },
        )
    assert excinfo.value.code == "CANDIDATE_ALREADY_PUBLISHED"


def test_bind_refuses_when_publication_membership_would_move(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    _write_json(fixture.state_path, state)

    class MovingClosure:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, value):  # noqa: ANN001
            # calls 1-2 = the two precondition checks, 3 = before image,
            # 4 = after image.  Only the after image sees a moved membership.
            self.calls += 1
            return {
                "status": "ready_unpublished",
                "published_candidate_ids": []
                if self.calls <= 3
                else ["auto_other_candidate"],
                "ready_unpublished_candidate_ids": [],
            }

    receipt, code = _run(fixture, closure_projector=MovingClosure())
    assert code == 2
    step = _step(receipt, "STATE_BIND")
    assert step["code"] == "PUBLICATION_MEMBERSHIP_MOVED"


def test_candidate_still_queued_refuses(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    state["pending_talk"] = [{"candidate_id": CANDIDATE}]
    _write_json(fixture.state_path, state)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "CANDIDATE_STILL_QUEUED"


# ---------------------------------------------------------------- idempotency


def test_repeat_import_is_idempotent(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    first, code = _run(fixture)
    assert code == 0, first
    state_after_first = fixture.state_path.read_bytes()
    record_after_first = (
        fixture.destination_package / f"{CANDIDATE}.record.json"
    ).read_bytes()

    second, code = _run(fixture)
    assert code == 0, second
    assert _step(second, "RELOCATE")["status"] == "ALREADY_RELOCATED"
    assert _step(second, "AUDIT")["write_status"] == "ALREADY_IDENTICAL"
    copy_step = _step(second, "COPY")
    statuses = {row["status"] for row in copy_step["files"]}
    assert statuses <= {"ALREADY_IDENTICAL", "PRESERVED_RELOCATED"}
    # 第二次导入既不重复建行，也不改动 state 字节
    assert fixture.state_path.read_bytes() == state_after_first
    assert (
        fixture.destination_package / f"{CANDIDATE}.record.json"
    ).read_bytes() == record_after_first
    state = json.loads(fixture.state_path.read_text(encoding="utf-8"))
    assert [row["candidate_id"] for row in state["picks"]] == [CANDIDATE]


def test_half_finished_relocation_refuses_instead_of_guessing(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture)
    assert code == 0
    journal_path = (
        fixture.destination_package / f".{CANDIDATE}.package-relocation.json"
    )
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["status"] = "PREPARED"
    journal_path.write_bytes(pi.json_bytes(journal))
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "RELOCATE")
    assert step["code"] == "RELOCATION_JOURNAL_INCONSISTENT"
    assert "pre-relocation" in step["hint"]


def test_relocated_document_replaced_by_source_refuses(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    assert _run(fixture)[1] == 0
    # someone rsynced the producing host's record back over the relocated one
    (fixture.destination_package / f"{CANDIDATE}.record.json").write_bytes(
        (fixture.staging_package / f"{CANDIDATE}.record.json").read_bytes()
    )
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "COPY")["code"] == "RELOCATED_DOCUMENT_DRIFT"


# ---------------------------------------------------------------- gate chain


def test_gate_chain_order_and_programmatic_title(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    gate = StubGateRunner(fixture)
    receipt, code = _run(fixture, gate=gate)
    assert code == 0
    scripts = [Path(call[1]).name for call in gate.calls]
    assert scripts == [
        "build_lidousha_daily_review_manifest.py",
        "audit_lidousha_review_package.py",
        "run_title_cover_joint_qc.py",
    ]
    qc_call = gate.calls[-1]
    # 标题程序化取自 review_manifest：逐字相同，全角引号不被降级成 ASCII
    assert qc_call[3] == TITLE
    assert "“" in qc_call[3]
    assert _step(receipt, "TITLE_COVER_QC")["title_sha256"] == (
        "sha256:" + hashlib.sha256(TITLE.encode("utf-8")).hexdigest()
    )
    audit_path = fixture.destination_package / cli.PACKAGE_AUDIT_NAME
    assert gate.audit_stdout is not None
    assert audit_path.read_bytes() == gate.audit_stdout.encode("utf-8")
    audit_step = _step(receipt, "AUDIT")
    assert audit_step["path"] == str(audit_path)
    assert audit_step["sha256"] == _sha256(gate.audit_stdout.encode("utf-8"))
    assert audit_step["write_status"] == "CREATED"


def test_qc_receipt_bound_to_a_different_title_refuses(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    ascii_title = TITLE.replace("“", '"').replace("”", '"')
    gate = StubGateRunner(fixture, qc_title=ascii_title)
    receipt, code = _run(fixture, gate=gate)
    assert code == 2
    assert _step(receipt, "TITLE_COVER_QC")["code"] == "QC_TITLE_SHA_MISMATCH"


def test_qc_receipt_bound_to_a_non_same_stem_cover_refuses(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    gate = StubGateRunner(
        fixture,
        qc_cover=str(fixture.destination_package / "covers" / "final-cover.png"),
    )
    receipt, code = _run(fixture, gate=gate)
    assert code == 2
    step = _step(receipt, "TITLE_COVER_QC")
    assert step["code"] == "QC_COVER_NOT_SAME_STEM"
    assert "B2" in step["hint"]


def test_blocking_audit_stops_the_chain(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    gate = StubGateRunner(fixture, audit_passed=False)
    receipt, code = _run(fixture, gate=gate)
    assert code == 2
    assert _step(receipt, "AUDIT")["code"] == "PACKAGE_AUDIT_BLOCKED"
    assert "run_title_cover_joint_qc.py" not in [
        Path(call[1]).name for call in gate.calls
    ]


def test_blocking_audit_rolls_back_authorized_failed_pick_consumption(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    before = fixture.state_path.read_bytes()

    receipt, code = _run_authorized_failed_pick(
        fixture,
        gate=StubGateRunner(fixture, audit_passed=False),
    )

    assert code == 2
    assert fixture.state_path.read_bytes() == before
    assert _step(receipt, "STATE_BIND")["status"] == (
        "ROLLED_BACK_AFTER_GATE_FAILURE"
    )
    assert b'"CONSUMED"' not in fixture.state_path.read_bytes()


def test_state_readback_failure_restores_exact_failed_pick_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_external_package(tmp_path)
    _configure_authorized_failed_pick(fixture)
    before = fixture.state_path.read_bytes()

    def corrupting_write_state(path, document, **kwargs):  # noqa: ANN001, ARG001
        pi.atomic_write_bytes(path, b'{"corrupt":true}\n')

    monkeypatch.setattr(cli, "write_state", corrupting_write_state)
    receipt, code = _run_authorized_failed_pick(fixture)

    assert code == 2
    assert fixture.state_path.read_bytes() == before
    assert _step(receipt, "STATE_BIND")["code"] == "STATE_READBACK_FAILED"
    assert b'"CONSUMED"' not in fixture.state_path.read_bytes()


def test_make_manifest_is_never_run(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    gate = StubGateRunner(fixture)
    receipt, code = _run(fixture, gate=gate)
    assert code == 0
    assert receipt["upload_allowed"] is False
    step = _step(receipt, "MAKE_MANIFEST")
    assert step["status"] == "SKIPPED_OUT_OF_SCOPE"
    assert "authorized_upload.py" in " ".join(step["next_command"])
    audit_flag = step["next_command"].index("--package-audit")
    assert step["next_command"][audit_flag + 1] == str(
        fixture.destination_package / cli.PACKAGE_AUDIT_NAME
    )
    assert not any("authorized_upload" in call[1] for call in gate.calls)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    before = fixture.state_path.read_bytes()
    receipt, code = _run(fixture, apply=False)
    assert code == 0
    assert receipt["status"] == "DRY_RUN_OK"
    assert not fixture.destination_package.exists()
    assert fixture.state_path.read_bytes() == before
    assert _step(receipt, "STATE_BIND")["status"] == "SKIPPED_DRY_RUN"
    command = _step(receipt, "MAKE_MANIFEST")["next_command"]
    audit_flag = command.index("--package-audit")
    assert command[audit_flag + 1] == str(
        fixture.destination_package / cli.PACKAGE_AUDIT_NAME
    )


def test_different_existing_audit_report_is_not_overwritten(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    assert _run(fixture)[1] == 0
    audit_path = fixture.destination_package / cli.PACKAGE_AUDIT_NAME
    drift = b'{"passed": true, "root": "different-governed-run"}\n'
    audit_path.write_bytes(drift)

    receipt, code = _run(fixture)

    assert code == 2
    step = _step(receipt, "AUDIT")
    assert step["code"] == "AUDIT_REPORT_OVERWRITE_REFUSED"
    assert audit_path.read_bytes() == drift


def test_symlinked_audit_report_path_is_refused_without_touching_target(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    assert _run(fixture)[1] == 0
    audit_path = fixture.destination_package / cli.PACKAGE_AUDIT_NAME
    audit_path.unlink()
    target = tmp_path / "outside-audit.json"
    target.write_bytes(b"outside-governed-evidence")
    audit_path.symlink_to(target)

    receipt, code = _run(fixture)

    assert code == 2
    step = _step(receipt, "AUDIT")
    assert step["code"] == "AUDIT_REPORT_PATH_UNSAFE"
    assert target.read_bytes() == b"outside-governed-evidence"


# ---------------------------------------------------------------- lane / shape


def test_song_lane_is_refused(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    record_path = fixture.staging_package / f"{CANDIDATE}.record.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["classification"] = "song"
    _write_json(record_path, record)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "LANE_NOT_SUPPORTED"


def test_song_titled_package_without_classification_is_refused(
    tmp_path: Path,
) -> None:
    """车道判定必须与 manifest builder 逐条一致：只看 classification 会让
    没标 classification 的歌切混进 talk 绑定，从而落进 picks 而不是 songs。"""

    fixture = _build_external_package(tmp_path)
    publish_path = fixture.staging_package / f"{CANDIDATE}.recut.publish.json"
    publish = json.loads(publish_path.read_text(encoding="utf-8"))
    publish["title"] = CHANNEL_PROFILE.song_title_prefix + "《怪獣の花唄》"
    _write_json(publish_path, publish)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "LANE_NOT_SUPPORTED"


def test_finalization_style_disagreement_refuses(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    chat_path = fixture.staging_candidate / f"{CANDIDATE}.chat-authority.json"
    chat = json.loads(chat_path.read_text(encoding="utf-8"))
    # declare uniform_host while the package still carries a speaker manifest
    chat["speaker_ass_path"] = None
    chat["speaker_ass_sha256"] = None
    chat["final_speaker_srt_sha256"] = chat["final_text_srt_sha256"]
    _write_json(chat_path, chat)
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "PREFLIGHT")
    assert step["code"] in {
        "FINALIZATION_STYLE_DISAGREEMENT",
        "STAGED_EVIDENCE_SHA_DRIFT",
    }


def test_symlinked_source_file_is_rejected(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    target = fixture.staging_package / "covers" / "final-cover.png"
    link = fixture.staging_package / "covers" / "alias.png"
    os.symlink(target, link)
    receipt, code = _run(fixture)
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "UNSAFE_PATH_SYMLINK"


def test_unsafe_workspace_root_override_refuses(tmp_path: Path) -> None:
    fixture = _build_external_package(tmp_path)
    receipt, code = _run(fixture, source_workspace_root="/")
    assert code == 2
    assert _step(receipt, "PREFLIGHT")["code"] == "UNSAFE_RELOCATION_ROOT"


def _point_speaker_profile_at_producing_repo(fixture: Fixture) -> None:
    for name in (
        f"{CANDIDATE}.recut.speaker-final.json",
        f"{CANDIDATE}.record.json",
    ):
        path = fixture.staging_package / name
        document = json.loads(path.read_text(encoding="utf-8"))
        target = document.get("speaker_finalization", document)
        target["profile"] = f"{SRC_REPO}/assets/lidousha/speaker_profile.json"
        _write_json(path, document)


def test_producing_host_repo_locator_needs_an_explicit_root(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _point_speaker_profile_at_producing_repo(fixture)
    receipt, code = _run(fixture)
    assert code == 2
    step = _step(receipt, "PREFLIGHT")
    assert step["code"] == "SOURCE_REPO_ROOT_UNRESOLVED"
    assert "--source-repo-root" in step["hint"]


def test_explicit_source_repo_root_projects_repo_locators(
    tmp_path: Path,
) -> None:
    fixture = _build_external_package(tmp_path)
    _point_speaker_profile_at_producing_repo(fixture)
    receipt, code = _run(fixture, source_repo_root=SRC_REPO)
    assert code == 0, receipt
    speaker = json.loads(
        (
            fixture.destination_package / f"{CANDIDATE}.recut.speaker-final.json"
        ).read_text(encoding="utf-8")
    )
    assert speaker["profile"] == (
        f"{fixture.repo_root}/assets/lidousha/speaker_profile.json"
    )
    preflight = next(
        row for row in receipt["steps"] if row["step"] == "PREFLIGHT" and "roots" in row
    )
    assert preflight["roots"]["source_repo_root"] == SRC_REPO
