"""Blur boxes never reach the host (subtract face and body), CPA-read boxes are used when given, and title candidates
that all fail self-check get two regeneration rounds, then the highest-scoring one is taken with its failed items
recorded."""

from __future__ import annotations

import json

from fakes import UNDERSTANDING_CALLS, FakeCpa, FakeRemote, fake_cover, make_ctx, srt

from src.autoslice.manual_lane import stage_impl as si
from src.autoslice.manual_lane.adapters import RemoteResult

RD = "/r/clip"
NAMES = ["李豆沙", "雾深Girimi"]
FACE = [560, 150, 1360, 850]


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_subtract_rect_cuts_the_hole_out_and_keeps_the_rest():
    assert si.subtract_rect([1650, 700, 1830, 1060], [1200, 0, 1700, 1080]) == [[1700, 700, 1830, 1060]]
    assert si.subtract_rect([0, 0, 100, 100], [200, 200, 300, 300]) == [[0, 0, 100, 100]]   # no overlap
    pieces = si.subtract_rect([0, 0, 100, 100], [40, 40, 60, 60])                          # hole inside
    assert len(pieces) == 4 and all(not _overlaps(p, [40, 40, 60, 60]) for p in pieces)


def test_default_blur_is_the_fixed_strip_minus_the_host_body_and_face():
    boxes = si.blur_boxes(None, None, FACE)
    assert boxes == [[1700, 700, 1830, 1060]]
    assert all(not _overlaps(b, p) for b in boxes for p in [[1200, 0, 1700, 1080], FACE])


def test_cpa_boxes_are_used_and_cut_out_the_person_where_they_overlap():
    # the fixed host body (x 1200-1700) is always cut out, so the box keeps only its right side
    assert si.blur_boxes([[1640, 690, 1840, 1070]], [[1100, 100, 1500, 1080]], FACE) == [[1700, 690, 1840, 1070]]
    # where the person's body reaches into the box, only the part beside the body is blurred
    assert si.blur_boxes([[1640, 690, 1840, 1070]], [[1600, 0, 1700, 1080]], FACE) == [[1700, 690, 1840, 1070]]


def test_blur_never_overlaps_the_face_or_the_person_for_any_boxes():
    person = [[1200, 0, 1700, 1080]]
    for danmaku in ([[1600, 600, 1900, 1080]], [[500, 100, 1400, 900]], [[1650, 700, 1830, 1060]]):
        for b in si.blur_boxes(danmaku, person, FACE):
            assert not _overlaps(b, FACE) and all(not _overlaps(b, p) for p in person)


def _cand(title, *, holds=False, person="她越长大越讨厌看那种有规矩的", on_stage=("李豆沙",)):
    return {"title": title, "on_stage": list(on_stage), "punchline_person": person,
            "swap_check": {"holds_with_other_host": holds, "reason": "x"}}


def _ctx(tmp_path, answers, *, frames=None):
    def script(name, body, remote):
        if name == "s6-frame":
            if frames is not None:
                remote.files[f"{RD}/cover/frames.json"] = json.dumps(frames).encode()
            return RemoteResult(0, "", "")
        if name == "s6-cover":
            fake_cover(remote, "/r/clip")
        return RemoteResult(0, "", "")
    remote = FakeRemote(on_script=script)
    cpa = FakeCpa(answers)
    ctx = make_ctx(tmp_path, remote=remote, cpa=cpa, session={
        "recording_date": "2026-10-24", "speaker_mode": "uniform_host", "enablement_evidence": None,
        "participants": [["李豆沙", "小李"], ["雾深Girimi", "雾深"]], "own_uploader_mids": [1]})
    (ctx.clip_dir / "subs").mkdir(parents=True, exist_ok=True)
    (ctx.clip_dir / "subs" / "final.srt").write_text(srt(3), encoding="utf-8")
    return ctx, cpa, remote


def _answer(cands, recommended=0):
    return json.dumps({"candidates": cands, "recommended": recommended, "cover_lines": ["甲", "乙"]},
                      ensure_ascii=False)


def test_all_candidates_failing_self_check_get_two_regeneration_rounds_then_the_best_is_taken(tmp_path):
    bad_swap = _cand("【李豆沙】她半分钟后真的破防了", holds=True)                                # fails swap
    bad_name = _cand("【李豆沙】她和弹幕吵起来了", on_stage=("李豆沙", "雾深Girimi"))   # fails names
    answers = [_answer([bad_swap, bad_name]), _answer([bad_swap]), _answer([bad_swap])]
    ctx, cpa, _ = _ctx(tmp_path, answers, frames={"chosen": 12.0, "checked": []})
    out = si.step6_title_cover(ctx)
    assert out.ok, out.note
    assert cpa.calls[:6] == [*UNDERSTANDING_CALLS, "title", "title-regen-1", "title-regen-2"]
    assert len([c for c in cpa.calls if c == "title" or c.startswith("title-regen")]) == 3   # no fourth round
    assert "候选全部未通过自检" in "\n".join(out.process)
    cands = json.loads((ctx.clip_dir / "title" / "candidates.json").read_text(encoding="utf-8"))
    assert cands["forced"] is True and cands["rounds"] == ["title", "title-regen-1", "title-regen-2"]
    assert cands["chosen"]["checks"]["ok"] is False
    assert cands["chosen"]["checks"]["score"] == len(si.TITLE_RULES) - 1
    assert cands["chosen"]["checks"]["failed"] == ["swap"]       # the highest score (6/7), the unpassed item recorded


def test_the_regeneration_prompt_carries_the_reasons_of_the_failed_round(tmp_path):
    ctx, cpa, _ = _ctx(tmp_path, [_answer([_cand("【李豆沙】她半分钟后真的破防了", holds=True)]),
                                  _answer([_cand("【李豆沙】她半分钟后真的破防了")])], frames={"chosen": None, "checked": []})
    assert si.step6_title_cover(ctx).ok
    assert "上一轮候选没有一条通过自检" in cpa.title_prompts[1] and "换主播自检未通过" in cpa.title_prompts[1]
    assert "上一轮候选没有一条通过自检" not in cpa.title_prompts[0]


def test_a_passing_first_round_is_not_regenerated(tmp_path):
    ctx, cpa, _ = _ctx(tmp_path, [_answer([_cand("【李豆沙】她半分钟后真的破防了")])], frames={"chosen": None, "checked": []})
    out = si.step6_title_cover(ctx)
    assert out.ok and cpa.calls == [*UNDERSTANDING_CALLS, "title", "title-judge-r0-1", "title-judge-r0-2", "cover-brief"]
    cands = json.loads((ctx.clip_dir / "title" / "candidates.json").read_text(encoding="utf-8"))
    assert cands["forced"] is False


def test_cpa_person_box_on_the_other_guest_does_not_unblur_the_host_body():
    # 692: CPA's person box was the guest on the left; the host on the right must still be kept clear
    boxes = si.blur_boxes([[1650, 700, 1830, 1080]], [[35, 178, 710, 910]], FACE)
    assert boxes == [[1700, 700, 1830, 1080]]
    assert all(not _overlaps(b, [1200, 0, 1700, 1080]) for b in boxes)


def test_text_may_not_cover_the_other_people_but_may_cover_the_host(tmp_path):
    # the renderer's keep boxes are where text may not go: the other guest CPA saw, never the host's fixed body
    from src.autoslice.manual_lane import stage_impl as si2
    ctx, _, remote = _ctx(tmp_path, [_answer([_cand("【李豆沙】她半分钟后真的破防了")])], frames={
        "chosen": 12.0, "checked": [], "boxes": {"danmaku": [], "person": [[1256, 298, 1755, 909]],
                                                 "guest": [[35, 178, 710, 910]]}})
    assert si2.step6_title_cover(ctx).ok
    spec = json.loads(remote.files[f"{RD}/cover/spec.json"].decode("utf-8"))
    assert spec["keep"] == [[35, 178, 710, 910]]
    assert [1200, 0, 1700, 1080] not in spec["keep"]


def test_the_host_box_CPA_returned_is_not_a_keep_box(tmp_path):
    # 15_229: CPA returned the host's box (person) and the guest's: text may cover the host, not the guest
    from src.autoslice.manual_lane import stage_impl as si2
    ctx, _, remote = _ctx(tmp_path, [_answer([_cand("【李豆沙】她半分钟后真的破防了")])], frames={
        "chosen": 12.0, "checked": [], "boxes": {"danmaku": [[1655, 700, 1920, 1080]],
                                                 "person": [[1256, 298, 1755, 909]], "guest": [[35, 177, 717, 910]]}})
    assert si2.step6_title_cover(ctx).ok
    spec = json.loads(remote.files[f"{RD}/cover/spec.json"].decode("utf-8"))
    assert spec["keep"] == [[35, 177, 717, 910]]
