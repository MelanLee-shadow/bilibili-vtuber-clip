from src.autoslice.chat_authority import ChatEvidence
from src.autoslice import producer_text_pipeline


def test_guard_precontext_retains_verified_232_second_delayed_thanks(monkeypatch):
    piece_start_ms = 1_000_000
    monkeypatch.setattr(
        producer_text_pipeline,
        "_piece_chat_evidence",
        lambda _piece: [
            ChatEvidence(
                "guard",
                piece_start_ms - 232_140,
                "舰长",
                "panoja",
                "/bound/chat.jsonl",
                "a" * 64,
                "42",
            ),
            ChatEvidence(
                "guard",
                piece_start_ms - 300_001,
                "舰长",
                "too-early",
                "/bound/chat.jsonl",
                "a" * 64,
                "43",
            ),
        ],
    )

    merged, authoritative = producer_text_pipeline._collect_timeline_chat(
        {
            "pieces": [
                {
                    "remote_media": "/recordings/source.mp4",
                    "start_ms": piece_start_ms,
                    "end_ms": piece_start_ms + 10_000,
                }
            ]
        },
        [10_000],
    )

    assert [item.sender for item in authoritative] == ["panoja"]
    assert authoritative[0].offset_ms == -232_140
    assert [item.text for item in merged] == ["【上舰此前·panoja】舰长"]
