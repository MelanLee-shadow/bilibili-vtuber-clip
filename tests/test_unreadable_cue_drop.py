"""「耳朵说这段物理上不可读」→ 删该 cue、出成品、落停泊态、永不上传。

Ivan 2026-08-10 逐字：「遇到这种情况证人报 WITNESS_IMPLAUSIBLE_SYLLABLE_RATE：
11 个音节塞进 0.92s，根本听不出来，应该直接报需要审查，并且在权宜上传时也不能
上传，可以把这段字幕删掉然后出成品等待审阅，而不是拦住。」

形态全部取自 free 真实回执（只读取证 2026-08-10）：
``out/2026-08-09/auto_214238_835_960`` —— 唯一阻断项 cue 33
``就好好休息休息``（65670→66590ms，正好 0.92s），
``verdict.reason_code == WITNESS_IMPLAUSIBLE_SYLLABLE_RATE``、
``detail == "11 syllables over 0.92s target"``。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import unreadable_span_policy as policy
from src.autoslice.exact_final_witness_authority import (
    DOWNGRADE_BRANCH,
    convergence_witness_gate,
    downgrade_convergence_finding,
)


# free 真实窗口与字节（auto_214238_835_960 cue 33）。
_CUE_TEXT = "就好好休息休息"
_CUE_START_MS = 65_670
_CUE_END_MS = 66_590
_PROPOSED = "正好休息会"
_SRT = (
    "1\n00:01:03,910 --> 00:01:05,670\n李姐，要不要摆点躺着的动作\n\n"
    "2\n00:01:05,670 --> 00:01:06,590\n" + _CUE_TEXT + "\n\n"
    "3\n00:01:06,590 --> 00:01:07,590\n躺着吗\n"
)
_UNCERTAIN_BLIND_WITNESS = {
    "schema_version": "subtitle-span-acoustic-witness.v1",
    "witness_protocol": "blind_pinyin",
    "status": "UNCERTAIN",
    "reason_code": "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE",
    "detail": "11 syllables over 0.92s target",
    "request_sha256": (
        "31ee4ad5444acd4e35e4c0f88465636997d65eb61558634a8d13abc8a33b7c06"
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _deadlocked_finding(
    *,
    verdict: dict | None = None,
    cue_index: int = 2,
    window: tuple[int, int] = (_CUE_START_MS, _CUE_END_MS),
    base_text: str = _CUE_TEXT,
    **adjudication_overrides,
) -> dict:
    """用引擎自己的降级函数造样本，别手抄 typed 形状。

    分支名/字段名再改，这里 import 就断或形态自动跟着变，不会像 2026-07-26
    那张白名单快照一样和引擎静默脱节。
    """

    gate = convergence_witness_gate(
        proposed=_PROPOSED,
        window=window,
        findings=[],
        history=[],
    )
    # 前提自检：没有合格盲见证时门必须 BLOCK，降级才会发生。
    assert gate["status"] == "BLOCK"
    row = downgrade_convergence_finding(
        {
            "cue_index": cue_index,
            "repair_class": "phonetic",
            "suspect": base_text,
            "base_text_sha256": _sha(base_text),
            "proposed_full_cue": _PROPOSED,
            "exact_release_adjudication": {
                "schema_version": "subtitle-span-adjudication.v1",
                "timing_immutable": True,
                "verdict": dict(
                    verdict if verdict is not None else _UNCERTAIN_BLIND_WITNESS
                ),
            },
        },
        gate=gate,
    )
    row["exact_release_adjudication"].update(adjudication_overrides)
    return row


def _flagged_audit(findings: list[dict], srt_text: str) -> dict:
    return {
        "schema_version": "final-review-audit.v2",
        "status": "FLAGGED",
        "release_gate": "BLOCK",
        "reason_codes": ["FINAL_REVIEW_UNRESOLVED_FINDINGS"],
        "reviewed_srt_sha256": "sha256:" + _sha(srt_text),
        "findings": findings,
        "validated_finding_count": len(findings),
    }


# ---------------------------------------------------------------------------
# (c) 判据边界：「机器没能决定」那一整类不许被误纳入
# ---------------------------------------------------------------------------


def test_only_the_implausible_rate_code_counts_as_physically_unreadable():
    """物理不可读只有一个原因码；其余每一个都仍然拦死。

    这是本改动最危险的一条边：把任意一个「机器没跑完」的码放进来，
    一次 provider 抽风就会变成「悄悄删一句然后交付」。
    """

    assert policy.PHYSICALLY_UNREADABLE_WITNESS_REASON_CODES == frozenset(
        {"WITNESS_IMPLAUSIBLE_SYLLABLE_RATE"}
    )
    # 两张表互斥，且合起来覆盖引擎真实吐出的全集。
    assert not (
        policy.PHYSICALLY_UNREADABLE_WITNESS_REASON_CODES
        & policy.MACHINE_UNDECIDED_WITNESS_REASON_CODES
    )
    assert (
        policy.unreadable_span_deadlock(_deadlocked_finding()) is not None
    )
    for code in sorted(policy.MACHINE_UNDECIDED_WITNESS_REASON_CODES):
        witness = dict(_UNCERTAIN_BLIND_WITNESS, reason_code=code)
        finding = _deadlocked_finding(verdict=witness)
        assert policy.unreadable_span_deadlock(finding) is None, code
        assert (
            policy.plan_unreadable_cue_drops(
                _SRT, _flagged_audit([finding], _SRT)
            )
            is None
        ), code


def test_reason_code_tables_match_the_engines_real_uncertain_universe():
    """两张表加起来必须覆盖 src 里真实产出的每一个 UNCERTAIN 原因码。

    引擎新开一个原因码而没人分类时，本测试红——避免新码默认落进任何一侧。
    """

    import re

    known = (
        policy.PHYSICALLY_UNREADABLE_WITNESS_REASON_CODES
        | policy.MACHINE_UNDECIDED_WITNESS_REASON_CODES
    )
    root = Path(__file__).resolve().parents[1]
    emitted: set[str] = set()
    verifier = (
        root / "src" / "autoslice" / "entity_audio_verifier.py"
    ).read_text(encoding="utf-8")
    for match in re.finditer(
        r"_uncertain\(\s*(?:request|verifier[^,]*),\s*\"([A-Z0-9_]+)\"",
        verifier,
    ):
        emitted.add(match.group(1))
    # 裁决层与可达性契约里另外两处直写的原因码。
    adjudication = (
        root / "src" / "autoslice" / "acoustic_witness_adjudication.py"
    ).read_text(encoding="utf-8")
    for code in (
        "WITNESS_UNAVAILABLE_KEEP_CURRENT",
        "LEGACY_SIGHTED_WITNESS_NOT_REUSABLE",
        "JUDGE_CALL_FAILED",
        "JUDGE_CHOICE_OUT_OF_SET",
    ):
        assert code in adjudication
        emitted.add(code)
    from src.autoslice.acoustic_witness_availability import (
        AUDIO_VERIFIER_UNAVAILABLE,
    )

    emitted.add(AUDIO_VERIFIER_UNAVAILABLE)
    assert emitted, "未能从引擎源码里抓到任何原因码——正则失配即视为失败"
    assert emitted <= known, sorted(emitted - known)


# ---------------------------------------------------------------------------
# (a)(d)(e) 删除粒度、时间轴、回执
# ---------------------------------------------------------------------------


def test_unreadable_cue_is_dropped_without_moving_any_timestamp():
    """删整条 cue 的文字，紧凑重编号；幸存 cue 的时间戳一字不改。"""

    finding = _deadlocked_finding()
    text, drops = policy.apply_unreadable_cue_drops(
        _SRT, _flagged_audit([finding], _SRT)
    )
    assert len(drops) == 1
    assert _CUE_TEXT not in text
    assert text == (
        "1\n00:01:03,910 --> 00:01:05,670\n李姐，要不要摆点躺着的动作\n\n"
        "2\n00:01:06,590 --> 00:01:07,590\n躺着吗\n"
    )
    # 逐 cue 核对：媒体一帧没剪，所以幸存 cue 的绝对时间必须原样保留。
    from src.autoslice.jingting_chunker import parse_srt_cues

    before = parse_srt_cues(_SRT)
    after = parse_srt_cues(text)
    survivors = [cue for cue in before if cue.text != _CUE_TEXT]
    assert [(cue.start_ms, cue.end_ms, cue.text) for cue in after] == [
        (cue.start_ms, cue.end_ms, cue.text) for cue in survivors
    ]
    # 索引连续（留空块会被 subtitle_validation 以 SRT_CUE_INDEX_NON_CONSECUTIVE 拒收）。
    assert [cue.index for cue in after] == ["1", "2"]


def test_drop_receipt_carries_the_deleted_range_and_the_witness_basis():
    """删字幕是有损操作：回执必须让 Ivan 一眼看到删了哪一段、凭什么删。"""

    _text, drops = policy.apply_unreadable_cue_drops(
        _SRT, _flagged_audit([_deadlocked_finding()], _SRT)
    )
    drop = drops[0]
    assert policy.valid_unreadable_cue_drop(drop) is True
    assert drop["cue_index"] == 2
    assert drop["matched_start_ms"] == _CUE_START_MS
    assert drop["matched_end_ms"] == _CUE_END_MS
    assert drop["span_duration_ms"] == 920
    assert drop["deleted_text"] == _CUE_TEXT
    assert drop["deleted_text_sha256"] == "sha256:" + _sha(_CUE_TEXT)
    assert drop["witness_reason_code"] == "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE"
    assert drop["witness_detail"] == "11 syllables over 0.92s target"
    assert drop["acoustic_witness"]["status"] == "UNCERTAIN"
    # 死锁出处直接绑引擎常量：引擎再改分支名时这里 import 就断，不会静默失配
    # （2026-07-26 那张 decided-keep 白名单快照脱节的同源教训）。
    assert drop["deadlock_policy_branch"] == DOWNGRADE_BRANCH
    assert drop["upload_authorized"] is False
    assert drop["review_authority"] == "HUMAN_OPERATOR"
    assert drop["timing_immutable"] is True
    # 判据原文缺席就不算合格回执。
    assert (
        policy.valid_unreadable_cue_drop({**drop, "witness_detail": ""})
        is False
    )
    assert (
        policy.valid_unreadable_cue_drop({**drop, "deleted_text": ""}) is False
    )
    assert (
        policy.valid_unreadable_cue_drop(
            {**drop, "upload_authorized": True}
        )
        is False
    )


# ---------------------------------------------------------------------------
# 负向金丝雀：typed 外壳每一项都吃重
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"policy_branch": "JUDGE_UNCERTAIN_KEEP_CURRENT"},
        {"status": "OBSERVED"},
        {"repaired": True},
        {"reason_code": "SOMETHING_ELSE"},
        {"decision_authority": "CPA_JUDGE"},
        {"witness_authority": "NONE"},
        {"timing_immutable": False},
        {
            "mutation_authority": {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED",
            }
        },
        {"history_convergence_acoustic_witness": {"status": "PASS"}},
    ],
)
def test_each_typed_conjunct_is_load_bearing(override):
    """伪造的降级壳子一律不许借本路逃逸。"""

    finding = _deadlocked_finding(**override)
    assert policy.unreadable_span_deadlock(finding) is None
    assert (
        policy.plan_unreadable_cue_drops(
            _SRT, _flagged_audit([finding], _SRT)
        )
        is None
    )


@pytest.mark.parametrize(
    "witness_override",
    [
        {"witness_protocol": "legacy_sighted"},
        {"schema_version": "something-else.v1"},
        {"status": "OBSERVED"},
    ],
)
def test_witness_shell_conjuncts_are_load_bearing(witness_override):
    finding = _deadlocked_finding(
        verdict={**_UNCERTAIN_BLIND_WITNESS, **witness_override}
    )
    assert policy.unreadable_span_deadlock(finding) is None


def test_drop_must_land_on_the_exact_cue_bytes_and_window():
    """cue 文本 sha 或时间窗对不上就不许删——防止删错一句。"""

    shifted = _deadlocked_finding(window=(_CUE_START_MS + 1, _CUE_END_MS))
    assert (
        policy.plan_unreadable_cue_drops(
            _SRT, _flagged_audit([shifted], _SRT)
        )
        is None
    )
    wrong_bytes = _deadlocked_finding(base_text="别的句子")
    assert (
        policy.plan_unreadable_cue_drops(
            _SRT, _flagged_audit([wrong_bytes], _SRT)
        )
        is None
    )
    wrong_index = _deadlocked_finding(cue_index=1)
    assert (
        policy.plan_unreadable_cue_drops(
            _SRT, _flagged_audit([wrong_index], _SRT)
        )
        is None
    )


def test_guard_refuses_the_closing_cue_and_therefore_never_empties_the_srt():
    """收束句不删（终点绑定挂着它）；同一条守卫顺带保证字幕不会被删空。"""

    closing_srt = (
        "1\n00:01:03,910 --> 00:01:05,670\n开场\n\n"
        "2\n00:01:05,670 --> 00:01:06,590\n" + _CUE_TEXT + "\n"
    )
    closing = _deadlocked_finding()
    assert (
        policy.plan_unreadable_cue_drops(
            closing_srt, _flagged_audit([closing], closing_srt)
        )
        is None
    )
    only_srt = "1\n00:01:05,670 --> 00:01:06,590\n" + _CUE_TEXT + "\n"
    only = _deadlocked_finding(cue_index=1)
    assert (
        policy.plan_unreadable_cue_drops(
            only_srt, _flagged_audit([only], only_srt)
        )
        is None
    )
    # 被接受的计划一定留下最后一条 cue —— 成品永远有字幕可看。
    text, drops = policy.apply_unreadable_cue_drops(
        _SRT, _flagged_audit([_deadlocked_finding()], _SRT)
    )
    assert drops and text.strip()
    assert "躺着吗" in text


def test_cpa_self_heal_wins_the_pass_so_deleting_stays_the_last_resort():
    """判官这一轮还改得动任何一条，就不许走有损删除。"""

    from src.autoslice.unreadable_cue_drop_stage import (
        stage_unreadable_cue_drop_pass,
    )

    def stage(**overrides):
        kwargs = dict(
            reason_code="FINAL_REVIEW_UNRESOLVED_FINDINGS",
            cpa_repairs=[],
            pass_budget_left=True,
            final_text=_SRT,
            audit=_flagged_audit([_deadlocked_finding()], _SRT),
            expected_srt_sha256="sha256:" + _sha(_SRT),
            passes=[],
            recut=None,
            chat_authority_audit={},
            chat_authority_path=Path("/nonexistent/chat.json"),
            review_audit_path=Path("/nonexistent/review.json"),
            snapshots=({}, {}, None),
        )
        kwargs.update(overrides)
        return stage_unreadable_cue_drop_pass(**kwargs)

    # CPA 还有可落盘的修复 → 本路必须让路（返回 None，连计划都不算）。
    assert stage(cpa_repairs=[{"action": "REPLACE_CUE_TEXT"}]) is None
    # 阻断码不是「未解决 findings」→ 不适用。
    assert stage(reason_code="FINAL_REVIEW_CARRYOVER_UNCONSUMED") is None
    # pass 预算用尽 → 不适用（不许在最后一轮偷偷改字节）。
    assert stage(pass_budget_left=False) is None
    # 已经删满上限 → 不适用。
    assert stage(passes=[{}] * policy.UNREADABLE_CUE_DROP_MAX_PASSES) is None
    # 反证：以上四条都放开时它确实会走到落盘（recut=None 让它在落盘处炸）。
    with pytest.raises(AttributeError):
        stage()


def test_any_other_blocking_finding_keeps_todays_block():
    """混进一条别的阻断项：整条候选照旧拦死，不做有损删除。"""

    other = {
        "cue_index": 1,
        "exact_release_adjudication": {
            "schema_version": "subtitle-span-adjudication.v1",
            "status": "UNCERTAIN",
            "policy_branch": "JUDGE_UNCERTAIN_KEEP_CURRENT",
            "repaired": False,
            "timing_immutable": True,
            "mutation_authority": {"status": "NOT_APPLIED"},
        },
    }
    audit = _flagged_audit([_deadlocked_finding(), other], _SRT)
    assert policy.plan_unreadable_cue_drops(_SRT, audit) is None
    assert policy.apply_unreadable_cue_drops(_SRT, audit) == (_SRT, [])


def test_disclosure_exit_still_refuses_this_finding():
    """本改动没有放宽披露出口：这条 finding 仍然不是「可披露的已决保留」。"""

    from src.autoslice.final_review_contract import is_keep_current_disclosed

    assert is_keep_current_disclosed(_deadlocked_finding()) is False


# ---------------------------------------------------------------------------
# 删除审计合同
# ---------------------------------------------------------------------------


def _drop_audit(srt_text: str, findings: list[dict]) -> tuple[str, dict]:
    text, drops = policy.apply_unreadable_cue_drops(
        srt_text, _flagged_audit(findings, srt_text)
    )
    pass_receipt = policy.build_unreadable_cue_drop_pass(
        pass_index=1,
        input_srt_sha256="sha256:" + _sha(srt_text),
        output_srt_sha256="sha256:" + _sha(text),
        drops=drops,
    )
    return text, policy.build_unreadable_cue_drop_audit(
        passes=[pass_receipt],
        status="PASS",
        final_srt_sha256="sha256:" + _sha(text),
    )


def test_drop_audit_contract_binds_the_hash_chain_and_every_receipt():
    text, audit = _drop_audit(_SRT, [_deadlocked_finding()])
    expected = "sha256:" + _sha(text)
    assert (
        policy.unreadable_cue_drop_audit_problem(
            audit, expected_srt_sha256=expected
        )
        is None
    )
    # 缺省 = 本路没跑过，既有交付一个字节不受影响。
    assert (
        policy.unreadable_cue_drop_audit_problem(
            None, expected_srt_sha256=expected
        )
        is None
    )
    assert (
        policy.unreadable_cue_drop_audit_problem(
            audit, expected_srt_sha256="sha256:" + "0" * 64
        )
        == "UNREADABLE_CUE_DROP_FINAL_HASH_MISMATCH"
    )
    broken_chain = json.loads(json.dumps(audit))
    broken_chain["passes"][0]["output_srt_sha256"] = "sha256:" + "1" * 64
    assert (
        policy.unreadable_cue_drop_audit_problem(
            broken_chain, expected_srt_sha256=expected
        )
        == "UNREADABLE_CUE_DROP_FINAL_HASH_MISMATCH"
    )
    forged = json.loads(json.dumps(audit))
    forged["passes"][0]["drops"][0]["witness_reason_code"] = "JUDGE_CALL_FAILED"
    assert (
        policy.unreadable_cue_drop_audit_problem(
            forged, expected_srt_sha256=expected
        )
        == "UNREADABLE_CUE_DROP_RECEIPT_INVALID"
    )
    not_authorized = json.loads(json.dumps(audit))
    not_authorized["upload_authorized"] = True
    assert (
        policy.unreadable_cue_drop_audit_problem(
            not_authorized, expected_srt_sha256=expected
        )
        == "UNREADABLE_CUE_DROP_AUDIT_INVALID"
    )


def test_release_contract_rejects_a_forged_drop_audit():
    """终审合同把删除审计当一等公民校验，不是随手挂个字段就放行。"""

    from src.autoslice.final_review_contract import (
        FinalReviewContractError,
        validate_final_review_release,
    )

    text, drop_audit = _drop_audit(_SRT, [_deadlocked_finding()])
    expected = "sha256:" + _sha(text)
    audit = _clean_audit(text)
    audit["unreadable_cue_drops"] = drop_audit
    assert validate_final_review_release(
        audit, expected_srt_sha256=expected
    )["status"] == "CLEAN"
    forged = json.loads(json.dumps(audit))
    forged["unreadable_cue_drops"]["passes"][0]["drops"][0][
        "authority"
    ] = "SELF_GRANTED"
    with pytest.raises(
        FinalReviewContractError, match="UNREADABLE_CUE_DROP_RECEIPT_INVALID"
    ):
        validate_final_review_release(forged, expected_srt_sha256=expected)


def _clean_audit(text: str) -> dict:
    return {
        "schema_version": "final-review-audit.v2",
        "status": "CLEAN",
        "release_gate": "PASS",
        "reason_codes": [],
        "reviewed_srt_sha256": "sha256:" + _sha(text),
        "discovery": {"status": "COMPLETE", "explicit_empty_findings": True},
        "correction_mutation_authority": {
            "schema_version": "subtitle-correction-mutation-audit.v1",
            "status": "PASS",
        },
        "findings": [],
        "validated_finding_count": 0,
        "boundary_semantic_review": {
            "schema_version": "talk-boundary-semantic-review.v1",
            "status": "PASS",
            "review_scope": "final_delivery",
            "reason_codes": [],
            "request_sha256": "sha256:" + "d" * 64,
            "cue_grid_sha256": "sha256:" + "e" * 64,
            "source_separation_witness": {
                "schema_version": (
                    "talk-boundary-source-separation-witness.v1"
                ),
                "status": "PASS",
                "source_review_sha256": "sha256:" + "a" * 64,
                "source_request_sha256": "sha256:" + "b" * 64,
                "source_cue_grid_sha256": "sha256:" + "c" * 64,
                "source_final_start_ms": 0,
                "source_final_end_ms": 90_000,
                "reason_codes": [],
            },
            "final_endpoint_binding": {
                "schema_version": "talk-boundary-final-endpoint-binding.v1",
                "status": "PASS",
                "recommended_end_cue_index": 2,
                "recommended_end_ms": 66_590,
                "final_closure_cue_index": 2,
                "final_snapped_end_ms": 66_590,
                "semantic_cue_grid_sha256": "sha256:" + "e" * 64,
                "final_cue_grid_sha256": "sha256:" + "e" * 64,
                "reason_codes": [],
            },
        },
    }


# ---------------------------------------------------------------------------
# (a) 全链路：终审循环里删掉 → 出成品 → 落旁车回执
# ---------------------------------------------------------------------------


def _gate_adapters(reviewer):
    from src.autoslice import producer_package_finalization as finalization

    def unused(*_args, **_kwargs):
        raise AssertionError("unrelated adapter called")

    return finalization.ProducerFinalizationAdapters(
        accurate_recut_command=unused,
        run_command=unused,
        write_source_range_srt=unused,
        apply_text_override_document=unused,
        run_speaker_finalization=unused,
        burn_preview_subtitles=unused,
        stage_publish_draft=unused,
        generate_upload_tags=unused,
        delivery_root=unused,
        run_exact_final_review=reviewer,
    )


def test_review_gate_drops_the_cue_ships_the_package_and_writes_the_sidecar(
    tmp_path: Path,
) -> None:
    """今天判死的那条候选：删掉一句、终审转 PASS、旁车回执落盘。"""

    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice import unreadable_cue_review

    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(_SRT, encoding="utf-8")
    chat_path = tmp_path / "candidate.chat-authority.json"
    calls: list[str] = []

    def reviewer(text, _authority, _offset, _end):
        calls.append(text)
        if _CUE_TEXT in text:
            audit = _clean_audit(text)
            audit.update(_flagged_audit([_deadlocked_finding()], text))
            return audit
        return _clean_audit(text)

    chat: dict = {}
    audit = finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=tmp_path,
        final_start=0,
        final_end=90_000,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=tmp_path,
            media_path=tmp_path / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
        ),
        chat_authority_audit=chat,
        chat_authority_path=chat_path,
        adapters=_gate_adapters(reviewer),
    )

    assert len(calls) == 2, "必须重审一遍删后的字节，不能自称干净"
    assert audit["status"] == "CLEAN"
    final_text = subtitle.read_text(encoding="utf-8")
    assert _CUE_TEXT not in final_text
    # 时间轴仍与音频对齐：幸存 cue 的时间戳原样在。
    assert "00:01:03,910 --> 00:01:05,670" in final_text
    assert "00:01:06,590 --> 00:01:07,590" in final_text
    drop_audit = audit["unreadable_cue_drops"]
    assert drop_audit["status"] == "PASS"
    assert drop_audit["final_srt_sha256"] == "sha256:" + _sha(final_text)
    assert chat["unreadable_cue_drops"] == drop_audit
    sidecar = unreadable_cue_review.drop_sidecar_path(tmp_path, "candidate")
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == drop_audit


def test_producer_sidecar_is_exactly_what_the_runner_globs(
    tmp_path: Path,
) -> None:
    """把 produce 落的旁车和 runner 的 glob 焊在一起，用生产目录布局。

    这两半分开测都会绿，合起来才拦得住本仓最典型的那类静默 fail-open：旁车路径
    与 glob 根一旦漂开，runner 读不到删除记录 → 状态照常 review_ready → 一个
    少了一句话的成品直接进日审清单、变成可上传。正是 Ivan 裁定里「在权宜上传时
    也不能上传」要挡的那条路，所以这条走完整链路：
    ``out/<cid>/replacement_recuts/<cid>.recut.srt`` → 终审门 → 停泊态。
    """

    from src.autoslice import producer_package_finalization as finalization
    from src.autoslice import speaker_guess, unreadable_cue_review

    out_root = tmp_path / "out"
    work_dir = out_root / "candidate"
    recut_dir = work_dir / "replacement_recuts"
    recut_dir.mkdir(parents=True)
    subtitle = recut_dir / "candidate.recut.srt"
    subtitle.write_text(_SRT, encoding="utf-8")
    delivery = tmp_path / "delivery" / "candidate.mp4"
    delivery.parent.mkdir()
    delivery.write_bytes(b"burned media")

    def reviewer(text, _authority, _offset, _end):
        audit = _clean_audit(text)
        if _CUE_TEXT in text:
            audit.update(_flagged_audit([_deadlocked_finding()], text))
        return audit

    finalization._run_exact_final_review_gate(
        cid="candidate",
        out_root=out_root,
        final_start=0,
        final_end=90_000,
        recut=finalization.FinalRecutArtifacts(
            recut_dir=recut_dir,
            media_path=recut_dir / "candidate.recut.mp4",
            subtitle_path=subtitle,
            text_manifest_path=None,
            text_manifest=None,
        ),
        chat_authority_audit={},
        chat_authority_path=out_root / "candidate.chat-authority.json",
        adapters=_gate_adapters(reviewer),
    )

    # runner 侧只拿 work_dir 去 glob——没有任何路径是测试手工拼出来的。
    record: dict = {
        "candidate_id": "candidate",
        "summary": {"delivery": str(delivery), "subtitle": str(subtitle)},
    }
    status = speaker_guess.finalize_delivered_talk_status(
        record,
        candidate_id="candidate",
        work_dir=work_dir,
        cover_ready=True,
    )
    assert status == unreadable_cue_review.UNREADABLE_CUE_REVIEW_STATUS
    receipt = record["unreadable_cue_review"]
    assert receipt["dropped_cue_count"] == 1
    assert receipt["dropped_cues"][0]["deleted_text"] == _CUE_TEXT
    assert receipt["dropped_cues"][0]["receipt_path"] == str(
        recut_dir / "candidate.unreadable-cue-drops.json"
    )
    assert receipt["review_artifacts"]["burned_video"] == str(delivery)
    # 成品真的在，Ivan 打得开；字幕已经少了那一句。
    assert delivery.is_file()
    assert _CUE_TEXT not in subtitle.read_text(encoding="utf-8")


def test_review_gate_still_blocks_a_machine_undecided_witness(
    tmp_path: Path,
) -> None:
    """(c) 「机器没能决定」那一类不进本路：照旧 FINAL_REVIEW_RELEASE_BLOCKED。"""

    from src.autoslice import producer_package_finalization as finalization

    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(_SRT, encoding="utf-8")
    finding = _deadlocked_finding(
        verdict=dict(
            _UNCERTAIN_BLIND_WITNESS,
            reason_code="WITNESS_REPORT_INVALID",
        )
    )

    def reviewer(text, _authority, _offset, _end):
        audit = _clean_audit(text)
        audit.update(_flagged_audit([finding], text))
        return audit

    with pytest.raises(SystemExit, match="FINAL_REVIEW_UNRESOLVED_FINDINGS"):
        finalization._run_exact_final_review_gate(
            cid="candidate",
            out_root=tmp_path,
            final_start=0,
            final_end=90_000,
            recut=finalization.FinalRecutArtifacts(
                recut_dir=tmp_path,
                media_path=tmp_path / "candidate.recut.mp4",
                subtitle_path=subtitle,
                text_manifest_path=None,
                text_manifest=None,
            ),
            chat_authority_audit={},
            chat_authority_path=tmp_path / "candidate.chat-authority.json",
            adapters=_gate_adapters(reviewer),
        )
    # 一个字节都没动。
    assert subtitle.read_text(encoding="utf-8") == _SRT
    assert not (
        tmp_path / "candidate.unreadable-cue-drops.json"
    ).exists()


def test_carryover_for_the_dropped_cue_does_not_resurrect_the_block(
    tmp_path: Path,
) -> None:
    """删掉之后，旧结转行按文本哈希找不到落点，不会二次拦死。"""

    from src.autoslice import producer_package_finalization as finalization

    replayable = finalization._replayable_exact_final_carryover_findings
    subtitle = tmp_path / "candidate.recut.srt"
    subtitle.write_text(_SRT, encoding="utf-8")
    carryover = tmp_path / "candidate.final-review-carryover.json"
    row = _deadlocked_finding()
    row["exact_release_adjudication"]["request"] = {
        "base_text_sha256": _sha(_CUE_TEXT),
        "current_cue": _CUE_TEXT,
        "proposed_cue": _PROPOSED,
        "matched_start_ms": _CUE_START_MS,
        "matched_end_ms": _CUE_END_MS,
    }
    carryover.write_text(
        json.dumps(
            {
                "schema_version": "final-review-carryover.v1",
                "findings": [row],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # 删之前：这行确实可重放（前提自检，否则本测试什么都没证明）。
    assert replayable(_SRT, carryover)
    dropped, _drops = policy.apply_unreadable_cue_drops(
        _SRT, _flagged_audit([_deadlocked_finding()], _SRT)
    )
    assert replayable(dropped, carryover) == []


# ---------------------------------------------------------------------------
# (b) 停泊态 + 每一条上传路径
# ---------------------------------------------------------------------------


def _parked_record(tmp_path: Path) -> dict:
    from src.autoslice import speaker_guess, unreadable_cue_review

    recuts = tmp_path / "replacement_recuts"
    recuts.mkdir()
    _text, drop_audit = _drop_audit(_SRT, [_deadlocked_finding()])
    unreadable_cue_review.drop_sidecar_path(recuts, "candidate").write_text(
        json.dumps(drop_audit, ensure_ascii=False), encoding="utf-8"
    )
    record: dict = {
        "candidate_id": "candidate",
        "hook": "跳完串烧累到大脑停转",
        "summary": {
            "delivery": str(tmp_path / "candidate.mp4"),
            "subtitle": str(tmp_path / "candidate.srt"),
        },
    }
    status = speaker_guess.finalize_delivered_talk_status(
        record,
        candidate_id="candidate",
        work_dir=tmp_path,
        cover_ready=True,
    )
    return {"status": status, "record": record}


def test_dropped_delivery_parks_for_human_review_instead_of_review_ready(
    tmp_path: Path,
) -> None:
    from src.autoslice import unreadable_cue_review

    parked = _parked_record(tmp_path)
    record = parked["record"]
    assert parked["status"] == (
        unreadable_cue_review.UNREADABLE_CUE_REVIEW_STATUS
    )
    assert record["status"] == parked["status"]
    receipt = record["unreadable_cue_review"]
    assert receipt["status"] == "PENDING_HUMAN_REVIEW"
    assert receipt["upload_authorized"] is False
    assert receipt["wakes_on"] == "HUMAN_OPERATOR_ONLY"
    assert receipt["dropped_cue_count"] == 1
    assert receipt["dropped_cues"][0]["deleted_text"] == _CUE_TEXT
    assert (
        receipt["dropped_cues"][0]["witness_detail"]
        == "11 syllables over 0.92s target"
    )
    # 成品路径写进 state：Ivan 打得开，free 容量清理也不会当孤儿删掉。
    assert receipt["review_artifacts"]["burned_video"].endswith(
        "candidate.mp4"
    )
    assert record["failure_recoverable"] is False
    assert unreadable_cue_review.is_unreadable_cue_review_hold(record)
    # 报表里有专章，Ivan 一眼看到少了哪一句、为什么。
    section = "\n".join(
        unreadable_cue_review.render_report_section([record])
    )
    assert _CUE_TEXT in section
    assert "11 syllables over 0.92s target" in section
    assert "不可上传" in section


def test_unverifiable_drop_receipt_still_parks(tmp_path: Path) -> None:
    """检测失败必须 fail-closed：回执坏了也照停，不能悄悄变 review_ready。"""

    from src.autoslice import speaker_guess, unreadable_cue_review

    recuts = tmp_path / "replacement_recuts"
    recuts.mkdir()
    unreadable_cue_review.drop_sidecar_path(
        recuts, "candidate"
    ).write_text("{not json", encoding="utf-8")
    record: dict = {"candidate_id": "candidate", "summary": {}}
    status = speaker_guess.finalize_delivered_talk_status(
        record,
        candidate_id="candidate",
        work_dir=tmp_path,
        cover_ready=True,
    )
    assert status == unreadable_cue_review.UNREADABLE_CUE_REVIEW_STATUS
    assert (
        record["unreadable_cue_review"]["dropped_cues"][0]["status"]
        == "RECEIPT_UNPARSEABLE"
    )


def test_parked_status_is_blocked_on_every_automated_upload_path(
    tmp_path: Path,
) -> None:
    """(b) 逐条封堵：状态门 → 日审清单 → 上传授权，一条都过不去。"""

    import scripts.free_session_autoslice as runner
    from src.autoslice import unreadable_cue_review
    from src.autoslice.batch_terminal_state import project_terminal_batch_state
    from src.autoslice.delivery_recovery import TALK_RECOVERY_FAILURE_STATUSES

    status = unreadable_cue_review.UNREADABLE_CUE_REVIEW_STATUS
    root = Path(__file__).resolve().parents[1]

    def source(*parts: str) -> str:
        return (root.joinpath(*parts)).read_text(encoding="utf-8")

    # 1. 状态门：不在已交付集合里 —— 这是全链路 fail-closed 的唯一承载点。
    assert status not in runner.DELIVERED_TALK_STATUSES
    assert status != runner.TALK_COVER_PENDING_STATUS
    # 重试车道也不收：这段音频的不可读是确定性的，重产一万次结论一样。
    assert status not in TALK_RECOVERY_FAILURE_STATUSES
    # 2. 日审清单只收 review_ready；没有清单条目 = v3 包认证不过 = upload() 进不去。
    assert (
        'pick.get("status") != "review_ready"'
        in source("scripts", "build_lidousha_daily_review_manifest.py")
    )
    # 3. 封面车道每一处「提成 review_ready」都被 cover_pending 守着，所以
    #    停泊件不会被封面绑定悄悄升进日审清单（说话人停泊同款风险面）。
    for module in ("cover_maintenance.py", "cover_repair.py"):
        lines = source("src", "autoslice", module).splitlines()
        promotions = [
            index
            for index, line in enumerate(lines)
            if '"review_ready"' in line and "status" in line
        ]
        assert promotions, module
        for index in promotions:
            window = "\n".join(lines[max(0, index - 4) : index + 1])
            assert "TALK_COVER_PENDING_STATUS" in window, (module, index)
    # 4. 包重绑 / 冻结恢复 / 复活器都按 review_ready 白名单拒收。
    assert 'status != "review_ready"' in source(
        "src", "autoslice", "package_import.py"
    )
    resume_src = source("scripts", "resume_frozen_talk_package.py")
    assert 'record_state.get("status") not in {' in resume_src
    assert status not in resume_src
    assert status not in source("scripts", "revive_rejected_candidates.py")
    # 5. 「权宜/快速通道」delivery_fast_path 不读任何状态、不产生上传授权。
    fast_path_src = source("src", "autoslice", "delivery_fast_path.py")
    assert "DELIVERED_TALK_STATUSES" not in fast_path_src
    assert "publication_registry" not in fast_path_src
    # 6. 运行时绝不代写出版登记（7667d9a 血泪：写错 runtime 登记会以
    #    PUBLICATION_RUNTIME_REGISTRY_INVALID 把**所有**上传一起拦掉）。
    for module in ("unreadable_cue_review.py", "unreadable_span_policy.py"):
        text = source("src", "autoslice", module)
        code = "\n".join(
            line
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ).split('"""')
        # 只看代码体（去掉模块 docstring），docstring 里引用登记模块名是允许的。
        body = "".join(code[2:]) if len(code) > 2 else text
        assert "publication_registry" not in body
        assert "runtime.v1.json" not in body
    # 7. 停泊件既不冒充已交付、也不会让整批卡在非终态。
    state = {
        "picks": [{"candidate_id": "c", "status": status}],
        "songs": [],
    }
    projection = project_terminal_batch_state(
        state,
        delivered_talk_statuses=runner.DELIVERED_TALK_STATUSES,
        talk_failure_statuses=TALK_RECOVERY_FAILURE_STATUSES,
        cover_pending_status=runner.TALK_COVER_PENDING_STATUS,
        exact_closure={"status": "NOT_APPLICABLE"},
        retry_epoch=None,
    )
    assert projection["delivered_talk"] == []
    assert state["status"] == "no_delivery"
