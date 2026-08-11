"""交付快路径：产出注定被后置所有权覆盖时，跳过它并留 typed 回执。

这里只住「谁拥有最终词面」的判定和它授权跳过的阶段包装，两条：

1. ``pinned_replay_reviewed_text_ownership``（2026-08-02 提速②，commit
   6261842）——已发布件的钉死重放修复：v2 精确区间重放拥有全部文本，
   ``verified_public_exact`` 拥有标题。
2. ``resolve_truth_full_ownership``（F20，Ivan 2026-08-09 立项）——人工真值
   交付车道：``materialize_reviewed_speaker_truth_delivery`` 把 hash-bound
   的 Ivan 真值编译成 reviewed 基线，并在基线 manifest 上盖一张
   ``truth-full-ownership-pin.v1``。pin 证明这份基线的**每一条**交付 cue 都
   由真值拥有词面（Ivan 复核的 override + 他留给机器判说话人但有正面人声
   仲裁的 cue），基线又以 exact_interval_replay 逐字节重放回交付面。

两条的共同论证形状是一样的：被跳过阶段的产出**注定被覆盖**，所以它只是
纯等待，不是安全边际。fail-closed 因此不受影响，也**不允许**顺手扩大：

- 跳过的都是「发现/改写」侧（审片员、声学证人、微 cue 候选盲声学发现）；
- 保留的都是「把关」侧：重放自身的基线 sha 校验、exact-final 终审
  （discovery=COMPLETE 硬门）、边界语义评审、终局 owner 逐字节校验、
  说话人定稿、渲染/烧录与上传前硬门。

判定失败（pin 缺失、形状不符、sha 不自洽）一律返回 ``None``，整条候选回落
正常全链——不做「按 cue 混合跳过」这种新语义。
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Iterable, Mapping, Sequence

from src.autoslice.chat_repair import apply_audio_entity_verification
from src.autoslice.microcue_acoustic_discovery import (
    SCHEMA_VERSION as MICROCUE_SCHEMA_VERSION,
    discover_microcue_findings,
)
from src.autoslice.restatement_recall import merge_restatement_priority_findings
from src.autoslice.subtitle_fidelity import valid_redelivery_baseline_config


TRUTH_FULL_OWNERSHIP_SCHEMA = "truth_full_ownership.v1"
TRUTH_FULL_OWNERSHIP_PIN_SCHEMA = "truth-full-ownership-pin.v1"
EXACT_REPLAY_BASELINE_SCHEMA = "subtitle-redelivery-baseline.v2"
FINAL_REVIEW_AUDIT_SCHEMA = "final-review-audit.v1"

# 回执里 declared 的跳过清单：每一项都带「为什么它的产出到不了交付」。
TRUTH_FULL_OWNERSHIP_SKIPPED_STAGES = (
    {
        "stage": "producer_text_pipeline._run_final_review",
        "kind": "cpa_reviewer_and_repair",
        "reason_code": "REVIEWER_OUTPUT_OVERWRITTEN_BY_EXACT_REPLAY",
    },
    {
        "stage": "chat_repair.apply_audio_entity_verification",
        "kind": "acoustic_witness",
        "reason_code": "TRANSCRIPT_ENTITY_SURFACE_OWNED_BY_TRUTH",
    },
    {
        "stage": "microcue_acoustic_discovery.discover_microcue_findings",
        "kind": "acoustic_witness",
        "reason_code": "MICROCUE_TEXT_OWNED_BY_TRUTH",
    },
    {
        "stage": "restatement_recall.merge_restatement_priority_findings",
        "kind": "priority_finding_injection",
        "reason_code": "RESTATEMENT_REPAIR_TARGETS_OWNED_BY_TRUTH",
    },
)

TRUTH_FULL_OWNERSHIP_PRESERVED_STAGES = (
    "redelivery_subtitle_baseline.apply_redelivery_subtitle_baseline",
    "producer_text_pipeline._run_exact_final_release_review",
    "boundary_semantic_review.review_final_boundary_semantics",
    "producer_package_finalization._verify_final_authority",
    "speaker_finalizer.finalize_speaker_subtitles",
    "producer_package_finalization._build_and_burn_record",
)

TRUTH_FULL_OWNERSHIP_CONDITIONS = (
    "spec.subtitle_redelivery_baseline is a valid subtitle-redelivery-baseline.v2 "
    "config with exact_interval_replay=true",
    "that baseline carries a truth-full-ownership-pin.v1 whose baseline_sha256 "
    "equals the baseline config sha256",
    "the pin's reviewed_override_count + machine_cues cover its whole cue_count",
    "the pin binds one repository-relative Ivan truth input by sha256",
    "no reviewed text override is in play (the producer already refuses that pair)",
)


def _clean_sha256(value: object) -> str:
    text = str(value or "").strip().removeprefix("sha256:")
    if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
        return text
    return ""


def _exact_replay_baseline(spec: Mapping[str, object]) -> Mapping[str, object] | None:
    baseline = spec.get("subtitle_redelivery_baseline")
    if (
        valid_redelivery_baseline_config(baseline)
        and isinstance(baseline, Mapping)
        and baseline.get("schema_version") == EXACT_REPLAY_BASELINE_SCHEMA
        and baseline.get("exact_interval_replay") is True
    ):
        return baseline
    return None


def pinned_replay_reviewed_text_ownership(
    spec: Mapping[str, object],
) -> dict[str, object] | None:
    """判定「审片员产出注定被覆盖」的钉死重放修复模式。

    两个后置所有权同时成立才返回披露块：v2 exact_interval_replay 基线拥有
    全部非真值文本（逐字节重放已发布 reviewed SRT），verified_public_exact
    出版权威拥有标题。此时审片员（_run_final_review）对一次性 ASR 文本的
    发现与修复不可能到达交付（r19 实证：交付 diff=恰真值台账句），其 1-2
    分钟深推理 CPA 调用是纯等待。任何条件缺失/形状不符 → None 走原路径。
    fail-closed 不变：重放自身校验基线 sha 且不符即中止；重放后的
    exact-final 终审（discovery=COMPLETE 硬门、绑交付 SRT sha）与边界评审
    照常运行。
    """

    baseline = _exact_replay_baseline(spec)
    if baseline is None:
        return None
    authority = spec.get("recovery_publication_authority")
    if not (
        isinstance(authority, Mapping)
        and authority.get("title_mode") == "verified_public_exact"
        and str(authority.get("authority_sha256") or "").strip()
    ):
        return None
    return {
        "baseline_schema_version": EXACT_REPLAY_BASELINE_SCHEMA,
        "baseline_sha256": str(baseline.get("sha256") or ""),
        "exact_interval_replay": True,
        "title_mode": "verified_public_exact",
        "publication_authority_sha256": str(
            authority.get("authority_sha256") or ""
        ),
    }


def _valid_ownership_pin(
    pin: object, *, baseline_sha256: str
) -> Mapping[str, object] | None:
    if (
        not isinstance(pin, Mapping)
        or pin.get("schema_version") != TRUTH_FULL_OWNERSHIP_PIN_SCHEMA
        or not str(pin.get("authority") or "").strip()
    ):
        return None
    pinned_baseline = _clean_sha256(pin.get("baseline_sha256"))
    if not pinned_baseline or pinned_baseline != baseline_sha256:
        return None
    truth_input = pin.get("truth_input")
    if (
        not isinstance(truth_input, Mapping)
        or not str(truth_input.get("path") or "").strip()
        or not _clean_sha256(truth_input.get("sha256"))
    ):
        return None
    cue_count = pin.get("cue_count")
    reviewed = pin.get("reviewed_override_count")
    machine = pin.get("machine_cues")
    if (
        isinstance(cue_count, bool)
        or not isinstance(cue_count, int)
        or cue_count <= 0
        or isinstance(reviewed, bool)
        or not isinstance(reviewed, int)
        or reviewed < 0
        or not isinstance(machine, list)
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= cue_count
            for value in machine
        )
        or len(set(machine)) != len(machine)
        or reviewed + len(machine) != cue_count
    ):
        return None
    arbitration_receipt = _clean_sha256(pin.get("arbitration_receipt_sha256"))
    arbitration_disposition = pin.get("arbitration_disposition")
    if machine:
        if not arbitration_receipt or arbitration_disposition is not None:
            return None
    else:
        truth_input_sha = _clean_sha256(truth_input.get("sha256"))
        if not (
            arbitration_receipt
            or (
                isinstance(arbitration_disposition, Mapping)
                and arbitration_disposition.get("schema_version")
                == "reviewed-speaker-arbitration-disposition.v1"
                and arbitration_disposition.get("status")
                == "NOT_REQUIRED_FULLY_LABELLED"
                and _clean_sha256(
                    arbitration_disposition.get("truth_input_sha256")
                )
                == truth_input_sha
                and _clean_sha256(
                    arbitration_disposition.get(
                        "automatic_labelled_srt_sha256"
                    )
                )
            )
        ):
            return None
    return pin


def resolve_truth_full_ownership(
    spec: Mapping[str, object],
) -> dict[str, object] | None:
    """F20：人工真值 100% 拥有交付词面时返回 ``truth_full_ownership.v1`` 回执。

    覆盖证明来自编译器盖的 pin（真值 sha × cue 区间 × 分桶计数）加上重放
    模式本身（exact_interval_replay 把整段区间逐字节换成那份基线）。任何
    一项不成立就返回 ``None``——整条候选走正常全链，不做部分跳过。
    """

    baseline = _exact_replay_baseline(spec)
    if baseline is None:
        return None
    if spec.get("subtitle_text_overrides") is not None:
        # 生产侧本来就拒绝「重放基线 + 文本 override」共存；这里同样拒绝，
        # 免得快路径成为绕开那条硬拦的侧门。
        return None
    baseline_sha256 = _clean_sha256(baseline.get("sha256"))
    if not baseline_sha256:
        return None
    pin = _valid_ownership_pin(
        baseline.get("truth_full_ownership"), baseline_sha256=baseline_sha256
    )
    if pin is None:
        return None
    raw_input = pin["truth_input"]
    truth_input = dict(raw_input) if isinstance(raw_input, Mapping) else {}
    machine_cues = sorted(int(value) for value in list(pin["machine_cues"] or []))
    return {
        "schema_version": TRUTH_FULL_OWNERSHIP_SCHEMA,
        "status": "TRUTH_FULL_OWNERSHIP",
        "coverage": {
            "cue_count": int(str(pin["cue_count"])),
            "reviewed_override_count": int(str(pin["reviewed_override_count"])),
            "machine_speaker_cue_count": len(machine_cues),
            "machine_speaker_cues": machine_cues,
            "text_ownership": "EXACT_INTERVAL_REPLAY_OF_TRUTH_COMPILED_BASELINE",
            "speaker_ownership": (
                "REVIEWED_OVERRIDES_PLUS_POSITIVE_VOICE_ARBITRATED_MACHINE_CUES"
            ),
            "proof": {
                "truth_input": truth_input,
                "baseline_sha256": baseline_sha256,
                "arbitration_receipt_sha256": (
                    _clean_sha256(pin.get("arbitration_receipt_sha256"))
                    or None
                ),
                "arbitration_disposition": (
                    dict(pin["arbitration_disposition"])
                    if isinstance(pin.get("arbitration_disposition"), Mapping)
                    else None
                ),
                "authority": str(pin.get("authority") or ""),
                "source_recording_basename": str(
                    baseline.get("source_recording_basename") or ""
                ),
                "source_sha256": _clean_sha256(baseline.get("source_sha256")),
                "absolute_source_start_ms": baseline.get(
                    "absolute_source_start_ms"
                ),
                "absolute_source_end_ms": baseline.get("absolute_source_end_ms"),
            },
        },
        "skipped_stages": [dict(row) for row in TRUTH_FULL_OWNERSHIP_SKIPPED_STAGES],
        "preserved_stages": list(TRUTH_FULL_OWNERSHIP_PRESERVED_STAGES),
        "effective_conditions": list(TRUTH_FULL_OWNERSHIP_CONDITIONS),
    }


def skipped_final_review_audit(status: str, **ownership: object) -> dict[str, object]:
    """审片员被后置所有权跳过时的 typed audit（两条快路径共用形状）。"""

    return {
        "schema_version": FINAL_REVIEW_AUDIT_SCHEMA,
        "status": status,
        "findings": [],
        "applied_count": 0,
        **ownership,
    }


def verify_transcript_entities(
    srt_text: str,
    *,
    referent_groups: Sequence[Any],
    entity_verifier: Callable | None,
    excluded_cue_indexes: Iterable[int] = (),
    truth_full_ownership: Mapping[str, object] | None = None,
) -> tuple[str, dict[str, Any]]:
    """转写实体声学仲裁；真值全所有权下跳过并披露，不请求声学证人。"""

    if truth_full_ownership is None:
        return apply_audio_entity_verification(
            srt_text,
            referent_groups=referent_groups,
            entity_verifier=entity_verifier,
            excluded_cue_indexes=excluded_cue_indexes,
        )
    srt_sha256 = hashlib.sha256(srt_text.encode("utf-8")).hexdigest()
    return srt_text, {
        # 与 apply_audio_entity_verification 的 audit 同 schema 同键面，只是
        # 多一个 status/reason_code，下游读者（含 ENTITY_VERDICT_REQUIRED 硬拦）
        # 不需要知道这条快路径存在。
        "schema_version": "transcript-entity-audit.v1",
        "status": "SKIPPED_TRUTH_FULL_OWNERSHIP",
        "reason_code": "TRANSCRIPT_ENTITY_SURFACE_OWNED_BY_TRUTH",
        "input_srt_sha256": srt_sha256,
        "output_srt_sha256": srt_sha256,
        "confirmed": [],
        "repairs": [],
        "entity_verdict_required": [],
        "truth_full_ownership": dict(truth_full_ownership),
    }


def discover_priority_findings(
    final_srt_text: str,
    *,
    timeline_offset_ms: int,
    entity_verifier: Callable | None,
    out_root: Any,
    cid: str,
    truth_full_ownership: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """exact-final 的优先 findings 通道；真值全所有权下零发现、零声学请求。

    回执沿用微 cue 的 schema 与 ``PASS`` 语义（和 ``NO_ELIGIBLE_MICROCUES``
    同级：本轮没有可交给终审的候选），另带跳过原因与所有权块。终审自身、
    边界评审与交付前硬门都不受影响。
    """

    if truth_full_ownership is None:
        findings, audit = discover_microcue_findings(
            final_srt_text,
            timeline_offset_ms=timeline_offset_ms,
            entity_verifier=entity_verifier,
        )
        return merge_restatement_priority_findings(
            final_srt_text, findings, audit, out_root=out_root, cid=cid
        )
    return [], {
        "schema_version": MICROCUE_SCHEMA_VERSION,
        "status": "PASS",
        "reason_code": "PRIORITY_DISCOVERY_SKIPPED_TRUTH_FULL_OWNERSHIP",
        "decision_authority": "NONE_DISCOVERY_ONLY",
        "mutation_authorized": False,
        "candidate_text_exposed_to_witness": False,
        "srt_sha256": "sha256:"
        + hashlib.sha256(final_srt_text.encode("utf-8")).hexdigest(),
        "timeline_offset_ms": int(timeline_offset_ms),
        "eligible": [],
        "findings": [],
        "truth_full_ownership": dict(truth_full_ownership),
    }
