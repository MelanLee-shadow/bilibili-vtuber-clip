from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.autoslice import agy_gemini_client
from src.autoslice.nested_media_acoustic_observer import observe_nested_media_acoustics
from src.autoslice.nested_media_acoustic_runtime import (
    VOICE_REFERENCE_CPA_PROMPT,
    VOICE_REFERENCE_OBSERVER_PROMPT,
    build_runtime_acoustic_callbacks,
)
from tests.test_nested_media_acoustic_observer import _inputs


_CPA_IDENTITY = {
    "transport": "fixture.cpa",
    "models": ["fixture-cpa-v1"],
    "effort": "medium",
}


def _observer_json(*, source: str = "PRESENT", host: str = "ABSENT", overlap: str = "ABSENT") -> str:
    return json.dumps(
        {
            "status": "OBSERVED",
            "source_media_speech": source,
            "host_speech": host,
            "overlap_speech": overlap,
            "evidence": {"basis": "audio-only-fixture"},
        }
    )


def _cpa_json(*, source: str = "PRESENT", host: str = "ABSENT", overlap: str = "ABSENT") -> str:
    return json.dumps(
        {
            "source_media_speech": source,
            "host_speech": host,
            "overlap_speech": overlap,
            "evidence": {"basis": "observer-only-fixture"},
        }
    )


def _build(values: dict[str, object], *, observer_response: str, cpa_response: str):
    calls = {"observer": 0, "cpa": 0}
    captured: dict[str, object] = {}

    def generate(**kwargs):
        calls["observer"] += 1
        captured.update(kwargs)
        return observer_response

    def cpa(prompt: str):
        calls["cpa"] += 1
        captured["cpa_prompt"] = prompt
        return cpa_response

    cpa.cpa_cache_identity = dict(_CPA_IDENTITY)

    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate,
        cpa_call=cpa,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    values["runtime_identity"] = dict(callbacks.runtime_identity)
    return callbacks, calls, captured


def _run(values: dict[str, object], callbacks):
    call_values = dict(values)
    call_values.pop("runtime_identity", None)
    return observe_nested_media_acoustics(
        **call_values,
        observe_audio=callbacks.observe_audio,
        cpa_judge=callbacks.cpa_judge,
        runtime_identity=callbacks.runtime_identity,
    )


@pytest.mark.parametrize("prefix", [
    'thought\nTARGET_SCOPE_JSON: {"target_interval_ms":[10,20]}\n',
    'thought\ncall:view_file{AbsolutePath:/private/audio.mp4,toolAction:Viewing}\n',
])
def test_native_prose_before_role_answer_keeps_normal_cpa_route(tmp_path: Path, prefix: str):
    values = _inputs(tmp_path)
    answer = _observer_json()
    callbacks, calls, captured = _build(
        values,
        observer_response=prefix + "```json\n" + answer + "\n```",
        cpa_response=_cpa_json(),
    )
    result = _run(values, callbacks)
    assert result["final_class"] == "source_media"
    assert calls == {"observer": 1, "cpa": 1}
    bound = json.loads(captured["cpa_prompt"].split("BOUND_OBSERVATION_JSON:\n", 1)[1])
    assert bound["observer"]["evidence"]["provider_completion"].startswith(prefix)


def test_repeated_compatible_role_drafts_pass_but_conflicting_answers_keep(tmp_path: Path):
    values = _inputs(tmp_path)
    answer = _observer_json()
    callbacks, calls, _ = _build(
        values, observer_response=answer + "\nFinal:\n" + answer,
        cpa_response=_cpa_json(),
    )
    assert _run(values, callbacks)["final_class"] == "source_media"
    assert calls == {"observer": 1, "cpa": 1}

    values = _inputs(tmp_path / "conflict")
    callbacks, calls, _ = _build(
        values,
        observer_response=answer + "\nFinal:\n" + _observer_json(source="ABSENT", host="PRESENT"),
        cpa_response=_cpa_json(),
    )
    result = _run(values, callbacks)
    assert result["final_class"] == "unknown"
    assert result["mutation_authorized"] is False
    assert calls == {"observer": 1, "cpa": 0}


def test_native_nested_states_reaches_cpa_with_original_completion(tmp_path: Path):
    values = _inputs(tmp_path)
    flat = json.loads(_observer_json())
    nested = {key: flat[key] for key in ("status", "evidence")}
    nested["states"] = {key: value for key, value in flat.items() if key.endswith("_speech")}
    completion = " me```json\n" + json.dumps(nested) + "\n```"
    callbacks, calls, captured = _build(
        values, observer_response=completion, cpa_response=_cpa_json(),
    )
    result = _run(values, callbacks)
    assert result["final_class"] == "source_media"
    assert calls == {"observer": 1, "cpa": 1}
    bound = json.loads(captured["cpa_prompt"].split("BOUND_OBSERVATION_JSON:\n", 1)[1])
    assert bound["observer"]["evidence"]["provider_completion"] == completion
    assert bound["observer"]["evidence"]["provider_payload"] == nested


def test_current_cue7_nested_evidence_roles_reach_cpa_without_rewriting(tmp_path: Path):
    """The immutable cue-7 completion is valid once its evidence roles are read."""
    values = _inputs(tmp_path)
    values.update(
        interval_ms=[18090, 27090],
        caption_frame_ms=22590,
        target_interval_ms=[22090, 23090],
        target_cue_indexes=[7],
        target_audio_window_ms=[18090, 27090],
    )
    mapping_path = Path(values["mapping_receipt_path"])
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping["lineage_mapping"] = {
        "outer_window_ms": [18090, 27090],
        "inner_window_ms": [18083.0, 27083.0],
        "lag_ms": 7.0,
    }
    mapping_path.write_text(json.dumps(mapping, sort_keys=True), encoding="utf-8")
    values["mapping_receipt_sha256"] = (
        "sha256:" + hashlib.sha256(mapping_path.read_bytes()).hexdigest()
    )

    raw_payload = {
        "status": "OBSERVED",
        "target_audio_offset_ms": [4000, 5000],
        "target_interval_ms": [22090, 23090],
        "target_cue_indexes": [7],
        "evidence": {
            "source_media_speech": "PRESENT",
            "host_speech": "ABSENT",
            "overlap_speech": "ABSENT",
            "note": "The target interval contains replayed audio matching the independent source reference performance.",
        },
    }
    raw_completion = json.dumps(raw_payload, ensure_ascii=False)
    cpa_payload = json.loads(_cpa_json())
    cpa_payload.update(
        target_audio_offset_ms=[4000, 5000],
        target_interval_ms=[22090, 23090],
        target_cue_indexes=[7],
    )
    callbacks, calls, _captured = _build(
        values,
        observer_response=raw_completion,
        cpa_response=json.dumps(cpa_payload),
    )

    result = _run(values, callbacks)

    assert result["status"] == "PASS"
    assert result["final_class"] == "source_media"
    assert calls == {"observer": 1, "cpa": 1}
    observer = json.loads(Path(result["observer_response_path"]).read_text())
    assert observer["evidence"]["provider_completion"] == raw_completion
    assert observer["evidence"]["provider_payload"] == raw_payload
    assert observer["evidence"]["provider_evidence"] == raw_payload["evidence"]


@pytest.mark.parametrize("field", ["evidence", "instance_evidence"])
def test_role_explanations_preserve_states_and_raw_evidence(tmp_path: Path, field: str):
    values = _inputs(tmp_path)
    explanations = {
        "host_speech": "Live host voice speaking continuously in part 3.",
        "source_media_speech": "No replay or source media audio detected during the target interval.",
        "overlap_speech": "Single speaker voice heard without overlapping speech.",
    }
    payload = json.loads(_observer_json(source="ABSENT", host="PRESENT"))
    del payload["evidence"]
    payload[field] = explanations
    completion = json.dumps(payload)
    callbacks, calls, captured = _build(
        values, observer_response=completion,
        cpa_response=_cpa_json(source="ABSENT", host="PRESENT"),
    )
    result = _run(values, callbacks)
    assert result["final_class"] == "host"
    assert calls == {"observer": 1, "cpa": 1}
    bound = json.loads(captured["cpa_prompt"].split("BOUND_OBSERVATION_JSON:\n", 1)[1])
    assert bound["observer"]["evidence"]["provider_completion"] == completion
    assert bound["observer"]["evidence"]["provider_evidence"] == explanations


@pytest.mark.parametrize("case", ["partial_prose", "mixed", "invalid_enums", "missing_primary"])
def test_role_explanations_cannot_hide_invalid_or_missing_states(tmp_path: Path, case: str):
    values = _inputs(tmp_path)
    payload = json.loads(_observer_json())
    payload["evidence"] = {role: "A descriptive sentence." for role in (
        "source_media_speech", "host_speech", "overlap_speech"
    )}
    if case == "partial_prose":
        del payload["evidence"]["overlap_speech"]
    elif case == "mixed":
        payload["evidence"]["host_speech"] = "PRESENT"
    elif case == "invalid_enums":
        payload["evidence"] = {role: "MAYBE" for role in payload["evidence"]}
    else:
        for role in ("source_media_speech", "host_speech", "overlap_speech"):
            del payload[role]
    callbacks, calls, _ = _build(
        values, observer_response=json.dumps(payload), cpa_response=_cpa_json(),
    )
    result = _run(values, callbacks)
    assert result["final_class"] == "unknown"
    assert calls == {"observer": 1, "cpa": 0}


@pytest.mark.parametrize("case", ["partial", "invalid", "conflict"])
def test_nested_evidence_role_declarations_fail_closed(tmp_path: Path, case: str):
    values = _inputs(tmp_path)
    payload = {
        "status": "OBSERVED",
        "evidence": {
            "source_media_speech": "PRESENT",
            "host_speech": "ABSENT",
            "overlap_speech": "ABSENT",
            "note": "nested role evidence",
        },
    }
    if case == "partial":
        del payload["evidence"]["overlap_speech"]
    elif case == "invalid":
        payload["evidence"]["host_speech"] = "MAYBE"
    else:
        payload.update(
            source_media_speech="PRESENT",
            host_speech="ABSENT",
            overlap_speech="ABSENT",
        )
        payload["evidence"]["host_speech"] = "PRESENT"
    callbacks, calls, _captured = _build(
        values,
        observer_response=json.dumps(payload),
        cpa_response=_cpa_json(),
    )

    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["final_class"] == "unknown"
    assert calls == {"observer": 1, "cpa": 0}
    observer = json.loads(Path(result["observer_response_path"]).read_text())
    assert observer["evidence"]["reason_code"] == "PROVIDER_RESPONSE_INVALID"


@pytest.mark.parametrize("case", ["conflict", "partial", "invalid", "earlier_conflict"])
def test_nested_states_conflicts_and_invalid_declarations_keep(tmp_path: Path, case: str):
    values = _inputs(tmp_path)
    flat = json.loads(_observer_json())
    nested = {key: flat[key] for key in ("status", "evidence")}
    nested["states"] = {key: value for key, value in flat.items() if key.endswith("_speech")}
    if case == "conflict":
        nested.update({key: value for key, value in flat.items() if key.endswith("_speech")})
        nested["host_speech"] = "PRESENT"
    elif case == "partial":
        nested["host_speech"] = "ABSENT"
    elif case == "invalid":
        nested["states"]["host_speech"] = []
    completion = json.dumps(nested)
    if case == "earlier_conflict":
        completion = _observer_json(source="ABSENT", host="PRESENT") + "\n" + completion
    callbacks, calls, _ = _build(values, observer_response=completion, cpa_response=_cpa_json())
    result = _run(values, callbacks)
    assert result["final_class"] == "unknown"
    assert result["mutation_authorized"] is False
    assert calls == {"observer": 1, "cpa": 0}


def test_runtime_sends_three_parts_in_fixed_order_without_text_context(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, calls, captured = _build(
        values,
        observer_response=_observer_json(),
        cpa_response=_cpa_json(),
    )

    result = _run(values, callbacks)

    assert result["status"] == "PASS"
    assert calls == {"observer": 1, "cpa": 1}
    parts = captured["inline_parts"]
    assert [data for data, _mime in parts] == [
        Path(values["host_enrollment_path"]).read_bytes(),
        Path(values["source_audio_path"]).read_bytes(),
        Path(values["target_audio_path"]).read_bytes(),
    ]
    assert captured["prompt"] == captured["prompt"].strip()
    assert "temperature" not in captured
    assert captured["max_output_tokens"] == 2048
    assert "source words" not in captured["prompt"]
    assert "candidate_id" not in captured["prompt"]
    assert "source words" not in captured["cpa_prompt"]
    assert "candidate_id" not in captured["cpa_prompt"]
    request = json.loads(Path(result["request_path"]).read_text(encoding="utf-8"))
    assert request["part_order"] == ["host_enrollment", "independent_source", "current_target"]
    assert request["runtime_identity"]["model"] == "fixture-model-v1"
    assert request["runtime_identity"]["observer_prompt_sha256"].startswith("sha256:")
    assert request["runtime_identity"]["cpa_prompt_sha256"].startswith("sha256:")
    receipt = json.loads(Path(result["receipt_binding"]["path"]).read_text(encoding="utf-8"))
    observer_response = json.loads(Path(result["observer_response_path"]).read_text(encoding="utf-8"))
    cpa_response = json.loads(Path(result["cpa_response_path"]).read_text(encoding="utf-8"))
    assert receipt["evidence"]["runtime_identity_sha256"] == request["runtime_identity"]["identity_sha256"]
    assert observer_response["evidence"]["provider_request_sha256"].startswith("sha256:")
    assert observer_response["evidence"]["provider_response_sha256"].startswith("sha256:")
    assert cpa_response["evidence"]["cpa_response_sha256"].startswith("sha256:")


def test_voice_reference_prompts_are_explicit_and_runtime_bound(tmp_path: Path):
    values = _inputs(tmp_path)
    observer_calls = []
    cpa_calls = []

    def generate(**kwargs):
        observer_calls.append(kwargs)
        return _observer_json()

    def cpa(prompt: str):
        cpa_calls.append(prompt)
        return _cpa_json()

    cpa.cpa_cache_identity = dict(_CPA_IDENTITY)
    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate,
        cpa_call=cpa,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
        observer_prompt=VOICE_REFERENCE_OBSERVER_PROMPT,
        cpa_prompt=VOICE_REFERENCE_CPA_PROMPT,
    )
    assert "UNALIGNED VOICE REFERENCE" in VOICE_REFERENCE_OBSERVER_PROMPT
    assert "timing alignment" in VOICE_REFERENCE_OBSERVER_PROMPT
    assert "authorize a DROP" in VOICE_REFERENCE_CPA_PROMPT
    assert callbacks.runtime_identity["observer_prompt_sha256"] == (
        "sha256:" + hashlib.sha256(VOICE_REFERENCE_OBSERVER_PROMPT.encode()).hexdigest()
    )
    assert callbacks.runtime_identity["cpa_prompt_sha256"] == (
        "sha256:" + hashlib.sha256(VOICE_REFERENCE_CPA_PROMPT.encode()).hexdigest()
    )
    values["runtime_identity"] = dict(callbacks.runtime_identity)
    result = _run(values, callbacks)
    assert result["status"] == "PASS"
    assert len(observer_calls) == len(cpa_calls) == 1
    assert "UNALIGNED VOICE REFERENCE" in observer_calls[0]["prompt"]
    assert "timing alignment" in observer_calls[0]["prompt"]
    assert "authorize a DROP" in cpa_calls[0]


def test_runtime_shared_client_request_omits_gemini3_sampling(tmp_path: Path, monkeypatch):
    values = _inputs(tmp_path)
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self):
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": _observer_json()}]}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _Response()

    def generate(**kwargs):
        return agy_gemini_client.generate_content(key="secret", **kwargs)

    def cpa(_prompt: str):
        return _cpa_json()

    cpa.cpa_cache_identity = dict(_CPA_IDENTITY)
    monkeypatch.setattr(agy_gemini_client.urllib.request, "urlopen", fake_urlopen)
    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate,
        cpa_call=cpa,
        provider="gemini_api",
        model="gemini-3.6-flash",
        transport="src.autoslice.agy_gemini_client.generate_content",
        transport_module="src.autoslice.agy_gemini_client",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    values["runtime_identity"] = dict(callbacks.runtime_identity)

    result = _run(values, callbacks)

    assert result["status"] == "PASS"
    generation_config = captured["body"]["generationConfig"]
    assert "temperature" not in generation_config
    assert "thinkingConfig" not in generation_config


def test_provider_503_is_unknown_keep_and_never_calls_cpa(tmp_path: Path):
    values = _inputs(tmp_path)
    calls = {"observer": 0, "cpa": 0}

    class Http503(RuntimeError):
        status_code = 503

    def generate(**_kwargs):
        calls["observer"] += 1
        raise Http503("https://provider.invalid/?Authorization=secret")

    def cpa(_prompt):
        calls["cpa"] += 1
        raise AssertionError("provider failure must not call CPA")

    cpa.cpa_cache_identity = dict(_CPA_IDENTITY)

    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate,
        cpa_call=cpa,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    values["runtime_identity"] = dict(callbacks.runtime_identity)
    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "OBSERVER_UNKNOWN_KEEP"
    assert calls == {"observer": 1, "cpa": 0}
    for path in Path(values["evidence_root"]).rglob("*.json"):
        assert "secret" not in path.read_text(encoding="utf-8")


def test_structured_provider_error_keeps_outer_reason_and_typed_status(tmp_path: Path):
    values = _inputs(tmp_path)

    class StructuredProviderError(RuntimeError):
        reason_code = "AGY_STRUCTURED_RESULT_ERROR"
        provider_error = True
        terminal_status = "ERROR"
        http_status = 503

    def generate(**_kwargs):
        raise StructuredProviderError("provider diagnostics are not retained")

    def cpa(_prompt):
        raise AssertionError("provider failure must not call CPA")

    cpa.cpa_cache_identity = dict(_CPA_IDENTITY)
    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate,
        cpa_call=cpa,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    values["runtime_identity"] = dict(callbacks.runtime_identity)
    result = _run(values, callbacks)
    response = json.loads(Path(result["observer_response_path"]).read_text(encoding="utf-8"))
    evidence = response["evidence"]
    assert evidence["reason_code"] == "PROVIDER_ERROR"
    assert evidence["structured_reason_code"] == "AGY_STRUCTURED_RESULT_ERROR"
    assert evidence["terminal_status"] == "ERROR"
    assert evidence["http_status"] == 503


@pytest.mark.parametrize("http_status,expected_calls", [(429, 1), (503, 2)])
def test_actual_limit_stops_dispatch_for_other_cue_in_same_consumer(
    tmp_path: Path, http_status: int, expected_calls: int,
):
    calls = []

    class ProviderError(RuntimeError):
        status_code = http_status

    def generate(**_kwargs):
        calls.append(http_status)
        raise ProviderError("private provider diagnostics")

    def cpa(_prompt):
        raise AssertionError("unknown audio must not dispatch CPA")

    cpa.cpa_cache_identity = dict(_CPA_IDENTITY)
    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate, cpa_call=cpa, provider="fixture-gemini",
        model="fixture-model-v1", transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    results = []
    for index in (1, 2):
        values = _inputs(tmp_path / str(index))
        values["observation_id"] = f"different-cue-{index}"
        results.append(_run(values, callbacks))

    assert len(calls) == expected_calls
    assert all(result["status"] == "EVIDENCE_ONLY" for result in results)
    assert results[0]["request_sha256"] != results[1]["request_sha256"]
    response = json.loads(Path(results[1]["observer_response_path"]).read_text())
    if http_status == 429:
        assert response["evidence"]["reason_code"] == "PROVIDER_LIMIT_BLOCKED_CURRENT_RUN"
        assert response["evidence"]["provider_dispatched"] is False
        assert response["evidence"]["originating_request_sha256"] == results[0]["request_sha256"]
    else:
        assert response["evidence"]["reason_code"] == "PROVIDER_ERROR"


@pytest.mark.parametrize("missing_scope_at", [None, "observer", "cpa"])
def test_context_audio_is_bound_to_exact_target_cue_in_provider_and_cpa(
    tmp_path: Path, missing_scope_at: str | None,
):
    values = _inputs(tmp_path)
    focus = {"target_interval_ms": [120, 180], "target_cue_indexes": [7],
             "target_audio_offset_ms": [20, 80]}
    values.update({key: value for key, value in focus.items() if key != "target_audio_offset_ms"})
    observer = json.loads(_observer_json())
    cpa = json.loads(_cpa_json())
    if missing_scope_at != "observer":
        observer.update(focus)
    if missing_scope_at != "cpa":
        cpa.update(focus)
    callbacks, calls, captured = _build(values, observer_response=json.dumps(observer),
                                      cpa_response=json.dumps(cpa))
    result = _run(values, callbacks)
    assert '"target_audio_offset_ms":[20,80]' in captured["prompt"]
    assert '"target_cue_indexes":[7]' in captured["prompt"]
    assert "source words" not in captured["prompt"]
    if missing_scope_at is None:
        assert result["status"] == "PASS"
        receipt = json.loads(Path(result["receipt_binding"]["path"]).read_text())
        assert receipt["target_interval_ms"] == [120, 180]
        assert receipt["target_cue_indexes"] == [7]
        assert '"target_audio_offset_ms":[20,80]' in captured["cpa_prompt"]
    else:
        assert result["status"] == "EVIDENCE_ONLY"
        if missing_scope_at == "observer":
            assert calls["cpa"] == 0
        response_path = result["observer_response_path" if missing_scope_at == "observer" else "cpa_response_path"]
        response = json.loads(Path(response_path).read_text())
        assert response["evidence"]["reason_code"].endswith("TARGET_SCOPE_MISMATCH")
        # A rejected response must retain exact provider bytes for later
        # diagnosis/replay; parsed JSON alone cannot reproduce their hash.
        raw_key = "provider_completion" if missing_scope_at == "observer" else "cpa_completion"
        raw_response = observer if missing_scope_at == "observer" else cpa
        assert response["evidence"][raw_key] == json.dumps(raw_response)


@pytest.mark.parametrize("evidence_field", ["evidence", "instance_evidence"])
def test_nested_target_scope_echo_is_consumed_by_normal_observer_and_cpa(
    tmp_path: Path, evidence_field: str,
):
    values = _inputs(tmp_path)
    focus = {"target_interval_ms": [120, 180], "target_cue_indexes": [7],
             "target_audio_offset_ms": [20, 80]}
    values.update({key: value for key, value in focus.items() if key != "target_audio_offset_ms"})
    observer = json.loads(_observer_json())
    cpa = json.loads(_cpa_json())
    for payload in (observer, cpa):
        evidence = payload.pop("evidence")
        evidence.update(focus)
        payload[evidence_field] = evidence

    callbacks, calls, _captured = _build(
        values, observer_response=json.dumps(observer), cpa_response=json.dumps(cpa)
    )
    result = _run(values, callbacks)

    assert result["status"] == "PASS"
    assert result["final_class"] == "source_media"
    assert calls == {"observer": 1, "cpa": 1}
    observer_response = json.loads(Path(result["observer_response_path"]).read_text())
    assert observer_response["evidence"]["provider_completion"] == json.dumps(observer)
    assert observer_response["evidence"]["provider_payload"][evidence_field] == {
        "basis": "audio-only-fixture",
        **focus,
    }


@pytest.mark.parametrize("window,offset", [([100, 200], [20, 80]), ([120, 180], [0, 60])])
def test_actual_attachment_window_controls_clock_not_context(tmp_path, window, offset):
    values = _inputs(tmp_path)
    focus = {"target_interval_ms": [120, 180], "target_cue_indexes": [7]}
    values.update(**focus, target_audio_window_ms=window)
    answer = {**json.loads(_observer_json()), **focus, "target_audio_offset_ms": offset}
    judge = {**json.loads(_cpa_json()), **focus, "target_audio_offset_ms": offset}
    callbacks, calls, captured = _build(values, observer_response=json.dumps(answer),
                                      cpa_response=json.dumps(judge))
    result = _run(values, callbacks)
    request = json.loads(Path(result["request_path"]).read_text())
    assert request["target_audio_window_ms"] == window
    assert request["target_audio_offset_ms"] == offset
    assert "Listen ONLY at target_audio_offset_ms" in captured["prompt"]
    assert result["status"] == "PASS"
    assert calls == {"observer": 1, "cpa": 1}


@pytest.mark.parametrize("offset", [None, [120, 180]])
def test_outer_echo_without_correct_attachment_clock_does_not_dispatch_cpa(tmp_path, offset):
    values = _inputs(tmp_path)
    focus = {"target_interval_ms": [120, 180], "target_cue_indexes": [7]}
    values.update(focus)
    answer = {**json.loads(_observer_json()), **focus}
    if offset is not None:
        answer["target_audio_offset_ms"] = offset
    callbacks, calls, _ = _build(values, observer_response=json.dumps(answer),
                                 cpa_response=_cpa_json())
    result = _run(values, callbacks)
    assert result["status"] == "EVIDENCE_ONLY"
    assert calls == {"observer": 1, "cpa": 0}
    assert json.loads(Path(result["observer_response_path"]).read_text())["evidence"]["reason_code"] == "PROVIDER_TARGET_SCOPE_MISMATCH"


@pytest.mark.parametrize("invalid_shape", [
    "top_level_incomplete",
    "top_level_conflict",
    "nested_wrong_cue",
    "nested_wrong_interval",
    "nested_conflict",
])
def test_invalid_target_scope_declarations_never_fallback_to_nested_echo(
    tmp_path: Path, invalid_shape: str,
):
    values = _inputs(tmp_path)
    focus = {"target_interval_ms": [120, 180], "target_cue_indexes": [7]}
    values.update(focus)
    observer = json.loads(_observer_json())
    observer["evidence"].update(focus)
    if invalid_shape == "top_level_incomplete":
        observer["target_interval_ms"] = list(focus["target_interval_ms"])
    elif invalid_shape == "top_level_conflict":
        observer.update(target_interval_ms=[121, 180], target_cue_indexes=[7])
    elif invalid_shape == "nested_wrong_cue":
        observer["evidence"]["target_cue_indexes"] = [8]
    elif invalid_shape == "nested_wrong_interval":
        observer["evidence"]["target_interval_ms"] = [121, 180]
    else:
        observer["instance_evidence"] = {
            "target_interval_ms": [121, 180],
            "target_cue_indexes": [7],
        }

    callbacks, calls, _captured = _build(
        values, observer_response=json.dumps(observer), cpa_response=_cpa_json()
    )
    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "OBSERVER_UNKNOWN_KEEP"
    assert calls == {"observer": 1, "cpa": 0}
    response = json.loads(Path(result["observer_response_path"]).read_text())
    assert response["evidence"]["reason_code"] == "PROVIDER_TARGET_SCOPE_MISMATCH"


@pytest.mark.parametrize("evidence", ["Actual target-local audio evidence", "", None])
def test_text_evidence_preserves_raw_answer_without_requiring_object_shape(tmp_path, evidence):
    values = _inputs(tmp_path)
    observer = json.loads(_observer_json())
    observer["evidence"] = evidence
    cpa = json.loads(_cpa_json())
    cpa["evidence"] = evidence
    callbacks, _calls, _captured = _build(
        values, observer_response=json.dumps(observer), cpa_response=json.dumps(cpa))
    result = _run(values, callbacks)
    response = json.loads(Path(result["observer_response_path"]).read_text())
    assert response["evidence"]["provider_payload"]["evidence"] == evidence
    if evidence:
        assert result["status"] == "PASS"
        assert response["evidence"]["provider_evidence"] == {"summary": evidence}
        judged = json.loads(Path(result["cpa_response_path"]).read_text())
        assert judged["evidence"]["cpa_payload"]["evidence"] == evidence
        assert judged["evidence"]["cpa_evidence"] == {"summary": evidence}
    else:
        assert result["status"] == "EVIDENCE_ONLY"
        assert response["evidence"]["reason_code"] == "PROVIDER_EVIDENCE_MISSING"


def test_cpa_cannot_create_or_reverse_role_state(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, calls, _captured = _build(
        values,
        observer_response=_observer_json(),
        cpa_response=_cpa_json(source="ABSENT", host="PRESENT", overlap="ABSENT"),
    )

    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "CPA_UNKNOWN_KEEP"
    assert calls == {"observer": 1, "cpa": 1}
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_sequential_host_and_source_do_not_get_overlap_receipt(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, calls, _ = _build(
        values,
        observer_response=_observer_json(host="PRESENT", overlap="ABSENT"),
        cpa_response=_cpa_json(host="PRESENT", overlap="ABSENT"),
    )
    result = _run(values, callbacks)
    assert calls == {"observer": 1, "cpa": 1}
    assert result["status"] == "EVIDENCE_ONLY"
    assert result["final_class"] == "unknown"
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_runtime_identity_change_does_not_replay_old_receipt(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks_a, calls, _captured = _build(
        values,
        observer_response=_observer_json(),
        cpa_response=_cpa_json(),
    )
    first = _run(values, callbacks_a)
    assert first["status"] == "PASS"

    def generate_b(**_kwargs):
        calls["observer"] += 1
        return _observer_json()

    def cpa_b(_prompt):
        calls["cpa"] += 1
        return _cpa_json()

    cpa_b.cpa_cache_identity = dict(_CPA_IDENTITY)

    callbacks_b = build_runtime_acoustic_callbacks(
        audio_generate=generate_b,
        cpa_call=cpa_b,
        provider="fixture-gemini",
        model="fixture-model-v2",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    values["runtime_identity"] = dict(callbacks_b.runtime_identity)
    second = _run(values, callbacks_b)

    assert second["request_sha256"] != first["request_sha256"]
    assert second["replayed"] is False
    assert calls["observer"] == 2

    callbacks_c = build_runtime_acoustic_callbacks(
        audio_generate=generate_b,
        cpa_call=cpa_b,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
        observer_prompt="changed observer contract",
    )
    values["runtime_identity"] = dict(callbacks_c.runtime_identity)
    third = _run(values, callbacks_c)
    assert third["request_sha256"] not in {first["request_sha256"], second["request_sha256"]}
    assert third["replayed"] is False
    assert calls["observer"] == 3


def test_cpa_identity_change_does_not_replay_old_receipt(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks_a, calls, _captured = _build(
        values,
        observer_response=_observer_json(),
        cpa_response=_cpa_json(),
    )
    first = _run(values, callbacks_a)
    assert first["status"] == "PASS"

    def generate_b(**_kwargs):
        calls["observer"] += 1
        return _observer_json()

    def cpa_b(_prompt):
        calls["cpa"] += 1
        return _cpa_json()

    cpa_b.cpa_cache_identity = {
        "transport": "fixture.cpa",
        "models": ["fixture-cpa-v2"],
        "effort": "medium",
    }
    callbacks_b = build_runtime_acoustic_callbacks(
        audio_generate=generate_b,
        cpa_call=cpa_b,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    values["runtime_identity"] = dict(callbacks_b.runtime_identity)
    second = _run(values, callbacks_b)

    assert second["status"] == "PASS"
    assert second["request_sha256"] != first["request_sha256"]
    assert second["replayed"] is False
    assert calls == {"observer": 2, "cpa": 2}


def test_missing_cpa_identity_is_unknown_without_dispatch(tmp_path: Path):
    values = _inputs(tmp_path)
    calls = {"observer": 0, "cpa": 0}

    def generate(**_kwargs):
        calls["observer"] += 1
        raise AssertionError("missing CPA identity must not dispatch observer")

    def cpa(_prompt):
        calls["cpa"] += 1
        raise AssertionError("missing CPA identity must not dispatch CPA")

    callbacks = build_runtime_acoustic_callbacks(
        audio_generate=generate,
        cpa_call=cpa,
        provider="fixture-gemini",
        model="fixture-model-v1",
        transport="fixture.generate_content",
        transport_module="tests.fixture_transport",
        transport_module_sha256="sha256:" + "a" * 64,
    )
    assert callbacks.runtime_identity is None
    values.pop("runtime_identity")
    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "RUNTIME_IDENTITY_REQUIRED"
    assert calls == {"observer": 0, "cpa": 0}


def test_provider_evidence_and_raw_completion_are_required_for_definite_roles(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, calls, _captured = _build(
        values,
        observer_response=json.dumps(
            {
                "status": "OBSERVED",
                "source_media_speech": "PRESENT",
                "host_speech": "ABSENT",
                "overlap_speech": "ABSENT",
                "evidence": {},
            }
        ),
        cpa_response=_cpa_json(),
    )

    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "OBSERVER_UNKNOWN_KEEP"
    assert calls == {"observer": 1, "cpa": 0}
    observer_path = Path(result["observer_response_path"])
    observer = json.loads(observer_path.read_text(encoding="utf-8"))
    assert observer["source_media_speech"] == "UNKNOWN"
    assert observer["evidence"]["provider_completion"]
    assert observer["evidence"]["provider_payload"]["evidence"] == {}


def test_instance_evidence_is_consumed_without_rewriting_provider_completion(tmp_path: Path):
    values = _inputs(tmp_path)
    raw_observer = _observer_json().replace('"evidence":', '"instance_evidence":')
    raw_cpa = _cpa_json().replace('"evidence":', '"instance_evidence":')
    callbacks, calls, _captured = _build(
        values, observer_response=raw_observer, cpa_response=raw_cpa
    )

    result = _run(values, callbacks)

    assert result["status"] == "PASS"
    assert result["final_class"] == "source_media"
    assert calls == {"observer": 1, "cpa": 1}
    observer = json.loads(Path(result["observer_response_path"]).read_text())
    cpa = json.loads(Path(result["cpa_response_path"]).read_text())
    assert observer["evidence"]["provider_completion"] == raw_observer
    assert observer["evidence"]["provider_evidence_field"] == "instance_evidence"
    assert cpa["evidence"]["cpa_completion"] == raw_cpa
    assert cpa["evidence"]["cpa_evidence_field"] == "instance_evidence"
    assert "response_adapter_sha256" in callbacks.runtime_identity


@pytest.mark.parametrize("invalid_evidence", [{}, None, " ", False, []])
def test_instance_evidence_cannot_override_explicit_invalid_evidence(
    tmp_path: Path, invalid_evidence
):
    values = _inputs(tmp_path)
    payload = json.loads(_observer_json())
    payload["instance_evidence"] = payload["evidence"]
    payload["evidence"] = invalid_evidence
    callbacks, calls, _captured = _build(
        values, observer_response=json.dumps(payload), cpa_response=_cpa_json()
    )

    result = _run(values, callbacks)

    assert result["final_class"] == "unknown"
    assert calls == {"observer": 1, "cpa": 0}
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))


def test_cpa_raw_completion_and_parsed_evidence_are_bound(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, _calls, _captured = _build(
        values,
        observer_response=_observer_json(),
        cpa_response=_cpa_json(),
    )

    result = _run(values, callbacks)

    cpa = json.loads(Path(result["cpa_response_path"]).read_text(encoding="utf-8"))
    evidence = cpa["evidence"]
    assert evidence["cpa_completion"]
    assert evidence["cpa_payload"]["evidence"] == {"basis": "observer-only-fixture"}
    assert evidence["cpa_evidence"] == {"basis": "observer-only-fixture"}
    assert evidence["cpa_bound_prompt_sha256"].startswith("sha256:")


def test_cpa_without_basis_cannot_create_receipt(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, calls, _captured = _build(
        values,
        observer_response=_observer_json(),
        cpa_response=json.dumps(
            {
                "source_media_speech": "PRESENT",
                "host_speech": "ABSENT",
                "overlap_speech": "ABSENT",
                "evidence": {},
            }
        ),
    )

    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "CPA_UNKNOWN_KEEP"
    assert calls == {"observer": 1, "cpa": 1}
    assert not list(Path(values["evidence_root"]).rglob("acoustic-receipt.json"))
    cpa = json.loads(Path(result["cpa_response_path"]).read_text(encoding="utf-8"))
    assert cpa["evidence"]["cpa_payload"]["evidence"] == {}


def test_malformed_observer_json_stays_unknown(tmp_path: Path):
    values = _inputs(tmp_path)
    callbacks, calls, _captured = _build(
        values,
        observer_response="not json",
        cpa_response=_cpa_json(),
    )

    result = _run(values, callbacks)

    assert result["status"] == "EVIDENCE_ONLY"
    assert result["reason_code"] == "OBSERVER_UNKNOWN_KEEP"
    assert calls == {"observer": 1, "cpa": 0}
