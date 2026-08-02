import json
from pathlib import Path

import pytest

from src.autoslice.chat_authority import ChatEvidence
from src.autoslice.clip_context import (
    ClipContextError,
    build_clip_context,
    clip_context_prompt_text,
    validate_clip_context,
)
from src.autoslice.speech_memory_ledger import load_scoped_speech_memory
from src.autoslice.story_contract import build_story_contract


REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_LEDGER = REPO_ROOT / "assets/lidousha/speech_memory_ledger.v1.json"


def test_speech_memory_is_candidate_scoped_and_never_mutation_authority():
    selected = load_scoped_speech_memory(
        MEMORY_LEDGER,
        speaker_id="lidousha",
        channel_id="lidousha",
        recording_date="2026-07-22",
        candidate_id="auto_193450_3573_3665r7",
        relation_id="20260722-lidousha-nancho-live-collaboration",
    )
    ids = {row["memory_id"] for row in selected["entries"]}
    assert "lidousha.idiolect.xiaodeba.r1" in ids
    assert "20260722.sumi.jieganmei.r1" in ids
    assert "20260722.hotpot.luyisheng.r1" not in ids
    assert selected["mutation_authorized"] is False
    assert selected["status"] == "PASS"

    solo = load_scoped_speech_memory(
        MEMORY_LEDGER,
        speaker_id="lidousha",
        channel_id="lidousha",
        recording_date="2026-07-22",
        candidate_id="auto_193450_3573_3665",
        relation_id=None,
    )
    solo_ids = {row["memory_id"] for row in solo["entries"]}
    assert "lidousha.idiolect.xiaodeba.r1" in solo_ids
    assert "20260722.sumi.jieganmei.r1" not in solo_ids


def test_clip_context_binds_whole_clip_chat_topic_and_memory():
    source_sha = "sha256:" + "1" * 64
    context = build_clip_context(
        candidate_id="auto_193450_3573_3665",
        spec={
            "date": "2026-07-22",
            "selection_hook": "李豆沙展示金发有角妹妹",
            "pieces": [
                {
                    "start_ms": 3_573_070,
                    "end_ms": 3_666_350,
                    "remote_media": "/recordings/official.mp4",
                    "source_media_sha256": source_sha,
                }
            ],
            "session_relation_authority": {
                "state": "CONFIRMED",
                "relation_id": "20260722-lidousha-nancho-live-collaboration",
            },
        },
        draft_srt="1\n00:00:00,000 --> 00:00:01,000\n姐感妹\n\n",
        authoritative_chat=[
            ChatEvidence(
                "superchat",
                500,
                "她叫秦吗",
                "viewer",
                "chat.jsonl",
                "sha256:" + "2" * 64,
                "event-1",
            )
        ],
        topic_resolution={"status": "RESOLVED", "selected_topic_ids": ["game"]},
        session_topic_authorities=(),
        speech_memory_ledger_path=MEMORY_LEDGER,
    )

    assert context["schema_version"] == "lidousha-clip-context.v1"
    assert str(context["context_sha256"]).startswith("sha256:")
    assert context["pieces"][0]["source_media_sha256"] == source_sha
    assert context["structured_chat"][0]["source_event_id"] == "event-1"
    prompt = clip_context_prompt_text(context)
    assert context["context_sha256"] in prompt
    assert "姐感妹" in prompt
    assert "候选" in prompt

    contract = build_story_contract(
        candidate_id="auto_193450_3573_3665",
        selection_hook="李豆沙展示金发有角妹妹",
        transcript_text="姐感妹",
        selection_scorecard=None,
        session_relation_authority=None,
        source_media_sha256s=[source_sha],
        clip_context=context,
    )
    assert contract["clip_context_binding"]["context_sha256"] == context["context_sha256"]
    assert contract["clip_context_binding"]["mutation_authorized"] is False


def test_committed_speech_memory_is_valid_json_and_revisioned():
    document = json.loads(MEMORY_LEDGER.read_text(encoding="utf-8"))
    assert document["schema_version"] == "lidousha-speech-memory-ledger.v1"
    ids = [row["memory_id"] for row in document["entries"]]
    assert len(ids) == len(set(ids))
    assert all(row["action"] == "CANDIDATE_ONLY" for row in document["entries"])


def test_clip_context_rejects_payload_candidate_date_and_source_drift():
    source_sha = "sha256:" + "3" * 64
    context = build_clip_context(
        candidate_id="candidate",
        spec={
            "date": "2026-07-22",
            "selection_hook": "原始钩子",
            "pieces": [
                {
                    "start_ms": 0,
                    "end_ms": 2_000,
                    "remote_media": "/recordings/source.mp4",
                    "source_media_sha256": source_sha,
                }
            ],
        },
        draft_srt="1\n00:00:00,000 --> 00:00:01,000\n原文\n",
        authoritative_chat=(),
        topic_resolution={"status": "NO_GRAPH"},
        session_topic_authorities=(),
        speech_memory_ledger_path=MEMORY_LEDGER,
    )
    validate_clip_context(
        context,
        candidate_id="candidate",
        recording_date="2026-07-22",
        source_media_sha256s=[source_sha],
    )

    with pytest.raises(ClipContextError, match="DIGEST_MISMATCH"):
        validate_clip_context({**context, "selection_hook": "被篡改"})
    with pytest.raises(ClipContextError, match="CANDIDATE_MISMATCH"):
        validate_clip_context(context, candidate_id="other")
    with pytest.raises(ClipContextError, match="DATE_MISMATCH"):
        validate_clip_context(context, recording_date="2026-07-21")
    with pytest.raises(ClipContextError, match="SOURCE_BINDING_MISMATCH"):
        validate_clip_context(
            context, source_media_sha256s=["sha256:" + "4" * 64]
        )


def test_prompt_preserves_mandatory_authorities_middle_callback_and_sc():
    source_sha = "sha256:" + "5" * 64
    cues = []
    for index in range(1, 122):
        text = f"第{index}句" + ("很长的上下文" * 20)
        if index == 61:
            text = "MID_CALLBACK_这是姐感的妹妹"
        if index == 121:
            text = "TAIL_CALLBACK_男的咋了这个直播间就是不熟啊"
        cues.append(
            f"{index}\n00:00:00,000 --> 00:00:01,000\n{text}"
        )
    chat = [
        ChatEvidence(
            "danmaku",
            index * 10,
            f"普通弹幕{index}",
            "",
            "chat.jsonl",
            "sha256:" + "6" * 64,
            f"dm-{index}",
        )
        for index in range(300)
    ]
    chat.append(
        ChatEvidence(
            "superchat",
            99_999,
            "SC_她叫秦",
            "viewer",
            "chat.jsonl",
            "sha256:" + "6" * 64,
            "sc-qin",
        )
    )
    context = build_clip_context(
        candidate_id="auto_193450_3573_3665",
        spec={
            "date": "2026-07-22",
            "selection_hook": "李豆沙展示秦并被南町nightin调侃",
            "pieces": [
                {
                    "start_ms": 0,
                    "end_ms": 200_000,
                    "remote_media": "/recordings/source.mp4",
                    "source_media_sha256": source_sha,
                }
            ],
            "session_relation_authority": {
                "state": "CONFIRMED",
                "relation_id": "20260722-lidousha-nancho-live-collaboration",
                "participants": ["lidousha", "nancho"],
            },
        },
        draft_srt="\n\n".join(cues) + "\n",
        authoritative_chat=chat,
        topic_resolution={
            "status": "RESOLVED",
            "selected_topic_ids": ["qin-character"],
        },
        session_topic_authorities=(
            {"canonical": "秦", "source": "screen_text"},
        ),
        speech_memory_ledger_path=MEMORY_LEDGER,
    )

    prompt = clip_context_prompt_text(context)

    assert len(prompt) <= 18_000
    assert "recording_date: 2026-07-22" in prompt
    assert "20260722-lidousha-nancho-live-collaboration" in prompt
    assert "qin-character" in prompt
    assert '"canonical":"秦"' in prompt
    assert "MID_CALLBACK_这是姐感的妹妹" in prompt
    assert "TAIL_CALLBACK_男的咋了这个直播间就是不熟啊" in prompt
    assert "SC_她叫秦" in prompt
    assert context["retrieval_budget"]["structured_chat_truncated"] is True


def test_context_rejects_lossy_whole_clip_transcript_and_hash_drift():
    with pytest.raises(
        ClipContextError,
        match="WHOLE_CLIP_TRANSCRIPT_TOO_LARGE",
    ):
        build_clip_context(
            candidate_id="candidate",
            spec={
                "date": "2026-07-22",
                "pieces": [
                    {
                        "start_ms": 0,
                        "end_ms": 1_000,
                        "remote_media": "/recordings/source.mp4",
                        "source_media_sha256": "sha256:" + "7" * 64,
                    }
                ],
            },
            draft_srt="字" * 60_001,
            authoritative_chat=(),
            topic_resolution={"status": "NO_GRAPH"},
            session_topic_authorities=(),
            speech_memory_ledger_path=MEMORY_LEDGER,
        )

    context = build_clip_context(
        candidate_id="candidate",
        spec={
            "date": "2026-07-22",
            "pieces": [
                {
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "remote_media": "/recordings/source.mp4",
                    "source_media_sha256": "sha256:" + "8" * 64,
                }
            ],
        },
        draft_srt="1\n00:00:00,000 --> 00:00:01,000\n原文\n",
        authoritative_chat=(),
        topic_resolution={"status": "NO_GRAPH"},
        session_topic_authorities=(),
        speech_memory_ledger_path=MEMORY_LEDGER,
    )
    tampered = dict(context)
    tampered["whole_clip_draft_srt_sha256"] = "sha256:" + "0" * 64
    payload = dict(tampered)
    payload.pop("context_sha256")
    import hashlib

    tampered["context_sha256"] = "sha256:" + hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    with pytest.raises(ClipContextError, match="WHOLE_CLIP_TRANSCRIPT_INVALID"):
        validate_clip_context(tampered)
