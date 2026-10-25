import hashlib
import json
from copy import deepcopy

import pytest

from src.autoslice import final_review_auditor as auditor
from src.autoslice.acoustic_witness_adjudication import (
    REQUIRE_COMPLETE_UTTERANCE_SUPPORT,
    judge_word_choice,
)
from src.autoslice.final_review_auditor import (
    FinalReviewAuditError,
    adjudicate_context_finding,
    adjudicate_exact_release_findings,
    audit_correction_mutation_authority,
    audit_final_subtitles,
    build_context_adjudication_request,
    route_findings,
)
from src.autoslice.screen_read_witness import screen_read_token_alignment


def _srt(*texts: str) -> str:
    blocks = []
    for index, text in enumerate(texts, start=1):
        blocks.append(
            f"{index}\n00:00:{index * 5:02d},000 --> 00:00:{index * 5 + 4:02d},000\n{text}"
        )
    return "\n\n".join(blocks) + "\n"


def _literal_review_llm(callback):
    """Keep literal-mechanism fixtures unresolved under the current risk policy.

    These fixtures exercise proposal bounds, provenance, and acoustic routing;
    they provide no positive evidence of contextual meaning equivalence.
    Explicit risk responses and malformed response structures stay untouched.
    """
    def call(prompt):
        if "本次不授权改字、删字或听音调度" in prompt:
            agenda = json.loads(prompt.rsplit("\n", 1)[-1])
            return json.dumps({"findings": [
                {"cue": row["cue"], "content_risk": "meaning_reversal",
                 "risk_basis": f"当前整句「{row['current_cue']}」的提名疑点仍未解决，须继续逐字核实。"}
                for row in agenda
            ]}, ensure_ascii=False)
        raw = callback(prompt)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if not isinstance(parsed, dict) or not isinstance(parsed.get("findings"), list):
            return raw
        for row in parsed["findings"]:
            if isinstance(row, dict) and "content_risk" not in row:
                row.update(
                    content_risk="meaning_reversal",
                    risk_basis="本例的逐字疑点尚未解决，交普通 typed 核实；未声称当前句意等价。",
                )
        return json.dumps(parsed, ensure_ascii=False)

    return call


def _fake_llm(findings):
    return _literal_review_llm(
        lambda _prompt: json.dumps({"findings": findings}, ensure_ascii=False)
    )


def _extract(raw):
    return json.loads(raw)


@pytest.mark.parametrize("current,proposed", [
    ("我有23个", "我有2、3个"),
    ("2、3个跟没谈过有什么区别", "23个跟没谈过有什么区别"),
])
def test_segmentation_separator_has_bounded_candidate_but_no_mutation(current, proposed):
    source = _srt(current)
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([{
            "cue": 1, "kind": "context", "repair_class": "segmentation",
            "proposed_full_cue": proposed, "why": "数字边界需核对",
        }]),
        extract_json=_extract,
    )
    assert findings[0]["suspect"]
    assert findings[0]["suggestion"]
    assert findings[0]["proposed_full_cue"] == proposed
    request = build_context_adjudication_request(source, findings[0])
    assert request["proposed_cue"] == proposed
    unchanged, audit = route_findings(source, findings)
    assert unchanged == source
    assert audit["applied_count"] == 0


def test_explicit_no_candidate_whole_cue_doubt_is_preserved_for_cpa():
    source = _srt("对了，你是天气吗")
    calls = []
    findings = audit_final_subtitles(
        source,
        llm_call=lambda prompt: calls.append(prompt) or _fake_llm([{
            "cue": 1, "kind": "context", "repair_class": "disclosure_only",
            "proposed_full_cue": None, "why": "没有安全候选，需补听核对",
        }])(prompt),
        extract_json=_extract,
    )
    assert len(calls) == 1
    assert findings[0]["suspect"] == "对了，你是天气吗"
    assert findings[0]["suggestion"] is None
    assert findings[0]["proposed_full_cue"] is None
    unchanged, audit = route_findings(source, findings)
    assert unchanged == source
    assert audit["applied_count"] == 0


def test_segmentation_separator_does_not_admit_lexical_insertion():
    with pytest.raises(FinalReviewAuditError, match="FINDINGS_ALL_INVALID"):
        audit_final_subtitles(
            _srt("我有23个"),
            llm_call=_fake_llm([{
                "cue": 1, "suspect": "23", "repair_class": "segmentation",
                "proposed_full_cue": "我有230个", "why": "未见证的增字",
            }]),
            extract_json=_extract,
        )


def test_range_context_carryover_is_only_a_srt_file_drift_fallback(monkeypatch):
    source = _srt("第一句", "第二句", "当前句", "第四句")
    finding = {
        "cue_index": 3,
        "suspect": "当前",
        "suggestion": "目前",
        "proposed_full_cue": "目前句",
        "repair_class": "phonetic",
        "_prior_range_acoustic_observations": [{"observation": "old"}],
    }
    reason = {"value": "RANGE_ACOUSTIC_SRT_FILE_DRIFT"}
    calls = []

    def strict(_srt, _request, observation):
        assert observation == {"observation": "old"}
        return None, reason["value"]

    def carryover(srt, request, observation):
        calls.append((srt, request, observation))
        return {"context_only": True, "authority": "EVIDENCE_ONLY"}, None

    monkeypatch.setattr(auditor, "validate_range_acoustic_observation", strict)
    monkeypatch.setattr(
        auditor,
        "adapt_range_acoustic_context_observation",
        carryover,
    )
    request = build_context_adjudication_request(source, finding)
    assert request["context_only_range_acoustic_observations"] == [
        {"context_only": True, "authority": "EVIDENCE_ONLY"}
    ]
    assert len(calls) == 1

    reason["value"] = "RANGE_ACOUSTIC_SOURCE_FILE_DRIFT"
    rejected_request = build_context_adjudication_request(source, finding)
    assert "context_only_range_acoustic_observations" not in rejected_request
    assert len(calls) == 1


@pytest.mark.parametrize("source_drift", [False, True])
def test_missing_candidate_proposal_and_convergence_receive_validated_range_audio(
    monkeypatch, tmp_path, source_drift,
):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt("前文", "当前句", "后文")
    observation = {
        "provider": "mai", "authority": "EVIDENCE_ONLY",
        "transcript_scope": "RANGE_WITHOUT_SINGLE_CUE_TIMESTAMPS",
        "verdict": {"transcript": "整句补听资料", "response_sha256": "a" * 64},
        "current_cue_texts": [{"cue_index": 2, "text": "当前稿语境"}],
        "native_word_overlap_evidence": {
            "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
            "coordinate_system": "delivery_clip_ms",
            "table": {"rows": [{"cue_spans": [
                {"n": 2, "start_ms": 2100, "end_ms": 2900,
                 "text": "原始词时间观察"},
            ]}]},
        },
    }
    monkeypatch.setattr(
        auditor, "validate_range_acoustic_observation",
        lambda *_args: (None, "RANGE_ACOUSTIC_SOURCE_FILE_DRIFT")
        if source_drift else (observation, None),
    )
    calls = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps({"status": "UNRESOLVED", "proposed_cue": ""})
        return json.dumps({"decision": "UNRESOLVED", "replacement_text": "", "reason": "证据仍不足"})

    output, audit = adjudicate_context_finding(
        source,
        {"cue_index": 2, "suspect": "当前句", "suggestion": None,
         "proposed_full_cue": None, "repair_class": "phonetic",
         "_prior_range_acoustic_observations": [observation]},
        entity_verifier=lambda _request: pytest.fail("no new audio was requested"),
        judge_llm_call=cpa,
    )
    assert len(calls) == 2
    assert all(("整句补听资料" in prompt) is not source_drift for prompt in calls)
    assert all(("当前稿语境" in prompt) is not source_drift for prompt in calls)
    assert all(("原始词时间观察" in prompt) is not source_drift for prompt in calls)
    assert output == source
    assert audit["status"] == "MISSING_PROPOSAL_UNRESOLVED"
    assert audit["cpa_missing_proposal_convergence"]["status"] == "UNRESOLVED"
    assert audit["cpa_missing_proposal_convergence"]["mutation_authorized"] is False
    if not source_drift:
        for receipt in (audit["proposal_bootstrap"], audit["cpa_missing_proposal_convergence"]):
            assert receipt["range_acoustic_context_count"] == 1
            assert receipt["range_acoustic_context_authority"] == "EVIDENCE_ONLY"
    else:
        assert "range_acoustic_context_count" not in audit["proposal_bootstrap"]
    replay, replay_audit = adjudicate_context_finding(
        source,
        {"cue_index": 2, "suspect": "当前句", "suggestion": None,
         "proposed_full_cue": None, "repair_class": "phonetic",
         "_prior_range_acoustic_observations": [observation]},
        entity_verifier=lambda _request: pytest.fail("no new audio was requested"),
        judge_llm_call=lambda _prompt: pytest.fail("unchanged uncertainty must be cached"),
    )
    assert replay == source
    assert replay_audit["status"] == "MISSING_PROPOSAL_UNRESOLVED"
    assert replay_audit["proposal_bootstrap"]["served_from_cache"] is True
    assert replay_audit["cpa_missing_proposal_convergence"]["served_from_cache"] is True


@pytest.mark.parametrize("needs_audio", [False, True])
@pytest.mark.parametrize("force_acoustic", [False, True])
def test_final_context_judge_asks_for_audio_only_when_text_is_unresolved(needs_audio, force_acoustic):
    source = _srt("这首歌还欠着呢", "还没有歌杂呢", "之后再唱", "换个话题", "说回刚才欠的歌")
    events = []

    def judge(prompt):
        events.append("CPA")
        if len(events) == 1:
            assert "TEXT_FIRST" in prompt
            assert "说回刚才欠的歌" in prompt
        return json.dumps({"choice": "PROPOSED", "needs_audio": needs_audio and len(events) == 1,
                           "ranking": [{"choice": "PROPOSED", "p": .9},
                                       {"choice": "CURRENT", "p": .09},
                                       {"choice": "NEITHER", "p": .01}],
                           "reason": "欠歌的语境支持歌债"}, ensure_ascii=False)

    def audio(request):
        events.append("AUDIO")
        assert "whole_clip_current_srt" not in request
        return _witness(request, "hai mei you ge zhai ne")

    output, audit = adjudicate_context_finding(
        source, {"cue_index": 2, "suspect": "歌杂", "suggestion": "歌债",
                 "proposed_full_cue": "还没有歌债呢", "repair_class": "phonetic",
                 "why": "欠歌话题", "force_acoustic": force_acoustic},
        entity_verifier=audio, judge_llm_call=judge,
    )
    assert events == (["CPA", "AUDIO", "CPA"] if needs_audio else ["CPA"])
    assert "还没有歌债呢" in output
    assert "00:00:10,000 --> 00:00:14,000" in output
    assert audit["mutation_authority"]["status"] == "PASS"
    assert audit["verdict"]["status"] == ("OBSERVED" if needs_audio else "UNCERTAIN")


def test_history_cpa_needs_audio_binds_a_fresh_request_before_fetching():
    source = _srt("三二")
    finding, old_request = _history_audio_finding(source)
    events = []

    def audio(request):
        events.append(("AUDIO", request))
        return _witness(request, "san er")

    def judge(prompt):
        events.append(("CPA", prompt))
        return json.dumps({
            "choice": "CURRENT",
            "reason": "补听后当前整句成立",
            "current_utterance_supported": True,
            "current_utterance_support_reason": "完整句音频与当前稿一致",
        }, ensure_ascii=False)

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=audio,
        judge_llm_call=judge,
        require_complete_utterance_support=True,
    )

    assert [event[0] for event in events] == ["AUDIO", "CPA"]
    assert output == source
    assert audit["status"] == "OBSERVED"
    assert audit["history_audio_recovery"]["status"] == "BLOCK"
    assert audit["history_audio_recovery"]["cpa_reason_code"] == (
        "CPA_REQUESTS_MORE_AUDIO"
    )
    assert audit["request"]["request_sha256"] != old_request["request_sha256"]
    assert audit["request"]["history_convergence_audio_recovery"]["reason_code"] == (
        "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    )


def test_history_cpa_needs_audio_uses_strict_cache_before_fetch():
    source = _srt("三二")
    finding, _old_request = _history_audio_finding(source)
    events = []

    class CachedVerifier:
        def probe_witness_cache(self, request):
            events.append(("CACHE", request))
            return {
                **_witness(request, "san er"),
                "served_from_cache": True,
            }

        def __call__(self, _request):
            events.append(("AUDIO", _request))
            raise AssertionError("strict cache hit must avoid a fresh fetch")

    def judge(prompt):
        events.append(("CPA", prompt))
        return json.dumps({
            "choice": "CURRENT",
            "reason": "缓存见证支持当前稿",
            "current_utterance_supported": True,
            "current_utterance_support_reason": "完整句支持",
        }, ensure_ascii=False)

    _output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=CachedVerifier(),
        judge_llm_call=judge,
        require_complete_utterance_support=True,
    )

    assert [event[0] for event in events] == ["CACHE", "CPA"]
    assert audit["verdict"]["served_from_cache"] is True


def test_history_cpa_needs_audio_precedes_missing_proposal_keep_existing():
    source = _srt("三二")
    finding, old_request = _history_audio_finding(source)
    finding["_cpa_missing_proposal_convergence"] = {
        "status": "RESOLVED",
        "decision": "KEEP_EXISTING",
    }
    events = []

    class Verifier:
        def probe_witness_cache(self, request):
            events.append(("CACHE", request))
            return None

        def __call__(self, request):
            events.append(("AUDIO", request))
            return _witness(request, "san er")

    def judge(prompt):
        events.append(("CPA", prompt))
        return json.dumps({
            "choice": "CURRENT",
            "reason": "补听后当前整句成立",
            "current_utterance_supported": True,
            "current_utterance_support_reason": "完整句支持",
        }, ensure_ascii=False)

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=Verifier(),
        judge_llm_call=judge,
        require_complete_utterance_support=True,
    )

    assert output == source
    assert [event[0] for event in events] == ["CACHE", "AUDIO", "CPA"]
    assert audit["status"] == "OBSERVED"
    assert audit["history_audio_recovery"]["status"] == "BLOCK"
    assert audit["request"]["request_sha256"] != old_request["request_sha256"]


def test_history_cpa_needs_audio_keeps_request_bound_uncertain_pending():
    source = _srt("三二")
    finding, _old_request = _history_audio_finding(source)
    events = []

    class UncertainVerifier:
        def probe_witness_cache(self, request):
            events.append(("CACHE", request))
            return None

        def __call__(self, request):
            events.append(("AUDIO", request))
            uncertain = _witness(request, "san er")
            uncertain.update(
                status="UNCERTAIN",
                reason_code="TARGET_TRANSCRIPTION_UNCERTAIN",
            )
            return uncertain

    def judge(prompt):
        events.append(("CPA", prompt))
        raise AssertionError(
            "a request-bound UNCERTAIN witness must not enter text-first CPA"
        )

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=UncertainVerifier(),
        judge_llm_call=judge,
        require_complete_utterance_support=True,
    )

    assert output == source
    assert [event[0] for event in events] == ["CACHE", "AUDIO"]
    assert audit["status"] == "UNCERTAIN"
    assert audit["repaired"] is False
    assert audit["reason_code"] == (
        "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    )
    assert audit["verdict"]["status"] == "UNCERTAIN"
    assert audit["verdict"]["reason_code"] == "TARGET_TRANSCRIPTION_UNCERTAIN"
    assert audit["verdict"]["request_sha256"] == events[1][1]["request_sha256"]
    assert audit["history_audio_recovery"]["status"] == "BLOCK"
    assert audit["history_audio_recovery"]["cpa_reason_code"] == (
        "CPA_REQUESTS_MORE_AUDIO"
    )
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"


@pytest.mark.parametrize("invalid", ["uncertain", "drift"])
def test_invalid_or_uncertain_history_audio_gate_cannot_close_or_fetch(invalid):
    source = _srt("三二")
    finding, _old_request = _history_audio_finding(
        source,
        gate_status="UNCERTAIN" if invalid == "uncertain" else "BLOCK",
        drift=invalid == "drift",
    )
    finding["_cpa_missing_proposal_convergence"] = {
        "status": "RESOLVED",
        "decision": "KEEP_EXISTING",
    }
    events = []

    def audio(_request):
        events.append("AUDIO")
        raise AssertionError("invalid history gate must not dispatch audio")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=audio,
        judge_llm_call=lambda _prompt: events.append("CPA") or json.dumps({
            "choice": "CURRENT", "reason": "伪造 gate 不应关闭",
        }),
    )

    assert output == source
    assert events == []
    assert audit["status"] == "UNCERTAIN"
    assert audit["repaired"] is False
    assert audit["reason_code"] == "HISTORY_CONVERGENCE_AUDIO_GATE_INVALID"


def test_same_cue_reentry_keeps_prior_audio_in_text_judge_only():
    from src.autoslice.deferred_same_cue_resolution import adjudicate_routed_findings

    text = "我们把链子看完"
    source = _srt(text, text)
    findings = []
    for cue, suspect, suggestion in ((1, "链子", "恋死"), (1, "链", "片"), (2, "链", "片")):
        start = text.index(suspect)
        findings.append({
            "cue_index": cue, "suspect": suspect, "suggestion": suggestion,
            "proposed_full_cue": text.replace(suspect, suggestion),
            "base_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "span_start_codepoint": start, "span_end_codepoint": start + len(suspect),
            "repair_class": "phonetic",
        })
    prompts = []
    audio_requests = []

    def judge(prompt):
        prompts.append(prompt)
        if len(prompts) == 3:
            assert "同一源时间窗此前已取得的音频观察" in prompt
            assert "wo men ba lian zi kan wan" in prompt
            assert '"prior_suspect": "链子"' in prompt
        if len(prompts) == 4:
            assert "prior_acoustic_observations" not in prompt
            assert "同一源时间窗此前已取得的音频观察" not in prompt
        return json.dumps({
            "choice": "CURRENT", "needs_audio": len(prompts) == 1,
            "ranking": [{"choice": "CURRENT", "p": .95},
                        {"choice": "PROPOSED", "p": .04},
                        {"choice": "NEITHER", "p": .01}],
            "reason": "结合已有证据保留当前文字",
        }, ensure_ascii=False)

    def audio(request):
        audio_requests.append(request)
        assert "prior_acoustic_observations" not in request
        return _witness(request, "wo men ba lian zi kan wan")

    output, *_ = adjudicate_routed_findings(
        source, findings, max_adjudications=3,
        adjudicate=lambda current, finding: adjudicate_context_finding(
            current, finding, entity_verifier=audio, judge_llm_call=judge,
        ),
    )
    assert output == source
    assert len(prompts) == 4 and len(audio_requests) == 1
    history = findings[1]["context_audio_adjudication"]["request"]["prior_acoustic_observations"]
    assert history[0]["verdict"]["request_sha256"] == audio_requests[0]["request_sha256"]
    assert findings[1]["context_audio_adjudication"]["verdict"]["audio_attempted"] is False


def test_secondary_handoff_keeps_distinct_provider_receipts_but_dedupes_exact_copy(
    monkeypatch,
):
    source = _srt("他说今天会来")
    finding = {
        "cue_index": 1,
        "suspect": "他说",
        "suggestion": "TA说",
        "proposed_full_cue": "TA说今天会来",
        "repair_class": "phonetic",
    }
    mai = {
        "source_request_sha256": "same-request",
        "provider": "mai",
        "receipt_sha256": "receipt-mai",
    }
    moss = {
        "source_request_sha256": "same-request",
        "provider": "moss",
        "receipt_sha256": "receipt-moss",
    }
    captured = {}

    monkeypatch.setattr(
        auditor,
        "prepare_missing_proposal_candidate",
        lambda **kwargs: (kwargs["finding"], None, None),
    )

    def build_request(_srt, current_finding, **_kwargs):
        captured["observations"] = current_finding[
            "_prior_secondary_audio_observations"
        ]
        return {
            "request_sha256": "request",
            "current_cue": "他说今天会来",
            "proposed_cue": "TA说今天会来",
            "repair_class": "phonetic",
        }

    monkeypatch.setattr(auditor, "build_context_adjudication_request", build_request)
    monkeypatch.setattr(
        auditor,
        "dispatch_context_witness",
        lambda **_kwargs: (
            {"status": "UNCERTAIN", "reason_code": "TEST_ONLY"},
            {"request_sha256": "request"},
            None,
            None,
        ),
    )

    auditor.adjudicate_context_finding(
        source,
        {
            **finding,
            "_prior_secondary_audio_observations": [mai],
        },
        entity_verifier=None,
        prior_secondary_audio_observations=[mai, moss],
    )

    assert captured["observations"] == [mai, moss]


@pytest.mark.parametrize("invalid", ["binding", "window"])
def test_prior_audio_context_rejects_wrong_binding_or_source_window(invalid):
    from src.autoslice.acoustic_witness_adjudication import build_witness_request

    source = _srt("我们把链子看完")
    finding = {"cue_index": 1, "suspect": "链子", "suggestion": "片子",
               "proposed_full_cue": "我们把片子看完", "repair_class": "phonetic"}
    prior = build_context_adjudication_request(source, finding)
    witness = _witness(build_witness_request(prior), "wo men ba lian zi kan wan")
    if invalid == "binding":
        witness["request_sha256"] = "f" * 64
    else:
        prior["matched_start_ms"] += 1
    request = build_context_adjudication_request(source, {
        **finding, "_prior_acoustic_observations": [{"request": prior, "verdict": witness}],
    })
    assert "prior_acoustic_observations" not in request


def test_auditor_validates_and_drops_unanchored_findings():
    source = _srt("欢迎季下", "正常的一句话")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "suspect": "季",
                "replacement": "记",
                "kind": "nonword",
                "proposed_full_cue": "欢迎记下",
                "repair_class": "phonetic",
                "why": "非词",
            },
            {"cue": 2, "suspect": "不存在的片段", "kind": "context", "why": "x"},
            {"cue": 99, "suspect": "正常", "kind": "context", "why": "越界"},
        ]),
        extract_json=_extract,
    )

    assert len(findings) == 1
    assert findings[0]["suspect"] == "季"


def test_router_does_not_apply_ungrounded_homophone_spelling():
    """A reviewer proposal is not textual authority for a homophone spelling."""
    source = _srt("欢迎季下", "今天小雨来了没")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"},
        {"cue_index": 2, "suspect": "小雨", "kind": "self_ref", "suggestion": "小主", "why": "自称可疑"},
    ]

    output, audit = route_findings(source, findings, protected_term_set=frozenset())

    assert "欢迎季下" in output
    assert "小雨" in output  # 非同音建议不改写
    assert audit["applied_count"] == 0
    routed = {row["suspect"]: row["routed"] for row in audit["findings"]}
    assert routed == {"季下": "disclosure", "小雨": "disclosure"}
    assert audit["findings"][0]["orthography_authority"]["status"] == "BLOCK"


def test_router_never_touches_protected_chat_cues():
    source = _srt("欢迎季下")
    findings = [
        {"cue_index": 1, "suspect": "季下", "kind": "nonword", "suggestion": "记下", "why": "非词"}
    ]

    output, audit = route_findings(
        source, findings, protected_cue_indexes=[1], protected_term_set=frozenset()
    )

    assert "季下" in output
    assert audit["applied_count"] == 0
    assert audit["findings"][0]["routed"] == "disclosure_protected"


def test_protected_meme_terms_never_auto_fixed():
    """抽查实证：审片员想把梗词「立语」同音改成「俚语」。词典
    权威高于审片直觉——钦定词面只披露永不自动改写。"""
    from src.autoslice.final_review_auditor import protected_terms

    source = _srt("怎么还有立语啊")
    findings = [
        {"cue_index": 1, "suspect": "立语", "kind": "nonword", "suggestion": "俚语", "why": "x"}
    ]

    output, audit = route_findings(
        source, findings, protected_term_set=frozenset({"立语"})
    )

    assert "立语" in output
    assert "俚语" not in output
    assert audit["findings"][0]["routed"] == "disclosure_protected_term"
    assert audit["applied_count"] == 0

    # 真实词典加载必须护住 立语 与 做0.4（glossary 行首术语解析回归）
    real = protected_terms()
    assert "立语" in real
    assert "做0.4" in real


def test_exact_cue_truth_can_restore_phrase_containing_registered_peer():
    source = _srt("你磕李墨的意思是说李就是李")
    canonical = "你磕礼豆沙的意思是说礼就是1"
    findings = [
        {
            "cue_index": 1,
            "suspect": "李墨的意思是说李就是李",
            "suggestion": "礼豆沙的意思是说礼就是1",
            "proposed_full_cue": canonical,
            "candidate_provenance": {
                "kind": "glossary",
                "surface": canonical,
            },
            "repair_class": "source_backed_entity",
            "kind": "context",
            "why": "整句已由源真值审定",
        }
    ]

    output, audit = route_findings(
        source,
        findings,
        protected_term_set=frozenset({"李墨", "礼豆沙"}),
        registered_term_set=frozenset({"李墨", "礼豆沙"}),
        exact_cue_canon_set=frozenset({canonical}),
    )

    assert canonical in output
    assert audit["applied_count"] == 1
    assert audit["findings"][0]["routed"] == "exact_cue_canon"
    assert audit["findings"][0]["mutation_authority"] == {
        "schema_version": "exact-cue-canon-authority.v1",
        "status": "PASS",
        "decision_authority": "EXPLICIT_SOURCE_TRUTH",
        "canonical_cue": canonical,
        "registered_name_conflict": False,
    }
    mutation_audit = audit_correction_mutation_authority(audit)
    assert mutation_audit["status"] == "PASS"
    assert mutation_audit["validated_mutation_count"] == 1


def test_auditor_llm_failure_blocks_instead_of_becoming_clean():
    def broken(prompt):
        raise RuntimeError("cpa down")

    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("一句"), llm_call=broken, extract_json=_extract
        )

    assert (
        raised.value.reason_code
        == "FINAL_REVIEW_PROVIDER_OR_JSON_UNAVAILABLE"
    )


@pytest.mark.parametrize(
    ("payload", "reason_code"),
    [
        ({}, "FINAL_REVIEW_RESPONSE_FINDINGS_MISSING"),
        ({"findings": None}, "FINAL_REVIEW_RESPONSE_FINDINGS_INVALID"),
        ({"findings": {}}, "FINAL_REVIEW_RESPONSE_FINDINGS_INVALID"),
        (
            {"findings": [{"cue": 99, "suspect": "不存在"}]},
            "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID",
        ),
    ],
)
def test_auditor_malformed_response_never_collapses_to_empty_findings(
    payload, reason_code
):
    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("一句"),
            llm_call=lambda _prompt: json.dumps(payload),
            extract_json=_extract,
        )

    assert raised.value.reason_code == reason_code


def test_auditor_only_explicit_empty_findings_is_clean_discovery():
    assert (
        audit_final_subtitles(
            _srt("一句"),
            llm_call=lambda _prompt: '{"findings":[]}',
            extract_json=_extract,
        )
        == []
    )


def test_auditor_reasks_cpa_once_after_all_findings_are_schema_invalid():
    prompts = []
    responses = iter(
        [
            '{"findings":[{"cue":99,"suspect":"不存在"}]}',
            '{"findings":[]}',
        ]
    )

    def retrying_llm(prompt):
        prompts.append(prompt)
        return next(responses)

    assert (
        audit_final_subtitles(
            _srt("一句"),
            llm_call=retrying_llm,
            extract_json=_extract,
        )
        == []
    )
    assert len(prompts) == 2
    assert "上一轮返回了非空 findings" not in prompts[0]
    assert "上一轮返回了非空 findings" in prompts[1]
    assert "CUE_OUT_OF_RANGE" in prompts[1]
    assert '"cue": 99' in prompts[1]
    assert "不是新一轮全片审查" in prompts[1]


def test_auditor_schema_retry_cannot_introduce_a_new_finding():
    responses = iter(
        [
            '{"findings":[{"cue":1,"kind":"context","why":"疑点无改法"}]}',
            json.dumps(
                {
                    "findings": [
                        {
                            "cue": 2,
                            "kind": "context",
                            "proposed_full_cue": "第二句修正版",
                            "repair_class": "phonetic",
                            "why": "新找了另一条",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )

    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("第一句", "第二句"),
            llm_call=_literal_review_llm(lambda _prompt: next(responses)),
            extract_json=_extract,
        )

    assert raised.value.reason_code == "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID"
    assert "SCHEMA_REPAIR_NEW_FINDING_FORBIDDEN" in raised.value.detail


def test_auditor_schema_retry_diagnostics_bind_original_row_and_cue_text():
    prompts = []
    responses = iter(
        [
            json.dumps(
                {
                    "findings": [
                        {"cue": 1, "kind": "context", "why": "口吃可疑"}
                    ]
                },
                ensure_ascii=False,
            ),
            '{"findings":[]}',
        ]
    )

    assert audit_final_subtitles(
        _srt("因因为我一会要玩游戏"),
        llm_call=_literal_review_llm(lambda prompt: prompts.append(prompt) or next(responses)),
        extract_json=_extract,
    ) == []
    assert '"current_cue": "因因为我一会要玩游戏"' in prompts[1]
    assert '"why": "口吃可疑"' in prompts[1]


def test_auditor_schema_retry_turns_unbounded_doubt_into_bounded_proposal():
    prompts = []
    responses = iter(
        [
            '{"findings":[{"cue":1,"kind":"context","why":"量词语境可疑"}]}',
            json.dumps(
                {
                    "findings": [
                        {
                            "cue": 1,
                            "kind": "context",
                            "proposed_full_cue": "他一副很无辜的样子",
                            "repair_class": "phonetic",
                            "why": "一副与样子构成固定搭配",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        ]
    )

    findings = audit_final_subtitles(
        _srt("他欺负很无辜的样子"),
        llm_call=_literal_review_llm(lambda prompt: (
            prompts.append(prompt) or next(responses)
        )),
        extract_json=_extract,
    )

    assert findings[0]["proposed_full_cue"] == "他一副很无辜的样子"
    assert findings[0]["suspect"] == "欺负"
    assert findings[0]["suggestion"] == "一副"
    assert "NO_BOUNDED_SPAN_OR_PROPOSAL" in prompts[1]


def test_auditor_schema_repair_retry_is_bounded_and_fail_closed():
    calls = 0

    def always_invalid(_prompt):
        nonlocal calls
        calls += 1
        return '{"findings":[{"cue":99,"suspect":"不存在"}]}'

    with pytest.raises(
        FinalReviewAuditError,
        match="FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID",
    ):
        audit_final_subtitles(
            _srt("一句"),
            llm_call=always_invalid,
            extract_json=_extract,
        )

    assert calls == 2


def test_auditor_prompt_distinguishes_gibberish_code_switch_from_real_foreign_dialogue():
    captured = {}

    def review(prompt):
        captured["prompt"] = prompt
        return '{"findings":[]}'

    audit_final_subtitles(
        _srt("因为主播是侄女，kowa，kowai", "本物の気持ちです"),
        llm_call=review,
        extract_json=_extract,
    )

    prompt = captured["prompt"]
    assert "侄女，kowa，kowai" in prompt
    assert "真正的日语、英语对白" in prompt
    assert "不能翻译" in prompt
    assert "重复或近乎平行的句式" in prompt
    assert "直女/侄女" in prompt
    assert "不行不行，并非不行" in prompt


def test_auditor_derives_bounded_edit_despite_advisory_scope_mismatch():
    source = _srt("地狱在理解，觉得成人吗")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "suspect": "地狱在理解",
                "replacement": "地狱再爱我，觉得成人吗",
                "kind": "context",
                "proposed_full_cue": "地狱再爱我，觉得成人吗",
                "repair_class": "phonetic",
                "why": "作品名",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suspect"] == "在理解"
    assert findings[0]["suggestion"] == "再爱我"
    assert findings[0]["proposed_full_cue"] == "地狱再爱我，觉得成人吗"
    assert findings[0]["reported_scope_warnings"] == [
        "REPORTED_SUSPECT_SCOPE_MISMATCH",
        "REPORTED_REPLACEMENT_SCOPE_MISMATCH",
    ]


def test_auditor_keeps_bounded_full_cue_when_advisory_suspect_is_not_verbatim():
    source = _srt("以后做可以煮吗")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "suspect": "以后做，可以煮吗",
                "replacement": "以后做可以煮久点",
                "kind": "context",
                "proposed_full_cue": "以后做可以煮久点",
                "repair_class": "phonetic",
                "why": "原句语境不完整",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suspect"] == "吗"
    assert findings[0]["suggestion"] == "久点"
    assert findings[0]["proposed_full_cue"] == "以后做可以煮久点"
    assert findings[0]["reported_scope_warnings"] == [
        "REPORTED_SUSPECT_SCOPE_MISMATCH",
        "REPORTED_REPLACEMENT_SCOPE_MISMATCH",
    ]


def test_auditor_normalizes_machine_verifiable_cue_index_alias():
    findings = audit_final_subtitles(
        _srt("所以剩一点给我"),
        llm_call=_fake_llm([
            {
                "cue_index": 1,
                "kind": "context",
                "proposed_full_cue": "所以顺便带我",
                "repair_class": "phonetic",
                "why": "语境和近音均支持",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["cue_index"] == 1
    assert findings[0]["suspect"] == "剩一点给"
    assert findings[0]["suggestion"] == "顺便带"
    assert findings[0]["input_contract_normalizations"] == [
        "cue_index_to_cue"
    ]


def test_auditor_all_invalid_error_records_bounded_rejection_diagnostics():
    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            _srt("一句"),
            llm_call=lambda _prompt: json.dumps(
                {
                    "findings": [
                        {"cue_index": 99, "suspect": "不存在"},
                        {"suspect": "一句"},
                    ]
                },
                ensure_ascii=False,
            ),
            extract_json=_extract,
        )

    detail = json.loads(raised.value.detail)
    assert detail["raw_count"] == 2
    assert detail["invalid_rows"] == [
        {
            "reason": "CUE_OUT_OF_RANGE",
            "cue": 99,
            "cue_count": 1,
            "returned_row": {"cue_index": 99, "suspect": "不存在"},
        },
        {
            "reason": "CUE_MISSING_OR_INVALID",
            "keys": ["suspect"],
            "returned_row": {"suspect": "一句"},
        },
    ]


def test_auditor_derives_title_span_only_when_source_surface_is_witnessed():
    source = _srt("书名叫地狱再爱我", "地狱在理解，觉得成人吗")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 2,
                "kind": "entity",
                "proposed_full_cue": "地狱再爱我，你觉得成人吗",
                "repair_class": "source_backed_entity",
                "source_surface": "地狱再爱我",
                "evidence_cue_ids": [1],
                "why": "前一句给出书名",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suspect"] == "在理解，"
    assert findings[0]["suggestion"] == "再爱我，你"
    assert findings[0]["candidate_provenance"] == {
        "kind": "transcript_context",
        "surface": "地狱再爱我",
        "nearest_cue_distance": 1,
    }


def test_auditor_can_cite_structured_chat_as_name_spelling_evidence():
    source = _srt("但是因为提")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "kind": "entity",
                "proposed_full_cue": "但是因为xyz",
                "repair_class": "source_backed_entity",
                "source_surface": "xyz",
                "why": "同一时间窗弹幕重复使用该专名",
            }
        ]),
        extract_json=_extract,
        structured_context_text="danmaku @1000ms: 大家：xyz是这样的",
    )

    assert findings[0]["suggestion"] == "xyz"
    assert findings[0]["candidate_provenance"] == {
        "kind": "structured_context",
        "surface": "xyz",
    }


def test_auditor_rejects_reviewer_only_proper_name():
    source = _srt("那群 P 7赖我的群绝对不止有我一个人")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "kind": "entity",
                "proposed_full_cue": "那群P7Live的群绝对不止有我一个人",
                "repair_class": "source_backed_entity",
                "source_surface": "P7Live",
                "why": "猜测组织名",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suggestion"] is None
    assert findings[0]["suggestion_rejected_reason"] == "ENTITY_SOURCE_SURFACE_UNWITNESSED"


@pytest.mark.parametrize("context_source", ["transcript", "glossary", "structured"])
def test_explicit_entity_disclosure_cannot_gain_source_authority_from_text_echo(context_source):
    source = _srt("风风风雨子前妻", "感觉是风雨子前妻那种")
    # A single replacement character can occur in unrelated prose. It is not
    # a witnessed complete name, even if several context surfaces contain it.
    if context_source == "transcript":
        source = _srt("风风风雨子前妻", "人与自然")
    kwargs = {
        "glossary_text": "人物与作品" if context_source == "glossary" else "",
        "structured_context_text": "与观众互动" if context_source == "structured" else "",
    }
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([{
            "cue": 1, "kind": "entity", "repair_class": "disclosure_only",
            "proposed_full_cue": "风风风与子前妻", "source_surface": None,
            "why": "仅有近音候选，没有可靠专名来源",
        }]),
        extract_json=_extract,
        **kwargs,
    )
    finding = findings[0]
    assert finding["repair_class"] == "disclosure_only"
    assert finding["suggestion"] is None
    assert finding["suggestion_rejected_reason"] == "REPAIR_CLASS_DISCLOSURE_ONLY"
    assert finding["candidate_provenance"] is None
    assert "source_surface_inference" not in finding
    unchanged, audit = route_findings(source, findings)
    assert unchanged == source
    assert audit["applied_count"] == 0


def test_parallel_repeat_recovers_misclassified_entity_provenance_but_keeps_audio_gate():
    """7/22 大哥骂赢案：终审已经发现同槽重复，却因模型把 entity
    标成 phonetic 且漏 source_surface 而只披露。完整替换词面在后文逐字
    重复时可恢复 provenance，但引入注册实体仍不得走纯文本自动改写。"""

    source = _srt(
        "小主又被大哥骂赢了",
        "哪里又变成小主被大N霸凌了",
    )
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "entity",
                    "proposed_full_cue": "小主又被大N霸凌了",
                    "repair_class": "phonetic",
                    "evidence_cue_ids": [2],
                    "why": "后文立即平行复述同一句式",
                }
            ]
        ),
        extract_json=_extract,
    )

    finding = findings[0]
    assert finding["suspect"] == "哥骂赢"
    assert finding["suggestion"] == "N霸凌"
    assert finding["repair_class"] == "source_backed_entity"
    assert finding["candidate_provenance"] == {
        "kind": "transcript_context",
        "surface": "N霸凌",
        "nearest_cue_distance": 1,
    }
    assert finding["source_surface_inference"]["basis"] == (
        "exact_replacement_repeated_in_local_authority"
    )

    output, audit = route_findings(
        source,
        findings,
        protected_term_set=frozenset(),
        entity_surface_set=frozenset({"大N"}),
    )
    assert "小主又被大哥骂赢了" in output
    assert audit["findings"][0]["routed"] == "disclosure"
    assert audit["findings"][0]["entity_surface_conflict"] is True


def test_auditor_cannot_bypass_entity_provenance_by_mislabeling_latin_name():
    source = _srt("那群 P 7赖我的群绝对不止有我一个人")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([
            {
                "cue": 1,
                "kind": "context",
                "proposed_full_cue": "那群P7Live的群绝对不止有我一个人",
                "repair_class": "phonetic",
                "why": "猜测组织名但伪装成近音修复",
            }
        ]),
        extract_json=_extract,
    )

    assert findings[0]["suggestion"] is None
    assert (
        findings[0]["suggestion_rejected_reason"]
        == "LATIN_SCRIPT_REPAIR_REQUIRES_SOURCE_PROVENANCE"
    )


def test_bound_chat_latin_lexeme_reaches_cpa_when_audio_is_unavailable():
    source = _srt("它要是博客")
    clip_context = {
        "structured_chat": [
            {
                "kind": "danmaku",
                "text": "因为是boku",
                "source_event_id": "event-1",
                "source_sha256": "a" * 64,
            }
        ]
    }
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "它要是boku",
                    "repair_class": "phonetic",
                    "why": "整段正在讨论日语第一人称",
                }
            ]
        ),
        extract_json=_extract,
        structured_context_text="danmaku @19049ms: 因为是boku",
        candidate_context=clip_context,
    )

    finding = findings[0]
    assert finding["suggestion"] == "boku"
    assert finding["candidate_provenance"] == {
        "kind": "structured_chat_bound",
        "surface": "boku",
        "source_sha256": "a" * 64,
        "source_event_id": "event-1",
    }
    assert finding["source_surface_inference"] == {
        "surface": "boku",
        "basis": "exact_latin_surface_in_bound_structured_chat",
    }
    assert finding["latin_candidate_support"]["basis"] == (
        "BOUND_STRUCTURED_CHAT"
    )

    def unavailable_witness(request):
        return {
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "request_sha256": request["request_sha256"],
            "status": "UNCERTAIN",
            "reason_code": "ENTITY_AUDIO_PROVIDER_FAILED",
        }

    output, adjudication = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=unavailable_witness,
        clip_context=clip_context,
        judge_llm_call=_judge("PROPOSED"),
    )
    assert "它要是boku" in output
    assert adjudication["policy_branch"] == (
        "CPA_JUDGE_APPLY_PROPOSED_WITHOUT_AUDIO_WITNESS"
    )
    assert adjudication["decision_authority"] == "CPA_JUDGE"
    assert adjudication["mutation_authority"]["status"] == "PASS"


def test_repeated_foreign_lexeme_proposals_reach_cpa_without_chat_or_audio():
    source = _srt("应该是阿达西这种", "啊 DC 就像偶")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "应该是atashi这种",
                    "repair_class": "phonetic",
                    "evidence_cue_ids": [2],
                    "why": "同一日语第一人称反复漂移",
                },
                {
                    "cue": 2,
                    "kind": "context",
                    "proposed_full_cue": "atashi就像偶",
                    "repair_class": "phonetic",
                    "evidence_cue_ids": [1],
                    "why": "同一日语第一人称反复漂移",
                },
            ]
        ),
        extract_json=_extract,
    )

    assert [finding["suggestion"] for finding in findings] == [
        "atashi",
        "atashi",
    ]
    for finding in findings:
        assert finding["candidate_provenance"] is None
        assert finding["latin_candidate_support"] == {
            "schema_version": "latin-lexical-candidate-support.v1",
            "token": "atashi",
            "basis": "CPA_CROSS_CUE_PROPOSAL_CONSENSUS",
            "cue_indexes": [1, 2],
        }
        assert "suggestion_rejected_reason" not in finding


def test_priority_candidates_are_not_crowded_out_by_reviewer_limit():
    source = _srt("错字", "名多的孩子")
    raw = [
        {
            "cue": 1,
            "kind": "context",
            "proposed_full_cue": "对字",
            "repair_class": "phonetic",
            "why": f"duplicate reviewer row {index}",
        }
        for index in range(24)
    ]
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(raw),
        extract_json=_extract,
        extra_raw_findings=[
            {
                "cue": 2,
                "kind": "context",
                "suspect": "名多",
                "proposed_full_cue": "鸣人的孩子",
                "repair_class": "phonetic",
                "why": "fidelity candidate",
            }
        ],
        prioritize_extra_raw_findings=True,
    )

    assert findings[0]["cue_index"] == 2
    assert findings[0]["suggestion"] == "鸣人"


def test_context_request_uses_full_cue_candidates_and_adjacent_lines():
    source = _srt("前一句", "还没有歌杂呢", "后一句", "再后一句")
    request = build_context_adjudication_request(
        source,
        {
            "cue_index": 2,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
    )

    assert request["schema_version"] == "subtitle-span-acoustic-check-request.v1"
    assert [row["canonical"] for row in request["candidate_entities"]] == [
        "还没有歌杂呢",
        "还没有歌债呢",
    ]
    assert [row["candidate_id"] for row in request["candidate_entities"]] == [
        "CURRENT",
        "PROPOSED",
    ]
    assert request["context_before"] == "前一句"
    assert request["context_after"] == "后一句\n再后一句"
    assert request["context_start_ms"] < request["matched_start_ms"]
    assert request["context_end_ms"] > request["matched_end_ms"]
    assert request["context_end_ms"] < 20_000  # cue 4 text is context, its audio is excluded
    assert request["source_media_timeline_offset_ms"] == 0
    assert len(request["request_sha256"]) == 64


def test_context_request_hash_binds_source_media_timeline_offset():
    source = (
        "1\n"
        "00:00:00,250 --> 00:00:02,810\n"
        "就请坐在左边的弹\n"
    )
    finding = {
        "cue_index": 1,
        "suspect": "弹",
        "suggestion": "互相弹",
        "proposed_full_cue": "就请坐在左边的互相弹",
        "repair_class": "phonetic",
        "why": "exact-final release check",
    }

    delivery_local = build_context_adjudication_request(source, finding)
    padded_source = build_context_adjudication_request(
        source,
        finding,
        source_media_timeline_offset_ms=9_770,
    )

    assert delivery_local["matched_start_ms"] == padded_source["matched_start_ms"] == 250
    assert delivery_local["matched_end_ms"] == padded_source["matched_end_ms"] == 2_810
    assert delivery_local["source_media_timeline_offset_ms"] == 0
    assert padded_source["source_media_timeline_offset_ms"] == 9_770
    assert delivery_local["request_sha256"] != padded_source["request_sha256"]
    assert delivery_local["evidence_id"] != padded_source["evidence_id"]


@pytest.mark.parametrize("invalid_offset", [-1, True, 1.5, "9770"])
def test_context_request_rejects_invalid_source_media_timeline_offset(
    invalid_offset,
):
    source = _srt("还没有歌杂呢")
    with pytest.raises(ValueError, match="SOURCE_MEDIA_TIMELINE_OFFSET_INVALID"):
        build_context_adjudication_request(
            source,
            {
                "cue_index": 1,
                "suspect": "歌杂",
                "suggestion": "歌债",
                "proposed_full_cue": "还没有歌债呢",
                "repair_class": "phonetic",
            },
            source_media_timeline_offset_ms=invalid_offset,
        )



def _witness(request, heard, *, audible=True, uncertain=()):
    """Valid dictation-witness verdict for the given witness request."""
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": audible,
        "heard_pinyin": heard,
        "uncertain_positions": list(uncertain),
        "syllable_count": len(heard.split()),
        "confidence": 0.9,
        "reason": "test witness",
    }


def _judge(choice):
    return lambda prompt: json.dumps(
        {
            "choice": choice,
            "reason": "test",
            "current_utterance_supported": choice == "CURRENT",
            "current_utterance_support_reason": (
                "test fixture supports the complete current utterance"
                if choice == "CURRENT"
                else "test fixture does not support the complete current utterance"
            ),
        }
    )


def _history_audio_finding(
    source,
    *,
    cue_index=1,
    gate_status="BLOCK",
    needs_audio=True,
    drift=False,
):
    finding = {
        "cue_index": cue_index,
        "base_text_sha256": hashlib.sha256("三二".encode()).hexdigest(),
        "suspect": "三二",
        "suggestion": "方案二",
        "proposed_full_cue": "方案二",
        "repair_class": "phonetic",
        "why": "历史 CPA 需要完整音频",
    }
    old_request = build_context_adjudication_request(source, finding)
    gate = {
        "schema_version": "history-convergence-acoustic-witness-authority.v1",
        "status": gate_status,
        "reason_code": "CPA_REQUESTS_MORE_AUDIO",
        "judge_prompt_sha256": "a" * 64,
        "judge_completion_sha256": "b" * 64,
        "judge_served_from_cache": False,
        "needs_audio": needs_audio,
        "reason": "历史上下文仍需完整句音频",
    }
    if drift:
        gate["matched_start_ms"] = old_request["matched_start_ms"] + 1
    finding["exact_release_adjudication"] = {
        "schema_version": "subtitle-span-adjudication.v1",
        "status": "UNCERTAIN",
        "repaired": False,
        "reason_code": "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED",
        "policy_branch": "HISTORY_CONVERGENCE_DOWNGRADED_TO_DISCLOSURE_ONLY",
        "timing_immutable": True,
        "request": old_request,
        "history_convergence_acoustic_witness": gate,
        "decision_authority": "CPA_PROPOSAL_ONLY",
        "witness_authority": "ACOUSTIC_WITNESS_REQUIRED",
    }
    return finding, old_request


def _history_audio_duplicate_rows(source, *, cue_index=1):
    typed, _old_request = _history_audio_finding(
        source, cue_index=cue_index
    )
    typed = {
        **typed,
        "cue": cue_index,
        # The normal pending-current row is proposal-less; its old request
        # remains evidence for the next proposal bootstrap.
        "proposed_full_cue": None,
        "suggestion": None,
    }
    microcue = {
        "cue": cue_index,
        "kind": "context",
        "suspect": "三二",
        "proposed_full_cue": None,
        "repair_class": "phonetic",
        "why": "当前 microcue 发现",
    }
    return microcue, typed


def _retry_plan(question="连听前后句，核对本句的实际句界和说话来源"):
    return {
        "action": "expand_context", "question": question,
        "why_previous_failed": "孤立短窗不能区分未完成话语与相邻声音",
        "expected_gain": "确定目标整句的来源和边界",
        "verification": "逐词对齐原音，保留仍无法确认的内容",
    }


@pytest.mark.parametrize("plan_lane", ["fresh", "extra"])
def test_priority_duplicate_plan_reaches_missing_proposal_consumers(monkeypatch, plan_lane):
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    source = _srt("前文", "我听，听到你不许哦", "后文")
    nomination = {
        "cue": 2, "kind": "context", "suspect": "我听，听到你不许哦",
        "proposed_full_cue": None, "repair_class": "phonetic", "why": "既有疑点仍未解决",
    }
    planned = {**nomination, "why": "本轮重新核对完整句界", "continuation_plan": _retry_plan()}
    findings = audit_final_subtitles(
        source, llm_call=_fake_llm([planned] if plan_lane == "fresh" else []),
        extract_json=_extract,
        extra_raw_findings=[nomination] + ([planned] if plan_lane == "extra" else []),
        prioritize_extra_raw_findings=True,
    )
    assert len(findings) == 1
    assert findings[0]["continuation_plan"] == _retry_plan()
    prompts = []

    def unresolved(prompt):
        prompts.append(prompt)
        return json.dumps({
            "status": "UNRESOLVED", "proposed_cue": "", "decision": "UNRESOLVED",
            "replacement_text": "", "needs_audio": False, "reason": "边界尚未确定",
        })

    output, result = adjudicate_context_finding(
        source, findings[0], entity_verifier=None, judge_llm_call=unresolved,
    )
    assert output == source
    assert result["repaired"] is False
    assert len(prompts) == 2
    assert all(_retry_plan()["question"] in prompt for prompt in prompts)


@pytest.mark.parametrize("plan_lane", ["fresh", "extra"])
def test_priority_duplicate_conflicting_plans_cannot_revive_by_input_order(plan_lane):
    source = _srt("可疑词")
    nomination = {"cue": 1, "suspect": "可疑词", "proposed_full_cue": None,
                  "repair_class": "phonetic", "why": "同一疑点", "continuation_plan": _retry_plan()}
    conflict = {**nomination, "continuation_plan": _retry_plan("先核对另一项互不相同的框架")}
    candidates = [conflict, deepcopy(nomination)]
    merged = auditor._merged_raw_findings(
        candidates if plan_lane == "fresh" else [],
        [nomination] + (candidates if plan_lane == "extra" else []),
        auditor.parse_srt_cues(source), prioritize_extra=True, current_srt_text=source,
    )
    assert len(merged) == 1
    assert "continuation_plan" not in merged[0]


@pytest.mark.parametrize("mismatch", [
    "invalid_plan", "repair_class", "candidate", "source_surface", "candidate_provenance",
    "base", "srt", "remap", "occurrence",
])
def test_priority_duplicate_plan_rejects_invalid_or_different_question(mismatch):
    source = _srt("前文", "可疑词可疑词")
    nomination = {
        "cue": 2, "suspect": "可疑词", "proposed_full_cue": None,
        "repair_class": "phonetic", "why": "同一疑点",
        "span_start_codepoint": 0, "span_end_codepoint": 3,
    }
    planned = {**nomination, "continuation_plan": _retry_plan()}
    if mismatch == "invalid_plan":
        planned["continuation_plan"].pop("verification")
    elif mismatch == "repair_class":
        planned["repair_class"] = "segmentation"
    elif mismatch == "candidate":
        planned["proposed_full_cue"] = "另一个候选"
    elif mismatch == "source_surface":
        planned["source_surface"] = "另一来源词面"
    elif mismatch == "candidate_provenance":
        planned["candidate_provenance"] = {"kind": "different_source"}
    elif mismatch == "base":
        planned["base_text_sha256"] = "f" * 64
    elif mismatch == "srt":
        planned["reviewed_srt_sha256"] = "sha256:" + "f" * 64
    elif mismatch == "remap":
        planned["_carryover_replay_remap"] = {"from_cue": 1, "to_cue": 2}
    else:
        planned.update(span_start_codepoint=3, span_end_codepoint=6)
    merged = auditor._merged_raw_findings(
        [planned], [nomination], auditor.parse_srt_cues(source),
        prioritize_extra=True, current_srt_text=source,
    )
    assert "continuation_plan" not in merged[0]


def test_duplicate_microcue_carries_current_history_needs_audio_gate():
    source = _srt("三二")
    microcue, typed = _history_audio_duplicate_rows(source)

    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([]),
        extract_json=_extract,
        extra_raw_findings=[microcue, typed],
        prioritize_extra_raw_findings=True,
    )

    assert len(findings) == 1
    assert findings[0]["why"] == "当前 microcue 发现"
    carried = findings[0]["exact_release_adjudication"]
    assert carried["history_convergence_acoustic_witness"]["needs_audio"] is True
    assert carried["request"]["current_cue"] == "三二"
    assert carried["request"]["whole_clip_current_srt"] == source


def test_duplicate_history_audio_conflict_cannot_revive_later_authority():
    source = _srt("三二")
    microcue, authority_a = _history_audio_duplicate_rows(source)
    authority_b = deepcopy(authority_a)
    gate_b = authority_b["exact_release_adjudication"][
        "history_convergence_acoustic_witness"
    ]
    gate_b["judge_prompt_sha256"] = "c" * 64
    gate_b["judge_completion_sha256"] = "d" * 64

    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([]),
        extract_json=_extract,
        # A/B are both valid but disagree; the final A must not win by order.
        extra_raw_findings=[microcue, authority_a, authority_b, deepcopy(authority_a)],
        prioritize_extra_raw_findings=True,
    )

    assert len(findings) == 1
    assert "exact_release_adjudication" not in findings[0]


@pytest.mark.parametrize(
    "drift", ["base", "request", "remap", "forged", "suspect_only"]
)
def test_duplicate_history_needs_audio_gate_rejects_stale_or_forged_binding(drift):
    source = _srt("前文", "三二")
    microcue, typed = _history_audio_duplicate_rows(source, cue_index=2)
    if drift == "base":
        typed["base_text_sha256"] = "f" * 64
    elif drift == "request":
        typed["exact_release_adjudication"]["request"]["current_cue"] = "外部伪造当前文本"
    elif drift == "remap":
        typed["cue"] = 1
    elif drift == "forged":
        typed["exact_release_adjudication"]["history_convergence_acoustic_witness"][
            "judge_prompt_sha256"
        ] = "伪造"
    else:
        typed.pop("exact_release_adjudication")

    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm([]),
        extract_json=_extract,
        extra_raw_findings=[microcue, typed],
        prioritize_extra_raw_findings=True,
    )

    assert len(findings) == 1
    assert "exact_release_adjudication" not in findings[0]


def test_word_choice_prompt_includes_profile_bound_hard_meme_policy():
    request = {
        "request_sha256": "a" * 64,
        "current_cue": "这就是经典的侄女",
        "proposed_cue": "这就是经典的直女",
        "suspect": "侄女",
        "replacement": "直女",
        "repair_class": "phonetic",
    }
    witness = _witness(request, "zhe jiu shi jing dian de zhi nv")
    prompts = []

    result = judge_word_choice(
        llm_call=lambda prompt: (
            prompts.append(prompt)
            or json.dumps({"choice": "CURRENT", "reason": "规范词面"})
        ),
        check_request=request,
        witness=witness,
    )

    assert result["choice"] == "CURRENT"
    assert len(prompts) == 1
    assert "hard-meme-canon" in prompts[0]
    assert "「直女」一律写作「侄女」" in prompts[0]


def test_word_choice_renders_scoped_pronoun_policy_evidence_as_context_only():
    from src.autoslice import pronoun_consistency

    source = _srt("她说梦", "后文回扣")
    _, audit = pronoun_consistency.discover_candidate_pronoun_findings(
        source,
        policy_text="女性主播的指代政策。",
        candidate_context_text="梦境叙事语境。",
        llm_call=lambda _prompt: json.dumps(
            {
                "decisions": [
                    {
                        "occurrence_id": "cue-1-occurrence-1",
                        "action": "KEEP_CURRENT",
                        "current_token": "她",
                        "replacement_token": "她",
                        "reason": "指向女性主播",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        extract_json=json.loads,
    )
    request = build_context_adjudication_request(
        source,
        {
            "cue_index": 1,
            "suspect": "她",
            "suggestion": "他",
            "proposed_full_cue": "他说梦",
            "repair_class": "phonetic",
            "_candidate_pronoun_policy_audit": audit,
        },
    )
    witness = _witness(request, "ta shuo meng")
    prompts = []
    verdict = judge_word_choice(
        llm_call=lambda prompt: prompts.append(prompt)
        or json.dumps({"choice": "CURRENT", "reason": "保留闭集当前候选"}),
        check_request=request,
        witness=witness,
    )

    assert verdict["choice"] == "CURRENT"
    assert verdict["candidate_pronoun_policy_evidence"]["status"] == "EVIDENCE_ONLY"
    assert len(prompts) == 1
    assert "候选级代词语义审计（仅上下文证据）" in prompts[0]
    assert "没有听音" in prompts[0]
    assert "mutation_authorized" in prompts[0]


def test_forbidden_hard_meme_cpa_proposal_keeps_original_srt():
    source = _srt("这就是经典的侄女")
    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "侄女",
            "suggestion": "直女",
            "proposed_full_cue": "这就是经典的直女",
            "repair_class": "phonetic",
            "why": "测试硬梗词面约束",
        },
        entity_verifier=lambda request: _witness(
            request, "zhe jiu shi jing dian de zhi nv"
        ),
        judge_llm_call=_judge("PROPOSED"),
    )

    assert output == source
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "HARD_MEME_CANON_FORBIDDEN_PROPOSED"
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"
    rejection = audit["hard_meme_surface_rejection"]
    assert rejection["status"] == "REJECTED"
    assert rejection["unresolved"] is True
    assert rejection["mutation_authorized"] is False
    assert rejection["replacements"][0]["surface"] == "直女"


def _nested_exact_context_keep_case():
    source = _srt("那我不应该说咱", "嗯，咱有点像迪酱", "我要吃午饭")

    def witness(request):
        return _witness(request, "en zan you dian xiang zi cheng")

    def cpa(prompt):
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "proposed_cue": "嗯，咱有点像自称",
                    "reason": "上下文候选",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "盲听支持候选",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "测试候选被选中",
            },
            ensure_ascii=False,
        )

    _, nested = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "迪酱",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "disclosure_only",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
        require_complete_utterance_support=True,
    )
    current = "嗯，咱有点像迪酱"
    row = {
        "cue_index": 2,
        "suspect": "迪酱",
        "suggestion": "自称",
        "proposed_full_cue": "嗯，咱有点像自称",
        "repair_class": "disclosure_only",
        "base_text_sha256": hashlib.sha256(current.encode()).hexdigest(),
        "exact_release_adjudication": nested,
        "_cpa_missing_proposal_convergence": {
            "schema_version": "subtitle-missing-proposal-cpa-convergence.v1",
            "status": "RESOLVED",
            "decision": "KEEP_EXISTING",
            "cue_index": 2,
            "current_cue_sha256": hashlib.sha256(current.encode()).hexdigest(),
            "final_srt_sha256": (
                "sha256:" + hashlib.sha256(source.encode()).hexdigest()
            ),
            "context_sha256": "sha256:" + "0" * 64,
        },
    }
    return source, row


def test_exact_release_carries_nested_cpa_repair_through_context_keep():
    source, row = _nested_exact_context_keep_case()

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [row],
        entity_verifier=lambda request: pytest.fail(
            f"outer context KEEP must not call a new witness: {request}"
        ),
        judge_llm_call=lambda prompt: pytest.fail(
            f"outer context KEEP must not call CPA: {prompt}"
        ),
    )

    assert resolved == []
    assert len(unresolved) == 1
    promoted = unresolved[0]["exact_release_adjudication"]
    assert promoted["repaired"] is True
    assert promoted["request"]["proposed_cue"] == "嗯，咱有点像自称"
    assert promoted["nested_repair_promotion"]["status"] == (
        "PENDING_NORMAL_SELF_HEAL"
    )
    outer = promoted["context_only_outer_adjudication"]
    assert outer["repaired"] is False
    assert outer["cpa_missing_proposal_convergence"]["decision"] == (
        "KEEP_EXISTING"
    )
    assert outer["mutation_authority"]["status"] == "NOT_APPLIED"
    assert promoted["cpa_resource_pressure"] == outer["cpa_resource_pressure"]

    from src.autoslice.producer_package_finalization import (
        _apply_exact_final_cpa_repairs,
    )

    healed, repairs = _apply_exact_final_cpa_repairs(
        source,
        {"findings": unresolved},
    )
    assert "嗯，咱有点像自称" in healed
    assert "嗯，咱有点像迪酱" not in healed
    assert len(repairs) == 1


@pytest.mark.parametrize("drift", ["base", "window", "request"])
def test_nested_exact_cpa_binding_drift_stays_unresolved(drift):
    source, row = _nested_exact_context_keep_case()
    nested = row["exact_release_adjudication"]
    assert isinstance(nested, dict)
    nested = deepcopy(nested)
    request = nested["request"]
    if drift == "base":
        request["base_text_sha256"] = "f" * 64
    elif drift == "window":
        request["matched_start_ms"] += 1
    else:
        request["current_cue"] = "外部伪造当前文本"
    row["exact_release_adjudication"] = nested

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [row],
        entity_verifier=lambda request: pytest.fail(
            f"invalid nested receipt must not call a new witness: {request}"
        ),
        judge_llm_call=lambda prompt: pytest.fail(
            f"invalid nested receipt must not call CPA: {prompt}"
        ),
    )

    assert resolved == []
    assert len(unresolved) == 1
    assert unresolved[0]["exact_release_nested_repair_blocked_reason"].startswith(
        "NESTED_EXACT_REPAIR_"
    )
    assert (
        unresolved[0]["exact_release_adjudication"]["repaired"] is False
    )


def test_exact_release_adjudication_threads_source_media_timeline_offset():
    source = (
        "1\n"
        "00:00:00,250 --> 00:00:02,810\n"
        "就请坐在左边的弹\n"
    )
    seen_requests = []

    def reject_proposal(request):
        seen_requests.append(request)
        return _witness(request, "jiu qing zuo zai zuo bian de tan")

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 1,
                "suspect": "弹",
                "suggestion": "互相弹",
                "proposed_full_cue": "就请坐在左边的互相弹",
                "repair_class": "phonetic",
            }
        ],
        entity_verifier=reject_proposal,
        source_media_timeline_offset_ms=9_770,
        judge_llm_call=_judge("CURRENT"),
    )

    assert not unresolved
    assert len(resolved) == 1
    assert seen_requests[0]["matched_start_ms"] == 250
    assert seen_requests[0]["matched_end_ms"] == 2_810
    assert seen_requests[0]["source_media_timeline_offset_ms"] == 9_770
    assert (
        resolved[0]["exact_release_adjudication"]["request"]
        [REQUIRE_COMPLETE_UTTERANCE_SUPPORT]
        is True
    )


@pytest.mark.parametrize(
    "support_fields",
    [
        {
            "current_utterance_supported": False,
            "current_utterance_support_reason": (
                "CURRENT is only relatively closer than the proposal."
            ),
        },
        {},
    ],
)
def test_exact_release_relative_current_win_without_complete_support_stays_unresolved(
    support_fields,
):
    """Relative pinyin superiority cannot disclose an incomplete CURRENT cue."""

    source = _srt("就请坐在左边的弹")

    def witness(request):
        return _witness(request, "jiu qing zuo zai zuo bian de tan")

    def judge(_prompt):
        payload = {"choice": "CURRENT", "reason": "relative pinyin win"}
        payload.update(support_fields)
        return json.dumps(payload)

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 1,
                "suspect": "弹",
                "suggestion": "互相弹",
                "proposed_full_cue": "就请坐在左边的互相弹",
                "repair_class": "phonetic",
            }
        ],
        entity_verifier=witness,
        judge_llm_call=judge,
    )

    assert resolved == []
    assert len(unresolved) == 1
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["request"][REQUIRE_COMPLETE_UTTERANCE_SUPPORT] is True
    nested_judge = adjudication.get("witness_judge", {}).get("judge", {})
    assert nested_judge.get("choice") != "NEITHER"
    if support_fields:
        assert nested_judge["choice"] == "CURRENT"
        assert nested_judge["current_utterance_supported"] is False
    else:
        assert nested_judge.get("reason_code") == "CURRENT_UTTERANCE_SUPPORT_REQUIRED"


def test_context_request_builds_real_title_fix_without_duplicating_suffix():
    source = _srt("地狱在理解，觉得成人吗")
    request = build_context_adjudication_request(
        source,
        {
            "cue_index": 1,
            "suspect": "在理解，",
            "suggestion": "再爱我，你",
            "proposed_full_cue": "地狱再爱我，你觉得成人吗",
            "repair_class": "source_backed_entity",
            "why": "前后文反复出现作品名",
        },
    )

    assert request["proposed_cue"] == "地狱再爱我，你觉得成人吗"


def test_context_adjudication_applies_only_exact_candidate_and_keeps_timing():
    source = _srt("前一句", "还没有歌杂呢", "后一句")

    def choose_proposed(request):
        return _witness(request, "hai mei you ge zhai ne")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
        entity_verifier=choose_proposed,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "还没有歌债呢" in output
    assert "00:00:10,000 --> 00:00:14,000" in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert audit["request"]["current_cue"] == "还没有歌杂呢"
    assert audit["verdict"]["heard_pinyin"] == "hai mei you ge zhai ne"


def _failed_independent_routes(request):
    """Actual typed route failures precede the phoneme last-resort fixture."""
    return [{
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "witness_protocol": "candidate_blind_transcript",
        "request_sha256": request["request_sha256"], "status": "UNCERTAIN",
        "provider": name, "model": name + "-test", "reason_code": "ASR_TARGET_UNCERTAIN",
        "candidate_exposure": "none", "authority": "EVIDENCE_ONLY", "mutation_authorized": False,
        "source_media_sha256": "a" * 64,
        "audio_start_ms": request["matched_start_ms"] + request["source_media_timeline_offset_ms"],
        "audio_end_ms": request["matched_end_ms"] + request["source_media_timeline_offset_ms"],
    } for name in ("mai", "moss")]


def test_neither_rebuilds_one_third_candidate_then_cpa_judges_it():
    """A malformed two-choice set must return to the proposal layer, not
    become a permanent exact-final blocker or let the proposal mutate bytes.
    """

    source = _srt("前一句", "就是那种生吻", "是深吻还是湿吻")

    def witness(request):
        return _witness(request, "jiu shi na zhong wen")

    calls: list[str] = []

    witness.observe_independent_witnesses = _failed_independent_routes

    def cpa(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        calls.append(prompt)
        if "# 字幕坏闭集重建" in prompt:
            assert "普通日语词句必须写成假名/惯用日文" in prompt
            assert "不得写罗马音或中文谐音" in prompt
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "candidate_failures": "独立听写均报告目标不确定",
                    "evidence_basis": "实际目标拼音和绑定前后文",
                    "residual_uncertainty": "音素有支持，仍需最终CPA裁决",
                    "proposed_cue": "就是那种吻",
                    "reason": "拼音只支持没有深生修饰的完整口播",
                },
                ensure_ascii=False,
            )
        if len([row for row in calls if "# 字幕选字裁决" in row]) == 1:
            return json.dumps(
                {
                    "choice": "NEITHER",
                    "reason": "坏闭集",
                    "current_utterance_supported": False,
                    "current_utterance_support_reason": "第三候选尚未裁决",
                }
            )
        return json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "第三候选匹配",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "第三候选被选中",
            }
        )

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "生",
            "suggestion": "深",
            "proposed_full_cue": "就是那种深吻",
            "repair_class": "phonetic",
            "why": "后文出现深吻",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
    )

    assert "就是那种吻" in output
    assert "就是那种生吻" not in output
    assert audit["repaired"] is True
    assert audit["proposal_rebuild"]["status"] == "PROPOSED"
    assert audit["proposal_rebuild"]["mutation_authorized"] is False
    assert audit["rebuilt_finding"]["proposed_full_cue"] == "就是那种吻"
    assert audit["request"]["proposed_cue"] == "就是那种吻"
    assert len(calls) == 3


def test_exact_release_adopts_rebuilt_candidate_for_same_run_self_heal():
    source = _srt("前一句", "就是那种生吻", "是深吻还是湿吻")

    def witness(request):
        return _witness(request, "jiu shi na zhong wen")

    judge_count = 0
    calls: list[str] = []

    witness.observe_independent_witnesses = _failed_independent_routes

    def cpa(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        nonlocal judge_count
        calls.append(prompt)
        if "# 字幕坏闭集重建" in prompt:
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "candidate_failures": "独立听写均报告目标不确定",
                    "evidence_basis": "实际目标拼音和绑定前后文",
                    "residual_uncertainty": "音素有支持，仍需最终CPA裁决",
                    "proposed_cue": "就是那种吻",
                    "reason": "第三候选",
                },
                ensure_ascii=False,
            )
        judge_count += 1
        return json.dumps(
            {
                "choice": "NEITHER" if judge_count == 1 else "PROPOSED",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "测试候选尚未支持整句",
            }
        )

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 2,
                "suspect": "生",
                "suggestion": "深",
                "proposed_full_cue": "就是那种深吻",
                "repair_class": "phonetic",
            }
        ],
        entity_verifier=witness,
        judge_llm_call=cpa,
    )

    assert resolved == []
    assert len(unresolved) == 1
    assert unresolved[0]["proposed_full_cue"] == "就是那种吻"
    assert unresolved[0]["suspect"] == "生"
    assert unresolved[0]["suggestion"] == ""
    adjudication = unresolved[0]["exact_release_adjudication"]
    assert adjudication["repaired"] is True
    assert adjudication["request"]["proposed_cue"] == "就是那种吻"
    assert adjudication["request"][REQUIRE_COMPLETE_UTTERANCE_SUPPORT] is True
    cpa_judge_prompts = [
        prompt for prompt in calls if "# 字幕选字裁决" in prompt
    ]
    assert len(cpa_judge_prompts) == 2
    assert all(
        "exact-final CURRENT 整句闭合要求" in prompt
        for prompt in cpa_judge_prompts
    )

    from src.autoslice.producer_package_finalization import (
        _apply_exact_final_cpa_repairs,
    )

    healed, repairs = _apply_exact_final_cpa_repairs(
        source,
        {"findings": unresolved},
    )
    assert "就是那种吻" in healed
    assert "就是那种生吻" not in healed
    assert len(repairs) == 1
    assert repairs[0]["decision_authority"] == "CPA_JUDGE"


def test_missing_disclosure_candidate_is_proposed_then_acoustically_judged():
    source = _srt("那我不应该说咱", "嗯，咱有点像迪酱", "我要吃午饭")
    witness_requests = []
    calls: list[str] = []

    def witness(request):
        witness_requests.append(request)
        return _witness(request, "en zan you dian xiang zi cheng")

    def cpa(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "proposed_cue": "嗯，咱有点像自称",
                    "reason": "上下文在讨论第一人称自称",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "盲听与语境一致",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "测试候选被选中",
            }
        )

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "迪酱",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "disclosure_only",
            "why": "无明确词义或专名依据",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
    )

    assert "嗯，咱有点像自称" in output
    assert audit["repaired"] is True
    assert audit["decision_authority"] == "CPA_JUDGE"
    assert audit["proposal_bootstrap"]["status"] == "PROPOSED"
    assert audit["proposal_bootstrap"]["mutation_authorized"] is False
    assert audit["rebuilt_finding"]["repair_class"] == "phonetic"
    assert audit["rebuilt_finding"]["candidate_provenance"] == {
        "kind": "cpa_context_proposal",
        "mutation_authorized": False,
        "prompt_sha256": audit["proposal_bootstrap"]["prompt_sha256"],
    }
    assert len(calls) == 2
    assert len(witness_requests) == 1
    assert "proposed_cue" not in witness_requests[0]


def test_missing_disclosure_candidate_stays_blocked_when_cpa_cannot_propose():
    source = _srt("那我不应该说咱", "嗯，咱有点像迪酱", "我要吃午饭")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "迪酱",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "disclosure_only",
        },
        entity_verifier=lambda request: pytest.fail(
            f"AGY must not run without a candidate: {request}"
        ),
        judge_llm_call=lambda _prompt: json.dumps(
            {"status": "UNRESOLVED", "proposed_cue": "", "reason": "证据不足"},
            ensure_ascii=False,
        ),
    )

    assert output == source
    assert audit["status"] == "MISSING_PROPOSAL_UNRESOLVED"
    assert audit["repaired"] is False
    assert audit["proposal_bootstrap"]["status"] == "UNRESOLVED"
    assert (
        audit["cpa_missing_proposal_convergence"]["status"] == "INVALID"
    )
    assert audit["decision_authority"] == "CPA_JUDGE_FAILED"


def test_missing_candidate_second_cpa_can_keep_existing_without_agy():
    source = _srt("我昨天去上班", "上班好累", "下班了")
    calls = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "proposed_cue": "",
                    "reason": "首轮不猜",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "decision": "KEEP_EXISTING",
                "replacement_text": "",
                "reason": "前后都在谈上下班，原句合理",
            },
            ensure_ascii=False,
        )

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 2,
                "suspect": "上班好累",
                "suggestion": None,
                "proposed_full_cue": None,
                "repair_class": "disclosure_only",
                "why": "审片员认为可能是口语残句",
            }
        ],
        entity_verifier=lambda request: pytest.fail(
            f"AGY must not decide a CPA context-only convergence: {request}"
        ),
        clip_context={
            "structured_chat": [
                {
                    "offset_ms": 8_000,
                    "kind": "danmaku",
                    "sender": "A",
                    "text": "上班辛苦了",
                },
                {
                    "offset_ms": 12_000,
                    "kind": "danmaku",
                    "sender": "B",
                    "text": "下班休息",
                },
            ]
        },
        judge_llm_call=cpa,
    )

    assert unresolved == []
    assert resolved[0]["resolution"] == "CPA_CONTEXT_ONLY_KEEP_EXISTING"
    adjudication = resolved[0]["exact_release_adjudication"]
    convergence = adjudication["cpa_missing_proposal_convergence"]
    assert convergence["decision"] == "KEEP_EXISTING"
    assert convergence["final_srt_sha256"] == (
        "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
    )
    assert convergence["context_sha256"].startswith("sha256:")
    assert adjudication["decision_authority"] == "CPA_JUDGE"
    assert adjudication["mutation_authority"]["status"] == "NOT_APPLIED"
    assert len(calls) == 2
    assert "前 3 / 后 3" in calls[1]
    assert "上班辛苦了" in calls[1]
    assert "下班休息" in calls[1]


@pytest.mark.parametrize("needs_audio", [False, True])
@pytest.mark.parametrize("convergence_decision", ["KEEP_EXISTING", "UNRESOLVED"])
def test_missing_phonetic_keep_requires_candidate_blind_audio_observation(
    needs_audio, convergence_decision,
):
    source = _srt("我昨天去上班", "上班好累", "下班了")
    calls = []
    witness_requests = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "proposed_cue": "",
                    "reason": "没有安全候选",
                },
                ensure_ascii=False,
            )
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps(
                {
                    "decision": convergence_decision,
                    "needs_audio": convergence_decision == "UNRESOLVED",
                    "replacement_text": "",
                    "reason": "上下班语境并不否定现句",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "choice": "CURRENT",
                "needs_audio": needs_audio,
                "reason": "候选盲听与当前字幕相容",
                "current_utterance_supported": True,
                "current_utterance_support_reason": "目标时窗完整支持当前 cue",
            },
            ensure_ascii=False,
        )

    def witness(request):
        witness_requests.append(request)
        return _witness(request, "shang ban hao lei")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "上班好累",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "phonetic",
            "why": "缺失候选的发音疑点",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
    )

    assert output == source
    assert len(calls) == 3
    assert len(witness_requests) == 1
    assert "proposed_cue" not in witness_requests[0]
    assert audit["request"]["candidate_blind_observation_scaffolding"] is True
    assert audit["request"]["current_cue"] == audit["request"]["proposed_cue"]
    assert audit["status"] == ("UNCERTAIN" if needs_audio else "OBSERVED")
    assert audit["repaired"] is False
    assert audit["candidate_blind_observation"]["status"] == ("BLOCK" if needs_audio else "PASS")
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"
    if not needs_audio:
        assert audit["cpa_missing_proposal_convergence"]["decision"] == convergence_decision


@pytest.mark.parametrize("supported,needs_audio,resolved", [(True, False, True), (False, False, False), (True, True, False)])
def test_exact_current_scaffold_support_closes_stale_convergence(supported, needs_audio, resolved):
    source = _srt("我昨天去上班", "上班好累", "下班了")
    calls = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps({"status": "UNRESOLVED", "proposed_cue": ""})
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps({"decision": "UNRESOLVED", "needs_audio": True,
                "replacement_text": "", "reason": "旧记录仍待补听"}, ensure_ascii=False)
        return json.dumps({"choice": "CURRENT", "needs_audio": needs_audio,
            "current_utterance_supported": supported,
            "current_utterance_support_reason": "完整当前句原音支持"}, ensure_ascii=False)

    unresolved, accepted = adjudicate_exact_release_findings(source, [{
        "cue_index": 2, "suspect": "上班好累", "suggestion": None,
        "proposed_full_cue": None, "repair_class": "phonetic"}],
        entity_verifier=lambda request: _witness(request, "shang ban hao lei"),
        judge_llm_call=cpa)
    assert bool(accepted) is resolved
    assert bool(unresolved) is not resolved
    audit = (accepted or unresolved)[0]["exact_release_adjudication"]
    assert audit["cpa_missing_proposal_convergence"]["decision"] == "UNRESOLVED"
    assert audit["repaired"] is False
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"
    if resolved:
        assert accepted[0]["resolution"] == "CPA_CURRENT_SCAFFOLD_KEEP_EXISTING"
        assert len(calls) == 3


def test_missing_phonetic_keep_blocks_when_candidate_blind_witness_is_unavailable():
    source = _srt("我昨天去上班", "上班好累", "下班了")
    calls = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {"status": "UNRESOLVED", "proposed_cue": ""},
                ensure_ascii=False,
            )
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps(
                {"decision": "KEEP_EXISTING", "replacement_text": ""},
                ensure_ascii=False,
            )
        pytest.fail(
            "an unavailable candidate-blind witness must block before CPA choice"
        )

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "上班好累",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "phonetic",
        },
        entity_verifier=lambda _request: None,
        judge_llm_call=cpa,
    )

    assert output == source
    assert len(calls) == 2
    assert audit["status"] == "UNCERTAIN"
    assert audit["candidate_blind_observation"]["status"] == "BLOCK"
    assert (
        audit["candidate_blind_observation"]["reason_code"]
        == "CANDIDATE_BLIND_WITNESS_UNAVAILABLE"
    )
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"


def test_missing_phonetic_keep_blocks_when_current_support_is_not_declared():
    source = _srt("我昨天去上班", "上班好累", "下班了")
    calls = []
    independent_requests = []

    def witness(request):
        return _witness(request, "shang ban hao lei")

    def independent(request):
        independent_requests.append(request)
        return []

    witness.observe_independent_witnesses = independent

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {"status": "UNRESOLVED", "proposed_cue": ""},
                ensure_ascii=False,
            )
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps(
                {"decision": "KEEP_EXISTING", "replacement_text": ""},
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "choice": "CURRENT",
                "reason": "相对当前更接近",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "目标句完整性未确认",
            },
            ensure_ascii=False,
        )

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "上班好累",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "phonetic",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
    )

    assert output == source
    assert len(calls) == 3
    assert len(independent_requests) == 1
    assert audit["status"] == "UNCERTAIN"
    assert audit["candidate_blind_observation"]["status"] == "BLOCK"
    assert audit["witness_judge"]["judge"]["choice"] == "CURRENT"
    assert audit["witness_judge"]["judge"]["current_utterance_supported"] is False
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"


@pytest.mark.parametrize("decision,terminal_current", [
    ("KEEP_EXISTING", False),
    ("UNRESOLVED", False),
    ("REPLACE_WITH_EXACT_TEXT", False),
    ("UNRESOLVED", True),
])
def test_missing_proposal_backfeeds_accepted_native_audio_to_same_cpa_context(
    tmp_path, monkeypatch, decision, terminal_current,
):
    from src.autoslice.local_asr_target_evidence import MODELS, SCHEMA, digest
    from src.autoslice.native_context_witness import (
        _native_transcript_witness, _prepare_request,
    )

    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path))
    source = _srt("我昨天去上班", "上班好累", "下班了")
    calls = []
    audio_requests = []
    original_witness_request = None

    def witness(request):
        nonlocal original_witness_request
        original_witness_request = original_witness_request or request["request_sha256"]
        if request["request_sha256"] != original_witness_request:
            # A new semantic replacement still needs its own normal witness.
            # The earlier exact observations do not authorize this new text.
            return None
        return _witness(request, "shang ban hao lei")

    def independent(request):
        audio_requests.append(request)
        prepared = _prepare_request(request)
        rows = []
        for provider, transcript in [("mai", "上班好累"), ("moss", "上班好累。")]:
            receipt = {
                "schema_version": SCHEMA, "status": "OBSERVED",
                "provider": provider, "model": MODELS[provider],
                "candidate_exposure": "none", "authority": "EVIDENCE_ONLY",
                "mutation_authorized": False, "source_media_sha256": "a" * 64,
                "input_audio_sha256": "b" * 64, "response_sha256": "c" * 64,
                "target_start_ms": prepared.physical_start_ms,
                "target_end_ms": prepared.physical_end_ms,
                "crop_is_exact_target": True,
                "observation_scope": "complete_exact_cue_no_padding",
                "transcript": transcript, "native_segments": [],
            }
            receipt["receipt_sha256"] = digest(receipt)
            rows.append(_native_transcript_witness(
                request=request, prepared=prepared, native_observation=receipt,
                source_sha256="a" * 64, provider=provider,
            ))
        return rows

    witness.observe_independent_witnesses = independent

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps({"status": "UNRESOLVED", "proposed_cue": ""})
        if "# 字幕缺失候选最终收敛" in prompt:
            if "本轮正常补听返回的目标 cue 原音观察" in prompt:
                assert '"provider": "mai"' in prompt
                assert '"provider": "moss"' in prompt
                assert '"exact_transcript": "上班好累。"' in prompt
                assert '"authority": "EVIDENCE_ONLY"' in prompt
                return json.dumps({
                    "decision": decision,
                    "replacement_text": "上班很累" if decision == "REPLACE_WITH_EXACT_TEXT" else "",
                    "needs_audio": decision == "UNRESOLVED",
                    "reason": "结合两路原音观察重新审查完整现稿，仍保留观察分歧",
                }, ensure_ascii=False)
            return json.dumps({
                "decision": "UNRESOLVED", "replacement_text": "",
                "needs_audio": True, "reason": "尚未取得真实补听",
            }, ensure_ascii=False)
        independent_seen = bool(audio_requests)
        return json.dumps({
            "choice": "CURRENT", "needs_audio": not independent_seen,
            "current_utterance_supported": independent_seen and terminal_current,
            "current_utterance_support_reason": "实际原音支持当前完整 cue" if independent_seen else "未确认",
            "reason": "实际听写与当前文本相容",
        }, ensure_ascii=False)

    finding = {
        "cue_index": 2, "suspect": "上班好累", "proposed_full_cue": None,
        "suggestion": None, "repair_class": "disclosure_only",
    }
    output, audit = adjudicate_context_finding(
        source, finding, entity_verifier=witness, judge_llm_call=cpa,
        source_media_timeline_offset_ms=10630,
        require_complete_utterance_support=True,
    )
    assert output == source
    assert audit["repaired"] is False
    if terminal_current:
        # A bound complete CURRENT verdict after real native evidence is the
        # terminal decision. The earlier missing-audio convergence stays in
        # history and must not launch a second judge that overturns it.
        assert audit["status"] == "OBSERVED"
        assert audit["candidate_blind_observation"]["status"] == "PASS"
        assert audit["cpa_missing_proposal_convergence"]["decision"] == "UNRESOLVED"
        assert "native_audio_convergence_backfeed" not in audit
        assert not any("本轮正常补听返回的目标 cue 原音观察" in p for p in calls)
        unresolved, resolved = adjudicate_exact_release_findings(
            source, [finding], entity_verifier=witness, judge_llm_call=cpa,
            source_media_timeline_offset_ms=10630,
        )
        assert not unresolved
        assert len(resolved) == 1
        assert resolved[0]["exact_release_adjudication"]["repaired"] is False
        return
    backfeed = audit["native_audio_convergence_backfeed"]
    assert backfeed["prior_convergence"]["needs_audio"] is True
    assert backfeed["convergence"]["exact_acoustic_context_count"] == 2
    assert backfeed["convergence"]["decision"] == decision
    assert audit["status"] == ("OBSERVED" if decision == "KEEP_EXISTING" else "UNCERTAIN")
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"
    if decision == "REPLACE_WITH_EXACT_TEXT":
        assert audit["reason_code"] == "CONTEXT_REWRITE_ACOUSTIC_WITNESS_REQUIRED"
    assert len(audio_requests) == 1
    assert len([p for p in calls if "本轮正常补听返回的目标 cue 原音观察" in p]) == 1

    # The normal semantic cache keys the current request and immutable native
    # receipts. Replaying the same unresolved evidence makes no new CPA call.
    calls.clear()
    audio_requests.clear()
    output, replay = adjudicate_context_finding(
        source, finding, entity_verifier=witness, judge_llm_call=cpa,
        source_media_timeline_offset_ms=10630,
        require_complete_utterance_support=True,
    )
    assert output == source
    assert replay["native_audio_convergence_backfeed"]["convergence"]["served_from_cache"] is True
    assert not any("本轮正常补听返回的目标 cue 原音观察" in p for p in calls)

    audio_requests.clear()
    unresolved, resolved = adjudicate_exact_release_findings(
        source, [finding], entity_verifier=witness, judge_llm_call=cpa,
        source_media_timeline_offset_ms=10630,
    )
    if decision == "KEEP_EXISTING":
        assert not unresolved, json.dumps(unresolved, ensure_ascii=False)
        assert resolved[0]["resolution"] == "CPA_CONTEXT_ONLY_KEEP_EXISTING"
    else:
        assert not resolved
        assert len(unresolved) == 1


def test_context_keep_with_explicit_audio_request_is_not_terminal():
    from src.autoslice.missing_proposal_bootstrap import converge_missing_proposal

    converged, audit = converge_missing_proposal(
        srt_text=_srt("上班好累"),
        finding={"cue_index": 1, "suspect": "上班好累"},
        structured_chat="", proposal_audit={},
        derive_single_span_edit=auditor._derive_single_span_edit,
        llm_call=lambda _prompt: json.dumps({
            "decision": "KEEP_EXISTING", "replacement_text": "",
            "needs_audio": True, "reason": "仍须补听确认",
        }),
    )
    assert converged is None
    assert audit["status"] == "UNRESOLVED"
    assert audit["needs_audio"] is True
    assert audit["mutation_authorized"] is False


@pytest.mark.parametrize("drift", ["window", "receipt", "request", "authority", "source", "current_srt"])
def test_native_convergence_backfeed_rejects_drifted_current_receipt(drift):
    from src.autoslice.local_asr_target_evidence import MODELS, SCHEMA, digest
    from src.autoslice.native_context_witness import (
        _native_transcript_witness, _prepare_request,
    )
    from src.autoslice.independent_context_adjudication import independent_asr_candidates

    source = _srt("上班好累")
    request = build_context_adjudication_request(source, {
        "cue_index": 1, "suspect": "好", "suggestion": "很",
        "proposed_full_cue": "上班很累",
    }, source_media_timeline_offset_ms=10630)
    target = auditor.build_witness_request(request)
    prepared = _prepare_request(target)
    receipt = {
        "schema_version": SCHEMA, "status": "OBSERVED",
        "provider": "mai", "model": MODELS["mai"],
        "candidate_exposure": "none", "authority": "EVIDENCE_ONLY",
        "mutation_authorized": False, "source_media_sha256": "a" * 64,
        "input_audio_sha256": "b" * 64, "response_sha256": "c" * 64,
        "target_start_ms": prepared.physical_start_ms,
        "target_end_ms": prepared.physical_end_ms,
        "crop_is_exact_target": True,
        "observation_scope": "complete_exact_cue_no_padding",
        "transcript": "上班好累", "native_segments": [],
    }
    receipt["receipt_sha256"] = digest(receipt)
    row = _native_transcript_witness(
        request=target, prepared=prepared, native_observation=receipt,
        source_sha256="a" * 64, provider="mai",
    )
    def witness(_request):
        return None
    witness.observe_independent_witnesses = lambda _request: [row]
    _, _, audit = independent_asr_candidates(request, witness)
    assert len(auditor._native_exact_convergence_context(request, audit)) == 1
    bad = deepcopy(row)
    if drift == "window":
        bad["native_observation"]["target_start_ms"] += 1
    elif drift == "receipt":
        bad["native_observation"]["transcript"] = "伪造听写"
    elif drift == "request":
        bad["request_sha256"] = "d" * 64
    elif drift == "source":
        bad["native_observation"]["source_media_sha256"] = "d" * 64
    elif drift == "current_srt":
        request = build_context_adjudication_request(_srt("上班好累", "新增后文"), {
            "cue_index": 1, "suspect": "好", "suggestion": "很",
            "proposed_full_cue": "上班很累",
        }, source_media_timeline_offset_ms=10630)
    else:
        bad["native_observation"]["mutation_authorized"] = True
    audit["accepted_observations"] = [bad]
    assert auditor._native_exact_convergence_context(request, audit) == ()


@pytest.mark.parametrize("closure_available", [True, False])
@pytest.mark.parametrize("support_declared", [True, False])
def test_exact_text_current_requires_its_own_context_terminal(
    closure_available, support_declared
):
    source = _srt("有些人受伤了", "我们需要去抢救他们", "救护车还没来")
    calls = []

    def cpa(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({
                "choice": "CURRENT", "needs_audio": False,
                "ranking": [{"choice": "CURRENT", "p": .95},
                            {"choice": "PROPOSED", "p": .04},
                            {"choice": "NEITHER", "p": .01}],
                "reason": "他们可回指受伤的人，改为TA们没有依据",
                "current_utterance_supported": support_declared,
                "current_utterance_support_reason": (
                    "已有目标时窗的观察支持现稿" if support_declared else "本轮没听原音"
                ),
            }, ensure_ascii=False)
        assert "完整当前字幕" in prompt
        assert source in prompt
        assert "明确选择 CURRENT 且 needs_audio=false" in prompt
        if not closure_available:
            raise RuntimeError("provider unavailable")
        return json.dumps({"decision": "KEEP_EXISTING", "replacement_text": "",
                           "reason": "完整原句在救援语境中成立，没有需修正的文字问题"},
                          ensure_ascii=False)

    unresolved, resolved = adjudicate_exact_release_findings(
        source, [{"cue_index": 2, "suspect": "他", "suggestion": "TA",
                  "proposed_full_cue": "我们需要去抢救TA们", "repair_class": "phonetic"}],
        entity_verifier=lambda request: pytest.fail("CPA explicitly did not request audio"),
        judge_llm_call=cpa,
    )
    assert len(calls) == 2
    if closure_available:
        assert unresolved == []
        receipt = resolved[0]["exact_release_adjudication"]
        assert resolved[0]["resolution"] == "CPA_CONTEXT_ONLY_KEEP_EXISTING"
        assert receipt["verdict"]["status"] == "NOT_REQUIRED"
        assert receipt["mutation_authority"]["status"] == "NOT_APPLIED"
        assert receipt["cpa_missing_proposal_convergence"]["final_srt_sha256"] == (
            "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        )
    else:
        assert resolved == []
        assert unresolved[0]["exact_release_adjudication"]["status"] == (
            "TEXT_FIRST_CURRENT_CLOSURE_UNRESOLVED"
        )


def test_missing_candidate_second_cpa_exact_text_self_heals_with_acoustic_witness():
    source = _srt("今天可以吗", "今天能多啵啵嘛", "好不好")

    def cpa(prompt):
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "proposed_cue": "明天能多啵啵嘛",
                    "reason": "错误地只改了目标外文字",
                },
                ensure_ascii=False,
            )
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps(
                {
                    "decision": "REPLACE_WITH_EXACT_TEXT",
                    "replacement_text": "今天能多抱抱嘛",
                    "reason": "相邻语境是在请求抱抱",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "盲听与文字候选一致",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "测试候选被选中",
            },
            ensure_ascii=False,
        )

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 2,
                "suspect": "啵啵",
                "suggestion": None,
                "proposed_full_cue": None,
                "repair_class": "disclosure_only",
                "why": "语境用词可疑",
            }
        ],
        entity_verifier=lambda request: _witness(
            request, "jin tian neng duo bao bao ma"
        ),
        judge_llm_call=cpa,
    )

    assert resolved == []
    finding = unresolved[0]
    assert finding["proposed_full_cue"] == "今天能多抱抱嘛"
    adjudication = finding["exact_release_adjudication"]
    assert adjudication["repaired"] is True
    assert (
        adjudication["policy_branch"]
        in {
            "WITNESS_JUDGE_APPLY_PROPOSED",
            "CPA_SEMANTIC_ORTHOGRAPHY_TIEBREAK_APPLY_PROPOSED",
        }
    )
    binding = adjudication["request"]["cpa_convergence_binding"]
    assert binding["final_srt_sha256"] == (
        "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest()
    )
    assert binding["context_sha256"].startswith("sha256:")
    assert adjudication["verdict"]["status"] == "OBSERVED"
    assert adjudication["mutation_authority"]["status"] == "PASS"
    from src.autoslice.producer_package_finalization import (
        _apply_exact_final_cpa_repairs,
    )

    healed, repairs = _apply_exact_final_cpa_repairs(
        source,
        {"findings": unresolved},
    )
    assert "今天能多抱抱嘛" in healed
    assert len(repairs) == 1
    assert repairs[0]["decision_authority"] == "CPA_JUDGE"


def test_exact_text_convergence_allows_shared_prefix_inside_wide_suspect():
    source = _srt("他说完了", "我不知道", "他就说下一件事")

    def cpa(prompt):
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "proposed_cue": "",
                    "reason": "首轮未给候选，继续测试独立语境收敛",
                },
                ensure_ascii=False,
            )
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps(
                {
                    "decision": "REPLACE_WITH_EXACT_TEXT",
                    "replacement_text": "我上次",
                    "reason": "保留共享主语我，替换其后的误听片段",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"choice": "PROPOSED", "reason": "盲听支持文字候选"},
            ensure_ascii=False,
        )

    output, adjudication = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "我不知道",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "phonetic",
        },
        entity_verifier=lambda request: _witness(request, "wo shang ci"),
        judge_llm_call=cpa,
    )

    assert "我上次" in output
    assert "我不知道" not in output
    assert adjudication["status"] == "OBSERVED"
    assert adjudication["repaired"] is True
    convergence = adjudication["cpa_missing_proposal_convergence"]
    assert convergence["status"] == "RESOLVED"
    assert convergence["decision"] == "REPLACE_WITH_EXACT_TEXT"
    assert convergence["replacement_text"] == "我上次"


def test_exact_text_convergence_handles_repeated_narrow_suspect():
    source = _srt("前一句", "直女不是侄女", "后一句")

    output, adjudication = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "女",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "phonetic",
            "why": "审片员只标出重复出现的单字",
        },
        entity_verifier=lambda request: _witness(
            request, "zhi nv bu shi zi nv"
        ),
        judge_llm_call=lambda prompt: json.dumps(
            (
                {
                    "decision": "REPLACE_WITH_EXACT_TEXT",
                    "replacement_text": "直女不是子女",
                    "reason": "完整 cue 语境支持后一处是子女",
                }
                if "# 字幕缺失候选最终收敛" in prompt
                else {"choice": "PROPOSED", "reason": "盲听支持文字候选"}
            ),
            ensure_ascii=False,
        ),
    )

    assert "直女不是子女" in output
    assert adjudication["status"] == "OBSERVED"
    assert adjudication["repaired"] is True
    assert (
        adjudication["proposal_bootstrap"]["reason_code"]
        == "SUSPECT_NOT_UNIQUE"
    )
    convergence = adjudication["cpa_missing_proposal_convergence"]
    assert convergence["status"] == "RESOLVED"
    assert convergence["suspect_occurrence_count"] == 2
    assert convergence["decision"] == "REPLACE_WITH_EXACT_TEXT"


def test_candidate_less_repeated_surface_preserves_explicit_occurrence():
    source = _srt("直女不是侄女")
    findings = audit_final_subtitles(
        source, llm_call=_fake_llm([{
            "cue": 1, "kind": "context", "suspect": "女",
            "proposed_full_cue": None, "repair_class": "phonetic",
            "span_start_codepoint": 5, "span_end_codepoint": 6,
            "why": "只核末字，不猜替换",
        }]), extract_json=_extract,
    )
    assert len(findings) == 1
    assert findings[0]["span_start_codepoint"] == 5
    assert findings[0]["span_end_codepoint"] == 6
    scaffold = auditor._candidate_blind_missing_proposal_scaffold(source, findings[0])
    assert scaffold is not None
    assert scaffold["proposed_full_cue"] == "直女不是侄女"
    assert scaffold["span_start_codepoint"] == 5
    assert scaffold["candidate_provenance"]["mutation_authorized"] is False
    request = build_context_adjudication_request(source, scaffold)
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    witness_request = build_witness_request(request)
    assert "proposed_cue" not in witness_request


@pytest.mark.parametrize("span", [(4, 5), (5, 7), (True, 6), (5, None)])
def test_bad_explicit_suspect_span_never_falls_back_to_text_search(span):
    source = _srt("直女不是侄女")
    finding = {
        "cue_index": 1, "suspect": "女", "proposed_full_cue": None,
        "span_start_codepoint": span[0], "span_end_codepoint": span[1],
    }
    assert auditor._candidate_blind_missing_proposal_scaffold(source, finding) is None


def test_bootstrap_repeated_surface_uses_reviewed_occurrence(monkeypatch):
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal
    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=_srt("直女不是侄女"), finding={
            "cue_index": 1, "suspect": "女",
            "span_start_codepoint": 5, "span_end_codepoint": 6,
        }, structured_chat="",
        llm_call=lambda _: json.dumps({"status": "PROPOSED", "proposed_cue": "直女不是侄儿"}),
        derive_single_span_edit=auditor._derive_single_span_edit,
    )
    assert rebuilt is not None
    assert rebuilt["span_start_codepoint"] == 5
    assert rebuilt["suggestion"] == "儿"
    assert audit["mutation_authorized"] is False


def test_missing_candidate_second_cpa_provider_failure_stays_blocked():
    source = _srt("我走了之后")
    calls = 0

    def cpa(_prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "proposed_cue": "",
                    "reason": "首轮不猜",
                },
                ensure_ascii=False,
            )
        raise RuntimeError("provider unavailable")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "我走了之后",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "disclosure_only",
        },
        entity_verifier=lambda request: pytest.fail(
            f"AGY must not run without a decided candidate: {request}"
        ),
        judge_llm_call=cpa,
    )

    assert output == source
    assert audit["status"] == "MISSING_PROPOSAL_UNRESOLVED"
    assert (
        audit["reason_code"]
        == "CPA_CONVERGENCE_PROVIDER_OR_JSON_UNAVAILABLE"
    )
    assert audit["decision_authority"] == "CPA_JUDGE_FAILED"
    assert calls == 2


def test_exact_release_self_heals_a_bootstrapped_missing_candidate():
    source = _srt("那我不应该说咱", "嗯，咱有点像迪酱", "我要吃午饭")

    def cpa(prompt):
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "proposed_cue": "嗯，咱有点像自称",
                    "reason": "上下文在讨论第一人称自称",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "choice": "PROPOSED",
                "reason": "盲听支持",
                "current_utterance_supported": False,
                "current_utterance_support_reason": "测试候选被选中",
            }
        )

    unresolved, resolved = adjudicate_exact_release_findings(
        source,
        [
            {
                "cue_index": 2,
                "suspect": "迪酱",
                "suggestion": None,
                "proposed_full_cue": None,
                "repair_class": "disclosure_only",
            }
        ],
        entity_verifier=lambda request: _witness(
            request, "en zan you dian xiang zi cheng"
        ),
        judge_llm_call=cpa,
    )

    assert resolved == []
    assert unresolved[0]["proposed_full_cue"] == "嗯，咱有点像自称"
    assert unresolved[0]["suggestion"] == "自称"
    from src.autoslice.producer_package_finalization import (
        _apply_exact_final_cpa_repairs,
    )

    healed, repairs = _apply_exact_final_cpa_repairs(
        source,
        {"findings": unresolved},
    )
    assert "嗯，咱有点像自称" in healed
    assert len(repairs) == 1
    assert repairs[0]["decision_authority"] == "CPA_JUDGE"


def test_missing_candidate_applies_final_cpa_choice_with_conflict_disclosed():
    source = _srt(
        "那咱算不算？咱",
        "烦 嗯 烦死人了下 はい りっちゃん",
        "我要吃午饭",
    )

    def cpa(prompt):
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "proposed_cue": "嗯，咱有点像あたし",
                    "reason": "整条是混杂碎片，语境在讨论日语第一人称",
                },
                ensure_ascii=False,
            )
        return json.dumps({"choice": "PROPOSED", "reason": "盲听与语境一致"})

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "烦 嗯 烦死人了下 はい りっちゃん",
            "suggestion": None,
            "proposed_full_cue": None,
            "repair_class": "phonetic",
            "why": "整条语义崩坏",
        },
        entity_verifier=lambda request: _witness(
            request, "en zan you dian xiang a ta xi"
        ),
        judge_llm_call=cpa,
    )

    assert output == _srt(
        "那咱算不算？咱", "嗯，咱有点像あたし", "我要吃午饭"
    )
    assert audit["repaired"] is True
    assert audit["policy_branch"] == (
        "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
    )
    diagnostic = audit["witness_judge"]["witness_conflict_diagnostic"]
    assert diagnostic["effect"] == "DISCLOSURE_ONLY"
    assert diagnostic["additional_support_found"] is False
    assert "witness_conflict_gate" not in audit["witness_judge"]
    assert audit["proposal_bootstrap"]["bounded_full_cue_repair"] is True
    assert audit["proposal_bootstrap"]["mutation_authorized"] is False
    assert audit["rebuilt_finding"]["span_start_codepoint"] == 0
    assert audit["rebuilt_finding"]["span_end_codepoint"] == 18


@pytest.mark.parametrize(
    "current,suspect,proposed",
    [
        ("然后屏幕全按跟追兵爆了。", "然后屏幕全按跟追兵爆了。",
         "也会屏幕全按跟追兵爆了。"),
        ("我见过一个东方向，玩东方向玩恐怖游戏", "我见过一个东方向，玩东方向玩",
         "我见过一个东方像，玩东方像玩恐怖游戏"),
        ("这是小四在商场遇见的", "小四", "这是小室在商场遇见的"),
        ("这个词尾在这里", "词尾", "这个词的尾部在这里"),
    ],
)
def test_complete_proposal_keeps_unchanged_words_inside_reviewed_suspect(
    current, suspect, proposed, monkeypatch,
):
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=_srt(current), finding={"cue_index": 1, "suspect": suspect},
        structured_chat="",
        llm_call=lambda _prompt: json.dumps(
            {"status": "PROPOSED", "proposed_cue": proposed}, ensure_ascii=False,
        ),
        derive_single_span_edit=auditor._derive_single_span_edit,
    )

    assert rebuilt is not None
    assert rebuilt["proposed_full_cue"] == proposed
    start, end = rebuilt["span_start_codepoint"], rebuilt["span_end_codepoint"]
    assert current[start:end] == suspect
    assert current[:start] + rebuilt["suggestion"] + current[end:] == proposed
    assert audit["minimal_edit_expanded_to_suspect"] is True
    assert audit["mutation_authorized"] is False
    assert rebuilt["candidate_provenance"]["mutation_authorized"] is False


@pytest.mark.parametrize(
    "current,suspect,proposed",
    [
        ("不许操控直播间观众", "不许操控直播间观众", "不许操控直播间观众必须操控"),
        ("甲乙丙", "乙", "甲X乙丙"),
        ("甲乙丙", "乙", "甲乙X丙"),
        ("甲乙丙", "甲乙", "甲X乙丙"),
    ],
)
def test_complete_proposal_expands_insertion_inside_reviewed_suspect(
    current, suspect, proposed, monkeypatch,
):
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=_srt(current), finding={"cue_index": 1, "suspect": suspect},
        structured_chat="",
        llm_call=lambda _prompt: json.dumps(
            {"status": "PROPOSED", "proposed_cue": proposed}, ensure_ascii=False,
        ),
        derive_single_span_edit=auditor._derive_single_span_edit,
    )

    assert rebuilt is not None
    start, end = rebuilt["span_start_codepoint"], rebuilt["span_end_codepoint"]
    assert current[start:end] == suspect
    assert current[:start] + rebuilt["suggestion"] + current[end:] == proposed
    assert audit["minimal_edit_expanded_to_suspect"] is True
    assert audit["mutation_authorized"] is False


def test_complete_proposal_rejects_insertion_outside_reviewed_suspect(monkeypatch):
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=_srt("甲乙丙"), finding={"cue_index": 1, "suspect": "乙"},
        structured_chat="",
        llm_call=lambda _prompt: json.dumps(
            {"status": "PROPOSED", "proposed_cue": "甲乙丙X"},
            ensure_ascii=False,
        ),
        derive_single_span_edit=auditor._derive_single_span_edit,
    )

    assert rebuilt is None
    assert audit["reason_code"] == "MISSING_PROPOSAL_DOES_NOT_COVER_SUSPECT"
    assert audit["mutation_authorized"] is False


def test_complete_proposal_cannot_fix_a_different_suspect(monkeypatch):
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal

    monkeypatch.delenv("AUTOSLICE_BASE", raising=False)
    rebuilt, audit = bootstrap_missing_proposal(
        srt_text=_srt("这是小四在商场遇见的"),
        finding={"cue_index": 1, "suspect": "小四"}, structured_chat="",
        llm_call=lambda _prompt: json.dumps(
            {"status": "PROPOSED", "proposed_cue": "这是小四在市场遇见的"},
            ensure_ascii=False,
        ),
        derive_single_span_edit=auditor._derive_single_span_edit,
    )

    assert rebuilt is None
    assert audit["reason_code"] == "MISSING_PROPOSAL_DOES_NOT_COVER_SUSPECT"
    assert audit["mutation_authorized"] is False


def test_context_adjudication_keeps_current_when_semantics_conflict_with_audio():
    source = _srt("还没有歌杂呢")

    def acoustics_veto_proposal(request):
        return _witness(request, "hai mei you ge za ne")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
        entity_verifier=acoustics_veto_proposal,
        judge_llm_call=_judge("CURRENT"),
    )

    assert output == source
    assert audit["status"] == "OBSERVED"
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"


def test_source_backed_letter_name_spelling_survives_acoustic_grapheme_veto():
    source = _srt("哪里又变成小主被大大恩霸凌了")
    finding = {
        "cue_index": 1,
        "suspect": "大大恩",
        "suggestion": "大N",
        "proposed_full_cue": "哪里又变成小主被大N霸凌了",
        "repair_class": "source_backed_entity",
        "candidate_provenance": {
            "kind": "glossary",
            "surface": "大N",
        },
        "why": "同一人物昵称已有来源见证",
    }

    def acoustics_reports_spoken_en(request):
        return _witness(
            request, "na li you bian cheng xiao zhu bei da da en ba ling le"
        )

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=acoustics_reports_spoken_en,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "大N霸凌" in output
    assert "大大恩" not in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == (
        "CPA_JUDGE_WITH_TEXT_AUTHORITY_APPLY_PROPOSED"
    )
    assert audit["orthography_equivalence"]["matched"] is True


@pytest.mark.parametrize("choice", ["CURRENT", "PROPOSED"])
def test_glossary_conflict_follows_final_cpa_choice_without_granting_glossary_authority(choice):
    """Keep the historical conflicting inputs without restoring the retired veto.

    Step 41 assigns final ordinary text choice to CPA. These synthetic verdicts
    test that consumer contract, not which historical word was actually spoken.
    """

    source = _srt("你知道我要偶遇啊！偶遇")
    finding = {
        "cue_index": 1,
        "suspect": "偶遇啊！偶遇",
        "suggestion": "殉情啊！殉情",
        "proposed_full_cue": "你知道我要殉情啊！殉情",
        "repair_class": "source_backed_entity",
        "candidate_provenance": {
            "kind": "glossary",
            "surface": "殉情",
        },
        "why": "鹅鸭杀恋人机制词，语境呼应死亡后果",
    }

    def short_misaligned_witness(request):
        # 生产实况：证人只覆盖前句尾字「懂吗」加错位片段，未听清目标差异段。
        return _witness(request, "dong ma ni")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=short_misaligned_witness,
        judge_llm_call=_judge(choice),
    )

    if choice == "CURRENT":
        assert output == source
        assert audit["repaired"] is False
        assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    else:
        assert output == _srt("你知道我要殉情啊！殉情")
        assert audit["repaired"] is True
        assert audit["policy_branch"] == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
        diagnostic = audit["witness_judge"]["witness_conflict_diagnostic"]
        assert diagnostic["effect"] == "DISCLOSURE_ONLY"
        assert diagnostic["additional_support_found"] is False
        assert "witness_conflict_gate" not in audit["witness_judge"]
    assert audit["orthography_ambiguous"] is False


def test_glossary_candidate_with_registered_misheard_direction_wins_witness_conflict():
    """公开规则回归修正正向金丝雀之一：kmx 类误听面方向历史合法胜出案例。

 ``停放熊 -> kmx`` 是 ``assets/lidousha/glossary.txt`` 登记的已知 kmx 误听
 面，但只登记在 ``expected_value_respell_pairs()``（不在
 ``respell_pairs()``，两者来源不同），所以 ``orthography_ambiguous``
 （只查 ``respell_pairs()`` 的 ``_declared_respell_edit``）对这一对必然是
 False。84e3603 首版守卫只要 ``candidate_provenance.kind == "glossary"``
 就拦截，会连带把这类历史上应当胜出的登记误听方向也拦掉——这正是
 公开规则指出的回归风险。此用例证明收窄后的 ``registered_direction`` 检查
 （同时查 ``respell_pairs()`` 与 ``expected_value_respell_pairs()``）放行
 了它；回退到收窄前的守卫代码，本用例会转为失败（错误地拦截）。"""

    source = _srt("你听到停放熊在门口叫了吗")
    finding = {
        "cue_index": 1,
        "suspect": "停放熊",
        "suggestion": "kmx",
        "proposed_full_cue": "你听到kmx在门口叫了吗",
        "repair_class": "source_backed_entity",
        "candidate_provenance": {
            "kind": "glossary",
            "surface": "kmx",
        },
        "why": "kmx 常见误听面",
    }

    def short_misaligned_witness(request):
        return _witness(request, "dong ma ni")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=short_misaligned_witness,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "kmx" in output
    assert "停放熊" not in output
    assert audit["repaired"] is True
    assert audit["orthography_ambiguous"] is False
    assert audit["policy_branch"] == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"


def test_glossary_candidate_with_bound_structured_chat_support_wins_witness_conflict():
    """公开规则回归修正正向金丝雀之二：独立结构化文字支持胜出案例。

 与上面 cue59 负向金丝雀同一事实模式（候选「殉情」、同一错位证人、同一
 judge PROPOSED），唯一变量是这次候选替换词面「殉情」被一条 sha256 绑定
 的弹幕/SC 独立佐证（``clip_context.structured_chat``）——这正是 公开规则
 描述的「历史上耳朵持续听错、词表/独立文字证据佐证的候选正确胜出」场景
 的机制对照组。收窄后的 ``structured_text_support`` 检查放行；回退到
 收窄前的守卫代码（不接收 ``clip_context``），本用例会转为失败（错误地
 拦截），证明这不是巧合通过。"""

    source = _srt("你知道我要偶遇啊！偶遇")
    finding = {
        "cue_index": 1,
        "suspect": "偶遇啊！偶遇",
        "suggestion": "殉情啊！殉情",
        "proposed_full_cue": "你知道我要殉情啊！殉情",
        "repair_class": "source_backed_entity",
        "candidate_provenance": {
            "kind": "glossary",
            "surface": "殉情",
        },
        "why": "鹅鸭杀恋人机制词，弹幕独立确认",
    }
    clip_context = {
        "structured_chat": [
            {
                "kind": "danmaku",
                "text": "啊啊啊两人要殉情了吗",
                "source_event_id": "event-59",
                "source_sha256": "b" * 64,
            }
        ]
    }

    def short_misaligned_witness(request):
        return _witness(request, "dong ma ni")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=short_misaligned_witness,
        clip_context=clip_context,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "殉情" in output
    assert "偶遇" not in output
    assert audit["repaired"] is True
    assert audit["orthography_ambiguous"] is False
    assert audit["policy_branch"] == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"


def test_cpa_can_use_distinguishing_pinyin_without_text_authority():
    source = _srt("毁神来了")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "毁神",
        "suggestion": "绘声",
        "proposed_full_cue": "绘声来了",
        "repair_class": "phonetic",
        "why": "模型猜测另一种写法",
    }

    def acoustics_claims_spelling_difference(request):
        return _witness(request, "hui sheng lai le")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=acoustics_claims_spelling_difference,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "绘声来了" in output
    assert audit["repaired"] is True
    assert audit["orthography_ambiguous"] is False
    assert audit["orthography_authority"]["status"] == "BLOCK"
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert audit["mutation_authority"]["basis"] == (
        "CPA_ACOUSTIC_PRONUNCIATION_DISAMBIGUATION"
    )


def test_strict_homophone_tie_judge_semantic_tiebreak_applies_proposed():
    """公开规则概率裁定令：一/咦 同音，音频定义上中立，judge 按语义
 排序拍板施改；mutation basis 为 SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK。"""

    source = _srt("一、那现在就等")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "一、",
        "suggestion": "咦，",
        "proposed_full_cue": "咦，那现在就等",
        "repair_class": "phonetic",
        "why": "语气词更通顺",
    }

    def neutral_witness(request):
        return _witness(request, "yi na xian zai jiu deng")

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=neutral_witness,
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "咦，那现在就等" in output
    assert audit["repaired"] is True
    assert audit["orthography_ambiguous"] is True
    assert audit["orthography_authority"]["status"] == "BLOCK"
    assert audit["policy_branch"] == (
        "CPA_SEMANTIC_ORTHOGRAPHY_TIEBREAK_APPLY_PROPOSED"
    )
    assert audit["mutation_authority"]["status"] == "PASS"
    assert audit["mutation_authority"]["basis"] == (
        "SEMANTIC_JUDGE_ORTHOGRAPHY_TIEBREAK"
    )


def test_strict_homophone_tie_judge_current_keeps_and_discloses():
    source = _srt("一、那现在就等")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "一、",
        "suggestion": "咦，",
        "proposed_full_cue": "咦，那现在就等",
        "repair_class": "phonetic",
        "why": "语气词更通顺",
    }

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "yi na xian zai jiu deng"
        ),
        judge_llm_call=_judge("CURRENT"),
    )

    assert output == source
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert audit["mutation_authority"]["status"] == "NOT_APPLIED"


def test_near_homophone_without_authority_still_goes_to_cpa():
    """近音可由拼音区分；没有文字权威时也必须由 CPA 明确裁决，不能由
    预判门在 CPA 之前替它保持现文本。"""

    source = _srt("毁神来了")
    judge_calls = []

    def counting_judge(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        judge_calls.append(prompt)
        return _judge("PROPOSED")(prompt)

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "kind": "context",
            "suspect": "毁神",
            "suggestion": "绘声",
            "proposed_full_cue": "绘声来了",
            "repair_class": "phonetic",
            "why": "模型猜测另一种写法",
        },
        entity_verifier=lambda request: _witness(
            request, "hui sheng lai le"
        ),
        judge_llm_call=counting_judge,
    )

    assert "绘声来了" in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"
    assert len(judge_calls) == 1


def test_mutation_audit_accepts_semantic_tiebreak_and_blocks_laundering():
    """审计端与生产端对称复算：真实语义拍板收 PASS；把 judge 实际选
    CURRENT 的行贴上 SEMANTIC basis 必须 BLOCK（防洗白）。"""

    import copy

    source = _srt("一、那现在就等")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "一、",
        "suggestion": "咦，",
        "proposed_full_cue": "咦，那现在就等",
        "repair_class": "phonetic",
        "why": "语气词更通顺",
    }
    _output, adj = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "yi na xian zai jiu deng"
        ),
        judge_llm_call=_judge("PROPOSED"),
    )
    row = {
        **finding,
        "routed": "context_audio_adjudicated_fix",
        "context_audio_adjudication": adj,
    }

    audit = audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [row],
            "applied_count": 1,
        }
    )
    assert audit["status"] == "PASS"
    assert audit["validated_mutation_count"] == 1

    forged = copy.deepcopy(row)
    forged["context_audio_adjudication"]["witness_judge"]["judge"][
        "choice"
    ] = "CURRENT"
    forged_audit = audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "APPLIED",
            "findings": [forged],
            "applied_count": 1,
        }
    )
    assert forged_audit["status"] == "BLOCK"


def test_context_adjudication_rejects_unbound_verdict():
    source = _srt("还没有歌杂呢")

    def stale_verdict(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": "0" * 64,
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
        }

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
            "why": "重复话题",
        },
        entity_verifier=stale_verdict,
    )

    assert output == source
    assert audit["status"] == "UNCERTAIN"


def test_context_adjudication_rejects_any_model_returned_text_channel():
    source = _srt("还没有歌杂呢")

    def illicit_text_response(request):
        return {
            "schema_version": "subtitle-span-acoustic-check-verdict.v1",
            "request_sha256": request["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "current_fit": "PLAUSIBLE",
            "proposed_fit": "SUPPORTED",
            "canonical_entity": "模型自行写出的第三句",
        }

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "歌杂",
            "suggestion": "歌债",
            "proposed_full_cue": "还没有歌债呢",
            "repair_class": "phonetic",
        },
        entity_verifier=illicit_text_response,
    )

    assert output == source
    assert audit["status"] == "UNCERTAIN"


def test_source_backed_entity_insertion_survives_contract(monkeypatch):
    """xyz 整词漏听案：ASR 零召回的专名只能靠插入修复。
    source_backed_entity + 词表见证 + 声学仲裁三重门下允许空 suspect 插入。"""
    srt = (
        "1\n00:00:00,000 --> 00:00:04,000\n只有怎么，你为什么会这样称呼主播\n\n"
        "2\n00:00:05,000 --> 00:00:09,000\n第二句正常文本\n"
    )
    findings = audit_final_subtitles(
        srt,
        llm_call=_literal_review_llm(lambda prompt: json.dumps(
            {
                "findings": [
                    {
                        "cue": 1,
                        "kind": "entity",
                        "proposed_full_cue": "只有xyz怎么，你为什么会这样称呼主播",
                        "repair_class": "source_backed_entity",
                        "source_surface": "xyz",
                        "evidence_cue_ids": [],
                        "why": "SC称呼串只有xyz会说，ASR整词漏听",
                    }
                ]
            },
            ensure_ascii=False,
        )),
        extract_json=json.loads,
        glossary_text="- 人名/ID：xyz（主播常提的人）",
        structured_context_text="selection_hook: 只有xyz会这样称呼主播",
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding["suspect"] == ""
    assert finding["suggestion"] == "xyz"
    assert finding.get("suggestion_rejected_reason") is None
    assert finding["candidate_provenance"] == {"kind": "glossary", "surface": "xyz"}

    # 插入建议不走同音自动应用（空 suspect 与 xyz 不同音），必须进声学仲裁。
    output, audit = route_findings(srt, findings)
    assert "只有xyz怎么" not in output
    row = audit["findings"][0]
    assert row["routed"] == "disclosure"

    # 声学仲裁支持插入候选时才真正落地。
    def prefer_proposed(request):
        return _witness(
            request,
            "zhi you xyz zen me ni wei shen me hui zhe yang cheng hu zhu bo",
        )

    repaired_srt, adj = adjudicate_context_finding(
        srt, findings[0], entity_verifier=prefer_proposed,
        judge_llm_call=_judge("PROPOSED"),
    )
    assert adj["repaired"] is True
    assert "只有xyz怎么" in repaired_srt


def test_plain_insertion_without_source_provenance_still_rejected():
    """非 source_backed 的插入建议依旧被拒（防审片员自由加词）。"""
    srt = "1\n00:00:00,000 --> 00:00:04,000\n只有怎么会这样\n"
    with pytest.raises(FinalReviewAuditError) as raised:
        audit_final_subtitles(
            srt,
            llm_call=_literal_review_llm(lambda prompt: json.dumps(
                {
                    "findings": [
                        {
                            "cue": 1,
                            "kind": "context",
                            "proposed_full_cue": "只有他怎么会这样",
                            "repair_class": "phonetic",
                            "why": "凭感觉加词",
                        }
                    ]
                },
                ensure_ascii=False,
            )),
            extract_json=json.loads,
        )
    assert (
        raised.value.reason_code
        == "FINAL_REVIEW_RESPONSE_FINDINGS_ALL_INVALID"
    )


def test_spoken_unit_insertion_is_bounded_then_goes_through_cpa():
    """A missing ordinary spoken word is not an entity-only insertion.

    The full-cue diff owns an empty span, but the candidate still cannot
    mutate bytes until AGY supplies candidate-blind pronunciation evidence
    and CPA chooses PROPOSED from the closed set.
    """

    source = _srt("为什么是")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "为什么是又",
                    "repair_class": "spoken_unit",
                    "why": "疑似漏了追问里的又",
                }
            ]
        ),
        extract_json=json.loads,
    )

    finding = findings[0]
    assert finding["suspect"] == ""
    assert finding["suggestion"] == "又"
    assert finding["span_start_codepoint"] == finding["span_end_codepoint"]
    routed, audit = route_findings(source, findings)
    assert routed == source
    assert audit["findings"][0]["routed"] == "disclosure"

    repaired, adjudication = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "wei shen me shi you"
        ),
        judge_llm_call=_judge("PROPOSED"),
    )
    assert "为什么是又" in repaired
    assert adjudication["decision_authority"] == "CPA_JUDGE"


def test_spoken_unit_partial_deletion_is_bounded_then_goes_through_cpa():
    """An ASR-added discourse word may be deleted only after CPA adjudication."""

    source = _srt("什么会问出来的问题啊")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "会问出来的问题啊",
                    "repair_class": "spoken_unit",
                    "why": "与前句连读时 ASR 多出了什么",
                }
            ]
        ),
        extract_json=json.loads,
    )

    finding = findings[0]
    assert finding["suspect"] == "什么"
    assert finding["suggestion"] == ""
    assert finding["proposed_full_cue"] == "会问出来的问题啊"
    routed, audit = route_findings(source, findings)
    assert routed == source
    assert audit["findings"][0]["routed"] == "disclosure"

    repaired, adjudication = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _witness(
            request, "hui wen chu lai de wen ti a"
        ),
        judge_llm_call=_judge("PROPOSED"),
    )
    assert "什么" not in repaired
    assert "会问出来的问题啊" in repaired
    assert adjudication["decision_authority"] == "CPA_JUDGE"
    assert adjudication["mutation_authority"]["status"] == "PASS"


def test_spoken_unit_cannot_delete_the_whole_cue():
    source = _srt("咳咳咳")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "",
                    "repair_class": "spoken_unit",
                    "why": "不能用普通口语类删掉整条",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["suggestion"] is None
    assert (
        findings[0]["suggestion_rejected_reason"]
        == "SPOKEN_UNIT_DROP_CUE_NOT_ALLOWED"
    )


def test_acoustic_partial_delete_removes_only_unspoken_prefix():
    source = _srt("我草，乱说的啊")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "乱说的啊",
                    "repair_class": "acoustic_delete",
                    "why": "前缀疑似无声，后半句有口播",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["suggestion"] == ""
    routed, route_audit = route_findings(source, findings)
    assert routed == source
    assert route_audit["findings"][0]["routed"] == "disclosure"

    def strict_delete(request):
        return _witness(request, "luan shuo de a")

    repaired, audit = adjudicate_context_finding(
        source, findings[0], entity_verifier=strict_delete,
        judge_llm_call=_judge("PROPOSED"),
    )
    assert "我草" not in repaired
    assert "乱说的啊" in repaired
    assert audit["policy_branch"] == "WITNESS_JUDGE_APPLY_PROPOSED"


def test_acoustic_drop_cue_requires_whole_target_inaudible():
    source = _srt("我草")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "",
                    "repair_class": "acoustic_drop_cue",
                    "why": "整条疑似无声幻听",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["suspect"] == "我草"

    def inaudible(request):
        return _witness(request, "?", audible=False, uncertain=(0,))

    repaired, audit = adjudicate_context_finding(
        source,
        findings[0],
        entity_verifier=inaudible,
        judge_llm_call=_judge("DROP"),
    )
    assert repaired == ""
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
    assert audit["witness_judge"]["judge"]["choice"] == "DROP"
    assert audit["drop_authority"]["status"] == "PASS"


def test_inaudible_cpa_drop_bypasses_text_rebuild_and_drops_whole_cue():
    source = _srt("给我换哪一个来着", "咳咳", "先这样好了")
    calls: list[str] = []

    def cpa(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        calls.append(prompt)
        if "# 字幕坏闭集重建" in prompt:
            raise AssertionError("inaudible three-way choice must not rebuild text")
        assert "<DROP_CUE: EMPTY SUBTITLE>" in prompt
        return json.dumps({"choice": "DROP", "reason": "目标窗没有可闻语音"})

    repaired, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "咳咳",
            "suggestion": "嗯嗯",
            "proposed_full_cue": "嗯嗯",
            "repair_class": "phonetic",
            "why": "声学初筛怀疑是连续 m 音",
        },
        entity_verifier=lambda request: _witness(
            request, "", audible=False
        ),
        judge_llm_call=cpa,
    )

    assert "咳咳" not in repaired
    assert "先这样好了" in repaired
    assert audit["repaired"] is True
    assert audit["policy_branch"] == "CPA_JUDGE_APPLY_INAUDIBLE_DROP_CUE"
    assert audit["request"]["repair_class"] == "acoustic_drop_cue"
    assert audit["request"]["proposed_cue"] == ""
    assert audit["rebuilt_finding"]["repair_class"] == "acoustic_drop_cue"
    assert audit["inaudible_drop_promotion"]["status"] == "PASS"
    assert audit["inaudible_drop_promotion"]["cpa_final_choice"] == "DROP"
    assert audit["drop_authority"]["choice"] == "DROP"
    assert len(calls) == 1


@pytest.mark.parametrize("choice", ["DROP", "CURRENT"])
def test_missing_candidate_inaudible_witness_reaches_explicit_cpa_choice(choice):
    source = _srt("感觉到了主角的堕落", "了", "但是你在看别人作品")
    calls = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕缺失候选重建" in prompt:
            return json.dumps({"status": "UNRESOLVED", "proposed_cue": ""})
        if "# 字幕缺失候选最终收敛" in prompt:
            return json.dumps({"decision": "UNRESOLVED", "needs_audio": True,
                               "reason": "需要目标音频"})
        assert "<DROP_CUE: EMPTY SUBTITLE>" in prompt
        return json.dumps({"choice": choice, "reason": "目标是清嗓声",
                           "needs_audio": False,
                           "current_utterance_supported": False,
                           "current_utterance_support_reason": "没有可闻语音"})

    output, audit = adjudicate_context_finding(
        source,
        {"cue_index": 2, "suspect": "了", "suggestion": None,
         "proposed_full_cue": None, "repair_class": "phonetic"},
        entity_verifier=lambda request: _witness(request, "", audible=False),
        judge_llm_call=cpa,
    )

    assert len(calls) == 3
    assert audit["witness_judge"]["judge"]["choice"] == choice
    if choice == "DROP":
        assert audit["repaired"] is True
        assert audit["drop_authority"]["status"] == "PASS"
        assert audit["inaudible_drop_promotion"]["cpa_final_choice"] == "DROP"
        assert "\n了\n" not in output
        assert "00:00:15,000 --> 00:00:19,000" in output
    else:
        assert output == source
        assert audit["repaired"] is False
        assert "drop_authority" not in audit
        assert audit["mutation_authority"]["status"] == "NOT_APPLIED"


def test_inaudible_cpa_nonempty_proposed_is_typed_override_not_drop():
    source = _srt("给我换哪一个来着", "咳咳", "先这样好了")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "咳咳",
            "suggestion": "嗯嗯",
            "proposed_full_cue": "嗯嗯",
            "repair_class": "phonetic",
        },
        entity_verifier=lambda request: _witness(
            request, "", audible=False
        ),
        judge_llm_call=_judge("PROPOSED"),
    )

    assert "咳咳" not in output
    assert "嗯嗯" in output
    assert audit["repaired"] is True
    assert audit["policy_branch"] == (
        "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS"
    )
    assert audit["request"]["proposed_cue"] == "嗯嗯"
    assert audit["mutation_authority"] == {
        "schema_version": "subtitle-correction-mutation-authority.v1",
        "status": "PASS",
        "basis": "CPA_EXPLICIT_OVERRIDE_INAUDIBLE_WITNESS",
    }
    assert (
        audit["witness_judge"]["inaudible_witness_override"]["status"]
        == "PASS"
    )
    assert "drop_authority" not in audit


def test_inaudible_three_way_stays_blocked_when_cpa_keeps_current():
    source = _srt("给我换哪一个来着", "咳咳", "先这样好了")
    def cpa(prompt):
        if "# 字幕坏闭集重建" in prompt:
            raise AssertionError("CURRENT must terminate the three-way decision")
        return json.dumps({"choice": "CURRENT"})

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "咳咳",
            "suggestion": "嗯嗯",
            "proposed_full_cue": "嗯嗯",
            "repair_class": "phonetic",
        },
        entity_verifier=lambda request: _witness(
            request, "", audible=False
        ),
        judge_llm_call=cpa,
    )

    assert output == source
    assert audit["repaired"] is False
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert "inaudible_drop_promotion" not in audit


def test_direct_drop_neither_is_not_reasked_as_the_same_empty_candidate():
    source = _srt("咳咳")
    calls: list[str] = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕坏闭集重建" in prompt:
            return json.dumps(
                {
                    "status": "UNRESOLVED",
                    "proposed_cue": "",
                    "reason": "不生成第三候选",
                },
                ensure_ascii=False,
            )
        return json.dumps({"choice": "NEITHER"})

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "咳咳",
            "suggestion": "",
            "span_start_codepoint": 0,
            "span_end_codepoint": 2,
            "proposed_full_cue": "",
            "repair_class": "acoustic_drop_cue",
        },
        entity_verifier=lambda request: _witness(
            request, "", audible=False
        ),
        judge_llm_call=cpa,
    )

    assert output == source
    assert audit["repaired"] is False
    assert "inaudible_drop_promotion" not in audit
    assert len(calls) == 1


def test_audible_acoustic_drop_neither_rebuilds_nonempty_third_candidate():
    """1411 regression: an audible cue cannot stay trapped in CURRENT/empty."""

    source = _srt("草原，到这来吧", "小林你怎么被点了", "不知道")
    witness_requests: list[dict] = []
    calls: list[str] = []

    def witness(request):
        witness_requests.append(request)
        return _witness(request, "xiao zhu ni zen me bei dian le", audible=True)

    witness.observe_independent_witnesses = _failed_independent_routes

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕坏闭集重建" in prompt:
            assert "- REJECTED_PROPOSED: " in prompt
            return json.dumps(
                {
                    "status": "PROPOSED",
                    "candidate_failures": "独立听写均报告目标不确定",
                    "evidence_basis": "实际目标拼音和绑定前后文",
                    "residual_uncertainty": "音素有支持，仍需最终CPA裁决",
                    "proposed_cue": "小主你怎么被电了",
                    "reason": "听写与弹幕语境支持非空第三候选",
                },
                ensure_ascii=False,
            )
        judge_count = len(
            [row for row in calls if "# 字幕选字裁决" in row]
        )
        if judge_count == 1:
            assert "<DROP_CUE: EMPTY SUBTITLE>" not in prompt
            return json.dumps({"choice": "NEITHER", "reason": "现字幕与空删除均不成立"})
        assert "repair_class=spoken_unit" in prompt
        assert "小主你怎么被电了" in prompt
        return json.dumps({"choice": "PROPOSED", "reason": "第三候选匹配"})

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 2,
            "suspect": "小林你怎么被点了",
            "suggestion": "",
            "span_start_codepoint": 0,
            "span_end_codepoint": 8,
            "proposed_full_cue": "",
            "repair_class": "acoustic_drop_cue",
            "why": "初审误以为整条是幻听",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
    )

    assert "小主你怎么被电了" in output
    assert "小林你怎么被点了" not in output
    assert audit["repaired"] is True
    assert audit["request"]["repair_class"] == "spoken_unit"
    assert audit["request"]["proposed_cue"] == "小主你怎么被电了"
    assert audit["rebuilt_finding"]["repair_class"] == "spoken_unit"
    rebuild = audit["proposal_rebuild"]
    assert rebuild["status"] == "PROPOSED"
    assert rebuild["mutation_authorized"] is False
    assert rebuild["original_repair_class"] == "acoustic_drop_cue"
    assert rebuild["rebuilt_repair_class"] == "spoken_unit"
    assert rebuild["rejected_candidates"] == ["小林你怎么被点了", ""]
    assert len(witness_requests) == 1
    assert len(calls) == 3
    assert audit["verdict"]["request_sha256"] == witness_requests[0][
        "request_sha256"
    ]
    reuse = rebuild["witness_reuse"]
    assert reuse["basis"] == "IDENTICAL_AUDIO_GEOMETRY"
    assert reuse["original_witness_request_sha256"] == (
        "sha256:" + witness_requests[0]["request_sha256"]
    )
    assert reuse["reused_witness_request_sha256"] == (
        "sha256:" + witness_requests[0]["request_sha256"]
    )
    assert reuse["rebuilt_witness_request_sha256"] != (
        "sha256:" + witness_requests[0]["request_sha256"]
    )
    for key in (
        "cue_indexes",
        "matched_start_ms",
        "matched_end_ms",
        "context_start_ms",
        "context_end_ms",
        "source_media_timeline_offset_ms",
    ):
        assert audit["request"][key] == witness_requests[0][key]


def test_audible_acoustic_drop_current_choice_does_not_rebuild():
    source = _srt("咳咳")
    calls: list[str] = []

    def cpa(prompt):
        calls.append(prompt)
        if "# 字幕坏闭集重建" in prompt:
            raise AssertionError("CURRENT terminates the audible closed set")
        return json.dumps({"choice": "CURRENT"})

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "咳咳",
            "suggestion": "",
            "span_start_codepoint": 0,
            "span_end_codepoint": 2,
            "proposed_full_cue": "",
            "repair_class": "acoustic_drop_cue",
        },
        entity_verifier=lambda request: _witness(
            request, "ke ke", audible=True
        ),
        judge_llm_call=cpa,
    )

    assert output == source
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
    assert "proposal_rebuild" not in audit
    assert len(calls) == 1


def test_exact_source_transcript_is_candidate_blind_third_candidate_only():
    """806 regression: a same-window full transcript repairs a bad closed set.

    The audio provider sees geometry only.  Its text remains a proposed third
    candidate until a fresh CPA choice selects it.
    """

    from src.autoslice.exact_source_transcript_contract import (
        seal_exact_source_transcript_observation,
    )

    source = _srt("难听难听，对吧")
    calls: list[str] = []
    transcript_requests: list[dict] = []
    source_sha = "a" * 64
    audio_sha = "b" * 64

    def witness(request):
        return {
            **_witness(request, "yao jiu jiu tian dui ba"),
            "source_media_sha256": source_sha,
            "audio_clip_sha256": audio_sha,
            "audio_start_ms": 4_600,
            "audio_end_ms": 9_400,
            "timeline_binding": {
                "schema_version": "subtitle-audio-timeline-binding.v1",
                "source_media_timeline_offset_ms": 0,
                "delivery_local": {
                    "target_start_ms": 5_000,
                    "target_end_ms": 9_000,
                    "context_start_ms": 4_500,
                    "context_end_ms": 9_500,
                },
                "source_media": {
                    "target_start_ms": 5_000,
                    "target_end_ms": 9_000,
                    "crop_start_ms": 4_600,
                    "crop_end_ms": 9_400,
                },
            },
        }

    def exact_transcript(request):
        transcript_requests.append(dict(request))
        assert set(request) == {
            "schema_version",
            "kind",
            "cue_indexes",
            "matched_start_ms",
            "matched_end_ms",
            "context_start_ms",
            "context_end_ms",
            "source_media_timeline_offset_ms",
            "request_sha256",
        }
        forbidden = {
            "syllable_count_hint",
            "candidate_entities",
            "current_cue",
            "proposed_cue",
            "matched_audio_text",
            "context_before",
            "context_after",
            "suspect",
            "replacement",
            "candidate_id",
            "final_srt_sha256",
        }
        assert forbidden.isdisjoint(request)
        return seal_exact_source_transcript_observation(
            request=request,
            exact_transcript="nineteen nineteen，对吧",
            audible_language="mixed",
            source_media_sha256=source_sha,
            audio_clip_sha256=audio_sha,
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            response_sha256="c" * 64,
            timeline_binding={
                "schema_version": "subtitle-audio-timeline-binding.v1",
                "source_media_timeline_offset_ms": 0,
                "delivery_local": {
                    "target_start_ms": 5_000,
                    "target_end_ms": 9_000,
                    "context_start_ms": 4_500,
                    "context_end_ms": 9_500,
                },
                "source_media": {
                    "target_start_ms": 5_000,
                    "target_end_ms": 9_000,
                    "crop_start_ms": 4_600,
                    "crop_end_ms": 9_400,
                },
            },
        )

    def cpa(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        calls.append(prompt)
        if len(calls) == 1:
            return json.dumps({"choice": "NEITHER", "reason": "bad closed set"})
        assert "nineteen nineteen，对吧" in prompt
        return json.dumps({"choice": "PROPOSED", "reason": "exact transcript"})

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "难听难听",
            "suggestion": "南町nightin",
            "proposed_full_cue": "南町nightin，对吧",
            "repair_class": "phonetic",
            "why": "exact-final finding",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
        exact_source_transcript_provider=exact_transcript,
        clip_context={
            "schema_version": "clip-context.v1",
            "candidate_id": "auto_213135_806_1068",
            "context_sha256": "sha256:" + "d" * 64,
            "whole_clip_draft_srt_sha256": "sha256:" + "e" * 64,
            "pieces": [{"source_media_sha256": source_sha}],
        },
    )

    assert "nineteen nineteen，对吧" in output
    assert len(transcript_requests) == 1
    assert len(calls) == 2
    assert audit["repaired"] is True
    handoff = audit["exact_source_transcript_handoff"]
    assert handoff["status"] == "PROPOSED"
    assert handoff["decision_authority"] == "NONE"
    assert handoff["mutation_authorized"] is False
    assert handoff["candidate_id"] == "auto_213135_806_1068"
    assert handoff["final_srt_sha256"] == (
        "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    )
    assert audit["mutation_authority"]["status"] == "PASS"


def test_exact_source_transcript_reuses_retained_agy_parent_after_independent_candidate():
    """A candidate-blind transcript cannot replace the exact handoff parent."""

    from src.autoslice.exact_source_transcript_contract import (
        seal_exact_source_transcript_observation,
    )
    from src.autoslice.exact_source_transcript_authority import (
        valid_exact_source_transcript_adjudication_handoff,
    )

    source = _srt("难听难听，对吧")
    source_sha = "a" * 64
    audio_sha = "b" * 64
    timeline = {
        "schema_version": "subtitle-audio-timeline-binding.v1",
        "source_media_timeline_offset_ms": 0,
        "delivery_local": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "context_start_ms": 4_500,
            "context_end_ms": 9_500,
        },
        "source_media": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "crop_start_ms": 4_600,
            "crop_end_ms": 9_400,
        },
    }
    clip_context = {
        "schema_version": "clip-context.v1",
        "candidate_id": "auto_213135_806_1068",
        "context_sha256": "sha256:" + "d" * 64,
        "whole_clip_draft_srt_sha256": "sha256:" + "e" * 64,
        "pieces": [{"source_media_sha256": source_sha}],
    }
    initial_witness_requests: list[dict] = []
    independent_requests: list[dict] = []
    transcript_requests: list[dict] = []

    def witness(request):
        initial_witness_requests.append(dict(request))
        return {
            **_witness(request, "yao jiu jiu tian dui ba"),
            "witness_protocol": "blind_pinyin",
            "source_media_sha256": source_sha,
            "audio_clip_sha256": audio_sha,
            "audio_start_ms": 4_600,
            "audio_end_ms": 9_400,
            "timeline_binding": timeline,
        }

    def observe_independent(target):
        independent_requests.append(dict(target))
        return [{
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "witness_protocol": "candidate_blind_transcript",
            "request_sha256": target["request_sha256"],
            "status": "OBSERVED",
            "target_audible": True,
            "exact_transcript": "候选听写，对吧",
            "provider": "mai",
            "model": "mai-test",
            "source_media_sha256": source_sha,
            "audio_start_ms": 5_000,
            "audio_end_ms": 9_000,
            "candidate_exposure": "none",
            "authority": "EVIDENCE_ONLY",
            "mutation_authorized": False,
        }]

    witness.observe_independent_witnesses = observe_independent

    def exact_transcript(request):
        transcript_requests.append(dict(request))
        return seal_exact_source_transcript_observation(
            request=request,
            exact_transcript="nineteen nineteen，对吧",
            audible_language="mixed",
            source_media_sha256=source_sha,
            audio_clip_sha256=audio_sha,
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            response_sha256="c" * 64,
            timeline_binding=timeline,
        )

    cpa_judge_count = 0

    def cpa(prompt):
        nonlocal cpa_judge_count
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        if "# 字幕选字裁决" not in prompt:
            return json.dumps({"choice": "NEITHER"})
        cpa_judge_count += 1
        return json.dumps({
            "choice": "NEITHER" if cpa_judge_count < 3 else "PROPOSED",
            "reason": "third candidate" if cpa_judge_count >= 3 else "closed set",
        })

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "suspect": "难听难听",
            "suggestion": "南町nightin",
            "proposed_full_cue": "南町nightin，对吧",
            "repair_class": "phonetic",
        },
        entity_verifier=witness,
        judge_llm_call=cpa,
        exact_source_transcript_provider=exact_transcript,
        clip_context=clip_context,
    )

    assert "nineteen nineteen，对吧" in output
    assert len(independent_requests) == 1
    assert len(transcript_requests) == 1
    assert audit["repaired"] is True
    assert audit["independent_asr"]["observations"][0]["exact_transcript"] == (
        "候选听写，对吧"
    )
    handoff = audit["exact_source_transcript_handoff"]
    assert handoff["parent_witness_request_sha256"] == (
        "sha256:" + initial_witness_requests[0]["request_sha256"]
    )
    assert handoff["parent_witness_request_sha256"] == (
        "sha256:" + audit["independent_asr"]["prior_witness"]["request_sha256"]
    )
    assert audit["mutation_authority"]["status"] == "PASS"
    assert valid_exact_source_transcript_adjudication_handoff(
        audit,
        srt_text=source,
        candidate_provenance=audit["rebuilt_finding"]["candidate_provenance"],
        clip_context=clip_context,
    )


@pytest.mark.parametrize("drift", ("request", "audio", "geometry"))
def test_exact_source_transcript_parent_drift_stays_fail_closed(drift):
    """Parent reuse never bypasses the exact handoff binding checks."""

    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.exact_source_transcript_provider import (
        rebuild_candidate_from_exact_source_transcript,
    )
    from src.autoslice.final_review_auditor import (
        _derive_single_span_edit,
        _exact_source_transcript_parent_witness,
    )

    source, clip_context, original_witness, _rebuilt_request, handoff = (
        _exact_handoff_fixture()
    )
    finding = {
        "cue_index": 1,
        "suspect": "难听难听",
        "suggestion": "南町nightin",
        "proposed_full_cue": "南町nightin，对吧",
        "repair_class": "phonetic",
    }
    initial_request = build_context_adjudication_request(
        source, finding, clip_context=clip_context
    )
    witness_request = build_witness_request(initial_request)

    prior = deepcopy(original_witness)
    candidate = deepcopy(original_witness)
    candidate["witness_protocol"] = "candidate_blind_transcript"
    candidate.pop("heard_pinyin")
    candidate.pop("uncertain_positions")
    candidate.pop("syllable_count")
    candidate.pop("confidence")
    candidate["exact_transcript"] = "candidate transcript"
    if drift == "request":
        prior["request_sha256"] = "f" * 64
    elif drift == "audio":
        prior["audio_clip_sha256"] = "f" * 64
    else:
        prior["timeline_binding"]["source_media"]["crop_start_ms"] += 1

    selected = _exact_source_transcript_parent_witness(
        verdict=candidate,
        independent_asr_audit={"prior_witness": prior},
        witness_request=witness_request,
    )
    if drift == "request":
        assert selected is candidate
    else:
        assert selected is prior

    rebuilt, receipt = rebuild_candidate_from_exact_source_transcript(
        finding=finding,
        initial_check_request=initial_request,
        witness_request=witness_request,
        witness=selected,
        srt_text=source,
        clip_context=clip_context,
        provider=lambda _request: handoff["provider_observation"],
        derive_single_span_edit=_derive_single_span_edit,
    )
    assert rebuilt is None
    assert receipt["status"] == "INVALID"


def test_exact_source_transcript_uses_real_806_geometry_and_source_identity():
    from src.autoslice.exact_source_transcript_contract import (
        seal_exact_source_transcript_observation,
    )

    source = (
        "1\n00:00:00,250 --> 00:00:03,570\n"
        "其实我真的非常非常羡慕，嗯\n\n"
        "2\n00:00:04,010 --> 00:00:06,529\n"
        "就是在公司的大家\n\n"
        "3\n00:00:06,529 --> 00:00:12,040\n"
        "就礼墨拉布里二叔、七宝\n\n"
        "4\n00:00:12,040 --> 00:00:13,920\n"
        "难听难听，对吧\n\n"
        "5\n00:00:13,960 --> 00:00:15,840\n"
        "很，很羡慕\n"
    )
    source_sha = (
        "7a818c1c5a2775eeb866a2a15b4413e65daa209f635da561e34d4ae5711f2442"
    )
    audio_sha = (
        "8a7e16e8dd115b98fbe61d4815a60c40a55cf8b873d0926405ce8b513d157a65"
    )
    timeline = {
        "schema_version": "subtitle-audio-timeline-binding.v1",
        "source_media_timeline_offset_ms": 9_780,
        "delivery_local": {
            "target_start_ms": 12_040,
            "target_end_ms": 13_920,
            "context_start_ms": 6_029,
            "context_end_ms": 16_340,
        },
        "source_media": {
            "target_start_ms": 21_820,
            "target_end_ms": 23_700,
            "crop_start_ms": 21_400,
            "crop_end_ms": 24_100,
        },
    }
    physical_requests = []

    def witness(request):
        assert request["cue_indexes"] == [4]
        assert request["matched_start_ms"] == 12_040
        assert request["matched_end_ms"] == 13_920
        assert request["source_media_timeline_offset_ms"] == 9_780
        return {
            **_witness(request, "yi jiu jiu ling nian dui ba"),
            "witness_protocol": "blind_pinyin",
            "source_media_sha256": source_sha,
            "audio_clip_sha256": audio_sha,
            "audio_start_ms": 21_400,
            "audio_end_ms": 24_100,
            "timeline_binding": timeline,
        }

    def exact_transcript(request):
        physical_requests.append(dict(request))
        assert request["matched_start_ms"] == 12_040
        assert request["matched_end_ms"] == 13_920
        assert request["source_media_timeline_offset_ms"] == 9_780
        assert "syllable_count_hint" not in request
        return seal_exact_source_transcript_observation(
            request=request,
            exact_transcript="1919，对吧",
            audible_language="mixed",
            source_media_sha256=source_sha,
            audio_clip_sha256=audio_sha,
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            response_sha256="c" * 64,
            timeline_binding=timeline,
        )

    choices = iter(("NEITHER", "PROPOSED"))
    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 4,
            "suspect": "难听难听",
            "suggestion": "南町nightin",
            "proposed_full_cue": "南町nightin，对吧",
            "repair_class": "phonetic",
        },
        entity_verifier=witness,
        judge_llm_call=lambda _prompt: json.dumps(
            ({"choice": "NEITHER", "needs_audio": True} if "TEXT_FIRST" in _prompt else {"choice": next(choices)})
        ),
        exact_source_transcript_provider=exact_transcript,
        source_media_timeline_offset_ms=9_780,
        clip_context={
            "schema_version": "clip-context.v1",
            "candidate_id": "auto_213135_806_1068",
            "context_sha256": (
                "sha256:5d116d54cab6d27d4729e60ddc5276a28c79c0442467a306bfb61b1539334dc1"
            ),
            "whole_clip_draft_srt_sha256": (
                "sha256:7c079c76db402173af936f68f62b0ada87625bdea6a48705f4e882ff46c59c39"
            ),
            "pieces": [
                {
                    "source_media_sha256": (
                        "sha256:c05d0de47bfb643ceafa40a58bc928611c9773c9421d71374c847b47ee8789a1"
                    )
                }
            ],
        },
    )

    assert len(physical_requests) == 1
    assert "1919，对吧" in output
    handoff = audit["exact_source_transcript_handoff"]
    assert handoff["source_media_sha256"] == "sha256:" + source_sha
    assert handoff["timeline_binding"] == timeline
    assert handoff["source_media_sha256"] != (
        "sha256:c05d0de47bfb643ceafa40a58bc928611c9773c9421d71374c847b47ee8789a1"
    )

    broad = json.loads(json.dumps(timeline))
    broad["source_media"]["crop_start_ms"] = 17_680
    broad["source_media"]["crop_end_ms"] = 23_680
    with pytest.raises(
        ValueError,
        match="EXACT_SOURCE_TRANSCRIPT_OBSERVATION_INVALID",
    ):
        seal_exact_source_transcript_observation(
            request=physical_requests[0],
            exact_transcript="1919，对吧",
            audible_language="mixed",
            source_media_sha256=source_sha,
            audio_clip_sha256=audio_sha,
            provider="agy",
            model="Gemini 3.6 Flash (High)",
            response_sha256="c" * 64,
            timeline_binding=broad,
        )


def _exact_handoff_fixture():
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.exact_source_transcript_contract import (
        build_exact_source_transcript_request,
        seal_exact_source_transcript_observation,
    )
    from src.autoslice.exact_source_transcript_provider import (
        rebuild_candidate_from_exact_source_transcript,
    )
    from src.autoslice.final_review_auditor import _derive_single_span_edit

    source = _srt("难听难听，对吧")
    clip_context = {
        "schema_version": "clip-context.v1",
        "candidate_id": "auto_213135_806_1068",
        "context_sha256": "sha256:" + "d" * 64,
        "whole_clip_draft_srt_sha256": "sha256:" + "e" * 64,
    }
    finding = {
        "cue_index": 1,
        "suspect": "难听难听",
        "suggestion": "南町nightin",
        "proposed_full_cue": "南町nightin，对吧",
        "repair_class": "phonetic",
    }
    initial = build_context_adjudication_request(
        source, finding, clip_context=clip_context
    )
    witness_request = build_witness_request(initial)
    timeline = {
        "schema_version": "subtitle-audio-timeline-binding.v1",
        "source_media_timeline_offset_ms": 0,
        "delivery_local": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "context_start_ms": 4_500,
            "context_end_ms": 9_500,
        },
        "source_media": {
            "target_start_ms": 5_000,
            "target_end_ms": 9_000,
            "crop_start_ms": 4_600,
            "crop_end_ms": 9_400,
        },
    }
    witness = {
        **_witness(witness_request, "yao jiu jiu tian dui ba"),
        "witness_protocol": "blind_pinyin",
        "source_media_sha256": "a" * 64,
        "audio_clip_sha256": "b" * 64,
        "audio_start_ms": 4_600,
        "audio_end_ms": 9_400,
        "timeline_binding": timeline,
    }
    physical = build_exact_source_transcript_request(witness_request)
    observation = seal_exact_source_transcript_observation(
        request=physical,
        exact_transcript="nineteen nineteen，对吧",
        audible_language="mixed",
        source_media_sha256="a" * 64,
        audio_clip_sha256="b" * 64,
        provider="agy",
        model="Gemini 3.6 Flash (High)",
        response_sha256="c" * 64,
        timeline_binding=timeline,
    )
    rebuilt, handoff = rebuild_candidate_from_exact_source_transcript(
        finding=finding,
        initial_check_request=initial,
        witness_request=witness_request,
        witness=witness,
        srt_text=source,
        clip_context=clip_context,
        provider=lambda _request: observation,
        derive_single_span_edit=_derive_single_span_edit,
    )
    assert rebuilt is not None
    rebuilt_request = build_context_adjudication_request(
        source, rebuilt, clip_context=clip_context
    )
    return source, clip_context, witness, rebuilt_request, handoff


def _reseal_exact_handoff(handoff):
    from src.autoslice.exact_source_transcript_contract import _canonical_sha256

    handoff.pop("receipt_sha256", None)
    handoff["receipt_sha256"] = "sha256:" + _canonical_sha256(handoff)
    return handoff


def test_prior_exact_transcript_survives_new_finding_without_authority_rebinding():
    from src.autoslice.exact_source_transcript_contract import (
        exact_source_transcript_context_candidates,
    )
    from src.autoslice.producer_text_pipeline import (
        _persisted_exact_source_transcript_handoffs,
    )

    source, context, witness, _rebuilt, handoff = _exact_handoff_fixture()
    original = deepcopy(handoff)
    request = build_context_adjudication_request(source, {
        "cue_index": 1, "suspect": "难听难听", "suggestion": "难听",
        "proposed_full_cue": "难听，对吧", "repair_class": "phonetic",
    }, clip_context=context)
    # Same audio geometry; different reviewer proposal and request identity.
    assert request["request_sha256"] != handoff["input_request"]["request_sha256"]
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    witness = {**witness, **_witness(build_witness_request(request), "yao jiu jiu tian dui ba")}
    rows = exact_source_transcript_context_candidates(
        [handoff, handoff], check_request=request, witness=witness,
        clip_context=context,
    )
    assert len(rows) == 1
    assert rows[0]["text"] == "nineteen nineteen，对吧"
    assert rows[0]["source"]["mutation_authorized"] is False
    assert handoff == original
    saved = {"final_review_audit": {
        "findings": [{"exact_release_adjudication": {
            "exact_source_transcript_handoff": handoff,
        }}],
        "prior_exact_source_transcript_handoffs": [handoff],
    }}
    assert _persisted_exact_source_transcript_handoffs(saved) == (handoff,)


@pytest.mark.parametrize("drift", ["source", "crop", "window", "candidate", "receipt", "context_missing"])
def test_prior_exact_transcript_context_rejects_binding_drift(drift):
    from src.autoslice.exact_source_transcript_contract import (
        exact_source_transcript_context_candidates,
    )
    source, context, witness, request, handoff = _exact_handoff_fixture()
    witness, request, context, handoff = map(deepcopy, (witness, request, context, handoff))
    if drift == "source":
        witness["source_media_sha256"] = "f" * 64
    elif drift == "crop":
        witness["audio_clip_sha256"] = "f" * 64
    elif drift == "window":
        request["matched_end_ms"] += 1
    elif drift == "candidate":
        context["candidate_id"] = "another-candidate"
    elif drift == "context_missing":
        context = None
    else:
        handoff["proposed_cue"] = "invented transcript"
    assert exact_source_transcript_context_candidates(
        [handoff], check_request=request, witness=witness, clip_context=context,
    ) == []


def test_ordinary_cpa_judge_receives_prior_target_transcript_before_relistening():
    source, context, original_witness, _rebuilt, handoff = _exact_handoff_fixture()
    finding = dict(handoff["input_request"])
    finding.update(cue_index=1, suggestion="南町nightin",
                   proposed_full_cue="南町nightin，对吧",
                   _prior_exact_source_transcript_handoffs=[handoff])
    prompts = []

    def verify(request):
        return {**original_witness, **_witness(request, "yao jiu jiu tian dui ba")}

    def judge(prompt):
        if "TEXT_FIRST" in prompt:
            return json.dumps({"choice": "NEITHER", "needs_audio": True})
        prompts.append(prompt)
        assert "prior_exact_target_transcripts_evidence_only" in prompt
        assert "nineteen nineteen，对吧" in prompt
        return json.dumps({"choice": "CURRENT", "needs_audio": False,
                           "current_utterance_supported": True,
                           "current_utterance_support_reason": "all bound evidence compared"})

    output, audit = adjudicate_context_finding(
        source, finding, clip_context=context, entity_verifier=verify,
        judge_llm_call=judge, require_complete_utterance_support=True,
    )
    assert output == source
    assert len(prompts) == 1
    assert audit["request"]["prior_exact_source_transcript_candidates"][0]["text"] == handoff["proposed_cue"]
    assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"


def test_bad_closed_set_rebuild_preserves_existing_target_transcripts():
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.independent_context_adjudication import bind_request
    from src.autoslice.exact_source_transcript_provider import (
        exact_source_transcript_candidate_provenance,
    )
    source, context, witness, _rebuilt_request, handoff = _exact_handoff_fixture()
    initial = handoff["input_request"]
    candidates = [{"text": "甲听写", "source": {"provider": "mai"}},
                  {"text": "乙听写", "source": {"provider": "moss"}}]
    evidence_request = bind_request(initial, proposed_candidates=candidates)
    rebuilt_finding = {"cue_index": 1, "suspect": "难听难听",
                       "suggestion": "nineteen nineteen",
                       "span_start_codepoint": 0, "span_end_codepoint": 4,
                       "proposed_full_cue": handoff["proposed_cue"],
                       "repair_class": "spoken_unit",
                       "candidate_provenance": exact_source_transcript_candidate_provenance(handoff),
                       "_exact_source_transcript_handoff": handoff}
    request = auditor._prepare_rebuilt_candidate_request(
        srt_text=source, rebuilt_finding=rebuilt_finding, proposal_rebuild_audit={},
        clip_context=context, source_media_timeline_offset_ms=0,
        witness_request=build_witness_request(initial), witness=witness,
        candidate_evidence_request=evidence_request,
    )
    assert request is not None
    assert request["proposed_candidates"] == candidates
    assert request["proposed_cue"] == handoff["proposed_cue"]


def test_new_context_proposal_retains_old_transcript_without_its_candidate_authority():
    from src.autoslice.acoustic_witness_adjudication import build_witness_request
    from src.autoslice.exact_source_transcript_provider import (
        exact_source_transcript_candidate_provenance,
    )
    from src.autoslice.missing_proposal_bootstrap import bootstrap_missing_proposal

    source, context, witness, _old_request, handoff = _exact_handoff_fixture()
    original = deepcopy(handoff)
    finding = {
        "cue_index": 1, "suspect": "难听难听",
        "suggestion": "nineteen nineteen", "span_start_codepoint": 0,
        "span_end_codepoint": 4, "proposed_full_cue": handoff["proposed_cue"],
        "repair_class": "spoken_unit",
        "candidate_provenance": exact_source_transcript_candidate_provenance(handoff),
        "_exact_source_transcript_handoff": handoff,
    }
    rebuilt, proposal = bootstrap_missing_proposal(
        srt_text=source, finding=finding, structured_chat="",
        llm_call=lambda _: json.dumps({
            "status": "PROPOSED", "proposed_cue": "一九一九，对吧",
            "reason": "independent range evidence proposes another form",
        }, ensure_ascii=False),
        derive_single_span_edit=auditor._derive_single_span_edit,
    )
    assert rebuilt is not None
    assert "_exact_source_transcript_handoff" not in rebuilt
    assert rebuilt["_prior_exact_source_transcript_handoffs"] == [original]
    request = auditor._prepare_rebuilt_candidate_request(
        srt_text=source, rebuilt_finding=rebuilt, proposal_rebuild_audit=proposal,
        clip_context=context, source_media_timeline_offset_ms=0,
        witness_request=build_witness_request(handoff["input_request"]), witness=witness,
        candidate_evidence_request=handoff["input_request"],
    )
    assert request is not None
    assert request["proposed_cue"] == "一九一九，对吧"
    assert "exact_source_transcript_handoff" not in request
    prior = request["prior_exact_source_transcript_candidates"]
    assert prior[0]["text"] == original["proposed_cue"]
    assert prior[0]["source"]["mutation_authorized"] is False
    assert handoff == original
    assert proposal["mutation_authorized"] is False


@pytest.mark.parametrize(
    "drift",
    (
        "current", "rejected", "final_srt", "candidate", "run", "v1",
        "receipt_missing", "observation_missing",
    ),
)
def test_exact_source_handoff_rejects_resealed_stale_or_unbound_receipts(drift):
    import copy

    from src.autoslice.exact_source_transcript_contract import (
        _canonical_sha256,
        _text_sha256,
        valid_exact_source_transcript_handoff,
    )

    source, clip_context, witness, rebuilt_request, original = _exact_handoff_fixture()
    handoff = copy.deepcopy(original)
    if drift in {"current", "rejected"}:
        field = "current_cue" if drift == "current" else "proposed_cue"
        handoff["input_request"][field] = "fully resealed stale input"
        unsigned = {
            key: value
            for key, value in handoff["input_request"].items()
            if key != "request_sha256"
        }
        request_sha = _canonical_sha256(unsigned)
        handoff["input_request"]["request_sha256"] = request_sha
        handoff["input_request_sha256"] = "sha256:" + request_sha
        hash_field = (
            "current_cue_sha256" if drift == "current" else "rejected_proposed_sha256"
        )
        handoff[hash_field] = "sha256:" + _text_sha256(
            handoff["input_request"][field]
        )
        run_payload = {
            "candidate_id": handoff["candidate_id"],
            "clip_context_sha256": handoff["clip_context_sha256"].removeprefix("sha256:"),
            "whole_clip_draft_srt_sha256": handoff[
                "whole_clip_draft_srt_sha256"
            ].removeprefix("sha256:"),
            "input_request_sha256": request_sha,
            "parent_witness_request_sha256": handoff[
                "parent_witness_request_sha256"
            ].removeprefix("sha256:"),
            "final_srt_sha256": handoff["final_srt_sha256"].removeprefix("sha256:"),
        }
        handoff["run_binding_sha256"] = "sha256:" + _canonical_sha256(run_payload)
    elif drift == "final_srt":
        stale_sha = _text_sha256(source + "stale")
        handoff["input_srt_sha256"] = "sha256:" + stale_sha
        handoff["final_srt_sha256"] = "sha256:" + stale_sha
        run_payload = {
            "candidate_id": handoff["candidate_id"],
            "clip_context_sha256": handoff["clip_context_sha256"].removeprefix("sha256:"),
            "whole_clip_draft_srt_sha256": handoff[
                "whole_clip_draft_srt_sha256"
            ].removeprefix("sha256:"),
            "input_request_sha256": handoff["input_request_sha256"].removeprefix("sha256:"),
            "parent_witness_request_sha256": handoff[
                "parent_witness_request_sha256"
            ].removeprefix("sha256:"),
            "final_srt_sha256": stale_sha,
        }
        handoff["run_binding_sha256"] = "sha256:" + _canonical_sha256(run_payload)
    elif drift == "candidate":
        handoff["candidate_id"] = "stale_candidate"
        handoff["run_binding_sha256"] = "sha256:" + _canonical_sha256(
            {
                "candidate_id": handoff["candidate_id"],
                "clip_context_sha256": handoff["clip_context_sha256"].removeprefix("sha256:"),
                "whole_clip_draft_srt_sha256": handoff[
                    "whole_clip_draft_srt_sha256"
                ].removeprefix("sha256:"),
                "input_request_sha256": handoff["input_request_sha256"].removeprefix("sha256:"),
                "parent_witness_request_sha256": handoff[
                    "parent_witness_request_sha256"
                ].removeprefix("sha256:"),
                "final_srt_sha256": handoff["final_srt_sha256"].removeprefix("sha256:"),
            }
        )
    elif drift == "run":
        handoff["run_binding_sha256"] = "sha256:" + "f" * 64
    elif drift == "v1":
        handoff["schema_version"] = "exact-final-source-transcript-handoff.v1"
    elif drift == "receipt_missing":
        handoff.pop("receipt_sha256")
        assert not valid_exact_source_transcript_handoff(
            handoff,
            srt_text=source,
            check_request=rebuilt_request,
            witness=witness,
            clip_context=clip_context,
        )
        return
    else:
        handoff.pop("provider_observation")
    _reseal_exact_handoff(handoff)

    assert not valid_exact_source_transcript_handoff(
        handoff,
        srt_text=source,
        check_request=rebuilt_request,
        witness=witness,
        clip_context=clip_context,
    )


def test_witness_conflict_diagnostic_validates_exact_source_handoff_without_veto():
    import copy

    from src.autoslice.acoustic_witness_adjudication import adjudicate_with_witness

    _source, clip_context, witness, request, handoff = _exact_handoff_fixture()
    repaired, branch, audit = adjudicate_with_witness(
        check_request=request,
        witness=witness,
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
        clip_context=clip_context,
    )
    assert repaired is True
    assert branch == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
    assert audit["witness_conflict_diagnostic"]["exact_source_transcript_handoff"] is True
    assert audit["witness_conflict_diagnostic"]["effect"] == "DISCLOSURE_ONLY"
    assert "witness_conflict_gate" not in audit

    invalid_request = copy.deepcopy(request)
    invalid_handoff = copy.deepcopy(handoff)
    invalid_handoff.pop("receipt_sha256")
    invalid_request["exact_source_transcript_handoff"] = invalid_handoff
    repaired, branch, audit = adjudicate_with_witness(
        check_request=invalid_request,
        witness=witness,
        llm_call=lambda _prompt: json.dumps({"choice": "PROPOSED"}),
        clip_context=clip_context,
    )
    assert repaired is True
    assert branch == "CPA_JUDGE_APPLY_PROPOSED_OVER_WITNESS_CONFLICT"
    assert audit["witness_conflict_diagnostic"]["exact_source_transcript_handoff"] is False
    assert audit["witness_conflict_diagnostic"]["effect"] == "DISCLOSURE_ONLY"
    assert "witness_conflict_gate" not in audit


@pytest.mark.parametrize(
    "drift",
    (
        "arbitrary_request",
        "uncertain",
        "inaudible",
        "legacy_protocol",
        "candidate_injection",
        "unsealed_check_request",
    ),
)
def test_exact_source_handoff_reentry_requires_canonical_fresh_witness(drift):
    import copy

    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
    )
    from src.autoslice.exact_source_transcript_contract import (
        valid_exact_source_transcript_handoff,
    )

    source, clip_context, witness, rebuilt_request, handoff = (
        _exact_handoff_fixture()
    )
    reentry_request = build_witness_request(rebuilt_request)
    assert reentry_request["request_sha256"] != handoff[
        "parent_witness_request_sha256"
    ].removeprefix("sha256:")
    fresh_witness = copy.deepcopy(witness)
    fresh_witness["request_sha256"] = reentry_request["request_sha256"]
    assert valid_exact_source_transcript_handoff(
        handoff,
        srt_text=source,
        check_request=rebuilt_request,
        witness=fresh_witness,
        clip_context=clip_context,
    )

    if drift == "arbitrary_request":
        fresh_witness["request_sha256"] = "f" * 64
    elif drift == "uncertain":
        fresh_witness["status"] = "UNCERTAIN"
    elif drift == "inaudible":
        fresh_witness["target_audible"] = False
    elif drift == "legacy_protocol":
        fresh_witness.pop("witness_protocol")
    elif drift == "candidate_injection":
        fresh_witness["proposed_cue"] = "provider-priming candidate"
    else:
        rebuilt_request["request_sha256"] = "f" * 64

    assert not valid_exact_source_transcript_handoff(
        handoff,
        srt_text=source,
        check_request=rebuilt_request,
        witness=fresh_witness,
        clip_context=clip_context,
    )


@pytest.mark.parametrize(
    "drift",
    ("bad_protocol", "bad_request", "bad_status"),
)
def test_exact_source_handoff_rejects_resealed_bad_parent_witness(drift):
    import copy

    from src.autoslice.exact_source_transcript_contract import (
        _canonical_sha256,
        build_exact_source_transcript_handoff,
        valid_exact_source_transcript_handoff,
    )

    source, clip_context, original_witness, _request, original = (
        _exact_handoff_fixture()
    )
    handoff = copy.deepcopy(original)
    witness_request = handoff["parent_witness_request"]
    witness = copy.deepcopy(original_witness)
    if drift == "bad_protocol":
        witness["witness_protocol"] = "legacy_sighted"
    elif drift == "bad_request":
        witness_request["candidate_entities"] = ["priming candidate"]
        witness["candidate_entities"] = ["priming candidate"]
    else:
        witness["status"] = "UNCERTAIN"
    witness_request.pop("request_sha256")
    witness_request_sha256 = _canonical_sha256(witness_request)
    witness_request["request_sha256"] = witness_request_sha256
    witness["request_sha256"] = witness_request_sha256
    handoff["parent_witness_request_sha256"] = (
        "sha256:" + witness_request_sha256
    )
    handoff["parent_witness_observation_sha256"] = (
        "sha256:" + _canonical_sha256(witness)
    )
    run_payload = {
        "candidate_id": handoff["candidate_id"],
        "clip_context_sha256": handoff["clip_context_sha256"].removeprefix(
            "sha256:"
        ),
        "whole_clip_draft_srt_sha256": handoff[
            "whole_clip_draft_srt_sha256"
        ].removeprefix("sha256:"),
        "input_request_sha256": handoff["input_request_sha256"].removeprefix(
            "sha256:"
        ),
        "parent_witness_request_sha256": witness_request_sha256,
        "final_srt_sha256": handoff["final_srt_sha256"].removeprefix(
            "sha256:"
        ),
    }
    handoff["run_binding_sha256"] = (
        "sha256:" + _canonical_sha256(run_payload)
    )
    _reseal_exact_handoff(handoff)

    assert not valid_exact_source_transcript_handoff(
        handoff,
        srt_text=source,
        witness=witness,
        clip_context=clip_context,
    )
    with pytest.raises(
        ValueError,
        match="EXACT_SOURCE_TRANSCRIPT_HANDOFF_BINDING_INVALID",
    ):
        build_exact_source_transcript_handoff(
            observation=handoff["provider_observation"],
            physical_request=handoff["physical_request"],
            initial_check_request=handoff["input_request"],
            witness_request=witness_request,
            witness=witness,
            srt_text=source,
            clip_context=clip_context,
        )


def test_exact_provider_failure_receipt_requires_real_typed_attempt():
    import copy

    from src.autoslice.exact_source_transcript_contract import _canonical_sha256
    from src.autoslice.exact_source_transcript_provider import (
        seal_exact_source_transcript_provider_failure,
        valid_exact_source_transcript_provider_failure,
    )

    _source, _context, _witness_row, _request, handoff = _exact_handoff_fixture()
    physical = handoff["physical_request"]
    receipt = seal_exact_source_transcript_provider_failure(
        request=physical,
        provider_failures=[
            {"provider": "agy", "category": "AGY_TIMEOUT", "attempted": True}
        ],
    )
    assert valid_exact_source_transcript_provider_failure(receipt, request=physical)
    assert not valid_exact_source_transcript_provider_failure({}, request=physical)

    not_attempted = copy.deepcopy(receipt)
    not_attempted["provider_failures"][0]["attempted"] = False
    not_attempted.pop("failure_receipt_sha256")
    not_attempted["failure_receipt_sha256"] = _canonical_sha256(not_attempted)
    assert not valid_exact_source_transcript_provider_failure(
        not_attempted, request=physical
    )
    with pytest.raises(ValueError):
        seal_exact_source_transcript_provider_failure(
            request=physical,
            provider_failures=[
                {
                    "provider": "agy",
                    "category": "AGY_INVALID_OUTPUT",
                    "attempted": True,
                }
            ],
        )


@pytest.mark.parametrize("http_status", (400, 401, 403, 404, 422))
def test_exact_provider_failure_rejects_deterministic_gemini_4xx(http_status):
    from src.autoslice.exact_source_transcript_provider import (
        seal_exact_source_transcript_provider_failure,
    )

    _source, _context, _witness_row, _request, handoff = _exact_handoff_fixture()
    with pytest.raises(ValueError):
        seal_exact_source_transcript_provider_failure(
            request=handoff["physical_request"],
            provider_failures=[
                {
                    "provider": "gemini_api",
                    "category": "GEMINI_API_REQUEST_FAILED",
                    "attempted": True,
                    "http_status": http_status,
                }
            ],
        )


@pytest.mark.parametrize("error_type", ("ValueError", "RuntimeError"))
def test_exact_provider_failure_rejects_nontransport_without_http_status(error_type):
    from src.autoslice.exact_source_transcript_provider import (
        seal_exact_source_transcript_provider_failure,
    )

    _source, _context, _witness_row, _request, handoff = _exact_handoff_fixture()
    with pytest.raises(ValueError):
        seal_exact_source_transcript_provider_failure(
            request=handoff["physical_request"],
            provider_failures=[
                {
                    "provider": "gemini_api",
                    "category": "GEMINI_API_REQUEST_FAILED",
                    "attempted": True,
                    "error_type": error_type,
                }
            ],
        )


def test_exact_provider_failure_accepts_no_status_url_transport_error():
    from src.autoslice.exact_source_transcript_provider import (
        seal_exact_source_transcript_provider_failure,
    )

    _source, _context, _witness_row, _request, handoff = _exact_handoff_fixture()
    receipt = seal_exact_source_transcript_provider_failure(
        request=handoff["physical_request"],
        provider_failures=[
            {
                "provider": "gemini_api",
                "category": "GEMINI_API_REQUEST_FAILED",
                "attempted": True,
                "error_type": "URLError",
            }
        ],
    )
    assert receipt["retry_class"] == "provider_transient"


def test_exact_provider_failure_infra_marker_rejects_same_geometry_stale_check():
    import copy

    from src.autoslice.exact_source_transcript_contract import _canonical_sha256
    from src.autoslice.exact_source_transcript_provider import (
        rebuild_candidate_from_exact_source_transcript,
        seal_exact_source_transcript_provider_failure,
    )
    from src.autoslice.final_review_auditor import (
        exact_source_transcript_infra_reason_codes,
    )

    source, clip_context, witness, _rebuilt_request, proposed_handoff = (
        _exact_handoff_fixture()
    )
    initial_request = proposed_handoff["input_request"]
    witness_request = proposed_handoff["parent_witness_request"]
    physical_request = proposed_handoff["physical_request"]
    failure = seal_exact_source_transcript_provider_failure(
        request=physical_request,
        provider_failures=[
            {"provider": "agy", "category": "AGY_TIMEOUT", "attempted": True}
        ],
    )
    finding = {
        "cue_index": 1,
        "suspect": initial_request["suspect"],
        "suggestion": initial_request["replacement"],
        "proposed_full_cue": initial_request["proposed_cue"],
        "base_text_sha256": initial_request["base_text_sha256"],
        "repair_class": initial_request["repair_class"],
    }
    rebuilt, unavailable = rebuild_candidate_from_exact_source_transcript(
        finding=finding,
        initial_check_request=initial_request,
        witness_request=witness_request,
        witness=witness,
        srt_text=source,
        clip_context=clip_context,
        provider=lambda _request: failure,
        derive_single_span_edit=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failure path must not derive a candidate")
        ),
    )
    assert rebuilt is None
    row = {
        **finding,
        "exact_release_adjudication": {
            "request": initial_request,
            "verdict": witness,
            "exact_source_transcript_handoff": unavailable,
        },
    }
    marker = ["FINAL_REVIEW_ADJUDICATION_INFRA_UNRESOLVED"]
    assert exact_source_transcript_infra_reason_codes(
        [row], srt_text=source, clip_context=clip_context
    ) == marker

    stale = copy.deepcopy(row)
    stale_request = stale["exact_release_adjudication"]["request"]
    stale_request["replacement"] = "foreign same-geometry candidate"
    stale_request["proposed_cue"] = "foreign same-geometry candidate，对吧"
    stale_request["request_sha256"] = _canonical_sha256(
        {key: value for key, value in stale_request.items() if key != "request_sha256"}
    )
    assert exact_source_transcript_infra_reason_codes(
        [stale], srt_text=source, clip_context=clip_context
    ) == []

    stale = copy.deepcopy(row)
    stale["proposed_full_cue"] = "foreign same-geometry candidate，对吧"
    assert exact_source_transcript_infra_reason_codes(
        [stale], srt_text=source, clip_context=clip_context
    ) == []
    assert exact_source_transcript_infra_reason_codes(
        [row], srt_text=source + "\nstale SRT\n", clip_context=clip_context
    ) == []
    stale_context = {
        **clip_context,
        "context_sha256": "sha256:" + "f" * 64,
    }
    assert exact_source_transcript_infra_reason_codes(
        [row], srt_text=source, clip_context=stale_context
    ) == []
    stale = copy.deepcopy(row)
    stale["exact_release_adjudication"]["verdict"]["heard_pinyin"] = (
        "foreign same-geometry witness"
    )
    assert exact_source_transcript_infra_reason_codes(
        [stale], srt_text=source, clip_context=clip_context
    ) == []


def test_exact_source_handoff_rejects_broad_foreign_geometry_even_if_resealed():
    import copy

    from src.autoslice.exact_source_transcript_contract import (
        _canonical_sha256,
        exact_source_transcript_prompt,
        valid_exact_source_transcript_handoff,
    )

    source, clip_context, witness, rebuilt_request, original = _exact_handoff_fixture()
    handoff = copy.deepcopy(original)
    observation = handoff["provider_observation"]
    broad = copy.deepcopy(observation["timeline_binding"])
    broad["source_media"]["crop_start_ms"] = 1_000
    broad["source_media"]["crop_end_ms"] = 13_000
    observation["timeline_binding"] = broad
    observation["audio_start_ms"] = 1_000
    observation["audio_end_ms"] = 13_000
    observation["prompt_sha256"] = hashlib.sha256(
        exact_source_transcript_prompt(
            request=handoff["physical_request"], timeline_binding=broad
        ).encode()
    ).hexdigest()
    observation.pop("observation_sha256")
    observation["observation_sha256"] = _canonical_sha256(observation)
    handoff["timeline_binding"] = broad
    handoff["prompt_sha256"] = "sha256:" + observation["prompt_sha256"]
    handoff["provider_observation_sha256"] = (
        "sha256:" + observation["observation_sha256"]
    )
    _reseal_exact_handoff(handoff)

    assert not valid_exact_source_transcript_handoff(
        handoff,
        srt_text=source,
        check_request=rebuilt_request,
        witness=witness,
        clip_context=clip_context,
    )


@pytest.mark.parametrize(
    ("current", "rejected", "rebuilt"),
    (("原句", "", ""), ("原句", "", "原句"), ("原句", "旧提案", "旧提案")),
)
def test_proposal_rebuild_rejects_empty_current_and_rejected_candidates(
    current, rejected, rebuilt
):
    from src.autoslice.final_review_auditor import (
        _rebuild_candidate_after_neither,
    )

    finding = {
        "cue_index": 1,
        "repair_class": (
            "acoustic_drop_cue" if rejected == "" else "phonetic"
        ),
    }
    candidate, audit = _rebuild_candidate_after_neither(
        finding=finding,
        asr_attempts={"routes_exhausted": True, "observations": [{"status": "UNCERTAIN"}]},
        request={
            "current_cue": current,
            "proposed_cue": rejected,
            "context_before": "",
            "context_after": "",
        },
        witness={
            "schema_version": "subtitle-span-acoustic-witness.v1",
            "status": "OBSERVED",
            "target_audible": True,
            "heard_pinyin": "yuan ju",
            "syllable_count": 2,
            "uncertain_positions": [],
            "confidence": 0.9,
        },
        llm_call=lambda _prompt: json.dumps(
            {
                "status": "PROPOSED",
                "candidate_failures": "两路目标不确定",
                "evidence_basis": "原音音素",
                "residual_uncertainty": "候选仍须判",
                "proposed_cue": rebuilt,
                "reason": "test invalid candidate",
            },
            ensure_ascii=False,
        ),
    )

    assert candidate is None
    assert audit["status"] == "INVALID"
    assert audit["reason_code"] == "PROPOSAL_REBUILD_TEXT_CONTRACT_INVALID"


def test_glossary_near_homophone_uses_expected_value_canon():
    """未登记误听面→glossary 规范词走高收益零 CPA 通道。"""
    srt = _srt("只剩下核酸天下了", "第二句正常文本")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "只剩下和成天下了",
                    "repair_class": "phonetic",
                    "source_surface": "和成天下",
                    "why": "槟榔品牌语境，核酸天下不成词",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 品牌/话题词：和成天下（槟榔品牌）",
    )
    assert findings[0]["candidate_provenance"] == {"kind": "glossary", "surface": "和成天下"}

    output, audit = route_findings(srt, findings)
    assert "和成天下" in output
    row = audit["findings"][0]
    assert row["routed"] == "expected_value_canon"
    assert row["expected_value_gate"]["policy"] == "GLOSSARY_HIGH_PRIOR"
    assert row["expected_value_gate"]["registered_name_conflict"] is False
    assert row["mutation_authority"]["decision_authority"] == (
        "EXPECTED_VALUE_CANON"
    )
    assert audit["applied_count"] == 1
    assert audit_correction_mutation_authority(audit)["status"] == "PASS"


def test_registered_proper_names_are_equal_and_exit_expected_value_lane():
    srt = _srt("我今天在看恋青", "第二句")
    finding = {
        "cue_index": 1,
        "kind": "entity",
        "suspect": "恋青",
        "suggestion": "恋死",
        "proposed_full_cue": "我今天在看恋死",
        "repair_class": "phonetic",
        "candidate_provenance": {"kind": "glossary", "surface": "恋死"},
        "base_text_sha256": hashlib.sha256(
            "我今天在看恋青".encode("utf-8")
        ).hexdigest(),
        "why": "两个登记作品名近音",
    }

    output, audit = route_findings(
        srt,
        [finding],
        protected_term_set=frozenset({"恋青", "恋死"}),
        registered_term_set=frozenset({"恋青", "恋死"}),
    )

    assert output == srt
    assert audit["applied_count"] == 0
    assert audit["findings"][0]["routed"] == "disclosure"
    assert audit["findings"][0]["requires_cpa_judge"] is True
    assert audit["findings"][0]["registered_name_conflict"] is True
    assert audit["findings"][0]["expected_value_gate"] == {
        "schema_version": "glossary-expected-value-gate.v1",
        "status": "BLOCK",
        "policy": "REGISTERED_NAME_EQUALITY",
        "candidate_provenance_kind": "glossary",
        "registered_target": "恋死",
        "current_registered_term": True,
        "proposed_registered_term": True,
        "registered_name_conflict": True,
        "routed": "CPA_REQUIRED",
    }


def test_entity_surface_suspect_never_text_applied():
    """xyz/示例球 保向铁律：suspect 是注册实体词面时 T1 让位声学仲裁。"""
    srt = _srt("我的示例球又来直播间了", "第二句")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "entity",
                    "proposed_full_cue": "我的xyz又来直播间了",
                    "repair_class": "source_backed_entity",
                    "source_surface": "xyz",
                    "why": "帕鲁语境示例球是xyz误听",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 人名/ID：xyz",
    )
    assert findings and findings[0]["suggestion"] == "xyz"

    # protected_term_set 置空以隔离测试实体选边车道本身（真实运行里
    # 示例球还会先被词表保护车道拦下——两道防线殊途同归都不许纯文本改写）。
    output, audit = route_findings(
        srt,
        findings,
        protected_term_set=frozenset(),
        entity_surface_set=frozenset({"xyz", "示例球"}),
    )
    assert "示例球" in output  # 未被纯文本改写
    row = audit["findings"][0]
    assert row["routed"] == "disclosure"
    assert row.get("entity_surface_conflict") is True


def test_witnessed_but_phonetically_distant_stays_disclosure():
    srt = _srt("只剩下苹果手机了", "上面提到和成天下")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "只剩下和成天下了",
                    "repair_class": "phonetic",
                    "source_surface": "和成天下",
                    "why": "强行替换",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 品牌/话题词：和成天下（槟榔品牌）",
    )
    output, audit = route_findings(srt, findings)
    assert "苹果手机" in output
    assert audit["findings"][0]["routed"] == "disclosure"


def test_same_derived_transcript_witness_requires_acoustic_confirmation():
    """醉堆→这一堆可以由前文召回，但同一 ASR 派生文本不能自证落字。"""
    srt = _srt("旁边这一堆都是新来的", "醉堆小主好可爱哦")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 2,
                    "kind": "context",
                    "proposed_full_cue": "这一堆小主好可爱哦",
                    "repair_class": "phonetic",
                    "source_surface": "这一堆",
                    "why": "醉堆不成词，前文刚说旁边这一堆",
                }
            ]
        ),
        extract_json=json.loads,
    )
    assert findings[0]["candidate_provenance"]["kind"] == "transcript_context"
    assert findings[0]["candidate_provenance"]["nearest_cue_distance"] == 1
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["correlated_text_witness"] is True

    output, audit = route_findings(srt, findings)
    assert "醉堆小主好可爱哦" in output
    row = audit["findings"][0]
    assert row["routed"] == "disclosure"


def test_widened_span_does_not_admit_absurd_shared_tail():
    """反例：苹果天下→和成天下（词表见证 + 共享「天下」尾巴）不许被扩窗
    量法抬上线——扩窗只放 1 共享字 + 0.65 高阈值。"""
    srt = _srt("只剩下苹果天下了", "第二句")
    findings = audit_final_subtitles(
        srt,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "只剩下和成天下了",
                    "repair_class": "phonetic",
                    "source_surface": "和成天下",
                    "why": "强行替换",
                }
            ]
        ),
        extract_json=json.loads,
        glossary_text="- 品牌/话题词：和成天下（槟榔品牌）",
    )
    output, audit = route_findings(srt, findings)
    assert "苹果天下" in output
    assert audit["findings"][0]["routed"] == "disclosure"


def test_candidate_only_memory_cannot_be_promoted_to_source_witness():
    source = _srt("就是霸凌的那种小的吧")
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "就是霸凌的那种晓得吧",
                    "repair_class": "source_backed_entity",
                    "source_surface": "晓得吧",
                    "why": "长期口癖候选",
                }
            ]
        ),
        extract_json=json.loads,
        candidate_context_text="idiolect candidate only: 晓得吧",
    )

    assert len(findings) == 1
    assert findings[0]["suggestion"] is None
    assert (
        findings[0]["suggestion_rejected_reason"]
        == "ENTITY_SOURCE_SURFACE_UNWITNESSED"
    )


def test_candidate_only_memory_forces_acoustic_even_when_homophone():
    source = _srt("就是霸凌的那种小的吧")
    memory_id = "channel.idiolect.xiaodeba.r1"
    candidate_context = {
        "speech_memory": {
            "ledger_sha256": "sha256:" + "a" * 64,
            "entries": [
                {
                    "memory_id": memory_id,
                    "candidate_canonicals": ["晓得吧"],
                }
            ],
        }
    }
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "就是霸凌的那种晓得吧",
                    "repair_class": "phonetic",
                    "candidate_memory_id": memory_id,
                    "why": "长期口癖候选",
                }
            ]
        ),
        extract_json=json.loads,
        candidate_context_text="candidate only",
        candidate_context=candidate_context,
    )
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["candidate_provenance"]["kind"] == "speech_memory_candidate"

    output, audit = route_findings(source, findings, protected_term_set=frozenset())
    assert "小的吧" in output
    assert audit["findings"][0]["routed"] == "disclosure"


def test_candidate_memory_display_prefix_is_normalized_only_to_verified_id():
    source = _srt("她是一个桔梗妹")
    memory_id = "channel.idiolect.jieganmei.r1"
    candidate_context = {
        "speech_memory": {
            "ledger_sha256": "sha256:" + "b" * 64,
            "entries": [
                {
                    "memory_id": memory_id,
                    "candidate_canonicals": ["姐感妹"],
                }
            ],
        }
    }
    findings = audit_final_subtitles(
        source,
        llm_call=_fake_llm(
            [
                {
                    "cue": 1,
                    "kind": "context",
                    "proposed_full_cue": "她是一个姐感妹",
                    "repair_class": "phonetic",
                    "candidate_memory_id": f"id={memory_id}",
                    "why": "模型复制了上下文展示标签",
                }
            ]
        ),
        extract_json=json.loads,
        candidate_context_text=f"id={memory_id}",
        candidate_context=candidate_context,
    )

    assert findings[0]["suggestion"] == "姐感"
    assert findings[0]["candidate_memory_id"] == memory_id
    assert findings[0]["candidate_memory_id_raw"] == f"id={memory_id}"
    assert findings[0]["force_acoustic"] is True
    assert findings[0]["candidate_provenance"]["memory_id"] == memory_id


def _weak_witness(request, heard, *, confidence=0.6, uncertain=()):
    return {
        "schema_version": "subtitle-span-acoustic-witness.v1",
        "request_sha256": request["request_sha256"],
        "status": "OBSERVED",
        "target_audible": True,
        "heard_pinyin": heard,
        "uncertain_positions": list(uncertain),
        "syllable_count": len(heard.split()),
        "confidence": confidence,
        "reason": "fast speech",
    }


def test_screen_read_escalation_recovers_fast_spoken_ui_line():
    """424_522 1:24 案（公开规则）：快速念屏「战斗回合用尽，即将
 离开战场」音频糊——弱证词触发读屏，OCR 池拼音对齐命中，verified_ocr
 出处进入同一裁决引擎，judge PROPOSED 后施改。"""

    source = _srt("战斗回合永进即将离开占场")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "永进",
        "suggestion": "用尽",
        "proposed_full_cue": "战斗回合用尽即将离开占场",
        "repair_class": "phonetic",
        "why": "语境不通",
    }
    probes = []

    def fake_screen_probe(start_ms, end_ms):
        probes.append((start_ms, end_ms))
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": "/x/padded.mp4",
            "span_start_ms": start_ms,
            "span_end_ms": end_ms,
            "frame_ms": [start_ms + 100],
            "pool": ["战斗回合用尽，即将离开战场", "设置", "退出对局"],
        }

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request,
            "zhan dou hui he yong jin ji jiang li kai zhan chang",
            confidence=0.6,
        ),
        judge_llm_call=_judge("PROPOSED"),
        screen_read_probe=fake_screen_probe,
    )

    assert probes, "弱证词必须触发读屏探针"
    assert "战斗回合用尽，即将离开战场" in output
    assert audit["repaired"] is True
    esc = audit["screen_read_witness"]
    assert esc["source"] == "screen_frames"
    assert esc["match"]["text"] == "战斗回合用尽，即将离开战场"
    assert esc["screen_token_alignment"]["passes"] is True
    assert esc["screen_token_alignment"]["token_coverage"] >= 0.8
    assert audit["request"]["candidate_provenance"]["kind"] == "verified_ocr"
    assert audit["mutation_authority"]["status"] == "PASS"


def test_weak_screen_match_stays_evidence_only_and_preserves_provenance():
    source = _srt("又回去各自睡觉")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "各自",
        "suggestion": "各字",
        "proposed_full_cue": "又回去各字睡觉",
        "repair_class": "phonetic",
        "candidate_provenance": {
            "kind": "cpa_context_proposal",
            "history_sha256": "history-bound",
            "mutation_authorized": False,
        },
        "why": "语境不通",
    }
    original_finding = deepcopy(finding)

    def fake_screen_probe(start_ms, end_ms):
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": "/bound/padded.mp4",
            "span_start_ms": start_ms,
            "span_end_ms": end_ms,
            "frame_ms": [start_ms + 100, end_ms - 100],
            "receipts": [{"receipt_id": "r1", "answer": "ocr"}],
            "pool": ["要唱的歌"],
        }

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request,
            "you yao hui qu gen ju ti shuo",
            confidence=0.6,
        ),
        judge_llm_call=_judge("CURRENT"),
        screen_read_probe=fake_screen_probe,
    )

    assert output == source
    assert finding == original_finding
    alignment = audit["screen_read_witness"]["screen_token_alignment"]
    assert alignment["passes"] is False
    assert alignment["token_coverage"] < 0.8
    evidence = audit["request"]["candidate_provenance"]["screen_read_evidence"]
    assert evidence["status"] == "EVIDENCE_ONLY"
    assert evidence["reason_code"] == "SCREEN_MATCH_TOKEN_ALIGNMENT_WEAK"
    assert evidence["media_path"] == "/bound/padded.mp4"
    assert audit["request"]["candidate_provenance"]["kind"] == (
        "cpa_context_proposal"
    )
    assert audit["request"]["candidate_provenance"]["history_sha256"] == (
        "history-bound"
    )
    assert "verified_ocr" not in str(audit["request"]["candidate_provenance"])


def test_screen_read_token_alignment_records_ordered_token_gate():
    weak = screen_read_token_alignment(
        heard_pinyin="you yao hui qu gen ju ti shuo",
        candidate="要唱的歌",
    )
    strong = screen_read_token_alignment(
        heard_pinyin="zhan dou hui he yong jin ji jiang li kai zhan chang",
        candidate="战斗回合用尽，即将离开战场",
    )
    assert weak["gate"] == "ordered_token_coverage"
    assert weak["order_preserved"] is True
    assert weak["matched_token_count"] < weak["candidate_token_count"]
    assert weak["passes"] is False
    assert weak["reason_code"] == "ORDERED_TOKEN_COVERAGE_BELOW_THRESHOLD"
    assert strong["passes"] is True
    assert strong["token_coverage"] == 1.0
    assert strong["reason_code"] == "ORDERED_TOKEN_COVERAGE_PASS"


def test_screen_read_token_alignment_short_dictation_is_evidence_only():
    for heard_pinyin in ("yao", "yaoyaohuiqugenjutishuo"):
        result = screen_read_token_alignment(
            heard_pinyin=heard_pinyin,
            candidate="要唱的歌",
        )
        assert result["passes"] is False
        assert result["matched_token_count"] == 0
        assert result["token_coverage"] == 0.0
        assert result["order_preserved"] is False
        assert result["reason_code"] == "HEARD_TOKEN_SEQUENCE_TOO_SHORT"


def test_unbound_cpa_candidate_mapping_still_triggers_screen_read():
    """A CPA proposal mapping is still unbound and must reach frame escalation."""

    source = _srt("战斗回合永进即将离开占场")
    probes = []

    def fake_screen_probe(start_ms, end_ms):
        probes.append((start_ms, end_ms))
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": "/x/padded.mp4",
            "span_start_ms": start_ms,
            "span_end_ms": end_ms,
            "frame_ms": [start_ms + 100, end_ms - 100],
            "pool": ["战斗回合用尽，即将离开战场"],
        }

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "kind": "context",
            "suspect": "永进",
            "suggestion": "用尽",
            "proposed_full_cue": "战斗回合用尽即将离开占场",
            "repair_class": "phonetic",
            "candidate_provenance": {
                "kind": "cpa_context_proposal",
                "mutation_authorized": False,
            },
        },
        entity_verifier=lambda request: _weak_witness(
            request,
            "zhan dou hui he yong jin ji jiang li kai zhan chang",
        ),
        judge_llm_call=_judge("PROPOSED"),
        screen_read_probe=fake_screen_probe,
    )

    assert probes
    assert "战斗回合用尽，即将离开战场" in output
    assert audit["request"]["candidate_provenance"]["kind"] == "verified_ocr"


def test_bound_verified_ocr_does_not_probe_frames_again():
    source = _srt("战斗回合永进即将离开占场")
    probes = []

    def unexpected_screen_probe(start_ms, end_ms):
        probes.append((start_ms, end_ms))
        raise AssertionError("bound OCR must not trigger another frame probe")

    output, audit = adjudicate_context_finding(
        source,
        {
            "cue_index": 1,
            "kind": "context",
            "suspect": "永进",
            "suggestion": "用尽",
            "proposed_full_cue": "战斗回合用尽即将离开占场",
            "repair_class": "phonetic",
            "candidate_provenance": {
                "kind": "verified_ocr",
                "surface": "用尽",
                "screen_read": {"media_path": "/x/padded.mp4", "frame_ms": [100]},
            },
        },
        entity_verifier=lambda request: _weak_witness(
            request,
            "zhan dou hui he yong jin ji jiang li kai zhan chang",
        ),
        judge_llm_call=_judge("PROPOSED"),
        screen_read_probe=unexpected_screen_probe,
    )

    assert probes == []
    assert "战斗回合用尽，即将离开战场" not in output
    assert audit.get("screen_read_witness") is None


@pytest.mark.parametrize(
    ("choice", "expected_output"),
    [
        ("CURRENT", "战斗回合永进即将离开占场"),
        ("PROPOSED", "战斗回合用尽即将离开占场"),
    ],
)
def test_unmatched_screen_pool_is_cpa_evidence_only(choice, expected_output):
    source = _srt("战斗回合永进即将离开占场")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "永进",
        "suggestion": "用尽",
        "proposed_full_cue": "战斗回合用尽即将离开占场",
        "repair_class": "phonetic",
        "candidate_provenance": {
            "kind": "cpa_context_proposal",
            "mutation_authorized": False,
        },
    }
    original_request = build_context_adjudication_request(source, finding)
    prompts = []

    def fake_screen_probe(start_ms, end_ms):
        return {
            "schema_version": "screen-read-witness.v1",
            "media_path": "/x/padded.mp4",
            "span_start_ms": start_ms,
            "span_end_ms": end_ms,
            "frame_ms": [start_ms + 100, end_ms - 100],
            "receipts": [
                {
                    "receipt_id": "screen-r1",
                    "answer": "无关屏幕文本",
                }
            ],
            "pool": ["无关屏幕文本"],
        }

    def capture_judge(prompt):
        prompts.append(prompt)
        return json.dumps({"choice": choice, "reason": "test"})

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request,
            "zhan dou hui he yong jin ji jiang li kai zhan chang",
        ),
        judge_llm_call=capture_judge,
        screen_read_probe=fake_screen_probe,
    )

    evidence = audit["request"]["candidate_provenance"]["screen_read_evidence"]
    assert output == _srt(expected_output)
    assert audit["request"]["candidate_provenance"]["kind"] == "cpa_context_proposal"
    assert audit["request"]["candidate_provenance"]["mutation_authorized"] is False
    assert evidence["status"] == "EVIDENCE_ONLY"
    assert evidence["mutation_authorized"] is False
    assert evidence["pool"] == ["无关屏幕文本"]
    assert evidence["receipts"] == [
        {"receipt_id": "screen-r1", "answer": "无关屏幕文本"}
    ]
    assert audit["screen_read_witness"]["evidence_only"]["reason_code"] == (
        "SCREEN_POOL_NO_PINYIN_MATCH"
    )
    assert audit["request"]["request_sha256"] != original_request["request_sha256"]
    assert "无关屏幕文本" in "\n".join(prompts)
    assert "verified_ocr" not in str(audit["request"]["candidate_provenance"])


@pytest.mark.parametrize("pool", [["战斗回合用尽，即将离开战场"], ["无关屏幕文本"]])
@pytest.mark.parametrize("require_complete", [False, True])
def test_screen_upgrade_preserves_exact_current_utterance_obligation(pool, require_complete):
    source = _srt("战斗回合永进即将离开占场")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "永进",
        "suggestion": "用尽",
        "proposed_full_cue": "战斗回合用尽即将离开占场",
        "repair_class": "phonetic",
    }
    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request, "zhan dou hui he yong jin ji jiang li kai zhan chang"
        ),
        # A relative CURRENT choice deliberately lacks a whole-cue claim.
        judge_llm_call=lambda prompt: json.dumps({"choice": "CURRENT", "reason": "relative choice"}),
        screen_read_probe=lambda start, end: {
            "schema_version": "screen-read-witness.v1",
            "media_path": "/x/padded.mp4",
            "span_start_ms": start,
            "span_end_ms": end,
            "frame_ms": [start, end - 1],
            "pool": pool,
        },
        source_media_timeline_offset_ms=10630,
        require_complete_utterance_support=require_complete,
    )
    assert output == source
    assert audit["request"].get("require_complete_utterance_support", False) is require_complete
    assert audit["request"]["source_media_timeline_offset_ms"] == 10630
    judge = audit["witness_judge"]["judge"]
    if require_complete:
        assert judge["reason_code"] == "CURRENT_UTTERANCE_SUPPORT_REQUIRED"
        assert judge["choice"] == "UNCERTAIN"
    else:
        assert judge["choice"] == "CURRENT"


def test_semantic_trigger_prefers_danmaku_pool_before_frames():
    """维护者 同日扩展：语境不通（有 finding 无出处）即触发，且弹幕池先于
    画面——池命中时探针一次都不该调用，出处=structured_chat_bound。"""

    source = _srt("大家说的都是磨牙好赢")
    finding = {
        "cue_index": 1,
        "kind": "context",
        "suspect": "磨牙好赢",
        "suggestion": "摩耶好楹",
        "proposed_full_cue": "大家说的都是摩耶好楹",
        "repair_class": "phonetic",
        "why": "语境不通",
    }
    probes = []

    def never_screen_probe(start_ms, end_ms):
        probes.append((start_ms, end_ms))
        return {"pool": []}

    output, audit = adjudicate_context_finding(
        source,
        finding,
        entity_verifier=lambda request: _weak_witness(
            request,
            "da jia shuo de dou shi mo ye hao ying",
            confidence=0.95,  # 听得清——触发靠语境（finding 无出处）
        ),
        judge_llm_call=_judge("PROPOSED"),
        clip_context={
            "structured_chat": [
                {
                    "sender": "观众A",
                    "text": "魔涯号营来啦",
                    "source_sha256": "a" * 64,
                },
                {"sender": "观众B", "text": "今天天气不错"},
            ]
        },
        screen_read_probe=never_screen_probe,
    )

    assert probes == [], "弹幕池命中时不许烧视觉调用"
    assert "大家说的都是魔涯号营来啦" in output  # 嵌入式拼接，句架保留
    esc = audit["screen_read_witness"]
    assert esc["source"] == "danmaku_pool"
    assert (
        audit["request"]["candidate_provenance"]["kind"]
        == "structured_chat_bound"
    )


def test_pinned_replay_skip_yields_zero_mutation_pass():
    audit = audit_correction_mutation_authority(
        {
            "schema_version": "final-review-audit.v1",
            "status": "SKIPPED_PINNED_REPLAY",
            "findings": [],
            "applied_count": 0,
            "pinned_replay_ownership": {
                "exact_interval_replay": True,
                "baseline_sha256": "de" + "ad" * 31,
                "publication_authority_sha256": "sha256:" + "ab" * 32,
            },
        }
    )
    assert audit["status"] == "PASS"
    assert audit["validated_mutation_count"] == 0
    assert audit["pinned_replay_skip"]["exact_interval_replay"] is True


def test_pinned_replay_skip_without_disclosure_blocks():
    for broken in (
        {"status": "SKIPPED_PINNED_REPLAY", "findings": []},
        {
            "status": "SKIPPED_PINNED_REPLAY",
            "findings": [],
            "pinned_replay_ownership": {"exact_interval_replay": False},
        },
        {
            "status": "SKIPPED_PINNED_REPLAY",
            "findings": [{"routed": "expected_value_canon"}],
            "pinned_replay_ownership": {
                "exact_interval_replay": True,
                "baseline_sha256": "de" + "ad" * 31,
                "publication_authority_sha256": "sha256:" + "ab" * 32,
            },
        },
    ):
        audit = audit_correction_mutation_authority(broken)
        assert audit["status"] == "BLOCK"
        assert audit["failures"][0]["reason_code"] == (
            "PINNED_REPLAY_SKIP_UNDISCLOSED"
        )


@pytest.mark.parametrize("cache_case", ["valid", "miss", "wrong_request", "uncertain", "error"])
def test_existing_bound_audio_is_not_ignored_by_text_first_judge(cache_case):
    source = _srt("这图书馆没人", "这是图书馆没人")
    events = []

    def audio(_request):
        raise AssertionError("no fresh audio call is needed")

    def probe(request):
        events.append("CACHE")
        if cache_case == "miss":
            return None
        if cache_case == "error":
            raise OSError("cache unavailable")
        result = {**_witness(request, "zhe shi tu shu guan mei ren"), "served_from_cache": True}
        if cache_case == "wrong_request":
            result["request_sha256"] = "0" * 64
        if cache_case == "uncertain":
            result["status"] = "UNCERTAIN"
        return result

    audio.probe_witness_cache = probe

    def judge(prompt):
        events.append("CPA")
        if cache_case == "valid":
            assert "TEXT_FIRST" not in prompt
            assert "zhe shi tu shu guan mei ren" in prompt
            choice = "CURRENT"
        else:
            assert "TEXT_FIRST" in prompt
            choice = "PROPOSED"
        return json.dumps({"choice": choice, "needs_audio": False,
                           "ranking": [{"choice": choice, "p": .98},
                                       {"choice": "PROPOSED" if choice == "CURRENT" else "CURRENT", "p": .01},
                                       {"choice": "NEITHER", "p": .01}], "reason": "根据已有证据裁决"})

    output, audit = adjudicate_context_finding(
        source, {"cue_index": 2, "suspect": "是", "suggestion": "",
                 "proposed_full_cue": "这图书馆没人", "repair_class": "spoken_unit",
                 "base_text_sha256": hashlib.sha256("这是图书馆没人".encode()).hexdigest(),
                 "span_start_codepoint": 1, "span_end_codepoint": 2,
                 "why": "语法怀疑，但已有实际听音"},
        entity_verifier=audio, judge_llm_call=judge,
    )
    assert events == ["CACHE", "CPA"]
    if cache_case == "valid":
        assert output == source
        assert audit["policy_branch"] == "JUDGE_KEEPS_CURRENT"
        assert audit["verdict"]["served_from_cache"] is True
        assert "text_first_judge" not in audit
    else:
        assert output != source
        assert audit["verdict"]["reason_code"] == "CPA_TEXT_FIRST_NOT_REQUESTED"
