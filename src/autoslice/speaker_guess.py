"""证据不足时的 best-effort 说话人分离（"猜"），而不是不产出。

维护者 两句逐字，缺一不可：

1. 「它必须无论如何至少先猜一个说话人，我才能审查，不能猜都不猜」——
   ``b4d4000`` 把证据不足从"判死"改成了"停泊等人审阅"，但**停泊件根本没有产物**
   （producer rc=1，没有成品视频、没有烧字幕），于是"等人工审阅"是空话。
2. 「我说的猜不是全片统一李豆沙，这样的话我要修正的工作量太大了，**我要的就是
   正常分离两说话人，尽最大努力分开，然后再由我改正**」——统一色会把所有归属
   抹平，他要逐句重标；一个"尽力而为但可能有错"的分离，他只改错的那几句。

**为什么技术上做得到**（不是把不合格证据判成合格）：两条真实受害者
``auto_220747_1271_1323``/``auto_213135_62_138`` 的死因都是
``not enough 李豆沙 clip anchors: []`` / ``[2]``——这是
``_prepare_campplus_anchor_state`` 里"可信主播声纹银行"的构建失败（清过
``host_session_seed_min`` 的 cue 少于 2 条），**不是分离本身失败**。此时
``seed_scores``（每条 cue 对已登记声纹的余弦相似度中位数）**已经逐句算好了**，
下游的客人锚点挖掘、two-means 边界、模糊带、语境投票整条链都还没跑。把
seed 分最高的 N 条 cue 提名成主播锚点，整条既有链照常跑完，产出的就是**真正
的逐句双人分离**，而且每条 cue 都带 ``seed_score``/``host_score``/
``guest_score``/``margin``——正好是 维护者 定位"该改哪几句"要的东西。

**阈值一字未动**。回落是**明示的降级**，不是放宽门：``host_session_seed_min``
的数值没改，提名件逐条记录自己**没有**清过它（``clears_host_session_seed_min``
恒 False），受影响的 cue 逐条落进 ``low_confidence_cues``。合格证据走的仍是
``status="READY"`` 那条路，猜出来的走 ``status="SPEAKER_GUESS"``，两者在
manifest、包内文件名与运行时状态上全程可分。

**梯子有底**（维护者「无论如何」的落地）。三级，逐级降级、逐级可区分：

``GUESSED_CLIP_ANCHORS``
    锚点银行不足 → 提名 seed 分 top-N 当主播锚点，**完整双人分离照跑**。
``UNRESOLVED_CONTEXT_DELIVERED``
    分离跑完了，只是若干 cue 的语境没定（原本会 ``SPEAKER_REVIEW_REQUIRED``
    删掉产物）→ 照常交付自动标注，把未定 cue 逐条标出来。
``IDENTITY_INDETERMINATE_UNIFORM_HOST``
    连提名锚点都救不回来（第二次 indeterminate）→ 最后兜底才用全片统一主播色。
    这一级 维护者 明说修正成本高，所以它**只是兜底**，receipt 的 ``rung`` 与报表
    都明写"分离没跑成"，绝不与上面两级混为一谈。

**fail-closed 不因为有产物而松动**：``SPEAKER_GUESS`` 的 manifest
``production_ready`` 恒 False；producer 只在 ``speaker_mode=="auto"`` 且自己
主动要过 guess 时才接受它（``required`` 永远看不到这个开关）；成品落盘后
lane 仍把候选压回 ``b4d4000`` 的停泊态（``speaker_review_required`` /
``speaker_evidence_insufficient``），停泊态不在 ``DELIVERED_TALK_STATUSES``
里，于是不进 ``review_ready``、不进日审清单、不可上传。上传唯一授权仍是仓内
``assets/lidousha/publication_registry.v1.json``。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from src.autoslice import unreadable_cue_review
from src.autoslice.speaker_common import HOST_SPEAKER, SpeakerIdentityIndeterminate


SPEAKER_GUESS_SCHEMA = "speaker-best-effort-guess.v1"
#: 只有这个状态代表"分离结果是猜的"。``READY`` 的语义（证据充分）不被稀释。
SPEAKER_GUESS_STATUS = "SPEAKER_GUESS"
GUESSED_HOST_ANCHOR_SCOPE = "guessed_clip"

RUNG_GUESSED_ANCHORS = "GUESSED_CLIP_ANCHORS"
RUNG_UNRESOLVED_CONTEXT = "UNRESOLVED_CONTEXT_DELIVERED"
RUNG_UNIFORM_HOST = "IDENTITY_INDETERMINATE_UNIFORM_HOST"

#: 逐级人话，报表与 receipt 共用一套措辞，免得两处口径漂移。
RUNG_LABELS = {
    RUNG_GUESSED_ANCHORS: "猜主播锚点后完整双人分离（逐句归属可能有错）",
    RUNG_UNRESOLVED_CONTEXT: "双人分离已跑完，部分 cue 语境未定",
    RUNG_UNIFORM_HOST: "分离没跑成，兜底全片统一主播色",
}

#: 每条 cue 的证据缺口码。维护者 要的是"改哪几句"，所以缺口逐 cue 记，不是整片一个标记。
GAP_HOST_ANCHOR_GUESSED = "HOST_ANCHOR_GUESSED"
GAP_CONTEXT_UNRESOLVED = "CONTEXT_UNRESOLVED"
GAP_AMBIGUOUS_MARGIN = "AMBIGUOUS_ACOUSTIC_MARGIN"
GAP_NO_SEPARATION = "NO_ACOUSTIC_SEPARATION"

#: manifest 会被完整 scp 回来并进包，逐 cue 行必须有上限；总数另记，不靠行数推。
MAX_DISCLOSED_CUE_ROWS = 400

#: ``_resolve_unresolved_speaker_gate`` 里"语境没定 → 停止交付"那一条的消息前缀。
#: 只认这一条：同一个异常类型还承载"分析器返回了非法证据"等真缺陷，那些必须继续硬失败。
UNRESOLVED_CONTEXT_STOP_PREFIX = (
    "whole-clip context did not resolve ambiguous speaker cues"
)


def is_unresolved_context_stop(exc: BaseException) -> bool:
    return str(exc).startswith(UNRESOLVED_CONTEXT_STOP_PREFIX)


def nominate_host_anchors(
    seed_scores: Sequence[float],
    *,
    anchor_count: int,
    seed_min: float,
) -> tuple[list[int], list[dict[str, object]]] | None:
    """提名 seed 分最高的若干 cue 当主播锚点（**不动阈值，明示降级**）。

    返回 ``(indices, rows)``；cue 总数不足 2 条时返回 ``None``——那种片子连
    "两个说话人"这个前提都不成立，交给上层降到最后一级兜底。

    ``rows`` 逐条写明该 cue 的 seed 分与它**没有**清过的阈值，这样 维护者 一眼
    看得出这次猜有多虚（真实受害者是 ``[]``/``[2]``，即 0~1 条清过阈值）。
    """

    if anchor_count < 2:
        anchor_count = 2
    if len(seed_scores) < 2:
        return None
    ranked = sorted(
        range(len(seed_scores)), key=lambda index: seed_scores[index], reverse=True
    )
    indices = sorted(ranked[:anchor_count])
    rows = [
        {
            "source_index": index + 1,
            "seed_score": round(float(seed_scores[index]), 8),
            "host_session_seed_min": float(seed_min),
            "clears_host_session_seed_min": bool(
                float(seed_scores[index]) >= float(seed_min)
            ),
        }
        for index in indices
    ]
    return indices, rows


def nominated_rows_from_analysis(
    analysis: Mapping[str, object],
) -> list[dict[str, object]]:
    """从 analysis 反推提名件，免得为一份收据把状态穿过四层调用。

    ``host_anchor_cues`` 与逐 cue ``seed_score`` 本来就随分析结果一路返回，
    ``policy.host_session_seed_min`` 也在里面——重建是无损的。
    """

    if analysis.get("host_anchor_scope") != GUESSED_HOST_ANCHOR_SCOPE:
        return []
    policy = analysis.get("policy")
    seed_min = (policy or {}).get("host_session_seed_min") if isinstance(policy, Mapping) else None
    scores = {
        row.get("source_index"): row.get("seed_score")
        for row in _decision_rows(analysis)
    }
    anchors = analysis.get("host_anchor_cues")
    rows: list[dict[str, object]] = []
    for index in anchors if isinstance(anchors, list) else []:
        score = scores.get(index)
        rows.append(
            {
                "source_index": index,
                "seed_score": score,
                "host_session_seed_min": seed_min,
                "clears_host_session_seed_min": bool(
                    isinstance(score, (int, float))
                    and isinstance(seed_min, (int, float))
                    and float(score) >= float(seed_min)
                ),
            }
        )
    return rows


def _decision_rows(analysis: Mapping[str, object]) -> list[Mapping[str, object]]:
    decisions = analysis.get("decisions")
    if not isinstance(decisions, list):
        return []
    return [row for row in decisions if isinstance(row, Mapping)]


def _int_set(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {
        int(item)
        for item in value
        if isinstance(item, int) and not isinstance(item, bool)
    }


def low_confidence_cues(
    analysis: Mapping[str, object], *, rung: str
) -> tuple[list[dict[str, object]], int]:
    """逐 cue 列出"这句的归属凭什么、缺什么"。

    返回 ``(rows, total)``；``rows`` 按 ``MAX_DISCLOSED_CUE_ROWS`` 截断，
    ``total`` 是未截断的真实条数。
    """

    unresolved = _int_set(analysis.get("context_unresolved_cues"))
    ambiguous = _int_set(analysis.get("context_required_cues"))
    anchor_guessed = (
        analysis.get("host_anchor_scope") == GUESSED_HOST_ANCHOR_SCOPE
    )
    rows: list[dict[str, object]] = []
    for decision in _decision_rows(analysis):
        index = decision.get("source_index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        gaps: list[str] = []
        if rung == RUNG_UNIFORM_HOST:
            gaps.append(GAP_NO_SEPARATION)
        if index in unresolved:
            gaps.append(GAP_CONTEXT_UNRESOLVED)
        elif index in ambiguous:
            gaps.append(GAP_AMBIGUOUS_MARGIN)
        if anchor_guessed:
            gaps.append(GAP_HOST_ANCHOR_GUESSED)
        if not gaps:
            continue
        rows.append(
            {
                "source_index": index,
                "speaker": decision.get("speaker"),
                "decision_source": decision.get("decision_source"),
                "seed_score": decision.get("seed_score"),
                "host_score": decision.get("host_score"),
                "guest_score": decision.get("guest_score"),
                "margin": decision.get("margin"),
                "reason_codes": gaps,
            }
        )
    return rows[:MAX_DISCLOSED_CUE_ROWS], len(rows)


def speaker_counts(analysis: Mapping[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in _decision_rows(analysis):
        speaker = str(decision.get("speaker") or "")
        if speaker:
            counts[speaker] = counts.get(speaker, 0) + 1
    return counts


def build_guess_receipt(
    analysis: Mapping[str, object],
    *,
    rung: str,
    blocked_reason: str,
    nominated_anchors: Sequence[Mapping[str, object]] = (),
    review_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """成品自述"我是猜的、猜的依据是什么、哪几句最可能错"。

    与 ``speaker_manual_review`` 的停泊回执分工：那份说"这条在等人看"，这份说
    "这条的说话人归属是怎么猜出来的"。两份都显式写死 ``upload_authorized``
    False——真正的 fail-closed 由运行时状态与出版登记承载，回执只是收据。
    """

    rows, total = low_confidence_cues(analysis, rung=rung)
    policy = analysis.get("policy")
    policy = policy if isinstance(policy, Mapping) else {}
    receipt: dict[str, object] = {
        "schema_version": SPEAKER_GUESS_SCHEMA,
        "status": SPEAKER_GUESS_STATUS,
        "rung": rung,
        "rung_label": RUNG_LABELS.get(rung, rung),
        # 仓内既有 *_authority / evidence_source 范式：权威面写"这不是证据支撑的"。
        "speaker_authority": "GUESSED_NOT_EVIDENCE_BACKED",
        "evidence_source": "campplus_seed_scores_below_host_session_seed_min"
        if rung == RUNG_GUESSED_ANCHORS
        else (
            "campplus_two_means_with_unresolved_context"
            if rung == RUNG_UNRESOLVED_CONTEXT
            else "none_acoustic_identity_indeterminate"
        ),
        "upload_authorized": False,
        "review_authority": "HUMAN_OPERATOR",
        "resolution": "SPEAKER_TURN_OVERRIDE_OR_EXPLICIT_REJECTION",
        # 明说没放宽门，免得后人把这条当成"阈值可以调"的先例。
        "thresholds_unchanged": True,
        "blocked_reason": str(blocked_reason)[:600],
        "mode": analysis.get("mode"),
        "multi_speaker_detected": analysis.get("multi_speaker_detected"),
        "host_anchor_scope": analysis.get("host_anchor_scope"),
        "host_anchor_cues": analysis.get("host_anchor_cues"),
        "clip_host_anchor_candidates": analysis.get("clip_host_anchor_candidates"),
        "host_session_seed_min": policy.get("host_session_seed_min"),
        # 猜锚点这一级最要命的失败模式不是"某几句标错"，而是**整体极性反了**：
        # 被提名成主播的那簇其实是客人。维护者 改一次极性就能翻回来，但他得先知道
        # 有这个可能——所以写在回执里，不让他从 seed 分自己推。
        "host_guest_polarity": (
            "GUESSED_FROM_TOP_SEED_CUES_MAY_BE_INVERTED"
            if rung == RUNG_GUESSED_ANCHORS
            else "NOT_GUESSED"
        ),
        "nominated_host_anchors": [dict(row) for row in nominated_anchors],
        "source_cue_count": len(_decision_rows(analysis)),
        "speaker_counts": speaker_counts(analysis),
        "low_confidence_cue_count": total,
        "low_confidence_cues_truncated": total > len(rows),
        "low_confidence_cues": rows,
    }
    if review_manifest is not None:
        # 原本要给人看的 hash-bound 证据面不能因为改成交付就丢；剥掉 analysis
        # （已在 manifest 顶层）只留绑定与 cue 清单。
        receipt["review_evidence"] = {
            key: value
            for key, value in review_manifest.items()
            if key != "analysis"
        }
    return receipt


def uniform_host_last_resort_analysis(
    *, cue_count: int, host_speaker: str, reason: str
) -> dict[str, object]:
    """最后一级兜底：分离没跑成，全片按主播标注。

    ``identity_indeterminate_analysis`` 把每条 cue 标成 GUEST 并要求 review，
    那是"拒绝交付"的投影，不能直接当交付标注用（会把主播自己的话全标成客人）。
    这里显式产一份统一主播标注，并且 ``review_required`` 为 False——它不再走
    审阅门删产物那条路，改由上层的 guess receipt + 停泊态承载 fail-closed。
    """

    return {
        "mode": "speaker_guess_uniform_host_last_resort",
        "multi_speaker_detected": False,
        "review_required": False,
        "host_anchor_scope": "guess_last_resort",
        "identity_indeterminate_reason": str(reason)[:600],
        "context_unresolved_cues": [],
        "context_required_cues": list(range(1, cue_count + 1)),
        "decisions": [
            {
                "source_index": index,
                "speaker": host_speaker,
                "decision_source": "speaker_guess_uniform_host_last_resort",
                "margin": None,
            }
            for index in range(1, cue_count + 1)
        ],
    }


def finalizer_manifest_block_reason(
    manifest: Mapping[str, object], *, best_effort_guess: bool
) -> str | None:
    """producer 侧的收货门：返回拒收理由，``None`` 表示可以继续验哈希绑定。

    ``READY`` 的语义不变（证据充分 + ``production_ready``）。``SPEAKER_GUESS``
    只有在**本次主动要过降级**且 manifest 自带 guess 回执时才收——没要过却收到
    guess，或 guess 没有回执，一律当篡改拒收（fail-closed 不因为有产物而松动）。
    """

    status = manifest.get("status")
    if status == SPEAKER_GUESS_STATUS:
        if not best_effort_guess or not isinstance(manifest.get("speaker_guess"), Mapping):
            return "unrequested or unreceipted speaker guess"
        if manifest.get("production_ready") is not False:
            return "speaker guess must never claim production_ready"
        return None
    if status != "READY" or manifest.get("production_ready") is not True:
        return str(manifest.get("reason"))
    return None


class GuessLadder:
    """把"无论如何都要有产物"的梯子收在一个对象里，finalizer 只留调用点。

    三级见 module docstring。``enabled`` 为 False（``required`` 档、以及任何没
    主动要过降级的调用）时它**完全透明**：原样抛原异常、原样交回审阅 manifest，
    既有行为一个字节不变。
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        self.rung: str | None = None
        self.reason = ""

    def escalate(
        self,
        identity_error: BaseException,
        *,
        retry: Callable[..., dict[str, object]],
        cue_count: int,
    ) -> dict[str, object]:
        """第一级：提名锚点重跑完整分离；再不行才落到最后一级兜底。"""

        if not self.enabled:
            raise identity_error
        self.reason = str(identity_error)
        try:
            analysis = retry(guess_host_anchors=True)
        except SpeakerIdentityIndeterminate as exc:
            self.reason = f"{self.reason} -> {exc}"
            self.rung = RUNG_UNIFORM_HOST
            return uniform_host_last_resort_analysis(
                cue_count=cue_count, host_speaker=HOST_SPEAKER, reason=self.reason
            )
        self.rung = RUNG_GUESSED_ANCHORS
        return analysis

    def absorb_gate_stop(self, exc: BaseException) -> None:
        """第二级之一：语境没定导致的"停止交付"。其余同类型异常必须继续硬失败。"""

        if not (self.enabled and is_unresolved_context_stop(exc)):
            raise exc
        self.reason = self.reason or str(exc)
        self.rung = self.rung or RUNG_UNRESOLVED_CONTEXT

    def absorb_gate_manifest(self, review_manifest: Mapping[str, object] | None) -> None:
        """第二级之二：审阅 manifest 建成了——改成交付，证据面转进 guess 回执。"""

        if review_manifest is None:
            return
        self.reason = self.reason or str(review_manifest.get("reason") or "")
        self.rung = self.rung or RUNG_UNRESOLVED_CONTEXT

    def receipt(
        self,
        analysis: Mapping[str, object],
        review_manifest: Mapping[str, object] | None,
    ) -> dict[str, object] | None:
        if self.rung is None:
            return None
        return build_guess_receipt(
            analysis,
            rung=self.rung,
            blocked_reason=self.reason,
            nominated_anchors=nominated_rows_from_analysis(analysis),
            review_manifest=review_manifest,
        )


def summary_digest(manifest: Mapping[str, object] | None) -> dict[str, object] | None:
    """producer stdout 摘要里的紧凑指纹（lane 从这里认出"这是猜的"）。

    完整 receipt 留在 speaker manifest 里；摘要块受 ``last_json_block`` 的
    4000 字节尾窗约束，塞进逐 cue 清单会把整个摘要挤没。
    """

    if not isinstance(manifest, Mapping):
        return None
    receipt = manifest.get("speaker_guess")
    if manifest.get("status") != SPEAKER_GUESS_STATUS or not isinstance(
        receipt, Mapping
    ):
        return None
    return {
        "schema_version": SPEAKER_GUESS_SCHEMA,
        "rung": receipt.get("rung"),
        "rung_label": receipt.get("rung_label"),
        "speaker_authority": receipt.get("speaker_authority"),
        "upload_authorized": False,
        "source_cue_count": receipt.get("source_cue_count"),
        "low_confidence_cue_count": receipt.get("low_confidence_cue_count"),
        "speaker_counts": receipt.get("speaker_counts"),
        "blocked_reason": str(receipt.get("blocked_reason") or "")[:200],
    }


def _guess_digest_from_summary(result: Mapping[str, object]) -> dict[str, object] | None:
    summary = result.get("summary")
    if not isinstance(summary, Mapping):
        return None
    if summary.get("speaker_status") != SPEAKER_GUESS_STATUS:
        return None
    digest = summary.get("speaker_guess")
    return dict(digest) if isinstance(digest, Mapping) else {}


def _guess_digest_from_manifests(work_dir: object) -> dict[str, object] | None:
    """从落盘的 speaker manifest 认 guess——这是**权威**源，stdout 摘要只是便道。

    为什么不能只信 stdout：lane 读的是 ``last_json_block(attempt_output[-4000:])``，
    交付摘要本身就带 closure_sentence / boundary_repairs / 一串绝对路径，再加上
    guess 指纹，撑破 4000 字节尾窗完全可能。撑破之后 ``last_json_block`` 会退而
    匹配到某个**嵌套**对象（比如 timing_qa），于是 ``speaker_status`` 读不到，
    猜出来的成品就被判成 review_ready ——检测失败必须 fail-closed，不能 fail-open。

    多份 manifest 时只要有一份自称 guess 就停泊：宁可多停一条让人看一眼，
    也不能把猜的当证据充分放行。
    """

    from pathlib import Path

    try:
        paths = sorted(Path(str(work_dir)).glob("replacement_recuts/*.speaker-final.json"))
    except OSError:
        return None
    for path in paths:
        try:
            import json

            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        digest = summary_digest(document if isinstance(document, Mapping) else {})
        if digest is not None:
            return digest
    return None


def delivered_talk_status(
    result: dict, *, candidate_id: str, work_dir: object | None = None
) -> str:
    """交付成功后的最终状态：猜出来的成品压回停泊态，其余照常 review_ready。

    这是 rc==0 的路径，``classify_talk_failure`` 不会跑，所以失败面字段必须逐个
    补齐——尤其 ``failure_recovery_fingerprint``：少了它，requeue 会拿
    ``failure_kind=None`` 去算**全量** pipeline 指纹，于是仓里任何一次改动都能
    唤醒它、每次都重烧一遍完整产线。
    """

    digest = _guess_digest_from_summary(result)
    if digest is None and work_dir is not None:
        digest = _guess_digest_from_manifests(work_dir)
    if digest is None:
        return "review_ready"
    from src.autoslice import speaker_manual_review

    rung = str(digest.get("rung") or "")
    # 停泊态沿用 b4d4000 的两个既有状态名，不新造：带 hash-bound cue 清单的
    # 走 speaker_review_required，锚点不足那类走 speaker_evidence_insufficient。
    status = (
        "speaker_review_required"
        if rung == RUNG_UNRESOLVED_CONTEXT
        else "speaker_evidence_insufficient"
    )
    result["status"] = status
    result["speaker_guess"] = digest
    result["failure_kind"] = "speaker_evidence"
    result["failure_stage"] = "speaker_finalization"
    result["failure_recoverable"] = False
    result.setdefault(
        "failure_message",
        f"SPEAKER_GUESS[{rung}]: {digest.get('blocked_reason') or 'speaker evidence insufficient'}",
    )
    try:
        result["failure_recovery_fingerprint"] = _runner_recovery_fingerprint(
            candidate_id
        )
    except Exception:  # noqa: BLE001 - 指纹算不出不该毙掉已经产好的成品
        result.pop("failure_recovery_fingerprint", None)
    speaker_manual_review.park_for_manual_review(
        result,
        reason="speaker_attribution_guessed_pending_human_correction",
        guess=digest,
        artifacts=delivered_artifact_paths(result),
    )
    return status


def finalize_delivered_talk_status(
    result: dict, *, candidate_id: str, work_dir: object, cover_ready: bool
) -> str:
    """produce 成功收尾时**唯一**决定终态的地方；停泊优先级高于封面待定。

    次序不是随手排的：``media_ready_cover_pending`` 有自己的修复车道，封面一旦
    绑定成功那条车道会**直接**把状态提成 ``review_ready``（``cover_maintenance``
    里 ``rec["status"] = "review_ready"``），根本不重跑 produce。如果猜出来的成品
    先掉进封面待定，它就会绕过停泊、经封面车道升进日审清单并变成可上传——正是
    fail-closed 要挡的那条路。所以先判停泊：停泊件不许被封面车道认领。

    第二类停泊（维护者「不可读窗」裁定）同理接在这里：produce 删过
    字幕的成品一律不许进 ``review_ready``。回执**无论如何都盖**——即使这条已
    经因说话人证据不足停泊，维护者 也必须看到「这里还少了一句话」。
    """

    status = delivered_talk_status(
        result, candidate_id=candidate_id, work_dir=work_dir
    )
    unreadable_drops = unreadable_cue_review.drops_from_work_dir(work_dir)
    if unreadable_drops:
        unreadable_cue_review.park_for_unreadable_cue_review(
            result,
            drops=unreadable_drops,
            artifacts=delivered_artifact_paths(result),
            held_status=status,
        )
        if status == "review_ready":
            result["status"] = (
                unreadable_cue_review.UNREADABLE_CUE_REVIEW_STATUS
            )
            result["failure_recoverable"] = False
            result.setdefault(
                "failure_message",
                "UNREADABLE_CUE_DROPPED_PENDING_HUMAN_REVIEW: "
                + "; ".join(
                    f"cue {drop.get('cue_index')} "
                    f"「{drop.get('deleted_text')}」 "
                    f"{drop.get('witness_reason_code') or drop.get('status')}"
                    for drop in unreadable_drops
                )[:600],
            )
            return str(result["status"])
        return status
    if status != "review_ready":
        return status
    if not cover_ready:
        result["status"] = _cover_pending_status()
        result["cover_integrity_status"] = "INVALID_OR_MISSING_INITIAL_COVER"
        result["cover_pending_reason_codes"] = ["TALK_DELIVERY_COVER_PROOF_REQUIRED"]
        return str(result["status"])
    result["status"] = "review_ready"
    return "review_ready"


def _cover_pending_status() -> str:
    from src.autoslice import talk_lane

    return str(talk_lane._runner.TALK_COVER_PENDING_STATUS)


def _runner_recovery_fingerprint(candidate_id: str) -> str:
    from src.autoslice import talk_lane

    return talk_lane._runner.talk_failure_recovery_fingerprint(
        "speaker_evidence", candidate_id
    )


def delivered_artifact_paths(result: Mapping[str, object]) -> dict[str, object]:
    """把成品路径写进 state。

    两个作用：维护者 从报表直接拿到"打开哪个文件看"；``free`` 的容量清理在删
    ``out/``/媒体前会扫 state 引用（2026-07/08 两次误删的血泪），停泊件的成品
    必须被 state 引用住才不会被当孤儿清掉。
    """

    summary = result.get("summary")
    summary = summary if isinstance(summary, Mapping) else {}
    paths = {
        "burned_video": summary.get("delivery"),
        "text_srt": summary.get("subtitle"),
        "speaker_srt": summary.get("speaker_subtitle"),
        "speaker_ass": summary.get("speaker_ass"),
    }
    return {key: value for key, value in paths.items() if value}
