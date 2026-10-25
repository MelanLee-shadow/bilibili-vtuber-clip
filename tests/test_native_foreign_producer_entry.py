from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from src.autoslice.channel_profile import binding_for_profile, load_channel_profile

from src.autoslice import producer_request
from src.autoslice import producer_text_pipeline as pipeline


def _args(spec_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        spec=spec_path,
        subtitle_text_overrides=None,
        subtitle_regression=None,
        speaker_overrides=None,
        ssh_host="localhost",
        speaker_mode="uniform_host",
    )


def _write_request_spec(
    tmp_path: Path,
    *,
    provider: object = None,
) -> Path:
    spec: dict[str, object] = {
        "candidate_id": "fixture",
        "human_truth_mode": "withheld",
        "output_root": str(tmp_path / "out"),
        "pieces": [],
    }
    if provider is not None:
        spec["foreign_script_witness_provider"] = provider
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def test_request_preflight_rejects_invalid_native_provider_before_branding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    branding_called = False

    def fail_if_branding_is_reached(*_args: object, **_kwargs: object) -> None:
        nonlocal branding_called
        branding_called = True
        raise AssertionError("invalid native provider must fail during request preflight")

    monkeypatch.setattr(producer_request, "require_branding_intro", fail_if_branding_is_reached)

    for provider in (True, [], "auto"):
        path = _write_request_spec(tmp_path, provider=provider)
        with pytest.raises(ValueError, match="foreign_script_witness_provider"):
            producer_request.load_producer_request(
                _args(path),
                repo_root=Path(__file__).resolve().parents[1],
                profile_asset_file=lambda _name: tmp_path / "unused.json",
                trusted_parent_profile_binding=binding_for_profile(load_channel_profile(Path(__file__).resolve().parents[1], environ={})),
            )

    assert branding_called is False


@pytest.mark.parametrize("provider", [None, "agy", "moss", "mai"])
def test_request_preflight_accepts_explicit_native_provider_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str | None,
) -> None:
    monkeypatch.setenv("AUTOSLICE_HUMAN_TRUTH_MODE", "withheld")
    monkeypatch.setattr(producer_request, "require_branding_intro", lambda *_a, **_k: None)
    path = _write_request_spec(tmp_path, provider=provider)

    request = producer_request.load_producer_request(
        _args(path),
        repo_root=Path(__file__).resolve().parents[1],
        profile_asset_file=lambda _name: tmp_path / "unused.json",
        trusted_parent_profile_binding=binding_for_profile(load_channel_profile(Path(__file__).resolve().parents[1], environ={})),
    )

    assert request.spec.get("foreign_script_witness_provider") == provider


def _srt(*texts: str) -> str:
    rows = []
    for index, text in enumerate(texts):
        start_ms = index * 2_000
        end_ms = start_ms + 1_000
        rows.append(
            f"{index + 1}\n"
            f"00:00:{start_ms // 1_000:02d},000 --> 00:00:{end_ms // 1_000:02d},000\n"
            f"{text}"
        )
    return "\n\n".join(rows) + "\n"


def _stub_language_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.autoslice import audio_language_discovery

    (tmp_path / "media.mp4").write_bytes(b"native-route-regression-media")
    monkeypatch.setattr(audio_language_discovery, "scan_audio_languages", lambda **_kwargs: {
        "status": "OBSERVED", "foreign_spans": [], "semantic_anomalies": [], "speech_turns": [],
    })


def test_full_text_finalization_routes_native_witness_and_registers_late_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.autoslice import native_foreign_witness as native

    current = _srt(
        "这是正常中文。",
        "you们知道苹果要出，呃，you",
        "这里结束了。",
    )
    proposed = "你们知道苹果要出，呃，you"

    def factory(**kwargs: object):
        assert kwargs["provider"] == "moss"
        assert kwargs["budget_receipt_path"] == (
            tmp_path / "native-audio-budget.json"
        )

        def observe(*, start_ms: int, end_ms: int) -> dict[str, object]:
            assert (start_ms, end_ms) == (2_000, 3_000)
            return {
                "status": "OBSERVED",
                "transcript": proposed,
                "provider": "moss",
                "model": "moss-transcribe-diarize-pro",
                "input_audio_sha256": "a" * 64,
                "source_media_sha256": "b" * 64,
                "response_sha256": "c" * 64,
                "candidate_exposure": "none",
                "mutation_authorized": False,
            }

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)
    _stub_language_discovery(tmp_path, monkeypatch)
    monkeypatch.setattr(
        pipeline,
        "_build_final_review_llm_call",
        lambda: lambda _prompt: json.dumps(
            {
                "choice": "PROPOSED",
                "candidate_id": "PROPOSAL",
                "reason": "Synthetic native witness regression choice.",
            }
        ),
    )
    chat_authority = {
        "status": "PASS",
        "source_truth_preview_receipts": {
            stage: {
                "schema_version": "source-truth-deterministic-preview.v1",
                "ledger_sha256": None,
            }
            for stage in ("pre_entity_arbitration", "pre_correction_review")
        },
    }

    result = pipeline._finalize_text_evidence(
        spec={
            "pieces": [],
            "foreign_script_witness_provider": "moss",
        },
        durations=[],
        srt_text=current,
        chat_authority_audit=chat_authority,
        transcript_entity_audit={"status": "PASS"},
        referent_groups=[],
        final_review_audit={},
        song_name_candidates=[],
        known_songs_path=tmp_path / "known-songs.json",
        session_topic_authorities=(),
        source_language_witness_srt=current,
        text_override_path=None,
        source_truth_ledger_path=None,
        out_root=tmp_path,
        cid="fixture",
        padded=tmp_path / "media.mp4",
    )

    assert result.srt_text == _srt(
        "这是正常中文。",
        proposed,
        "这里结束了。",
    )
    witness = chat_authority["foreign_script_consistency_audit"]["audio_witness_rows"][0]
    assert witness["provider"] == "moss"
    owner = chat_authority["entity_repairs"][-1]
    assert owner["mode"] == "final_foreign_script_cpa_adjudication"
    assert owner["decision_authority"] == "CPA_JUDGE"
    assert owner["matched_start_ms"] == 2_000
    assert owner["matched_end_ms"] == 3_000
    assert owner["after"] == [proposed]
    registration = chat_authority["final_foreign_script_cpa_surface_registrations"][-1]
    assert registration["status"] == "REGISTERED"


def _finalize_with_stub_adjudication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: str,
    *,
    append_after_adjudication: str = "",
):
    """Finalize with an audio-witnessed mixed phrase; optionally add a late cue."""
    _stub_language_discovery(tmp_path, monkeypatch)
    witness_status = "WITNESSED_MIXED_PHRASE_AUDIO"

    def adjudicate(_padded, srt_text, audit, *_args, **kwargs):
        if kwargs["source_language"]:
            return srt_text, audit
        assert audit["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
        audit["status"] = witness_status
        audit["audio_witness_rows"] = [
            {"cue_index": row["cue_index"], "witnessed": True}
            for row in audit["mixed_cjk_latin_cues"]
        ]
        return srt_text + append_after_adjudication, audit

    monkeypatch.setattr(pipeline, "_adjudicate_final_language", adjudicate)
    chat_authority = {
        "status": "PASS",
        "source_truth_preview_receipts": {
            stage: {
                "schema_version": "source-truth-deterministic-preview.v1",
                "ledger_sha256": None,
            }
            for stage in ("pre_entity_arbitration", "pre_correction_review")
        },
    }
    kwargs = dict(
        spec={"pieces": []},
        durations=[],
        srt_text=current,
        chat_authority_audit=chat_authority,
        transcript_entity_audit={"status": "PASS"},
        referent_groups=[],
        final_review_audit={},
        song_name_candidates=[],
        known_songs_path=tmp_path / "known-songs.json",
        session_topic_authorities=(),
        source_language_witness_srt=current,
        text_override_path=None,
        source_truth_ledger_path=None,
        out_root=tmp_path,
        cid="fixture",
        padded=tmp_path / "media.mp4",
    )
    return chat_authority, lambda: pipeline._finalize_text_evidence(**kwargs)


def test_finalization_keeps_witnessed_mixed_phrase_and_binds_raw_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _srt("这是正常中文。", "今天我们 play the game 吧", "这里结束了。")
    chat_authority, run = _finalize_with_stub_adjudication(tmp_path, monkeypatch, current)

    result = run()  # raw guard is BLOCKED but fully covered by the witnessed row

    audit = chat_authority["foreign_script_consistency_audit"]
    assert result.srt_text == current
    assert audit["status"] == "WITNESSED_MIXED_PHRASE_AUDIO"
    assert audit["audio_witness_rows"] == [{"cue_index": 2, "witnessed": True}]
    assert audit["raw_guard"]["status"] == "BLOCKED_MIXED_CJK_LATIN_PHRASE"
    assert audit["raw_guard_srt_sha256"] == chat_authority["final_output_srt_sha256"]
    assert not pipeline._foreign_guard_blocks(audit)


def test_finalization_blocks_new_mixed_phrase_added_after_adjudication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _srt("这是正常中文。", "今天我们 play the game 吧", "这里结束了。")
    late = "4\n00:00:06,000 --> 00:00:07,000\n然后 you know what 我说\n"
    chat_authority, run = _finalize_with_stub_adjudication(
        tmp_path, monkeypatch, current, append_after_adjudication="\n" + late,
    )

    with pytest.raises(SystemExit, match="FOREIGN_SOURCE_TRANSCRIPTION_REQUIRED"):
        run()

    audit = chat_authority["foreign_script_consistency_audit"]
    assert audit["status"] == "WITNESSED_MIXED_PHRASE_AUDIO"
    assert len(audit["raw_guard"]["mixed_cjk_latin_cues"]) == 2


@pytest.mark.parametrize("provider", [None, "agy"])
def test_legacy_finalization_does_not_pass_native_provider_keyword(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str | None,
) -> None:
    _stub_language_discovery(tmp_path, monkeypatch)
    current = _srt("这是第一句。", "这是第二句。", "这是第三句。")
    calls: list[dict[str, object]] = []

    def adjudicate(*args: object, **kwargs: object):
        calls.append(kwargs)
        return args[1], args[2]

    monkeypatch.setattr(pipeline, "_adjudicate_final_language", adjudicate)
    chat_authority = {
        "status": "PASS",
        "source_truth_preview_receipts": {
            stage: {
                "schema_version": "source-truth-deterministic-preview.v1",
                "ledger_sha256": None,
            }
            for stage in ("pre_entity_arbitration", "pre_correction_review")
        },
    }
    spec: dict[str, object] = {"pieces": []}
    if provider is not None:
        spec["foreign_script_witness_provider"] = provider

    pipeline._finalize_text_evidence(
        spec=spec,
        durations=[],
        srt_text=current,
        chat_authority_audit=chat_authority,
        transcript_entity_audit={"status": "PASS"},
        referent_groups=[],
        final_review_audit={},
        song_name_candidates=[],
        known_songs_path=tmp_path / "known-songs.json",
        session_topic_authorities=(),
        source_language_witness_srt=current,
        text_override_path=None,
        source_truth_ledger_path=None,
        out_root=tmp_path,
        cid="fixture",
        padded=tmp_path / "media.mp4",
    )

    assert len(calls) == 2
    assert all("native_provider" not in call for call in calls)
    assert calls[0]["source_language"] is True
    assert calls[1]["source_language"] is False


@pytest.mark.parametrize('provider', [None, 'agy', 'moss', 'mai'])
def test_native_context_option_composes_inside_producer_entity_verifier(tmp_path, monkeypatch, provider):
    from types import SimpleNamespace
    from src.autoslice import entity_audio_verifier, native_context_witness
    from src.autoslice.acoustic_witness_adjudication import WITNESS_REQUEST_SCHEMA

    monkeypatch.delenv('CPA_BASE_URL', raising=False)
    monkeypatch.delenv('CPA_API_KEY', raising=False)
    monkeypatch.setattr(pipeline, 'witness_audio_locally_resolvable', lambda *_a, **_k: True)
    monkeypatch.setattr(pipeline, 'load_referent_groups', lambda *_a, **_k: [])
    calls = []
    def fallback(request):
        return {'route': 'existing', 'request': request}
    monkeypatch.setattr(entity_audio_verifier, 'build_local_audio_entity_verifier', lambda **_k: fallback)
    def build_native(**kwargs):
        assert kwargs['fallback'] is fallback
        assert kwargs['source_media'] == tmp_path/'source.mp4'
        assert kwargs['output_dir'] == tmp_path
        calls.append(kwargs['provider'])
        return lambda request: {'route': kwargs['provider'], 'request': request}
    monkeypatch.setattr(native_context_witness, 'build_native_context_verifier', build_native)
    spec = {'date': '2026-08-23', 'local_audio_witness_provider': provider}
    producer_request._validate_foreign_script_witness_provider(spec)
    context = pipeline._build_entity_verification_context(
        spec=spec, padded=tmp_path/'source.mp4', padded_dur=2000,
        host='localhost', text_override_path=None, cid='native-route', out_root=tmp_path,
        srt_text=_srt('有界路由测试'), authoritative_chat=[],
        adapters=SimpleNamespace(profile_asset_file=lambda _: tmp_path/'unused', topic_graph_disabled=lambda: True),
    )
    request = {'schema_version': WITNESS_REQUEST_SCHEMA}
    expected = provider if provider in {'moss', 'mai'} else 'existing'
    assert context.verify_confusable_entity(request) == {'route': expected, 'request': request}
    assert calls == ([provider] if provider in {'moss', 'mai'} else [])


@pytest.mark.parametrize('provider', [True, [], 'auto'])
def test_native_context_option_rejects_invalid_provider(provider):
    with pytest.raises(ValueError, match='local_audio_witness_provider'):
        producer_request._validate_foreign_script_witness_provider({'local_audio_witness_provider': provider})


def test_native_context_option_passes_explicit_local_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from src.autoslice import entity_audio_verifier, native_context_witness

    monkeypatch.delenv('CPA_BASE_URL', raising=False)
    monkeypatch.delenv('CPA_API_KEY', raising=False)
    monkeypatch.setattr(pipeline, 'witness_audio_locally_resolvable', lambda *_a, **_k: True)
    monkeypatch.setattr(pipeline, 'load_referent_groups', lambda *_a, **_k: [])
    def fallback(request):
        return {'route': 'existing', 'request': request}
    monkeypatch.setattr(entity_audio_verifier, 'build_local_audio_entity_verifier', lambda **_k: fallback)
    captured = {}

    def build_native(**kwargs):
        captured.update(kwargs)
        return lambda request: {'route': kwargs['provider'], 'request': request}

    monkeypatch.setattr(native_context_witness, 'build_native_context_verifier', build_native)
    independent_captured = {}

    def build_independent(**kwargs):
        independent_captured.update(kwargs)
        return kwargs['fallback']

    monkeypatch.setattr(
        native_context_witness,
        'build_independent_context_witnesses',
        build_independent,
    )
    spec = {
        'date': '2026-08-23',
        'local_audio_witness_provider': 'moss',
        'local_audio_witness_budget': {'max_windows': 24, 'max_audio_ms': 180_000},
    }
    producer_request._validate_foreign_script_witness_provider(spec)
    pipeline._build_entity_verification_context(
        spec=spec, padded=tmp_path/'source.mp4', padded_dur=2000,
        host='localhost', text_override_path=None, cid='native-budget', out_root=tmp_path,
        srt_text=_srt('有界预算路由测试'), authoritative_chat=[],
        adapters=SimpleNamespace(profile_asset_file=lambda _: tmp_path/'unused', topic_graph_disabled=lambda: True),
    )

    assert captured['provider'] == 'moss'
    assert captured['max_windows'] == 24
    assert captured['max_audio_ms'] == 180_000
    assert captured['budget_receipt_path'] == tmp_path / 'native-audio-budget.json'
    assert captured['native_observation_cache_root'] == tmp_path
    assert independent_captured['native_observation_cache_root'] == tmp_path
    assert independent_captured['output_dir'] == tmp_path / 'independent-context'


def test_explicit_budget_registers_before_local_audio_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from src.autoslice.supplement_audio_budget import get_budget

    monkeypatch.delenv("CPA_BASE_URL", raising=False)
    monkeypatch.delenv("CPA_API_KEY", raising=False)
    monkeypatch.setattr(
        pipeline,
        "witness_audio_locally_resolvable",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(pipeline, "load_referent_groups", lambda *_a, **_k: [])
    source = tmp_path / "not-yet-materialized-source.mp4"
    spec = {
        "date": "2026-08-23",
        "local_audio_witness_provider": "moss",
        "local_audio_witness_budget": {
            "max_windows": 24,
            "max_audio_ms": 180_000,
        },
    }
    pipeline._build_entity_verification_context(
        spec=spec,
        padded=source,
        padded_dur=2_000,
        host="localhost",
        text_override_path=None,
        cid="native-budget-before-gate",
        out_root=tmp_path,
        srt_text=_srt("有界预算先注册"),
        authoritative_chat=[],
        adapters=SimpleNamespace(
            profile_asset_file=lambda _name: tmp_path / "unused",
            topic_graph_disabled=lambda: True,
        ),
    )
    budget = get_budget(source)
    assert budget is not None
    assert budget.max_windows == 24
    assert budget.max_audio_ms == 180_000


@pytest.mark.parametrize('before,after', [('我找小室说话', '我找小诗说话'), ('露医生来了', '陆医生来了'), ('kmx叫妈妈', 'KMX叫妈妈')])
def test_p1_normal_finalizer_restores_entity_reviewed_name_slot(tmp_path, before, after):
    current, reference = _srt("先说一句", after, "最后说完"), _srt("先说一句", before, "最后说完")
    audit = {'status': 'PASS', 'source_truth_preview_receipts': {
        stage: {'schema_version': 'source-truth-deterministic-preview.v1', 'ledger_sha256': None}
        for stage in ('pre_entity_arbitration', 'pre_correction_review')}}
    result = pipeline._finalize_text_evidence(
        spec={'pieces': []}, durations=[], srt_text=current, protected_name_source_srt=reference,
        chat_authority_audit=audit, transcript_entity_audit={'status': 'PASS'}, referent_groups=[],
        final_review_audit={}, song_name_candidates=[], known_songs_path=tmp_path/'songs.json',
        session_topic_authorities=(), source_language_witness_srt=current,
        text_override_path=None, source_truth_ledger_path=None, out_root=tmp_path, cid='fixture',
    )
    assert result.srt_text == reference
    assert audit['protected_name_slot_repairs'][0]['protected_name'] in before


def test_p1_name_protection_preserves_common_words_and_nickname():
    from src.autoslice.surface_canon import protect_reviewed_name_slots
    for text in ('两个小时以后', '陆医生今天值班', '露医生来了'):
        assert protect_reviewed_name_slots(_srt(text), _srt(text)) == (_srt(text), [])
    assert protect_reviewed_name_slots(_srt('谢谢礼物'), _srt('小室')) == (_srt('谢谢礼物'), [])


@pytest.mark.parametrize('surface', ['kimo熊', 'kimo 熊', 'Kimo熊', 'kimoxiong', '基默熊'])
def test_p1_normal_finalizer_compresses_explicit_kmx_equivalences(tmp_path, surface):
    current = _srt('先说一句', surface + '叫妈妈', '最后说完')
    audit = {'status': 'PASS', 'source_truth_preview_receipts': {
        stage: {'schema_version': 'source-truth-deterministic-preview.v1', 'ledger_sha256': None}
        for stage in ('pre_entity_arbitration', 'pre_correction_review')}}
    result = pipeline._finalize_text_evidence(
        spec={'pieces': []}, durations=[], srt_text=current,
        chat_authority_audit=audit, transcript_entity_audit={'status': 'PASS'}, referent_groups=[],
        final_review_audit={}, song_name_candidates=[], known_songs_path=tmp_path/'songs.json',
        session_topic_authorities=(), source_language_witness_srt=current,
        text_override_path=None, source_truth_ledger_path=None, out_root=tmp_path, cid='fixture',
    )
    assert result.srt_text == _srt('先说一句', 'kmx叫妈妈', '最后说完')
