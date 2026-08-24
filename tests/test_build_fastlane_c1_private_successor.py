from scripts.build_fastlane_c1_private_successor import (
    load_authority,
    parse_srt,
    project_ass,
    project_cues,
    render_srt,
    validate_projection,
)


def _source_srt() -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},500\ncue-{index}"
        for index in range(1, 37)
    ) + "\n"


def test_c1_authority_partitions_the_complete_source_grid() -> None:
    authority = load_authority()
    classified = authority["cue_classification"]
    assert set(classified["live_lidousha_cues"]) | set(classified["watched_video_cues"]) == set(range(1, 37))
    assert not set(classified["live_lidousha_cues"]) & set(classified["watched_video_cues"])
    assert authority["future_delivery_constraint"] == {
        "requires_same_bv_repair": True,
        "forbid_new_bv": True,
        "preserve_public_identity_and_metadata": True,
    }


def test_c1_projection_preserves_every_live_cue_and_only_drops_watched_video() -> None:
    authority = load_authority()
    source = parse_srt(_source_srt())
    projected = project_cues(source, authority)
    validate_projection(source, projected, authority)
    text = render_srt(projected)
    assert "薇欧拉酱只是喜欢阿拉蕾酱而已" in text
    assert "主包给练舞室的姐姐们推荐" in text
    assert "daisukino ararei chan" in text
    assert "00:01:31,100 --> 00:01:31,900\n来了" in text
    for index in authority["cue_classification"]["watched_video_cues"]:
        assert f"cue-{index}" not in text


def test_c1_ass_projection_removes_watched_dialogue_and_inserts_live_reaction() -> None:
    authority = load_authority()
    source = parse_srt(_source_srt())
    projected = project_cues(source, authority)
    ass = "\n".join(
        f"Dialogue: 0,0:00:{index:02d}.00,0:00:{index:02d}.50,Default,,0,0,0,,cue-{index}"
        for index in range(1, 37)
    ) + "\n"
    result = project_ass(ass, source, projected)
    assert "cue-9" not in result
    assert "薇欧拉酱只是喜欢阿拉蕾酱而已" in result
    assert "0:01:31.10,0:01:31.90,Default,,0,0,0,,来了" in result
    assert result.index("cue-19") < result.index("来了") < result.index("cue-20")
