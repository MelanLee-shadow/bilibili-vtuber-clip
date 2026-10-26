import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.autoslice import producer_package_finalization as finalization
from src.autoslice.producer_prepared_recovery import validate_resume_options
from src.autoslice.producer_request import parse_producer_args


def test_m34_resume_input_accepts_relocated_prepared_bytes(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from pathlib import Path
    from src.autoslice import producer_prepared_recovery as recovery

    runtime = tmp_path / "runtime"
    original = runtime / "original"
    original.mkdir(parents=True)
    root = runtime / "successor"
    root.mkdir()
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    paths = {}
    for role in ("video", "subtitle", "uniform_host_ass", "padded", "piece"):
        paths[role] = original / role
        paths[role].write_bytes(role.encode())
    def write(role, value):
        paths[role] = original / (role + ".json")
        paths[role].write_text(json.dumps(value))
    sha = recovery._sha256
    write("chat_authority", {"final_review_audit": {"boundary_semantic_review": {}}})
    write("clip_context", {"context_sha256": "context"})
    hashes = {key: sha(paths[role]) for role, key in (
        ("subtitle", "subtitle_sha256"), ("chat_authority", "chat_authority_audit_sha256"),
        ("clip_context", "clip_context_file_sha256"), ("video", "burned_video_sha256"),
        ("uniform_host_ass", "ass_sha256"), ("video", "video_sha256"))}
    row = {"output_path": str(paths["piece"]), "output_sha256": sha(paths["piece"])}
    write("source_provenance", {"schema_version": "lidousha-speaker-recut-provenance.v1",
        "final_recut": {"source_path": str(paths["padded"]), "source_sha256": sha(paths["padded"]),
            "output_path": str(paths["video"]), "output_sha256": sha(paths["video"]), "start_ms": 0, "end_ms": 1000},
        "source_piece": row, "padded": {"output_path": str(paths["padded"]),
            "output_sha256": sha(paths["padded"]), "inputs": [{"path": row["output_path"], "sha256": row["output_sha256"]}]}})
    write("record", {"candidate_id": "candidate", "speaker_mode": "uniform_host", "artifact_hashes": hashes,
        "media_path": str(paths["video"]), "subtitle_path": str(paths["subtitle"]),
        "chat_authority_audit_path": str(paths["chat_authority"]), "clip_context_payload_sha256": "context",
        "start_ms": 0, "end_ms": 1000, "boundary_audit": {"final_start_ms": 0, "final_end_ms": 1000},
        "burned_preview": {"path": str(paths["video"])}})
    write("spec", {"candidate_id": "candidate", "runtime_root": str(runtime), "output_root": str(original), "pieces": []})
    prepared = original / "prepared.json"
    prepared.write_bytes(b"prepared receipt")
    args = SimpleNamespace(spec=paths["spec"], resume_prepared=prepared, resume_spec_sha256=sha(paths["spec"]),
        resume_output_root=root, prepare_only=True, correct="cpa", ssh_host="localhost", speaker_mode="uniform_host")
    request = SimpleNamespace(cid="candidate", out_root=root, spec={}, branding_intro=None)
    monkeypatch.setattr(recovery, "load_prepared_delivery", lambda **kw: SimpleNamespace(prepared_sha256=sha(kw["manifest_path"])))
    checked = SimpleNamespace(document={"lane": "talk", "candidate_id": "candidate"},
        checked=[({"role": role}, paths[role]) for role in (
            "record", "source_provenance", "subtitle", "chat_authority", "clip_context", "video", "uniform_host_ass")])
    monkeypatch.setattr(recovery, "exclusive_runner_commit", lambda _: nullcontext(None))
    monkeypatch.setattr(recovery, "historical_prepared_review", lambda *_: checked)
    monkeypatch.setattr(recovery, "validate_clip_context", lambda *a, **kw: None)
    monkeypatch.setattr(recovery, "_restore_source_piece_bindings", lambda **_: [])
    monkeypatch.setattr(recovery, "source_piece_clock", lambda *a, **kw: {})
    monkeypatch.setattr(recovery, "_validate_review_geometry", lambda *a, **kw: None)
    monkeypatch.setattr(recovery, "_replay_bound_burn", lambda *a, **kw: True)
    monkeypatch.setattr(recovery, "_validated_burned_artifact", lambda _: paths["video"])
    monkeypatch.setattr(recovery, "_validated_burned_ass_artifact", lambda _: paths["uniform_host_ass"])
    monkeypatch.setattr(recovery, "validate_final_subtitle_audio_check", lambda *a, **kw: None)
    first = recovery.load_prepared_producer_recovery(args=args, request=request)
    moved = original / "moved-prepared.json"
    moved.write_bytes(prepared.read_bytes())
    args.resume_prepared = moved
    receipt_path = root / "prepared-resume-input.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["native_audio_account"] = "/previous/root/native-account"
    receipt_path.write_text(json.dumps(receipt))
    reused = recovery.load_prepared_producer_recovery(args=args, request=request)
    assert reused.native_audio_state.cache_root == first.native_audio_state.cache_root
    assert reused.native_audio_state.cache_root == runtime / ".native-audio-resume" / "candidate" / sha(paths["padded"])
    moved.write_bytes(b"changed receipt")
    with pytest.raises(ValueError, match="SUCCESSOR_IDENTITY_DRIFT"):
        recovery.load_prepared_producer_recovery(args=args, request=request)
    moved.write_bytes(prepared.read_bytes())
    for field, value in (("release_authorized", True), ("candidate_id", "other"), ("spec_sha256", "f" * 64)):
        receipt_path.write_text(json.dumps(dict(receipt, **{field: value})))
        with pytest.raises(ValueError, match="SUCCESSOR_IDENTITY_DRIFT"):
            recovery.load_prepared_producer_recovery(args=args, request=request)


@pytest.mark.parametrize("damage", [None, "spec", "overlap", "public", "new_truth", "symlink"])
def test_prepared_resume_cli_refuses_changed_or_overlapping_inputs(tmp_path, damage):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"output_root": str(tmp_path / "original")}))
    args = parse_producer_args(
        [
            "--spec",
            str(spec),
            "--resume-prepared",
            str(tmp_path / "prepared.json"),
            "--resume-spec-sha256",
            hashlib.sha256(spec.read_bytes()).hexdigest(),
            "--resume-output-root",
            str(tmp_path / "successor"),
            "--prepare-only",
            "--speaker-mode",
            "uniform_host",
            "--correct",
            "cpa",
        ],
        description="",
        speaker_display_name="host",
        default_speaker_mode="uniform_host",
    )
    if damage == "spec":
        spec.write_text(spec.read_text() + "\n")
    elif damage == "overlap":
        args.resume_output_root = tmp_path / "original" / "nested"
    elif damage == "public":
        args.prepare_only = False
    elif damage == "new_truth":
        args.subtitle_text_overrides = tmp_path / "new.json"
    elif damage == "symlink":
        target = tmp_path / "target"
        target.mkdir()
        args.resume_output_root.symlink_to(target, target_is_directory=True)
    if damage:
        with pytest.raises(ValueError, match="PREPARED_RESUME_"):
            validate_resume_options(args)
    else:
        validate_resume_options(args)


@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("initial_pending", [None, "current", "proposed"])
def test_completed_stages_skip_old_work_but_real_text_change_gets_new_burn(
    tmp_path, monkeypatch, repair, initial_pending,
):
    text = "1\n00:00:00,000 --> 00:00:01,000\n当前文字\n"
    srt = tmp_path / "candidate.recut.srt"
    srt.write_text(text)
    recut = finalization.FinalRecutArtifacts(tmp_path, tmp_path / "media.mp4", srt, None, None)
    from tests.test_producer_current_text_auto_resume import _completed_current_review
    from src.autoslice.final_review_contract import validate_final_review_release, FinalReviewContractError
    from src.autoslice.final_review_auditor import audit_final_subtitles
    old_review, _ = _completed_current_review(text)
    if initial_pending:
        # A historical serialized disclosure is retained as input, never
        # recompiled into a fresh current risk assessment by this fixture.
        old_review.pop('meaning_policy')
        old_review['unresolved_findings_disclosed'] = [{'cue_index': 1, 'kind': 'context',
            'base_text_sha256': hashlib.sha256('当前文字'.encode()).hexdigest(),
            'repair_class': 'phonetic', 'proposed_full_cue': '经正式裁决修复的文字',
            'why': '具体候选需要正常裁决。', 'content_risk': 'noncritical_uncertainty',
            'risk_basis': '旧宽泛风险分类。'}]
        with pytest.raises(FinalReviewContractError, match='FINAL_REVIEW_MEANING_POLICY_REVIEW_REQUIRED'):
            validate_final_review_release(old_review)
    else:
        validate_final_review_release(old_review)
    old_record = {"burned_preview": {"path": "original-burn"}}
    completed = finalization.CompletedFinalizationArtifacts(
        recut,
        finalization.SpeakerArtifacts(None, None, None, None),
        finalization.AuthorityArtifacts(None, None),
        old_record,
        old_review,
    )
    counts = {"exact": 0, "burn": 0, "postburn": 0}

    def forbidden(**_kwargs):
        pytest.fail("entered an already completed producer phase")

    def exact(**kwargs):
        counts["exact"] += 1
        if initial_pending and counts['exact'] == 1:
            assert kwargs['chat_authority_audit']['final_review_audit'] == old_review
        if not (initial_pending == 'current' and counts['exact'] == 1):
            kwargs["recut"].subtitle_path.write_text(text.replace("当前文字", f"经第{counts['exact']}次正式裁决修复的文字"))
        current, _ = _completed_current_review(kwargs['recut'].subtitle_path.read_text())
        kwargs['chat_authority_audit']['final_review_audit'] = current
        return current

    def burn(**kwargs):
        counts["burn"] += 1
        assert kwargs["render_revision_marker"] == "postburn-typed-srt-v1"
        return {"burned_preview": {"path": "new-revision-burn"}}

    def postburn(**kwargs):
        counts["postburn"] += 1
        if repair and counts["postburn"] == 1:
            kwargs["chat_authority_audit"]["postburn_subtitle_audio_review"] = {
                "status": "FLAGGED",
                "release_gate": "BLOCK",
                "findings": [{"cue_index": 1}],
            }
            raise SystemExit("GENUINE_NEW_CONTENT_FINDING")
        return kwargs["record"]

    monkeypatch.setattr(finalization, "_branding_intro_required_for_talk", lambda: False)
    monkeypatch.setattr(finalization, "deployment_authority_binding", lambda _root: {})
    monkeypatch.setattr(finalization, "resolve_talk_delivery_runtime_root", lambda _spec: tmp_path)
    monkeypatch.setattr(finalization, "_materialize_final_recut", forbidden)
    monkeypatch.setattr(finalization, "_run_exact_final_review_gate", exact)
    monkeypatch.setattr(finalization, "_finalize_speaker", lambda **_kw: completed.speaker)
    monkeypatch.setattr(finalization, "_verify_final_authority", lambda **_kw: completed.authority)
    monkeypatch.setattr(finalization, "_build_and_burn_record", burn)
    monkeypatch.setattr(finalization, "_run_postburn_subtitle_audio_review", postburn)
    monkeypatch.setattr(
        finalization, "validated_postburn_priority_findings", lambda **_kw: ([{}], "request")
    )
    monkeypatch.setattr(finalization, "_stage_record", lambda **kw: kw["record"])
    monkeypatch.setattr(
        finalization, "prepare_and_emit_talk_delivery_from_finalization", lambda **kw: kw["staged"]
    )
    options = SimpleNamespace(prepare_only=True, substrate="aggregate_asr")
    result = finalization.finalize_producer_package(
        options=options,
        profile_id="profile",
        speaker_subtitle_style_id="style",
        spec={"clip_context": {"candidate_id": "candidate"}},
        cid="candidate",
        out_root=tmp_path,
        host="localhost",
        padded=tmp_path / "padded.mp4",
        padded_provenance_path=tmp_path / "padded.json",
        piece_provenance_rows=[],
        final_start=50,
        final_end=1050,
        sanitized=[],
        timing_qa={},
        audit={},
        text_override_path=None,
        subtitle_regression_path=None,
        chat_authority_audit={"final_review_audit": deepcopy(old_review)},
        chat_authority_path=tmp_path / "chat.json",
        branding_intro=None,
        adapters=SimpleNamespace(),
        completed=completed,
    )
    assert counts == {"exact": int(bool(initial_pending)) + int(repair),
        "burn": int(initial_pending == 'proposed') + int(repair),
        "postburn": 1 + int(repair)}
    assert result["burned_preview"]["path"] == ("new-revision-burn" if repair or initial_pending == 'proposed' else "original-burn")
    assert old_record == {"burned_preview": {"path": "original-burn"}}


@pytest.mark.parametrize("explicit,versioned", [(False, False), (False, True), (True, True)])
def test_resume_loader_keeps_original_runtime_when_successor_has_different_depth(
    tmp_path, monkeypatch, explicit, versioned,
):
    from pathlib import Path
    from src.autoslice import producer_request
    from src.autoslice.channel_profile import binding_for_profile, load_channel_profile
    from src.autoslice.producer_delivery_prepare import resolve_talk_delivery_runtime_root
    from src.autoslice.repository_asset_authority import _canonical_sha256
    monkeypatch.delenv("AUTOSLICE_RUNTIME_VERSION_ROOT", raising=False)
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "delivery")
    monkeypatch.setattr(producer_request, "require_branding_intro", lambda *a, **k: None)
    monkeypatch.setattr(producer_request, "validate_producer_runtime_prerequisites", lambda **k: None)
    runtime = tmp_path / "runtime"
    original = runtime / "out/2026-11-04"
    original.mkdir(parents=True)
    repo = runtime / "versions" / ("a" * 40) / "repo" if versioned else runtime / "repo"
    repo.mkdir(parents=True)
    (repo / "DEPLOYED_COMMIT").write_text("a" * 40 + "\n")
    body = {"schema_version": "deployed-authority-manifest.v1", "deployed_commit": "a" * 40,
            "entries": {"assets/policy": {"bytes": 1, "sha256": "sha256:" + "b" * 64}}}
    (repo / "DEPLOYED_AUTHORITY_MANIFEST.json").write_text(json.dumps(
        {**body, "manifest_sha256": _canonical_sha256(body)}))
    if versioned:
        (repo / "DEPLOYED_MANIFEST.json").write_text("{}")
        (runtime / "current").mkdir()
        (runtime / "current/repo").symlink_to(repo)
    document = {"candidate_id": "resumed-member", "output_root": str(original), "pieces": []}
    if explicit:
        document["runtime_root"] = str(runtime)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(document))
    before = spec.read_bytes()
    successor = runtime / "production-inputs/recording/resume-v2/output"
    args = parse_producer_args([
        "--spec", str(spec), "--resume-prepared", str(runtime / "prepared.json"),
        "--resume-spec-sha256", hashlib.sha256(before).hexdigest(),
        "--resume-output-root", str(successor), "--prepare-only", "--correct", "cpa",
        "--speaker-mode", "uniform_host"], description="", speaker_display_name="host",
        default_speaker_mode="uniform_host")
    source = Path(__file__).parents[1]
    binding = binding_for_profile(load_channel_profile(source, environ={}))
    request = producer_request.load_producer_request(
        args, repo_root=source, profile_asset_file=lambda k: tmp_path / "unused",
        trusted_parent_profile_binding=binding)
    assert request.spec["runtime_root"] == str(runtime)
    assert resolve_talk_delivery_runtime_root(request.spec) == runtime
    assert request.out_root == successor / document["candidate_id"]
    assert spec.read_bytes() == before
    # A successor outside the original runtime must fail before creating it.
    args.resume_output_root = tmp_path / "foreign-runtime/successor"
    with pytest.raises(producer_request.ProducerRuntimePrerequisiteError,
                       match="resume output escapes original runtime"):
        producer_request.load_producer_request(
            args, repo_root=source, profile_asset_file=lambda k: tmp_path / "unused",
            trusted_parent_profile_binding=binding)
    assert not args.resume_output_root.exists()
    assert spec.read_bytes() == before


@pytest.mark.parametrize("damage", [None, "source", "cut", "window", "declared", "request"])
def test_retained_source_binding_reaches_current_context_consumer(tmp_path, monkeypatch, damage):
    from pathlib import Path
    from src.autoslice import producer_prepared_recovery as recovery
    from src.autoslice.clip_context import build_clip_context, validate_clip_context, ClipContextError

    originals, rows = [], []
    for index in range(2):
        source = tmp_path / f"source-{index}.mp4"
        cut = tmp_path / f"cut-{index}.mp4"
        source.write_bytes(f"original-source-{index}".encode())
        cut.write_bytes(f"original-cut-{index}".encode())
        originals.append({"remote_media": str(source), "start_ms": 100 + index * 1000,
                          "end_ms": 1100 + index * 1000})
        rows.append({"source_path": str(source), "source_sha256": recovery._sha256(source),
                     "output_path": str(cut), "output_sha256": recovery._sha256(cut),
                     "start_ms": originals[-1]["start_ms"], "end_ms": originals[-1]["end_ms"]})
    bound = deepcopy(originals)
    for piece, row in zip(bound, rows, strict=True):
        piece["source_media_sha256"] = "sha256:" + row["source_sha256"]
    context = build_clip_context(
        candidate_id="retained-member", spec={"date": "2026-07-22", "pieces": bound},
        draft_srt="1\n00:00:00,000 --> 00:00:01,000\n当前文字\n", authoritative_chat=[],
        topic_resolution={}, session_topic_authorities=[],
        speech_memory_ledger_path=Path(__file__).parents[1] / "assets/lidousha/speech_memory_ledger.v1.json",
    )
    if damage == "declared":
        originals[0]["source_media_sha256"] = "sha256:" + "0" * 64
    spec_path = tmp_path / "original-spec.json"
    spec_path.write_text(json.dumps({"pieces": originals}))
    before = spec_path.read_bytes()
    spec = json.loads(before)
    monkeypatch.setattr(recovery, "ffprobe_duration_ms", lambda _path: 1000)
    # The actual downstream source-set gate used to receive an empty set.
    with pytest.raises(ClipContextError, match="SOURCE_BINDING_MISMATCH"):
        validate_clip_context(context, source_media_sha256s=[])
    if damage == "source":
        Path(rows[0]["source_path"]).write_bytes(b"changed-source")
    elif damage == "cut":
        Path(rows[1]["output_path"]).write_bytes(b"changed-cut")
    elif damage == "window":
        rows[1]["start_ms"] += 1
    elif damage == "request":
        spec["pieces"][0]["remote_media"] = str(tmp_path / "other.mp4")
    if damage:
        with pytest.raises((ValueError, RuntimeError), match="DRIFT|MISMATCH"):
            recovery._restore_source_piece_bindings(
                spec=spec, original_pieces=originals, context_pieces=context["pieces"], rows=rows)
    else:
        assert recovery._restore_source_piece_bindings(
            spec=spec, original_pieces=originals, context_pieces=context["pieces"], rows=rows) == [1000, 1000]
        validate_clip_context(context, source_media_sha256s=[
            piece["source_media_sha256"] for piece in spec["pieces"]])
        assert spec["pieces"] == bound
    assert spec_path.read_bytes() == before
    assert json.loads(before)["pieces"] == originals


@pytest.mark.parametrize("change", [None, "subtitle", "media", "cover", "context", "native_gate", "public", "title_gate", "tags"])
def test_completed_editorial_reuse_enters_real_stage_without_generation(tmp_path, monkeypatch, change):
    from pathlib import Path
    import shutil
    from src.autoslice import producer_prepared_recovery as recovery
    from src.autoslice import producer_completed_prepare_revalidation as native
    from src.autoslice import candidate_entity_publish_gate as title_gate
    from src.autoslice.clip_context import build_clip_context
    from src.autoslice.publish_staging_paths import current_editorial_binding

    runtime = tmp_path / "runtime"
    source, successor = runtime / "original", runtime / "successor"
    source.mkdir(parents=True)
    successor.mkdir()
    paths = {role: source / (role + ".json" if role in {"record", "publish"} else role)
             for role in ("record", "publish", "subtitle", "cover", "video", "media")}
    paths["subtitle"].write_text("1\n00:00:00,000 --> 00:00:01,000\n当前文字\n")
    for role in ("cover", "video", "media"):
        paths[role].write_bytes(role.encode())
    def sha(path):
        return "sha256:" + recovery._sha256(path)
    original = {
        "candidate_id": "retained", "media_path": str(paths["media"]),
        "subtitle_path": str(paths["subtitle"]), "story_contract": {"selection_hook": "真实事件"},
        "artifact_hashes": {"burned_video_sha256": sha(paths["video"])},
        "upload_tags": {"status": "OK", "final_tags": ["李豆沙", "真实事件"]},
        "burned_preview": {"status": "BURNED", "path": str(paths["video"]),
                           "burned_sha256": sha(paths["video"])},
    }
    staging = {"candidate_id": "retained", "title": "已通过审查的标题", "cover_status": "AI_COVER_READY",
               "cover_path": str(paths["cover"]), "upload_enabled": change == "public",
               "publish_json_path": str(paths["publish"]), "cover_generation": {}}
    staging["current_binding"] = current_editorial_binding(original, staging, candidate_id="retained")
    original["publish_staging"] = staging
    publish = {**deepcopy(staging), "subtitle_sha256": sha(paths["subtitle"])}
    paths["record"].write_text(json.dumps(original))
    paths["publish"].write_text(json.dumps(publish))
    for role in ("media", "subtitle"):
        shutil.copyfile(paths[role], successor / role)
    current = deepcopy(original)
    current.update(media_path=str(successor / "media"), subtitle_path=str(successor / "subtitle"))
    pieces = [{"start_ms": 2000, "end_ms": 3000, "remote_media": "/source.mp4",
               "source_media_sha256": "sha256:" + "a" * 64}]
    context = build_clip_context(
        candidate_id="retained", spec={"date": "2026-07-22", "pieces": pieces},
        draft_srt=paths["subtitle"].read_text(), authoritative_chat=[], topic_resolution={},
        session_topic_authorities=[], speech_memory_ledger_path=Path(__file__).parents[1]
        / "assets/lidousha/speech_memory_ledger.v1.json",
    )
    context_path = successor / "context.json"
    context_path.write_text(json.dumps(context))
    spec = {"date": "2026-07-22", "pieces": deepcopy(pieces), "clip_context": context,
            "clip_context_path": str(context_path)}
    if change in {"subtitle", "media"}:
        (successor / change).write_bytes(b"new current input")
    elif change == "cover":
        paths["cover"].write_bytes(b"drifted original pixels")
    elif change == "context":
        spec["pieces"][0]["source_media_sha256"] = "sha256:" + "b" * 64
    elif change == "tags":
        current["upload_tags"]["final_tags"] = ["duplicate", "duplicate"]
    checked = SimpleNamespace(checked=[({"role": role}, path) for role, path in paths.items()])
    monkeypatch.setattr(recovery, "historical_prepared_review", lambda *_args: checked)
    consumed = []
    def validate(handle, result):
        consumed.append(handle)
        if change == "native_gate":
            raise ValueError("CURRENT_NATIVE_GATE_REJECTED")
    monkeypatch.setattr(native, "validate_completed_prepare_artifacts", validate)
    monkeypatch.setattr(title_gate, "evaluate_candidate_title_gates", lambda **_kwargs:
                        SimpleNamespace(authority_error="current title rejected" if change == "title_gate" else None,
                                        violations=[]))
    recut = finalization.FinalRecutArtifacts(successor, successor / "media", successor / "subtitle", None, None)
    handle = SimpleNamespace(runtime_root=runtime, prepared_sha256="c" * 64)
    completed = finalization.CompletedFinalizationArtifacts(
        recut, None, None, current, {}, prepared_handle=handle)
    before = {p: p.read_bytes() for p in paths.values()}
    if change in {"subtitle", "media"}:
        assert recovery.stage_completed_editorial(
            completed=completed, spec=spec, cid="retained", recut=recut, record=current) is None
    else:
        def forbidden(*_args, **_kwargs):
            pytest.fail("regenerated an unchanged completed title/cover/tag")
        monkeypatch.setattr(finalization, "build_publication_llm_call", forbidden)
        def call():
            return finalization._stage_record(
                options=SimpleNamespace(), spec=spec, cid="retained", recut=recut, record=current,
                adapters=SimpleNamespace(stage_publish_draft=forbidden, generate_upload_tags=forbidden),
                completed=completed)
        if change:
            with pytest.raises((ValueError, RuntimeError), match="DRIFT|MISMATCH|REJECTED|NOT_PRIVATE"):
                call()
        else:
            staged = call()
            assert staged.staging["title"] == staging["title"]
            assert staged.staging["cover_path"] == staging["cover_path"]
            assert staged.record["prepared_editorial_recovery"]["new_title_cover_or_tag_calls"] == 0
            assert staged.record_path.parent == successor
            assert json.loads(Path(staged.staging["publish_json_path"]).read_text())["subtitle_path"] == current["subtitle_path"]
    assert consumed == [handle]
    assert {p: p.read_bytes() for p in paths.values()} == before
