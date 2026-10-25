"""A target-only, source-bound terminal decision; never a new audio provider.

Raw judge answers and native witnesses are immutable inputs. This module proves
one exact text replacement, and preserves every outside question as an ordinary
finding. It does not make the rest of the utterance correct or authorize release.
"""

from __future__ import annotations

from .review_errors import ContractRejection

from .review_evidence_store import cold_input

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import difflib
import json
import re
import unicodedata

FIELD = "span_context_resolution"
SCHEMA = "subtitle-span-context-phoneme-resolution.v1"
BINDING_SCHEMA = "subtitle-edit-span-binding.v1"
POLICY = "CONTEXT_PHONEME_FIT"
SCOPE_FIELD = "span_review"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _hash(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", value):
        raise ContractRejection("SPAN_DIGEST_INVALID")
    return value.removeprefix("sha256:")


def _interval(value, size: int) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(x) is not int for x in value)
        or not 0 <= value[0] < value[1] <= size
    ):
        raise ContractRejection("SPAN_LOCATION_INVALID")
    return value[0], value[1]


def edit_binding(request: Mapping, finding: Mapping | None = None) -> dict | None:
    """Bind an exact codepoint replacement to the existing physical cue clock.

    No proportional word timestamp is invented. Observation slices below carry
    their own actual audio window, separately from the text codepoint interval.
    """
    current, proposed = request.get("current_cue"), request.get("proposed_cue")
    if not isinstance(current, str) or not isinstance(proposed, str) or current == proposed:
        return None
    suspect, replacement = request.get("suspect"), request.get("replacement")
    if (
        not isinstance(suspect, str)
        or not suspect
        or not isinstance(replacement, str)
        or not replacement
    ):
        return None  # Insertion, deletion and full-cue silence keep their owners.
    stored = request.get("edit_span_binding")
    indices = (stored or {}).get("codepoint_span") if isinstance(stored, Mapping) else None
    if finding is not None and any(
        k in finding for k in ("span_start_codepoint", "span_end_codepoint")
    ):
        indices = [finding.get("span_start_codepoint"), finding.get("span_end_codepoint")]
    if indices is None:
        # Count overlapping occurrences as well: e.g. 年年 appears twice in
        # 年年年. A proposed replacement does not supply a measured location.
        start = current.find(suspect)
        if start < 0 or current.find(suspect, start + 1) >= 0:
            raise ContractRejection("SPAN_LOCATION_AMBIGUOUS")
        indices = [start, start + len(suspect)]
    start, end = _interval(indices, len(current))
    if current[start:end] != suspect or current[:start] + replacement + current[end:] != proposed:
        raise ContractRejection("SPAN_REPLACEMENT_DRIFT")
    whole = request.get("whole_clip_current_srt")
    if not isinstance(whole, str) or request.get("base_text_sha256") != text_sha(current):
        raise ContractRejection("SPAN_CURRENT_TEXT_DRIFT")
    from src.autoslice.jingting_chunker import parse_srt_cues

    cues = [c for c in parse_srt_cues(whole) if c.text.strip()]
    ids = request.get("cue_indexes")
    if (
        not isinstance(ids, list)
        or len(ids) != 1
        or type(ids[0]) is not int
        or not 1 <= ids[0] <= len(cues)
    ):
        raise ContractRejection("SPAN_CUE_INVALID")
    cue = cues[ids[0] - 1]
    offset = request.get("source_media_timeline_offset_ms")
    if (
        type(offset) is not int
        or offset < 0
        or cue.text != current
        or (request.get("matched_start_ms"), request.get("matched_end_ms"))
        != (cue.start_ms, cue.end_ms)
    ):
        raise ContractRejection("SPAN_CURRENT_CLOCK_DRIFT")
    result = {
        "schema_version": BINDING_SCHEMA,
        "srt_sha256": text_sha(whole),
        "cue_index": ids[0],
        "source_context_sha256": digest(request.get("clip_context_binding") or {}),
        "current_cue_sha256": text_sha(current),
        "proposed_cue_sha256": text_sha(proposed),
        "codepoint_span": [start, end],
        "current_span": suspect,
        "replacement": replacement,
        "cue_window_ms": [cue.start_ms, cue.end_ms],
        "source_offset_ms": offset,
        "physical_cue_window_ms": [cue.start_ms + offset, cue.end_ms + offset],
    }
    result["binding_sha256"] = digest(result)
    if stored is not None and stored != result:
        raise ContractRejection("SPAN_BINDING_DRIFT")
    return result


def _speech(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKC", text)
        if not c.isspace() and not unicodedata.category(c).startswith("P")
    ).casefold()


def _mandarin(text: str) -> tuple[str, ...]:
    # The T owner is the only single-digit pronunciation table. Repetitions
    # remain repetitions; the orthography key's legacy duplicate collapse is
    # deliberately NOT an acoustic fit algorithm.
    from src.autoslice.closed_set_proposal_rebuild import _pinyin

    if re.search(r"[A-Za-z\u3040-\u30ff]", text):
        raise ContractRejection("SPAN_NON_MANDARIN_TEXT")
    tokens = tuple(_pinyin(text).split())
    if not tokens or any(re.fullmatch("[a-züv]+", x) is None for x in tokens):
        raise ContractRejection("SPAN_PRONUNCIATION_UNAVAILABLE")
    return tokens


def range_observations(request: Mapping) -> tuple[list[dict], list[dict], list[dict]]:
    """Consume saved ordinary range receipts; never invent narrow timing.

    Completed wider context is a completed route, not necessarily a transcript
    of this cue. Only native-contained segments/words or an exact-cue crop can
    enter phoneme comparison. Other material stays explicitly context-only.
    """
    from src.autoslice.independent_context_adjudication import (
        validate_range_acoustic_observation,
        adapt_range_acoustic_context_observation,
    )

    def validate(source, request, supplied):
        valid, reason = validate_range_acoustic_observation(source, request, supplied)
        if valid is None:
            valid, reason = adapt_range_acoustic_context_observation(source, request, supplied)
        return valid, reason

    return _project_range_observations(request, validate)


def project_validated_range_observation(request: Mapping, supplied: Mapping, validated: Mapping):
    """Project an owner's ALREADY validated range, without recursive admission.

    This is a pure text/timing projection, not an authority or validation
    shortcut. The ordinary source/clock/raw/admission owner must run first.
    """
    rows, _, _ = _project_range_observations(
        {**request, 'context_only_range_acoustic_observations': [supplied]},
        lambda source, check, offered: (validated, None))
    return rows[0] if rows else None


def _project_range_observations(request: Mapping, validate):
    from src.autoslice.subtitle_audio_evidence import evidence_table

    source = request["whole_clip_current_srt"]
    start, end = request["matched_start_ms"], request["matched_end_ms"]
    offset, cue_index = request["source_media_timeline_offset_ms"], request["cue_indexes"][0]
    observations, receipts, invalid = [], [], []
    for supplied in request.get("context_only_range_acoustic_observations") or []:
        if not isinstance(supplied, Mapping) or supplied.get("evidence_scope") not in {
            "RANGE_CONTEXT_ONLY",
            "RANGE_CONTEXT_ONLY_SRT_CARRYOVER",
        }:
            continue  # Independent watched-source speech is not the host's target.
        validated, reason = validate(source, request, supplied)
        rid = digest(supplied)
        raw_status = (validated or {}).get("verdict") or {}
        # Each provider owns its successful protocol. The normal segmented
        # adapter has already validated the immutable raw/transport/source and
        # deliberately leaves no synthetic status on its normalized verdict.
        observed = raw_status.get("status") == "OBSERVED"
        if ((validated or {}).get("artifact_bindings", {}).get("segmented_input_path")
                and (validated or {}).get("provider") in {"agy", "gemini_api"}):
            observed = (raw_status.get("original_provider_response") or {}).get("status") == "OBSERVED"
        if (not validated or validated.get("status") not in {"OBSERVED", "CONTEXT_ONLY"}
                or not observed):
            invalid.append(
                {"observation_id": rid, "reason_code": reason or "SPAN_RANGE_NOT_OBSERVED"}
            )
            continue
        raw = validated.get("verdict") or {}
        artifacts = validated["artifact_bindings"]
        native = raw.get("native_observation")
        segments = []
        if isinstance(native, Mapping):
            origin = raw["audio_start_ms"] - offset
            shifted = deepcopy(native)
            for segment in shifted.get("native_segments", []):
                segment["start_ms"] += origin
                segment["end_ms"] += origin
                for word in segment.get("words") or []:
                    word["start_ms"] += origin
                    word["end_ms"] += origin
            table = (validated.get("native_word_overlap_evidence") or {}).get(
                "table"
            ) or evidence_table(source, shifted)
            rows = table.get("rows") or []
            crossing = any(
                cue_index in w.get("overlapping_cues", [])
                for row in rows
                for w in row.get("unassigned_words", [])
            )
            if not crossing:
                segments = [
                    x for row in rows for x in row.get("cue_spans", []) if x["n"] == cue_index
                ]
        whole = validated.get("witness_request") or validated.get("range") or {}
        # A provider's whole exact-cue crop needs no inferred word timestamp.
        # G2: compare on the source clock -- a clock-rebased observation keeps
        # its own local window and offset (a request without one is on ours).
        whole_offset = whole.get("source_media_timeline_offset_ms", offset)
        exact_crop = (
            type(whole.get("matched_start_ms")) is int and type(whole.get("matched_end_ms")) is int
            and type(whole_offset) is int
            and (whole["matched_start_ms"] + whole_offset, whole["matched_end_ms"] + whole_offset)
            == (start + offset, end + offset)
        )
        if not segments and exact_crop and isinstance(raw.get("exact_transcript"), str):
            segments = [{"text": raw["exact_transcript"], "start_ms": start, "end_ms": end}]
        route = "range:" + (
            "gemini"
            if validated.get("provider") in {"agy", "gemini_api", "gemini"}
            else validated["provider"]
        )
        receipt = {
            "route": route,
            "status": "COMPLETED",
            "receipt_id": rid,
            "source_media_sha256": _hash(validated["media_source_binding"]["media_sha256"]),
            "artifact_bindings": deepcopy(artifacts),
            "target_projection": "NATIVE_TIMED_OR_EXACT_CROP"
            if segments
            else "CONTEXT_ONLY_NO_TARGET_TIMING",
            "raw_range": deepcopy(dict(supplied)),
        }
        if validated.get("observation_class") == "CONTEXT_ASSISTED_AUDIO":
            # Hints and their influenced transcript are one dependent piece
            # of context. They cannot become an independent pinyin projection.
            receipt.update(target_projection="DEPENDENT_CONTEXT_ASSISTED_AUDIO",
                           independent_witness=False)
            receipts.append(receipt)
            continue
        receipts.append(receipt)
        if not segments:
            continue
        text = "".join(x["text"] for x in segments)
        if not text.strip():
            continue
        crop = [raw.get("audio_start_ms"), raw.get("audio_end_ms")]
        if any(type(x) is not int for x in crop):
            window = validated.get("source_window") or {}
            crop = [window.get("crop_start_ms"), window.get("crop_end_ms")]
        if (
            any(type(x) is not int for x in crop)
            or not crop[0] <= start + offset < end + offset <= crop[1]
        ):
            invalid.append({"observation_id": rid, "reason_code": "SPAN_RANGE_COVERAGE_INVALID"})
            continue
        observations.append(
            {
                "observation_id": rid,
                "kind": "range_transcript",
                "language": "native_unlabelled",
                "text": text,
                "provider": validated["provider"],
                "model": validated["model"],
                "source_media_sha256": receipt["source_media_sha256"],
                "audio_clip_sha256": _hash(
                    artifacts.get("audio_clip_sha256") or artifacts.get("audio_clip")
                ),
                "response_sha256": _hash(
                    artifacts.get("raw_response_sha256") or artifacts.get("raw_response")
                ),
                "physical_audio_window_ms": crop,
                "native_cue_segments": segments,
                "witness_request": deepcopy(dict(whole)),
                "raw_observation": deepcopy(dict(supplied)),
            }
        )
    return observations, receipts, invalid


def _blind_source_is_clip_recording(request: Mapping, artifacts: Mapping, media_sha, sources) -> bool:
    """Same-source test for a reopened blind witness (41:1489-1492).

    A witness heard on producer padded/recut media is not source drift when
    the ordinary provenance validator binds that media to the clip's recording
    ancestor. The ancestry is read only from that validated binding.
    """
    expected = {_hash(x) for x in sources}
    if _hash(media_sha) in expected:
        return True
    from src.autoslice.targeted_relisten import validated_media_recordings

    media_path = artifacts.get("source_media") if isinstance(artifacts, Mapping) else None
    recordings = validated_media_recordings(request, media_path, media_sha)
    return bool(recordings) and {_hash(x) for x in recordings} <= expected


@cold_input
def observation_catalogue(request: Mapping, witness: Mapping) -> tuple[list[dict], list[dict]]:
    """Revalidate native target observations, retaining all conflicting ones.

    Range/source-only contexts are never relabelled as exact cue observations.
    The native transcript owner and native original-language owner remain the
    validators; a model label, candidate string or hash alone is insufficient.
    """
    from src.autoslice.acoustic_witness_adjudication import (
        build_witness_request,
        own_witness_request,
        retry_judge_evidence,
    )
    from src.autoslice.exact_final_witness_authority import (
        _valid_native_witness,
        _valid_blind_witness,
    )

    target = build_witness_request(request)
    gathered: list[tuple[Mapping, Mapping]] = [(witness, target)]
    argon_original_requests = {}
    gathered.extend(
        (x, target) for x in witness.get("independent_observations", []) if isinstance(x, Mapping)
    )
    for prior in request.get("prior_acoustic_observations", []):
        if not isinstance(prior, Mapping):
            continue
        if isinstance(prior.get("verdict"), Mapping) and isinstance(
            prior.get("witness_request"), Mapping
        ):
            bound = prior["witness_request"]
            from src.autoslice.acoustic_witness_adjudication import same_physical_target
            if digest({k: v for k, v in bound.items() if k != "request_sha256"}) == bound.get(
                "request_sha256"
            ) and same_physical_target(bound, target):
                # Same physical target under its own clock (padded vs delivery).
                gathered.append((prior["verdict"], bound))
                original = prior.get('check_request')
                if isinstance(original, Mapping) and prior['verdict'].get('argon_escalation_evidence'):
                    from .audio_question_identity import same_audio_question
                    if (digest({k: v for k, v in original.items() if k != 'request_sha256'}) == original.get('request_sha256')
                            and same_audio_question(original, request, compare_proposed=False, strict_source=True)[0]):
                        clean = {k: v for k, v in prior['verdict'].items()
                            if k not in {'independent_observations', 'served_from_cache'}}
                        argon_original_requests[digest(clean)] = original
        elif prior.get("schema_version") == "subtitle-span-acoustic-witness.v1":
            gathered.append((prior, target))
    for candidate in request.get("proposed_candidates", []):
        source = candidate.get("source") if isinstance(candidate, Mapping) else None
        if isinstance(source, Mapping):
            gathered.append((source, target))
    upgrade = request.get("span_termination_upgrade") or {}
    gathered.extend(
        (row, target) for row in upgrade.get("observations", []) if isinstance(row, Mapping)
    )
    # G1: rows heard for an earlier request (the independent routes and their
    # candidate sources keep the candidate-free request, 41) are projected
    # against that OWN request when its payload travels with the evidence;
    # never against the current request hash, which mixes evidence_id/wording.
    own_requests = [
        (request.get("independent_asr_evidence") or {}).get("witness_request")
        if isinstance(request.get("independent_asr_evidence"), Mapping) else None,
        *(prior.get("witness_request") for prior in request.get("prior_acoustic_observations", [])
          if isinstance(prior, Mapping)),
    ]
    originals = []

    def original_observations(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, Mapping):
            if value.get("schema_version") == "exact-final-source-transcript-observation.v2":
                originals.append(value)
            else:
                for key, item in value.items():
                    if key not in {"whole_clip_current_srt", "raw_completion", "prompt"}:
                        original_observations(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                original_observations(item, depth + 1)

    for value in (
        request.get("exact_source_transcript_handoff"),
        request.get("primary_original_language_observations"),
        request.get("prior_exact_source_transcript_candidates"),
        request.get("proposed_candidates"),
    ):
        original_observations(value)
    ordinary_original_ids = {digest(row) for row in originals}
    original_observations(upgrade.get("observations"))
    result, invalid = {}, []
    for raw, bound in gathered:
        if (
            raw.get("schema_version") != "subtitle-span-acoustic-witness.v1"
            or raw.get("status") != "OBSERVED"
        ):
            continue
        # Remove only container duplication, not evidence fields or failures.
        row = {
            k: deepcopy(v)
            for k, v in raw.items()
            if k not in {"independent_observations", "served_from_cache"}
        }
        identity = digest(row)
        if identity in result:
            continue
        try:
            if (
                row.get("argon_escalation_evidence") is not None
                or row.get("model") == "gemini-4-argon"
            ):
                from .argon_span_termination import catalogue_observation

                projected = catalogue_observation(argon_original_requests.get(identity, request), row)
                result[projected["observation_id"]] = projected
                continue
            # G1 same_hearing: the row's own request -- the projection request
            # itself, or a supplied original request it was heard for -- is the
            # binding; the physical target must equal ``bound``'s.
            own = (bound if row.get("request_sha256") == bound.get("request_sha256")
                   else own_witness_request(row, *own_requests))
            if row.get("witness_protocol") == "blind_pinyin":
                if not _valid_blind_witness(row, request_sha256=bound["request_sha256"],
                                            reuse_target=bound, bound_request=own):
                    raise ContractRejection("SPAN_BLIND_PHONEME_RECEIPT_INVALID")
                if row.get("relisten_artifacts") is not None:
                    # A candidate-free scaffold may change the semantic
                    # request ID. Reopen this explicitly supplied observation
                    # against its ORIGINAL request and original files; do not
                    # relabel the raw response or create another hearing.
                    from src.autoslice.entity_witness_cache_probe import validate_relisten_witness_artifacts

                    artifacts = validate_relisten_witness_artifacts(request=own or bound, witness=row)
                    sources = (request.get("clip_context_binding") or {}).get("source_media_sha256s", [])
                    if sources and not _blind_source_is_clip_recording(
                            request, artifacts, row["source_media_sha256"], sources):
                        raise ContractRejection("SPAN_BLIND_SOURCE_MEDIA_DRIFT")
                else:
                    material = retry_judge_evidence(request, row, bound_request=own or bound)
                    if not material:
                        raise ContractRejection("SPAN_AUDIO_IDENTITY_MISSING")
                kind, text, language = "blind_pinyin", row["heard_pinyin"], "zh"
            elif row.get("witness_protocol") == "candidate_blind_transcript":
                if not _valid_native_witness(row, witness_request=bound, bound_request=own):
                    raise ContractRejection("SPAN_NATIVE_TRANSCRIPT_RECEIPT_INVALID")
                # Project against the request this observation was bound to.
                material = retry_judge_evidence(request, row, bound_request=own or bound)
                kind, text, language = (
                    "native_transcript",
                    row["exact_transcript"],
                    "native_unlabelled",
                )
            else:
                raise ContractRejection("SPAN_OBSERVATION_PROTOCOL_UNSUPPORTED")
            result[identity] = {
                "observation_id": identity,
                "kind": kind,
                "text": text,
                "language": language,
                "provider": row.get("provider"),
                "model": row.get("model"),
                "source_media_sha256": _hash(row["source_media_sha256"]),
                "audio_clip_sha256": _hash(row["audio_clip_sha256"]),
                "response_sha256": _hash(row["response_sha256"]),
                "physical_audio_window_ms": [row["audio_start_ms"], row["audio_end_ms"]],
                "witness_request": deepcopy(dict(own or bound)),
                "raw_observation": row,
            }
        except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            invalid.append({"observation_id": identity, "reason_code": str(exc)})
    if originals:
        from src.autoslice.exact_source_transcript_contract import (
            build_exact_source_transcript_request,
            valid_exact_source_transcript_observation,
        )

        physical = build_exact_source_transcript_request(target)
        for original in originals:
            identity = digest(original)
            if identity in result:
                continue
            if not valid_exact_source_transcript_observation(original, request=physical):
                invalid.append(
                    {
                        "observation_id": identity,
                        "reason_code": "SPAN_ORIGINAL_LANGUAGE_RECEIPT_INVALID",
                    }
                )
                continue
            geo = original["timeline_binding"]["source_media"]
            result[identity] = {
                "observation_id": identity,
                "kind": "original_language",
                "text": original["exact_transcript"],
                "language": original["audible_language"],
                "route_origin": "primary_original_language"
                if identity in ordinary_original_ids
                else "upgrade",
                "provider": original["provider"],
                "model": original["model"],
                "source_media_sha256": _hash(original["source_media_sha256"]),
                "audio_clip_sha256": _hash(original["audio_clip_sha256"]),
                "response_sha256": _hash(original["response_sha256"]),
                "physical_audio_window_ms": [geo["crop_start_ms"], geo["crop_end_ms"]],
                "witness_request": deepcopy(dict(target)),
                "raw_observation": deepcopy(dict(original)),
            }
    envelope = request.get("argon_escalation_evidence")
    if envelope is not None:
        from .argon_span_termination import catalogue_observation, original_hearings
        from .argon_escalation_integration import transcript_witness

        try:
            projected = catalogue_observation(request, transcript_witness(request, envelope))
            result[projected["observation_id"]] = projected
            for old in original_hearings(request, envelope):
                prior = next(
                    (
                        r
                        for r in result.values()
                        if all(
                            r.get(k) == old.get(k)
                            for k in (
                                "provider",
                                "source_media_sha256",
                                "audio_clip_sha256",
                                "response_sha256",
                            )
                        )
                    ),
                    None,
                )
                if prior is None:
                    result[old["observation_id"]] = old
                else:
                    prior["route_origin"] = "primary_original_hearing"
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            invalid.append({"observation_id": "argon-native", "reason_code": str(exc)})
    ranged, _, range_invalid = range_observations(request)
    for row in ranged:
        result[row["observation_id"]] = row
    return list(result.values()), [*invalid, *range_invalid]


def prompt_context(request: Mapping, witness: Mapping) -> str:
    try:
        binding = edit_binding(request)
        if binding is None:
            return ""
        observations, invalid = observation_catalogue(request, witness)
    except (ValueError, TypeError, KeyError):
        return ""
    catalogue = [
        {k: v for k, v in x.items() if k not in {"raw_observation", "witness_request"}}
        for x in observations
    ]
    from src.autoslice.witness_self_consistency import bound_witness_assessment, assessment_prompt
    consistency_context = assessment_prompt(bound_witness_assessment(request, witness))
    return consistency_context + (
        "\nSPAN_REVIEW_INPUT\n"
        + json.dumps(
            {
                "binding": binding,
                "observations": catalogue,
                "invalid_observations": invalid,
                "request_sha256": request.get("request_sha256"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\nEND_SPAN_REVIEW_INPUT\n"
        + (
            "整句拼音距离在此跨度裁决中不提供字词相容性依据，只比较本目标的真实观察子串。"
            "本轮改字仅限binding.codepoint_span（Python Unicode codepoint，半开区间），其余原字、标点、重复词不动。"
            "候选首选与补听问题必须分开。相同目标读音的不同汉字不是反对候选的音素；数字单字读音按现有规则，"
            "多位数、编号、尾音与重复不能随意吞并。外语用原语言记录，绝不硬套汉语拼音。"
            "必须返回span_review定位结果；不能以省略字段表达不确定。能定位时按下述结构返回；"
            "不能定位时仍返回binding_sha256、request_sha256、status=UNLOCATABLE和具体unlocatable_reason，"
            "all_audio_questions_located=false，不编造alignment或时间。候选偏好不等于可改字："
            '{"binding_sha256":"原binding摘要","request_sha256":"本次请求摘要","context_basis":"本目标的具体语境依据",'
            '"all_audio_questions_located":true,"target_needs_audio":true或false,'
            '"alignments":[{"observation_id":"原观察ID","span":[起点,终点],"quote":"逐字原观察子串"}],'
            '"audio_questions":[{"codepoint_span":[起点,终点],"reason":"具体可听辨疑点","observation_ids":["原观察ID"]}]}。'
            "blind_pinyin的span按空格分割的原音素token编号，其余按原听写codepoint；不能制造词时间。"
            "alignments仅定位本次binding目标替换：每个适用observation_id严格只有一条目标alignment。"
            "同一观察的句首/尾词等目标外音节不要再放入alignments；在对应audio_questions的"
            "可选observation_alignments中单独给出原观察ID、span、quote，不能把外部定位当目标支持。"
            "每个适用观察都要定位，所有分歧必须保留，不能以包装/重放重复计同一路。"
            "如果原改动跨度仍有任何有效听写的音素支持，必须保留每路相容才收口的严格规则。"
            "仅当所有已完成有效听写都明确否定原跨度（不是缺观察、缺字或未知），不得因好候选间有分歧退回全无支持的原文。"
            "从至少有一路真实音素支持的候选中，按独立路线支持最多的读法加语境选取；票数相同由具体语境明确选择，不能发明未被任何路线支持的读法。"
            "这一规则不把needs_audio改false；仍原样定位全部疑点并保留每路支持/异议，程序会独立复验。目标外的句首/句尾问题独立定位到当前cue，"
            "不因目标同音或首选高概率而称其它词正确。无法区分目标词和相邻词/重复边界时不填伪定位，保留未决。"
            "needs_audio原字段仍如实返回；本字段不自行授权改字或放行。\n"
        )
    )


def capture_upgrade(request: Mapping, verifier) -> dict:
    """Read-only optional upgrade seam; enabling without state always blocks.

    The upgrade owner must validate its native result before returning an
    existing exact-source observation. No submission/poll/retry occurs here.
    """
    binding = edit_binding(request)
    callback = getattr(verifier, "span_termination_upgrade_status", None)
    enabled = getattr(verifier, "span_termination_upgrade_enabled", False) is True or callable(
        callback
    )
    if not enabled:
        return {"enabled": False, "status": "DISABLED", "observations": []}
    if not callable(callback):
        return {"enabled": True, "status": "STATE_UNAVAILABLE", "observations": []}
    try:
        state = callback(request)
    except Exception as exc:
        return {
            "enabled": True,
            "status": "STATE_UNAVAILABLE",
            "error_type": type(exc).__name__,
            "observations": [],
        }
    if state == {"enabled": False, "status": "DISABLED", "observations": []}:
        if getattr(verifier, "span_termination_upgrade_enabled", False) is True:
            return {"enabled": True, "status": "STATE_INVALID", "observations": []}
        return deepcopy(dict(state))
    if (
        not isinstance(state, Mapping)
        or state.get("enabled") is not True
        or binding is None
        or state.get("edit_binding_sha256") != binding["binding_sha256"]
    ):
        return {"enabled": True, "status": "STATE_INVALID", "observations": []}
    return deepcopy(dict(state))


def bind_runtime_request(request: Mapping, verifier) -> dict:
    """Bind runtime capability/source state before the normal scoped judge."""
    from src.autoslice.independent_context_adjudication import bind_request

    inventory = getattr(verifier, "span_termination_ordinary_routes", None)
    if not isinstance(inventory, tuple) or edit_binding(request) is None:
        return dict(request)
    reader = getattr(verifier, "span_termination_source_identity", None)
    try:
        source = reader() if callable(reader) else None
    except Exception:
        source = None
    state = request.get("span_termination_upgrade")
    if not isinstance(state, Mapping):
        state = capture_upgrade(request, verifier)
    return bind_request(
        request,
        span_termination_inventory=list(inventory),
        span_termination_source_sha256=source,
        span_termination_upgrade=deepcopy(dict(state)),
    )


def route_state(request: Mapping, witness: Mapping, independent: Mapping | None, verifier) -> dict:
    """The runtime declares routes; the model never declares them exhausted."""
    inventory = getattr(verifier, "span_termination_ordinary_routes", None)
    if verifier is None and isinstance(request.get("span_termination_inventory"), list):
        inventory = tuple(request["span_termination_inventory"])
    observations, invalid = observation_catalogue(request, witness)
    state = {
        "ordinary_inventory": list(inventory) if isinstance(inventory, tuple) else None,
        "ordinary": [],
        "upgrade": deepcopy(
            request.get("span_termination_upgrade") or capture_upgrade(request, verifier)
        ),
        "invalid_observations": invalid,
    }
    native = (independent or {}).get("observations") or witness.get("independent_observations") or []
    primary = (independent or {}).get("prior_witness") or witness
    if verifier is None:
        # Candidates may replace the witness envelope, never the prior actual
        # primary acquisition. Use only an already present bound observation.
        primary = next((x.get("verdict", x) for x in request.get("prior_acoustic_observations", [])
                        if isinstance(x, Mapping)
                        and x.get("verdict", x).get("provider") in {"agy", "gemini_api"}), primary)
    _, ranges, _ = range_observations(request)
    state["range_receipts"] = ranges
    for route in inventory or ():
        if route.startswith("range:"):
            receipts = [r for r in ranges if r["route"] == route]
            state["ordinary"].append(
                {
                    "route": route,
                    "status": "COMPLETED" if receipts else "UNRESOLVED",
                    "range_receipt_ids": [r["receipt_id"] for r in receipts],
                    "observation_ids": [],
                }
            )
            continue
        raw = (
            primary
            if route == "primary"
            else next((x for x in native if x.get("provider") == route), {})
        )
        identifiers = [
            o["observation_id"]
            for o in observations
            if o["raw_observation"].get("response_sha256") == raw.get("response_sha256")
            and o["source_media_sha256"]
            == str(raw.get("source_media_sha256")).removeprefix("sha256:")
            and o.get("provider") == raw.get("provider")
            and o.get("model") == raw.get("model")
            and o.get("audio_clip_sha256") == raw.get("audio_clip_sha256")
            and o.get("physical_audio_window_ms")
            == [raw.get("audio_start_ms"), raw.get("audio_end_ms")]
        ]
        if route == "primary" and request.get("argon_escalation_evidence") is not None:
            identifiers = [
                o["observation_id"]
                for o in observations
                if o.get("route_origin") == "primary_original_hearing"
            ]
        # The primary route can have completed its own original-language
        # transcript after the blind Mandarin protocol reported inapplicability.
        # A native observation, not a network/error return, establishes that.
        if route == "primary" and not identifiers:
            identifiers = [
                o["observation_id"]
                for o in observations
                if o["kind"] == "original_language"
                and o.get("route_origin") == "primary_original_language"
                and o["provider"] in {"agy", "gemini_api"}
            ]
        state["ordinary"].append(
            {
                "route": route,
                "status": "COMPLETED" if identifiers else "UNRESOLVED",
                "observation_ids": identifiers,
                "raw_status": raw.get("status"),
                "reason_code": raw.get("reason_code"),
            }
        )
    return state


def _outside_question(binding, request, interval, reason, references, comparisons=None):
    start, end = _interval(interval, len(request["current_cue"]))
    target_start, target_end = binding["codepoint_span"]
    if not (end <= target_start or start >= target_end):
        raise ContractRejection("SPAN_OUTSIDE_QUESTION_OVERLAPS_TARGET")
    row = {
        "codepoint_span": [start, end],
        "current_text": request["current_cue"][start:end],
        "reason": reason,
        "observation_ids": sorted(set(references)),
        "status": "UNRESOLVED",
        "comparison": comparisons or [],
    }
    row["question_sha256"] = digest(row)
    return row


def compile_resolution(request: Mapping, witness: Mapping, judge: Mapping, routes: Mapping) -> dict:
    """Pure terminal rule. All selectors and receipts are replayed by consumers."""
    binding = edit_binding(request)
    if binding is None:
        raise ContractRejection("SPAN_NOT_A_REPLACEMENT")
    unsigned = {k: v for k, v in request.items() if k != "request_sha256"}
    if digest(unsigned) != request.get("request_sha256"):
        raise ContractRejection("SPAN_REQUEST_DRIFT")
    if (
        judge.get("status") != "JUDGED"
        or judge.get("choice") != "PROPOSED"
        or judge.get("check_request_sha256") != request.get("request_sha256")
        or judge.get("selected_candidate_text", request["proposed_cue"]) != request["proposed_cue"]
    ):
        raise ContractRejection("SPAN_JUDGE_NOT_PROPOSED")
    raw = judge.get("span_review_raw_completion")
    if not isinstance(raw, str) or text_sha(raw) != judge.get("completion_sha256"):
        raise ContractRejection("SPAN_RAW_SCOPE_REQUIRED")
    from src.autoslice.llm_client import extract_json_object

    payload = extract_json_object(raw)
    scope = payload.get(SCOPE_FIELD)
    if isinstance(scope, Mapping) and scope.get("status") == "UNLOCATABLE":
        raise ContractRejection("SPAN_LOCALIZATION_UNLOCATABLE")
    if judge.get("needs_audio") is not (payload.get("needs_audio") is not False) or judge.get(
        "needs_audio_requested"
    ) is not (payload.get("needs_audio") is True):
        raise ContractRejection("SPAN_RAW_AUDIO_FLAG_DRIFT")
    ranking = judge.get("ranking") or []
    raw_ranking = []
    for row in payload.get("ranking") or []:
        if (
            isinstance(row, Mapping)
            and str(row.get("choice") or "").strip().upper() in judge.get("choice_set", [])
            and type(row.get("p")) in (int, float)
            and 0 <= row["p"] <= 1
        ):
            raw_ranking.append({"choice": str(row["choice"]).strip().upper(), "p": float(row["p"])})
    if raw_ranking != ranking:
        raise ContractRejection("SPAN_RAW_RANKING_DRIFT")
    if ranking:
        if max(ranking, key=lambda x: x["p"])["choice"] != "PROPOSED":
            raise ContractRejection("SPAN_RAW_RANKING_NOT_PROPOSED")
    elif str(payload.get("choice") or "").upper() != "PROPOSED":
        raise ContractRejection("SPAN_RAW_CHOICE_NOT_PROPOSED")
    if (
        not isinstance(scope, Mapping)
        or scope != judge.get(SCOPE_FIELD)
        or scope.get("binding_sha256") != binding["binding_sha256"]
        or scope.get("request_sha256") != request.get("request_sha256")
        or scope.get("all_audio_questions_located") is not True
        or type(scope.get("target_needs_audio")) is not bool
        or not isinstance(scope.get("context_basis"), str)
        or not scope["context_basis"].strip()
    ):
        raise ContractRejection("SPAN_JUDGE_SCOPE_INVALID")
    catalogue, invalid = observation_catalogue(request, witness)
    if not catalogue or invalid:
        raise ContractRejection("SPAN_VALID_TARGET_OBSERVATIONS_REQUIRED")
    sources = {o["source_media_sha256"] for o in catalogue}
    if len(sources) != 1:
        raise ContractRejection("SPAN_OBSERVATION_SOURCE_CONFLICT")
    if sources != {_hash(request.get("span_termination_source_sha256"))}:
        raise ContractRejection("SPAN_RUNTIME_SOURCE_IDENTITY_REQUIRED")
    direct = request.get("source_media_sha256")
    if direct is not None and sources != {_hash(direct)}:
        raise ContractRejection("SPAN_PHYSICAL_SOURCE_DRIFT")
    inventory = routes.get("ordinary_inventory")
    ordinary = routes.get("ordinary")
    if (
        not isinstance(inventory, list)
        or inventory != request.get("span_termination_inventory")
        or not inventory
        or inventory[0] != "primary"
        or len(set(inventory)) != len(inventory)
        or not isinstance(ordinary, list)
        or [x.get("route") for x in ordinary] != inventory
        or any(
            x.get("status") != "COMPLETED"
            or not (x.get("observation_ids") or x.get("range_receipt_ids"))
            for x in ordinary
        )
    ):
        raise ContractRejection("SPAN_ORDINARY_ROUTES_NOT_EXHAUSTED")
    ids = {o["observation_id"] for o in catalogue}
    if any(not set(row["observation_ids"]) <= ids for row in ordinary):
        raise ContractRejection("SPAN_ROUTE_OBSERVATION_DRIFT")
    by_id = {o["observation_id"]: o for o in catalogue}
    for row in ordinary:
        if (
            not row["route"].startswith("range:")
            and row["route"] != "primary"
            and any(by_id[k]["provider"] != row["route"] for k in row["observation_ids"])
        ):
            raise ContractRejection("SPAN_ROUTE_PROVIDER_DRIFT")
    _, actual_ranges, _ = range_observations(request)
    if actual_ranges != routes.get("range_receipts", []):
        raise ContractRejection("SPAN_RANGE_ROUTE_RECEIPT_DRIFT")
    for row in ordinary:
        if row["route"].startswith("range:") and row.get("range_receipt_ids") != [
            r["receipt_id"] for r in actual_ranges if r["route"] == row["route"]
        ]:
            raise ContractRejection("SPAN_RANGE_ROUTE_INCOMPLETE")
    upgrade = routes.get("upgrade")
    if not isinstance(upgrade, Mapping):
        raise ContractRejection("SPAN_UPGRADE_STATE_MISSING")
    if upgrade.get("schema_version") == "argon-native-span-upgrade.v1":
        from .argon_span_termination import validate_upgrade

        validate_upgrade(request, upgrade)
    if upgrade.get("enabled") is True:
        if (
            upgrade.get("status") != "COMPLETED"
            or not upgrade.get("observations")
            or upgrade.get("edit_binding_sha256") != binding["binding_sha256"]
            or upgrade.get("submission_outcome") != "COMPLETED"
            or not isinstance(upgrade.get("native_receipt_sha256"), str)
        ):
            raise ContractRejection("SPAN_UPGRADE_NOT_TERMINAL")
        _hash(upgrade["native_receipt_sha256"])
        accepted = {digest(o["raw_observation"]) for o in catalogue}
        if any(digest(row) not in accepted for row in upgrade["observations"]):
            raise ContractRejection("SPAN_UPGRADE_NATIVE_OBSERVATION_INVALID")
    elif upgrade != {"enabled": False, "status": "DISABLED", "observations": []}:
        raise ContractRejection("SPAN_UPGRADE_DISABLED_STATE_INVALID")
    if upgrade != request.get(
        "span_termination_upgrade", {"enabled": False, "status": "DISABLED", "observations": []}
    ):
        raise ContractRejection("SPAN_UPGRADE_BINDING_DRIFT")
    alignments = scope.get("alignments")
    if not isinstance(alignments, list) or any(not isinstance(a, Mapping) for a in alignments):
        raise ContractRejection("SPAN_ALIGNMENT_REQUIRED")
    aligned = {a.get("observation_id"): a for a in alignments}
    if len(aligned) != len(alignments):
        raise ContractRejection("SPAN_DUPLICATE_ALIGNMENT")
    questions = scope.get("audio_questions")
    if not isinstance(questions, list) or (
        judge.get("needs_audio_requested") is True and not questions
    ):
        raise ContractRejection("SPAN_AUDIO_QUESTIONS_UNLOCATED")
    outside = []
    start, end = binding["codepoint_span"]
    for question in questions:
        interval = question.get("codepoint_span")
        left, right = _interval(interval, len(request["current_cue"]))
        refs = question.get("observation_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or not set(refs) <= ids
            or not isinstance(question.get("reason"), str)
            or not question["reason"].strip()
        ):
            raise ContractRejection("SPAN_QUESTION_EVIDENCE_INVALID")
        annotations = question.get("observation_alignments", [])
        if not isinstance(annotations, list):
            raise ContractRejection("SPAN_QUESTION_EVIDENCE_INVALID")
        for annotation in annotations:
            if not isinstance(annotation, Mapping) or annotation.get("observation_id") not in refs:
                raise ContractRejection("SPAN_QUESTION_EVIDENCE_INVALID")
            observed = by_id[annotation["observation_id"]]
            units = observed["text"].split() if observed["kind"] == "blind_pinyin" else observed["text"]
            first, last = _interval(annotation.get("span"), len(units))
            quote = " ".join(units[first:last]) if observed["kind"] == "blind_pinyin" else units[first:last]
            if annotation.get("quote") != quote:
                raise ContractRejection("SPAN_OBSERVATION_QUOTE_DRIFT")
        if end <= left or right <= start:
            outside.append(_outside_question(binding, request, interval, question["reason"], refs))
        elif not start <= left < right <= end:
            raise ContractRejection("SPAN_QUESTION_CROSSES_TARGET_BOUNDARY")
    target = binding["replacement"]
    foreign = bool(re.search(r"[A-Za-z\u3040-\u30ff]", target))
    from src.autoslice.witness_self_consistency import bound_witness_assessment
    inconsistent = bound_witness_assessment(request, witness)['status'] == 'SELF_INCONSISTENT'
    comparable = [o for o in catalogue if not ((foreign or inconsistent) and o["kind"] == "blind_pinyin")]
    excluded = [
        {
            "observation_id": o["observation_id"],
            "reason": ("SELF_INCONSISTENT_WITNESS_CANNOT_SUSTAIN_CONFLICT" if inconsistent
                else "MANDARIN_PHONEMES_NOT_A_FOREIGN_LANGUAGE_CONTRACT"),
        }
        for o in catalogue
        if o not in comparable
    ]
    if foreign and not any(
        (
            o["kind"] == "original_language"
            and o["language"]
            in ({"ja", "mixed"} if re.search(r"[\u3040-\u30ff]", target) else {"en", "mixed"})
        )
        or (
            o["kind"] == "argon_native_transcript"
            and bool(
                re.search(
                    r"[\u3040-\u30ff]" if re.search(r"[\u3040-\u30ff]", target) else r"[A-Za-z]",
                    o["text"],
                )
            )
        )
        for o in comparable
    ):
        raise ContractRejection("SPAN_NATIVE_FOREIGN_LANGUAGE_REQUIRED")
    relevant_aligned = set(aligned)
    if inconsistent:
        relevant_aligned -= {o['observation_id'] for o in catalogue if o['kind'] == 'blind_pinyin'}
    if not comparable or relevant_aligned != {o["observation_id"] for o in comparable}:
        raise ContractRejection("SPAN_ALL_RELEVANT_OBSERVATIONS_REQUIRED")
    comparisons = []
    for observation in comparable:
        alignment = aligned[observation["observation_id"]]
        value = observation["text"]
        blind = observation["kind"] == "blind_pinyin"
        units = value.split() if blind else value
        left, right = _interval(alignment.get("span"), len(units))
        quote = " ".join(units[left:right]) if blind else units[left:right]
        if quote != alignment.get("quote"):
            raise ContractRejection("SPAN_OBSERVATION_QUOTE_DRIFT")
        before = " ".join(units[:left]) if blind else units[:left]
        after = " ".join(units[right:]) if blind else units[right:]
        if (start == 0 and _speech(before)) or (
            end == len(request["current_cue"]) and _speech(after)
        ):
            raise ContractRejection("SPAN_TARGET_EDGE_HAS_UNASSIGNED_PHONEMES")
        if blind:
            uncertain = observation["raw_observation"].get("uncertain_positions", [])
            if any(left <= i < right for i in uncertain) or "?" in units[left:right]:
                raise ContractRejection("SPAN_TARGET_PHONEMES_UNCERTAIN")
            expected, actual = _mandarin(target), tuple(units[left:right])
            basis = "ORIGINAL_BLIND_MANDARIN_PHONEMES"
        elif foreign:
            if observation["kind"] != "original_language" and not re.search(
                r"[A-Za-z\u3040-\u30ff]", quote
            ):
                raise ContractRejection("SPAN_WRONG_LANGUAGE_TRANSCRIPT")
            expected, actual = (_speech(target),), (_speech(quote),)
            basis = "NATIVE_ORIGINAL_LANGUAGE_LITERAL_AGREEMENT_NO_PINYIN_INFERRED"
        else:
            expected, actual = _mandarin(target), _mandarin(quote)
            basis = "PINYIN_DERIVED_FROM_BOUND_TRANSCRIPTION_NOT_RAW_PHONEMES"
        # A duplicated occurrence cannot be chosen by probability or first-hit.
        if blind:
            locations = [
                i
                for i in range(len(units) - len(actual) + 1)
                if tuple(units[i : i + len(actual)]) == actual
            ]
        else:
            locations = [m.start() for m in re.finditer("(?=" + re.escape(quote) + ")", value)]
        if not blind and not foreign:
            from src.autoslice.closed_set_proposal_rebuild import _pinyin

            # Ambiguity search must not require a Mandarin-only *whole cue*.
            # Unsupported-language units delimit the search; they neither
            # become Mandarin phonemes nor veto an already validated target.
            # Keep unknown/numeric tokens as barriers, and count homophonic
            # repeats across all Mandarin regions rather than selecting one.
            phonetic_locations = []
            for region, part in enumerate(re.split(r"[A-Za-z\u3040-\u30ff]+", value)):
                all_tokens = tuple(_pinyin(part).split())
                phonetic_locations.extend(
                    (region, i)
                    for i in range(len(all_tokens) - len(actual) + 1)
                    if all_tokens[i : i + len(actual)] == actual
                )
            if len(phonetic_locations) != 1:
                raise ContractRejection("SPAN_OBSERVATION_LOCATION_AMBIGUOUS")
        if len(locations) != 1:
            raise ContractRejection("SPAN_OBSERVATION_LOCATION_AMBIGUOUS")
        for interval, old, observed in (
            ([0, start], request["current_cue"][:start], before),
            ([end, len(request["current_cue"])], request["current_cue"][end:], after),
        ):
            if interval[0] == interval[1]:
                continue
            if blind:
                try:
                    same = _mandarin(old) == tuple(observed.split())
                except ValueError:
                    same = False
            else:
                same = _speech(old) == _speech(observed)
            if not same:
                differences = [(0, len(old))]
                if not blind:
                    differences = []
                    for tag, a, b, c, d in difflib.SequenceMatcher(
                        None, old, observed, autojunk=False
                    ).get_opcodes():
                        if tag == "equal" or _speech(old[a:b]) == _speech(observed[c:d]):
                            continue
                        if tag == "insert":
                            if (interval[0] == end and a == 0) or (
                                interval[1] == start and a == len(old)
                            ):
                                raise ContractRejection("SPAN_ADJACENT_PHONEMES_AMBIGUOUS")
                            # Locate a question about this existing boundary,
                            # not an invented word or a measured word timestamp.
                            a, b = max(0, a - 1), min(len(old), a + 1)
                        if a < b:
                            differences.append((a, b))
                for a, b in differences:
                    outside.append(
                        _outside_question(
                            binding,
                            request,
                            [interval[0] + a, interval[0] + b],
                            "Bound independent observation differs outside the applied span.",
                            [observation["observation_id"]],
                            [{"current": old, "observed": observed}],
                        )
                    )
        comparisons.append(
            {
                "observation_id": observation["observation_id"],
                "observation_span": [left, right],
                "observed_quote": quote,
                "proposed_span": target,
                "comparison_basis": basis,
                "proposed_units": list(expected),
                "observed_units": list(actual),
                "compatible": expected == actual,
                "physical_audio_window_ms": observation["physical_audio_window_ms"],
                "source_media_sha256": observation["source_media_sha256"],
                "audio_clip_sha256": observation["audio_clip_sha256"],
                "response_sha256": observation["response_sha256"],
                "word_timing_measured": False,
            }
        )
    support_summary = None
    if not all(row["compatible"] for row in comparisons):
        # Relax only when every complete, valid target reading actually rejects
        # CURRENT. Missing words/unknown routes/invalid observations are not a
        # negative vote, and no context-only candidate is manufactured here.
        current_span = request["current_cue"][start:end]
        current_units = (_speech(current_span),) if foreign else _mandarin(current_span)
        current_supported = any(tuple(row["observed_units"]) == current_units for row in comparisons)
        if current_supported:
            raise ContractRejection("SPAN_TARGET_PHONEMES_INCOMPATIBLE")
        def merely_omitted(units):
            iterator = iter(current_units)
            return len(units) < len(current_units) and all(any(u == v for v in iterator) for u in units)
        if any(merely_omitted(tuple(row["observed_units"])) for row in comparisons):
            raise ContractRejection("SPAN_CURRENT_REJECTION_NOT_ESTABLISHED")
        if any(isinstance(raw, Mapping) and raw.get("status") != "OBSERVED"
               for raw in witness.get("independent_observations", [])):
            raise ContractRejection("SPAN_UNRESOLVED_OBSERVATION_NOT_A_REJECTION")
        # Rewrapped/replayed/range copies do not add votes. Multiple distinct
        # readings from one provider/model are retained but cast no extra vote.
        by_observation = {o["observation_id"]: o for o in comparable}
        ballots = {}
        for row in comparisons:
            o = by_observation[row["observation_id"]]
            route = (o["provider"], o["model"])
            ballots.setdefault(route, set()).add(tuple(row["observed_units"]))
        tally = {}
        for readings in ballots.values():
            if len(readings) == 1:
                reading = next(iter(readings))
                tally[reading] = tally.get(reading, 0) + 1
        proposed_units = tuple(comparisons[0]["proposed_units"])
        votes = tally.get(proposed_units, 0)
        if not votes or votes < max(tally.values(), default=0):
            raise ContractRejection("SPAN_PROPOSED_NOT_ACOUSTIC_PLURALITY")
        support_summary = {
            "current_supported": False, "current_units": list(current_units),
            "proposed_units": list(proposed_units), "proposed_route_votes": votes,
            "independent_route_count": len(ballots),
            "route_ballots": [{"provider": key[0], "model": key[1],
                               "readings": [list(r) for r in sorted(readings)],
                               "counted": len(readings) == 1}
                              for key, readings in sorted(ballots.items())],
            "support_counts": [{"units": list(k), "routes": v} for k, v in sorted(tally.items())],
            "tie_break": "EXPLICIT_CPA_CONTEXT_CHOICE_AMONG_MAXIMALLY_SUPPORTED_READINGS",
            "dissent_retained": True,
        }
    unique = {q["question_sha256"]: q for q in outside}
    result = {
        "schema_version": SCHEMA,
        "resolution": POLICY,
        "status": "TARGET_RESOLVED",
        "binding": binding,
        "context_basis": scope["context_basis"],
        "comparisons": comparisons,
        "excluded_protocol_observations": excluded,
        "outside_findings": list(unique.values()),
        "route_state": deepcopy(dict(routes)),
        "raw_needs_audio": judge.get("needs_audio"),
        "request": deepcopy(dict(request)),
        "witness": deepcopy(dict(witness)),
        "judge": deepcopy(dict(judge)),
        "publication_authorized": False,
        "scope": "EXACT_CODEPOINT_REPLACEMENT_ONLY",
    }
    if support_summary is not None:
        result["decision_rule"] = "UNSUPPORTED_CURRENT_ACOUSTIC_PLURALITY_WITH_CONTEXT"
        result["support_summary"] = support_summary
    result["receipt_sha256"] = digest(result)
    return result


@cold_input
def validated_resolution(
    value: Mapping,
    *,
    current: str | None = None,
    proposed: str | None = None,
    window: tuple[int, int] | None = None,
    srt_text: str | None = None,
) -> dict:
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA:
        raise ContractRejection("SPAN_RESOLUTION_MISSING")
    rebuilt = compile_resolution(
        value["request"], value["witness"], value["judge"], value["route_state"]
    )
    if rebuilt != value:
        raise ContractRejection("SPAN_RESOLUTION_DRIFT")
    request = value["request"]
    if (
        (current is not None and current != request["current_cue"])
        or (proposed is not None and proposed != request["proposed_cue"])
        or (window is not None and list(window) != value["binding"]["cue_window_ms"])
        or (srt_text is not None and text_sha(srt_text) != value["binding"]["srt_sha256"])
    ):
        raise ContractRejection("SPAN_RESOLUTION_CURRENT_INPUT_DRIFT")
    return rebuilt


def marked(adjudication: Mapping) -> bool:
    return (
        FIELD in adjudication
        or adjudication.get("policy_branch") == POLICY
        or (adjudication.get("mutation_authority") or {}).get("basis") == POLICY
    )


def valid_adjudication(adjudication: Mapping, **kwargs) -> bool:
    try:
        value = validated_resolution(adjudication.get(FIELD), **kwargs)
        return (
            adjudication.get("policy_branch") == POLICY
            and adjudication.get("request") == value["request"]
            and (adjudication.get("witness_judge") or {}).get("judge") == value["judge"]
            and adjudication.get("mutation_authority")
            == {
                "schema_version": "subtitle-correction-mutation-authority.v1",
                "status": "PASS",
                "basis": POLICY,
            }
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def outside_question_packet(request: Mapping, witness: Mapping, judge: Mapping) -> dict | None:
    """Retain independently located questions even when the target cannot apply.

    This packet proves only that an actual scoped reviewer raised a question;
    it is never a phoneme-fit decision or mutation/release authority.
    """
    binding = edit_binding(request)
    raw = judge.get("span_review_raw_completion")
    if binding is None or not isinstance(raw, str):
        return None
    from src.autoslice.llm_client import extract_json_object

    payload = extract_json_object(raw)
    scope = payload.get("span_review")
    if (
        judge.get("status") != "JUDGED"
        or text_sha(raw) != judge.get("completion_sha256")
        or digest({k: v for k, v in request.items() if k != "request_sha256"})
        != request.get("request_sha256")
        or judge.get("check_request_sha256") != request.get("request_sha256")
        or not isinstance(scope, Mapping)
        or scope != judge.get("span_review")
        or scope.get("binding_sha256") != binding["binding_sha256"]
        or scope.get("request_sha256") != request.get("request_sha256")
    ):
        raise ContractRejection("SPAN_PENDING_SCOPE_DRIFT")
    observations, _ = observation_catalogue(request, witness)
    observed = {o["observation_id"]: o for o in observations}
    start, end = binding["codepoint_span"]
    outside = []
    for question in scope.get("audio_questions") or []:
        a, b = _interval(question.get("codepoint_span"), len(request["current_cue"]))
        if not (b <= start or a >= end):
            continue
        refs = question.get("observation_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or not set(refs) <= set(observed)
            or any(
                observed[i]["source_media_sha256"]
                != _hash(request.get("span_termination_source_sha256"))
                for i in refs
            )
            or not isinstance(question.get("reason"), str)
            or not question["reason"].strip()
        ):
            raise ContractRejection("SPAN_PENDING_SOURCE_INVALID")
        outside.append(_outside_question(binding, request, [a, b], question["reason"], refs))
    if not outside:
        return None
    packet = {
        "schema_version": "subtitle-span-outside-questions.v1",
        "status": "UNRESOLVED",
        "binding": binding,
        "outside_findings": outside,
        "request": deepcopy(dict(request)),
        "witness": deepcopy(dict(witness)),
        "judge": deepcopy(dict(judge)),
        "mutation_authorized": False,
        "publication_authorized": False,
    }
    packet["receipt_sha256"] = digest(packet)
    return packet


@cold_input
def validate_question_packet(packet: Mapping) -> dict:
    if not isinstance(packet, Mapping):
        raise ContractRejection("SPAN_PENDING_PACKET_INVALID")
    actual = outside_question_packet(packet["request"], packet["witness"], packet["judge"])
    if actual is None or actual != packet:
        raise ContractRejection("SPAN_PENDING_PACKET_DRIFT")
    return actual


def minimal_edit_binding(request: dict, finding: Mapping) -> dict | None:
    """Shrink only text-equal flanks of an already valid single replacement.

    This is codepoint scope, not an acoustic timestamp or approval. Validate
    the original declared span first so an ambiguous/stale proposal stays bad.
    Insertions/deletions keep their existing separate owners.
    """
    original = edit_binding(request, finding)
    if original is None:
        return None
    old, new = original["current_span"], original["replacement"]
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(old), len(new)) - prefix
           and old[len(old) - 1 - suffix] == new[len(new) - 1 - suffix]):
        suffix += 1
    old_end, new_end = len(old) - suffix, len(new) - suffix
    if prefix >= old_end or prefix >= new_end:
        return original
    start = original["codepoint_span"][0] + prefix
    end = original["codepoint_span"][1] - suffix
    if prefix or suffix:
        request["declared_edit_scope"] = original
        request["suspect"], request["replacement"] = old[prefix:old_end], new[prefix:new_end]
    request["span_localization_contract"] = "located-or-explicitly-unlocatable.v1"
    return edit_binding(request, {"span_start_codepoint": start, "span_end_codepoint": end})


def compile_judge_scope(request: Mapping, witness: Mapping, judge: Mapping) -> dict:
    """Seal an outcome for this original judge, including text-first/cache returns.

    A blocked diagnostic never revokes a separate valid text-only authority.
    The full original request/witness are retained for an independent cold
    consumer; no missing alignment, raw flag, timestamp or observation is made.
    """
    bare = {k: v for k, v in judge.items() if k != "span_compilation"}
    required = request.get("span_localization_contract") == "located-or-explicitly-unlocatable.v1"
    has_raw = isinstance(bare.get("span_review_raw_completion"), str)
    missing_legacy_raw = bare.get("raw_reply_status") == "LEGACY_RAW_UNAVAILABLE"
    if not isinstance(bare.get("span_review"), Mapping) and not (has_raw or missing_legacy_raw or (required and bare.get("status") == "JUDGED")):
        return dict(judge)
    bare["span_compilation_required"] = True
    resolution, reason = None, None
    not_applicable = not required and not isinstance(bare.get("span_review"), Mapping)
    try:
        if missing_legacy_raw:
            raise ContractRejection("SPAN_LEGACY_RAW_UNAVAILABLE")
        if not_applicable:
            raise ContractRejection("SPAN_NOT_APPLICABLE_NONREPLACEMENT")
        if not isinstance(bare.get("span_review"), Mapping):
            raise ContractRejection("SPAN_LOCALIZATION_RESULT_REQUIRED")
        resolution = compile_resolution(request, witness, bare,
            route_state(request, witness, request.get("independent_asr_evidence"), None))
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        reason = str(exc)
    outcome = {"schema_version": "span-judge-compilation.v1",
        "compiler_revision": "phase-consumption.v3",
        **({"scope_status": "NOT_APPLICABLE"} if not_applicable else {}),
        "request": deepcopy(dict(request)), "witness": deepcopy(dict(witness)),
        "request_sha256": request.get("request_sha256"),
        "completion_sha256": bare.get("completion_sha256"),
        "judge_sha256": digest(bare), "resolution": resolution,
        "blocked_reason": reason, "mutation_authorized": False}
    outcome["outcome_sha256"] = digest(outcome)
    return {**bare, "span_compilation": outcome}


@cold_input
def validate_persisted_judge_scopes(value: object) -> int:
    """Replay per-judge outcomes from persisted review/typed history, no models."""
    from src.autoslice.continuation_subchecks import session_memo

    # Within one producer cold read, identical persisted judge content that
    # already replayed exactly is not recompiled by the next ingress validator.
    replayed = session_memo("span_judge_scope_replay")
    seen = set()
    def visit(node):
        if isinstance(node, Mapping):
            from src.autoslice.span_judgment_history import validate_history
            validate_history(node)
            outcome = node.get("span_compilation")
            if node.get("span_compilation_required") is True and outcome is None:
                raise ContractRejection("SPAN_COMPILATION_REQUIRED")
            if outcome is not None:
                if not isinstance(outcome, Mapping) or outcome.get("schema_version") != "span-judge-compilation.v1":
                    raise ContractRejection("SPAN_COMPILATION_SCHEMA_INVALID")
                key = digest(node)
                if key not in seen and replayed is not None and key in replayed:
                    seen.add(key)
                if key not in seen:
                    bare = {k: v for k, v in node.items() if k != "span_compilation"}
                    if (outcome.get("judge_sha256") != digest(bare)
                        or outcome.get("outcome_sha256") != digest({k:v for k,v in outcome.items() if k != "outcome_sha256"})):
                        raise ContractRejection("SPAN_COMPILATION_IDENTITY_DRIFT")
                    historical = outcome.get("compiler_revision") in {None, "durable-native-routes.v2"}
                    if historical and outcome.get("resolution") is None:
                        # Historical negative diagnostics retain original identities
                        # and never grant a repair. A newer native adapter can give
                        # a different refusal reason; do not mutate that old record.
                        raw = bare.get("span_review_raw_completion")
                        if (not isinstance(outcome.get("blocked_reason"), str)
                                or not outcome["blocked_reason"]
                                or not isinstance(raw, str)
                                or text_sha(raw) != outcome.get("completion_sha256")
                                or outcome.get("request_sha256") != bare.get("check_request_sha256")
                                or digest({k:v for k,v in outcome["request"].items() if k != "request_sha256"})
                                    != outcome.get("request_sha256")):
                            raise ContractRejection("SPAN_LEGACY_NEGATIVE_IDENTITY_INVALID")
                    else:
                        expected = compile_judge_scope(outcome["request"], outcome["witness"], bare)
                        if historical:
                            legacy = expected.get("span_compilation", {})
                            legacy.pop("compiler_revision", None)
                            if outcome.get("compiler_revision") is not None:
                                legacy["compiler_revision"] = outcome["compiler_revision"]
                            legacy.pop("outcome_sha256", None)
                            legacy["outcome_sha256"] = digest(legacy)
                        if expected.get("span_compilation") != outcome:
                            raise ContractRejection("SPAN_COMPILATION_REPLAY_DRIFT")
                    seen.add(key)
                    if replayed is not None:
                        replayed[key] = True
            for name, item in node.items():
                if name != "span_compilation":
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)
    visit(value)
    return len(seen)
