import json
from pathlib import Path

import pytest

import scripts.free_session_autoslice as runner
from src.autoslice.chat_evidence import build_human_text_entity_verifier
from scripts.apply_subtitle_text_overrides import (
    TextCue,
    apply_overrides,
    decision_output_witness_sha256,
    source_cue_witness_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
CID = "auto_210624_656_909"
JAPANESE = "特別して 特別してー あー ちょー あ あ あ あ わたし"


def _asset() -> dict:
    return json.loads(
        (
            ROOT
            / "assets/lidousha/subtitle_text_overrides"
            / f"{CID}.text.v1.json"
        ).read_text(encoding="utf-8")
    )


def _source_cues() -> list[TextCue]:
    cues = [
        TextCue(index, "00:00:00,000", "00:00:00,100", f"unchanged-{index}")
        for index in range(1, 51)
    ]
    cues[48] = TextCue(49, "00:01:46,770", "00:01:48,330", "我看一下脱口杯")
    cues[49] = TextCue(50, "00:01:48,330", "00:01:52,880", JAPANESE)
    return cues


def test_210624_operator_truth_replays_only_mandarin_cue_and_locks_japanese() -> None:
    asset = _asset()
    source = _source_cues()

    assert source_cue_witness_sha256(source, asset) == asset["source_cue_witness_sha256"]
    assert (
        decision_output_witness_sha256(source, asset)
        == asset["decision_output_witness_sha256"]
    )

    replayed, decisions = apply_overrides(source, asset)
    assert [cue.text for cue in replayed[:48]] == [cue.text for cue in source[:48]]
    assert replayed[48].text == "我看一下。"
    assert replayed[49].text == JAPANESE
    assert replayed[49].text.encode("utf-8") == source[49].text.encode("utf-8")
    assert [decision["source"]["source_index"] for decision in decisions] == [49, 50]


def test_210624_operator_truth_fails_closed_on_target_or_japanese_cue_drift() -> None:
    asset = _asset()
    drifted_target = _source_cues()
    drifted_target[48] = TextCue(49, "00:01:46,770", "00:01:48,330", "我看一下动作呗")
    with pytest.raises(ValueError, match="source cue 49 text drift"):
        apply_overrides(drifted_target, asset)

    drifted_japanese = _source_cues()
    drifted_japanese[49] = TextCue(50, "00:01:48,330", "00:01:52,880", "特別して")
    with pytest.raises(ValueError, match="source cue 50 text drift"):
        apply_overrides(drifted_japanese, asset)


def test_210624_truth_asset_is_candidate_local_and_no_upload_authority() -> None:
    asset = _asset()
    assert asset["schema_version"] == 4
    assert set(asset) == {
        "schema_version",
        "candidate_id",
        "source_cue_witness_sha256",
        "decision_output_witness_sha256",
        "upload",
        "overrides",
    }
    assert asset["candidate_id"] == CID
    assert asset["upload"] is False
    assert all(
        override["authority"]
        == "Ivan operator truth: 210那你说的是我看一下之后接的是日语， Tokubei joshite。"
        for override in asset["overrides"]
    )


def test_210624_truth_changes_only_its_own_talk_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "REPO_ROOT", ROOT)
    monkeypatch.setattr(runner, "pipeline_fingerprint", lambda: "sha256:" + "a" * 64)
    monkeypatch.delenv("AUTOSLICE_HUMAN_TRUTH_MODE", raising=False)

    assert runner.talk_pipeline_fingerprint(CID) != "sha256:" + "a" * 64
    assert runner.talk_pipeline_fingerprint("auto_unrelated_1_2") == (
        "sha256:" + "a" * 64
    )


def test_210624_no_upload_truth_is_accepted_by_chat_preflight_only_for_its_candidate() -> None:
    path = (
        ROOT
        / "assets/lidousha/subtitle_text_overrides"
        / f"{CID}.text.v1.json"
    )
    verifier = build_human_text_entity_verifier(path, candidate_id=CID)
    assert verifier({"evidence_id": "0" * 64}) is None
    with pytest.raises(ValueError, match="candidate_id mismatch"):
        build_human_text_entity_verifier(path, candidate_id="auto_wrong_1_2")


def test_subtitle_authority_recovery_fingerprint_tracks_only_candidate_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    truth_root = tmp_path / "assets/lidousha/subtitle_text_overrides"
    truth_root.mkdir(parents=True)
    ledger = tmp_path / "assets/lidousha/subtitle-truth-ledger.json"
    monkeypatch.setattr(runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        runner,
        "profile_asset_directory",
        lambda key: truth_root
        if key == "subtitle_text_overrides"
        else tmp_path / f"unused-{key}",
    )
    monkeypatch.setattr(
        runner,
        "profile_asset_file",
        lambda key: ledger
        if key == "subtitle_truth_ledger"
        else tmp_path / f"unused-{key}.json",
    )
    monkeypatch.delenv("AUTOSLICE_HUMAN_TRUTH_MODE", raising=False)

    target_before = runner.talk_failure_recovery_fingerprint(
        "subtitle_authority", CID
    )
    unrelated_before = runner.talk_failure_recovery_fingerprint(
        "subtitle_authority", "auto_unrelated_1_2"
    )
    override = truth_root / f"{CID}.text.v1.json"
    override.write_text('{"truth":"first"}\n', encoding="utf-8")

    target_after = runner.talk_failure_recovery_fingerprint(
        "subtitle_authority", CID
    )
    assert target_after != target_before
    assert runner.talk_failure_recovery_fingerprint(
        "subtitle_authority", "auto_unrelated_1_2"
    ) == unrelated_before

    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    withheld = runner.talk_failure_recovery_fingerprint("subtitle_authority", CID)
    override.write_text('{"truth":"changed but withheld"}\n', encoding="utf-8")
    assert (
        runner.talk_failure_recovery_fingerprint("subtitle_authority", CID)
        == withheld
    )
