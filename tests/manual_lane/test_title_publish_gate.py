"""Step 6's title self-check runs the publish gate (P1, coordinator): the same functions as the upload path, not a second
list.  A title the gate rejects fails step 6 with the gate's own codes; a title it passes gets no publish_gate reason;
and for the same title step 6 and step 8 reach the same conclusion."""

from __future__ import annotations

from fakes import srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.publication_title_exception import candidate_title_policy_violations
from src.autoslice.title_policy import canonicalize_publish_title

NAMES = ("李豆沙", "雾深Girimi")
GOOD = "【李豆沙】她半分钟后真的破防了"
TOO_LONG = "【李豆沙】" + "很长很长的一句话" * 8     # 64 characters of body: over the 49-character cap


def _cand(title, *, on_stage=("李豆沙",)):
    return {"title": title, "on_stage": list(on_stage), "punchline_person": "她",
            "swap_check": {"holds_with_other_host": False, "reason": "换主播不成立"}}


def _step8(title):
    """The upload path's side: candidate_title_policy_violations (the surface check needs a candidate id, step 6 has
    none, so it is dropped on both sides), plus the canonical envelope check final_human_review applies."""
    codes = candidate_title_policy_violations(candidate_id="x", title=title, lane="talk", source_fact_review=None)
    codes = [c for c in codes if c != "candidate_public_or_reviewed_title_surface_conflict"]
    if canonicalize_publish_title(title, lane="talk") != title.strip():
        codes.append("publish_title_not_canonical")
    return sorted(set(codes))


def test_a_title_the_gate_rejects_fails_step_six_with_the_gates_own_codes():
    checks = si.title_checks(_cand(TOO_LONG), NAMES, srt(3))
    assert not checks["ok"] and "publish_gate" in checks["failed"]
    assert "publish_title_length_out_of_bounds" in checks["reasons"]
    assert checks["score"] == len(si.TITLE_RULES) - len(checks["failed"])


def test_a_title_the_gate_passes_gets_no_publish_gate_reason():
    checks = si.title_checks(_cand(GOOD), NAMES, srt(3))
    assert "publish_gate" not in checks["failed"]
    assert not any(r.startswith("publish_") for r in checks["reasons"])
    assert checks["ok"] is True and checks["score"] == len(si.TITLE_RULES)


def test_step_six_and_step_eight_reach_the_same_conclusion_on_the_same_title():
    titles = [GOOD, TOO_LONG, "【李豆沙】短", "【李豆沙×雾深Girimi】刚说不多赘述，聊恨海情天却聊到「好难过好难过，但是好爽」",
              "【李豆沙×无名人】这段话的主角不在名单里，标题却写了她的名字"]
    for title in titles:
        six = sorted(set(si.publish_gate_violations(title)))
        eight = _step8(title)
        assert six == eight, (title, six, eight)


def test_the_gate_codes_are_not_a_copy_of_the_rules():
    # the gate is called, so a code the gate returns appears verbatim; nothing is renamed or invented here
    codes = si.publish_gate_violations(TOO_LONG)
    assert codes and all(not c.startswith("publish_gate_error") for c in codes)
    assert "publish_title_length_out_of_bounds" in codes
