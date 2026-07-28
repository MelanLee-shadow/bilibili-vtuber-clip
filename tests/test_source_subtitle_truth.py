from copy import deepcopy
import json
from pathlib import Path

import pytest

from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.producer_text_finalization import (
    verify_chat_authority_final_surfaces,
)
from src.autoslice.source_subtitle_truth import (
    apply_source_subtitle_truth,
    build_source_truth_preview_receipt,
    ledger_required_owner_contracts,
)
from src.autoslice.subtitle_fidelity import resolve_deferred_foreign_introductions


REPO_ROOT = Path(__file__).resolve().parents[1]


def _srt(*rows: tuple[int, int, str]) -> str:
    blocks = []
    for index, (start, end, text) in enumerate(rows, start=1):
        blocks.append(
            f"{index}\n00:00:{start:02d},000 --> 00:00:{end:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _srt_ms(*rows: tuple[int, int, str]) -> str:
    def timestamp(value: int) -> str:
        seconds, millis = divmod(value, 1_000)
        return f"00:00:{seconds:02d},{millis:03d}"

    blocks = []
    for index, (start, end, text) in enumerate(rows, start=1):
        blocks.append(
            f"{index}\n{timestamp(start)} --> {timestamp(end)}\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _ledger(tmp_path, entries, *, source_aliases=None):
    path = tmp_path / "truth.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "source_aliases": source_aliases or [],
                "entries": entries,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_source_alias_requires_exact_piece_hash_and_projects_offset(tmp_path):
    alias_sha256 = "sha256:" + "a" * 64
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "official-replay-alias",
                "recording_basename": "canonical.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "审定文本",
                "required": True,
            }
        ],
        source_aliases=[
            {
                "alias_id": "official-replay-v1",
                "alias_recording_basename": "replay.mp4",
                "alias_source_sha256": alias_sha256,
                "canonical_recording_basename": "canonical.mp4",
                "alias_timeline_offset_ms": 2_000,
                "authority": "exact media hash plus reviewed audio alignment",
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((10_000, 14_000, "误听文本")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/replay.mp4",
                    "source_media_sha256": alias_sha256,
                    # Alias interval 112000..116000 maps to canonical
                    # interval 110000..114000.
                    "start_ms": 102_000,
                    "end_ms": 122_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert "审定文本" in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["source_aliases"] == [
        {
            "alias_id": "official-replay-v1",
            "alias_recording_basename": "replay.mp4",
            "alias_source_sha256": alias_sha256,
            "alias_timeline_offset_ms": 2_000,
        }
    ]
    assert audit["applied"][0]["declared_output_contract"] == {
        "schema_version": "source-truth-declared-output.v1",
        "action": "replace_cue",
        "canonical_texts": ["审定文本"],
        "required_text": "",
    }


def test_real_source_truth_audit_row_positively_witnesses_kana_name(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "structured-sc-sender",
                "revision_id": "r1",
                "assertion_state": "VERIFIED_ACTIVE",
                "recording_basename": "recording.mp4",
                "source_start_ms": 10_000,
                "source_end_ms": 14_000,
                "action": "replace_cue",
                "text": "谢谢小凑るう子的钢镚",
                "evidence_class": "HASH_BOUND_STRUCTURED_SUPERCHAT_SENDER",
                "authority": "hash-bound structured SC sender",
                "required": True,
            }
        ],
    )
    corrected, truth_audit = apply_source_subtitle_truth(
        _srt((10, 14, "谢谢小路路口的钢棒")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 20_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    resolution = resolve_deferred_foreign_introductions(
        {
            "unproven_foreign_introductions": [
                {
                    "cue_index": 1,
                    "start_ms": 10_000,
                    "end_ms": 14_000,
                    "draft": "谢谢小路路口的钢棒",
                    "attempted": "谢谢小凑るう子的钢镚",
                }
            ]
        },
        corrected,
        authority_rows=[
            *(truth_audit.get("applied") or []),
            *(truth_audit.get("satisfied") or []),
        ],
        authority_kind="source_subtitle_truth",
    )

    assert truth_audit["status"] == "APPLIED"
    assert resolution["status"] == "PASS"
    assert resolution["findings"][0]["positive_witness_authority_ids"] == [
        "structured-sc-sender"
    ]


@pytest.mark.parametrize(
    ("piece_hash", "reason"),
    [
        (None, "SOURCE_SUBTITLE_TRUTH_ALIAS_PIECE_SHA256_MISSING"),
        (
            "sha256:" + "b" * 64,
            "SOURCE_SUBTITLE_TRUTH_ALIAS_PIECE_SHA256_MISMATCH",
        ),
    ],
)
def test_source_alias_fails_closed_without_exact_piece_hash(
    tmp_path, piece_hash, reason
):
    alias_sha256 = "sha256:" + "a" * 64
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "bound-alias",
                "recording_basename": "canonical.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "审定文本",
                "required": True,
            }
        ],
        source_aliases=[
            {
                "alias_id": "official-replay-v1",
                "alias_recording_basename": "replay.mp4",
                "alias_source_sha256": alias_sha256,
                "canonical_recording_basename": "canonical.mp4",
                "alias_timeline_offset_ms": 0,
                "authority": "reviewed evidence",
            }
        ],
    )
    piece = {
        "remote_media": "/source/replay.mp4",
        "start_ms": 100_000,
        "end_ms": 120_000,
    }
    if piece_hash is not None:
        piece["source_media_sha256"] = piece_hash

    with pytest.raises(RuntimeError, match=reason):
        apply_source_subtitle_truth(
            _srt_ms((10_000, 14_000, "误听文本")),
            spec={"pieces": [piece]},
            durations=[20_000],
            ledger_path=ledger,
        )


def test_source_interval_truth_applies_to_every_overlapping_candidate(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "same-source",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "kmx在线下叫李豆沙",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    for piece_start, cue_start, cue_end in (
        (100_000, 10, 14),
        (105_000, 5, 9),
    ):
        corrected, audit = apply_source_subtitle_truth(
            _srt((cue_start, cue_end, "提莫怂在线下叫李豆莎")),
            spec={
                "pieces": [
                    {
                        "remote_media": "/source/recording.mp4",
                        "start_ms": piece_start,
                        "end_ms": piece_start + 20_000,
                    }
                ]
            },
            durations=[20_000],
            ledger_path=ledger,
        )
        assert "kmx在线下叫李豆沙" in corrected
        assert audit["status"] == "APPLIED"


def test_replace_cue_majority_gate_excludes_grazing_neighbor(tmp_path):
    """1573 r12 实案几何：真值窗尾带落值轮网格坐标，本轮句尾早移 190ms，
    窗尾以 ≥80ms 绝对门擦进邻句——邻句整条曾被纳入 projection，终验
    aggregate 永远多一句。replace 目标 cue 须过半重叠：11% 擦入的邻句
    出局，99% 重叠的真 owner 保留。"""
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "grazing-neighbor",
                "recording_basename": "recording.mp4",
                "source_start_ms": 10_010,
                "source_end_ms": 13_270,
                "action": "replace_cue",
                "text": "但其实背地里是被欺负的那种",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (10_000, 13_080, "但其实背弟里是被欺负的那种"),
            (13_080, 14_740, "那我不是一直都是这样的吗"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 20_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert "但其实背地里是被欺负的那种" in corrected
    assert "那我不是一直都是这样的吗" in corrected
    row = next(
        r
        for r in audit["applied"]
        if r.get("truth_id") == "grazing-neighbor"
    )
    projection = row["resolved_target_projection"]
    assert len(projection["cues"]) == 1
    assert projection["cues"][0]["after_text"] == "但其实背地里是被欺负的那种"


def test_replace_cue_can_pin_reviewed_spoken_start_after_silent_prefix(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "silent-hallucinated-prefix",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "乱说的啊",
                "spoken_start_ms": 111_500,
                "authority": "Ivan plus bounded raw-audio onset check",
                "required": True,
            }
        ],
    )
    source = _srt_ms((10_000, 14_000, "我草，乱说的啊"))
    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert "00:00:11,500 --> 00:00:14,000" in corrected
    assert "乱说的啊" in corrected
    assert "我草" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["timing_pin"] == {
        "source_spoken_start_ms": 111_500,
        "before_start_ms": 10_000,
        "after_start_ms": 11_500,
    }


def test_spoken_start_pin_fails_closed_when_target_is_not_unique(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "ambiguous-onset",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "审定文本",
                "spoken_start_ms": 111_500,
                "required": True,
            }
        ],
    )
    source = _srt_ms(
        (10_000, 12_000, "第一条"),
        (12_000, 14_000, "第二条"),
    )
    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert corrected == source
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "SPOKEN_START_TARGET_NOT_UNIQUE"
    )


def test_spoken_start_pin_preserves_pre_onset_neighbor_and_moves_late_cue_earlier(
    tmp_path,
):
    """7/22 official replay: the broad silent-prefix window grazes the prior
    question, while the reviewed spoken onset belongs uniquely to the next cue.
    """

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "silent-prefix-after-question",
                "recording_basename": "recording.mp4",
                "source_start_ms": 117_320,
                "source_end_ms": 120_350,
                "action": "replace_cue",
                "text": "乱说的啊",
                "spoken_start_ms": 118_250,
                "authority": "reviewed raw-audio onset",
                "required": True,
            }
        ],
    )
    source = _srt_ms(
        (14_760, 18_080, "你为什么提到我就要最最最喜欢？草"),
        (18_440, 20_280, "乱说的啊"),
        (20_800, 23_640, "怎么到现在就是乱说的哈"),
    )
    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 130_000,
                }
            ]
        },
        durations=[30_000],
        ledger_path=ledger,
    )

    assert audit["status"] == "APPLIED"
    assert "你为什么提到我就要最最最喜欢？草" in corrected
    assert "00:00:18,250 --> 00:00:20,280\n乱说的啊" in corrected
    assert audit["applied"][0]["cue_indexes"] == [2]
    assert audit["applied"][0]["timing_pin"] == {
        "source_spoken_start_ms": 118_250,
        "before_start_ms": 18_440,
        "after_start_ms": 18_250,
    }


def test_source_interval_truth_does_not_leak_to_other_recording(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "bound-source",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "kmx",
                "required": True,
            }
        ],
    )
    original = _srt((10, 14, "提防"))
    corrected, audit = apply_source_subtitle_truth(
        original,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/other.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert corrected == original
    assert audit["status"] == "NO_RELEVANT_INTERVAL"


def test_drop_cue_requires_full_containment_and_renumbers(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "silent-hallucination",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "drop_cue",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/source/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 120_000,
            }
        ]
    }
    source = _srt((8, 10, "前一句"), (10, 14, "无声幻听"), (14, 18, "后一句"))

    corrected, audit = apply_source_subtitle_truth(
        source,
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )

    assert "无声幻听" not in corrected
    assert "1\n00:00:08,000" in corrected
    assert "2\n00:00:14,000" in corrected
    assert "3\n" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["drop_status"] == "APPLIED"

    second, second_audit = apply_source_subtitle_truth(
        corrected,
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )
    assert second == corrected
    assert second_audit["status"] == "ALREADY_SATISFIED"
    assert second_audit["satisfied"][0]["drop_status"] == "ALREADY_ABSENT"


def test_drop_cue_fails_closed_when_cue_straddles_truth_interval(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "do-not-over-delete",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "drop_cue",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    source = _srt((9, 15, "真实前半句，无声后半句"), (15, 18, "后一句"))

    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )

    assert corrected == source
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "DROP_CUE_STRADDLES_TRUTH_INTERVAL"
    )
    assert audit["failures"][0]["drop_status"] == "CONFLICT"


def test_governed_machine_consensus_cannot_self_promote(tmp_path):
    path = tmp_path / "truth.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "governance": {
                    "schema_version": "source-subtitle-truth-governance.v2",
                    "governed_entry_start_index": 0,
                },
                "source_aliases": [],
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "machine-guess",
                        "revision_id": "r1",
                        "assertion_state": "VERIFIED_ACTIVE",
                        "evidence_class": "ENGINE_PLUS_ACOUSTIC_MAJORITY",
                        "authority": "machine vote",
                        "decision_authority": "CPA_CANDIDATE_ONLY",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 110_000,
                        "source_end_ms": 114_000,
                        "action": "replace_cue",
                        "text": "机器猜测",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        RuntimeError,
        match="SOURCE_SUBTITLE_TRUTH_DIRECT_MUTATION_AUTHORITY_FORBIDDEN",
    ):
        apply_source_subtitle_truth(
            _srt((10, 14, "原文")),
            spec={
                "pieces": [
                    {
                        "remote_media": "/source/recording.mp4",
                        "start_ms": 100_000,
                        "end_ms": 120_000,
                    }
                ]
            },
            durations=[20_000],
            ledger_path=path,
        )


def test_governed_human_revision_is_accepted(tmp_path):
    path = tmp_path / "truth.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "governance": {
                    "schema_version": "source-subtitle-truth-governance.v2",
                    "governed_entry_start_index": 0,
                },
                "source_aliases": [],
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "human-review",
                        "revision_id": "r1",
                        "assertion_state": "VERIFIED_ACTIVE",
                        "evidence_class": "HASH_BOUND_REVIEWED_SUBTITLE",
                        "authority": "Ivan reviewed",
                        "decision_authority": "IVAN_OPERATOR_TRUTH",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 110_000,
                        "source_end_ms": 114_000,
                        "action": "replace_cue",
                        "text": "审定文本",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    corrected, audit = apply_source_subtitle_truth(
        _srt((10, 14, "原文")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=path,
    )

    assert "审定文本" in corrected
    assert audit["status"] == "APPLIED"


def test_governed_cpa_candidate_stays_proposed_and_does_not_mutate(tmp_path):
    path = tmp_path / "truth.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "source-subtitle-truth-ledger.v1",
                "governance": {
                    "schema_version": "source-subtitle-truth-governance.v2",
                    "governed_entry_start_index": 0,
                },
                "source_aliases": [],
                "entries": [
                    {
                        "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                        "truth_id": "cpa-candidate",
                        "revision_id": "r1",
                        "assertion_state": "PROPOSED",
                        "evidence_class": "HASH_BOUND_STRUCTURED_EVENT",
                        "authority": "candidate evidence for CPA",
                        "decision_authority": "CPA_CANDIDATE_ONLY",
                        "recording_basename": "recording.mp4",
                        "source_start_ms": 110_000,
                        "source_end_ms": 114_000,
                        "action": "replace_cue",
                        "text": "候选文本",
                        "required": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    source = _srt((10, 14, "原文"))
    corrected, audit = apply_source_subtitle_truth(
        source,
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=path,
    )

    assert corrected == source
    assert audit["status"] == "NO_RELEVANT_INTERVAL"


def test_piece_duration_jitter_does_not_capture_adjacent_cue(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "encoded-duration-jitter",
                "recording_basename": "recording.mp4",
                "source_start_ms": 21_000,
                "source_end_ms": 22_000,
                "action": "replace_cue",
                "text": "正确文本",
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (11_000, 12_000, "误听文本"),
            (12_000, 13_000, "下一句"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 10_000,
                },
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 20_000,
                    "end_ms": 30_000,
                },
            ]
        },
        durations=[10_003, 10_000],
        ledger_path=ledger,
    )

    assert "正确文本" in corrected
    assert "下一句" in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["cue_indexes"] == [1]


def test_source_interval_substring_repair_is_local_and_idempotent(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "emoticon-reading",
                "recording_basename": "recording.mp4",
                "source_start_ms": 210_000,
                "source_end_ms": 216_000,
                "action": "replace_substring",
                "replacements": [
                    {"surface": "风光风光", "canonical": "分号分号"},
                    {"surface": "封号封号", "canonical": "分号分号"},
                ],
                "required_text": "分号分号",
                "required": True,
            }
        ],
    )
    spec = {
        "pieces": [
            {
                "remote_media": "/source/recording.mp4",
                "start_ms": 200_000,
                "end_ms": 220_000,
            }
        ]
    }
    corrected, audit = apply_source_subtitle_truth(
        _srt((10, 16, "主播念成风光风光了")),
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )
    assert "分号分号" in corrected
    assert audit["status"] == "APPLIED"

    second, second_audit = apply_source_subtitle_truth(
        corrected,
        spec=spec,
        durations=[20_000],
        ledger_path=ledger,
    )
    assert second == corrected
    assert second_audit["status"] == "ALREADY_SATISFIED"


def test_july18_hecheng_kmx_truth_handles_downstream_third_homophone():
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((25_730, 27_960, "我这真的有一些题")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/source/22966160_20260718-23-59-36.mp4"
                    ),
                    # 窗口止于 kmx 钉区间末端：2026-07-19 起同一录播 545.5s
                    # 处还有「为什么幻听」钉，本测试只回放 kmx 三重同音案。
                    "start_ms": 517_500,
                    "end_ms": 545_460,
                }
            ]
        },
        durations=[27_960],
        ledger_path=(
            REPO_ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json"
        ),
    )

    assert "我这真的有一些kmx" in corrected
    assert audit["status"] == "APPLIED"
    assert not audit["failures"]


def test_required_included_truth_fails_closed_when_timeline_has_no_cue(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "required",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "正确文本",
                "required": True,
            }
        ],
    )
    _corrected, audit = apply_source_subtitle_truth(
        _srt((1, 2, "别处")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == "REPLACE_CUE_TARGET_NOT_UNIQUE"


def test_post_context_truth_is_corrected_but_not_boundary_owner(
    tmp_path,
):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "later-candidate-truth",
                "recording_basename": "recording.mp4",
                "source_start_ms": 118_000,
                "source_end_ms": 120_000,
                "action": "replace_cue",
                "text": "后续候选正确文本",
                "required": True,
            }
        ],
    )
    spec = {
        "candidate_id": "candidate-a",
        "semantic_start_ms": 100_000,
        "semantic_end_ms": 110_000,
        "pieces": [
            {
                "remote_media": "/source/recording.mp4",
                "start_ms": 95_000,
                "end_ms": 125_000,
            }
        ],
    }
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((23_000, 25_000, "后续候选误听")),
        spec=spec,
        durations=[30_000],
        ledger_path=ledger,
    )
    contracts = ledger_required_owner_contracts(
        spec=spec,
        durations=[30_000],
        ledger_path=ledger,
    )

    assert "后续候选正确文本" in corrected
    assert audit["applied"][0]["truth_id"] == "later-candidate-truth"
    assert contracts == []


def test_lead_context_truth_is_not_boundary_owner(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "lead-context-truth",
                "recording_basename": "recording.mp4",
                "source_start_ms": 96_000,
                "source_end_ms": 98_000,
                "action": "replace_cue",
                "text": "前置语境",
                "required": True,
            }
        ],
    )

    contracts = ledger_required_owner_contracts(
        spec={
            "candidate_id": "candidate-a",
            "semantic_start_ms": 100_000,
            "semantic_end_ms": 110_000,
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 95_000,
                    "end_ms": 125_000,
                }
            ],
        },
        durations=[30_000],
        ledger_path=ledger,
    )

    assert contracts == []


def test_bounded_leading_cue_jitter_becomes_story_boundary_owner(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "opening-cue-jitter",
                "recording_basename": "recording.mp4",
                "source_start_ms": 99_960,
                "source_end_ms": 102_000,
                "action": "replace_cue",
                "text": "开场完整问句",
                "required": True,
            }
        ],
    )

    contracts = ledger_required_owner_contracts(
        spec={
            "candidate_id": "candidate-a",
            "semantic_start_ms": 100_000,
            "semantic_end_ms": 110_000,
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 95_000,
                    "end_ms": 125_000,
                }
            ],
        },
        durations=[30_000],
        ledger_path=ledger,
    )

    assert contracts == [
        {
            "owner_kind": "source_subtitle_truth",
            "owner_id": "opening-cue-jitter",
            "required": True,
            "source_start_ms": 99_960,
            "source_end_ms": 102_000,
            "owner_scope_sha256": contracts[0]["owner_scope_sha256"],
            "local_windows": [
                {"start_ms": 4_960, "end_ms": 7_000}
            ],
        }
    ]


def test_leading_story_straddle_beyond_jitter_tolerance_fails(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "wide-opening-straddle",
                "recording_basename": "recording.mp4",
                "source_start_ms": 99_000,
                "source_end_ms": 102_000,
                "action": "replace_cue",
                "text": "无法安全归属的开场",
                "required": True,
            }
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_STRADDLE",
    ):
        ledger_required_owner_contracts(
            spec={
                "candidate_id": "candidate-a",
                "semantic_start_ms": 100_000,
                "semantic_end_ms": 110_000,
                "pieces": [
                    {
                        "remote_media": "/source/recording.mp4",
                        "start_ms": 95_000,
                        "end_ms": 125_000,
                    }
                ],
            },
            durations=[30_000],
            ledger_path=ledger,
        )


def test_story_scope_straddle_fails_closed(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "straddling-truth",
                "recording_basename": "recording.mp4",
                "source_start_ms": 109_000,
                "source_end_ms": 112_000,
                "action": "replace_cue",
                "text": "跨界文本",
                "required": True,
            }
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="SOURCE_TRUTH_BOUNDARY_OWNER_SCOPE_STRADDLE",
    ):
        ledger_required_owner_contracts(
            spec={
                "candidate_id": "candidate-a",
                "semantic_start_ms": 100_000,
                "semantic_end_ms": 110_000,
                "pieces": [
                    {
                        "remote_media": "/source/recording.mp4",
                        "start_ms": 95_000,
                        "end_ms": 125_000,
                    }
                ],
            },
            durations=[30_000],
            ledger_path=ledger,
        )


def test_next_topic_truth_is_required_context_but_not_boundary_owner(
    tmp_path,
):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "next-topic-nickname",
                "recording_basename": "recording.mp4",
                "source_start_ms": 118_000,
                "source_end_ms": 120_000,
                "action": "replace_substring",
                "replacements": [
                    {
                        "surface": "香香烧烤",
                        "canonical": "邪恶守宫",
                    }
                ],
                "required_text": "邪恶守宫",
                "boundary_role": "next_topic_witness",
                "required": True,
            }
        ],
    )
    spec = {
        "semantic_end_ms": 110_000,
        "pieces": [
            {
                "remote_media": "/source/recording.mp4",
                "start_ms": 100_000,
                "end_ms": 125_000,
            }
        ],
    }
    corrected, audit = apply_source_subtitle_truth(
        _srt((18, 20, "香香烧烤开始下一条SC")),
        spec=spec,
        durations=[25_000],
        ledger_path=ledger,
    )
    contracts = ledger_required_owner_contracts(
        spec=spec,
        durations=[25_000],
        ledger_path=ledger,
    )

    assert "邪恶守宫开始下一条SC" in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["boundary_role"] == "next_topic_witness"
    assert contracts == []


def test_next_topic_truth_is_not_a_final_delivery_owner(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "post-story-context-only",
                "recording_basename": "recording.mp4",
                "source_start_ms": 118_000,
                "source_end_ms": 120_000,
                "action": "replace_substring",
                "replacements": [
                    {"surface": "香香烧烤", "canonical": "邪恶守宫"}
                ],
                "required_text": "邪恶守宫",
                "boundary_role": "next_topic_witness",
                "required": True,
            }
        ],
    )
    _corrected, truth_audit = apply_source_subtitle_truth(
        _srt_ms((18_000, 20_000, "香香烧烤开始下一条SC")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 125_000,
                }
            ]
        },
        durations=[25_000],
        ledger_path=ledger,
    )
    authority_audit = {
        "source_subtitle_truth_audit": truth_audit,
    }
    final_srt = _srt_ms((0, 10_000, "当前故事已经闭环"))

    assert verify_chat_authority_final_surfaces(
        authority_audit,
        final_text_srt=final_srt,
        final_speaker_srt=final_srt,
        delivery_start_ms=0,
        delivery_end_ms=10_000,
    )
    receipt = authority_audit[
        "final_source_truth_owner_verification"
    ]
    assert receipt["status"] == "PASS"
    assert receipt["required_truth_row_count"] == 0
    assert receipt["context_only_truth_row_count"] == 1
    assert receipt["required_window_count"] == 0


def test_next_topic_truth_cannot_overlap_story_target(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "misclassified-story-truth",
                "recording_basename": "recording.mp4",
                "source_start_ms": 108_000,
                "source_end_ms": 112_000,
                "action": "replace_cue",
                "text": "不能逃出故事",
                "boundary_role": "next_topic_witness",
                "required": True,
            }
        ],
    )

    with pytest.raises(
        RuntimeError,
        match="SOURCE_TRUTH_NEXT_TOPIC_WITNESS_OVERLAPS_STORY",
    ):
        ledger_required_owner_contracts(
            spec={
                "semantic_end_ms": 110_000,
                "pieces": [
                    {
                        "remote_media": "/source/recording.mp4",
                        "start_ms": 100_000,
                        "end_ms": 125_000,
                    }
                ],
            },
            durations=[25_000],
            ledger_path=ledger,
        )


def test_committed_hotpot_next_sc_truth_does_not_move_cue_911_endpoint():
    ledger = (
        REPO_ROOT / "assets/lidousha/subtitle_truth_ledger.v1.json"
    )
    contracts = ledger_required_owner_contracts(
        spec={
            "semantic_end_ms": 2_056_480,
            "pieces": [
                {
                    "remote_media": (
                        "/source/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 1_853_760,
                    "end_ms": 2_116_480,
                }
            ],
        },
        durations=[262_720],
        ledger_path=ledger,
    )
    by_id = {str(row["owner_id"]): row for row in contracts}

    assert "20260722-hotpot-evil-gecko-guard-thanks-r1" in by_id
    assert "20260722-hotpot-evil-gecko-nickname-first-r1" not in by_id
    assert "20260722-hotpot-evil-gecko-nickname-callback-r1" not in by_id
    assert max(
        int(window["end_ms"])
        for row in contracts
        for window in row["local_windows"]
    ) <= 202_720


def test_required_truth_fails_closed_when_candidate_cuts_through_interval(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "partial-phrase",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "完整原话",
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt((0, 2, "半句")),
        spec={
            "pieces": [
                {
                    "remote_media": "/source/recording.mp4",
                    "start_ms": 112_000,
                    "end_ms": 120_000,
                }
            ]
        },
        durations=[8_000],
        ledger_path=ledger,
    )

    assert "完整原话" not in corrected
    assert audit["status"] == "FAILED"
    assert audit["failures"][0]["reason_code"] == (
        "SOURCE_INTERVAL_PARTIALLY_RETAINED"
    )


def test_symlink_ledger_is_rejected(tmp_path):
    real = _ledger(tmp_path, [])
    linked = tmp_path / "linked.json"
    linked.symlink_to(real)
    with pytest.raises(RuntimeError, match="LEDGER_INVALID"):
        apply_source_subtitle_truth(
            "",
            spec={"pieces": []},
            durations=[],
            ledger_path=linked,
        )


def test_replace_cue_split_across_recued_cues_counts_satisfied(tmp_path):
    """2026-07-19 合并跳切实证：fresh 重转写把钉子区间切成两条 cue，
    真值文本跨界拼接已逐字成立 → satisfied（no-op），不再 NOT_UNIQUE 失败。"""
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "split-cue-satisfied",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 118_000,
                "action": "replace_cue",
                "text": "很多人笑出声这件事情kmx要不要想想为什么",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    srt = _srt(
        (10, 14, "很多人笑出声这件事情"),
        (14, 18, "kmx要不要想想为什么"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={
            "pieces": [
                {"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 120_000}
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["failures"] == []
    assert audit["satisfied"][0]["truth_id"] == "split-cue-satisfied"
    assert corrected == srt  # no-op


def test_replace_cue_split_cues_garble_now_redistributes(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "split-cue-wrong",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 118_000,
                "action": "replace_cue",
                "text": "很多人笑出声这件事情kmx要不要想想为什么",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    srt = _srt(
        (10, 14, "很多人笑出声这件事情"),
        (14, 18, "乒乓球要不要想想为什么"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={
            "pieces": [
                {"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 120_000}
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    # 2026-07-20 契约升级：高相似跨 cue 残渣正是多 cue 重分配的正解场景
    # ——钉文落刀、乒乓球残渣清除；邻句保护由辖区收缩测试单独把守。
    assert audit["status"] == "APPLIED"
    assert "乒乓球" not in corrected
    assert "kmx" in corrected


def test_replace_cue_split_cues_punctuation_insensitive_satisfied(tmp_path):
    """2026-07-19 实证第二层：审定文本「…事情，kmx」跨 cue 时逗号由边界
    停顿表达——去标点归一后内容成立即 satisfied，不被一个标点冤枉。"""
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "punct-split",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_000,
                "action": "replace_cue",
                "text": "很多人笑出声这件事情，kmx",
                "authority": "Ivan",
                "required": True,
            }
        ],
    )
    srt = _srt(
        (10, 13, "很多人笑出声这件事情"),
        (13, 16, "kmx要不要考虑一下为什么"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={
            "pieces": [
                {"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 120_000}
            ]
        },
        durations=[20_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["failures"] == []
    assert corrected == srt


def test_multi_cue_replace_redistributes_pin_text(tmp_path):
    """多 cue 辖区重分配（2026-07-20 kmx r5 案）：fresh 掷出不同切分时,
    整句钉按相似度分布到覆盖 cue,零内容发明、时间轴不动。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "only-kmx",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 116_000,
                "action": "replace_cue",
                "text": "只有kmx会这样称呼李豆沙",
                "required": True,
            }
        ],
    )
    srt = (
        "1\n00:00:10,000 --> 00:00:13,000\n只有，只有kmx。\n\n"
        "2\n00:00:13,000 --> 00:00:16,000\n你们怎么会这样称呼李豆沙？\n"
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "APPLIED"
    assert not audit["failures"]
    joined = corrected.replace("\n", "")
    assert "只有kmx会这样称呼李豆沙" in joined.replace("，", "").replace("。", "") or (
        "只有kmx" in corrected and "称呼李豆沙" in corrected
    )
    # 两条 cue 都有文本(不许空 cue),且不再含错误残渣「你们怎么会」
    row = audit["applied"][0]
    parts = row["multi_cue_redistribution"]
    assert len(parts) == 2 and all(p.strip() for p in parts)
    assert "你们" not in corrected


def test_unrelated_neighbor_cue_never_overwritten_by_pin(tmp_path):
    """邻句保护（辖区收缩）：钉窗擦到与钉文零共通的邻句时,邻句原文分毫
    不动;收缩后单 cue 落刀正常执行。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "graze-neighbor",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 114_200,
                "action": "replace_cue",
                "text": "只有kmx会这样称呼李豆沙",
                "required": True,
            }
        ],
    )
    srt = _srt_ms(
        (10_000, 14_000, "只有，只有kmx这样称李豆沙"),
        (14_050, 18_000, "今天晚饭吃番茄炒蛋"),
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert "今天晚饭吃番茄炒蛋" in corrected
    assert "只有kmx会这样称呼李豆沙" in corrected
    assert audit["status"] == "APPLIED"
    projection = audit["applied"][0]["resolved_target_projection"]
    assert [cue["cue_index"] for cue in projection["cues"]] == [1]

    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    assert verify_chat_authority_final_surfaces(
        {"source_subtitle_truth_audit": deepcopy(audit)},
        final_text_srt=corrected,
        final_speaker_srt=corrected,
        delivery_start_ms=0,
        delivery_end_ms=30_000,
    )
    tampered = corrected.replace(
        "只有kmx会这样称呼李豆沙",
        "只有kmx会这样称呼李豆沙多余内容",
    )
    assert not verify_chat_authority_final_surfaces(
        {"source_subtitle_truth_audit": deepcopy(audit)},
        final_text_srt=tampered,
        final_speaker_srt=tampered,
        delivery_start_ms=0,
        delivery_end_ms=30_000,
    )


def test_projection_aggregates_one_truth_split_across_two_source_pieces(
    tmp_path,
):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "two-piece-truth",
                "recording_basename": "recording.mp4",
                "source_start_ms": 1_000,
                "source_end_ms": 3_000,
                "action": "replace_cue",
                "text": "审定前审定后",
                "required": True,
            }
        ],
    )
    final, audit = apply_source_subtitle_truth(
        _srt_ms(
            (1_000, 2_000, "审定前"),
            (2_000, 3_000, "审定后"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/x/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 2_000,
                },
                {
                    "remote_media": "/x/recording.mp4",
                    "start_ms": 2_000,
                    "end_ms": 4_000,
                },
            ]
        },
        durations=[2_000, 2_000],
        ledger_path=ledger,
    )

    assert audit["status"] == "ALREADY_SATISFIED"
    projection = audit["satisfied"][0]["resolved_target_projection"]
    assert [cue["after_text"] for cue in projection["cues"]] == [
        "审定前",
        "审定后",
    ]

    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    assert verify_chat_authority_final_surfaces(
        {"source_subtitle_truth_audit": deepcopy(audit)},
        final_text_srt=final,
        final_speaker_srt=final,
        delivery_start_ms=0,
        delivery_end_ms=4_000,
    )
    tampered = final.replace("审定后", "被篡改")
    assert not verify_chat_authority_final_surfaces(
        {"source_subtitle_truth_audit": deepcopy(audit)},
        final_text_srt=tampered,
        final_speaker_srt=tampered,
        delivery_start_ms=0,
        delivery_end_ms=4_000,
    )


def test_invalid_resolved_target_projection_fails_final_owner_closed(
    tmp_path,
):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "projection-integrity",
                "recording_basename": "recording.mp4",
                "source_start_ms": 1_000,
                "source_end_ms": 2_000,
                "action": "replace_cue",
                "text": "审定文本",
                "required": True,
            }
        ],
    )
    final, truth_audit = apply_source_subtitle_truth(
        _srt_ms((1_000, 2_000, "误听文本")),
        spec={
            "pieces": [
                {
                    "remote_media": "/x/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 3_000,
                }
            ]
        },
        durations=[3_000],
        ledger_path=ledger,
    )
    from src.autoslice.producer_text_finalization import (
        verify_chat_authority_final_surfaces,
    )

    broken_audits = []
    wrong_threshold = deepcopy(truth_audit)
    wrong_threshold["applied"][0]["resolved_target_projection"][
        "min_overlap_ms"
    ] = 79
    broken_audits.append(wrong_threshold)

    wrong_index = deepcopy(truth_audit)
    wrong_index["applied"][0]["resolved_target_projection"]["cues"][0][
        "cue_index"
    ] = 2
    broken_audits.append(wrong_index)

    outside_window = deepcopy(truth_audit)
    projected_cue = outside_window["applied"][0][
        "resolved_target_projection"
    ]["cues"][0]
    projected_cue["start_ms"] = 2_100
    projected_cue["end_ms"] = 2_900
    broken_audits.append(outside_window)

    for broken in broken_audits:
        chat_audit = {"source_subtitle_truth_audit": broken}
        assert not verify_chat_authority_final_surfaces(
            chat_audit,
            final_text_srt=final,
            final_speaker_srt=final,
            delivery_start_ms=0,
            delivery_end_ms=3_000,
        )
        assert chat_audit["final_verification_failure"] == (
            "SOURCE_TRUTH_FINAL_OWNER_NOT_VERIFIED"
        )


def _grazing_source_truth_preview_fixture(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "exact-target-after-graze",
                "recording_basename": "recording.mp4",
                "source_start_ms": 850,
                "source_end_ms": 2_000,
                "action": "replace_cue",
                "text": "目标正确文本",
                "required": True,
            }
        ],
    )
    draft = _srt_ms(
        (0, 1_000, "完全无关邻句"),
        (1_000, 2_000, "目标正确文木"),
    )
    _projected, audit = apply_source_subtitle_truth(
        draft,
        spec={
            "pieces": [
                {
                    "remote_media": "/x/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 3_000,
                }
            ]
        },
        durations=[3_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "APPLIED"
    return draft, audit


def test_source_truth_preview_exact_projection_excludes_150ms_graze(
    tmp_path,
):
    draft, audit = _grazing_source_truth_preview_fixture(tmp_path)

    receipt = build_source_truth_preview_receipt(
        input_srt_text=draft,
        source_truth_audit=audit,
        stage="pre_entity_authority",
    )

    assert receipt["status"] == "PASS"
    assert receipt["exact_protected_cue_indexes"] == [2]
    assert receipt["fallback_protected_cue_indexes"] == []
    assert receipt["protected_cue_indexes"] == [2]
    assert receipt["fallback_local_windows"] == []
    assert receipt["exact_projection_owners"][0]["cue_indexes"] == [2]
    # The preview exposes only target coordinates and hashes.  It never emits
    # the projected correction as a replacement pipeline output.
    serialized = json.dumps(receipt, ensure_ascii=False)
    assert "目标正确文本" not in serialized
    assert "目标正确文木" not in serialized


def test_source_truth_preview_required_failure_ignores_projection_and_falls_back(
    tmp_path,
):
    draft, successful_audit = _grazing_source_truth_preview_fixture(tmp_path)
    failed_audit = deepcopy(successful_audit)
    failed_row = failed_audit["applied"].pop()
    failed_row["reason_code"] = "REQUIRED_SOURCE_TRUTH_NOT_SATISFIED"
    # A failure must not inherit even a superficially precise projection.
    failed_row["resolved_target_projection"] = {
        "schema_version": "stale-or-tampered",
        "cues": [{"cue_index": 2}],
    }
    failed_audit["failures"] = [failed_row]
    failed_audit["status"] = "FAILED"

    receipt = build_source_truth_preview_receipt(
        input_srt_text=draft,
        source_truth_audit=failed_audit,
        stage="pre_entity_authority",
    )

    assert receipt["status"] == "UNRESOLVED_REQUIRED"
    assert receipt["exact_protected_cue_indexes"] == []
    # The raw 850..2000 discovery window overlaps cue 1 by 150 ms, exceeding
    # the conservative 80 ms threshold, so unresolved fallback protects both.
    assert receipt["fallback_protected_cue_indexes"] == [1, 2]
    assert receipt["protected_cue_indexes"] == [1, 2]
    assert receipt["fallback_local_windows"] == [
        {"start_ms": 850, "end_ms": 2_000}
    ]


def test_source_truth_preview_optional_truth_never_protects_or_owns(
    tmp_path,
):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "best-effort-only",
                "recording_basename": "recording.mp4",
                "source_start_ms": 1_000,
                "source_end_ms": 2_000,
                "action": "replace_cue",
                "text": "可选审定",
                "required": False,
            }
        ],
    )
    draft = _srt_ms((1_000, 2_000, "可选误听"))
    _projected, audit = apply_source_subtitle_truth(
        draft,
        spec={
            "pieces": [
                {
                    "remote_media": "/x/recording.mp4",
                    "start_ms": 0,
                    "end_ms": 3_000,
                }
            ]
        },
        durations=[3_000],
        ledger_path=ledger,
    )
    assert audit["applied"][0]["required"] is False
    # Even a broken optional projection is ignored rather than promoted into a
    # protection or final-owner contract.
    audit["applied"][0]["resolved_target_projection"] = {"invalid": True}

    receipt = build_source_truth_preview_receipt(
        input_srt_text=draft,
        source_truth_audit=audit,
        stage="pre_entity_authority",
    )

    assert receipt["status"] == "PASS"
    assert receipt["ignored_optional_truth_ids"] == ["best-effort-only"]
    assert receipt["exact_projection_owners"] == []
    assert receipt["protected_cue_indexes"] == []
    assert receipt["fallback_local_windows"] == []


def test_source_truth_preview_success_projection_must_be_valid_and_grid_bound(
    tmp_path,
):
    draft, audit = _grazing_source_truth_preview_fixture(tmp_path)
    broken_audits = []

    missing = deepcopy(audit)
    del missing["applied"][0]["resolved_target_projection"]
    broken_audits.append(missing)

    wrong_threshold = deepcopy(audit)
    wrong_threshold["applied"][0]["resolved_target_projection"][
        "min_overlap_ms"
    ] = 79
    broken_audits.append(wrong_threshold)

    wrong_grid_time = deepcopy(audit)
    wrong_grid_time["applied"][0]["resolved_target_projection"]["cues"][0][
        "start_ms"
    ] = 1_050
    broken_audits.append(wrong_grid_time)

    for broken in broken_audits:
        with pytest.raises(
            RuntimeError,
            match="SOURCE_TRUTH_PREVIEW_PROJECTION_INVALID",
        ):
            build_source_truth_preview_receipt(
                input_srt_text=draft,
                source_truth_audit=broken,
                stage="pre_entity_authority",
            )


def test_source_truth_preview_receipt_hash_binds_grid_ledger_and_stage(
    tmp_path,
):
    draft, audit = _grazing_source_truth_preview_fixture(tmp_path)
    baseline = build_source_truth_preview_receipt(
        input_srt_text=draft,
        source_truth_audit=audit,
        stage="pre_entity_authority",
    )
    other_stage = build_source_truth_preview_receipt(
        input_srt_text=draft,
        source_truth_audit=audit,
        stage="pre_final_review",
    )
    other_grid = build_source_truth_preview_receipt(
        input_srt_text=draft.replace("完全无关邻句", "另一条无关邻句"),
        source_truth_audit=audit,
        stage="pre_entity_authority",
    )
    other_ledger_audit = deepcopy(audit)
    other_ledger_audit["ledger_sha256"] = "sha256:" + "b" * 64
    other_ledger = build_source_truth_preview_receipt(
        input_srt_text=draft,
        source_truth_audit=other_ledger_audit,
        stage="pre_entity_authority",
    )

    assert baseline["receipt_sha256"] != other_stage["receipt_sha256"]
    assert baseline["receipt_sha256"] != other_grid["receipt_sha256"]
    assert baseline["receipt_sha256"] != other_ledger["receipt_sha256"]


def test_committed_ledger_supersedes_hallucinated_opening_suffix():
    """旧错误钉子留审计历史，但不得再回放南町/LLNNHHB后缀。"""

    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    srt = _srt_ms((0, 2_800, "这不是主播最最最最喜欢"),)
    spec = {
        "pieces": [
            {
                "remote_media": "/recordings/22966160_20260722-19-35-15.mp4",
                "start_ms": 672_920,
                "end_ms": 675_480,
            }
        ]
    }

    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec=spec,
        durations=[2_560],
        ledger_path=ledger,
    )

    assert "这不是主播最最最最喜欢" in corrected
    assert "南町nightin" not in corrected
    assert "llnnhhb" not in corrected.casefold()
    assert audit["status"] == "ALREADY_SATISFIED"
    assert [row["truth_id"] for row in audit["satisfied"]] == [
        "20260722-nancho-confrontation-opening-human-r2"
    ]
    assert audit["inactive"][0]["truth_id"] == (
        "20260722-nancho-confrontation-opening-mixed-name"
    )


def test_committed_ledger_omits_disputed_brainflick_opening_prefix():
    """冲突前缀留空，只保留多路声学证据共同支持的核心。"""

    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((250, 2_810, "李姐晚上好，就请坐在左边的弹"),),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 1_475_750,
                    "end_ms": 1_478_560,
                }
            ]
        },
        durations=[2_810],
        ledger_path=ledger,
    )

    assert [cue.text for cue in parse_srt_cues(corrected)] == [
        "请坐在左边的弹"
    ]
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == (
        "20260722-nancho-brainflick-opening-conservative-core-r1"
    )

    replay_corrected, replay_audit = apply_source_subtitle_truth(
        _srt_ms((250, 2_810, "就请坐在左边的弹"),),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recovery/22966160_20260722-19-34-50.mp4"
                    ),
                    "source_media_sha256": (
                        "sha256:"
                        "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2"
                        "ddbe7765436718989a"
                    ),
                    "start_ms": 1_475_750,
                    "end_ms": 1_478_560,
                }
            ]
        },
        durations=[2_810],
        ledger_path=ledger,
    )

    assert [cue.text for cue in parse_srt_cues(replay_corrected)] == [
        "请坐在左边的弹"
    ]
    assert replay_audit["applied"][0]["source_aliases"][0]["alias_id"] == (
        "20260722-official-replay-bv1fjg16xex6"
    )


def test_committed_ledger_projects_nancho_truth_to_hash_bound_official_replay():
    """7/22 官方回放只在精确哈希绑定时继承原录制时间轴的审定钉子。"""

    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    replay_sha256 = (
        "sha256:"
        "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 2_800, "这不是主播最最最最喜欢")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recovery/22966160_20260722-19-34-50.mp4"
                    ),
                    "source_media_sha256": replay_sha256,
                    "start_ms": 672_920,
                    "end_ms": 675_480,
                }
            ]
        },
        durations=[2_560],
        ledger_path=ledger,
    )

    assert "这不是主播最最最最喜欢" in corrected
    assert "南町nightin" not in corrected
    assert "llnnhhb" not in corrected.casefold()
    assert audit["status"] == "ALREADY_SATISFIED"
    assert audit["satisfied"][0]["source_aliases"][0]["alias_id"] == (
        "20260722-official-replay-bv1fjg16xex6"
    )


@pytest.mark.parametrize(
    ("source_start_ms", "source_end_ms", "draft", "expected", "truth_id"),
    [
        (
            820_590,
            824_090,
            "确实要住她家了，要小心小n老师啊，",
            "确实要，要小心小N老师啊，",
            "20260722-nancho-confrontation-no-duplicate-residence-r1",
        ),
        (
            871_920,
            874_980,
            "NNL一般都是NNLL，是吗",
            "N和L一般都是NNLL，是吗",
            "20260722-nancho-confrontation-n-and-l-segmentation-r1",
        ),
        (
            835_950,
            836_510,
            "行",
            "行啊",
            "20260722-nancho-confrontation-xing-a-response-r1",
        ),
    ],
)
def test_committed_ledger_repairs_new_nancho_acoustic_truths_on_official_replay(
    source_start_ms, source_end_ms, draft, expected, truth_id
):
    """新声学钉子必须按 hash-bound 官方回放的绝对时间轴稳定重放。"""

    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    duration_ms = source_end_ms - source_start_ms
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, duration_ms, draft)),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recovery/22966160_20260722-19-34-50.mp4"
                    ),
                    "source_media_sha256": (
                        "sha256:"
                        "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
                    ),
                    "start_ms": source_start_ms,
                    "end_ms": source_end_ms,
                }
            ]
        },
        durations=[duration_ms],
        ledger_path=ledger,
    )

    assert [cue.text for cue in parse_srt_cues(corrected)] == [expected]
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == truth_id
    assert audit["applied"][0]["source_aliases"][0]["alias_id"] == (
        "20260722-official-replay-bv1fjg16xex6"
    )


def test_committed_ledger_repairs_huishen_nasal_final_spelling_drift():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 1_370, "好像是毁神吧"),
            (1_370, 3_530, "毁神说救救李姐"),
            (5_980, 8_600, "然后绘声什么都没有做"),
            (11_560, 14_120, "毁神发了一句"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 748_620,
                    "end_ms": 762_740,
                }
            ]
        },
        durations=[14_120],
        ledger_path=ledger,
    )

    assert "然后毁神什么都没有做" in corrected
    assert "绘声" not in corrected
    assert audit["status"] == "APPLIED"
    row = next(
        row
        for row in audit["applied"]
        if row["truth_id"]
        == "20260722-nancho-confrontation-huishen-surface"
    )
    assert row["replacements"] == [
        {"cue_index": 3, "surface": "绘声", "canonical": "毁神"}
    ]


def test_committed_ledger_repairs_sumi_na_xiang_le_acoustic_verdict():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 4_620, "诶，怎么有点像礼墨Sumi拿下了黑色的有角")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 3_588_020,
                    "end_ms": 3_592_640,
                }
            ]
        },
        durations=[4_620],
        ledger_path=ledger,
    )

    assert "礼墨Sumi？哪像了，黑色的，有角" in corrected
    assert "拿下了黑色的有角" not in corrected
    assert audit["status"] == "APPLIED"


def test_committed_ledger_preserves_reviewed_chair_190_surface():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 3_790, "哈哈，一瞅1190是谣言啊"),
            (3_790, 6_130, "190，190是谣言啊"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 1_619_250,
                    "end_ms": 1_625_380,
                }
            ]
        },
        durations=[6_130],
        ledger_path=ledger,
    )

    assert "哈哈哈哈，190，190是谣言啊" in corrected
    assert "粉毛是我之前染过粉毛啊" in corrected
    assert "1190" not in corrected
    assert audit["status"] == "APPLIED"


def test_committed_ledger_preserves_brainflick_give_up_turn():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 1_880, "算好了，我弹了啊")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 1_510_740,
                    "end_ms": 1_512_620,
                }
            ]
        },
        durations=[1_880],
        ledger_path=ledger,
    )

    assert "算了，好了，我弹了啊" in corrected
    assert "算好了，我弹了啊" not in corrected
    assert audit["status"] == "APPLIED"


def test_committed_ledger_keeps_independently_adjudicated_brainflick_phrases():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 2_670, "你弹一弹啊"),
            (13_550, 15_110, "管他谁是左边的呢"),
            (18_030, 22_610, "再弹，再，再一弹一弹"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 1_520_750,
                    "end_ms": 1_543_360,
                }
            ]
        },
        durations=[22_610],
        ledger_path=ledger,
    )

    assert "互相弹一弹啊" in corrected
    assert "再弹，再，再硬弹一弹" in corrected
    assert "硬弹一弹啊" not in corrected
    assert "再弹，再，再互相弹一弹" not in corrected
    assert "你弹一弹啊" not in corrected
    assert "再一弹一弹" not in corrected
    assert audit["status"] == "APPLIED"
    assert {
        row["truth_id"] for row in audit["applied"]
    } >= {
        "20260722-nancho-brainflick-mutual-flick-r2",
        "20260722-nancho-brainflick-hard-flick-callback-r1",
    }
    assert {
        row["truth_id"] for row in audit["inactive"]
    } >= {
        "20260722-nancho-brainflick-hard-flick-r1",
    }


def test_mention_postconditions_do_not_let_one_correct_name_hide_another(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "same-name-twice",
                "recording_basename": "recording.mp4",
                "source_start_ms": 100_000,
                "source_end_ms": 104_000,
                "action": "replace_substring",
                "replacements": [{"surface": "绘声", "canonical": "毁神"}],
                "required_text": "毁神",
                "mention_postconditions": [
                    {
                        "source_start_ms": 100_000,
                        "source_end_ms": 102_000,
                        "required_text": "毁神",
                        "forbidden_tokens": [],
                    },
                    {
                        "source_start_ms": 102_000,
                        "source_end_ms": 104_000,
                        "required_text": "毁神",
                        "forbidden_tokens": [],
                    },
                ],
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 2_000, "毁神来了"), (2_000, 4_000, "神秘没做事")),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 104_000,
                }
            ]
        },
        durations=[4_000],
        ledger_path=ledger,
    )

    assert "毁神来了" in corrected
    assert audit["status"] == "FAILED"
    assert any(
        row["reason_code"] == "MENTION_REQUIRED_TEXT_MISSING"
        and row["mention_ordinal"] == 2
        for row in audit["failures"]
    )


def test_mention_postconditions_own_each_preexisting_correct_mention(
    tmp_path,
):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "same-name-twice-correct",
                "recording_basename": "recording.mp4",
                "source_start_ms": 100_000,
                "source_end_ms": 104_000,
                "action": "replace_substring",
                "replacements": [
                    {"surface": "绘声", "canonical": "毁神"}
                ],
                "required_text": "毁神",
                "mention_postconditions": [
                    {
                        "source_start_ms": 100_000,
                        "source_end_ms": 102_000,
                        "required_text": "毁神",
                        "forbidden_tokens": ["绘声"],
                    },
                    {
                        "source_start_ms": 102_000,
                        "source_end_ms": 104_000,
                        "required_text": "毁神",
                        "forbidden_tokens": ["绘声"],
                    },
                ],
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 2_000, "毁神来了"),
            (2_000, 4_000, "毁神什么都没做"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 104_000,
                }
            ]
        },
        durations=[4_000],
        ledger_path=ledger,
    )

    assert "毁神来了" in corrected
    assert "毁神什么都没做" in corrected
    assert audit["status"] == "ALREADY_SATISFIED"
    row = audit["satisfied"][0]
    assert row["mention_owner_resolution"] == {
        "status": "PASS",
        "cue_indexes": [1, 2],
        "failure_reason_codes": [],
    }
    assert row["resolved_target_projection"]["cues"] == [
        {
            "cue_index": 1,
            "start_ms": 0,
            "end_ms": 2_000,
            "before_text": "毁神来了",
            "after_text": "毁神来了",
        },
        {
            "cue_index": 2,
            "start_ms": 2_000,
            "end_ms": 4_000,
            "before_text": "毁神什么都没做",
            "after_text": "毁神什么都没做",
        },
    ]


def test_mention_postconditions_never_mutate_unowned_parent_cue(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "one-owned-mention-in-broad-parent",
                "recording_basename": "recording.mp4",
                "source_start_ms": 100_000,
                "source_end_ms": 104_000,
                "action": "replace_substring",
                "replacements": [
                    {"surface": "绘声", "canonical": "毁神"}
                ],
                "required_text": "毁神",
                "mention_postconditions": [
                    {
                        "source_start_ms": 100_000,
                        "source_end_ms": 102_000,
                        "required_text": "毁神",
                        "forbidden_tokens": ["绘声"],
                    }
                ],
                "required": True,
            }
        ],
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 2_000, "毁神已正确"),
            (2_000, 4_000, "绘声未审定"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 104_000,
                }
            ]
        },
        durations=[4_000],
        ledger_path=ledger,
    )

    assert "毁神已正确" in corrected
    assert "绘声未审定" in corrected
    assert "毁神未审定" not in corrected
    assert audit["status"] == "ALREADY_SATISFIED"
    row = audit["satisfied"][0]
    assert row["mention_owner_resolution"] == {
        "status": "PASS",
        "cue_indexes": [1],
        "failure_reason_codes": [],
    }
    assert row.get("replacements") in (None, [])
    assert [
        cue["cue_index"]
        for cue in row["resolved_target_projection"]["cues"]
    ] == [1]


def test_mention_postconditions_reject_one_cue_owning_two_mentions(tmp_path):
    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "same-name-twice-merged",
                "recording_basename": "recording.mp4",
                "source_start_ms": 100_000,
                "source_end_ms": 104_000,
                "action": "replace_substring",
                "replacements": [
                    {"surface": "绘声", "canonical": "毁神"}
                ],
                "required_text": "毁神",
                "mention_postconditions": [
                    {
                        "source_start_ms": 100_000,
                        "source_end_ms": 102_000,
                        "required_text": "毁神",
                        "forbidden_tokens": [],
                    },
                    {
                        "source_start_ms": 102_000,
                        "source_end_ms": 104_000,
                        "required_text": "毁神",
                        "forbidden_tokens": [],
                    },
                ],
                "required": True,
            }
        ],
    )
    _corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 4_000, "毁神说完，毁神又说"),),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/recording.mp4",
                    "start_ms": 100_000,
                    "end_ms": 104_000,
                }
            ]
        },
        durations=[4_000],
        ledger_path=ledger,
    )

    assert audit["status"] == "FAILED"
    assert {
        (
            row["reason_code"],
            row["mention_ordinal"],
        )
        for row in audit["failures"]
        if row.get("mention_ordinal") is not None
    } == {
        ("MENTION_POSTCONDITION_TARGET_NOT_ISOLATED", 1),
        ("MENTION_POSTCONDITION_TARGET_NOT_ISOLATED", 2),
    }


def test_committed_huishen_mentions_are_independent_exact_owners():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 1_370, "好像是毁神吧"),
            (1_370, 3_530, "毁神说救救李姐"),
            (3_530, 5_980, "然后大N老师就来了"),
            (5_980, 8_600, "然后毁神什么都没有做"),
            (8_600, 11_560, "但是大N老师拯救完李姐之后"),
            (11_560, 14_120, "毁神发了一句"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 748_620,
                    "end_ms": 762_740,
                }
            ]
        },
        durations=[14_120],
        ledger_path=ledger,
    )

    assert "绘声" not in corrected
    assert audit["status"] == "ALREADY_SATISFIED"
    row = next(
        row
        for row in audit["satisfied"]
        if row["truth_id"]
        == "20260722-nancho-confrontation-huishen-surface"
    )
    assert row["mention_owner_resolution"]["status"] == "PASS"
    assert row["mention_owner_resolution"]["cue_indexes"] == [1, 2, 4, 6]
    assert [
        cue["cue_index"]
        for cue in row["resolved_target_projection"]["cues"]
    ] == [1, 2, 4, 6]


def test_committed_ledger_repairs_hotpot_parallel_repeat_entity_phrase():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 1_610, "小李又被大哥骂赢了")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 2_001_780,
                    "end_ms": 2_003_390,
                }
            ]
        },
        durations=[1_610],
        ledger_path=ledger,
    )

    assert "小李又被大N霸凌了" in corrected
    assert "大哥骂赢" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == (
        "20260722-nancho-hotpot-dan-bullying-r1"
    )


def test_committed_ledger_repairs_hotpot_spoken_letter_name_to_canonical_entity():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 3_460, "哪里又变成小李被大大恩霸凌了")),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/22966160_20260722-19-35-15.mp4",
                    "start_ms": 2_009_220,
                    "end_ms": 2_012_680,
                }
            ]
        },
        durations=[3_460],
        ledger_path=ledger,
    )

    assert "哪里又变成小李被大N霸凌了" in corrected
    assert "大大恩" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == (
        "20260722-nancho-hotpot-dan-orthography-callback-r1"
    )


def test_committed_ledger_repairs_chair_bullying_phrase_across_bad_split():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 1_240, "感觉像被留了"),
            (1_240, 2_480, "像霸凌"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/22966160_20260722-19-35-15.mp4",
                    "start_ms": 1_597_960,
                    "end_ms": 1_600_440,
                }
            ]
        },
        durations=[2_480],
        ledger_path=ledger,
    )

    assert "".join(cue.text for cue in parse_srt_cues(corrected)) == "感觉像被豆沙霸凌"
    assert "被留了" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == (
        "20260722-nancho-chair-bullying-phrase-r1"
    )


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "draft", "expected", "truth_id"),
    [
        (
            1_580_300,
            1_581_980,
            "然后我在坐做一个",
            "然后我就坐这一个",
            "20260722-nancho-chair-seat-phrase-r1",
        ),
        (
            1_642_350,
            1_644_920,
            "我真违心啊",
            "我真的很有型啊",
            "20260722-nancho-chair-youxing-r1",
        ),
    ],
)
def test_committed_ledger_repairs_v13_hash_bound_chair_acoustic_findings(
    start_ms, end_ms, draft, expected, truth_id
):
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, end_ms - start_ms, draft)),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
            ]
        },
        durations=[end_ms - start_ms],
        ledger_path=ledger,
    )

    assert "".join(cue.text for cue in parse_srt_cues(corrected)) == expected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == truth_id


def test_real_1475_retry_witness_widening_does_not_absorb_1573_owners():
    """Widened witness context keeps four brainflick owners, but repairs chair."""

    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    replay_sha256 = (
        "sha256:"
        "0eb2778dc53e5eabbccae089e5db92d3fb3662d90e1dd2ddbe7765436718989a"
    )
    base_spec = {
        "candidate_id": "auto_193450_1475_1543",
        "semantic_start_ms": 1_475_980,
        "semantic_end_ms": 1_543_290,
        "given_end_ms": 1_543_760,
        "pieces": [
            {
                "remote_media": (
                    "/recovery/22966160_20260722-19-34-50.mp4"
                ),
                "source_media_sha256": replay_sha256,
                "start_ms": 1_475_750,
                "end_ms": 1_560_000,
            }
        ],
    }
    widened_spec = deepcopy(base_spec)
    widened_spec["pieces"][0]["end_ms"] = 1_650_000

    initial = ledger_required_owner_contracts(
        spec=base_spec,
        durations=[84_250],
        ledger_path=ledger,
    )
    widened = ledger_required_owner_contracts(
        spec=widened_spec,
        durations=[174_250],
        ledger_path=ledger,
    )
    initial_projection = [
        (
            row["owner_id"],
            row["source_start_ms"],
            row["source_end_ms"],
            row["local_windows"],
        )
        for row in initial
    ]
    widened_projection = [
        (
            row["owner_id"],
            row["source_start_ms"],
            row["source_end_ms"],
            row["local_windows"],
        )
        for row in widened
    ]

    # 2026-07-25: the 管他谁是左边的呢 idiom truth sits inside 1475's story
    # scope and legitimately joins the owner set (4 brainflick + 1 idiom).
    assert len(initial_projection) == 5
    assert widened_projection == initial_projection
    # 1573's truths (chair bullying phrase, 贝利) stay context-only: they sit
    # inside the widened witness window but outside 1475's immutable scope.
    assert all("chair" not in str(row["owner_id"]) for row in widened)
    assert all("beili" not in str(row["owner_id"]) for row in widened)

    corrected, audit = apply_source_subtitle_truth(
        (
            "1\n"
            "00:02:02,210 --> 00:02:04,690\n"
            "感觉像被留了像霸凌\n"
        ),
        spec=widened_spec,
        durations=[174_250],
        ledger_path=ledger,
    )
    assert "感觉像被豆沙霸凌" in corrected
    assert any(
        row["truth_id"]
        == "20260722-nancho-chair-bullying-phrase-r1"
        for row in audit["applied"]
    )


@pytest.mark.parametrize(
    ("start_ms", "end_ms", "draft", "expected"),
    [
        (3_625_860, 3_627_420, "就是她喜欢打工人", "就是她使唤的打工人"),
        (1_911_690, 1_913_880, "谢谢南家星耀的SC", "谢谢南町家的星耀的SC"),
        (1_942_140, 1_946_210, "谢谢刚刚 PANJA 的舰长", "谢谢刚刚panoja的舰长"),
        (1_999_620, 2_001_780, "谢谢小路路口的钢镚", "谢谢小凑るう子的钢镚"),
        (2_062_300, 2_064_990, "香香烧烤拿烟头烫的好", "邪恶守宫拿烟头烫的好"),
        (
            732_210,
            735_610,
            "泉水之，就是之前1V1的时候",
            "泉水之恩就是之前1V1的时候",
        ),
        (1_961_200, 1_962_320, "谢哥不互动", "邪恶守宫"),
    ],
)
def test_committed_ledger_preserves_new_acoustic_and_entity_truths(
    start_ms, end_ms, draft, expected
):
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, end_ms - start_ms, draft)),
        spec={
            "pieces": [
                {
                    "remote_media": "/recordings/22966160_20260722-19-35-15.mp4",
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                }
            ]
        },
        durations=[end_ms - start_ms],
        ledger_path=ledger,
    )

    assert expected in corrected
    assert draft not in corrected
    assert audit["status"] == "APPLIED"


def test_committed_ledger_drops_full_post_nightin_silence_hallucination():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 4_140, "嗯，LLNNHHB，是这个"),
            (4_140, 6_770, "这个L是李乐莎的L吗"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 776_470,
                    "end_ms": 783_240,
                }
            ]
        },
        durations=[6_770],
        ledger_path=ledger,
    )

    assert corrected == ""
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == (
        "20260722-nancho-confrontation-drop-hallucinated-formula-r4"
    )
    assert {
        row["truth_id"] for row in audit["inactive"]
    } >= {
        "20260722-nancho-confrontation-latin-formula-positive-r1",
        "20260722-nancho-confrontation-drop-hallucinated-formula-r2",
        "20260722-nancho-confrontation-drop-hallucinated-formula-r3",
    }


def test_committed_ledger_preserves_real_opening_speech_after_silent_prefix():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 2_560, "这不是主播最最最最喜欢的南町nightin吗，llnnhhb"),
            (2_560, 5_120, "你为什么在说我的时候要最最最最喜欢，草"),
            (5_120, 8_150, "我草，乱说的啊"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 672_920,
                    "end_ms": 681_070,
                }
            ]
        },
        durations=[8_150],
        ledger_path=ledger,
    )

    cues = parse_srt_cues(corrected)
    assert [cue.text for cue in cues] == [
        "这不是主播最最最最喜欢",
        "你为什么在说我的时候要最最最最喜欢",
        "乱说的啊",
    ]
    assert cues[-1].start_ms == 6_050
    assert "草" not in corrected
    assert audit["status"] == "APPLIED"
    assert {
        row["truth_id"] for row in audit["applied"]
    } >= {
        "20260722-nancho-confrontation-opening-human-r2",
        "20260722-nancho-confrontation-no-cao-response-r1",
        "20260722-nancho-confrontation-silent-prefix",
    }


def test_committed_ledger_repairs_qin_heterosexual_pun():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (0, 2_920, "这直播间这很非常包容"),
            (2_900, 5_940, "其实是最包容一系列的直播间"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 3_659_910,
                    "end_ms": 3_665_850,
                }
            ]
        },
        durations=[5_940],
        ledger_path=ledger,
    )

    assert "这直播间还是非常包容" in corrected
    assert "这很非常包容" not in corrected
    assert "其实是最包容异性恋的直播间" in corrected
    assert "一系列" not in corrected
    assert audit["status"] == "APPLIED"


def test_committed_ledger_repairs_first_jieganmei_occurrence():
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 2_620, "她是一个桔梗妹")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 3_594_460,
                    "end_ms": 3_597_080,
                }
            ]
        },
        durations=[2_620],
        ledger_path=ledger,
    )

    assert "她是一个姐感妹" in corrected
    assert "桔梗妹" not in corrected
    assert audit["status"] == "APPLIED"
    assert audit["applied"][0]["truth_id"] == (
        "20260722-qin-jieganmei-full-clause-r1"
    )


@pytest.mark.parametrize("asr_surface", ["陆医生", "露蒂丝", "露蒂斯"])
def test_committed_ledger_repairs_hotpot_lu_doctor_surface_family(asr_surface):
    ledger = (
        Path(__file__).resolve().parents[1]
        / "assets"
        / "lidousha"
        / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms((0, 4_020, f"对哇，你说这句话跟{asr_surface}好像啊")),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260722-19-35-15.mp4"
                    ),
                    "start_ms": 2_016_790,
                    "end_ms": 2_020_810,
                }
            ]
        },
        durations=[4_020],
        ledger_path=ledger,
    )

    assert "跟露医生好像啊" in corrected
    assert asr_surface not in corrected or asr_surface == "露医生"
    assert audit["status"] == "APPLIED"


def test_misheard_cue_joins_dominion_via_pinyin(tmp_path):
    """672 案形态一（2026-07-27）：「南町nightin」被本轮听成「难听难听」，
    字符零共通但语音同一——拼音相似度使其保留辖区成员资格，钉文重分配
    落地，误听残渣清除。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "nancho-favorite",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 117_000,
                "action": "replace_cue",
                "text": "最最最最喜欢的南町nightin",
                "required": True,
            }
        ],
    )
    srt = (
        "1\n00:00:10,000 --> 00:00:13,000\n呃，就搜寻到了最最最最喜欢的\n\n"
        "2\n00:00:13,000 --> 00:00:16,800\n难听难听\n"
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "APPLIED", audit["failures"]
    assert not audit["failures"]
    assert "南町nightin" in corrected
    assert "难听" not in corrected


def test_repeated_variant_window_containment_admission(tmp_path):
    """672 案形态二：她把同一句说了两遍变体（礼太多了/这个礼有点太多了），
    两 cue 都完整在真值窗内——窗口时间即裁定辖区，低字符相似不阻止钉文
    落地；admission 在 applied 行披露。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "too-many-li",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 116_200,
                "action": "replace_cue",
                "text": "李太多了哈",
                "required": True,
            }
        ],
    )
    srt = (
        "1\n00:00:10,000 --> 00:00:13,000\n哈哈哈，礼太多了\n\n"
        "2\n00:00:13,000 --> 00:00:16,000\n哈哈哈，这个礼有点太多了\n"
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert audit["status"] == "APPLIED", audit["failures"]
    assert not audit["failures"]
    row = audit["applied"][0]
    assert row.get("window_containment_admission") is True
    text_only = "".join(
        line
        for line in corrected.splitlines()
        if line and not line[0].isdigit()
    )
    assert "李太多了哈" in text_only.replace("，", "").replace("。", "")
    assert "这个礼有点" not in corrected


def test_straddling_cue_blocks_containment_admission(tmp_path):
    """邻句保护不动摇：第二 cue 伸出真值窗（包含度<0.9）且文本相似不过
    0.55 线时，照旧 REPLACE_CUE_TARGET_NOT_UNIQUE 失败，窗外语音分毫不动。"""

    ledger = _ledger(
        tmp_path,
        [
            {
                "knowledge_type": "SOURCE_INTERVAL_TRUTH",
                "truth_id": "straddle-guard",
                "recording_basename": "recording.mp4",
                "source_start_ms": 110_000,
                "source_end_ms": 116_000,
                "action": "replace_cue",
                "text": "李太多了哈",
                "required": True,
            }
        ],
    )
    srt = (
        "1\n00:00:10,000 --> 00:00:13,000\n哈哈哈，礼太多了\n\n"
        "2\n00:00:13,000 --> 00:00:18,000\n哈哈哈，这个礼有点太多了顺便说下一件事\n"
    )
    corrected, audit = apply_source_subtitle_truth(
        srt,
        spec={"pieces": [{"remote_media": "/x/recording.mp4", "start_ms": 100_000, "end_ms": 130_000}]},
        durations=[30_000],
        ledger_path=ledger,
    )
    assert audit["failures"], audit
    assert audit["failures"][0]["reason_code"] == "REPLACE_CUE_TARGET_NOT_UNIQUE"
    assert "顺便说下一件事" in corrected


def test_committed_1209_truth_survives_fresh_asr_cue_merge():
    """1209 fresh ASR 可把礼墨句并进相邻 cue；operator truth 仍应唯一落刀。"""

    ledger = (
        REPO_ROOT / "assets" / "lidousha" / "subtitle_truth_ledger.v1.json"
    )
    corrected, audit = apply_source_subtitle_truth(
        _srt_ms(
            (34_290, 35_410, "什么？"),
            (36_730, 39_290, "这对吗？现那就差柠檬没吃了"),
        ),
        spec={
            "pieces": [
                {
                    "remote_media": (
                        "/recordings/22966160_20260724-18-31-22.mp4"
                    ),
                    "start_ms": 1_199_070,
                    "end_ms": 1_239_080,
                }
            ]
        },
        durations=[40_010],
        ledger_path=ledger,
    )

    assert [cue.text for cue in parse_srt_cues(corrected)] == [
        "神了，这对吗",
        "切，那就差礼墨没吃了",
    ]
    assert audit["status"] == "APPLIED"
    assert not audit["failures"]
    assert {
        row["truth_id"] for row in audit["applied"]
    } >= {
        "20260724-beans-shenle-r2",
        "20260724-beans-limo-first-repeat-r2",
    }
