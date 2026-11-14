"""Typed physical questions and their local, versioned dependencies.

Relations constrain decisions; they never authorize edits. Original SRT and
receipts remain intact for their ordinary cold validators.
"""
from __future__ import annotations

from .review_errors import ContractRejection

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import TypeAlias, TypedDict, cast

from .audio_question_identity import physical_target, source_binding
from .canonical_json_hash import canonical_sha256

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


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


MATERIAL_SCHEMA = 'finding-materials.v2'


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
        return _finalize_materials(value)


def _snapshot(value: object) -> str:
    return json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def digest(value: object) -> str:
    if isinstance(value, (PhysicalQuestion, MaterialVersion)):
        value = value.to_document()
    return canonical_sha256(value)


def json_value(value: object) -> JsonValue:
    """Validate the serialization boundary instead of propagating untyped JSON."""
    return _json_value(value, None)


def _json_value(value: object, memo: dict[int, JsonValue] | None) -> JsonValue:
    """Internal readers may retain sharing; public values remain independent trees."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    identity = id(value)
    if memo is not None and identity in memo:
        return memo[identity]
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractRejection('FINDING_JSON_INVALID')
            result[key] = _json_value(child, memo)
        copied: JsonValue = result
    elif isinstance(value, (list, tuple)):
        copied = [_json_value(child, memo) for child in value]
    else:
        raise ContractRejection('FINDING_JSON_INVALID')
    if memo is not None:
        memo[identity] = copied
    return copied


def _object(value: object, reason: str, *, read_only: bool = False) -> dict[str, JsonValue]:
    result = _json_value(value, {} if read_only else None)
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


def physical_question_value(check: object, *, codepoint_span: object = None) -> PhysicalQuestion:
    request = _object(check, 'PHYSICAL_TARGET_DIFFERS', read_only=True)
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


def _validate_material_shape(raw: dict[str, JsonValue]) -> None:
    if (set(raw) != set(FindingMaterials.__annotations__) or raw.get('schema_version') != MATERIAL_SCHEMA
            or any(not isinstance(raw.get(k), dict) or not raw[k]
                   for k in ('physical_question', 'local_context', 'contracts', 'model_identity'))
            or any(not isinstance(raw.get(k), list)
                   or any(not isinstance(s, str) for s in _array(raw[k], 'FINDING_MATERIALS_BINDING_DRIFT'))
                   or raw[k] != sorted(set(_text(s, 'FINDING_MATERIALS_BINDING_DRIFT')
                       for s in _array(raw[k], 'FINDING_MATERIALS_BINDING_DRIFT')))
                   for k in ('candidates', 'bound_observation_ids'))):
        raise ContractRejection('FINDING_MATERIALS_BINDING_DRIFT')


def _finalize_materials(value: object, *, _read_only: bool = False) -> FindingMaterials:
    """Validate a newly generated snapshot and compute its bindings once."""
    raw = _object(value, 'FINDING_MATERIALS_BINDING_DRIFT', read_only=_read_only)
    _validate_material_shape(raw)
    raw['physical_question_sha256'] = digest(raw['physical_question'])
    raw['materials_sha256'] = digest({k:v for k,v in raw.items()
        if k not in {'physical_question_sha256', 'materials_sha256'}})
    return cast(FindingMaterials, raw)


def validate_materials(value: object, *, _read_only: bool = False) -> FindingMaterials:
    raw = _object(value, 'FINDING_MATERIALS_BINDING_DRIFT', read_only=_read_only)
    _validate_material_shape(raw)
    if (raw['physical_question_sha256'] != digest(raw['physical_question'])
            or raw['materials_sha256'] != digest({k:v for k,v in raw.items()
                if k not in {'physical_question_sha256', 'materials_sha256'}})):
        raise ContractRejection('FINDING_MATERIALS_BINDING_DRIFT')
    return cast(FindingMaterials, raw)


def materials_for_model(value: object, model_identity: object, *,
                        _read_only: bool = False) -> FindingMaterials:
    updated: FindingMaterials = {**validate_materials(value, _read_only=_read_only),
        'model_identity': _object(model_identity, 'FINDING_MATERIALS_BINDING_DRIFT')}
    return _finalize_materials(updated, _read_only=_read_only)


def materials_for_origin(value: object, origin_check: object, *,
                         _read_only: bool = False) -> FindingMaterials:
    """Freeze P's original codepoint anchor; keep the actual edit in M's data."""
    material = validate_materials(value, _read_only=_read_only)
    origin = physical_question_value(origin_check)
    actual = material['physical_question']
    physical = origin.to_document()
    if actual['source'] != physical['source'] or actual['source_window_ms'] != physical['source_window_ms']:
        raise ContractRejection('FINDING_ORIGIN_PHYSICAL_DRIFT')
    if _read_only:
        # The consumed-ruling writer retains a validated graph. Rebinding its
        # physical anchor only replaces these two fields; a JSON round trip
        # would duplicate every historical occurrence without changing M.
        updated: FindingMaterials = {**material, 'physical_question': physical}
        return _finalize_materials(updated, _read_only=True)
    return MaterialVersion(origin, _snapshot(material['local_context']),
        tuple(material['candidates']), tuple(material['bound_observation_ids']),
        _snapshot(material['contracts']), _snapshot(material['model_identity'])).to_document()


def effect_key(materials: object, *, channel: str, provider: str, model: object) -> str:
    value = materials_for_model(materials, model, _read_only=True)
    return digest({'physical_question_sha256': value['physical_question_sha256'],
        'materials_sha256': value['materials_sha256'], 'channel': channel,
        'provider': provider, 'model': model})


