"""证人判定「这一句音频物理上不可读」时：删掉那条字幕、照常出成品、等人审。

Ivan 2026-08-10 逐字裁定：

    「遇到这种情况证人报 ``WITNESS_IMPLAUSIBLE_SYLLABLE_RATE``：11 个音节塞进
    0.92s，根本听不出来，应该直接报需要审查，并且在权宜上传时也不能上传，
    可以把这段字幕删掉然后出成品等待审阅，而不是拦住。」

被改的是哪条路：``exact_final_witness_authority`` 在「CPA 收敛判官选了 PROPOSED
但代码级见证门 BLOCK」时把 finding 降级成
``HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY``（审片员明说「别改、只披露」），
同时把 adjudication 的 ``status`` 写成 ``UNCERTAIN``。上一版
``decided_history_convergence_disclosure`` 已经把这一支按耳朵状态劈开：
``verdict.status == "OBSERVED"``（耳朵真给了读音）可随包披露发出去，其余仍拦死。
本模块处置的正是**被拦死的那一半里的一个真子集**——耳朵不但没给读音，还明确
报了「这段音频物理上装不下这么多音节」。那既不是「机器没跑」也不是「内容有问
题」，是**这一句本身不可读**；把整条候选拦死是错的处置。

判据边界（为什么只收一个 reason code，别顺手加）
------------------------------------------------
``entity_audio_verifier`` 的证人链一共吐这些 UNCERTAIN 原因码，逐个判：

*物理不可读*（本模块处置）
  ``WITNESS_IMPLAUSIBLE_SYLLABLE_RATE``
      音节率越过普通话物理上限（引擎自己的注释：峰值约 9 音节/秒）。无论把它
      读成「这窗里真塞了这么多音节」还是「听写溢出了目标窗」，结论都一样：
      **这 0.92s 的窗口拿不到可信转写**，重试一万次也定不了案。

*机器没能决定*（一律留在原地继续拦死，不许进本路）
  ``WITNESS_REPORT_INVALID``            报告形状非法 —— 模型没按协议答
  ``WITNESS_PROMPT_COPY_DETECTED``      模型抄了示范句 —— 根本没听
  ``WITNESS_REQUEST_PROTOCOL_INVALID``  请求侧接线错
  ``WITNESS_REQUEST_CARRIES_CANDIDATES``盲听协议被污染
  ``AUDIO_VERIFIER_UNAVAILABLE``        本轮压根没有 provider 听过（F21 专用码）
  ``ENTITY_AUDIO_PROVIDER_FAILED``      provider 调用失败
  ``ENTITY_AUDIO_CROP_FAILED``          音频切不出来
  ``ENTITY_AUDIO_REQUEST_INVALID`` / ``_CANDIDATES_INVALID`` /
  ``_TIMELINE_OFFSET_INVALID`` / ``_REQUEST_HASH_MISMATCH`` / ``_SPAN_INVALID``
                                        请求/绑定不合法
  ``ENTITY_AUDIO_UNCERTAIN``            模型答了但答不出结论
裁决层同类（``acoustic_witness_adjudication``）：``WITNESS_UNAVAILABLE_KEEP_CURRENT``、
``LEGACY_SIGHTED_WITNESS_NOT_REUSABLE``、``JUDGE_CALL_FAILED``、
``JUDGE_CHOICE_OUT_OF_SET`` —— 全是「机器没跑完/没答完」。把这些放进来就是把
fail-closed 拆了：一次 provider 抽风会变成「悄悄删一句然后交付」。

为什么谓词卡的是整套 typed 形状而不是一个字符串
------------------------------------------------
``reason_code`` 是 finding 里的一个普通字段，谁写谁算。所以本模块的
``unreadable_span_deadlock`` 和 ``decided_history_convergence_disclosure``
同款：降级分支名、门 schema/BLOCK/reason_code、mutation basis、decision/witness
authority、timing_immutable、证人 schema/协议/状态 —— 逐项对齐；再加上删除必须
落在**这条 cue 自己的字节和时间窗**上（cue 文本 sha 与门窗口双向核对）。任何一
处对不上都视为伪造的壳子，按原路继续拦死。

删除粒度与时间轴
----------------
删**整条 cue 的文字**，不动任何时间戳：把 ``text`` 置空后**紧凑重编号**，幸存
cue 的 ``start_ms``/``end_ms`` 一字不改，媒体一帧不剪。这与既有
``acoustic_drop_cue``/``DROP_CUE`` 完全同一套机制（``producer_package_finalization``
里 CPA 自愈那条 DROP 路径），只是授权来源不同 —— 所以烧录时间轴仍与音频对齐，
后续 cue 不会被顺移。留空块不行：``subtitle_validation`` 会以
``SRT_BLOCK_TOO_SHORT`` + ``SRT_CUE_INDEX_NON_CONSECUTIVE`` 拒收。

守卫（fail-closed 侧，不是放宽）
  * 不许删最后一条 cue —— 收束句被终点绑定
    （``talk-boundary-final-endpoint-binding.v1``）挂着，删了只会换一个更难懂的
    阻断码。这条同时蕴含「不会把整条字幕删空」（最后一条必然幸存），所以不另写
    一条全删空守卫：翻不红的守卫是伪装成安全的死代码。这种情况保持今天的行为，
    照旧拦死。
  * **只有当本轮所有阻断项都是这一类死锁时才走本路**。混进任何一条别的阻断项，
    整条候选照旧按今天拦死 —— 那条 finding 本来就该拦，删了字幕也救不回来，
    反而白白有损。
  * 调用侧（``unreadable_cue_drop_stage``）还压着一条次序守卫：CPA 判官这一轮
    只要还改得动任何一条，就先走 CPA 自愈，本路一律不动。删字幕永远是最后手段。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from src.autoslice.acoustic_witness_protocol import BLIND_PINYIN_PROTOCOL
from src.autoslice.exact_final_witness_authority import (
    DOWNGRADE_BRANCH,
    GATE_SCHEMA,
    REQUIRED_REASON,
)
from src.autoslice.jingting_chunker import parse_srt_cues


DROP_SCHEMA = "unreadable-cue-drop.v1"
DROP_PASS_SCHEMA = "unreadable-cue-drop-pass.v1"
DROP_AUDIT_SCHEMA = "unreadable-cue-drop-audit.v1"
DROP_DECISION_AUTHORITY = "UNREADABLE_SPAN_POLICY"
DROP_AUTHORITY = "IVAN_2026-08-10_UNREADABLE_SPAN_DELETE_AND_PARK"
DROP_DISPOSITION = "SUBTITLE_DELETED_PENDING_HUMAN_REVIEW"
WITNESS_SCHEMA = "subtitle-span-acoustic-witness.v1"
MUTATION_SCHEMA = "subtitle-correction-mutation-authority.v1"
# 与 exact-final 自愈同量级；实际上一条候选通常只有一两条不可读窗。
UNREADABLE_CUE_DROP_MAX_PASSES = 5

# 唯一收录项。改这张表等于改 fail-closed 边界，必须带 Ivan 的新裁定。
PHYSICALLY_UNREADABLE_WITNESS_REASON_CODES = frozenset(
    {
        "WITNESS_IMPLAUSIBLE_SYLLABLE_RATE",
    }
)

# 全集的另一半，显式写出来供测试逐条钉死「不许被误纳入」。
MACHINE_UNDECIDED_WITNESS_REASON_CODES = frozenset(
    {
        "WITNESS_REPORT_INVALID",
        "WITNESS_PROMPT_COPY_DETECTED",
        "WITNESS_REQUEST_PROTOCOL_INVALID",
        "WITNESS_REQUEST_CARRIES_CANDIDATES",
        "AUDIO_VERIFIER_UNAVAILABLE",
        "ENTITY_AUDIO_PROVIDER_FAILED",
        "ENTITY_AUDIO_CROP_FAILED",
        "ENTITY_AUDIO_REQUEST_INVALID",
        "ENTITY_AUDIO_CANDIDATES_INVALID",
        "ENTITY_AUDIO_TIMELINE_OFFSET_INVALID",
        "ENTITY_AUDIO_REQUEST_HASH_MISMATCH",
        "ENTITY_AUDIO_SPAN_INVALID",
        "ENTITY_AUDIO_UNCERTAIN",
        "WITNESS_UNAVAILABLE_KEEP_CURRENT",
        "LEGACY_SIGHTED_WITNESS_NOT_REUSABLE",
        "JUDGE_CALL_FAILED",
        "JUDGE_CHOICE_OUT_OF_SET",
    }
)

_SHA256_HEX_RX = re.compile(r"^[0-9a-f]{64}$")
_SHA256_RX = re.compile(r"^sha256:[0-9a-f]{64}$")
_WITNESS_RECEIPT_KEYS = (
    "schema_version",
    "witness_protocol",
    "status",
    "reason_code",
    "detail",
    "request_sha256",
)


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unreadable_span_deadlock(finding: object) -> dict[str, Any] | None:
    """「耳朵说这段物理上听不出来」造成的死锁；不是就返回 None。

    收录条件是整套 typed 形状（见模块 docstring），不是一次字符串匹配。返回值
    只是删除计划所需的坐标，本身不构成授权 —— 授权在
    ``plan_unreadable_cue_drops`` 里与真实 cue 字节核对后才成立。
    """

    if not isinstance(finding, Mapping):
        return None
    adjudication = finding.get("exact_release_adjudication")
    if not isinstance(adjudication, Mapping):
        adjudication = finding.get("context_audio_adjudication")
    if not isinstance(adjudication, Mapping):
        return None
    timing_immutable = (
        adjudication.get("timing_immutable") is True
        or finding.get("timing_immutable") is True
    )
    gate = adjudication.get("history_convergence_acoustic_witness")
    mutation = adjudication.get("mutation_authority")
    witness = adjudication.get("verdict")
    cue_index = finding.get("cue_index")
    if not (
        adjudication.get("policy_branch") == DOWNGRADE_BRANCH
        and adjudication.get("status") == "UNCERTAIN"
        and adjudication.get("repaired") is False
        and adjudication.get("reason_code") == REQUIRED_REASON
        and adjudication.get("decision_authority") == "CPA_PROPOSAL_ONLY"
        and adjudication.get("witness_authority") == "ACOUSTIC_WITNESS_REQUIRED"
        and timing_immutable
        and isinstance(gate, Mapping)
        and gate.get("schema_version") == GATE_SCHEMA
        and gate.get("status") == "BLOCK"
        and gate.get("reason_code") == REQUIRED_REASON
        and isinstance(mutation, Mapping)
        and mutation.get("schema_version") == MUTATION_SCHEMA
        and mutation.get("status") == "NOT_APPLIED"
        and mutation.get("basis") == REQUIRED_REASON
        and isinstance(witness, Mapping)
        and witness.get("schema_version") == WITNESS_SCHEMA
        and witness.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
        and witness.get("status") == "UNCERTAIN"
        and witness.get("reason_code")
        in PHYSICALLY_UNREADABLE_WITNESS_REASON_CODES
        and not isinstance(cue_index, bool)
        and isinstance(cue_index, int)
        and cue_index >= 1
    ):
        return None
    start_ms = gate.get("matched_start_ms")
    end_ms = gate.get("matched_end_ms")
    base_text_sha256 = str(finding.get("base_text_sha256") or "")
    if (
        isinstance(start_ms, bool)
        or not isinstance(start_ms, int)
        or isinstance(end_ms, bool)
        or not isinstance(end_ms, int)
        or not 0 <= start_ms < end_ms
        or _SHA256_HEX_RX.fullmatch(base_text_sha256) is None
    ):
        return None
    return {
        "cue_index": int(cue_index),
        "matched_start_ms": int(start_ms),
        "matched_end_ms": int(end_ms),
        "base_text_sha256": base_text_sha256,
        "witness": dict(witness),
    }


def _witness_receipt(witness: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: witness.get(key)
        for key in _WITNESS_RECEIPT_KEYS
        if key in witness
    }


def plan_unreadable_cue_drops(
    srt_text: str, audit: object
) -> list[dict[str, Any]] | None:
    """全部阻断项都是「物理不可读」死锁时给出删除计划，否则 ``None``。

    ``None`` 的含义是「本路不适用，按今天照旧拦死」，与「计划为空」严格区分。
    """

    if not isinstance(audit, Mapping):
        return None
    findings = audit.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    cues = parse_srt_cues(srt_text)
    if not cues:
        return None
    plans: list[dict[str, Any]] = []
    dropped_indexes: set[int] = set()
    for finding in findings:
        deadlock = unreadable_span_deadlock(finding)
        if deadlock is None:
            # 混进任何一条别的阻断项：整体不走本路（见模块 docstring）。
            return None
        cue_index = int(deadlock["cue_index"])
        if not 1 <= cue_index <= len(cues):
            return None
        cue = cues[cue_index - 1]
        if (
            _sha_text(cue.text) != deadlock["base_text_sha256"]
            or cue.start_ms != deadlock["matched_start_ms"]
            or cue.end_ms != deadlock["matched_end_ms"]
            or not cue.text.strip()
        ):
            return None
        if cue_index in dropped_indexes:
            # 同一条 cue 上的多条 finding 只删一次。
            continue
        dropped_indexes.add(cue_index)
        witness = deadlock["witness"]
        plans.append(
            {
                "schema_version": DROP_SCHEMA,
                "status": "APPLIED",
                "disposition": DROP_DISPOSITION,
                "cue_index": cue_index,
                "matched_start_ms": cue.start_ms,
                "matched_end_ms": cue.end_ms,
                "span_duration_ms": cue.end_ms - cue.start_ms,
                "deleted_text": cue.text,
                "deleted_text_sha256": "sha256:" + _sha_text(cue.text),
                "witness_reason_code": str(witness.get("reason_code") or ""),
                "witness_detail": str(witness.get("detail") or "")[:300],
                "acoustic_witness": _witness_receipt(witness),
                "deadlock_policy_branch": DOWNGRADE_BRANCH,
                "decision_authority": DROP_DECISION_AUTHORITY,
                "authority": DROP_AUTHORITY,
                "timing_immutable": True,
                "upload_authorized": False,
                "review_authority": "HUMAN_OPERATOR",
            }
        )
    if not plans:
        return None
    if len(cues) in dropped_indexes:
        # 收束句归终点绑定管，不在本路的处置范围内。这一条同时**蕴含**「不会把
        # 字幕删空」：最后一条 cue 必然幸存，所以不再单写一条全删空守卫——写了
        # 也永远走不到，负向金丝雀翻不红的守卫就是伪装成安全的死代码。
        return None
    return plans


def apply_unreadable_cue_drops(
    srt_text: str, audit: object
) -> tuple[str, list[dict[str, Any]]]:
    """把不可读 cue 的文字删掉并紧凑重编号；时间戳一字不动。"""

    plans = plan_unreadable_cue_drops(srt_text, audit)
    if not plans:
        return srt_text, []
    from src.autoslice.producer_text_finalization import _render_cues_to_srt

    cues = parse_srt_cues(srt_text)
    dropped = {int(plan["cue_index"]) for plan in plans}
    for cue_index in dropped:
        current = cues[cue_index - 1]
        cues[cue_index - 1] = type(current)(
            index=current.index,
            start_ms=current.start_ms,
            end_ms=current.end_ms,
            text="",
        )
    survivors = [
        type(cue)(
            index=index,
            start_ms=cue.start_ms,
            end_ms=cue.end_ms,
            text=cue.text,
        )
        for index, cue in enumerate(
            (cue for cue in cues if cue.text.strip()), start=1
        )
    ]
    return _render_cues_to_srt(survivors), plans


def valid_unreadable_cue_drop(drop: object) -> bool:
    """一条删除回执自洽且带完整判据（回放/审计侧的唯一认定口径）。"""

    if not isinstance(drop, Mapping):
        return False
    witness = drop.get("acoustic_witness")
    cue_index = drop.get("cue_index")
    start_ms = drop.get("matched_start_ms")
    end_ms = drop.get("matched_end_ms")
    duration_ms = drop.get("span_duration_ms")
    deleted_text = drop.get("deleted_text")
    return bool(
        drop.get("schema_version") == DROP_SCHEMA
        and drop.get("status") == "APPLIED"
        and drop.get("disposition") == DROP_DISPOSITION
        and drop.get("decision_authority") == DROP_DECISION_AUTHORITY
        and drop.get("authority") == DROP_AUTHORITY
        and drop.get("deadlock_policy_branch") == DOWNGRADE_BRANCH
        and drop.get("timing_immutable") is True
        and drop.get("upload_authorized") is False
        and drop.get("review_authority") == "HUMAN_OPERATOR"
        and not isinstance(cue_index, bool)
        and isinstance(cue_index, int)
        and cue_index >= 1
        and not isinstance(start_ms, bool)
        and isinstance(start_ms, int)
        and not isinstance(end_ms, bool)
        and isinstance(end_ms, int)
        and 0 <= start_ms < end_ms
        and not isinstance(duration_ms, bool)
        and isinstance(duration_ms, int)
        and duration_ms == end_ms - start_ms
        and isinstance(deleted_text, str)
        and bool(deleted_text.strip())
        and drop.get("deleted_text_sha256")
        == "sha256:" + _sha_text(deleted_text)
        and isinstance(witness, Mapping)
        and witness.get("schema_version") == WITNESS_SCHEMA
        and witness.get("witness_protocol") == BLIND_PINYIN_PROTOCOL
        and witness.get("status") == "UNCERTAIN"
        and witness.get("reason_code")
        in PHYSICALLY_UNREADABLE_WITNESS_REASON_CODES
        and drop.get("witness_reason_code") == witness.get("reason_code")
        # 判据原文必须随件；Ivan 一眼要看到「11 syllables over 0.92s target」。
        and isinstance(drop.get("witness_detail"), str)
        and bool(str(drop.get("witness_detail")).strip())
        and _SHA256_HEX_RX.fullmatch(
            str(witness.get("request_sha256") or "")
        )
        is not None
    )


def build_unreadable_cue_drop_pass(
    *,
    pass_index: int,
    input_srt_sha256: str,
    output_srt_sha256: str,
    drops: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": DROP_PASS_SCHEMA,
        "pass_index": pass_index,
        "input_srt_sha256": input_srt_sha256,
        "output_srt_sha256": output_srt_sha256,
        "drops": list(drops),
    }


def build_unreadable_cue_drop_audit(
    *,
    passes: list[dict[str, Any]],
    status: str,
    final_srt_sha256: str | None = None,
) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "schema_version": DROP_AUDIT_SCHEMA,
        "status": status,
        "disposition": "AWAITING_HUMAN_UNREADABLE_CUE_REVIEW",
        "authority": DROP_AUTHORITY,
        "upload_authorized": False,
        "passes": list(passes),
    }
    if final_srt_sha256 is not None:
        audit["final_srt_sha256"] = final_srt_sha256
    return audit


def unreadable_cue_drop_audit_problem(
    receipt: object, *, expected_srt_sha256: str | None
) -> str | None:
    """返回阻断原因码；``None`` 表示这份删除审计完整自洽。

    与 ``exact-final-cpa-self-heal-audit.v1`` 同款：逐 pass 的
    input→output 哈希必须首尾相接，最后一段 output 必须等于最终交付字节。
    """

    if receipt is None:
        return None
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != DROP_AUDIT_SCHEMA
        or receipt.get("status") != "PASS"
        or receipt.get("authority") != DROP_AUTHORITY
        or receipt.get("upload_authorized") is not False
    ):
        return "UNREADABLE_CUE_DROP_AUDIT_INVALID"
    final_sha256 = str(receipt.get("final_srt_sha256") or "")
    if _SHA256_RX.fullmatch(final_sha256) is None or (
        expected_srt_sha256 is not None
        and final_sha256 != expected_srt_sha256
    ):
        return "UNREADABLE_CUE_DROP_FINAL_HASH_MISMATCH"
    passes = receipt.get("passes")
    if (
        not isinstance(passes, list)
        or not 1 <= len(passes) <= UNREADABLE_CUE_DROP_MAX_PASSES
    ):
        return "UNREADABLE_CUE_DROP_AUDIT_INVALID"
    previous_output: str | None = None
    for expected_index, pass_receipt in enumerate(passes, start=1):
        if (
            not isinstance(pass_receipt, Mapping)
            or pass_receipt.get("schema_version") != DROP_PASS_SCHEMA
            or pass_receipt.get("pass_index") != expected_index
        ):
            return "UNREADABLE_CUE_DROP_AUDIT_INVALID"
        input_sha256 = str(pass_receipt.get("input_srt_sha256") or "")
        output_sha256 = str(pass_receipt.get("output_srt_sha256") or "")
        drops = pass_receipt.get("drops")
        if (
            _SHA256_RX.fullmatch(input_sha256) is None
            or _SHA256_RX.fullmatch(output_sha256) is None
            or input_sha256 == output_sha256
            or (
                previous_output is not None
                and input_sha256 != previous_output
            )
            or not isinstance(drops, list)
            or not drops
        ):
            return "UNREADABLE_CUE_DROP_AUDIT_INVALID"
        for drop in drops:
            if not valid_unreadable_cue_drop(drop):
                return "UNREADABLE_CUE_DROP_RECEIPT_INVALID"
        previous_output = output_sha256
    if previous_output != final_sha256:
        return "UNREADABLE_CUE_DROP_FINAL_HASH_MISMATCH"
    return None


def drops_from_audit(receipt: object) -> list[dict[str, Any]]:
    """把删除审计摊平成逐条回执（报表/停泊回执用）。"""

    if not isinstance(receipt, Mapping):
        return []
    passes = receipt.get("passes")
    if not isinstance(passes, list):
        return []
    drops: list[dict[str, Any]] = []
    for pass_receipt in passes:
        if not isinstance(pass_receipt, Mapping):
            continue
        for drop in pass_receipt.get("drops") or []:
            if isinstance(drop, Mapping):
                drops.append(dict(drop))
    return drops
