"""who_said_inputs: a guest who is also a channel of this project gets her own persona on her card."""

from src.autoslice.manual_lane import who_said_inputs


def test_a_guest_with_a_channel_profile_has_her_persona_and_one_without_has_none():
    sumi = who_said_inputs.persona_of(["礼墨Sumi", "Sumi", "礼墨"])
    assert "## 身份" in sumi and "粉丝文化与性格" in sumi
    assert "封面" not in "\n".join(line for line in sumi.splitlines() if line.startswith("## "))
    assert who_said_inputs.persona_of(["安晚awa", "awa"]) == ""
