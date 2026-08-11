"""语义路径 OR 聚合（v2 影子口径，与 v1 评分卡并存）。

ChatGPT Pro 2026-08-10 的结论（`docs/reviews/2026-08-10-or-gate-metric-and-
function-split.md` §二）：**裸 `max(单维分数)` 是错的，`softmax`/p-范数伪装成
OR 也是错的**。正确形态是"语义路径 OR"——

    硬门槛 ⟶ 若干"命名卖点路径"各自评分 ⟶ 取 max ⟶ 题材疲劳惩罚

    Q = max( P_broad − U_broad ,  max_j( P_j − U_j ) )
    E = Q − U_global − F(topic_fingerprint)

本模块只做上面这套聚合，**不动 v1 的任何算术**：``selection_scorecard.py`` 的
``DIMENSION_WEIGHTS`` / ``normalize_selection_scorecard`` / 校准锚点资产
（``assets/lidousha/selection_score_calibration.v1.json``，Ivan 2026-07-23 手签）
逐字不变。``selection_scorecard_is_valid`` 硬要求 ``weights == DIMENSION_WEIGHTS``
且锚点要求维度集合逐字相等——迁移它们是**政策行为**，不是重构。

## 两根轴为什么不在卖点路径里

- ``self_contained``：Pro 明确它是**必要条件不是卖点**，放最前当 gate，不参与 OR。
- ``lidousha_centrality``：Pro「仅仅『主播是主角』通常不足以构成值得发布的理由」，
  宜作归属条件而非可加权卖点轴。**而且它现在测的是错的东西**——rubric 那个括号
  写的是「李豆沙不可替代性」（能不能把她换掉），不是「她是不是这段的主体」；
  rubric 还明文把"身份不确定"赶进 ``uncertainty_penalty``（一个满分 100 里最多
  扣 15 的可补偿小减项）。2026-08-10 Ivan 盲审四条 tier-1 的真值证明这根轴与
  真值**反相关**：该发的那条 centrality=3，两条"主要发言人不是李豆沙"的是 4 和 3。

  Ivan 2026-08-10 裁定：「**必须要说话人分离才能判断李豆沙是不是主角，除非是单人
  直播**」「**说话人存疑都要直接给人工审阅**」。于是归属判定整体离开本模块，
  只以 ``attribution_status`` 这一个**输入接口**出现——见下。

## `attribution_status`：稳定接口，不在本模块判定

``VERIFIED_SOLO``（单人直播）/ ``VERIFIED_HOST_DOMINANT``（已分离且主播为主体）
可以继续算分；``UNVERIFIED`` **停泊转人工审阅**（Ivan 第 3 条），不是扣分、不是猜。
停泊复用既有机制 ``speaker_manual_review``（``speaker_review_required`` /
``speaker_evidence_insufficient`` 天然不在 ``DELIVERED_TALK_STATUSES`` 里），
本模块不新造平行状态。

判定本身归说话人分离车道——它当前的次序缺陷（``prioritize()`` 先于
``prepare_speaker_routing()``、生产未配 ``AUTOSLICE_SPEAKER_ROUTING_PROVIDER_*``）
是另一件事，与本模块解耦：无论那件怎么裁，这个接口都成立。

## ⚠️ 标定状态

``SOLO_PATH_BASE`` / 疲劳步长是**未标定的临时值**。Pro 自陈最没把握的正是
"单轴路径基础分带 H、最终席位阈值的具体数值——没有历史盲标数据不能拍定"。
因此本模块输出 ``provisional_calibration: True`` 且带 reason code
``V2_SHADOW_ONLY_NOT_A_RELEASE_GATE``：**v2 与 v1 并排记录，不决定任何席位**。
把 v2 调到"与 v1 高度相关"是**反目标**（Pro §五），本模块没有任何这样的项。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Sequence

from src.autoslice.channel_profile import load_channel_profile
from src.autoslice.selection_scorecard import (
    DIMENSION_WEIGHTS,
    _number,
    selection_scorecard_is_valid,
)
from src.autoslice.speaker_manual_review import SPEAKER_MANUAL_REVIEW_STATUSES

from pathlib import Path

_CHANNEL_PROFILE = load_channel_profile(Path(__file__).resolve().parents[2])
SCHEMA_VERSION = f"{_CHANNEL_PROFILE.profile_id}-selection-metric-or.v2"
DECISION_POLICY_VERSION = "semantic-path-or-with-standalone-proof.v2"
RUBRIC_VERSION = f"{_CHANNEL_PROFILE.profile_id}-scorecard-rubric.v1"

# 归属状态（本模块只消费，不判定）。
ATTRIBUTION_VERIFIED_SOLO = "VERIFIED_SOLO"
ATTRIBUTION_VERIFIED_HOST_DOMINANT = "VERIFIED_HOST_DOMINANT"
ATTRIBUTION_UNVERIFIED = "UNVERIFIED"
ATTRIBUTION_STATUSES = frozenset(
    {
        ATTRIBUTION_VERIFIED_SOLO,
        ATTRIBUTION_VERIFIED_HOST_DOMINANT,
        ATTRIBUTION_UNVERIFIED,
    }
)
# 停泊落到既有状态上，不新造名字。
PARK_STATUS = "speaker_review_required"
assert PARK_STATUS in SPEAKER_MANUAL_REVIEW_STATUSES

# 命名卖点路径：关系 / 立场 / 观众驱动 / 反差 / 笑点（Pro §二逐条）。
# `self_contained` 是 gate，`lidousha_centrality` 是归属条件——两者都不在这里。
SOLO_PATH_AXES: tuple[str, ...] = (
    "relationship_interaction",
    "stance_intensity",
    "audience_salience",
    "persona_reversal",
    "comedic_payoff",
)
# 综合路径「多项都不错」用同一组轴（把 gate 轴与归属轴排除后重新归一化）。
BROAD_PATH_AXES: tuple[str, ...] = SOLO_PATH_AXES
GATE_AXIS = "self_contained"
ATTRIBUTION_AXIS = "lidousha_centrality"

# 触发单轴 OR 的主张档位：只有 4 才是"主张"，且还要过证据合同才算 verified。
SOLO_PATH_CLAIM_LEVEL = 4
# 与 v1 既有可读性规则同口径（`normalize_selection_scorecard` 里 `<= 1` 判 Tier 3），
# 不是新阈值。
GATE_MIN_LEVEL = 2

# ⚠️ 未标定：见模块 docstring。平置，不按轴区分——没有盲标数据就没有区分的依据。
SOLO_PATH_BASE = 80.0
# 疲劳：同一**语义指纹**在同场重复出现才扣，与调用方声明的标签无关。
FATIGUE_STEP = 3.0
FATIGUE_MAX = 10.0

ADMISSIBLE_ATOM_KINDS = frozenset({"quote", "visual_event"})
ATOM_ROLE_ACTION = "action"
ATOM_ROLE_REACTION = "reaction"

_TUNABLES = {
    "solo_path_base": SOLO_PATH_BASE,
    "solo_path_claim_level": SOLO_PATH_CLAIM_LEVEL,
    "gate_min_level": GATE_MIN_LEVEL,
    "fatigue_step": FATIGUE_STEP,
    "fatigue_max": FATIGUE_MAX,
    "solo_path_axes": list(SOLO_PATH_AXES),
    "broad_path_axes": list(BROAD_PATH_AXES),
}
CONFIG_HASH = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(_TUNABLES, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
)


@dataclass(frozen=True)
class ProofAtom:
    """一条可定位的证据原子。

    ``identity`` 决定去重：**重复引用同一句不算两份**（Pro §二）。
    """

    axis: str
    kind: str
    cue_index: int
    start_ms: int
    end_ms: int
    role: str

    @property
    def identity(self) -> tuple[object, ...]:
        if self.kind == "quote":
            return ("quote", self.cue_index)
        return ("visual_event", self.start_ms, self.end_ms)

    @property
    def atom_id(self) -> str:
        raw = json.dumps(
            [self.axis, *[str(part) for part in self.identity]],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "atom:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _coerce_atom(raw: object) -> ProofAtom | None:
    """结构不合法的原子直接不成立——不猜、不补默认时间。"""

    if not isinstance(raw, Mapping):
        return None
    axis = str(raw.get("axis") or "")
    kind = str(raw.get("kind") or "")
    role = str(raw.get("role") or "")
    if axis not in SOLO_PATH_AXES or kind not in ADMISSIBLE_ATOM_KINDS:
        return None
    start = raw.get("start_ms")
    end = raw.get("end_ms")
    cue = raw.get("cue_index", -1)
    for value in (start, end, cue):
        if isinstance(value, bool) or not isinstance(value, int):
            return None
    assert isinstance(start, int) and isinstance(end, int) and isinstance(cue, int)
    if start < 0 or end < start:
        return None
    if kind == "quote" and cue < 0:
        return None
    if role and role not in {ATOM_ROLE_ACTION, ATOM_ROLE_REACTION}:
        return None
    return ProofAtom(
        axis=axis, kind=kind, cue_index=cue, start_ms=start, end_ms=end, role=role
    )


def admissible_atoms(
    raw_atoms: Sequence[object],
    *,
    candidate_start_ms: int,
    candidate_end_ms: int,
) -> dict[str, list[ProofAtom]]:
    """按轴分组的可采信原子：**可定位 + 去重**。

    可定位＝引文/视觉事件必须落在候选片段时间范围内（Pro §二）。窗外引用不是
    "扣分"，是这条原子根本不存在。
    """

    grouped: dict[str, list[ProofAtom]] = {axis: [] for axis in SOLO_PATH_AXES}
    seen: set[tuple[str, tuple[object, ...]]] = set()
    for raw in raw_atoms or ():
        atom = _coerce_atom(raw)
        if atom is None:
            continue
        if atom.start_ms < candidate_start_ms or atom.end_ms > candidate_end_ms:
            continue
        key = (atom.axis, atom.identity)
        if key in seen:
            continue
        seen.add(key)
        grouped[atom.axis].append(atom)
    return grouped


def event_closure(atoms: Sequence[ProofAtom]) -> tuple[bool, str]:
    """事件闭合：完整"动作→反应"链 **或** 两个不重叠的独立证据原子。

    两条是 **或**，不是且——Pro 明确「这样不会因为强求"两段证据"而误杀一个本身
    完整的单一高潮」：一条动作与紧随的反应在时间上重叠也算闭合。
    """

    actions = [atom for atom in atoms if atom.role == ATOM_ROLE_ACTION]
    reactions = [atom for atom in atoms if atom.role == ATOM_ROLE_REACTION]
    for action in actions:
        for reaction in reactions:
            if (
                action.identity != reaction.identity
                and reaction.start_ms >= action.start_ms
            ):
                return True, "ACTION_REACTION_CHAIN"
    ordered = sorted(atoms, key=lambda atom: (atom.start_ms, atom.end_ms))
    for index, earlier in enumerate(ordered):
        for later in ordered[index + 1:]:
            if later.start_ms >= earlier.end_ms:
                return True, "TWO_INDEPENDENT_ATOMS"
    return False, "EVENT_NOT_CLOSED"


def verify_standalone_proof(
    axis: str,
    level: int,
    atoms: Sequence[ProofAtom],
) -> tuple[bool, str]:
    """StandaloneProof 证据合同：`level=4` 只是主张，不是通行证。

    **证据不足不是"减 N 分"，而是路径不成立**（Pro §二）。
    """

    if level != SOLO_PATH_CLAIM_LEVEL:
        return False, "LEVEL_BELOW_CLAIM"
    if not atoms:
        return False, "NO_ADMISSIBLE_PROOF"
    closed, reason = event_closure(atoms)
    if not closed:
        return False, reason
    return True, reason


def _broad_path_score(dimensions: Mapping[str, int]) -> float:
    """旧的"多项都不错"综合路径，把 gate 轴与归属轴排除后重新归一化。"""

    total_weight = sum(DIMENSION_WEIGHTS[axis] for axis in BROAD_PATH_AXES)
    return sum(
        DIMENSION_WEIGHTS[axis] * 100.0 / total_weight * dimensions[axis] / 4
        for axis in BROAD_PATH_AXES
    )


def _axis_uncertainty(raw: Mapping[str, float] | None) -> dict[str, float]:
    table: dict[str, float] = {axis: 0.0 for axis in SOLO_PATH_AXES}
    for axis in SOLO_PATH_AXES:
        value = _number((raw or {}).get(axis, 0), minimum=0, maximum=15)
        table[axis] = 0.0 if value is None else value
    return table


def topic_fatigue(
    topic_fingerprint: str,
    session_fingerprint_counts: Mapping[str, int] | None,
) -> float:
    """疲劳只看**语义指纹**，不看调用方声明的标签。

    对抗变形「疲劳标签换成较轻类别（语义标签未变）」必须扣不掉分：本函数根本
    不读标签。疲劳是"组合选择效用"不是片子自身质量，所以在 max 之后全局扣。
    """

    if not topic_fingerprint:
        return 0.0
    count = (session_fingerprint_counts or {}).get(topic_fingerprint, 0)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 1:
        return 0.0
    return min(FATIGUE_MAX, FATIGUE_STEP * (count - 1))


def _gate_results(
    dimensions: Mapping[str, int], attribution_status: str
) -> tuple[dict[str, object], str]:
    gates: dict[str, object] = {}
    blocking = ""
    gates["attribution"] = {
        "status": attribution_status,
        "passed": attribution_status != ATTRIBUTION_UNVERIFIED,
        # 归属判定不在本模块——这里只消费接口。
        "reason": (
            "SPEAKER_ATTRIBUTION_UNVERIFIED"
            if attribution_status == ATTRIBUTION_UNVERIFIED
            else "SPEAKER_ATTRIBUTION_VERIFIED"
        ),
    }
    if attribution_status == ATTRIBUTION_UNVERIFIED:
        blocking = "SPEAKER_ATTRIBUTION_UNVERIFIED"
    passed_self_contained = dimensions[GATE_AXIS] >= GATE_MIN_LEVEL
    gates[GATE_AXIS] = {
        "level": dimensions[GATE_AXIS],
        "minimum": GATE_MIN_LEVEL,
        "passed": passed_self_contained,
        "reason": "" if passed_self_contained else "SELF_CONTAINED_GATE_FAILED",
    }
    if not passed_self_contained and not blocking:
        blocking = "SELF_CONTAINED_GATE_FAILED"
    return gates, blocking


def evaluate_selection_metric_v2(
    *,
    scorecard: Mapping[str, object],
    attribution_status: str,
    candidate_start_ms: int,
    candidate_end_ms: int,
    proof_atoms: Sequence[object] = (),
    axis_uncertainty: Mapping[str, float] | None = None,
    global_uncertainty: float = 0.0,
    topic_fingerprint: str = "",
    session_fingerprint_counts: Mapping[str, int] | None = None,
    fatigue_label: str = "",
    model_version: str = "",
) -> dict[str, object] | None:
    """算一份 v2 影子口径，与 v1 双口径并存。

    ``scorecard`` 必须是一张**已经有效的 v1 评分卡**（本模块不重算 v1，也不改它）。
    无效 → ``None``，调用方保持 v1 行为不变（fail-closed：v2 不成立不等于放行）。
    """

    if not selection_scorecard_is_valid(scorecard) or attribution_status not in (
        ATTRIBUTION_STATUSES
    ):
        return None
    if (
        isinstance(candidate_start_ms, bool)
        or isinstance(candidate_end_ms, bool)
        or not isinstance(candidate_start_ms, int)
        or not isinstance(candidate_end_ms, int)
        or candidate_start_ms < 0
        or candidate_end_ms <= candidate_start_ms
    ):
        return None
    global_penalty = _number(global_uncertainty, minimum=0, maximum=15)
    if global_penalty is None:
        return None
    assert isinstance(scorecard, Mapping)
    dimensions = dict(scorecard["dimensions"])  # type: ignore[arg-type]

    base: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "decision_policy_version": DECISION_POLICY_VERSION,
        "rubric_version": RUBRIC_VERSION,
        "config_hash": CONFIG_HASH,
        "model_version": str(model_version or ""),
        "provisional_calibration": True,
        "reason_codes": ["V2_SHADOW_ONLY_NOT_A_RELEASE_GATE"],
        # 双口径留痕（Pro §五）：v1 原样保留，绝不被 v2 覆盖。
        "legacy_raw_v1": scorecard["raw_score"],
        "legacy_effective_v1": scorecard["effective_score"],
        "attribution_status": attribution_status,
    }

    gates, blocking = _gate_results(dimensions, attribution_status)
    base["gate_results_v2"] = gates
    if blocking:
        base["status"] = (
            "PARKED" if blocking == "SPEAKER_ATTRIBUTION_UNVERIFIED" else "GATED"
        )
        base["quality_score_v2"] = None
        base["winning_path_v2"] = None
        base["path_scores_v2"] = {}
        base["proof_ids_v2"] = {}
        base["penalty_components_v2"] = {}
        # 键集合不随 status 变化：报表/持久化侧不必按状态分支取字段。
        base["verified_level_4_axes"] = []
        base["requires_speaker_manual_review"] = (
            blocking == "SPEAKER_ATTRIBUTION_UNVERIFIED"
        )
        base["reason_codes"] = [*base["reason_codes"], blocking]  # type: ignore[list-item]
        if blocking == "SPEAKER_ATTRIBUTION_UNVERIFIED":
            # Ivan 2026-08-10:「说话人存疑都要直接给人工审阅」——不是扣分、不是猜。
            base["speaker_manual_review_status"] = PARK_STATUS
        return base

    grouped = admissible_atoms(
        proof_atoms,
        candidate_start_ms=candidate_start_ms,
        candidate_end_ms=candidate_end_ms,
    )
    uncertainty = _axis_uncertainty(axis_uncertainty)
    path_scores: dict[str, object] = {}
    proof_ids: dict[str, list[str]] = {}
    verified_axes: list[str] = []

    # 综合路径。它依赖全部 broad 轴，所以扣的是这些轴的不确定性之和。
    broad_raw = _broad_path_score(dimensions)
    broad_penalty = sum(uncertainty[axis] for axis in BROAD_PATH_AXES)
    path_scores["broad"] = {
        "available": True,
        "base": round(broad_raw, 2),
        "uncertainty": round(broad_penalty, 2),
        "score": round(broad_raw - broad_penalty, 2),
        "reason": "BROAD_COMPOSITE",
    }

    # 单轴卖点路径。**路径相关的不确定性在取 max 之前、在路径内部扣**，且只扣
    # 该路径实际依赖的证据——否则落败轴的噪声会污染获胜路径（Pro §二）。
    for axis in SOLO_PATH_AXES:
        atoms = grouped[axis]
        verified, reason = verify_standalone_proof(axis, dimensions[axis], atoms)
        proof_ids[axis] = [atom.atom_id for atom in atoms]
        if not verified:
            path_scores[axis] = {
                "available": False,
                "base": None,
                "uncertainty": round(uncertainty[axis], 2),
                "score": None,
                "reason": reason,
            }
            continue
        verified_axes.append(axis)
        path_scores[axis] = {
            "available": True,
            "base": round(SOLO_PATH_BASE, 2),
            "uncertainty": round(uncertainty[axis], 2),
            "score": round(SOLO_PATH_BASE - uncertainty[axis], 2),
            "reason": reason,
        }

    available = [
        (str(name), float(entry["score"]))  # type: ignore[index]
        for name, entry in path_scores.items()
        if isinstance(entry, Mapping) and entry.get("available") is True
    ]
    # 取 max：名字稳定排序保证同分可复现。
    winning_path, quality = max(available, key=lambda item: (item[1], item[0]))
    fatigue = topic_fatigue(topic_fingerprint, session_fingerprint_counts)
    effective = max(0.0, quality - global_penalty - fatigue)

    base["status"] = "VALID"
    base["quality_score_v2"] = round(effective, 2)
    base["winning_path_v2"] = winning_path
    base["path_scores_v2"] = path_scores
    base["proof_ids_v2"] = proof_ids
    base["verified_level_4_axes"] = verified_axes
    base["penalty_components_v2"] = {
        "per_path_uncertainty": {axis: round(uncertainty[axis], 2) for axis in SOLO_PATH_AXES},
        "broad_uncertainty": round(broad_penalty, 2),
        "global_uncertainty": round(global_penalty, 2),
        "fatigue": round(fatigue, 2),
        # 声明的标签只回显，不参与计算——换个轻标签规避不掉疲劳。
        "declared_fatigue_label": str(fatigue_label or ""),
        "topic_fingerprint": str(topic_fingerprint or ""),
    }
    base["requires_speaker_manual_review"] = False
    return base
