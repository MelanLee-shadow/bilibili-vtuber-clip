import hashlib
import json
from pathlib import Path

import pytest

from scripts.apply_speaker_turn_overrides import Cue, sha256_file, write_srt
from scripts.apply_subtitle_text_overrides import TextCue
from src.autoslice import reviewed_speaker_baseline as reviewed_baseline_module
from src.autoslice import speaker_finalizer
from src.autoslice.reviewed_speaker_baseline import (
    REVIEWED_SPEAKER_BASELINE_SCHEMA,
    load_reviewed_speaker_baseline,
    materialize_reviewed_automatic_labels,
)
from src.autoslice.speaker_common import (
    GUEST_SPEAKER,
    HOST_SPEAKER,
    SpeakerFinalizationError,
)


AUTHORITY = "Ivan synthetic reviewed speaker fixture"
ARBITRATION_SHA = "a" * 64
CANDIDATE = "synthetic_candidate"


def _cues() -> list[TextCue]:
    return [
        TextCue(1, "00:00:00,000", "00:00:01,500", "主播一"),
        TextCue(2, "00:00:01,500", "00:00:03,100", "主播二"),
        TextCue(3, "00:00:03,100", "00:00:04,400", "待机器裁决"),
    ]


def _write_truth(root: Path) -> tuple[str, str]:
    path = root / "reports" / "synthetic.truth-diff.v2.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": "ivan-speaker-truth-diff.v2",
                "candidate_id": CANDIDATE,
                "cues": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(path.relative_to(root)), sha256_file(path)


def _override_row(cue: TextCue, speaker: str = HOST_SPEAKER) -> dict:
    return {
        "source_cue": cue.source_index,
        "expect": {"start": cue.start, "end": cue.end, "text": cue.text},
        "authority": AUTHORITY,
        "segments": [
            {
                "start": cue.start,
                "end": cue.end,
                "speaker": speaker,
                "text": cue.text,
            }
        ],
    }


def _document(root: Path) -> dict:
    cues = _cues()
    truth_path, truth_sha = _write_truth(root)
    automatic_path = root / "assets" / "synthetic.automatic-labelled.srt"
    automatic_path.parent.mkdir(exist_ok=True)
    write_srt(
        [
            Cue(
                source_index=cue.source_index,
                start=cue.start,
                end=cue.end,
                speaker=GUEST_SPEAKER,
                text=cue.text,
                decision_source="synthetic_machine",
            )
            for cue in cues
        ],
        automatic_path,
    )
    automatic_sha = sha256_file(automatic_path)
    return {
        "schema_version": 1,
        "candidate_id": CANDIDATE,
        "source_media_sha256": "b" * 64,
        "text_final_srt_sha256": "c" * 64,
        "source_srt_sha256": automatic_sha,
        "reviewed_speaker_baseline": {
            "schema_version": REVIEWED_SPEAKER_BASELINE_SCHEMA,
            "authority": AUTHORITY,
            "truth_input": {"path": truth_path, "sha256": truth_sha},
            "automatic_input": {
                "path": str(automatic_path.relative_to(root)),
                "sha256": automatic_sha,
            },
            "cue_count": len(cues),
            "anchor_source_cues": [1, 2],
            "machine_cues": [
                {
                    "source_cue": 3,
                    "expect": {
                        "start": cues[2].start,
                        "end": cues[2].end,
                        "text": cues[2].text,
                    },
                    "speaker": GUEST_SPEAKER,
                    "reason": "positive voice arbitration keeps machine ownership",
                    "arbitration": {
                        "human_voice_observed": True,
                        "receipt_sha256": ARBITRATION_SHA,
                    },
                }
            ],
        },
        "overrides": [_override_row(cues[0]), _override_row(cues[1])],
    }


def test_reviewed_baseline_binds_complete_partition_and_exact_host_anchors(
    tmp_path: Path,
) -> None:
    loaded = load_reviewed_speaker_baseline(
        _document(tmp_path),
        candidate_id=CANDIDATE,
        cues=_cues(),
        repo_root=tmp_path,
    )
    assert loaded is not None
    assert loaded.anchor_labels == {0: HOST_SPEAKER, 1: HOST_SPEAKER}
    assert loaded.machine_cues == (3,)
    assert loaded.machine_labels == {2: GUEST_SPEAKER}
    assert loaded.evidence["reviewed_cue_count"] == 2


def test_reviewed_speaker_baseline_rejects_labelled_text_surface(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    cues = _cues()
    labelled = "[李豆沙] 主播一"
    cues[0] = TextCue(1, cues[0].start, cues[0].end, labelled)
    document["overrides"][0]["expect"]["text"] = labelled
    document["overrides"][0]["segments"][0]["text"] = labelled

    with pytest.raises(
        SpeakerFinalizationError,
        match="BASELINE_CONTAINS_SPEAKER_LABEL_PREFIX",
    ):
        load_reviewed_speaker_baseline(
            document,
            candidate_id=CANDIDATE,
            cues=cues,
            repo_root=tmp_path,
        )


@pytest.mark.parametrize(
    "mutate, error",
    [
        (
            lambda document: document["reviewed_speaker_baseline"].update(
                {"cue_count": 4}
            ),
            "cue count drift",
        ),
        (
            lambda document: document["reviewed_speaker_baseline"].update(
                {"anchor_source_cues": [1]}
            ),
            "at least two",
        ),
        (
            lambda document: document["overrides"][0]["expect"].update(
                {"text": "漂移"}
            ),
            "text drift",
        ),
        (
            lambda document: document["reviewed_speaker_baseline"]["truth_input"].update(
                {"sha256": "0" * 64}
            ),
            "truth input hash drift",
        ),
        (
            lambda document: document["reviewed_speaker_baseline"].update(
                {"machine_cues": []}
            ),
            "cue partition drift",
        ),
        (
            lambda document: document["overrides"][0]["segments"][0].update(
                {"speaker": GUEST_SPEAKER}
            ),
            "exact full-cue",
        ),
    ],
)
def test_reviewed_baseline_rejects_drift_and_invalid_anchors(
    tmp_path: Path,
    mutate,
    error: str,
) -> None:
    document = _document(tmp_path)
    mutate(document)
    with pytest.raises(SpeakerFinalizationError, match=error):
        load_reviewed_speaker_baseline(
            document,
            candidate_id=CANDIDATE,
            cues=_cues(),
            repo_root=tmp_path,
        )


def test_reviewed_baseline_rejects_automatic_input_path_hash_and_grid_drift(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    automatic = document["reviewed_speaker_baseline"]["automatic_input"]
    automatic["path"] = "../escape.srt"
    with pytest.raises(SpeakerFinalizationError, match="repository-relative"):
        load_reviewed_speaker_baseline(
            document, candidate_id=CANDIDATE, cues=_cues(), repo_root=tmp_path
        )

    document = _document(tmp_path / "hash")
    document["reviewed_speaker_baseline"]["automatic_input"]["sha256"] = "0" * 64
    with pytest.raises(SpeakerFinalizationError, match="automatic input hash drift"):
        load_reviewed_speaker_baseline(
            document,
            candidate_id=CANDIDATE,
            cues=_cues(),
            repo_root=tmp_path / "hash",
        )

    grid_root = tmp_path / "grid"
    document = _document(grid_root)
    automatic_path = grid_root / document["reviewed_speaker_baseline"]["automatic_input"]["path"]
    automatic_path.write_text(
        automatic_path.read_text(encoding="utf-8").replace("待机器裁决", "漂移"),
        encoding="utf-8",
    )
    digest = sha256_file(automatic_path)
    document["source_srt_sha256"] = digest
    document["reviewed_speaker_baseline"]["automatic_input"]["sha256"] = digest
    with pytest.raises(SpeakerFinalizationError, match="cue 3 text drift"):
        load_reviewed_speaker_baseline(
            document,
            candidate_id=CANDIDATE,
            cues=_cues(),
            repo_root=grid_root,
        )


def test_reviewed_baseline_rejects_symlinked_automatic_input(tmp_path: Path) -> None:
    document = _document(tmp_path)
    relative = Path(
        document["reviewed_speaker_baseline"]["automatic_input"]["path"]
    )
    link = tmp_path / relative
    real = link.with_name("real.automatic-labelled.srt")
    link.rename(real)
    link.symlink_to(real.name)
    document["reviewed_speaker_baseline"]["automatic_input"]["sha256"] = sha256_file(real)
    document["source_srt_sha256"] = sha256_file(real)

    with pytest.raises(SpeakerFinalizationError, match="must not traverse symlinks"):
        load_reviewed_speaker_baseline(
            document, candidate_id=CANDIDATE, cues=_cues(), repo_root=tmp_path
        )


def test_v2_rejects_noncanonical_frozen_automatic_at_materialization(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    relative = Path(
        document["reviewed_speaker_baseline"]["automatic_input"]["path"]
    )
    automatic_path = tmp_path / relative
    automatic_path.write_bytes(
        automatic_path.read_text(encoding="utf-8").replace("\n", "\r\n").encode()
    )
    digest = sha256_file(automatic_path)
    document["source_srt_sha256"] = digest
    document["reviewed_speaker_baseline"]["automatic_input"]["sha256"] = digest
    loaded = load_reviewed_speaker_baseline(
        document,
        candidate_id=CANDIDATE,
        cues=_cues(),
        repo_root=tmp_path,
    )
    assert loaded is not None and loaded.frozen_automatic is not None
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with pytest.raises(SpeakerFinalizationError, match="canonical SHA-256 drift"):
        materialize_reviewed_automatic_labels(
            list(loaded.frozen_automatic),
            work_dir=work_dir,
            baseline=loaded,
        )


def test_v1_reviewed_baseline_retains_strict_whole_automatic_hash_gate(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    baseline = document["reviewed_speaker_baseline"]
    baseline["schema_version"] = "reviewed-speaker-baseline.v1"
    baseline.pop("automatic_input")
    baseline["machine_cues"][0].pop("speaker")
    loaded = load_reviewed_speaker_baseline(
        document,
        candidate_id=CANDIDATE,
        cues=_cues(),
        repo_root=tmp_path,
    )
    assert loaded is not None and loaded.frozen_automatic is None
    work = tmp_path / "v1-work"
    work.mkdir()
    with pytest.raises(SpeakerFinalizationError, match="source hash mismatch"):
        speaker_finalizer._materialize_speaker_labels(
            {
                "decisions": [
                    {
                        "speaker": HOST_SPEAKER,
                        "decision_source": "synthetic_machine",
                    }
                    for _cue in _cues()
                ]
            },
            cues=_cues(),
            work_dir=work,
            override_document=document,
            expected_automatic_sha256=str(document["source_srt_sha256"]),
            reviewed_baseline=loaded,
        )


def test_reviewed_host_anchors_rescue_a_clip_with_zero_profile_anchors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cues = _cues()
    media = tmp_path / "media.mp4"
    media.write_bytes(b"media")
    cue_paths = []
    for index in range(len(cues)):
        path = tmp_path / f"cue-{index}.wav"
        path.write_bytes(str(index).encode())
        cue_paths.append(path)
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    monkeypatch.setattr(
        speaker_finalizer,
        "_load_runtime",
        lambda *_args, **_kwargs: ({}, [{"path": reference}], "model-hash", object()),
    )
    monkeypatch.setattr(
        speaker_finalizer,
        "_policy",
        lambda _profile: {"host_session_anchor_count": 4, "host_session_seed_min": 0.68},
    )
    monkeypatch.setattr(
        speaker_finalizer,
        "_extract_cue_wavs",
        lambda *_args, **_kwargs: (object(), 16_000, cue_paths),
    )
    monkeypatch.setattr(
        speaker_finalizer,
        "_build_embedding_similarity",
        lambda **_kwargs: (lambda _left, _right: 0.1),
    )

    with pytest.raises(SpeakerFinalizationError, match="not enough"):
        speaker_finalizer._prepare_campplus_anchor_state(
            media_path=media,
            cues=cues,
            profile_path=tmp_path / "profile.json",
            reference_dir=tmp_path,
            model_dir=tmp_path,
            work_dir=tmp_path / "without-review",
            source_session_anchor_path=None,
        )

    state = speaker_finalizer._prepare_campplus_anchor_state(
        media_path=media,
        cues=cues,
        profile_path=tmp_path / "profile.json",
        reference_dir=tmp_path,
        model_dir=tmp_path,
        work_dir=tmp_path / "with-review",
        source_session_anchor_path=None,
        reviewed_anchor_labels={0: HOST_SPEAKER, 1: HOST_SPEAKER},
        reviewed_speaker_baseline={"authority": AUTHORITY},
    )
    assert state.clip_host_indices == []
    assert state.host_indices == [0, 1]
    assert state.host_anchor_scope == "reviewed_speaker_baseline"


def _write_text_srt(path: Path, cues: list[TextCue]) -> None:
    path.write_text(
        "\n\n".join(
            f"{cue.source_index}\n{cue.start} --> {cue.end}\n{cue.text}"
            for cue in cues
        )
        + "\n",
        encoding="utf-8",
    )


def test_reviewed_rows_override_analyzer_but_machine_owned_cue_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cues = _cues()
    media = tmp_path / "media.mp4"
    media.write_bytes(b"synthetic media")
    text_srt = tmp_path / "text.srt"
    _write_text_srt(text_srt, cues)
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "model").mkdir()

    automatic_path = tmp_path / "expected-automatic.srt"
    write_srt(
        [
            Cue(
                source_index=cue.source_index,
                start=cue.start,
                end=cue.end,
                speaker=GUEST_SPEAKER,
                text=cue.text,
                decision_source="synthetic_machine",
            )
            for cue in cues
        ],
        automatic_path,
    )
    document = _document(tmp_path)
    document.update(
        source_media_sha256=sha256_file(media),
        text_final_srt_sha256=sha256_file(text_srt),
        source_srt_sha256=sha256_file(automatic_path),
    )
    override = tmp_path / "override.json"
    override.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(reviewed_baseline_module, "REPO_ROOT", tmp_path)

    def analyzer(**kwargs):
        assert kwargs["reviewed_anchor_labels"] == {
            0: HOST_SPEAKER,
            1: HOST_SPEAKER,
        }
        assert kwargs["reviewed_speaker_baseline"]["machine_cues"] == [3]
        return {
            "host_anchor_scope": "reviewed_speaker_baseline",
            "context_unresolved_cues": [],
            "decisions": [
                {
                    "speaker": speaker,
                    "decision_source": "synthetic_machine",
                }
                for speaker in (HOST_SPEAKER, HOST_SPEAKER, GUEST_SPEAKER)
            ],
        }

    output_srt = tmp_path / "speaker.srt"
    manifest = speaker_finalizer.finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "references",
        model_dir=tmp_path / "model",
        output_srt_path=output_srt,
        output_ass_path=tmp_path / "speaker.ass",
        output_manifest_path=tmp_path / "speaker.json",
        work_dir=tmp_path / "work",
        candidate_id=CANDIDATE,
        override_path=override,
        analyzer=analyzer,
    )
    output = output_srt.read_text(encoding="utf-8")
    assert f"[{HOST_SPEAKER}] 主播一" in output
    assert f"[{HOST_SPEAKER}] 主播二" in output
    assert f"[{GUEST_SPEAKER}] 待机器裁决" in output
    assert manifest["reviewed_output_cue_count"] == 2
    assert manifest["host_anchor_scope"] == "reviewed_speaker_baseline"

    def machine_drift(**kwargs):
        result = analyzer(**kwargs)
        result["decisions"][-1]["speaker"] = HOST_SPEAKER
        return result

    drift_output = tmp_path / "drift.speaker.srt"
    drift_manifest = speaker_finalizer.finalize_speaker_subtitles(
        media_path=media,
        text_srt_path=text_srt,
        profile_path=profile,
        reference_dir=tmp_path / "references",
        model_dir=tmp_path / "model",
        output_srt_path=drift_output,
        output_ass_path=tmp_path / "drift.speaker.ass",
        output_manifest_path=tmp_path / "drift.speaker.json",
        work_dir=tmp_path / "drift-work",
        candidate_id=CANDIDATE,
        override_path=override,
        analyzer=machine_drift,
    )
    assert drift_output.read_bytes() == output_srt.read_bytes()
    replay = drift_manifest["analysis"]["reviewed_machine_baseline_replay"]
    assert replay["status"] == "FROZEN_MACHINE_BASELINE_APPLIED"
    assert replay["machine_cue_label_drift"] == [
        {
            "source_cue": 3,
            "frozen_speaker": GUEST_SPEAKER,
            "fresh_speaker": HOST_SPEAKER,
        }
    ]


def test_unresolved_machine_owned_cue_still_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cues = _cues()
    media = tmp_path / "media.mp4"
    media.write_bytes(b"synthetic media")
    text_srt = tmp_path / "text.srt"
    _write_text_srt(text_srt, cues)
    profile = tmp_path / "profile.json"
    profile.write_text("{}", encoding="utf-8")
    (tmp_path / "references").mkdir()
    (tmp_path / "model").mkdir()
    automatic_path = tmp_path / "expected-automatic.srt"
    write_srt(
        [
            Cue(
                source_index=cue.source_index,
                start=cue.start,
                end=cue.end,
                speaker=GUEST_SPEAKER,
                text=cue.text,
                decision_source="synthetic_machine",
            )
            for cue in cues
        ],
        automatic_path,
    )
    document = _document(tmp_path)
    document.update(
        source_media_sha256=sha256_file(media),
        text_final_srt_sha256=sha256_file(text_srt),
        source_srt_sha256=sha256_file(automatic_path),
    )
    override = tmp_path / "override.json"
    override.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(reviewed_baseline_module, "REPO_ROOT", tmp_path)

    def analyzer(**_kwargs):
        return {
            "context_unresolved_cues": [3],
            "decisions": [
                {
                    "speaker": GUEST_SPEAKER,
                    "decision_source": "synthetic_machine",
                }
                for _cue in cues
            ],
        }

    with pytest.raises(SpeakerFinalizationError, match="did not resolve.*3"):
        speaker_finalizer.finalize_speaker_subtitles(
            media_path=media,
            text_srt_path=text_srt,
            profile_path=profile,
            reference_dir=tmp_path / "references",
            model_dir=tmp_path / "model",
            output_srt_path=tmp_path / "speaker.srt",
            output_ass_path=tmp_path / "speaker.ass",
            output_manifest_path=tmp_path / "speaker.json",
            work_dir=tmp_path / "work",
            candidate_id=CANDIDATE,
            override_path=override,
            analyzer=analyzer,
        )
