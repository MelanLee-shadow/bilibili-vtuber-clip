"""证据不足时的 best-effort 说话人分离（维护者 第二次裁定）。

出处两句逐字，缺一不可：

  ①「它必须无论如何至少先猜一个说话人，我才能审查，不能猜都不猜」
  ②「我说的猜不是全片统一李豆沙，这样的话我要修正的工作量太大了，我要的就是
     正常分离两说话人，尽最大努力分开，然后再由我改正」

覆盖四条硬要求：
  (a) ``auto`` 档证据不足 → **有成品产出**，且成品标注为猜测；
  (b) 该成品**不可自动上传**、不进自动上传路径（停泊态承载 fail-closed）；
  (c) ``required`` 档行为不变（严格档从不要求降级，证据不足照旧硬失败）；
  (d) 证据充分时仍走真分离，不被回落污染。

外加 维护者 ② 的专项守卫：猜出来的**必须是双人分离**而不是全片统一主播色；
统一色只在梯子最后一级作为兜底，且与前两级在回执里明确可分。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import scripts.session_autoslice as runner
from src.autoslice import speaker_guess, speaker_manual_review
from src.autoslice.addressee_attribution import (
    SpeakerEvidenceRejected,
    SpeakerGuessReviewRequired,
    rebuild_speaker_evidence,
)
from src.autoslice.producer_speaker import (
    SpeakerFinalizationAdapters,
    run_producer_speaker_finalization,
)
from src.autoslice.publish_staging import _stage_publish_draft
from src.autoslice.review_evidence import SourceCue
from src.autoslice.source_fact_staging import resolve_initial_source_fact_review
from src.autoslice.speaker_common import (
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SpeakerIdentityIndeterminate,
)
from src.autoslice.speaker_finalizer import finalize_speaker_subtitles


ANCHOR_SHORTAGE = "not enough 李豆沙 clip anchors: []"


def _fixture(tmp_path: Path) -> dict[str, Path]:
    """两条 cue 的最小片子：够表达"一句主播、一句客人"。"""

    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"synthetic final media")
    text_srt = tmp_path / "text-final.srt"
    text_srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n那我们开始了啊\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n好耶我准备好了\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    references = tmp_path / "refs"
    model = tmp_path / "model"
    references.mkdir()
    model.mkdir()
    return {
        "media": media,
        "text_srt": text_srt,
        "profile": profile,
        "references": references,
        "model": model,
        "srt": tmp_path / "speaker-final.srt",
        "ass": tmp_path / "speaker-final.ass",
        "manifest": tmp_path / "speaker-final.json",
        "work": tmp_path / "speaker-work",
    }


def _two_speaker_analysis(*, guessed: bool) -> dict:
    """真实分离结果的形状：逐句归属 + 逐句声学分数。"""

    return {
        "mode": "multi_speaker",
        "multi_speaker_detected": True,
        "host_anchor_scope": (speaker_guess.GUESSED_HOST_ANCHOR_SCOPE if guessed else "clip"),
        "host_anchor_cues": [1, 2],
        "clip_host_anchor_candidates": [] if guessed else [1, 2],
        "policy": {"host_session_seed_min": 0.62},
        "context_required_cues": [2],
        "decisions": [
            {
                "source_index": 1,
                "speaker": HOST_SPEAKER,
                "decision_source": "campp_audio",
                "seed_score": 0.41,
                "host_score": 0.55,
                "guest_score": 0.20,
                "margin": 0.35,
            },
            {
                "source_index": 2,
                "speaker": GUEST_SPEAKER,
                "decision_source": "guest_default_ambiguity",
                "seed_score": 0.11,
                "host_score": 0.18,
                "guest_score": 0.49,
                "margin": -0.31,
            },
        ],
    }


def _shortage_then_guess(analysis: dict | None = None):
    """第一次（严格）锚点不足；只有带 guess_host_anchors 的第二次才出分离。"""

    calls: list[bool] = []

    def analyzer(**kwargs):
        guessed = bool(kwargs.get("guess_host_anchors"))
        calls.append(guessed)
        if not guessed:
            raise SpeakerIdentityIndeterminate(ANCHOR_SHORTAGE)
        return analysis if analysis is not None else _two_speaker_analysis(guessed=True)

    return analyzer, calls


def _finalize(inputs: dict[str, Path], analyzer, **extra) -> dict:
    return finalize_speaker_subtitles(
        media_path=inputs["media"],
        text_srt_path=inputs["text_srt"],
        profile_path=inputs["profile"],
        reference_dir=inputs["references"],
        model_dir=inputs["model"],
        output_srt_path=inputs["srt"],
        output_ass_path=inputs["ass"],
        output_manifest_path=inputs["manifest"],
        work_dir=inputs["work"],
        candidate_id="auto_220747_1271_1323",
        analyzer=analyzer,
        **extra,
    )


def _guess_evidence_bundle(
    tmp_path: Path,
) -> tuple[
    dict[str, Path],
    dict[str, object],
    list[SourceCue],
    bytes,
    bytes,
]:
    inputs = _fixture(tmp_path)
    analyzer, _calls = _shortage_then_guess()
    manifest = _finalize(inputs, analyzer, best_effort_guess=True)
    speaker_srt_bytes = inputs["srt"].read_bytes()
    manifest_bytes = inputs["manifest"].read_bytes()
    plain_sha = hashlib.sha256(inputs["text_srt"].read_bytes()).hexdigest()
    record: dict[str, object] = {
        "speaker_mode": "auto",
        "speaker_review_srt_path": str(inputs["srt"]),
        "speaker_finalization_manifest_path": str(inputs["manifest"]),
        "speaker_finalization_manifest_sha256": (
            "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        ),
        "speaker_finalization": manifest,
        "artifact_hashes": {
            "subtitle_sha256": "sha256:" + plain_sha,
            "speaker_review_srt_sha256": (
                "sha256:" + hashlib.sha256(speaker_srt_bytes).hexdigest()
            ),
        },
    }
    cues = [
        SourceCue("1", 0, 2_000, "那我们开始了啊", "zh", "speech", 1.0),
        SourceCue("2", 2_000, 4_000, "好耶我准备好了", "zh", "speech", 1.0),
    ]
    return inputs, record, cues, speaker_srt_bytes, manifest_bytes


# --- source-fact speaker-evidence 边界 --------------------------------------


def test_hash_bound_guess_is_review_only_not_addressee_evidence(
    tmp_path: Path,
) -> None:
    _inputs, record, cues, speaker_srt_bytes, manifest_bytes = _guess_evidence_bundle(tmp_path)

    with pytest.raises(SpeakerGuessReviewRequired) as error:
        rebuild_speaker_evidence(
            record,
            cues,
            speaker_srt_bytes=speaker_srt_bytes,
            speaker_manifest_bytes=manifest_bytes,
        )

    assert error.value.code == "SPEAKER_GUESS_REQUIRES_HUMAN_REVIEW"


def test_source_fact_provider_is_not_called_for_hash_bound_guess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inputs, record, cues, _speaker_srt_bytes, _manifest_bytes = _guess_evidence_bundle(tmp_path)
    monkeypatch.setattr(
        "src.autoslice.source_fact_staging.load_manual_title_keep_authority",
        lambda _candidate_id: (_ for _ in ()).throw(
            AssertionError("manual source-fact authority loaded for SPEAKER_GUESS")
        ),
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_staging.load_deterministic_text_surface_authority",
        lambda _candidate_id: (_ for _ in ()).throw(
            AssertionError("deterministic source-fact authority loaded for SPEAKER_GUESS")
        ),
    )
    calls = 0

    def source_fact_provider(_prompt: str) -> str:
        nonlocal calls
        calls += 1
        raise AssertionError("source-fact provider ran on SPEAKER_GUESS")

    resolution = resolve_initial_source_fact_review(
        candidate_id="auto_220747_1271_1323",
        title_source="fallback",
        title="【李豆沙】测试标题",
        selection_hook="测试 hook",
        story_contract={},
        record=record,
        cues=cues,
        source_fact_llm_call=source_fact_provider,
        recovery_publication_authority=None,
        title_authority_status="RESOLVED_FALLBACK",
        title_llm_enabled=False,
        prior_authority_error=None,
    )

    assert calls == 0
    assert resolution.review is None
    assert resolution.violation is None
    assert resolution.authority_error is None
    assert resolution.authority_status == "RESOLVED_FALLBACK"


def test_publish_staging_routes_hash_bound_guess_to_review_without_source_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, record, cues, _speaker_srt_bytes, _manifest_bytes = _guess_evidence_bundle(tmp_path)
    record.update(
        {
            "status": "MATERIALIZED",
            "media_path": str(inputs["media"]),
            "subtitle_path": str(inputs["text_srt"]),
        }
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_staging.load_manual_title_keep_authority",
        lambda _candidate_id: (_ for _ in ()).throw(
            AssertionError("manual source-fact authority loaded for SPEAKER_GUESS")
        ),
    )
    monkeypatch.setattr(
        "src.autoslice.source_fact_staging.load_deterministic_text_surface_authority",
        lambda _candidate_id: (_ for _ in ()).throw(
            AssertionError("deterministic source-fact authority loaded for SPEAKER_GUESS")
        ),
    )
    provider_calls = 0
    cover_calls = 0

    def source_fact_provider(_prompt: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("source-fact provider ran on SPEAKER_GUESS")

    def stage_cover(_record: dict[str, object], **_kwargs: object) -> dict[str, object]:
        nonlocal cover_calls
        cover_calls += 1
        cover_path = tmp_path / "guess-review.cover.png"
        cover_path.write_bytes(b"review cover")
        return {
            "status": "AI_COVER_READY",
            "cover_path": str(cover_path),
            "cover_generation": {"status": "READY"},
            "reason_codes": [],
        }

    staged = _stage_publish_draft(
        record,
        candidate_id="auto_220747_1271_1323",
        title="【李豆沙】猜测说话人等待人工复核",
        cues=cues,
        run_ffmpeg=False,
        title_llm_call=None,
        selection_hook="猜测说话人等待人工复核",
        source_fact_llm_call=source_fact_provider,
        stage_cover=stage_cover,
    )

    assert staged is not None
    publish = staged["publish_staging"]
    assert provider_calls == 0
    assert cover_calls == 1
    assert publish["source_fact_review"] is None
    assert publish["status"] == "STAGED"
    assert publish["title_authority_status"] != "BLOCKED_SOURCE_FACT_REVIEW"
    assert "source_fact_review_failed" not in publish["title_policy_violations"]
    assert publish["cover_status"] == "AI_COVER_READY"
    assert publish["upload_enabled"] is False


@pytest.mark.parametrize(
    ("tamper", "detail"),
    [
        (
            lambda receipt: receipt.__setitem__("upload_authorized", True),
            "invalid upload_authorized",
        ),
        (
            lambda receipt: receipt.__setitem__("rung_label", "错误层级"),
            "rung label differs",
        ),
        (
            lambda receipt: receipt.__setitem__("low_confidence_cue_count", 1),
            "low-confidence counts are incoherent",
        ),
        (
            lambda receipt: receipt["low_confidence_cues"][1].__setitem__("source_index", 1),
            "cue indices are invalid or duplicated",
        ),
        (
            lambda receipt: receipt.__setitem__("speaker_counts", {HOST_SPEAKER: 1}),
            "speaker counts are incoherent",
        ),
    ],
)
def test_rebound_guess_with_tampered_receipt_is_rejected(
    tmp_path: Path,
    tamper,
    detail: str,
) -> None:
    inputs, record, cues, speaker_srt_bytes, _manifest_bytes = _guess_evidence_bundle(tmp_path)
    manifest = json.loads(inputs["manifest"].read_text(encoding="utf-8"))
    tamper(manifest["speaker_guess"])
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    inputs["manifest"].write_bytes(manifest_bytes)
    record["speaker_finalization"] = manifest
    record["speaker_finalization_manifest_sha256"] = (
        "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    )
    provider_calls = 0

    def source_fact_provider(_prompt: str) -> str:
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("source-fact provider ran on tampered SPEAKER_GUESS")

    with pytest.raises(
        SpeakerEvidenceRejected,
        match=f"SPEAKER_GUESS_RECEIPT_INVALID: .*{detail}",
    ):
        rebuild_speaker_evidence(
            record,
            cues,
            speaker_srt_bytes=speaker_srt_bytes,
            speaker_manifest_bytes=manifest_bytes,
        )
    with pytest.raises(
        SpeakerEvidenceRejected,
        match=f"SPEAKER_GUESS_RECEIPT_INVALID: .*{detail}",
    ):
        resolve_initial_source_fact_review(
            candidate_id="auto_220747_1271_1323",
            title_source="fallback",
            title="【李豆沙】测试标题",
            selection_hook="测试 hook",
            story_contract={},
            record=record,
            cues=cues,
            source_fact_llm_call=source_fact_provider,
            recovery_publication_authority=None,
            title_authority_status="RESOLVED_FALLBACK",
            title_llm_enabled=False,
            prior_authority_error=None,
        )
    assert provider_calls == 0


# --- (a) 证据不足仍有成品，且标注为猜 ---------------------------------------


def test_auto_evidence_shortage_still_produces_a_marked_guess(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    analyzer, calls = _shortage_then_guess()

    manifest = _finalize(inputs, analyzer, best_effort_guess=True)

    # 有产物：这正是 b4d4000 缺的那一半（当时 rc=1，维护者 打开什么也看不到）。
    assert inputs["srt"].is_file() and inputs["ass"].is_file()
    assert calls == [False, True]
    # 标注为猜：状态与 READY 全程可分，且永不自称 production_ready。
    assert manifest["status"] == speaker_guess.SPEAKER_GUESS_STATUS
    assert manifest["production_ready"] is False
    assert json.loads(inputs["manifest"].read_text(encoding="utf-8"))["status"] == (
        speaker_guess.SPEAKER_GUESS_STATUS
    )
    receipt = manifest["speaker_guess"]
    assert receipt["rung"] == speaker_guess.RUNG_GUESSED_ANCHORS
    assert receipt["speaker_authority"] == "GUESSED_NOT_EVIDENCE_BACKED"
    assert receipt["upload_authorized"] is False
    assert ANCHOR_SHORTAGE in receipt["blocked_reason"]


def test_guess_is_a_real_two_speaker_separation_not_uniform_host(
    tmp_path: Path,
) -> None:
    """维护者 ② 的专项守卫：统一色会把归属全抹平，他的修正量就爆了。"""

    inputs = _fixture(tmp_path)
    analyzer, _calls = _shortage_then_guess()

    manifest = _finalize(inputs, analyzer, best_effort_guess=True)

    speakers = [row["speaker"] for row in manifest["final_decisions"]]
    assert set(speakers) == {HOST_SPEAKER, GUEST_SPEAKER}
    assert manifest["speaker_guess"]["speaker_counts"] == {
        HOST_SPEAKER: 1,
        GUEST_SPEAKER: 1,
    }
    # 烧字幕用的 ASS 真的是双色，不是单一主播样式。
    ass = inputs["ass"].read_text(encoding="utf-8")
    assert "Style: LDS" in ass and "Style: GUEST" in ass
    assert ",LDS," in ass and ",GUEST," in ass


def test_guess_receipt_points_at_the_cues_that_need_correcting(
    tmp_path: Path,
) -> None:
    """ "哪几句没证据"必须逐句可定位，而不是整片一个"不可信"标记。"""

    inputs = _fixture(tmp_path)
    analyzer, _calls = _shortage_then_guess()

    receipt = _finalize(inputs, analyzer, best_effort_guess=True)["speaker_guess"]

    # 提名件逐条自陈没清过阈值——阈值一字未动，是明示降级不是放宽门。
    assert receipt["thresholds_unchanged"] is True
    assert receipt["host_session_seed_min"] == 0.62
    assert [row["clears_host_session_seed_min"] for row in receipt["nominated_host_anchors"]] == [
        False,
        False,
    ]
    assert receipt["clip_host_anchor_candidates"] == []
    # 整体极性可能反（被提名成主播的那簇其实是客人）——必须明写，不让 维护者 自己推。
    assert receipt["host_guest_polarity"] == "GUESSED_FROM_TOP_SEED_CUES_MAY_BE_INVERTED"
    rows = {row["source_index"]: row for row in receipt["low_confidence_cues"]}
    assert receipt["low_confidence_cue_count"] == 2
    # 锚点是猜的 → 每句都带这条缺口；第 2 句另有声学模糊带缺口。
    assert speaker_guess.GAP_HOST_ANCHOR_GUESSED in rows[1]["reason_codes"]
    assert speaker_guess.GAP_AMBIGUOUS_MARGIN in rows[2]["reason_codes"]
    assert rows[2]["margin"] == -0.31 and rows[2]["speaker"] == GUEST_SPEAKER


# --- 梯子的其余两级：可区分，且有底 -----------------------------------------


def test_unresolved_context_rung_delivers_instead_of_deleting_products(
    tmp_path: Path,
) -> None:
    """分离跑完了、只是若干 cue 语境未定——原本会删产物交 REVIEW_REQUIRED。"""

    inputs = _fixture(tmp_path)
    analysis = _two_speaker_analysis(guessed=False)
    analysis["context_unresolved_cues"] = [2]

    manifest = _finalize(inputs, lambda **_kwargs: analysis, best_effort_guess=True)

    assert inputs["srt"].is_file() and inputs["ass"].is_file()
    assert manifest["status"] == speaker_guess.SPEAKER_GUESS_STATUS
    receipt = manifest["speaker_guess"]
    assert receipt["rung"] == speaker_guess.RUNG_UNRESOLVED_CONTEXT
    # 锚点没猜过 → 不该出现 HOST_ANCHOR_GUESSED，只有那条真未定的 cue 被点名。
    rows = {row["source_index"]: row for row in receipt["low_confidence_cues"]}
    assert list(rows) == [2]
    assert rows[2]["reason_codes"] == [speaker_guess.GAP_CONTEXT_UNRESOLVED]
    assert set(row["speaker"] for row in manifest["final_decisions"]) == {
        HOST_SPEAKER,
        GUEST_SPEAKER,
    }


def test_ladder_bottom_rung_is_uniform_host_and_stays_distinguishable(
    tmp_path: Path,
) -> None:
    """连提名锚点都救不回来时才统一主播色，且回执明写"分离没跑成"。"""

    inputs = _fixture(tmp_path)

    def always_indeterminate(**kwargs):
        raise SpeakerIdentityIndeterminate(
            "guest evidence exists but purified guest anchors are insufficient: []"
            if kwargs.get("guess_host_anchors")
            else ANCHOR_SHORTAGE
        )

    manifest = _finalize(inputs, always_indeterminate, best_effort_guess=True)

    assert inputs["srt"].is_file() and inputs["ass"].is_file()
    receipt = manifest["speaker_guess"]
    assert receipt["rung"] == speaker_guess.RUNG_UNIFORM_HOST
    assert receipt["rung"] != speaker_guess.RUNG_GUESSED_ANCHORS
    assert receipt["evidence_source"] == "none_acoustic_identity_indeterminate"
    assert [row["speaker"] for row in manifest["final_decisions"]] == [
        HOST_SPEAKER,
        HOST_SPEAKER,
    ]
    # 兜底件每一句都自陈"没有声学分离"，维护者 一眼知道这条要整片重标。
    assert all(
        speaker_guess.GAP_NO_SEPARATION in row["reason_codes"]
        for row in receipt["low_confidence_cues"]
    )


# --- (c) required 档语义不动 -------------------------------------------------


def test_required_mode_never_requests_a_guess(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_binary(**kwargs):
        seen.update(kwargs)
        return {"status": "READY", "production_ready": True}

    for mode, expected in (("required", False), ("auto", True)):
        seen.clear()
        run_producer_speaker_finalization(
            speaker_mode=mode,
            host="localhost",
            candidate_id="auto_213135_62_138",
            media_path=tmp_path / "media.mp4",
            text_srt_path=tmp_path / "text.srt",
            output_srt_path=tmp_path / "out.srt",
            output_ass_path=tmp_path / "out.ass",
            output_manifest_path=tmp_path / "out.json",
            work_dir=tmp_path / "work",
            spec={},
            spec_parent=tmp_path,
            override_path=None,
            source_session_anchor_path=None,
            mixed_overlap_evidence_path=None,
            speaker_python=tmp_path / "python",
            final_source_start_ms=None,
            final_source_end_ms=None,
            adapters=SpeakerFinalizationAdapters(run_binary_finalizer=fake_binary),
        )
        assert seen["best_effort_guess"] is expected


def test_without_the_guess_flag_evidence_shortage_still_hard_fails(
    tmp_path: Path,
) -> None:
    """严格档（以及任何没要过降级的调用）行为一个字节不变：抛原异常、零产物。"""

    inputs = _fixture(tmp_path)
    analyzer, calls = _shortage_then_guess()

    with pytest.raises(SpeakerIdentityIndeterminate, match="clip anchors"):
        _finalize(inputs, analyzer)

    assert calls == [False]
    assert not inputs["srt"].exists() and not inputs["ass"].exists()


def test_producer_refuses_an_unrequested_or_unreceipted_guess() -> None:
    guessed = {
        "status": speaker_guess.SPEAKER_GUESS_STATUS,
        "production_ready": False,
        "speaker_guess": {"rung": speaker_guess.RUNG_GUESSED_ANCHORS},
    }
    # 没主动要过降级却收到 guess = 篡改，拒收。
    assert (
        speaker_guess.finalizer_manifest_block_reason(guessed, best_effort_guess=False)
        == "unrequested or unreceipted speaker guess"
    )
    # 要过降级 + 自带回执才收。
    assert speaker_guess.finalizer_manifest_block_reason(guessed, best_effort_guess=True) is None
    # guess 永远不许自称 production_ready。
    assert (
        speaker_guess.finalizer_manifest_block_reason(
            {**guessed, "production_ready": True}, best_effort_guess=True
        )
        == "speaker guess must never claim production_ready"
    )
    # READY 的判据一字未动。
    assert (
        speaker_guess.finalizer_manifest_block_reason(
            {"status": "READY", "production_ready": True}, best_effort_guess=True
        )
        is None
    )
    assert (
        speaker_guess.finalizer_manifest_block_reason(
            {"status": "BLOCKED", "reason": "no profile"}, best_effort_guess=True
        )
        == "no profile"
    )


# --- (d) 证据充分不被回落污染 -----------------------------------------------


def test_sufficient_evidence_still_takes_the_real_separation(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    calls: list[bool] = []

    def analyzer(**kwargs):
        calls.append(bool(kwargs.get("guess_host_anchors")))
        return _two_speaker_analysis(guessed=False)

    manifest = _finalize(inputs, analyzer, best_effort_guess=True)

    # 严格那一趟就成功了：不重跑、不盖 guess 回执、状态仍是 READY。
    assert calls == [False]
    assert manifest["status"] == "READY"
    assert manifest["production_ready"] is True
    assert manifest["speaker_guess"] is None
    assert manifest["host_anchor_scope"] == "clip"


def test_anchor_nomination_never_moves_the_threshold() -> None:
    """提名只改"选谁当锚点"，不改任何阈值数值。"""

    result = speaker_guess.nominate_host_anchors(
        [0.10, 0.55, 0.05, 0.40], anchor_count=2, seed_min=0.62
    )
    assert result is not None
    indices, rows = result
    assert indices == [1, 3]  # seed 分最高的两条，仍然全部低于 0.62
    assert all(row["host_session_seed_min"] == 0.62 for row in rows)
    assert not any(row["clears_host_session_seed_min"] for row in rows)
    # cue 少于两条时连"两个说话人"的前提都不成立 → 交回上层降到兜底级。
    assert speaker_guess.nominate_host_anchors([0.3], anchor_count=2, seed_min=0.62) is None


# --- (b) 猜出来的成品不可自动上传 -------------------------------------------


def _delivered_result(guess_digest: dict | None) -> dict:
    summary = {
        "delivery": "/opt/bilive/autoslice/delivery/2026-08-07/hook.mp4",
        "subtitle": "/opt/bilive/autoslice/delivery/2026-08-07/hook.srt",
        "speaker_subtitle": "/opt/bilive/autoslice/delivery/2026-08-07/hook.speaker.srt",
        "speaker_ass": "/opt/bilive/autoslice/delivery/2026-08-07/hook.speaker.ass",
        "speaker_status": (speaker_guess.SPEAKER_GUESS_STATUS if guess_digest else "OFF"),
    }
    if guess_digest:
        summary["speaker_guess"] = guess_digest
    return {"candidate_id": "auto_220747_1271_1323", "summary": summary}


def test_guessed_delivery_parks_and_is_not_auto_uploadable() -> None:
    result = _delivered_result(
        {
            "rung": speaker_guess.RUNG_GUESSED_ANCHORS,
            "rung_label": speaker_guess.RUNG_LABELS[speaker_guess.RUNG_GUESSED_ANCHORS],
            "low_confidence_cue_count": 2,
            "blocked_reason": ANCHOR_SHORTAGE,
        }
    )

    status = speaker_guess.delivered_talk_status(result, candidate_id="auto_220747_1271_1323")

    # fail-closed 判据：停泊态不在 DELIVERED_TALK_STATUSES 里 ⇒ 不可上传、
    # 不进 review_ready、进不了日审清单（后者要求 status == "review_ready"）。
    assert status in speaker_manual_review.SPEAKER_MANUAL_REVIEW_STATUSES
    assert status not in runner.DELIVERED_TALK_STATUSES
    assert result["status"] == status
    assert speaker_manual_review.is_speaker_manual_review_hold(result)
    receipt = result["speaker_manual_review"]
    assert receipt["upload_authorized"] is False
    assert receipt["disposition"] == ("AWAITING_HUMAN_SPEAKER_CORRECTION_ON_GUESSED_DELIVERY")
    # 成品路径写进 state：既是 维护者 的审阅入口，也挡住容量清理误删。
    assert receipt["review_artifacts"]["burned_video"].endswith("hook.mp4")
    assert receipt["review_artifacts"]["speaker_ass"].endswith(".speaker.ass")
    # rc==0 的路径上 classify_talk_failure 不会跑，失败面字段必须手工补齐——
    # 尤其恢复指纹：少了它 requeue 会拿全量 pipeline 指纹，任何改动都重烧一遍。
    assert result["failure_kind"] == "speaker_evidence"
    assert result["failure_stage"] == "speaker_finalization"
    assert result["failure_recoverable"] is False
    assert "retry_after_seconds" not in result
    assert "next_retry_at_epoch" not in result
    assert "next_retry_at" not in result
    assert result["failure_recovery_fingerprint"] == (
        runner.talk_failure_recovery_fingerprint("speaker_evidence", "auto_220747_1271_1323")
    )


def test_guess_park_outranks_the_cover_pending_lane() -> None:
    """封面待定车道会**直接**把状态提成 review_ready（不重跑 produce）。

    ``cover_maintenance`` 绑定成功时执行 ``rec["status"] = "review_ready"``。
    猜出来的成品若先掉进封面待定，就会绕过停泊、经封面车道升进日审清单并变成
    可上传——所以停泊必须先判、优先级高于封面。
    """

    result = _delivered_result({"rung": speaker_guess.RUNG_GUESSED_ANCHORS})

    status = speaker_guess.finalize_delivered_talk_status(
        result,
        candidate_id="auto_220747_1271_1323",
        work_dir="/nonexistent",
        cover_ready=False,  # 封面没就绪，旧次序会在这里返回 cover_pending
    )

    assert status in speaker_manual_review.SPEAKER_MANUAL_REVIEW_STATUSES
    assert status != runner.TALK_COVER_PENDING_STATUS
    assert result["status"] == status
    assert "cover_pending_reason_codes" not in result
    # 反面：非猜的成品照旧走封面待定，那条既有行为一个字节不变。
    plain = _delivered_result(None)
    assert (
        speaker_guess.finalize_delivered_talk_status(
            plain,
            candidate_id="auto_213135_62_138",
            work_dir="/nonexistent",
            cover_ready=False,
        )
        == runner.TALK_COVER_PENDING_STATUS
    )
    assert plain["cover_pending_reason_codes"] == ["TALK_DELIVERY_COVER_PROOF_REQUIRED"]


def test_truncated_stdout_summary_still_parks_via_the_speaker_manifest(
    tmp_path: Path,
) -> None:
    """检测失败必须 fail-closed：摘要撑破 4000 字节尾窗不能把猜的放成 review_ready。

    ``last_json_block`` 在尾窗被截断时会退而匹配到某个嵌套对象（这里模拟成
    ``timing_qa``），``speaker_status`` 就读不到了。落盘的 speaker manifest 是
    权威源，必须兜住。
    """

    recuts = tmp_path / "replacement_recuts"
    recuts.mkdir(parents=True)
    (recuts / "auto_220747_1271_1323.recut.speaker-final.json").write_text(
        json.dumps(
            {
                "status": speaker_guess.SPEAKER_GUESS_STATUS,
                "speaker_guess": {
                    "rung": speaker_guess.RUNG_GUESSED_ANCHORS,
                    "low_confidence_cue_count": 9,
                },
            }
        ),
        encoding="utf-8",
    )
    truncated = {"candidate_id": "auto_220747_1271_1323", "summary": {"counts": {}}}

    status = speaker_guess.finalize_delivered_talk_status(
        truncated,
        candidate_id="auto_220747_1271_1323",
        work_dir=tmp_path,
        cover_ready=True,
    )

    assert status in speaker_manual_review.SPEAKER_MANUAL_REVIEW_STATUSES
    assert status not in runner.DELIVERED_TALK_STATUSES
    assert truncated["speaker_manual_review"]["speaker_guess"]["low_confidence_cue_count"] == 9


def test_backfill_policy_repark_keeps_the_guess_and_the_artifacts() -> None:
    """同一条 pick 会被盖两次章，第二次不许把成品面抹掉。

    produce 收尾先盖（带成品与 guess 回执），紧接着运行时主循环的
    ``apply_talk_backfill_rejection_policy`` 按状态又盖一次（只知道原因码）。
    """

    from src.autoslice.delivery_recovery import apply_talk_backfill_rejection_policy

    result = _delivered_result({"rung": speaker_guess.RUNG_GUESSED_ANCHORS})
    speaker_guess.delivered_talk_status(result, candidate_id="auto_220747_1271_1323")

    assert apply_talk_backfill_rejection_policy(result, exact_selected=False) is True

    receipt = result["speaker_manual_review"]
    assert receipt["speaker_guess"]["rung"] == speaker_guess.RUNG_GUESSED_ANCHORS
    assert receipt["review_artifacts"]["burned_video"].endswith("hook.mp4")
    assert receipt["upload_authorized"] is False
    # 停泊仍然不铸 candidate_rejected 化石（b4d4000 的裁定不被本次改动推翻）。
    assert result["status"] in speaker_manual_review.SPEAKER_MANUAL_REVIEW_STATUSES
    assert "rejected_status" not in result


def test_unresolved_context_guess_parks_under_the_review_required_name() -> None:
    result = _delivered_result({"rung": speaker_guess.RUNG_UNRESOLVED_CONTEXT})
    assert (
        speaker_guess.delivered_talk_status(result, candidate_id="auto_213135_62_138")
        == "speaker_review_required"
    )


def test_non_guessed_delivery_still_reaches_review_ready() -> None:
    result = _delivered_result(None)
    assert (
        speaker_guess.delivered_talk_status(result, candidate_id="auto_213135_62_138")
        == "review_ready"
    )
    assert "speaker_manual_review" not in result


def test_summary_digest_only_fires_on_a_receipted_guess_manifest() -> None:
    assert speaker_guess.summary_digest(None) is None
    assert speaker_guess.summary_digest({"status": "READY"}) is None
    assert speaker_guess.summary_digest({"status": speaker_guess.SPEAKER_GUESS_STATUS}) is None
    digest = speaker_guess.summary_digest(
        {
            "status": speaker_guess.SPEAKER_GUESS_STATUS,
            "speaker_guess": {
                "rung": speaker_guess.RUNG_GUESSED_ANCHORS,
                "low_confidence_cue_count": 7,
                "blocked_reason": ANCHOR_SHORTAGE,
            },
        }
    )
    assert digest["rung"] == speaker_guess.RUNG_GUESSED_ANCHORS
    assert digest["low_confidence_cue_count"] == 7
    assert digest["upload_authorized"] is False


def test_report_shows_the_guessed_artifact_and_the_cue_count() -> None:
    result = _delivered_result(
        {
            "rung": speaker_guess.RUNG_GUESSED_ANCHORS,
            "rung_label": speaker_guess.RUNG_LABELS[speaker_guess.RUNG_GUESSED_ANCHORS],
            "low_confidence_cue_count": 3,
        }
    )
    speaker_guess.delivered_talk_status(result, candidate_id="auto_220747_1271_1323")

    lines = "\n".join(speaker_manual_review.render_report_section([result]))

    assert "猜主播锚点后完整双人分离" in lines
    assert "hook.mp4" in lines
    assert "| 3 |" in lines
