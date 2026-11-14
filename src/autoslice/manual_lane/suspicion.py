"""Variant C (TASK-5, U119): listen only where a doubt is, escalate only while it is unsettled (old-pipeline goal:
text first, listen only at a specific suspicious window; up to three levels per doubt; soft budget = account, never
refuse).

Pure functions here: detection rules and the escalation plan.  The CPA review and the listening calls are made by
stage_impl.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from . import content_compare, doubt_policy
from .lane_rules import rules

if TYPE_CHECKING:
    from .evidence_attach import ReadAloudRef
    from .mishearing import LexiconSurface

# Escalation: a doubt is heard by the cheapest witness first; each level adds the next witnesses, and a level is only
# run while the doubt is still unsettled (CPA chose with high confidence: is_settled).  A level is (name, kind,
# witnesses): kind "none" adds no witness (CPA decides from the text evidence already attached); kind "asr" re-recognises only the doubt's window with the ASR engines named (MAI, MOSS: fast, billed by
# audio seconds); kind "listen" is the Gemini blind listening (slugs as in stage_impl.LISTEN_MODELS; Gemini 3.8 Low, then
# argon, one family, U122).  A doubt still unsettled after the last level goes to CPA's choice, marked low.
LADDERS: dict[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "asr_first": (("L1", "asr", ("mai", "moss")), ("L2", "listen", ("38m",)), ("L3", "listen", ("argon",))),
    "gemini": (("L1", "listen", ("38m",)), ("L2", "listen", ("argon",))),
    # the old pipeline's order (40-subtitle-text: 先文字后听音): CPA first decides from the text evidence it already has
    "text_first": (("L0", "none", ()), ("L1", "listen", ("38m",)), ("L2", "listen", ("argon",))),
    "text_asr": (("L0", "none", ()), ("L1", "asr", ("mai", "moss")), ("L2", "listen", ("38m",)),
                 ("L3", "listen", ("argon",))),
}
# TASK-11 measured: same training score as Gemini-first, Gemini windows 74 -> 19.  The default is recorded in lane_rules
# listening.ladder (asr_first, TASK-29 ruling 1); MANUAL_LANE_LADDER overrides it.
DEFAULT_LADDER = str(rules("listening")["ladder"])


def ladder() -> str:
    name = os.environ.get("MANUAL_LANE_LADDER") or str(rules("listening")["ladder"])
    if name not in LADDERS:
        raise ValueError(f"MANUAL_LANE_LADDER must be one of {sorted(LADDERS)}, got {name!r}")
    return name


def escalation_levels() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    return LADDERS[ladder()]


SOFT_BUDGET = {"windows": 3, "seconds": 60.0, "per_window_calls": 3}   # accounted in PROCESS, never enforced


def asr_disagrees(cue_text: str, jianying_text: str | None) -> bool:
    """BCUT and the jianying ASR do not agree on this cue.  With lane_rules detection.content_key (TASK-28, COACH e05
    §0/§11) they are compared on their content characters (content_compare), and a difference only in how often a word
    is repeated is not a disagreement (the repeat rule decides it, §4).  No jianying text is no evidence here
    (one_side_empty covers it)."""
    if not jianying_text or not doubt_policy.sim_key(jianying_text):
        return False
    if not rules("detection").get("content_key"):
        return doubt_policy.sim_key(cue_text) != doubt_policy.sim_key(jianying_text)
    return (content_compare.differs_in_content(cue_text, jianying_text)
            and not content_compare.repeat_variant(cue_text, jianying_text))


def one_side_empty(cue_text: str, jianying_text: str | None) -> bool:
    """COACH e05 §1.1 第5条: BCUT has words for the cue and jianying has none (lane_rules detection.one_side_empty)."""
    return bool(rules("detection").get("one_side_empty")) and bool(doubt_policy.sim_key(cue_text)) \
        and not doubt_policy.sim_key(jianying_text or "")


def lexicon_hits(cue_text: str, pairs: Sequence[LexiconSurface]) -> list[str]:
    """Return only generated surface hits; scope is shown to CPA, not a replacement action."""
    hits = []
    for pair in pairs:
        misheard = str(pair.get("misheard") or "")
        if len(misheard) >= 2 and misheard in cue_text:
            hits.append(misheard)
    return sorted(set(hits))


def merge_suspicious(cue_ids: Sequence[int], *, asr: Mapping[int, bool], lexicon: Mapping[int, Sequence[str]],
                     review: Mapping[int, str], asr_notes: Mapping[int, Sequence[str]] | None = None,
                     read_aloud: Mapping[int, ReadAloudRef | None] | None = None,
                     speakers: Mapping[int, str] | None = None,
                     joins: Mapping[int, str] | None = None,
                     extra: Mapping[int, Sequence[str]] | None = None) -> dict[int, list[str]]:
    """Reasons per suspicious cue: the ASR disagreement, the lexicon hit, the CPA full-text review's reason, an ASR
    segment that BCUT may have missed or another segment inside the cue (evidence_attach), a danmaku the cue seems
    to read aloud, and a repeat at the join with the previous cue (normalize.join_repeat: decided by CPA, never
    removed by a rule)."""
    out: dict[int, list[str]] = {}
    for cue_id in cue_ids:
        reasons = []
        if asr.get(cue_id):
            reasons.append("BCUT 与剪映不一致")
        if lexicon.get(cue_id):
            reasons.append("对照表命中：" + "、".join(lexicon[cue_id]))
        if review.get(cue_id):
            reasons.append("CPA 审读：" + review[cue_id])
        for note in (asr_notes or {}).get(cue_id) or []:
            reasons.append("ASR 挂接：" + note)
        hit = (read_aloud or {}).get(cue_id)
        if hit:
            reasons.append(f"疑似念弹幕：原文「{hit['text']}」")
        if (speakers or {}).get(cue_id):
            reasons.append("声纹显示句内可能有两人说话")
        if (joins or {}).get(cue_id):
            reasons.append("相接处重复：" + str(cast("Mapping[int, str]", joins)[cue_id]))
        reasons.extend((extra or {}).get(cue_id) or [])   # TASK-28: one_side_empty, near names, unknown letters
        if reasons:
            out[cue_id] = reasons
    return out


def is_settled(decided: doubt_policy.Decision) -> bool:
    """A doubt is settled when CPA chose a text with high confidence, or decided the sentence is not subtitled at all
    (TASK-32: a drop is a decision, not an open doubt).  Anything else stays open for the next level."""
    if str(decided.get("confidence") or "") == "低":
        return False
    if decided.get("state") not in ("KEEP", "ADOPT", "FIT_CONTEXT"):
        return False
    return bool(decided.get("dropped")) or bool(str(decided.get("final_text") or "").strip())


def heard_by(readings: Mapping[str, str], slugs: Sequence[str]) -> bool:
    """The doubt's readings include a non-empty listening reading from one of `slugs` (audio:<slug>[@...])."""
    return any(key.startswith("audio:") and key[len("audio:"):].split("@", 1)[0] in slugs
               and doubt_policy.sim_key(str(text)) for key, text in readings.items())


def unheard_change(decided: doubt_policy.Decision, current: str, readings: Mapping[str, str]) -> bool:
    """CPA's decision changes the current text in content (content_compare.differs_in_content: punctuation, fillers,
    number spelling, 他/她/它 and the small words are no change) while no Gemini listening reading of the doubt exists
    (COACH e05 §2: a change without a 听写 is not taken as it stands; it is listened to first)."""
    final = str(decided.get("final_text") or "")
    return (decided.get("state") in ("ADOPT", "FIT_CONTEXT") and bool(final.strip())
            and content_compare.differs_in_content(current, final)
            and not heard_by(readings, sorted(doubt_policy.GEMINI_SLUGS)))


@dataclass
class ReviewResult:
    suspicious: dict[int, str]
    keep_together: dict[int, str]


def parse_review(text: str) -> ReviewResult:
    """Parse one full-text review, with an optional request to retain whole cues. Tolerant to fences."""
    import json

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("review has no JSON object")
    data = json.loads(text[start:end + 1])
    out: dict[int, str] = {}
    for row in data.get("suspicious") or []:
        if isinstance(row, dict) and str(row.get("cue_id", "")).strip():
            out[int(row["cue_id"])] = re.sub(r"\s+", " ", str(row.get("reason") or "")).strip() or "可疑"
    rows = data.get("keep_together", [])
    if not isinstance(rows, list):
        raise ValueError("keep_together must be a list")
    keep: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("keep_together row must be an object")
        cue_id, reason = row.get("cue_id"), row.get("reason")
        if (isinstance(cue_id, bool) or not isinstance(cue_id, (int, str))
                or (isinstance(cue_id, str) and not re.fullmatch(r"[+-]?\d+", cue_id.strip()))):
            raise ValueError("keep_together cue_id must be an integer")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("keep_together reason must be a nonempty string")
        keep[int(cue_id)] = re.sub(r"\s+", " ", reason).strip()
    return ReviewResult(suspicious=out, keep_together=keep)
