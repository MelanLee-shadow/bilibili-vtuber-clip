"""维护者 审片裁定新增专名「；；」保真回归。

「；；」（读"分号分号"）是李豆沙直播间专属的模拟哭哭表情梗；SC/弹幕带它时
必须逐字进字幕，不能被当成占位乱码顿号化或丢弃。此前
``_strip_unrenderable_for_subtitle`` 会把两个以上连续分号折叠成一个逗号，
属于「弹幕不修正」铁律要拦的改写。
"""

from src.autoslice.chat_authority import (
    ChatEvidence,
    apply_authoritative_chat_evidence,
)
from src.autoslice.chat_repair import _strip_unrenderable_for_subtitle
from src.autoslice.jingting_chunker import parse_srt_cues


def _srt(*texts: str) -> str:
    blocks = [
        f"{i}\n00:00:{i * 5:02d},000 --> 00:00:{i * 5 + 4:02d},000\n{t}"
        for i, t in enumerate(texts, start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def test_double_semicolon_emote_survives_unrenderable_stripping():
    assert _strip_unrenderable_for_subtitle("小豆老公；；") == "小豆老公；；"
    assert _strip_unrenderable_for_subtitle("；；不是你老公") == "；；不是你老公"
    # Half-width ASCII form must survive the same way.
    assert _strip_unrenderable_for_subtitle("小豆老公;;") == "小豆老公;;"


def test_double_semicolon_emote_survives_a_longer_run_too():
    # Three-or-more-semicolon runs are the same crying-emote meme, not a
    # different signal; none of them get collapsed into a comma anymore.
    assert _strip_unrenderable_for_subtitle("哭哭；；；") == "哭哭；；；"


def test_genuinely_unrenderable_symbols_are_still_stripped():
    # Private-use-area / emoji-style codepoints remain out-of-band garbage;
    # only the「；；」emote itself is exempted from stripping.
    dirty = "谢谢老板\U000f0001礼物"
    cleaned = _strip_unrenderable_for_subtitle(dirty)
    assert "\U000f0001" not in cleaned
    assert "谢谢老板" in cleaned
    assert "礼物" in cleaned


def test_double_semicolon_emote_survives_the_chat_authority_apply_path():
    danmu = "小豆老公；； 不是你老公"
    source = _srt("小豆老公不是你老公", "完整收束")

    def verify(_request):
        return None

    output, audit = apply_authoritative_chat_evidence(
        source,
        [ChatEvidence("superchat", 0, danmu)],
        support_srt_texts=[_srt(danmu, "完整收束")],
        entity_verifier=verify,
    )

    assert "；；" in output, output
    texts = [cue.text for cue in parse_srt_cues(output)]
    assert texts[0] == danmu
