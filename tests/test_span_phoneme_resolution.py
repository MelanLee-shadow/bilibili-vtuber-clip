"""Ordinary adjudicator controls: only low-level observations/model replies fake."""

from copy import deepcopy
import hashlib
import json
import socket

import pytest

from src.autoslice.final_review_auditor import (
    adjudicate_context_finding,
    build_context_adjudication_request,
)
from src.autoslice.local_asr_target_evidence import MODELS, exact_target_evidence, digest
from src.autoslice.native_context_witness import _native_transcript_witness, _prepare_request

FIELD = "span_context_resolution"
POLICY = "CONTEXT_PHONEME_FIT"


def validated_resolution(*args, **kwargs):
    # Lazy loading keeps the same public pre/post regression runnable on the
    # unmodified baseline, where the positive output assertion fails first.
    from src.autoslice.span_phoneme_resolution import validated_resolution as validate

    return validate(*args, **kwargs)


SOURCE_SHA = "a" * 64


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


@pytest.fixture(autouse=True)
def no_network(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "runtime"))

    def no(*args, **kwargs):
        raise AssertionError("No network/provider allowed in these offline controls")

    monkeypatch.setattr(socket.socket, "connect", no)
    monkeypatch.setattr(socket, "create_connection", no)


def srt(text):
    return f"1\n00:00:01,000 --> 00:00:04,000\n{text}\n"


class Observers:
    span_termination_ordinary_routes = ("primary", "mai", "moss")

    def span_termination_source_identity(self):
        return SOURCE_SHA

    def __init__(self, *, heard="ni xiang jia ling ya", transcripts=None):
        self.heard = heard
        self.transcripts = transcripts or {"mai": "你很想，甲零呀", "moss": "你想，甲零呀"}
        self.calls = []
        self.missing = None
        self.damage = None
        self.foreign = None
        self.span_termination_upgrade_enabled = False

    def __call__(self, target):
        self.calls.append(("primary", deepcopy(target)))
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "blind_pinyin",
            "request_sha256": target["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": self.heard,
            "syllable_count": len(self.heard.split()),
            "uncertain_positions": [],
            "confidence": 0.9,
            "source_media_sha256": SOURCE_SHA,
            "audio_clip_sha256": sha(b"primary audio"),
            "prompt_sha256": sha("primary prompt"),
            "response_sha256": sha(self.heard),
            "provider": "agy",
            "model": "fixture-primary",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
            "audio_start_ms": target["matched_start_ms"]
            + target["source_media_timeline_offset_ms"],
            "audio_end_ms": target["matched_end_ms"] + target["source_media_timeline_offset_ms"],
        }

    def observe_independent_witnesses(self, target):
        self.calls.append(("independent", deepcopy(target)))
        prepared = _prepare_request(target)
        result = []
        for provider, text in self.transcripts.items():
            if provider == self.missing:
                continue
            audio = b"synthetic exact cue audio"
            raw = {
                "provider": provider,
                "model": MODELS[provider],
                "input_audio_sha256": sha(audio),
                "response_sha256": sha(provider + text),
                "native_segments": [{"start_ms": 0, "end_ms": 3000, "text": text}],
            }
            native = exact_target_evidence(
                raw,
                audio=audio,
                source_sha256=SOURCE_SHA,
                start_ms=prepared.physical_start_ms,
                end_ms=prepared.physical_end_ms,
            )
            native["observation_scope"] = "complete_exact_cue_no_padding"
            native.pop("receipt_sha256")
            native["receipt_sha256"] = digest(native)
            row = _native_transcript_witness(
                request=target,
                prepared=prepared,
                native_observation=native,
                source_sha256=SOURCE_SHA,
                provider=provider,
            )
            if provider == "moss" and self.damage:
                if self.damage == "coverage":
                    row["audio_end_ms"] -= 10
                elif self.damage == "source":
                    row["source_media_sha256"] = "b" * 64
                elif self.damage == "request":
                    row["request_sha256"] = "c" * 64
                elif self.damage == "native":
                    row["native_observation"]["transcript"] = "Changed without native sealing"
                elif self.damage == "network":
                    row.update(status="UNCERTAIN", reason_code="NATIVE_NETWORK_UNAVAILABLE")
                elif self.damage == "dependency":
                    row.update(status="UNCERTAIN", reason_code="NATIVE_DEPENDENCY_UNAVAILABLE")
            result.append(row)
        return result


def model_for(
    *,
    target_quote="甲零",
    blind_quote="jia ling",
    choice="PROPOSED",
    located=True,
    outside=True,
    raw_mutator=None,
    audio=True,
    question_span=None,
):
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        reply = {
            "choice": choice,
            "candidate_id": "PROPOSAL",
            "needs_audio": audio,
            "current_utterance_supported": False,
            "current_utterance_support_reason": "Synthetic unresolved prefix.",
            "reason": "Synthetic contextual preference; prefix is a separate question.",
        }
        if located and "\nSPAN_REVIEW_INPUT\n" in prompt:
            data = json.loads(
                prompt.split("\nSPAN_REVIEW_INPUT\n", 1)[1].split("\nEND_SPAN_REVIEW_INPUT\n", 1)[0]
            )
            # This fixture models a new low-level response to the real
            # request's minimal scope; original recorded responses are never
            # rewritten. Trim only the known synthetic unchanged term flank.
            minimal = data["binding"]["replacement"]
            trim_left = target_quote.find(minimal)
            trim_right = len(target_quote) - trim_left - len(minimal)
            if trim_left < 0 or not all("\u4e00" <= c <= "\u9fff" for c in target_quote):
                trim_left = trim_right = 0
            alignments = []
            for observed in data["observations"]:
                if observed["kind"] == "blind_pinyin" and blind_quote is None:
                    continue
                value = observed["text"]
                quote = blind_quote if observed["kind"] == "blind_pinyin" else target_quote
                if observed["kind"] == "blind_pinyin":
                    parts = value.split()
                    wanted = quote.split()
                    left = next(
                        (i for i in range(len(parts)) if parts[i : i + len(wanted)] == wanted), 0
                    )
                    right = left + len(wanted)
                    if blind_quote and len(blind_quote.split()) == len(target_quote):
                        left += trim_left
                        right -= trim_right
                    actual = " ".join(parts[left:right])
                else:
                    left = value.find(quote)
                    if left < 0:
                        # Keep actual conflicting sound text; do not make a matching fake answer.
                        left = max(value.find("甲"), 0)
                        right = left + len(quote)
                    else:
                        right = left + len(quote)
                    left += trim_left
                    right -= trim_right
                    actual = value[left:right]
                alignments.append(
                    {
                        "observation_id": observed["observation_id"],
                        "span": [left, right],
                        "quote": actual,
                    }
                )
            binding = data["binding"]
            start, end = binding["codepoint_span"]
            interval = question_span or ([0, start] if outside and start > 0 else [start, end])
            reply["span_review"] = {
                "binding_sha256": binding["binding_sha256"],
                "request_sha256": data["request_sha256"],
                "context_basis": "A bounded name spelling fits the supplied conversation.",
                "all_audio_questions_located": True,
                "target_needs_audio": not outside,
                "alignments": alignments,
                "audio_questions": [
                    {
                        "codepoint_span": interval,
                        "reason": "Synthetic remaining audible question.",
                        "observation_ids": [x["observation_id"] for x in data["observations"]],
                    }
                ],
            }
        if raw_mutator is not None:
            raw_mutator(reply)
        return json.dumps(reply, ensure_ascii=False)

    model.prompts = prompts
    return model


def run_case(
    *, text="你想，甲0呀", old="甲0", new="甲零", verifier=None, model=None, finding_extra=None
):
    verifier = verifier or Observers()
    model = model or model_for()
    finding = {
        "cue_index": 1,
        "suspect": old,
        "suggestion": new,
        "repair_class": "phonetic",
        "base_text_sha256": sha(text),
        "proposed_full_cue": text.replace(old, new, 1),
        **(finding_extra or {}),
    }
    output, audit = adjudicate_context_finding(
        srt(text),
        finding,
        entity_verifier=verifier,
        judge_llm_call=model,
        require_complete_utterance_support=True,
    )
    return output, audit, verifier, model, finding


def test_public_adjudicator_terminates_target_and_preserves_raw_needs_audio():
    output, audit, verifier, model, _ = run_case()
    assert output == srt("你想，甲零呀"), audit.get("witness_judge")
    assert audit["repaired"] and audit["policy_branch"] == POLICY
    assert audit["witness_judge"]["judge"]["needs_audio"] is True
    assert audit["witness_judge"]["judge"]["needs_audio_requested"] is True
    resolution = validated_resolution(audit[FIELD], srt_text=srt("你想，甲0呀"))
    assert resolution["outside_findings"]
    assert all(row["status"] == "UNRESOLVED" for row in resolution["outside_findings"])
    assert all(row["compatible"] for row in resolution["comparisons"])
    assert any(
        row["comparison_basis"] == "PINYIN_DERIVED_FROM_BOUND_TRANSCRIPTION_NOT_RAW_PHONEMES"
        for row in resolution["comparisons"]
    )
    assert {name for name, _ in verifier.calls} == {"primary", "independent"}
    assert "同音字形的文字裁决" in model.prompts[0]


@pytest.mark.parametrize(
    "defect",
    [
        "CURRENT",
        "NEITHER",
        "unlocated",
        "different_phoneme",
        "extra_syllable",
        "missing_route",
        "coverage",
        "source",
        "request",
        "native",
        "network",
        "dependency",
    ],
)
def test_public_adjudicator_cannot_force_an_unsupported_target(defect):
    verifier = Observers()
    kwargs = {}
    model = model_for()
    if defect in {"CURRENT", "NEITHER"}:
        model = model_for(choice=defect)
    elif defect == "unlocated":
        model = model_for(located=False)
    elif defect == "different_phoneme":
        verifier.transcripts["moss"] = "你想，甲蓝呀"
    elif defect == "extra_syllable":
        kwargs = {"text": "你想，甲凌云", "old": "甲凌云", "new": "甲零"}
        verifier = Observers(
            heard="ni xiang jia ling yun", transcripts={"mai": "你想，甲零", "moss": "你想，甲凌云"}
        )
    elif defect == "missing_route":
        verifier.missing = "moss"
    else:
        verifier.damage = defect
    output, audit, _, _, _ = run_case(verifier=verifier, model=model, **kwargs)
    assert output == srt(kwargs.get("text", "你想，甲0呀"))
    assert not audit.get("repaired")
    assert FIELD not in audit


@pytest.mark.parametrize(
    "state",
    [
        "AVAILABLE",
        "GRANTED",
        "RUNNING",
        "OUTCOME_UNKNOWN",
        "NOT_AUTHORIZED",
        "DEPENDENCY_UNAVAILABLE",
        "NETWORK_UNAVAILABLE",
    ],
)
def test_enabled_upgrade_must_reach_a_real_terminal_before_target_application(state):
    verifier = Observers()

    def upgrade(request):
        return {
            "enabled": True,
            "status": state,
            "edit_binding_sha256": request["edit_span_binding"]["binding_sha256"],
            "submission_outcome": state,
            "observations": [],
        }

    verifier.span_termination_upgrade_status = upgrade
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]


def test_enabled_upgrade_without_status_cannot_be_treated_as_disabled():
    verifier = Observers()
    verifier.span_termination_upgrade_enabled = True
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]


@pytest.mark.parametrize(
    "change", ["source_text", "clock", "span", "raw_answer", "route", "outside_question"]
)
def test_cold_resolution_replay_rejects_drift(change):
    _, audit, *_ = run_case()
    value = deepcopy(audit[FIELD])
    if change == "source_text":
        value["request"]["current_cue"] = "Changed prefix"
    elif change == "clock":
        value["request"]["matched_start_ms"] += 1
    elif change == "span":
        value["binding"]["codepoint_span"][0] += 1
    elif change == "raw_answer":
        value["judge"]["needs_audio"] = False
    elif change == "route":
        value["route_state"]["ordinary"][1]["status"] = "AVAILABLE"
    else:
        value["outside_findings"] = []
    with pytest.raises((ValueError, KeyError, TypeError)):
        validated_resolution(value)


def test_repeated_current_surface_requires_exact_codepoint_location():
    text = "甲0甲0"
    with pytest.raises(ValueError):
        build_context_adjudication_request(
            srt(text), {"cue_index": 1, "suspect": "甲0", "suggestion": "甲零"}
        )
    request = build_context_adjudication_request(
        srt(text),
        {
            "cue_index": 1,
            "suspect": "甲0",
            "suggestion": "甲零",
            "span_start_codepoint": 2,
            "span_end_codepoint": 4,
            "proposed_full_cue": "甲0甲零",
        },
    )
    assert request["edit_span_binding"]["codepoint_span"] == [3, 4]
    assert request["declared_edit_scope"]["codepoint_span"] == [2, 4]


def upgrade_observation(request, text="你想，甲零呀", language="zh"):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.exact_source_transcript_contract import (
        build_exact_source_transcript_request,
        seal_exact_source_transcript_observation,
    )
    from src.autoslice.entity_audio_verifier import _prepare_audio_span

    native_request = build_exact_source_transcript_request(build_witness_request(request))
    span = _prepare_audio_span(
        request=native_request,
        context_mode=True,
        source_media_timeline_offset_ms=0,
        source_duration_ms=10000,
        witness_mode=True,
    )
    observed = seal_exact_source_transcript_observation(
        request=native_request,
        exact_transcript=text,
        audible_language=language,
        source_media_sha256=SOURCE_SHA,
        audio_clip_sha256=sha(b"upgrade-audio"),
        provider="agy",
        model="fixture-upgrade",
        response_sha256=sha("upgrade:" + text),
        timeline_binding=span.timeline_binding,
    )
    return {
        "enabled": True,
        "status": "COMPLETED",
        "submission_outcome": "COMPLETED",
        "edit_binding_sha256": request["edit_span_binding"]["binding_sha256"],
        "native_receipt_sha256": digest(observed),
        "observations": [observed],
    }


def test_completed_native_upgrade_is_consumed_not_waited_on_again():
    verifier = Observers()
    calls = []

    def existing(request):
        calls.append(request["edit_span_binding"]["binding_sha256"])
        return upgrade_observation(request)

    verifier.span_termination_upgrade_status = existing
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲零呀"), audit.get("witness_judge", {}).get(
        "span_termination_blocked_reason"
    )
    assert len(calls) == 1
    assert len(audit[FIELD]["comparisons"]) == 4
    validated_resolution(json.loads(json.dumps(audit[FIELD])))


@pytest.mark.parametrize(
    "damage", ["foreign_schema", "native_seal", "wrong_target", "wrong_source", "unobserved"]
)
def test_completed_label_alone_cannot_make_upgrade_evidence_valid(damage):
    verifier = Observers()

    def existing(request):
        state = upgrade_observation(request)
        row = state["observations"][0]
        if damage == "foreign_schema":
            row["schema_version"] = "unsupported-experiment"
        elif damage == "native_seal":
            row["exact_transcript"] = "Invented after observation"
        elif damage == "wrong_target":
            state["edit_binding_sha256"] = "b" * 64
        elif damage == "wrong_source":
            row["source_media_sha256"] = "b" * 64
        else:
            row["status"] = "UNKNOWN"
        return state

    verifier.span_termination_upgrade_status = existing
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]


def test_chinese_repeated_prefix_is_preserved_when_foreign_target_is_resolved():
    verifier = Observers(
        heard="ta shuo ta shuo mi tou kang duo li ya de su",
        transcripts={
            "mai": "どうぞどうぞ、ミトコンドリアです",
            "moss": "他说，他说，ミトコンドリアです",
        },
    )
    verifier.span_termination_upgrade_status = lambda req: upgrade_observation(
        req, "どうぞどうぞ、ミトコンドリアです", "mixed"
    )
    output, audit, *_ = run_case(
        text="他说，他说，米头康多里亚です",
        old="米头康多里亚",
        new="ミトコンドリア",
        verifier=verifier,
        model=model_for(target_quote="ミトコンドリア", blind_quote=None, question_span=[0, 5]),
    )
    assert output == srt("他说，他说，ミトコンドリアです"), audit.get("witness_judge", {}).get(
        "span_termination_blocked_reason"
    )
    proof = validated_resolution(audit[FIELD])
    assert proof["outside_findings"]
    assert all(
        "PINYIN" not in row["comparison_basis"] or "NO_PINYIN_INFERRED" in row["comparison_basis"]
        for row in proof["comparisons"]
    )
    assert len(proof["excluded_protocol_observations"]) == 1


@pytest.mark.parametrize(
    "defect", ["no_original_language", "wrong_native_language", "wrong_foreign_word"]
)
def test_foreign_branch_does_not_use_mandarin_or_incompatible_native_text(defect):
    verifier = Observers(
        heard="ta shuo ta shuo mi tou kang duo li ya de su",
        transcripts={
            "mai": "他说，他说，ミトコンドリアです",
            "moss": "他说，他说，ミトコンドリアです",
        },
    )
    if defect != "no_original_language":
        verifier.span_termination_upgrade_status = lambda req: upgrade_observation(
            req,
            "他说，他说，ミトコンドリアです"
            if defect != "wrong_foreign_word"
            else "他说，他说，リボソームです",
            "zh" if defect == "wrong_native_language" else "mixed",
        )
    output, audit, *_ = run_case(
        text="他说，他说，米头康多里亚です",
        old="米头康多里亚",
        new="ミトコンドリア",
        verifier=verifier,
        model=model_for(target_quote="ミトコンドリア", blind_quote=None, question_span=[0, 5]),
    )
    assert output == srt("他说，他说，米头康多里亚です") and not audit["repaired"]


@pytest.mark.parametrize("text", ["你想，甲零甲龄呀", "你想，甲零零呀"])
def test_repeated_or_homophonic_adjacent_phonemes_cannot_be_assigned_away(text):
    verifier = Observers(
        heard="ni xiang jia ling ya", transcripts={"mai": text, "moss": "你想，甲零呀"}
    )
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]


def test_available_unheard_wide_route_prevents_termination():
    verifier = Observers()
    verifier.span_termination_ordinary_routes += ("range:gemini",)
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]
    assert (
        audit["witness_judge"]["span_termination_blocked_reason"]
        == "SPAN_ORDINARY_ROUTES_NOT_EXHAUSTED"
    )


def test_correct_current_cannot_be_changed_by_terminal_rule():
    output, audit, *_ = run_case(
        text="你想，甲零呀", old="甲零", new="甲铃", model=model_for(choice="CURRENT")
    )
    assert output == srt("你想，甲零呀") and not audit["repaired"]


def test_installed_but_explicitly_disabled_upgrade_does_not_wait():
    verifier = Observers()
    verifier.span_termination_upgrade_status = lambda _: {
        "enabled": False,
        "status": "DISABLED",
        "observations": [],
    }
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲零呀") and audit["repaired"]


@pytest.mark.parametrize("actual_source", [None, "b" * 64])
def test_missing_or_changed_actual_runtime_source_cannot_accept_matching_witness_labels(
    actual_source,
):
    verifier = Observers()
    verifier.span_termination_source_identity = lambda: actual_source
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]


def test_single_configured_ordinary_route_does_not_require_unconfigured_native_routes():
    verifier = Observers()
    verifier.span_termination_ordinary_routes = ("primary",)
    verifier.observe_independent_witnesses = None
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲零呀"), audit.get("witness_judge", {}).get(
        "span_termination_blocked_reason"
    )
    assert [r["route"] for r in audit[FIELD]["route_state"]["ordinary"]] == ["primary"]


def test_english_target_uses_original_language_not_mandarin_transliteration():
    verifier = Observers(
        heard="ta shuo mao dou ya",
        transcripts={"mai": "他刚说，model呀", "moss": "他说，model呀"},
    )
    verifier.span_termination_upgrade_status = lambda req: upgrade_observation(
        req, "他说，model呀", "mixed"
    )
    output, audit, *_ = run_case(
        text="他说，猫豆呀",
        old="猫豆",
        new="model",
        verifier=verifier,
        model=model_for(target_quote="model", blind_quote=None, question_span=[0, 2]),
    )
    assert output == srt("他说，model呀"), {
        "status": audit.get("status"), "reason_code": audit.get("reason_code"),
        "policy_branch": audit.get("policy_branch"), "error_type": audit.get("error_type"),
        "keys": sorted(audit),
        "span_reason": audit.get("witness_judge", {}).get("span_termination_blocked_reason"),
    }
    assert all(
        row["comparison_basis"] == "NATIVE_ORIGINAL_LANGUAGE_LITERAL_AGREEMENT_NO_PINYIN_INFERRED"
        for row in audit[FIELD]["comparisons"]
    )


def test_overlapping_repetition_requires_explicit_location_before_provider_dispatch():
    text = "年年年"
    finding = {
        "cue_index": 1,
        "suspect": "年年",
        "suggestion": "年零",
        "repair_class": "phonetic",
        "proposed_full_cue": "年零年",
    }
    # str.count is non-overlapping: it reports 1 although there are two
    # possible codepoint intervals, [0, 2) and [1, 3).
    with pytest.raises(ValueError, match="SPAN_LOCATION_AMBIGUOUS"):
        build_context_adjudication_request(srt(text), finding)


def test_explicit_overlapping_repetition_flows_through_ordinary_adjudicator():
    verifier = Observers(
        heard="nian ling nian",
        transcripts={"mai": "年零年", "moss": "年零年"},
    )
    output, audit, *_ = run_case(
        text="年年年",
        old="年年",
        new="年零",
        verifier=verifier,
        model=model_for(
            target_quote="年零", blind_quote="nian ling", outside=False, question_span=[1, 2]
        ),
        finding_extra={"span_start_codepoint": 0, "span_end_codepoint": 2},
    )
    assert output == srt("年零年")
    proof = validated_resolution(audit[FIELD])
    assert proof["binding"]["codepoint_span"] == [1, 2]
    assert proof["outside_findings"] == []
    assert audit["witness_judge"]["judge"]["needs_audio"] is True


def test_foreign_words_outside_mandarin_target_remain_an_independent_question():
    verifier = Observers(
        transcripts={"mai": "OK，甲零呀", "moss": "你想，甲零呀"}
    )
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲零呀"), audit.get("witness_judge", {}).get(
        "span_termination_blocked_reason"
    )
    proof = validated_resolution(audit[FIELD])
    assert proof["outside_findings"]
    assert all(row["status"] == "UNRESOLVED" for row in proof["outside_findings"])
    assert all(row["proposed_units"] == ["ling"] for row in proof["comparisons"])


def test_foreign_separator_cannot_hide_a_second_homophonic_target():
    verifier = Observers(
        transcripts={"mai": "你想，甲零OK甲龄呀", "moss": "你想，甲零呀"}
    )
    output, audit, *_ = run_case(verifier=verifier)
    assert output == srt("你想，甲0呀") and not audit["repaired"]
    assert audit["witness_judge"]["span_termination_blocked_reason"] == (
        "SPAN_OBSERVATION_LOCATION_AMBIGUOUS"
    )


def test_overlong_foreign_candidate_retains_existing_edit_length_guard():
    output, audit, verifier, model, _ = run_case(
        text="他说，米头呀", old="米头", new="mitochondria",
        model=model_for(target_quote="mitochondria", blind_quote=None),
    )
    assert output == srt("他说，米头呀") and not audit["repaired"]
    assert audit["reason_code"] == "EDIT_LENGTH_DELTA_TOO_LARGE"
    assert not verifier.calls and not model.prompts
