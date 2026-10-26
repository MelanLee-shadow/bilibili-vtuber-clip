"""Frozen native envelopes must reach the ordinary cache-only comparison route."""

from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from src.autoslice import native_context_witness as native
from src.autoslice.native_context_witness import build_independent_context_witnesses as real_wrapper
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.acoustic_witness_adjudication import build_witness_request
from src.autoslice.final_review_auditor import build_context_adjudication_request
from tests.test_native_context_witness import _receipt, native_digest

from tests.test_argon_consumer_integration import offline, scene  # noqa: F401


def content(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def frozen_native(scene, monkeypatch):
    from src.autoslice.argon_escalation_contract import sha

    root = scene.root / "frozen-cache"
    calls = []

    def factory(**kwargs):
        provider = kwargs["provider"]

        def observe(*, start_ms, end_ms):
            calls.append(provider)
            observed = _receipt(
                source_sha256=sha(scene.source.read_bytes()),
                start_ms=start_ms,
                end_ms=end_ms,
                transcript="一百五十首" if provider == "mai" else "一百五十手",
                provider=provider,
            )
            observed["model"] = native.NATIVE_MODELS[provider]
            observed["receipt_sha256"] = native_digest(
                {k: v for k, v in observed.items() if k != "receipt_sha256"}
            )
            return observed

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    monkeypatch.setattr(native, "build_independent_context_witnesses", real_wrapper)
    request = build_context_adjudication_request(
        scene.srt,
        scene.finding,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        require_complete_utterance_support=True,
    )
    wr = build_witness_request(request)
    recorder = real_wrapper(
        fallback=lambda _: {},
        source_media=scene.source,
        output_dir=root / "independent-context",
        providers=("mai", "moss"),
    )
    rows = recorder.observe_independent_witnesses(wr)
    assert len(rows) == 2 and set(calls) == {"mai", "moss"}
    return root, wr, calls


@pytest.mark.parametrize("damage", [None, "source", "geometry", "symlink"])
def test_normal_comparison_factory_reads_frozen_native_envelopes_without_new_calls(
    scene, frozen_native, damage
):
    root, wr, calls = frozen_native
    paths = list((root / "independent-context").glob("*/native-context/*.json"))
    if damage:
        for p in paths:
            doc = json.loads(p.read_bytes())
            if damage == "source":
                doc["source_media_sha256"] = "a" * 64
            elif damage == "geometry":
                doc["audio_start_ms"] += 1
            else:
                saved = p.with_suffix(".original")
                p.rename(saved)
                p.symlink_to(saved)
                continue
            p.write_text(json.dumps(doc))
    before = content(root)
    (scene.root / "actual-comparison").mkdir()
    context = pipeline._build_entity_verification_context(
        spec={
            "date": "2026-10-16",
            "comparison_ordinary_cache_only": True,
            "comparison_witness_cache_root": str(root),
        },
        padded=scene.source,
        padded_dur=6000,
        host="localhost",
        text_override_path=None,
        cid="synthetic-consumer",
        out_root=scene.root / "actual-comparison",
        srt_text=scene.srt,
        authoritative_chat=[],
        adapters=SimpleNamespace(
            profile_asset_file=lambda _: scene.root / "missing", topic_graph_disabled=lambda: True
        ),
    )
    observations = context.verify_confusable_entity.observe_independent_witnesses(wr)
    assert len(observations) == (0 if damage else 2)
    assert content(root) == before and len(calls) == 2 and not scene.provider.calls
    if not damage:
        assert {r["provider"] for r in observations} == {"mai", "moss"}
        assert all(r["served_from_cache"] is True for r in observations)


def test_real_post_independent_judge_is_a_same_finding_attempt_not_dropped(scene, frozen_native):
    from src.autoslice.final_review_auditor import adjudicate_context_finding

    root, wr, calls = frozen_native
    scene.ordinary_status = "OBSERVED"
    scene.ordinary_choice = "NEITHER"
    scene.diagnosis = None
    out = scene.root / "whole-chain"
    out.mkdir()
    context = pipeline._build_entity_verification_context(
        spec={
            "date": "2026-10-16",
            "comparison_ordinary_cache_only": True,
            "comparison_witness_cache_root": str(root),
            "argon_escalation": {"enabled": True},
        },
        padded=scene.source,
        padded_dur=6000,
        host="localhost",
        text_override_path=None,
        cid="synthetic-consumer",
        out_root=out,
        srt_text=scene.srt,
        authoritative_chat=[],
        adapters=SimpleNamespace(
            profile_asset_file=lambda _: scene.root / "missing", topic_graph_disabled=lambda: True
        ),
    )
    # Create the real original 3.8 receipt through the ordinary verifier,
    # then replay it without a fresh ordinary model in the comparison route.
    ordinary = context.verify_confusable_entity.argon_escalation.ordinary_verifier
    ordinary(wr)
    output, audit = adjudicate_context_finding(
        scene.srt,
        scene.finding,
        entity_verifier=context.verify_confusable_entity,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    assert output == scene.srt
    # Step 41 (Argon normal entry): repeated-unresolved admission needs three
    # purposeful, distinct, effective audio observations; "新触发不再以两次文字
    # NEITHER冒充三次听音". One ordinary hearing plus the frozen independent
    # judge, with no reading-chat/lyrics diagnosis, does not trigger Argon.
    assert not audit.get("argon_escalation"), audit
    assert not scene.provider.calls
    assert len(calls) == 2
    # The real post-independent attempt is still kept on the same finding.
    integration = context.verify_confusable_entity.argon_escalation
    history_root = integration.runtime.base / "cache" / "argon-consumer"
    history = sorted(history_root.glob("*/ordinary-history/*.json"))
    assert history
    before = {str(p): p.read_bytes() for p in history}
    # A repeat neither posts a native job nor drops or rewrites that history.
    output, second = adjudicate_context_finding(
        scene.srt,
        scene.finding,
        entity_verifier=context.verify_confusable_entity,
        clip_context=scene.context,
        source_media_timeline_offset_ms=1000,
        judge_llm_call=scene.judge,
        require_complete_utterance_support=True,
    )
    assert output == scene.srt and not second.get("argon_escalation")
    assert not [r for r in scene.provider.calls if r["method"] == "POST"]
    assert len(calls) == 2
    assert all(Path(k).read_bytes() == v for k, v in before.items())
