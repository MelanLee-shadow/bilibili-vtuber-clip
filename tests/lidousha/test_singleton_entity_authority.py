from pathlib import Path

from src.autoslice.chat_evidence import load_referent_groups
from src.autoslice.chat_repair import apply_audio_entity_verification
from src.autoslice import producer_text_pipeline as pipeline
from src.autoslice.term_authority import protected_terms, respell_pairs


REPO_ROOT = Path(__file__).resolve().parents[2]
ENTITY_CONFUSABLES = REPO_ROOT / "assets/lidousha/entity_confusables.json"


def _adapters() -> pipeline.TextPipelineAdapters:
    def unused(*_args, **_kwargs):
        return None

    return pipeline.TextPipelineAdapters(
        build_aggregate_transcriber=unused,
        build_agy_transcriber=unused,
        load_term_boundary_surfaces=unused,
        profile_asset_file=lambda _name: ENTITY_CONFUSABLES,
        review_glossary=lambda: "",
        topic_graph_disabled=lambda: True,
        topic_graph_path=unused,
        topic_graph_expected_sha256=lambda: "",
    )


def _nancho_group():
    return next(
        group
        for group in load_referent_groups(ENTITY_CONFUSABLES, include_singletons=True)
        if {entity.canonical for entity in group.entities} == {"南町"}
    )


def _srt(text: str) -> str:
    return f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n"


def test_producer_entity_context_loads_singleton_surface_registries(tmp_path, monkeypatch):
    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setattr(
        pipeline, "witness_audio_locally_resolvable", lambda *_args, **_kwargs: False
    )

    context = pipeline._build_entity_verification_context(
        spec={"date": "2026-08-07"},
        padded=tmp_path / "missing-source.mp4",
        padded_dur=2_000,
        host="localhost",
        text_override_path=None,
        cid="auto_210739_1142_1436",
        out_root=tmp_path,
        srt_text=_srt("南天今天也来了"),
        authoritative_chat=[],
        adapters=_adapters(),
    )

    nancho = next(
        group
        for group in context.referent_groups
        if {entity.canonical for entity in group.entities} == {"南町"}
    )
    assert {"南天", "大白老师"} <= set(nancho.entities[0].surfaces)
    assert nancho.audio_verify_all_surfaces is True
    assert nancho.uncertain_keep_canonicals == ("南町",)


def test_singleton_confusable_pairs_reach_shared_term_authority():
    pairs = respell_pairs()

    assert ("南天", "南町") in pairs
    assert ("大白老师", "南町") in pairs
    assert {"南町", "南天", "大白老师"} <= protected_terms()


def test_singleton_group_never_mechanically_expands_a_legal_surface():
    group = _nancho_group()

    def resolve_nancho(request):
        return {
            "schema_version": "chat-entity-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "RESOLVED",
            "canonical_entity": "南町",
            "authority_kind": "audio_forced_choice",
            "confidence": 0.99,
            "heard_syllables": "nan cho nightin",
            "source_media_sha256": "a" * 64,
            "audio_clip_sha256": "b" * 64,
            "prompt_sha256": "c" * 64,
            "response_sha256": "d" * 64,
        }

    source = _srt("我跟南町nightin一起走")
    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[group],
        entity_verifier=resolve_nancho,
    )

    assert output == source
    assert audit["status"] == "VERIFIED"
    assert audit["repairs"] == []


def test_singleton_false_positive_is_fail_closed_without_rewriting():
    group = _nancho_group()
    source = _srt("南天门今天开放")

    output, audit = apply_audio_entity_verification(
        source,
        referent_groups=[group],
        entity_verifier=lambda _request: None,
    )

    assert output == source
    assert audit["status"] == "ENTITY_VERDICT_REQUIRED"
    assert audit["repairs"] == []
