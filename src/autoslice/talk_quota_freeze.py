"""准入时冻结：候选拿到席位那一刻生效的配额政策，写进它自己的 state 记录。

为什么需要：`prioritize()` 每个 tick 都把 `talk_backlog` 收回 `pending_talk`
重排一次，cap 与额外席位分数门都是**当场重算**的。2026-08-08 `4af4a88` 因此能
在没人察觉的情况下回溯改写 8/7 的合法性（`GAME_SESSION_EXTRA_SLOT_MIN_SCORE`
90->85 → 四条 89.0/87.25/86.75/86.0 一夜之间从落选变进席）。反向同样成立：政策
一收紧，已经在产甚至已经交付兄弟件的在席候选下一个 tick 就被打回 backlog。

冻结的语义：**席位一旦授予，它的合法性就由授予当时的政策说了算。** 后续的政策
变化只作用于新准入，不回溯打回、也不回溯放行已在席的候选。

冻结的边界（别把它当免死金牌）：这里只冻结**配额政策**。边界红旗、说话人、
BGM 排除、重叠隔离、内容门等一切非配额判定照常可以把冻结候选清出去——它们
和「这条当初该不该占这个席位」是两码事。
"""

from __future__ import annotations

from collections.abc import Mapping

from src.autoslice.talk_quota_policy import TalkQuotaPolicy

FREEZE_SCHEMA = "talk-quota-policy-freeze.v1"
FREEZE_FIELD = "talk_quota_policy_at_admission"


def freeze_admission_policy(
    item: dict, policy: TalkQuotaPolicy, *, admitted_position: int
) -> dict:
    """Stamp the policy in force onto a candidate that just won a seat."""

    stamp = {
        "schema_version": FREEZE_SCHEMA,
        "kind": policy.kind,
        "scope_key": policy.scope_key,
        "recording_date": policy.recording_date,
        "cap": policy.cap,
        "extra_slot_min_score": policy.extra_slot_min_score,
        "policy_source": policy.policy_source,
        "admitted_position": admitted_position,
    }
    item[FREEZE_FIELD] = stamp
    return stamp


def carry_frozen_admission(record: Mapping[str, object]) -> dict:
    """Spread-able carry of a pick's frozen seat onto its requeued item.

    A revived candidate re-enters `pending_talk` and is re-ranked with everyone
    else, but its seat was granted under the policy in force at admission —
    dropping the stamp on requeue would quietly reopen the retroactive-rewrite
    hole for exactly the most fragile class of candidate.
    """

    stamp = record.get(FREEZE_FIELD)
    return {FREEZE_FIELD: dict(stamp)} if isinstance(stamp, Mapping) else {}


def frozen_admission(item: Mapping[str, object], scope_key: str) -> dict | None:
    """Return this candidate's frozen seat for ``scope_key``, or ``None``.

    A malformed or scope-mismatched stamp is treated as absent — the candidate
    simply competes as fresh under today's policy.  A bad stamp must never
    crash selection, and must never grant a seat in a scope it was not issued
    for (a candidate whose scene context later reclassifies it out of the game
    scope does not carry its game-scope seat along).
    """

    stamp = item.get(FREEZE_FIELD)
    if not isinstance(stamp, Mapping):
        return None
    if stamp.get("schema_version") != FREEZE_SCHEMA:
        return None
    if str(stamp.get("scope_key") or "") != scope_key:
        return None
    cap = stamp.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        return None
    gate = stamp.get("extra_slot_min_score")
    if gate is not None and (isinstance(gate, bool) or not isinstance(gate, (int, float))):
        return None
    if not str(stamp.get("policy_source") or "").strip():
        return None
    return dict(stamp)
