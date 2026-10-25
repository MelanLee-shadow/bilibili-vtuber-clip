"""Explicit, source-bound assistance for a subsequent real audio observation.

This is an evidence protocol, not a subtitle writer or a second retry ledger.
Blind requests remain geometry-only. Assistance and its influenced response
are one dependent observation; neither can manufacture an independent witness.
"""
from __future__ import annotations

from .review_errors import ContractRejection

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType

from src.autoslice.jingting_chunker import parse_srt_cues

REQUEST_SCHEMA = "context-assisted-audio-request.v1"
ASSISTANCE_SCHEMA = "context-assisted-audio-input.v1"
OBSERVATION_SCHEMA = "context-assisted-audio-observation.v1"
OBSERVATION_CLASS = "CONTEXT_ASSISTED_AUDIO"
PROMPT_CONTRACT = "context-assisted-complete-block.v1"


@dataclass(frozen=True)
class AssistancePlanContract:
    catalogue: Mapping
    providers: tuple = ()
    assistance_providers: tuple = ()
    hypothesis_fields = (("CURRENT", "current_cue"), ("PROPOSED", "proposed_cue"))
    source_kind = "CURRENT_SUBTITLE_CONTEXT_NOT_TRUTH"
    hypothesis_role = "HYPOTHESIS_NOT_ANSWER"
    authority = MappingProxyType({"schema_version": ASSISTANCE_SCHEMA, "observation_class": OBSERVATION_CLASS,
                                  "independent_witness": False, "mutation_authorized": False})
    @property
    def name_fields(self):
        return frozenset(self.response_schema['properties']['proper_names']['items']['properties'])

    @property
    def prompt_fragment(self):
        # Render the plan schema's actual name fields; the prompt has no
        # separately maintained proper-name object shape.
        fields=','.join(self.response_schema['properties']['proper_names']['items']['properties'])
        return (
            "首次必须 CANDIDATE_BLIND_AUDIO；后续可选 CONTEXT_ASSISTED_AUDIO，使用同一个原audio附件"
            "和 context_refs 所引当前字幕语境、proper_names[{"+fields+"}]、question，"
            "context_summary须忠于引用来源；可显式 include_candidates=true。辅助文本不是答案，"
            "与可听原音冲突时以音频为准，不确定就保留 uncertain。仅 assistance_providers 可使用此协议。\n")

    @property
    def response_schema(self):
        from .response_schemas import obj, array, enum, S, B
        return obj({"context_refs": array(enum(*self.catalogue)), "question": S,
                    "context_summary": S, "proper_names": array(obj({
                        "text": S, "source_ref": enum(*self.catalogue)}, closed=True)),
                    "include_candidates": B}, required=["context_refs", "question"])

    def provider_available(self, provider):
        return provider in self.assistance_providers

    def valid_name(self, item, sources, *, closed=False, strip=False):
        return (isinstance(item, Mapping)
                and (not closed or set(item) == self.name_fields)
                and item.get("source_ref") in sources
                and isinstance(item.get("text"), str)
                and bool(item["text"].strip() if strip else item["text"])
                and item["text"] in sources[item["source_ref"]]["text"])


def assistance_plan_contract(check=None, *, providers=(), assistance_providers=()):
    """One source catalogue and protocol for the planner, writer and replay."""
    return AssistancePlanContract(source_catalogue(check) if check is not None else {},
                                  tuple(providers), tuple(assistance_providers))


@dataclass(frozen=True)
class AssistedResponseContract:
    assistance: Mapping | None = None

    @property
    def response_schema(self):
        from .response_schemas import assisted_audio_schema
        schema = assisted_audio_schema()
        if self.assistance is not None:
            schema['properties']['inferred']['items']['properties']['source_refs']['items'] = {
                'type':'string', 'enum':list(self.assistance['sources'])}
        return schema

    @property
    def version(self):
        return self.response_schema["properties"]["schema_version"]["enum"][0]

    def block_heard(self, raw):
        heard, full = raw.get("heard"), raw.get("full_transcript")
        if not isinstance(heard, str) or not isinstance(full, str):
            raise ContractRejection("ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID")
        if heard == full:
            return heard, None
        segments = [row for row in raw.get("segments", []) if isinstance(row, Mapping)]
        if (heard.strip() and full.strip() and heard in full and segments
                and all(row.get("transcript") == full for row in segments)):
            return full, heard
        raise ContractRejection("ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID")

    def valid_inference(self, item):
        return (isinstance(item, Mapping) and isinstance(item.get("text"), str)
                and bool(item["text"].strip()) and isinstance(item.get("source_refs"), list)
                and bool(item["source_refs"])
                and all(ref in self.assistance["sources"] for ref in item["source_refs"]))

    def prompt_fragment(self, duration_ms):
        # The example's citation must be one of the materials actually offered,
        # rather than an independently handwritten cue:1 that may not exist.
        source_ref = next(iter(self.assistance['sources']))
        from .contract_schema_projection import schema_example
        schema=self.response_schema
        example=schema_example(schema)
        example.update(heard='听到的完整语义块',full_transcript='听到的完整语义块')
        inference=schema_example(schema['properties']['inferred']['items'])
        inference.update(text='有来源的语境解释',source_refs=[source_ref])
        example['inferred']=[inference]
        segment=schema_example(schema['properties']['segments']['items'])
        segment.update(start_ms=0,end_ms=duration_ms,transcript=example['heard'])
        example['segments']=[segment]
        return '\nReturn exactly one JSON object: ' + json.dumps(example,ensure_ascii=False,separators=(',',':')) + '.\n'


def assisted_response_contract(assistance=None):
    return AssistedResponseContract(assistance)


def digest(value):
    from .canonical_json_hash import canonical_sha256
    return canonical_sha256(value)


def text_sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def anchor(check):
    """Candidate wording is not the physical target identity."""
    clip = check.get("clip_context_binding") or {}
    start, end, offset = (check.get(k) for k in
                          ("matched_start_ms", "matched_end_ms", "source_media_timeline_offset_ms"))
    if (any(type(x) is not int for x in (start, end, offset))
            or start + offset < 0 or end <= start or offset < 0):
        raise ContractRejection("ASSISTED_AUDIO_CLOCK_INVALID")
    sources = sorted(str(x).removeprefix("sha256:") for x in clip.get("source_media_sha256s", []))
    if not sources or not clip.get("candidate_id"):
        raise ContractRejection("ASSISTED_AUDIO_SOURCE_BINDING_REQUIRED")
    return {"candidate_id": clip["candidate_id"], "source_media_sha256s": sources,
            "physical_target_ms": [start + offset, end + offset],
            "source_media_timeline_offset_ms": offset,
            "current_srt_sha256": text_sha(check["whole_clip_current_srt"]),
            "current_cue_sha256": text_sha(check["current_cue"]),
            "clip_context_binding": deepcopy(dict(clip))}


def source_catalogue(check):
    """Only ordinary current context enters; no human truth/answer ledger."""
    sources = {}
    for index, cue in enumerate(parse_srt_cues(check["whole_clip_current_srt"]), 1):
        if cue.text.strip():
            sources["cue:" + str(index)] = {
                "source_kind": AssistancePlanContract.source_kind, "text": cue.text,
                "start_ms": cue.start_ms, "end_ms": cue.end_ms,
                "text_sha256": text_sha(cue.text)}
    for key in ("context_before", "context_after"):
        value = check.get(key)
        if isinstance(value, str) and value.strip():
            sources[key] = {"source_kind": AssistancePlanContract.source_kind,
                            "text": value, "text_sha256": text_sha(value)}
    return sources


def build_assistance(check, plan, *, blind_observation_ids):
    contract = assistance_plan_contract(check)
    catalogue = contract.catalogue
    refs = plan.get("context_refs")
    if (not isinstance(refs, list) or not refs or len(refs) != len(set(refs))
            or any(not isinstance(ref, str) or ref not in catalogue for ref in refs)):
        raise ContractRejection("ASSISTED_AUDIO_CONTEXT_REFS_INVALID")
    question = plan.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ContractRejection("ASSISTED_AUDIO_QUESTION_REQUIRED")
    summary = plan.get("context_summary", "")
    if not isinstance(summary, str):
        raise ContractRejection("ASSISTED_AUDIO_SUMMARY_INVALID")
    names = plan.get("proper_names", [])
    if not isinstance(names, list):
        raise ContractRejection("ASSISTED_AUDIO_NAMES_INVALID")
    selected = {ref: catalogue[ref] for ref in sorted(refs)}
    normalized_names = []
    for item in names:
        if not contract.valid_name(item, selected, closed=True, strip=True):
            raise ContractRejection("ASSISTED_AUDIO_NAME_SOURCE_MISMATCH")
        normalized_names.append(dict(item))
    hypotheses = []
    if plan.get("include_candidates") is True:
        # The original supplied hypotheses remain labelled and byte-bound;
        # later candidate reconstruction cannot retroactively change them.
        for label, field in contract.hypothesis_fields:
            text = check.get(field)
            if isinstance(text, str) and text:
                hypotheses.append({"label": label, "text": text, "text_sha256": text_sha(text),
                                   "role": contract.hypothesis_role})
    if not blind_observation_ids:
        raise ContractRejection("ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND")
    result = {
        **contract.authority,
        "anchor": anchor(check), "sources": selected, "context_summary": summary,
        "summary_role": "INTERPRETATION_OF_CITED_SOURCES_NOT_ANSWER",
        "proper_names": normalized_names, "question": question.strip(), "hypotheses": hypotheses,
        "prior_blind_observation_ids": sorted(set(blind_observation_ids)),
    }
    result["assistance_sha256"] = digest(result)
    return result


# Representation-only anchor fields: the whole-SRT bytes, the local clock
# (41 「本地 offset 与整片 SRT 字节只是表示」) and the whole clip-context record
# (context/draft-SRT/speech-memory hashes), on one clock or across a
# source/delivery clock change (G6; admin).  The physical target
# window, the source binding (candidate_id + source_media_sha256s, separate
# anchor fields; the piece clock where the binding carries one) and the
# current cue stay compared; rendered sources are compared one by one by the
# caller.
_ANCHOR_REPRESENTATION_FIELDS = ("current_srt_sha256", "source_media_timeline_offset_ms",
                                 "clip_context_binding")


def _text_only_revision(recorded, current, original_srt, check):
    """41:1489-1492 and 41 源/交付时钟: one physical target, another representation.

    The assisted observation consumed the rendered sources, current cue and
    hypotheses, not the whole SRT. A revision elsewhere, or the same clip on
    another local clock (padded 0 vs delivery 9800), keeps it when the bound
    original SRT has the same cue grid on the source clock; the rendered
    sources are then compared one by one against the current catalogue by the
    caller.
    """
    if (not isinstance(recorded, Mapping) or not isinstance(original_srt, str)
            or recorded.get("current_srt_sha256") != text_sha(original_srt)):
        return False
    from src.autoslice.audio_question_identity import piece_clock
    pieces = [piece_clock(anchor.get("clip_context_binding") or {})
              if isinstance(anchor.get("clip_context_binding"), Mapping) else None
              for anchor in (recorded, current)]
    if pieces[0] != pieces[1]:
        return False
    if ({k: v for k, v in recorded.items() if k not in _ANCHOR_REPRESENTATION_FIELDS}
            != {k: v for k, v in current.items() if k not in _ANCHOR_REPRESENTATION_FIELDS}):
        return False
    from src.autoslice.independent_context_adjudication import same_physical_cue_grid
    return same_physical_cue_grid(
        original_srt, recorded.get("source_media_timeline_offset_ms"),
        check["whole_clip_current_srt"], current.get("source_media_timeline_offset_ms"))


def _on_current_clock(item, shift):
    """A recorded cue source re-timed by the local-clock difference only."""

    if not shift or not isinstance(item, Mapping):
        return item
    moved = dict(item)
    for key in ("start_ms", "end_ms"):
        if type(moved.get(key)) is int:
            moved[key] = moved[key] + shift
    return moved


def validate_assistance(assistance, check=None, *, original_srt=None):
    contract = assistance_plan_contract()
    if not isinstance(assistance, Mapping):
        raise ContractRejection("ASSISTED_AUDIO_INPUT_INVALID")
    value = dict(assistance)
    declared = value.pop("assistance_sha256", None)
    if declared != digest(value):
        raise ContractRejection("ASSISTED_AUDIO_CONTEXT_HASH_DRIFT")
    if (any(value.get(k) != v if type(v) is not bool else value.get(k) is not v
            for k, v in contract.authority.items())
            or not value.get("prior_blind_observation_ids")):
        raise ContractRejection("ASSISTED_AUDIO_AUTHORITY_INVALID")
    if (not isinstance(value.get("question"), str) or not value["question"].strip()
            or not isinstance(value.get("context_summary"), str)
            or not isinstance(value.get("proper_names"), list)
            or not isinstance(value.get("hypotheses"), list)
            or not isinstance(value.get("prior_blind_observation_ids"), list)
            or any(not isinstance(x, str) or len(x) != 64
                   or any(c not in "0123456789abcdef" for c in x)
                   for x in value["prior_blind_observation_ids"])):
        raise ContractRejection("ASSISTED_AUDIO_INPUT_INVALID")
    sources = value.get("sources")
    if not isinstance(sources, Mapping) or not sources:
        raise ContractRejection("ASSISTED_AUDIO_CONTEXT_REFS_INVALID")
    for item in sources.values():
        if (not isinstance(item, Mapping) or not isinstance(item.get("text"), str)
                or item.get("text_sha256") != text_sha(item["text"])
                or item.get("source_kind") != contract.source_kind):
            raise ContractRejection("ASSISTED_AUDIO_SOURCE_TEXT_DRIFT")
    if check is not None:
        current_anchor = anchor(check)
        recorded_anchor = value.get("anchor")
        if recorded_anchor != current_anchor and not _text_only_revision(
                recorded_anchor, current_anchor, original_srt, check):
            raise ContractRejection("ASSISTED_AUDIO_CURRENT_CONTEXT_DRIFT")
        catalogue = assistance_plan_contract(check).catalogue
        # Cue sources carry local times; compare them on the source clock.
        shift = (recorded_anchor.get("source_media_timeline_offset_ms", 0)
                 - current_anchor["source_media_timeline_offset_ms"]
                 if isinstance(recorded_anchor, Mapping) else 0)
        if any(catalogue.get(ref) != _on_current_clock(item, shift) for ref, item in sources.items()):
            raise ContractRejection("ASSISTED_AUDIO_CONTEXT_SOURCE_DRIFT")
    for item in value.get("proper_names", []):
        if not contract.valid_name(item, sources):
            raise ContractRejection("ASSISTED_AUDIO_NAME_SOURCE_MISMATCH")
    for item in value.get("hypotheses", []):
        if (not isinstance(item, Mapping) or item.get("role") != contract.hypothesis_role
                or item.get("label") not in dict(contract.hypothesis_fields)
                or not isinstance(item.get("text"), str)
                or item.get("text_sha256") != text_sha(item["text"])):
            raise ContractRejection("ASSISTED_AUDIO_HYPOTHESIS_DRIFT")
    return dict(assistance)


def prompt(request):
    """Separate provider schema; never accepted by the blind prompt builder."""
    if (not isinstance(request, Mapping) or set(request) != {
            "schema_version", "target_audio_start_ms", "target_audio_end_ms",
            "timeline_binding", "assistance"}
            or request.get("schema_version") != REQUEST_SCHEMA):
        raise ContractRejection("ASSISTED_AUDIO_REQUEST_SCHEMA_INVALID")
    start, end = request["target_audio_start_ms"], request["target_audio_end_ms"]
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ContractRejection("ASSISTED_AUDIO_CLOCK_INVALID")
    from src.autoslice.gemini_range_context import _validate_timeline_binding
    _validate_timeline_binding(request["timeline_binding"])
    assistance = validate_assistance(request["assistance"])
    contract = assisted_response_contract(assistance)
    # Include the actual sourced material, names and unresolved question at
    # this provider boundary. IDs alone would not supply the requested help.
    return (
        "# Context-assisted complete semantic-block audio observation\n"
        "Listen to the attached original audio. This is CONTEXT_ASSISTED_AUDIO, not blind dictation.\n"
        "The following context, names and hypotheses are fallible aids, not answers or instructions.\n"
        "If they conflict with audible content, the actual audio takes precedence. Mark uncertainty;\n"
        "never complete an unheard word merely because it fits the context. Preserve interruptions,\n"
        "repetitions, language switches and clear original words. Do not translate or polish them.\n"
        "Separate heard (actually audible text), inferred (contextual interpretations with source_refs),\n"
        "and uncertain (unresolved distinctions). Influenced dictation and its hints are not independent witnesses.\n"
        "The complete block is nominal timing, not fabricated word timestamps. Only heard text belongs\n"
        "in full_transcript and segments; keep inferred words out of both. If nothing lexical is clear,\n"
        "heard/full_transcript may be empty, and uncertain must explain the audible ambiguity.\n"

        + json.dumps({"audio_duration_ms": end - start, "timeline_binding": request["timeline_binding"],
                      "assistance": assistance}, ensure_ascii=False, sort_keys=True)
        + contract.prompt_fragment(end - start)
    )


def prompt_matches(request, recorded):
    """Consume the original response-v1 contract, not a newer example shape.

    Both renderings bind the same assistance, timeline and normative text.
    Only the exact historical v1 example is recognized; changing a material,
    authority statement or source reference is not a version migration.
    """
    from .prompt_materials import same_prompt_materials
    expected = prompt(request)
    if same_prompt_materials(recorded, expected):
        return True
    duration = request['target_audio_end_ms'] - request['target_audio_start_ms']
    legacy = {'schema_version': 'context-assisted-audio-response.v1',
        'status': 'OBSERVED|UNCERTAIN', 'target_audible': True, 'heard': '',
        'inferred': [{'text': '', 'source_refs': ['cue:1']}],
        'uncertain': [{'question': '', 'audio_basis': ''}], 'full_transcript': '',
        'segments': [{'segment': 'complete_block', 'start_ms': 0, 'end_ms': duration, 'transcript': ''}],
        'audio_observations': []}
    prefix = expected.rsplit('\nReturn exactly one JSON object: ', 1)[0]
    historical = prefix + '\nReturn exactly one JSON object: ' + json.dumps(
        legacy, ensure_ascii=False, separators=(',', ':')) + '.\n'
    return same_prompt_materials(recorded, historical)


def split_text_notes(raw):
    """Separate free-text hearing notes from structured sound events.

    The assisted prompt template lists ``audio_observations: []`` without the
    blind-range sound-event schema, so providers answer with textual hearing
    notes. Those notes are assisted (non-independent) observations, kept on
    record; they are never sound events, text or timing. Mixed or empty-string
    lists are left unchanged for the ordinary validator to reject.
    """
    rows = raw.get("audio_observations") if isinstance(raw, Mapping) else None
    if (isinstance(rows, list) and rows
            and all(isinstance(row, str) and row.strip() for row in rows)):
        return {**raw, "audio_observations": []}, [row.strip() for row in rows]
    return raw, []


def _block_heard(raw) -> tuple[str, str | None]:
    """The block's heard text; a target-only ``heard`` excerpt is not a conflict.

    The prompt does not state that heard must repeat the whole block (its bytes
    stay unchanged so cached prompts are never re-bought). A
    reply whose full_transcript equals every complete-block segment transcript
    and contains its ``heard`` as a contiguous excerpt describes one heard
    block: that block text is the observation, the excerpt stays on record.
    Anything else keeps the original schema rejection.
    """
    return assisted_response_contract().block_heard(raw)


def validate_response(raw, assistance):
    validate_assistance(assistance)
    contract = assisted_response_contract(assistance)
    if (not isinstance(raw, Mapping) or raw.get("schema_version") != contract.version
            or not isinstance(raw.get("inferred"), list) or not isinstance(raw.get("uncertain"), list)):
        raise ContractRejection("ASSISTED_AUDIO_RESPONSE_SCHEMA_INVALID")
    block_heard, target_excerpt = contract.block_heard(raw)
    raw, text_notes = split_text_notes({**raw, "heard": block_heard})
    if any(row.get("transcript") != raw["heard"] for row in raw.get("segments", [])
           if isinstance(row, Mapping)):
        raise ContractRejection("ASSISTED_AUDIO_INFERENCE_IN_TRANSCRIPT")
    for item in raw["inferred"]:
        if not contract.valid_inference(item):
            raise ContractRejection("ASSISTED_AUDIO_INFERENCE_SOURCE_INVALID")
    for item in raw["uncertain"]:
        if (not isinstance(item, Mapping) or any(not isinstance(item.get(k), str) or not item[k].strip()
                                                for k in ("question", "audio_basis"))):
            raise ContractRejection("ASSISTED_AUDIO_UNCERTAINTY_INVALID")
    if (not raw["heard"].strip() and not raw["uncertain"] and not raw.get("audio_observations")
            and not text_notes):
        raise ContractRejection("ASSISTED_AUDIO_NO_USABLE_OBSERVATION")
    result = {"heard": raw["heard"], "inferred": deepcopy(raw["inferred"]),
              "uncertain": deepcopy(raw["uncertain"]),
              "context_sources": deepcopy(assistance["sources"]),
              "assistance_sha256": assistance["assistance_sha256"],
              "independent_witness": False}
    if target_excerpt is not None:
        result["target_heard_excerpt"] = target_excerpt
    if text_notes:
        result["assisted_audio_notes"] = text_notes
    return result


def validate_prior_blind(check, rows=None):
    """Revalidate the real saved observations; a caller-supplied count is useless."""
    from src.autoslice.independent_context_adjudication import validate_range_acoustic_observation
    identifiers = []
    # Reuse the existing raw/source/native validators. Exclude ranges here
    # to avoid recursively validating this assisted observation as its own
    # prerequisite.
    from src.autoslice.span_phoneme_resolution import observation_catalogue
    narrow_check = {k: v for k, v in check.items()
                    if k != "context_only_range_acoustic_observations"}
    narrow, _ = observation_catalogue(narrow_check, {})
    for item in narrow:
        if item.get("kind") in {"blind_pinyin", "native_transcript", "original_language"}:
            if item["kind"] == "blind_pinyin":
                from src.autoslice.entity_witness_cache_probe import validate_relisten_witness_artifacts
                validate_relisten_witness_artifacts(request=item["witness_request"],
                                                   witness=item["raw_observation"])
            physical = anchor(check)
            recorded = item["source_media_sha256"] in physical["source_media_sha256s"]
            if not recorded and item["kind"] != "blind_pinyin":
                # A native/original-language narrow row heard on the producer
                # media carries no media path to prove recording ancestry here.
                # It is simply not counted as a prior blind observation; it can
                # neither block nor manufacture the assisted prerequisite.
                continue
            # The catalogue has already reopened blind artifacts, checked the
            # recording ancestry and bound the physical target. Rebuilding its
            # ancestry/coverage test here produced an unreachable rejection;
            # invalid rows never enter `narrow` and cannot establish this prior.
            identifiers.append(digest({
                "source_media_sha256": item["source_media_sha256"],
                "audio_clip_sha256": item["audio_clip_sha256"],
                "raw_response_sha256": item["response_sha256"]}))
    for row in (rows if rows is not None else check.get("context_only_range_acoustic_observations", [])):
        if not isinstance(row, Mapping) or row.get("candidate_blind") is not True:
            continue
        valid, _ = validate_range_acoustic_observation(check["whole_clip_current_srt"], check, row)
        if valid and valid.get("candidate_blind") is True and valid.get("status") in {"OBSERVED", "UNCERTAIN", "CONTEXT_ONLY"}:
            bindings = valid["artifact_bindings"]
            identifiers.append(digest({k: bindings.get(k) for k in
                                      ("source_media_sha256", "audio_clip_sha256", "raw_response_sha256")}))
    if not identifiers:
        raise ContractRejection("ASSISTED_FIRST_OBSERVATION_MUST_BE_BLIND")
    return sorted(set(identifiers))
