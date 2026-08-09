import copy
import inspect
import json

import pytest

from src.autoslice.song_name_semantic_verification import (
    MappingSongLyricsProvider,
    SongLyricsLookupResult,
    SongNameSemanticVerificationError,
    build_song_name_semantic_verification,
    collect_song_semantic_evidence,
    validate_song_name_semantic_verification,
    verify_and_pin_song_names,
)
from src.autoslice import producer_text_pipeline


def _srt(*texts: str) -> str:
    return "\n\n".join(
        f"{index}\n00:00:0{index},000 --> 00:00:0{index + 1},000\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


def test_receipt_binds_candidate_evidence_match_surface_and_verdict():
    srt = _srt("我来说一下这首歌", "请感受穿越屏幕的热烈", "再一次爱上我吧")
    candidates = ["虚构星光码", "虚构舞台企划"]
    receipt = build_song_name_semantic_verification(
        srt,
        candidates=candidates,
        evidence_lines=collect_song_semantic_evidence(
            srt,
            title_quote="请感受穿越屏幕的热烈,再一次爱上我吧",
        ),
        local_provider=MappingSongLyricsProvider(
            {
                "虚构星光码": ["请感受穿越屏幕的热烈，再一次爱上我吧"],
                "虚构舞台企划": ["大家在虚构舞台挥手告别"],
            }
        ),
    )

    assert receipt["schema_version"] == "song-name-semantic-verification.v1"
    assert receipt["preferred_candidate"] == "虚构星光码"
    rows = {row["candidate"]: row for row in receipt["candidate_results"]}
    assert rows["虚构星光码"]["verdict"] == "MATCH"
    assert rows["虚构星光码"]["matched_surfaces"][0]["surface"]
    assert rows["虚构舞台企划"]["verdict"] == "DISPUTED"


def test_external_provider_is_typed_but_never_called_by_default():
    class ExplodingExternalProvider:
        provider_name = "synthetic_external"

        def lookup(self, request):
            raise AssertionError(f"default path called external provider for {request.candidate}")

    srt = _srt("下一首歌是虚构星光码")
    receipt = build_song_name_semantic_verification(
        srt,
        candidates=["虚构星光码"],
        evidence_lines=[
            {"source": "selection_hook", "cue_index": None, "text": "穿越屏幕的热烈"}
        ],
        local_provider=MappingSongLyricsProvider({}),
        external_provider=ExplodingExternalProvider(),
    )

    assert receipt["candidate_results"][0]["verdict"] == "LYRICS_UNAVAILABLE"
    assert receipt["verification_requests"] == [
        {
            "schema_version": "song-name-lyrics-verification-request.v1",
            "candidate": "虚构星光码",
            "purpose": "talk_song_name_semantic_verification",
            "local_lookup_status": "NOT_FOUND",
            "external_lookup_allowed": False,
            "external_provider": "synthetic_external",
            "status": "DISABLED_BY_DEFAULT",
        }
    ]


def test_receipt_hash_tamper_is_rejected():
    srt = _srt("下一首歌是虚构星光码")
    candidates = ["虚构星光码"]
    receipt = build_song_name_semantic_verification(
        srt,
        candidates=candidates,
        evidence_lines=[
            {"source": "title_quote", "cue_index": None, "text": "穿越屏幕的热烈光芒"}
        ],
        local_provider=MappingSongLyricsProvider(
            {"虚构星光码": ["穿越屏幕的热烈光芒再次抵达心底"]}
        ),
    )
    tampered = copy.deepcopy(receipt)
    tampered["candidate_results"][0]["verdict"] = "DISPUTED"

    with pytest.raises(SongNameSemanticVerificationError, match="hash mismatch"):
        validate_song_name_semantic_verification(
            tampered,
            srt_text=srt,
            candidates=candidates,
        )


def test_production_helper_uses_synthetic_local_catalog_and_writes_no_network(tmp_path):
    quote = "请感受穿越屏幕的热烈,再一次爱上我吧"
    known_songs = tmp_path / "known_songs.json"
    known_songs.write_text(
        json.dumps(
            {
                "schema_version": "synthetic-known-songs.v1",
                "songs": [
                    {"title": "ラブコード", "fingerprint": [quote]},
                    {"title": "LoveLive!", "fingerprint": ["虚构舞台挥手告别"]},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    srt = _srt("下一首歌是LoveLive!")

    output, audit, receipt = verify_and_pin_song_names(
        srt,
        candidates=["ラブコード", "LoveLive!"],
        known_songs_path=known_songs,
        title_quote=quote,
    )

    assert "《ラブコード》" in output
    assert audit["semantic_gate_status"] == "VERIFIED_UNIQUE_MATCH"
    assert receipt["verification_requests"] == []


def test_f19_producer_wiring_canary_persists_verification_before_pin_audit():
    """Canary: deleting the production choke-point call or sidecar makes this red."""

    source = inspect.getsource(producer_text_pipeline._finalize_text_evidence)

    assert "verify_and_pin_song_names(" in source
    assert '("song-name-semantic-verification", semantic_verification)' in source
    assert source.index("verify_and_pin_song_names(") < source.index(
        '("song-name-pin", song_name_pin_audit)'
    )


def test_explicit_external_provider_can_only_run_when_opted_in():
    class SyntheticExternalProvider:
        provider_name = "synthetic_external"

        def lookup(self, request):
            return SongLyricsLookupResult(status="NOT_FOUND", reason="synthetic miss")

    receipt = build_song_name_semantic_verification(
        _srt("下一首歌是虚构星光码"),
        candidates=["虚构星光码"],
        evidence_lines=[],
        local_provider=MappingSongLyricsProvider({}),
        external_provider=SyntheticExternalProvider(),
        allow_external_lookup=True,
    )

    assert receipt["verification_requests"][0]["external_lookup_allowed"] is True
    assert receipt["verification_requests"][0]["status"] == "CALLED"
