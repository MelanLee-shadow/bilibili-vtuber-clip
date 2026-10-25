"""Typed physical questions and their local, versioned dependencies.

Relations constrain decisions; they never authorize edits. Original SRT and
receipts remain intact for their ordinary cold validators.
"""
from __future__ import annotations

from .review_errors import ContractRejection

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from typing import TypeAlias, TypedDict, cast

from .audio_question_identity import physical_target, source_binding
from .jingting_chunker import parse_srt_cues

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class Relation(TypedDict):
    kind: str
    cue_indexes: list[int]


class DependencyGraph(TypedDict):
    schema_version: str
    cue_count: int
    text_sha256: str
    relations: list[Relation]


class ContextCue(TypedDict):
    cue_index: int
    source_start_ms: int
    source_end_ms: int
    text: str


class FindingContext(TypedDict):
    schema_version: str
    target_cue_indexes: list[int]
    scope_cue_indexes: list[int]
    cues: list[ContextCue]
    relations: list[Relation]
    context_sha256: str


class PhysicalQuestionDocument(TypedDict):
    source: list[JsonValue]
    source_window_ms: list[int]
    codepoint_span: list[int] | None


class MaterialRelation(TypedDict):
    kind: str
    members: list[list[int]]


class LocalMaterials(TypedDict):
    cues: list[list[JsonValue]]
    relations: list[MaterialRelation]


class FindingMaterials(TypedDict):
    schema_version: str
    physical_question: PhysicalQuestionDocument
    local_context: LocalMaterials
    candidates: list[str]
    bound_observation_ids: list[str]
    contracts: dict[str, JsonValue]
    model_identity: dict[str, JsonValue]
    physical_question_sha256: str
    materials_sha256: str


CONTEXT_SCHEMA = 'finding-context.v1'
MATERIAL_SCHEMA = 'finding-materials.v2'
RELATION_SCHEMA = 'finding-dependency-graph.v1'
# This is the existing terminal cue window, not a new review threshold.
LOCAL_CONTEXT_CUES = 4
RELATION_KINDS = frozenset({'refrain', 'restatement', 'speaker_turn', 'pronoun_chain', 'evidence'})
MODEL_AUDIT_KEYS = frozenset({'request_sha256', 'check_request_sha256', 'prompt_sha256',
    'editorial_request_sha256', 'no_gain_proof_sha256', 'proof_sha256', 'context_sha256',
    'whole_clip_draft_srt_sha256', 'reviewed_srt_sha256', 'srt_sha256', 'stopped_audit_sha256',
    'finding_sha256', 'evidence_id', 'whole_clip_srt_sha256', 'pass_index', 'round_index',
    'run_directory', 'run_dir', 'out_root', 'audio_path', 'local_audio_path', 'artifact_path',
    'request_path', 'source_media_path'})
SPAN_MATERIAL_FIELDS = frozenset({'schema_version', 'current_cue_sha256', 'proposed_cue_sha256',
    'codepoint_span', 'current_span', 'replacement', 'physical_cue_window_ms'})


@dataclass(frozen=True)
class PhysicalQuestion:
    """Immutable P; the one document conversion preserves the existing wire API."""

    recording_sha256s: tuple[str, ...]
    piece_clock: tuple[tuple[str, int, int], ...] | None
    source_window_ms: tuple[int, int]
    codepoint_span: tuple[int, int] | None

    def to_document(self) -> PhysicalQuestionDocument:
        clock: JsonValue = (None if self.piece_clock is None else
            [[sha, start, end] for sha, start, end in self.piece_clock])
        return {'source': [list(self.recording_sha256s), clock],
                'source_window_ms': list(self.source_window_ms),
                'codepoint_span': None if self.codepoint_span is None else list(self.codepoint_span)}


@dataclass(frozen=True)
class MaterialVersion:
    """Immutable M. JSON boundary snapshots cannot alias a caller's mutable data."""

    physical_question: PhysicalQuestion
    local_context_json: str
    candidates: tuple[str, ...]
    bound_observation_ids: tuple[str, ...]
    contracts_json: str
    model_identity_json: str

    def to_document(self) -> FindingMaterials:
        value: FindingMaterials = {'schema_version': MATERIAL_SCHEMA,
            'physical_question': self.physical_question.to_document(),
            'local_context': cast(LocalMaterials, json.loads(self.local_context_json)),
            'candidates': list(self.candidates), 'bound_observation_ids': list(self.bound_observation_ids),
            'contracts': _object(json.loads(self.contracts_json), 'FINDING_MATERIALS_BINDING_DRIFT'),
            'model_identity': _object(json.loads(self.model_identity_json), 'FINDING_MATERIALS_BINDING_DRIFT'),
            'physical_question_sha256': '', 'materials_sha256': ''}
        value['physical_question_sha256'] = digest(value['physical_question'])
        value['materials_sha256'] = digest({k:v for k,v in value.items()
            if k not in {'physical_question_sha256', 'materials_sha256'}})
        return validate_materials(value)


def _snapshot(value: object) -> str:
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value: object) -> str:
    if isinstance(value, (PhysicalQuestion, MaterialVersion)):
        value = value.to_document()
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def json_value(value: object) -> JsonValue:
    """Validate the serialization boundary instead of propagating untyped JSON."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractRejection('FINDING_JSON_INVALID')
            result[key] = json_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [json_value(child) for child in value]
    raise ContractRejection('FINDING_JSON_INVALID')


def _object(value: object, reason: str) -> dict[str, JsonValue]:
    result = json_value(value)
    if not isinstance(result, dict):
        raise ContractRejection(reason)
    return result


def _array(value: object, reason: str) -> list[JsonValue]:
    result = json_value(value)
    if not isinstance(result, list):
        raise ContractRejection(reason)
    return result


def _text(value: object, reason: str) -> str:
    if not isinstance(value, str):
        raise ContractRejection(reason)
    return value


def _integer(value: object, reason: str) -> int:
    if type(value) is not int:
        raise ContractRejection(reason)
    return value


def _indices(values: object, count: int) -> list[int]:
    result: set[int] = set()
    for value in _array(values, 'CONTEXT_CUE_INDEX_INVALID'):
        if isinstance(value, str) and value.startswith('cue:'):
            value = value[4:]
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if type(value) is not int or not 1 <= value <= count:
            raise ContractRejection('CONTEXT_CUE_INDEX_INVALID')
        result.add(value)
    return sorted(result)


def physical_question_value(check: object, *, codepoint_span: object = None) -> PhysicalQuestion:
    request = _object(check, 'PHYSICAL_TARGET_DIFFERS')
    target, source = physical_target(request), source_binding(request)
    if target is None:
        raise ContractRejection('PHYSICAL_TARGET_DIFFERS')
    if source is None:
        raise ContractRejection('SOURCE_BINDING_DIFFERS')
    bound = _object(request.get('edit_span_binding') or {}, 'SPAN_COMPILATION_GEOMETRY_INVALID')
    span_value = codepoint_span if codepoint_span is not None else bound.get('codepoint_span')
    if span_value is None and all(type(bound.get(k)) is int for k in ('start', 'end')):
        span_value = [bound['start'], bound['end']]
    span: list[int] | None = None
    if span_value is not None:
        items = _array(span_value, 'SPAN_COMPILATION_GEOMETRY_INVALID')
        if len(items) != 2 or any(type(v) is not int for v in items):
            raise ContractRejection('SPAN_COMPILATION_GEOMETRY_INVALID')
        span = [_integer(v, 'SPAN_COMPILATION_GEOMETRY_INVALID') for v in items]
        if not 0 <= span[0] <= span[1]:
            raise ContractRejection('SPAN_COMPILATION_GEOMETRY_INVALID')
    # G6 recording hashes/piece clock remain; display candidate IDs do not.
    recordings = tuple(_text(sha, 'SOURCE_BINDING_DIFFERS')
                       for sha in _array(source[1], 'SOURCE_BINDING_DIFFERS'))
    clock = None
    if source[2] is not None:
        pieces = []
        for item in _array(source[2], 'SOURCE_BINDING_DIFFERS'):
            piece = _array(item, 'SOURCE_BINDING_DIFFERS')
            if len(piece) != 3:
                raise ContractRejection('SOURCE_BINDING_DIFFERS')
            pieces.append((_text(piece[0], 'SOURCE_BINDING_DIFFERS'),
                _integer(piece[1], 'SOURCE_BINDING_DIFFERS'), _integer(piece[2], 'SOURCE_BINDING_DIFFERS')))
        clock = tuple(pieces)
    return PhysicalQuestion(recordings, clock, (target[0], target[1]),
                            None if span is None else (span[0], span[1]))


def physical_question(check: object, *, codepoint_span: object = None) -> PhysicalQuestionDocument:
    """Serialization adapter retained for the existing F2 and C/E consumers."""
    return physical_question_value(check, codepoint_span=codepoint_span).to_document()


def question_key(check: object, *, codepoint_span: object = None) -> str:
    return digest(physical_question(check, codepoint_span=codepoint_span))


def _relations(rows: Iterable[object], count: int) -> list[Relation]:
    normalized: dict[str, Relation] = {}
    for value in rows:
        row = _object(value, 'CONTEXT_RELATION_INVALID')
        kind = _text(row.get('kind'), 'CONTEXT_RELATION_INVALID')
        if kind not in RELATION_KINDS:
            raise ContractRejection('CONTEXT_RELATION_INVALID')
        ids = _indices(row.get('cue_indexes', []), count)
        if len(ids) >= 2:
            relation: Relation = {'kind': kind, 'cue_indexes': ids}
            normalized[digest(relation)] = relation
    return [normalized[key] for key in sorted(normalized)]


@lru_cache(maxsize=8)
def _text_relations(srt_text: str) -> str:
    from .refrain_detection import detect_refrains
    from .restatement_recall import RestatementCue, find_restatement_pairs, _split_label_prefix
    cues = [c for c in parse_srt_cues(srt_text) if c.text.strip()]
    rows: list[object] = [{'kind': 'refrain', 'cue_indexes': row['cue_indexes']}
                          for row in detect_refrains(cues)]
    spoken = []
    for index, cue in enumerate(cues, 1):
        _, label, text = _split_label_prefix(cue.text)
        spoken.append(RestatementCue(index, cue.start_ms / 1000, label, text))
    rows.extend({'kind': 'restatement', 'cue_indexes': [pair.early_index, pair.late_index]}
                for pair in find_restatement_pairs(spoken))
    return json.dumps(_relations(rows, len(cues)), ensure_ascii=False, sort_keys=True)


def relation_graph(srt_text: str, *, relations: Iterable[object] = ()) -> DependencyGraph:
    count = len([c for c in parse_srt_cues(srt_text) if c.text.strip()])
    rows = _relations([*_array(json.loads(_text_relations(srt_text)), 'CONTEXT_RELATION_INVALID'),
                       *relations], count)
    return {'schema_version': RELATION_SCHEMA, 'cue_count': count,
            'text_sha256': hashlib.sha256(srt_text.encode()).hexdigest(), 'relations': rows}


def pronoun_relations(srt_text: str, audit: object) -> list[Relation]:
    """Reuse the pronoun owner's complete occurrence/decision validation."""
    if audit is None:
        return []
    from .pronoun_consistency import scoped_pronoun_policy_evidence, _canonical_referent, _occurrences
    document = _object(audit, 'PRONOUN_CONTEXT_UNBOUND')
    occurrences = _array(document.get('occurrences'), 'PRONOUN_CONTEXT_UNBOUND')
    if not occurrences:
        if _occurrences(srt_text)[1]:
            raise ContractRejection('PRONOUN_CONTEXT_UNBOUND')
        return []
    first = _object(occurrences[0], 'PRONOUN_CONTEXT_UNBOUND')
    if scoped_pronoun_policy_evidence(srt_text, document,
            _integer(first.get('cue_index'), 'PRONOUN_CONTEXT_UNBOUND')) is None:
        raise ContractRejection('PRONOUN_CONTEXT_UNBOUND')
    by_id = {_text(row.get('occurrence_id'), 'PRONOUN_CONTEXT_UNBOUND'):
             _integer(row.get('cue_index'), 'PRONOUN_CONTEXT_UNBOUND')
             for value in occurrences for row in [_object(value, 'PRONOUN_CONTEXT_UNBOUND')]}
    groups: dict[str, set[int]] = {}
    for value in _array(document.get('decisions'), 'PRONOUN_CONTEXT_UNBOUND'):
        row = _object(value, 'PRONOUN_CONTEXT_UNBOUND')
        referent = str(_canonical_referent(row.get('referent')))
        if referent != 'UNSPECIFIED':
            occurrence_id = _text(row.get('occurrence_id'), 'PRONOUN_CONTEXT_UNBOUND')
            groups.setdefault(referent, set()).add(by_id[occurrence_id])
    return [{'kind': 'pronoun_chain', 'cue_indexes': sorted(ids)} for _, ids in sorted(groups.items()) if len(ids) > 1]


def _graph(value: object) -> DependencyGraph:
    row = _object(value, 'CONTEXT_RELATION_INVALID')
    count = _integer(row.get('cue_count'), 'CONTEXT_RELATION_INVALID')
    if count < 0 or row.get('schema_version') != RELATION_SCHEMA:
        raise ContractRejection('CONTEXT_RELATION_INVALID')
    return {'schema_version': RELATION_SCHEMA, 'cue_count': count,
        'text_sha256': _text(row.get('text_sha256'), 'CONTEXT_RELATION_INVALID'),
        'relations': _relations(_array(row.get('relations'), 'CONTEXT_RELATION_INVALID'), count)}


def context_for_check(check: object, *, finding: object = None, graph: object = None,
                      relations: Iterable[object] = ()) -> FindingContext:
    request = _object(check, 'FINDING_CONTEXT_BINDING_DRIFT')
    text = _text(request.get('whole_clip_current_srt'), 'FINDING_CONTEXT_BINDING_DRIFT')
    cues = [c for c in parse_srt_cues(text) if c.text.strip()]
    finding_row = _object(finding or {}, 'FINDING_CONTEXT_BINDING_DRIFT')
    graph_row = relation_graph(text, relations=relations) if graph is None else _graph(graph)
    if (graph_row['cue_count'] != len(cues)
            or graph_row['text_sha256'] != hashlib.sha256(text.encode()).hexdigest()):
        raise ContractRejection('CONTEXT_RELATION_INVALID')
    edges = graph_row['relations']
    targets = _indices(request.get('cue_indexes', []), len(cues))
    if not targets:
        raise ContractRejection('CONTEXT_CUE_INDEX_INVALID')
    scope = set(targets)
    scope.update(_indices(request.get('evidence_cue_ids') or [], len(cues)))
    scope.update(_indices(finding_row.get('evidence_cue_ids') or [], len(cues)))
    for index in targets:
        scope.update(range(max(1, index - LOCAL_CONTEXT_CUES),
                           min(len(cues), index + LOCAL_CONTEXT_CUES) + 1))
    while True:
        expanded = scope | {index for row in edges if scope.intersection(row['cue_indexes'])
                            for index in row['cue_indexes']}
        if expanded == scope:
            break
        scope = expanded
    dependencies = [row for row in edges if scope.intersection(row['cue_indexes'])]
    offset = _integer(request.get('source_media_timeline_offset_ms'), 'FINDING_CONTEXT_BINDING_DRIFT')
    projected: list[ContextCue] = [{'cue_index': i, 'source_start_ms': cues[i-1].start_ms + offset,
        'source_end_ms': cues[i-1].end_ms + offset, 'text': cues[i-1].text} for i in sorted(scope)]
    result: FindingContext = {'schema_version': CONTEXT_SCHEMA, 'target_cue_indexes': targets,
        'scope_cue_indexes': sorted(scope), 'cues': projected, 'relations': dependencies,
        'context_sha256': ''}
    result['context_sha256'] = digest({k:v for k,v in result.items() if k != 'context_sha256'})
    return result


def validate_context(context: object) -> FindingContext:
    raw = _object(context, 'FINDING_CONTEXT_BINDING_DRIFT')
    fields = set(FindingContext.__annotations__)
    if (set(raw) != fields or raw.get('schema_version') != CONTEXT_SCHEMA
            or raw.get('context_sha256') != digest({k:v for k,v in raw.items() if k != 'context_sha256'})):
        raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    scope_values = _array(raw.get('scope_cue_indexes'), 'FINDING_CONTEXT_BINDING_DRIFT')
    scope = [_integer(i, 'FINDING_CONTEXT_BINDING_DRIFT') for i in scope_values]
    if not scope or scope != sorted(set(scope)) or min(scope) < 1:
        raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    targets = _indices(raw.get('target_cue_indexes'), max(scope))
    rows = _array(raw.get('cues'), 'FINDING_CONTEXT_BINDING_DRIFT')
    if not targets or not set(targets).issubset(scope) or len(rows) != len(scope):
        raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    for index, value in zip(scope, rows, strict=True):
        row = _object(value, 'FINDING_CONTEXT_BINDING_DRIFT')
        start = _integer(row.get('source_start_ms'), 'FINDING_CONTEXT_BINDING_DRIFT')
        end = _integer(row.get('source_end_ms'), 'FINDING_CONTEXT_BINDING_DRIFT')
        if (set(row) != set(ContextCue.__annotations__) or row.get('cue_index') != index
                or not 0 <= start < end or not isinstance(row.get('text'), str)):
            raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    relations = _relations(_array(raw.get('relations'), 'FINDING_CONTEXT_BINDING_DRIFT'), max(scope))
    if relations != raw['relations'] or any(not set(r['cue_indexes']).issubset(scope) for r in relations):
        raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    return cast(FindingContext, raw)


def cue_projection(context: object) -> dict[str, str]:
    return {'cue:' + str(row['cue_index']): row['text'] for row in validate_context(context)['cues']}


def validate_context_for_check(check: object, context: object) -> FindingContext:
    request = _object(check, 'FINDING_CONTEXT_BINDING_DRIFT')
    bound = validate_context(context)
    cues = [c for c in parse_srt_cues(_text(request.get('whole_clip_current_srt'),
                                         'FINDING_CONTEXT_BINDING_DRIFT')) if c.text.strip()]
    if bound['target_cue_indexes'] != _indices(request.get('cue_indexes', []), len(cues)):
        raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    required = context_for_check(request, relations=bound['relations'])
    if (not set(required['scope_cue_indexes']).issubset(bound['scope_cue_indexes'])
            or not {digest(r) for r in required['relations']}.issubset({digest(r) for r in bound['relations']})):
        raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    offset = _integer(request.get('source_media_timeline_offset_ms'), 'FINDING_CONTEXT_BINDING_DRIFT')
    for row in bound['cues']:
        index = row['cue_index']
        if index > len(cues):
            raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
        cue = cues[index-1]
        if row != {'cue_index': index, 'source_start_ms': cue.start_ms + offset,
                   'source_end_ms': cue.end_ms + offset, 'text': cue.text}:
            raise ContractRejection('FINDING_CONTEXT_BINDING_DRIFT')
    return bound


def local_material_view(context: object) -> LocalMaterials:
    bound = validate_context(context)
    by_index = {r['cue_index']: r for r in bound['cues']}
    relations: list[MaterialRelation] = [{'kind': row['kind'],
        'members': [[by_index[i]['source_start_ms'], by_index[i]['source_end_ms']]
                    for i in row['cue_indexes']]} for row in bound['relations']]
    return {'cues': [[r['source_start_ms'], r['source_end_ms'], r['text']] for r in bound['cues']],
            'relations': sorted(relations, key=digest)}


def material_version_for_check(check: object, *, finding: object = None, graph: object = None,
                        relations: Iterable[object] = (), candidates: Iterable[str | None] | None = None,
                        observation_ids: Iterable[str] = (), contracts: object,
                        model_identity: object) -> MaterialVersion:
    request = _object(check, 'FINDING_MATERIALS_BINDING_DRIFT')
    relation_rows = tuple(relations)
    context = (validate_context_for_check(request, request['finding_context'])
        if request.get('finding_context') is not None and graph is None and finding is None and not relation_rows
        else context_for_check(request, finding=finding, graph=graph, relations=relation_rows))
    if candidates is None:
        candidates = [candidate_text for key in ('current_cue', 'proposed_cue')
                      if isinstance(candidate_text := request.get(key), str)]
    return MaterialVersion(physical_question_value(request), _snapshot(local_material_view(context)),
        tuple(sorted({s for s in candidates if isinstance(s, str)})), tuple(sorted(set(observation_ids))),
        _snapshot(_object(contracts, 'FINDING_MATERIALS_BINDING_DRIFT')),
        _snapshot(_object(model_identity, 'FINDING_MATERIALS_BINDING_DRIFT')))


def materials_for_check(check: object, *, finding: object = None, graph: object = None,
                        relations: Iterable[object] = (), candidates: Iterable[str | None] | None = None,
                        observation_ids: Iterable[str] = (), contracts: object,
                        model_identity: object) -> FindingMaterials:
    """The existing JSON adapter; native P/M users retain immutable values."""
    return material_version_for_check(check, finding=finding, graph=graph, relations=relations,
        candidates=candidates, observation_ids=observation_ids, contracts=contracts,
        model_identity=model_identity).to_document()


def model_data(value: object, *, _path: tuple[str, ...] = (), source_offset_ms: int = 0) -> JsonValue:
    """The v2 model and its key share this view; receipts are never rewritten.

    Only explicitly named audit addresses/hashes are omitted. Source/audio
    identity, readings, uncertain outcomes and normative content are retained.
    """
    parsed = json_value(value)
    if isinstance(parsed, dict):
        if _path and _path[-1] == 'structured_chat_sender_context' and parsed.get('clock') != 'SOURCE':
            for key in ('window_start_ms', 'window_end_ms', 'target_start_ms', 'target_end_ms'):
                timestamp = parsed.get(key)
                if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                    parsed[key] = timestamp + source_offset_ms
            rows = parsed.get('rows')
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        timestamp = row.get('offset_ms')
                        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
                            row['offset_ms'] = timestamp + source_offset_ms
            parsed['clock'] = 'SOURCE'
        # The existing span owner binds both full-SRT/local-clock audit data
        # and the actual edit. M retains the latter on its source clock; the
        # signed original span/request remains intact for cold validation.
        span = bool(_path and _path[-1] in {'edit_span_binding', 'declared_edit_scope'})
        return {key: model_data(child, _path=(*_path, key), source_offset_ms=source_offset_ms)
                for key, child in parsed.items() if key not in MODEL_AUDIT_KEYS
                and (not span or key in SPAN_MATERIAL_FIELDS)
                and not (_path and _path[-1] == 'closed_set_structured_evidence' and key == 'evidence_sha256')}
    if isinstance(parsed, list):
        return [model_data(child, _path=_path, source_offset_ms=source_offset_ms) for child in parsed]
    return parsed


def decision_materials(check: object, *, data: object, contracts: object, model_identity: object,
                       candidates: Iterable[str | None] | None = None,
                       observation_ids: Iterable[str] = ()) -> FindingMaterials:
    """Bind every actually rendered non-cue dependency, not a prompt hash."""
    versions = _object(contracts, 'FINDING_MATERIALS_BINDING_DRIFT')
    request = _object(check, 'FINDING_MATERIALS_BINDING_DRIFT')
    versions['decision_materials'] = model_data(data, source_offset_ms=_integer(
        request.get('source_media_timeline_offset_ms'), 'FINDING_MATERIALS_BINDING_DRIFT'))
    return materials_for_check(check, candidates=candidates, observation_ids=observation_ids,
                               contracts=versions, model_identity=model_identity)


def validate_materials(value: object) -> FindingMaterials:
    raw = _object(value, 'FINDING_MATERIALS_BINDING_DRIFT')
    if (set(raw) != set(FindingMaterials.__annotations__) or raw.get('schema_version') != MATERIAL_SCHEMA
            or any(not isinstance(raw.get(k), dict) or not raw[k]
                   for k in ('physical_question', 'local_context', 'contracts', 'model_identity'))
            or any(not isinstance(raw.get(k), list)
                   or any(not isinstance(s, str) for s in _array(raw[k], 'FINDING_MATERIALS_BINDING_DRIFT'))
                   or raw[k] != sorted(set(_text(s, 'FINDING_MATERIALS_BINDING_DRIFT')
                       for s in _array(raw[k], 'FINDING_MATERIALS_BINDING_DRIFT')))
                   for k in ('candidates', 'bound_observation_ids'))
            or raw['physical_question_sha256'] != digest(raw['physical_question'])
            or raw['materials_sha256'] != digest({k:v for k,v in raw.items()
                if k not in {'physical_question_sha256', 'materials_sha256'}})):
        raise ContractRejection('FINDING_MATERIALS_BINDING_DRIFT')
    return cast(FindingMaterials, raw)


def materials_for_model(value: object, model_identity: object) -> FindingMaterials:
    updated: FindingMaterials = {**validate_materials(value),
        'model_identity': _object(model_identity, 'FINDING_MATERIALS_BINDING_DRIFT')}
    updated['materials_sha256'] = digest({k:v for k,v in updated.items()
        if k not in {'physical_question_sha256', 'materials_sha256'}})
    return validate_materials(updated)


def materials_for_origin(value: object, origin_check: object) -> FindingMaterials:
    """Freeze P's original codepoint anchor; keep the actual edit in M's data."""
    material = validate_materials(value)
    origin = physical_question_value(origin_check)
    actual = material['physical_question']
    physical = origin.to_document()
    if actual['source'] != physical['source'] or actual['source_window_ms'] != physical['source_window_ms']:
        raise ContractRejection('FINDING_ORIGIN_PHYSICAL_DRIFT')
    return MaterialVersion(origin, _snapshot(material['local_context']),
        tuple(material['candidates']), tuple(material['bound_observation_ids']),
        _snapshot(material['contracts']), _snapshot(material['model_identity'])).to_document()


def effect_key(materials: object, *, channel: str, provider: str, model: object) -> str:
    value = materials_for_model(materials, model)
    return digest({'physical_question_sha256': value['physical_question_sha256'],
        'materials_sha256': value['materials_sha256'], 'channel': channel,
        'provider': provider, 'model': model})


def affected_findings(before_graph: object, after_graph: object, changed_cues: Iterable[int],
                      contexts: Mapping[str, FindingContext]) -> list[str]:
    before = {digest(row): row for row in _graph(before_graph)['relations']}
    after = {digest(row): row for row in _graph(after_graph)['relations']}
    relation_changes = {i for key in before.keys() ^ after.keys()
                        for i in (before.get(key) or after[key])['cue_indexes']}
    changed = set(changed_cues) | relation_changes
    return sorted(key for key, context in contexts.items()
                  if changed.intersection(validate_context(context)['scope_cue_indexes']))
