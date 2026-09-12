import hashlib
import json
import subprocess

import pytest

from src.autoslice.fastlane_original_patch import (
    OriginalPatchError,
    ROLE,
    SCHEMA,
    SUFFIX,
    compile_original_patch,
    load_original_patch,
    original_release_problems,
)

CID = "auto_original_1"
RAW = (
    "1\n00:00:00,000 --> 00:00:01,000\n都怪 QQ 群哦\n\n"
    "2\n00:00:01,100 --> 00:00:02,000\n就管人家叫李姐李姐\n\n"
    "3\n00:00:02,100 --> 00:00:03,000\n这个QQ群真热闹\n"
).encode()


def recipe(raw=RAW):
    return {
        "schema_version": SCHEMA,
        "candidate_id": CID,
        "original_review_input": {
            "path": CID + ".original-review.srt",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "role": ROLE,
            "source_reference": "Synthetic exact original shown to reviewer",
        },
        "operator_directive": {
            "kind": "REVIEWER_OPERATOR",
            "evidence_ref": "Synthetic explicit report",
        },
        "patches": [
            {
                "cue": 1,
                "start_ms": 0,
                "end_ms": 1000,
                "before": "都怪 QQ 群哦",
                "after": "都怪kmx哦",
                "decision_kind": "REVIEWER_EXACT_CORRECTION",
                "evidence_ref": "Synthetic correction: this mention is kmx",
            }
        ],
    }


def test_only_named_mention_changes_preserves_other_names_and_real_group():
    after, proof = compile_original_patch(RAW, recipe())
    assert after == RAW.replace("都怪 QQ 群哦".encode(), "都怪kmx哦".encode())
    assert "李姐李姐".encode() in after and "这个QQ群真热闹".encode() in after
    assert proof["provider_calls"] == 0 and proof["timing_and_unlisted_bytes_preserved"]
    assert proof["unchanged_cue_count"] == 2
    assert proof["scope_plan"]["whole_clip_rerun_required"] is False
    assert proof["upload_authorized_by_receipt"] is False


def test_crlf_and_original_time_headers_preserved():
    raw = RAW.replace(b"\n", b"\r\n")
    after, _ = compile_original_patch(raw, recipe(raw))
    assert after == raw.replace("都怪 QQ 群哦".encode(), "都怪kmx哦".encode())


@pytest.mark.parametrize(
    "change",
    [
        "sha",
        "role",
        "time",
        "before",
        "duplicate",
        "cue_bool",
        "model_authority",
        "empty",
        "foreign_field",
        "drop_cue",
    ],
)
def test_rejects_authority_or_scope_drift(change):
    r = recipe()
    if change == "sha":
        r["original_review_input"]["sha256"] = "a" * 64
    elif change == "role":
        r["original_review_input"]["role"] = "DIAGNOSTIC_TRAINING"
    elif change == "time":
        r["patches"][0]["start_ms"] = 1
    elif change == "before":
        r["patches"][0]["before"] = "另一份重新识别的文本"
    elif change == "duplicate":
        r["patches"] *= 2
    elif change == "cue_bool":
        r["patches"][0]["cue"] = True
    elif change == "model_authority":
        r["patches"][0]["decision_kind"] = "ASR_SELF_APPROVED"
    elif change == "empty":
        r["patches"][0]["after"] = ""
    elif change == "foreign_field":
        r["allow_asr_rewrite"] = True
    elif change == "drop_cue":
        r["patches"][0]["after"] = "\n\n4\n00:00:03,000 --> 00:00:04,000\n新句"
    with pytest.raises(OriginalPatchError):
        compile_original_patch(RAW, r)


def test_delegated_context_is_not_relabelled_human_exact():
    r = recipe()
    r["patches"][0]["decision_kind"] = "DELEGATED_CONTEXT_CORRECTION"
    _, proof = compile_original_patch(RAW, r)
    assert proof["changed_cues"][0]["decision_kind"] == "DELEGATED_CONTEXT_CORRECTION"


def git_fixture(tmp_path):
    root = tmp_path.resolve() / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    directory = root / "assets/lidousha/reviewed_subtitle_baselines"
    directory.mkdir(parents=True)
    (directory / (CID + ".original-review.srt")).write_bytes(RAW)
    (directory / (CID + SUFFIX)).write_text(json.dumps(recipe()))
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "seal original and approved patch",
        ],
        cwd=root,
        check=True,
    )
    return root, directory


def test_loader_checks_independent_committed_original_not_record_claims(tmp_path):
    root, directory = git_fixture(tmp_path)
    expected, proof = load_original_patch(root, directory, CID)
    final = root / "final.srt"
    final.write_bytes(expected)
    assert (
        original_release_problems(
            repo_root=root, directory=directory, candidate_id=CID, subtitle_path=final
        )
        == []
    )
    final.write_bytes(expected.replace("李姐李姐".encode(), "侄女侄女".encode()))
    assert original_release_problems(
        repo_root=root, directory=directory, candidate_id=CID, subtitle_path=final
    )
    # Recomputing a final record or a package self-hash cannot alter this comparison.
    assert proof["original_review_input_sha256"] == hashlib.sha256(RAW).hexdigest()


def test_unrelated_candidate_without_original_anchor_remains_existing_policy(tmp_path):
    root, directory = git_fixture(tmp_path)
    assert load_original_patch(root, directory, "unrelated") is None
    assert (
        original_release_problems(
            repo_root=root, directory=directory, candidate_id="unrelated", subtitle_path=None
        )
        == []
    )
    assert (
        original_release_problems(
            repo_root=root, directory=directory, candidate_id="", subtitle_path=None
        )
        == []
    )


@pytest.mark.parametrize("target", ["recipe", "original", "symlink"])
def test_committed_binding_drift_fails_closed(tmp_path, target):
    root, directory = git_fixture(tmp_path)
    plan = directory / (CID + SUFFIX)
    source = directory / (CID + ".original-review.srt")
    if target == "recipe":
        plan.write_text(plan.read_text() + " ")
    elif target == "original":
        source.write_bytes(RAW + b"\n")
    else:
        plan.unlink()
        plan.symlink_to(source)
    with pytest.raises((ValueError, OSError)):
        load_original_patch(root, directory, CID)


def test_byte_format_drift_not_silently_normalized_in_release_gate(tmp_path):
    root, directory = git_fixture(tmp_path)
    expected, _ = load_original_patch(root, directory, CID)
    final = root / "final.srt"
    final.write_bytes(expected.replace(b"\n", b"\r\n"))
    assert original_release_problems(
        repo_root=root, directory=directory, candidate_id=CID, subtitle_path=final
    )


def test_canonical_auditor_calls_original_gate_even_without_record_pin(tmp_path, monkeypatch):
    from scripts import audit_review_package as auditor

    calls = []

    def check(**kwargs):
        calls.append(kwargs)
        return ["unlisted correct nickname replaced by machine text"]

    monkeypatch.setattr("src.autoslice.fastlane_original_patch.original_release_problems", check)
    package = tmp_path / "package"
    package.mkdir()
    (package / "x.srt").write_bytes(RAW)
    (package / "review_manifest.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": CID,
                        "candidate_id": CID,
                        "stem": CID,
                        "kind": "talk",
                        "classification": "talk",
                        "subtitle_srt": "x.srt",
                        "title": "【李豆沙】原始已审稿只改点名错误",
                    }
                ],
                "date": "2026-06-01",
                "story_contract_required": False,
                "upload_allowed": False,
                "run_mode": "MANUAL_PRODUCE_REVIEW",
            }
        )
    )
    result = auditor.audit_package(package)
    assert calls and calls[0]["candidate_id"] == CID
    assert any(i["code"] == "OPERATOR_FASTLANE_ORIGINAL_BINDING_INVALID" for i in result["issues"])




def test_ready_original_with_no_requested_text_changes_is_not_forced_to_rerun():
    plan = recipe()
    plan["patches"] = []
    output, receipt = compile_original_patch(RAW, plan)
    assert output == RAW
    assert receipt["scope_plan"] is None
    assert receipt["change_scope"] == "UNCHANGED_ORIGINAL_REPLAY"
    assert receipt["changed_cues"] == []
    assert receipt["provider_calls"] == 0




def test_synthetic_original_restoration_keeps_unlisted_mentions_and_timing():
    from src.autoslice.jingting_chunker import parse_srt_cues

    release, proof = compile_original_patch(RAW, recipe())
    before = parse_srt_cues(RAW.decode())
    after = parse_srt_cues(release.decode())
    assert len(before) == len(after) == 3
    assert [row["cue"] for row in proof["changed_cues"]] == [1]
    assert after[1].text == before[1].text == "就管人家叫李姐李姐"
    assert after[2].text == before[2].text == "这个QQ群真热闹"
    assert [(r.start_ms, r.end_ms) for r in before] == [
        (r.start_ms, r.end_ms) for r in after
    ]
    assert proof["provider_calls"] == 0


def test_committed_synthetic_anchor_survives_without_new_transcription(tmp_path):
    root, directory = git_fixture(tmp_path)
    result, receipt = load_original_patch(root, directory, CID)
    expected, expected_receipt = compile_original_patch(RAW, recipe())
    assert result == expected
    assert receipt["changed_cues"] == expected_receipt["changed_cues"]
    assert receipt["provider_calls"] == 0
    assert receipt["diagnostic_track_executed"] is False
