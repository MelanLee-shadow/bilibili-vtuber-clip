"""Real ordinary wrapper/checkpoint; image and media transports stay offline."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time

import pytest

from src.autoslice import normal_cover_generation_checkpoint as checkpoint
from src.autoslice import publish_staging as staging
from src.autoslice.fixed_cover_stage import FixedCoverStageOptions
from src.autoslice.publish_staging_paths import validate_staged_editorial_files

TITLE = "【李豆沙】读春雪时分享自己的读书感受"


def record_at(root):
    root.mkdir(parents=True, exist_ok=True)
    for name, data in (
        ("candidate.srt", b"1\n00:00:00,000 --> 00:00:02,000\ncurrent source\n"),
        ("candidate.mp4", b"OFFLINE_MEDIA_FIXTURE"),
    ):
        path = root / name
        if not path.exists():
            path.write_bytes(data)
    return {
        "status": "MATERIALIZED",
        "candidate_id": "candidate",
        "subtitle_path": str(root / "candidate.srt"),
        "media_path": str(root / "candidate.mp4"),
    }


def generation_files(root):
    for name, data in (
        ("cover.png", b"OFFLINE_EXISTING_PIXELS"),
        ("request.json", b'{"offline_request": true}'),
        ("response.json", b'{"offline_response": true}'),
        ("reference.png", b"OFFLINE_REFERENCE"),
    ):
        (root / name).write_bytes(data)
    cover = root / "cover.png"
    sha = "sha256:" + hashlib.sha256(cover.read_bytes()).hexdigest()
    return {
        "status": "AI_COVER_READY",
        "cover_path": str(cover),
        "cover_sha256": sha,
        "cover_generation": {
            "method": "images.edit",
            "image_generation_used": True,
            "final_cover": str(cover),
            "final_cover_sha256": sha,
            "request_path": str(root / "request.json"),
            "response_path": str(root / "response.json"),
            "reference_path": str(root / "reference.png"),
        },
        "reason_codes": [],
    }


@pytest.mark.parametrize('separate_successor', [False, True])
def test_normal_pinned_failed_pixels_get_one_current_review_without_new_image(ordinary, monkeypatch, separate_successor):
    root, record, calls, state, run = ordinary
    record['story_contract'] = {'schema_version': 'lidousha-story-contract.v1',
        'cover_fallback_mode': 'HOST_ONLY_GENERIC', 'participants': [], 'relation_state': 'UNKNOWN'}
    def generate(_record, **kwargs):
        attempt = kwargs['private_artifact_root']
        calls.append(attempt)
        result = generation_files(attempt)
        generation = result['cover_generation']
        generation.update(image_generation_used=True, ai_background_sha256=result['cover_sha256'],
            story_contract=record['story_contract'], route_decision={'host_identity_required': True},
            final_host_identity_verification={'status': 'FAIL', 'reference_path': str(attempt/'reference.png'),
                'reference_sha256': checkpoint.file_identity(attempt/'reference.png')['sha256']})
        result.update(status='BLOCKED_AI_COVER_REQUIRED', cover_path=None,
                      reason_codes=['COVER_FINAL_HOST_IDENTITY_UNVERIFIED'])
        return result
    generate.cpa_cache_identity = {'transport': 'offline-fixture', 'models': ['fixture']}
    monkeypatch.setattr(staging, '_stage_ai_cover_unbound', generate)
    assert run()['status'] == 'BLOCKED_AI_COVER_REQUIRED'
    failure = next(root.glob('.normal-cover-generations/*/*/*/failure.json'))
    before = failure.read_bytes()
    expected = json.loads(before)['identity']
    for key in ('editorial_input', 'media', 'full_story_contract'):
        drifted = deepcopy(expected)
        drifted[key] = {'different': 'current material'}
        with pytest.raises(ValueError, match='COVER_REVIEW_CURRENT_INPUT_DRIFT'):
            checkpoint.retained_failed_generation(path=failure,
                expected_sha256=checkpoint.file_identity(failure)['sha256'], expected=drifted,
                artifact_root=root, candidate_id='candidate')
    with pytest.raises(ValueError, match='COVER_CHECKPOINT_RECEIPT_DRIFT'):
        checkpoint.retained_failed_generation(path=failure, expected_sha256='b'*64,
            expected=expected, artifact_root=root, candidate_id='candidate')
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE', str(failure))
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE_SHA256', checkpoint.file_identity(failure)['sha256'])
    if separate_successor:
        def run():
            return staging._stage_ai_cover(record, media_path=Path(record['media_path']),
                candidate_id='candidate', title=TITLE, cover_text='当前短字', run_ffmpeg=True,
                private_artifact_root=root / 'successor', retained_artifact_root=str(root))
    reviews = []
    def current_review(**kwargs):
        reviews.append(kwargs)
        assert not kwargs['comparison_output_path'].is_relative_to(failure.parent)
        return {'status': 'PASS', 'current_pixels_reviewed': kwargs['final_cover_sha256']}
    monkeypatch.setattr(staging, 'verify_final_host_identity', current_review)
    monkeypatch.setattr(staging, 'validate_final_host_identity_verification',
        lambda generation: generation['final_host_identity_verification']['status'] == 'PASS')
    monkeypatch.setattr(staging, '_enforce_final_talk_cover_thumbnail_gate', lambda result: result)
    ready = run()
    assert ready['status'] == 'AI_COVER_READY' and len(calls) == 1 and len(reviews) == 1
    assert ready['cover_generation']['previous_final_host_identity_verification']['status'] == 'FAIL'
    assert failure.read_bytes() == before
    assert run() == ready and len(calls) == 1 and len(reviews) == 1
    # Origin bytes and their network evidence remain dependencies on reuse.
    Path(ready['cover_generation']['request_path']).write_bytes(b'changed original request')
    blocked = run()
    assert blocked['status'] == 'BLOCKED_AI_COVER_REQUIRED'
    assert len(calls) == 1 and len(reviews) == 1 and failure.read_bytes() == before


def test_partial_retained_cover_binding_blocks_before_any_generation(ordinary, monkeypatch):
    _, _, calls, _, run = ordinary
    monkeypatch.setenv('AUTOSLICE_REVIEW_COVER_FAILURE', '/unused/failure.json')
    result = run()
    assert result['reason_codes'] == ['COVER_REVIEW_ORIGIN_BINDING_INCOMPLETE'] and calls == []


def test_completed_native_pixels_survive_consumer_code_identity_change(ordinary, monkeypatch):
    root, record, calls, _, run = ordinary
    ready = run()
    receipt = next(root.glob('.normal-cover-generations/*/*/*/result.json'))
    before = receipt.read_bytes()
    monkeypatch.setenv('AUTOSLICE_REUSE_COVER_RESULT', str(receipt))
    monkeypatch.setenv('AUTOSLICE_REUSE_COVER_RESULT_SHA256', checkpoint.file_identity(receipt)['sha256'])
    monkeypatch.setattr(staging, '_enforce_final_talk_cover_thumbnail_gate', lambda result: result)
    restored = run()
    assert restored['status'] == 'AI_COVER_READY' and len(calls) == 1
    assert restored['cover_sha256'] == ready['cover_sha256']
    assert restored['cover_generation']['retained_generation']['path'] == str(receipt)
    assert receipt.read_bytes() == before
    assert run() == restored and len(calls) == 1
    identity = json.loads(before)['identity']
    assert checkpoint.retained_editorial_identity(path=receipt,
        expected_sha256=checkpoint.file_identity(receipt)['sha256'], artifact_root=root,
        candidate_id='candidate', successful=True) == identity
    drifted = deepcopy(identity)
    drifted['options']['fixed_options']['approved_punch'] = ['a different instruction']
    with pytest.raises(ValueError, match='COVER_REVIEW_CURRENT_INPUT_DRIFT'):
        checkpoint.retained_generation(path=receipt, expected_sha256=checkpoint.file_identity(receipt)['sha256'],
            expected=drifted, artifact_root=root, candidate_id='candidate', successful=True)
    drifted = deepcopy(identity)
    asset = next(row for row in drifted['dependencies'] if Path(row['path']).suffix != '.py' and 'sha256' in row)
    asset['sha256'] = 'sha256:' + 'b' * 64
    with pytest.raises(ValueError, match='COVER_REVIEW_CURRENT_ASSET_DRIFT'):
        checkpoint.retained_generation(path=receipt, expected_sha256=checkpoint.file_identity(receipt)['sha256'],
            expected=drifted, artifact_root=root, candidate_id='candidate', successful=True)


@pytest.fixture
def ordinary(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    record = record_at(root)
    calls = []
    state = {"reject": False, "mutate": None}

    def generate(_record, **kwargs):
        attempt = kwargs["private_artifact_root"]
        calls.append(attempt)
        result = generation_files(attempt)
        if state["mutate"]:
            state["mutate"]()
        if state["reject"]:
            result["status"] = "BLOCKED_AI_COVER_REQUIRED"
        return result

    generate.cpa_cache_identity = {"transport": "offline-fixture", "models": ["fixture"]}
    monkeypatch.setattr(staging, "_stage_ai_cover_unbound", generate)
    monkeypatch.setattr(
        staging,
        "_resolve_cover_cpa_credentials",
        lambda: ("https://offline.invalid/v1", "not-a-credential"),
    )

    def run(**extra):
        return staging._stage_ai_cover(
            record,
            media_path=Path(record["media_path"]),
            candidate_id="candidate",
            title=extra.pop("title", TITLE),
            cover_text=extra.pop("cover_text", "当前短字"),
            run_ffmpeg=extra.pop("run_ffmpeg", True),
            private_artifact_root=root,
            **extra,
        )

    return root, record, calls, state, run


def test_wrapper_reuses_exact_success_and_preserves_receipt(ordinary):
    root, _, calls, _, run = ordinary
    first = run()
    before = {p: p.read_bytes() for p in root.glob(".normal-cover-generations/**/*") if p.is_file()}
    assert first["status"] == "AI_COVER_READY"
    assert run() == first
    assert len(calls) == 1
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize(
    "change",
    [
        "title",
        "cover_text",
        "subtitle",
        "media",
        "story",
        "research",
        "run_ffmpeg",
        "mode",
        "fixed_options",
        "punch",
        "full_text",
        "diversity",
        "model",
        "endpoint",
        "reference_time",
        "context",
        "font",
        "code",
        "asset",
        "missing_code",
        "emote_media",
    ],
)
def test_effective_input_drift_regenerates_in_preserved_sibling(ordinary, monkeypatch, change):
    root, record, calls, _, run = ordinary
    if change in {"code", "asset", "missing_code", "emote_media"}:
        # An isolated copy supplies real dependency byte reads. Only fixtures
        # change; production/worktree source and profile assets remain intact.
        source = staging.ROOT
        shadow = root / "shadow-source"
        dependencies = checkpoint.generation_identity(
            record=record,
            media_path=Path(record["media_path"]),
            title=TITLE,
            cover_text="当前短字",
            inputs={},
            options={},
            profile=staging.CHANNEL_PROFILE,
            repo_root=source,
            provider={},
            generator=staging._stage_ai_cover_unbound,
        )["dependencies"]
        for row in dependencies:
            path = Path(row["path"])
            if not row.get("missing") and path.is_relative_to(source):
                target = shadow / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        monkeypatch.setattr(staging, "ROOT", shadow)
    first = run()
    receipt = calls[0] / "result.json"
    saved = receipt.read_bytes()
    extra = {}
    if change == "title":
        extra["title"] = TITLE + "！"
    if change == "cover_text":
        extra["cover_text"] = "不同短字"
    if change == "subtitle":
        Path(record["subtitle_path"]).write_bytes(b"changed SRT same path")
    if change == "media":
        Path(record["media_path"]).write_bytes(b"changed media same path")
    if change == "story":
        record["story_contract"] = {
            "selection_hook": "different story",
            "extra_actual_input": "changed",
        }
    if change == "research":
        record["editorial_research"] = {"applicable": False, "reason": "changed application"}
    if change == "run_ffmpeg":
        extra["run_ffmpeg"] = False
    if change == "mode":
        monkeypatch.setenv("AUTOSLICE_COVER_MODE", "polish")
    if change == "fixed_options":
        extra["fixed_options"] = FixedCoverStageOptions(require_screenshot_direct=True)
    if change == "punch":
        extra["punch_allowed"] = True
    if change == "full_text":
        extra["full_text_cover_contract"] = {"status": "explicit offline input"}
    if change == "diversity":
        extra["diversity_slot"] = 3
    if change == "model":
        monkeypatch.setenv("CPA_IMAGE_MODEL", "different-explicit-model")
    if change == "endpoint":
        monkeypatch.setattr(
            staging,
            "_resolve_cover_cpa_credentials",
            lambda: ("https://different.invalid/v1", "not-a-credential"),
        )
    if change == "reference_time":
        monkeypatch.setenv("AUTOSLICE_COVER_REF_MS", "333")
    if change == "context":
        context = root / "game-context.json"
        context.write_bytes(b"different game context")
        monkeypatch.setenv("LIDOUSHA_SESSION_GAME_CONTEXT", str(context))
    if change == "font":
        directory = root / "fonts"
        directory.mkdir()
        (directory / "fixture-font.ttf").write_bytes(b"changed selected font bytes")
    if change in {"code", "asset", "missing_code", "emote_media"}:
        changed_path = (
            shadow / "src/autoslice/cover_generation.py"
            if change == "code"
            else staging.CHANNEL_PROFILE.asset_file("cover_art_direction", repo_root=shadow)
        )
        if change == "missing_code":
            (shadow / "src/autoslice/cover_generation.py").unlink()
        else:
            if change == "emote_media":
                from src.autoslice.cover_emote import emote_media_roots, load_emote_library

                changed_path = (
                    emote_media_roots(shadow)[-1] / load_emote_library(shadow).entries[0].hd_file
                )
            changed_path.write_bytes(changed_path.read_bytes() + b"\nOFFLINE_DEPENDENCY_DRIFT\n")
    second = run(**extra)
    if change == "missing_code":
        assert second["status"] == "BLOCKED_AI_COVER_REQUIRED" and len(calls) == 1
        assert receipt.read_bytes() == saved
        return
    assert second["status"] == "AI_COVER_READY"
    assert len(calls) == 2
    assert second["cover_path"] != first["cover_path"]
    assert receipt.read_bytes() == saved
    assert Path(first["cover_path"]).read_bytes() == b"OFFLINE_EXISTING_PIXELS"


@pytest.mark.parametrize("name", ["cover.png", "request.json", "response.json", "reference.png"])
def test_same_path_output_drift_does_not_reuse_old_success(ordinary, name):
    _, _, calls, _, run = ordinary
    run()
    saved = (calls[0] / "result.json").read_bytes()
    (calls[0] / name).write_bytes(b"DRIFTED SAME-PATH BYTES")
    result = run()
    assert result["status"] == "AI_COVER_READY" and len(calls) == 2
    assert (calls[0] / "result.json").read_bytes() == saved
    assert (calls[0] / name).read_bytes() == b"DRIFTED SAME-PATH BYTES"


def test_missing_output_regenerates_without_deleting_old_attempt(ordinary):
    _, _, calls, _, run = ordinary
    run()
    (calls[0] / "request.json").unlink()
    assert run()["status"] == "AI_COVER_READY" and len(calls) == 2
    assert (calls[0] / "result.json").exists()


def test_rejection_has_no_success_checkpoint(ordinary):
    _, _, calls, state, run = ordinary
    state["reject"] = True
    assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert not (calls[0] / "result.json").exists()
    assert json.loads((calls[0] / "failure.json").read_bytes())["status"] == "REJECTED_NOT_REUSABLE"
    state["reject"] = False
    assert run()["status"] == "AI_COVER_READY" and len(calls) == 2
    assert (calls[0] / "response.json").exists()


@pytest.mark.parametrize("change", ["subtitle", "mode", "model"])
def test_mutation_during_generation_is_not_cached(ordinary, monkeypatch, change):
    _, record, calls, state, run = ordinary
    state["mutate"] = (
        (lambda: Path(record["subtitle_path"]).write_bytes(b"late input"))
        if change == "subtitle"
        else (
            lambda: monkeypatch.setenv(
                "AUTOSLICE_COVER_MODE" if change == "mode" else "CPA_IMAGE_MODEL",
                "polish" if change == "mode" else "late-model",
            )
        )
    )
    result = run()
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert not (calls[0] / "result.json").exists()
    assert (calls[0] / "cover.png").exists()


@pytest.mark.parametrize(
    "unsafe",
    [
        "partial_pointer",
        "receipt_drift",
        "pointer_symlink",
        "output_symlink",
        "parent_symlink",
        "receipt_escape",
    ],
)
def test_unsafe_or_partial_checkpoint_blocks_before_dispatch(ordinary, unsafe):
    root, _, calls, _, run = ordinary
    run()
    pointer = next(root.glob(".normal-cover-generations/*/*/CURRENT.json"))
    if unsafe == "partial_pointer":
        pointer.write_bytes(b'{"receipt":')
    if unsafe == "receipt_drift":
        (calls[0] / "result.json").write_bytes(b'{"schema_version":"changed"}')
    if unsafe == "pointer_symlink":
        target = root / "foreign.json"
        target.write_bytes(b"foreign")
        pointer.unlink()
        pointer.symlink_to(target)
    if unsafe == "output_symlink":
        target = root / "foreign.png"
        target.write_bytes(b"foreign")
        cover = calls[0] / "cover.png"
        cover.unlink()
        cover.symlink_to(target)
    if unsafe == "parent_symlink":
        attempt = calls[0]
        moved = root / "moved"
        attempt.rename(moved)
        attempt.symlink_to(moved, target_is_directory=True)
    if unsafe == "receipt_escape":
        pointer.write_text(
            json.dumps({"receipt": "../result.json", "sha256": "sha256:" + "a" * 64})
        )
    assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert len(calls) == 1


def test_output_dotdot_escape_never_becomes_a_checkpoint(ordinary, monkeypatch):
    root, _, calls, _, run = ordinary
    original = staging._stage_ai_cover_unbound

    def unsafe(*args, **kwargs):
        result = original(*args, **kwargs)
        external = kwargs["private_artifact_root"].parent / "outside.png"
        external.write_bytes(b"outside")
        path = kwargs["private_artifact_root"] / ".." / "outside.png"
        result["cover_path"] = result["cover_generation"]["final_cover"] = str(path)
        result["cover_generation"]["final_cover_sha256"] = (
            "sha256:" + hashlib.sha256(b"outside").hexdigest()
        )
        return result

    unsafe.cpa_cache_identity = {"transport": "offline-fixture"}
    monkeypatch.setattr(staging, "_stage_ai_cover_unbound", unsafe)
    assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert not (calls[0] / "result.json").exists()


@pytest.fixture
def staged_receipt(ordinary, monkeypatch):
    _, _, calls, _, run = ordinary
    replace = checkpoint.os.replace

    def interrupted(source, destination):
        if destination.name == "result.json":
            raise OSError("offline interrupted atomic result install")
        return replace(source, destination)

    with monkeypatch.context() as context:
        context.setattr(checkpoint.os, "replace", interrupted)
        assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED"
    assert not (calls[0] / "result.json").exists()
    temporary, = calls[0].glob(".result.json.*.tmp")
    return ordinary, temporary


def test_complete_staged_receipt_recovers_original_inode_without_dispatch(staged_receipt):
    (_, _, calls, _, run), temporary = staged_receipt
    snapshot = checkpoint.stable_regular_snapshot(temporary, label="test staged original")
    assert run()["status"] == "AI_COVER_READY" and len(calls) == 1
    formal = checkpoint.stable_regular_snapshot(calls[0] / "result.json", label="test recovered")
    assert (formal.device, formal.inode, formal.payload) == (
        snapshot.device, snapshot.inode, snapshot.payload,
    )
    assert not temporary.exists()
    assert run()["status"] == "AI_COVER_READY" and len(calls) == 1


@pytest.mark.parametrize("damage", [
    "partial", "schema", "identity", "failure", "output_receipt", "output_bytes",
    "missing_output", "unsafe_output", "unsafe_staged", "invalid_staged_name",
    "multiple_staged", "formal_conflict", "control_locator",
])
def test_staged_receipt_damage_blocks_without_install_or_dispatch(staged_receipt, damage):
    (root, _, calls, _, run), temporary = staged_receipt
    saved = json.loads(temporary.read_bytes())
    if damage == "partial":
        temporary.write_bytes(b'{"schema_version":')
    elif damage in {"schema", "identity", "failure", "output_receipt", "control_locator"}:
        if damage == "schema":
            saved["schema_version"] = "foreign"
        if damage == "identity":
            saved["identity"]["title"] = "foreign title"
        if damage == "failure":
            saved["result"]["status"] = "BLOCKED_AI_COVER_REQUIRED"
        if damage == "output_receipt":
            saved["outputs"][0]["sha256"] = "sha256:" + "a" * 64
        if damage == "control_locator":
            saved["result"]["cover_generation"]["request_path"] = str(temporary)
        temporary.write_bytes(checkpoint.canonical(saved))
    elif damage == "output_bytes":
        (calls[0] / "cover.png").write_bytes(b"drifted existing pixels")
    elif damage == "missing_output":
        (calls[0] / "response.json").unlink()
    elif damage == "unsafe_output":
        target = root / "foreign.png"
        target.write_bytes(b"foreign bytes")
        (calls[0] / "cover.png").unlink()
        (calls[0] / "cover.png").symlink_to(target)
    elif damage == "unsafe_staged":
        target = root / "foreign-result.json"
        target.write_bytes(temporary.read_bytes())
        temporary.unlink()
        temporary.symlink_to(target)
    elif damage == "invalid_staged_name":
        temporary = temporary.rename(calls[0] / ".result.json.invalid.tmp")
    elif damage == "multiple_staged":
        (calls[0] / (".result.json." + "a" * 32 + ".tmp")).write_bytes(temporary.read_bytes())
    elif damage == "formal_conflict":
        (calls[0] / "result.json").write_bytes(b'{"foreign":true}')
    original = temporary.read_bytes()
    assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED" and len(calls) == 1
    assert temporary.read_bytes() == original
    if damage == "formal_conflict":
        assert (calls[0] / "result.json").read_bytes() == b'{"foreign":true}'
    else:
        assert not (calls[0] / "result.json").exists()


def test_formal_and_identical_staged_control_do_not_change_output_set(staged_receipt):
    (_, _, calls, _, run), temporary = staged_receipt
    original = temporary.read_bytes()
    (calls[0] / "result.json").write_bytes(original)
    (calls[0] / "failure.json").write_bytes(b'{"status":"REJECTED_NOT_REUSABLE"}')
    (calls[0] / (".failure.json." + "b" * 32 + ".tmp")).write_bytes(b'{"status":"failure"}')
    assert run()["status"] == "AI_COVER_READY" and len(calls) == 1
    assert temporary.read_bytes() == original
    saved = json.loads(original)
    assert checkpoint._outputs(saved["result"], calls[0]) == saved["outputs"]
    assert {Path(row["path"]).name for row in saved["outputs"]} == {
        "cover.png", "reference.png", "request.json", "response.json",
    }


def test_conflicting_complete_attempts_block_before_staged_install(staged_receipt):
    (_, _, calls, _, run), temporary = staged_receipt
    saved = json.loads(temporary.read_bytes())
    sibling = calls[0].parent / ("c" * 32)
    shutil.copytree(calls[0], sibling)
    for path in sibling.glob(".result.json.*.tmp"):
        path.unlink()

    def relocated(value):
        if isinstance(value, str) and value.startswith(str(calls[0]) + "/"):
            return str(sibling) + value[len(str(calls[0])):]
        if isinstance(value, dict):
            return {key: relocated(child) for key, child in value.items()}
        if isinstance(value, list):
            return [relocated(child) for child in value]
        return value

    (sibling / "result.json").write_bytes(checkpoint.canonical(relocated(saved)))
    assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED" and len(calls) == 1
    assert temporary.exists() and not (calls[0] / "result.json").exists()


@pytest.mark.parametrize("race", ["foreign_target", "staged_inode", "input", "output"])
def test_staged_recovery_rechecks_cas_and_current_inputs(staged_receipt, monkeypatch, race):
    (_, record, calls, _, run), temporary = staged_receipt
    install = checkpoint.install_staged_inode

    def changed(snapshot, **kwargs):
        if race == "foreign_target":
            kwargs["target"].write_bytes(b"foreign target after validation")
        if race == "staged_inode":
            replacement = temporary.with_name("replacement")
            replacement.write_bytes(temporary.read_bytes())
            os.replace(replacement, temporary)
        result = install(snapshot, **kwargs)
        if race == "input":
            Path(record["subtitle_path"]).write_bytes(b"late subtitle input")
        if race == "output":
            (calls[0] / "reference.png").write_bytes(b"late reference drift")
        return result

    monkeypatch.setattr(checkpoint, "install_staged_inode", changed)
    assert run()["status"] == "BLOCKED_AI_COVER_REQUIRED" and len(calls) == 1
    assert not (calls[0].parent / "CURRENT.json").exists()
    if race == "foreign_target":
        assert (calls[0] / "result.json").read_bytes() == b"foreign target after validation"


def test_screenshot_direct_needs_no_network_files(ordinary, monkeypatch):
    _, _, _, _, run = ordinary

    def local(_record, **kwargs):
        result = generation_files(kwargs["private_artifact_root"])
        result["cover_generation"].update(method="screenshot_direct", image_generation_used=False)
        for name in ("request", "response"):
            result["cover_generation"].pop(name + "_path")
            (kwargs["private_artifact_root"] / (name + ".json")).unlink()
        return result

    monkeypatch.setattr(staging, "_stage_ai_cover_unbound", local)
    assert run()["status"] == "AI_COVER_READY"
    assert run()["status"] == "AI_COVER_READY"


def test_opaque_callback_is_blocked_without_dispatch(ordinary):
    _, _, calls, _, run = ordinary
    value = "hidden input"
    result = run(art_direction_llm_call=lambda _: value)
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED" and not calls


def test_artifact_root_dotdot_is_blocked_before_any_attempt_write(ordinary):
    root, record, calls, _, _ = ordinary
    result = staging._stage_ai_cover(
        record,
        media_path=Path(record["media_path"]),
        candidate_id="candidate",
        title=TITLE,
        cover_text="当前短字",
        run_ffmpeg=True,
        private_artifact_root=root / "new-directory" / "..",
    )
    assert result["status"] == "BLOCKED_AI_COVER_REQUIRED" and not calls
    assert not (root / "new-directory").exists()


@pytest.mark.parametrize("key", ["request", "response", "final_cover"])
def test_checkpoint_requires_declared_hash_to_match_actual_output_bytes(tmp_path, key):
    result = generation_files(tmp_path)
    generation = result["cover_generation"]
    for name in ("request", "response"):
        generation[name + "_sha256"] = checkpoint.file_identity(Path(generation[name + "_path"]))[
            "sha256"
        ]
    if key == "final_cover":
        different = tmp_path / "different-cover.png"
        different.write_bytes(b"different pixels")
        generation["final_cover"] = str(different)
    else:
        generation[key + "_sha256"] = "sha256:" + "a" * 64
    with pytest.raises(ValueError, match="BYTES_DRIFT"):
        checkpoint._outputs(result, tmp_path)


# Fresh interpreter protocol: only external generation/media are fixtures.
# Its output is also retained in the repair evidence directory by the parent.
PROCESS_ROOT = None
PROCESS_ACTION = None


def process_generation(_record, **kwargs):
    root = PROCESS_ROOT
    with (root / "dispatches.jsonl").open("a") as stream:
        stream.write(
            json.dumps({"pid": os.getpid(), "attempt": str(kwargs["private_artifact_root"])}) + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
    if PROCESS_ACTION == "hold":
        (root / "entered").write_text("entered")
        deadline = time.monotonic() + 15
        while not (root / "release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("offline fixture release timeout")
            time.sleep(0.02)
    return generation_files(kwargs["private_artifact_root"])


def cold_process(root, action):
    global PROCESS_ROOT, PROCESS_ACTION
    PROCESS_ROOT, PROCESS_ACTION = root, action
    record = record_at(root)
    staging._stage_ai_cover_unbound = process_generation
    staging._resolve_cover_cpa_credentials = lambda: (
        "https://offline.invalid/v1",
        "not-a-credential",
    )
    if action == "pointer-interrupt":
        atomic = checkpoint._atomic_json

        def interrupt(path, value):
            if path.name == "CURRENT.json":
                raise OSError("offline pointer persistence interruption")
            return atomic(path, value)

        checkpoint._atomic_json = interrupt
    if action == "publish-interrupt":
        original = Path.write_text

        def interrupt_publish(path, *args, **kwargs):
            if path == root / "candidate.publish.json":
                raise OSError("offline exact publish save interruption")
            return original(path, *args, **kwargs)

        Path.write_text = interrupt_publish
    result = None
    error = None
    try:
        result = staging._stage_publish_draft(
            deepcopy(record),
            candidate_id="candidate",
            title=TITLE,
            cues=[],
            run_ffmpeg=True,
            title_llm_call=None,
            private_artifact_root=root,
            private_publish_json_path=root / "candidate.publish.json",
        )
        if result["publish_staging"]["cover_status"] == "AI_COVER_READY":
            validate_staged_editorial_files(
                result,
                result["publish_staging"],
                candidate_id="candidate",
                subtitle_path=Path(record["subtitle_path"]),
            )
    except OSError as exc:
        error = str(exc)
    row = {
        "pid": os.getpid(),
        "action": action,
        "error": error,
        "status": result["publish_staging"]["cover_status"] if result else None,
        "dispatches": len((root / "dispatches.jsonl").read_text().splitlines()),
        "current_consumer_accepted": bool(
            result and result["publish_staging"]["cover_status"] == "AI_COVER_READY"
        ),
    }
    print(json.dumps(row))


def process_command(root, action):
    return [sys.executable, "-B", str(Path(__file__).resolve()), str(root), action]


def child(root, action):
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(staging.__file__).resolve().parents[2]),
    }
    result = subprocess.run(
        process_command(root, action), capture_output=True, text=True, env=environment, timeout=25
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("failure", ["publish-interrupt", "pointer-interrupt"])
def test_cold_process_reuses_success_after_persistence_interrupt(tmp_path, failure):
    root = tmp_path.resolve()
    first = child(root, failure)
    second = child(root, "restart")
    third = child(root, "complete-draft-reentry")
    assert first["pid"] != second["pid"] != third["pid"]
    assert first["dispatches"] == second["dispatches"] == third["dispatches"] == 1
    assert second["current_consumer_accepted"] and third["current_consumer_accepted"]
    if failure == "publish-interrupt":
        assert first["error"] == "offline exact publish save interruption"
    else:
        assert first["status"] == "BLOCKED_AI_COVER_REQUIRED"


def test_concurrent_same_generation_has_one_provider_dispatch(tmp_path):
    root = tmp_path.resolve()
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(staging.__file__).resolve().parents[2]),
    }
    first = subprocess.Popen(
        process_command(root, "hold"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 10
        while not (root / "entered").exists():
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        blocked = child(root, "parallel")
        assert blocked["status"] == "BLOCKED_AI_COVER_REQUIRED" and blocked["dispatches"] == 1
        (root / "release").write_text("release")
        stdout, stderr = first.communicate(timeout=20)
        assert first.returncode == 0, stderr
        completed = json.loads(stdout)
        resumed = child(root, "after-concurrency")
        assert completed["current_consumer_accepted"] and resumed["current_consumer_accepted"]
        assert completed["dispatches"] == resumed["dispatches"] == 1
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()


if __name__ == "__main__":
    cold_process(Path(sys.argv[1]), sys.argv[2])
