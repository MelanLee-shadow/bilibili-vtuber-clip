"""Design section 3.6 table and executable dependency-snapshot equivalence."""
import ast
import copy
import json
from pathlib import Path
import shutil

import pytest
from src.autoslice import asset_write_bindings as bindings
from src.autoslice.persona_facts import InflightBinding

ROOT = Path(__file__).resolve().parents[1]
STAGES = {
    "source": ("subtitle_truth_ledger",),
    "boundary": ("subtitle_truth_ledger",),
    "speaker": ("voiceprint_profile", "streamer_registry"),
    "text": ("glossary", "game_glossary", "community_names", "entity_confusables", "gift_names", "known_songs",
             "subtitle_correction_principles", "subtitle_truth_ledger", "topic_entity_graph", "speech_memory_ledger"),
    "title": ("title_policy", "title_style", "manual_title_overrides", "persona"),
    "cover": ("cover_art_direction", "cover_identity_prompt", "cover_reference_overrides", "emote_library"),
    "package": ("branding_intro_manifest", "final_media_review_contracts"),
}
SONG = {"emote_library", "cover_identity_prompt", "known_songs", "published_songs", "persona", "slice_selection_metric",
        "title_policy", "title_style", "upload_tag_policy", "voiceprint_profile", "profile.json"}
AUDIT = {"glossary", "entity_confusables", "title_policy", "selection_score_calibration", "subtitle_truth_ledger"}
COVER = {"cover_art_direction", "cover_identity_prompt", "cover_reference_overrides", "persona", "emote_library", "profile.json"}
RUNTIME = {"timely_terms", "psplive_roster", "streamer_registry", "community_names", "topic_entity_graph"}
BROAD = {"entity_confusables", "community_name_sources", "community_names", "game_glossary", "glossary",
         "branding_intro_manifest", "cover_art_direction", "cover_reference_overrides", "cover_identity_prompt", "emote_library",
         "gift_names", "known_songs", "manual_archive_metadata", "manual_title_overrides", "persona", "psplive_roster",
         "psplive_roster_sources", "streamer_registry", "streamer_registry_sources", "session_relation_ledger",
         "selection_score_calibration", "slice_selection_metric", "speech_memory_ledger", "subtitle_correction_principles",
         "subtitle_truth_ledger", "timely_term_seeds", "timely_term_sources", "timely_terms", "topic_entity_graph",
         "title_policy", "title_style", "upload_tag_policy", "voiceprint_profile", "profile.json"}


def test_every_current_asset_matches_binding_table():
    profile = json.loads((ROOT / "profiles/lidousha/profile.json").read_bytes())
    keys = set(profile["assets"]["files"]) | {"profile.json", "persona_facts", "selection_census_clues"}
    for key in sorted(keys):
        actual = bindings.asset_binding_surfaces(key)
        expected = set()
        if any(key in keys for keys in STAGES.values()) or key == "session_relation_ledger": expected.add("TALK_STAGE_SNAPSHOT")
        if key in SONG: expected.add("SONG_PROOF_CLOSURE")
        if key in BROAD: expected.add("BROAD_PIPELINE_FINGERPRINT")
        if key in AUDIT: expected.add("PACKAGE_AUDIT_POLICY")
        if key in COVER: expected.add("COVER_GENERATION_CHECKPOINT")
        if key in RUNTIME: expected.add("RUNTIME_CANDIDATE_BINDING")
        if key in {"session_relation_ledger", "subtitle_truth_ledger"}: expected.add("AUTHORITY_RECEIPT_SHA")
        assert {surface.kind for surface in actual} == expected, key
        expected_stages = {stage for stage, keys in STAGES.items() if key in keys}
        if key == "session_relation_ledger": expected_stages.add("text")
        assert {s.detail for s in actual if s.kind == "TALK_STAGE_SNAPSHOT"} == expected_stages, key
        assert all(s.code_ref.rpartition(":")[0] and int(s.code_ref.rpartition(":")[2]) > 0 for s in actual)


@pytest.fixture
def binding_copy(tmp_path):
    relatives = ["scripts/session_autoslice.py", "src/autoslice/runner_pipeline_fingerprints.py",
        "src/autoslice/review_package_policy_fingerprint.py", "src/autoslice/normal_cover_generation_checkpoint.py",
        "src/autoslice/session_relation_authority.py", "src/autoslice/producer_source_truth_checkpoint.py", "profiles/lidousha/profile.json"]
    for relative in relatives:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


@pytest.mark.parametrize("owner", ["talk", "song", "broad", "audit", "cover", "runtime", "runtime_direct", "authority", "frozen_authority"])
def test_surfaces_follow_changed_definitions_not_a_handwritten_key_list(binding_copy, owner):
    root = binding_copy
    key = "future_knowledge"
    if owner == "talk":
        relative, old, new, expected = "scripts/session_autoslice.py", '"source": ("subtitle_truth_ledger",)', '"source": ("subtitle_truth_ledger", "future_knowledge")', "TALK_STAGE_SNAPSHOT"
    elif owner == "song":
        relative, old, new, expected = "src/autoslice/runner_pipeline_fingerprints.py", 'profile_asset_file("persona")', 'profile_asset_file("future_knowledge")', "SONG_PROOF_CLOSURE"
    elif owner == "broad":
        path = root / "profiles/lidousha/profile.json"
        profile = json.loads(path.read_bytes())
        profile["assets"]["pipeline_fingerprint_files"].append(key)
        path.write_text(json.dumps(profile))
        assert {s.kind for s in bindings.asset_binding_surfaces(key, repo_root=root)} == {"BROAD_PIPELINE_FINGERPRINT"}
        return
    elif owner == "audit":
        relative, old, new, expected = "src/autoslice/review_package_policy_fingerprint.py", 'asset_file("glossary")', 'asset_file("future_knowledge")', "PACKAGE_AUDIT_POLICY"
    elif owner == "cover":
        relative, old, new, expected = "src/autoslice/normal_cover_generation_checkpoint.py", '{"persona", "emote_library"}', '{"persona", "emote_library", "future_knowledge"}', "COVER_GENERATION_CHECKPOINT"
    elif owner == "runtime_direct":
        relative, old, new, expected = "scripts/session_autoslice.py", 'profile_asset_file("topic_entity_graph")', 'profile_asset_file("future_knowledge")', "RUNTIME_CANDIDATE_BINDING"
    elif owner == "frozen_authority":
        relative, old, new, expected = "src/autoslice/producer_source_truth_checkpoint.py", 'profile_asset_file("subtitle_truth_ledger")', 'profile_asset_file("future_knowledge")', "AUTHORITY_RECEIPT_SHA"
    elif owner == "authority":
        relative, old, new = "scripts/session_autoslice.py", 'asset_file("session_relation_ledger")', 'asset_file("future_knowledge")'
        expected = "AUTHORITY_RECEIPT_SHA"
    else:
        relative, old, new, expected = "scripts/session_autoslice.py", '("timely_terms", "timely_terms.json", "TIMELY_TERMS")', '("future_knowledge", "future.json", "FUTURE")', "RUNTIME_CANDIDATE_BINDING"
    path = root / relative
    source = path.read_text()
    assert old in source
    path.write_text(source.replace(old, new))
    assert expected in {s.kind for s in bindings.asset_binding_surfaces(key, repo_root=root)}


def test_cover_filter_covers_unregistered_future_assets():
    assert {s.kind for s in bindings.asset_binding_surfaces("new_cover_asset")} == {"COVER_GENERATION_CHECKPOINT"}


def test_unknown_changed_binding_filter_fails_closed(binding_copy):
    path = binding_copy / "src/autoslice/normal_cover_generation_checkpoint.py"
    path.write_text(path.read_text().replace('"cover" in key', 'key.startswith("cover")'))
    with pytest.raises(ValueError, match="Unsupported cover"):
        bindings.asset_binding_surfaces("persona_facts", repo_root=binding_copy)


def test_write_plan_uses_surfaces_and_pinned_inflight():
    assert bindings.plan_knowledge_write("persona_facts", inflight=()).decision == "WRITE_ANYTIME"
    assert bindings.plan_knowledge_write("selection_census_clues", inflight=()).decision == "WRITE_ANYTIME"
    assert bindings.plan_knowledge_write("persona", inflight=()).decision == "DEPLOY_CLASS_CHANGE"
    pinned = InflightBinding("exact", "TALK_STAGE_SNAPSHOT", "sha256:" + "a" * 64, "old-deploy")
    irrelevant = InflightBinding("other", "PACKAGE_AUDIT_POLICY", "sha256:" + "b" * 64, "old-deploy")
    decision = bindings.plan_knowledge_write("persona", inflight=(pinned, irrelevant))
    assert decision.decision == "DEFER_UNTIL_TERMINAL"
    assert decision.blocking_inflight == (pinned,)
    assert bindings.plan_knowledge_write("persona_facts", inflight=(pinned,)).decision == "WRITE_ANYTIME"


def test_extracted_stage_constant_is_exact_original_literal():
    from scripts import session_autoslice as runner
    assert runner.stage_asset_keys == STAGES


def test_existing_records_recompute_byte_identical_snapshots_after_constant_extraction(tmp_path, monkeypatch):
    from scripts import session_autoslice as runner
    from src.autoslice.runner_pipeline_fingerprints import TALK_STAGE_ROOTS
    # Reconstruct the old function by putting the exact old literal back inside
    # its original position. Both paths invoke the real snapshot implementation.
    tree = ast.parse((ROOT / "scripts/session_autoslice.py").read_bytes())
    constant = copy.deepcopy(next(n for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "stage_asset_keys" for t in n.targets)))
    old = copy.deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "capture_talk_dependency_snapshot"))
    old.body.insert(2, constant)
    module = ast.fix_missing_locations(ast.Module(body=[old], type_ignores=[]))
    repo = tmp_path / "repo"
    for consumers in TALK_STAGE_ROOTS.values():
        for consumer in consumers:
            path = repo / "src/autoslice" / f"{consumer.split(':')[0]}.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def _resolve_automatic_title():\n    return 'fixture'\n")
    monkeypatch.setattr(runner, "REPO_ROOT", repo)
    monkeypatch.setattr(runner, "BASE", tmp_path / "base")
    monkeypatch.setattr(runner, "REC_ROOT", tmp_path / "recordings")
    monkeypatch.setattr(runner, "_speaker_routing_provider_authority", lambda: None)
    for name in ("candidate_text_override_path", "candidate_subtitle_regression_path", "candidate_speaker_override_path",
                 "candidate_entity_projection_path", "candidate_public_text_surface_authority_path", "candidate_reviewed_subtitle_baseline"):
        monkeypatch.setattr(runner, name, lambda _cid: None)
    namespace = vars(runner).copy()
    namespace.pop("stage_asset_keys")
    exec(compile(module, "<pre-extraction-capture>", "exec"), namespace)
    records = [
        {"candidate_id": "auto_212005_163_311", "segment": "22966160_20260710-21-20-05.mp4", "start_ms": 163000,
         "end_ms": 311000, "given_end_ms": 318000, "given_end_authority": "维护者 reviewed complete closing sentence", "hook": "小李嘴硬"},
        {"candidate_id": "candidate", "segment": "real-input-name.mp4", "start_ms": 100000, "end_ms": 200000,
         "status": "failed", "failure_kind": "subtitle_authority", "failure_stage": "final_review_findings"},
    ]
    for record in records:
        before = namespace["capture_talk_dependency_snapshot"]("2026-08-13", record)
        after = runner.capture_talk_dependency_snapshot("2026-08-13", record)
        assert json.dumps(before, sort_keys=True).encode() == json.dumps(after, sort_keys=True).encode()
