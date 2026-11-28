"""Two guests shown apart, each in her own colour (the channel's request): step 4 names the guest lines (speaker/persons.json, the record
step 6 reads too), step 5 burns a named guest's lines in the guest style with her outline, a line not named keeps the
guest style, and the package's ASS check takes her style for a guest's."""

from __future__ import annotations

import json

import pytest
from test_guest_identity import _clip, _owner

from src.autoslice import subtitle_rendering as render
from src.autoslice.manual_lane import person_colours, step6_title
from src.autoslice.review_package_ass_audit import audit_review_package_ass

REGISTRY = {"schema_version": person_colours.SCHEMA, "policy": {}, "missing": [], "people": [
    {"name": "礼墨Sumi", "id": "sumi", "outline": "#C8901A"},
    {"name": "安晚awa", "id": "awa", "outline": "#9C4E64"}]}
SRT = ("1\n00:00:00,000 --> 00:00:01,800\n[李豆沙] 果然awawa就是这种幕后黑手的人。\n\n"
       "2\n00:00:02,000 --> 00:00:03,800\n[连线] 我我我不是，不要瞎说。\n\n"
       "3\n00:00:04,000 --> 00:00:05,800\n[连线] 好好可怕。\n\n"
       "4\n00:00:06,000 --> 00:00:07,800\n[连线] 这个是什么必杀技吗？\n")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "person_colours.v1.json"
    path.write_text(json.dumps(REGISTRY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(person_colours, "REGISTRY", path)
    return path


def _events(ass):
    return [line.split(",", 9) for line in ass.read_text(encoding="utf-8").splitlines() if line.startswith("Dialogue:")]


def _audit(srt, ass):
    """The step 8 check (finished_media_review A3): the package's ASS against its speaker.srt."""
    return audit_review_package_ass(root=srt.parent, item={"ass_path": ass.name, "speaker_srt": srt.name},
                                    portable_required=False, max_visual_lines=2, max_visual_line_chars=28)


def test_a_named_guest_is_burned_in_her_own_outline_and_the_package_check_takes_it(tmp_path, registry):
    from scripts.apply_speaker_turn_overrides import channel_burn_styles

    guest = person_colours.channel_guest_style()
    assert guest == channel_burn_styles()["guest"]
    known = person_colours.people()
    sumi, awa = person_colours.guest_style("礼墨Sumi", guest, known), person_colours.guest_style("安晚awa", guest, known)
    assert person_colours.guest_style("无前", guest, known) is None             # no colour of her own: the guest style
    assert sumi[0] == "GUEST-sumi" and sumi[1].split(",")[4] == "&H001A90C8" and awa[1].split(",")[4] == "&H00644E9C"
    # her gold is light against the white letters (2.81 < 3): an outline one pixel wider over an opaque black shadow;
    # 安晚awa's dark red keeps the guest style's outline and shadow
    assert sumi[1].split(",")[5] == "&H00000000" and float(sumi[1].split(",")[15]) == float(guest.split(",")[15]) + 1
    assert awa[1].split(",")[5] == guest.split(",")[5] and awa[1].split(",")[15] == guest.split(",")[15]
    srt, ass = tmp_path / "speaker.srt", tmp_path / "final.ass"
    srt.write_text(SRT, encoding="utf-8")
    render._write_sapphire_ass_from_srt(srt, ass, person_styles=dict([sumi, awa]),
                                                 cue_styles=[None, "GUEST-sumi", None, "GUEST-awa"])
    text = ass.read_text(encoding="utf-8")
    assert f"Style: GUEST-sumi,{sumi[1]}" in text and f"Style: GUEST-awa,{awa[1]}" in text
    assert [e[3] for e in _events(ass)] == ["LDS", "GUEST-sumi", "GUEST", "GUEST-awa"]
    assert not _audit(srt, ass).issues
    with pytest.raises(ValueError):                          # a host line is the host's, never a guest's colour
        render._write_sapphire_ass_from_srt(srt, ass, person_styles=dict([sumi]),
                                                     cue_styles=["GUEST-sumi", None, None, None])
    # a guest line in the host's style is still caught
    ass.write_text(text.replace("Dialogue: 0,0:00:02.00,0:00:03.80,GUEST-sumi", "Dialogue: 0,0:00:02.00,0:00:03.80,LDS"),
                   encoding="utf-8")
    assert "SUBTITLE_ASS_SPEAKER_SRT_STYLE_MISMATCH" in {i.code for i in _audit(srt, ass).issues}


def test_an_outline_that_does_not_read_on_white_stops_the_burn(tmp_path, monkeypatch):
    path = tmp_path / "person_colours.v1.json"
    path.write_text(json.dumps({**REGISTRY, "people": [{"name": "礼墨Sumi", "id": "sumi", "outline": "#F0F0E0"}]}),
                    encoding="utf-8")
    monkeypatch.setattr(person_colours, "REGISTRY", path)
    with pytest.raises(ValueError, match="看不清"):
        person_colours.guest_style("礼墨Sumi", person_colours.channel_guest_style(), person_colours.people())


def test_step_4_names_the_guest_lines_once_and_step_6_reads_the_same_names(tmp_path):
    from src.autoslice.manual_lane.step4_speaker import PERSONS, _name_persons

    ctx, _ = _clip(tmp_path, 'p')
    _owner(ctx, {2: "安晚awa", 3: "安晚awa", 4: "礼墨Sumi", 6: "礼墨Sumi"})
    calls, line = _name_persons(ctx)
    rows = json.loads((ctx.clip_dir / PERSONS).read_text(encoding="utf-8"))
    assert [r["person"] for r in rows] == ["李豆沙", "安晚awa", "安晚awa", "礼墨Sumi", "李豆沙", "礼墨Sumi"]
    assert calls == [] and "定到人的 4 句" in line
    named, asked = step6_title._identify_guests(ctx)       # the same input: the record step 4 kept, nothing asked again
    assert named == {2: "安晚awa", 3: "安晚awa", 4: "礼墨Sumi", 6: "礼墨Sumi"} and asked == []


def test_one_named_guest_and_nobody_unnamed_are_all_her_lines(tmp_path):
    from src.autoslice.manual_lane.step4_speaker import PERSONS, _name_persons

    ctx, _ = _clip(tmp_path, 'q', guests=(["安晚awa"],))
    _calls, line = _name_persons(ctx)
    rows = json.loads((ctx.clip_dir / PERSONS).read_text(encoding="utf-8"))
    assert [r["person"] for r in rows if r["speaker"] == "连线"] == ["安晚awa"] * 4 and "嘉宾句都是安晚awa" in line


def test_step_5_burns_each_named_guest_in_her_style_and_an_unnamed_line_in_the_guest_style(tmp_path, registry):
    from fakes import FakeCpa, FakeRemote, make_ctx
    from src.autoslice.manual_lane.adapters import RemoteResult
    from src.autoslice.manual_lane.step4_speaker import PERSONS
    from src.autoslice.manual_lane.step5_burn import step5_burn

    def run(name, body, remote):
        if name == "s5-burn":
            remote.files["/r/clip/burn/intro_result.json"] = json.dumps(
                {"status": "PREPENDED", "intro_id": "A", "intro_offset_ms": 1000}).encode()
        return RemoteResult(0, "", "")

    remote = FakeRemote(on_script=run)
    ctx = make_ctx(tmp_path, remote=remote, cpa=FakeCpa([]))
    (ctx.clip_dir / "speaker").mkdir(parents=True)
    (ctx.clip_dir / "speaker/speaker.srt").write_text(SRT, encoding="utf-8")
    # line 3 is on the path the two guests share: "礼墨Sumi或安晚awa" is nobody's name, so nobody's colour
    (ctx.clip_dir / PERSONS).write_text(json.dumps([
        {"start_s": 0.0, "end_s": 1.8, "speaker": "李豆沙", "person": "李豆沙"},
        {"start_s": 2.0, "end_s": 3.8, "speaker": "连线", "person": "礼墨Sumi"},
        {"start_s": 4.0, "end_s": 5.8, "speaker": "连线", "person": None},
        {"start_s": 6.0, "end_s": 7.8, "speaker": "连线", "person": "安晚awa"}], ensure_ascii=False), encoding="utf-8")
    out = step5_burn(ctx)
    assert out.ok
    sent = json.loads(remote.files["/r/clip/burn/speaker.persons.json"])
    assert sent["cues"] == [None, "GUEST-sumi", None, "GUEST-awa"] and set(sent["styles"]) == {"GUEST-sumi", "GUEST-awa"}
    assert "render_ass.py /r/clip/burn/speaker.srt /r/clip/burn/final.ass" in remote.scripts[-1][1]
    assert "/r/clip/burn/speaker.persons.json" in remote.scripts[-1][1]
    assert any("礼墨Sumi 1 句 #C8901A" in p and "1 句嘉宾句按通用嘉宾样式" in p for p in out.process)
    # what render_ass.py does with it on the host
    srt, ass = tmp_path / "speaker.srt", tmp_path / "final.ass"
    srt.write_text(SRT, encoding="utf-8")
    render._write_sapphire_ass_from_srt(srt, ass, person_styles=sent["styles"], cue_styles=sent["cues"])
    assert [e[3] for e in _events(ass)] == ["LDS", "GUEST-sumi", "GUEST", "GUEST-awa"]


def test_without_the_registry_every_guest_keeps_the_guest_style(tmp_path, monkeypatch):
    monkeypatch.setattr(person_colours, "REGISTRY", tmp_path / "absent.json")    # the public export strips it
    assert person_colours.people() == {}
