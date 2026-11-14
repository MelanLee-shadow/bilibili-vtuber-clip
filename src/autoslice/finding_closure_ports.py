"""Frozen ports for discovery owners, F2 owners and recorded-answer replay.

Legacy JSON is confined to RetainedObject, with one lossless conversion. Task,
event and patch records use the same types in every producer and consumer.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import math
from typing import cast

from .finding_closure import ClosureContractError, TaskSpec, sha

from .canonical_json_hash import canonical_sha256
from .retained_json_graph import (JsonValue as JsonValue, CanonicalStorage, CanonicalText,
    needs_legacy_key_codec, normalize_graph_atoms, share_equal_graph_nodes,
    detached_json_tree, detached_json_graph, canonical_graph_equal,
    restore_legacy_instance_state)


def json_value(value: object) -> JsonValue:
    # A stored evidence graph can share old immutable branches many times.
    # Validate/copy each source object once; the canonical encoder still emits
    # every logical occurrence, preserving the exact existing byte contract.
    memo: dict[int, JsonValue] = {}
    active: set[int] = set()
    def copy(item: object) -> JsonValue:
        if isinstance(item,float) and not math.isfinite(item):
            raise ClosureContractError('FINDING_CLOSURE_JSON_NONFINITE')
        if item is None or isinstance(item, (str, bool, int, float)):
            return item
        identity=id(item)
        if identity in active:
            raise ClosureContractError('FINDING_CLOSURE_JSON_VALUE_INVALID')
        if identity in memo:
            return memo[identity]
        active.add(identity)
        try:
            if isinstance(item, (tuple, list)):
                result: JsonValue = [copy(v) for v in item]
            elif isinstance(item, dict) and all(isinstance(k, str) for k in item):
                result = {str(k): copy(v) for k, v in item.items()}
            else:
                raise ClosureContractError('FINDING_CLOSURE_JSON_VALUE_INVALID')
        finally:
            active.remove(identity)
        memo[identity]=result
        return result
    try:
        return copy(value)
    finally:
        # The recursive closure keeps its memo alive until cyclic GC. Release
        # the copied graph when this call returns or raises.
        memo.clear()


def _validate_decoded_json(value: object) -> None:
    # Only the result of the default json.loads decoder belongs here. It has
    # already produced fresh list/dict containers, string keys and no cycles.
    # Walk its surviving values after the complete parse: rejecting constants
    # during decoding would change syntax-error and duplicate-key precedence.
    # Keep json_value's copying/normalization contract for caller-owned graphs.
    def visit(item: object) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise ClosureContractError('FINDING_CLOSURE_JSON_NONFINITE')
        if item is None or isinstance(item, (str, bool, int, float)):
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict) and all(isinstance(k, str) for k in item):
            for child in item.values():
                visit(child)
        else:
            raise ClosureContractError('FINDING_CLOSURE_JSON_VALUE_INVALID')
    visit(value)


@dataclass(frozen=True, init=False, eq=False)
class RetainedObject(CanonicalStorage):
    json_text: str = cast(str, CanonicalText())

    def __init__(self, json_text: str) -> None:
        if type(self) is not RetainedObject:
            # The original dataclass writes its public field before the
            # dynamic hook. Subclass descriptors/setters keep that contract.
            object.__setattr__(self, 'json_text', json_text)
            self.__post_init__()
            return
        if json_text is None:
            # None is the private deferred-text marker, not a valid public
            # input; retain the original JSON decoder's public TypeError.
            json.loads(cast(str, json_text))
        self._install_graph(None, json_text)
        # Preserve the dataclass constructor's dynamic subclass hook.
        self.__post_init__()

    def __post_init__(self) -> None:
        text = self.json_text
        try:
            parsed = json_value(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ClosureContractError('FINDING_CLOSURE_JSON_INVALID') from exc
        if not isinstance(parsed, dict):
            raise ClosureContractError("FINDING_CLOSURE_JSON_OBJECT_REQUIRED")
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        if canonical != text:
            raise ClosureContractError("FINDING_CLOSURE_JSON_NOT_CANONICAL")
        if type(self) is RetainedObject:
            self._install_graph(parsed, text)

    @classmethod
    def from_value(cls, value: object) -> RetainedObject:
        normalized = json_value(value)
        if not isinstance(normalized, dict):
            raise ClosureContractError('FINDING_CLOSURE_JSON_OBJECT_REQUIRED')
        if cls is not RetainedObject or needs_legacy_key_codec(normalized):
            canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
            if cls is not RetainedObject:
                return cls(canonical)
            result = object.__new__(cls)
            result._install_graph(None, canonical)
            return result
        # Keep the validated, caller-detached DAG. Explicit .json_text still
        # uses the original full canonical codec; ordinary reads and hashes
        # must not retain a multi-GiB expansion of repeated immutable history.
        result = object.__new__(cls)
        result._install_graph(share_equal_graph_nodes(normalize_graph_atoms(normalized)), None)
        return result

    @property
    def sha256(self) -> str:
        if type(self) is not RetainedObject:
            return self._public_text_sha256()
        text = self._canonical_text
        if text is not None and type(text) is not str:
            # A str subclass can override encode with mutable behavior.
            return self._public_text_sha256()
        cached = self._canonical_digest
        if cached is not None:
            return cached
        if text is not None:
            result = self._public_text_sha256()
        else:
            result = canonical_sha256(self._json_graph)
        object.__setattr__(self, '_canonical_digest', result)
        return result

    def _public_text_sha256(self) -> str:
        if len(self.json_text) <= 65_536:
            return sha(self.json_text)
        digest = hashlib.sha256()
        for start in range(0, len(self.json_text), 65_536):
            digest.update(self.json_text[start:start + 65_536].encode('utf-8'))
        return digest.hexdigest()

    def value(self) -> dict[str, JsonValue]:
        if type(self) is not RetainedObject or self._json_graph is None:
            # A subclass may intentionally omit super().__post_init__(). Keep
            # the original public value validation on that legacy path.
            parsed: JsonValue = json.loads(self.json_text)
            _validate_decoded_json(parsed)
        else:
            parsed = detached_json_tree(self._json_graph)
        if not isinstance(parsed, dict):
            raise ClosureContractError("FINDING_CLOSURE_JSON_OBJECT_REQUIRED")
        return parsed

    def read_view(self) -> dict[str, JsonValue]:
        """Detached, ordered snapshot for consumers that only read history.

        This preserves private history sharing within the snapshot. The owner
        and other snapshots remain isolated; mutable occurrence semantics are
        supplied only by the unchanged ``value()`` interface. Subclasses keep
        their existing public decoding/validation behavior.
        """
        if type(self) is not RetainedObject or self._json_graph is None:
            return self.value()
        return cast(dict[str, JsonValue], detached_json_graph(self._json_graph))

    def __eq__(self, other: object) -> bool:
        if other.__class__ is not self.__class__:
            return NotImplemented
        peer = other
        if type(self) is not RetainedObject:
            return self.json_text == peer.json_text
        if any(text is not None and type(text) is not str
               for text in (self._canonical_text, peer._canonical_text)):
            return self.json_text == peer.json_text
        if self._json_graph is None or peer._json_graph is None:
            return self.json_text == peer.json_text
        return canonical_graph_equal(self._json_graph, peer._json_graph)

    def __hash__(self) -> int:
        # Preserve the public dataclass hash contract. Pipeline identity uses
        # .sha256 above and therefore does not materialize canonical text.
        return hash((self.json_text,))

    def __getstate__(self) -> object:
        if type(self) is not RetainedObject:
            return object.__getstate__(self)
        # Public copy/pickle state must not expose the private graph or rerun
        # constructors. A restored object has only one authoritative view.
        return {'_retained_graph_state': 1, 'attributes': dict(self.__dict__),
                'graph': json_value(self._json_graph) if self._canonical_text is None else None,
                'text': self._canonical_text}

    def __setstate__(self, state: object) -> None:
        if type(self) is not RetainedObject:
            restore_legacy_instance_state(self, state)
            return
        if not isinstance(state, dict):
            raise TypeError('invalid retained JSON pickle state')
        if state.get('_retained_graph_state') == 1:
            text = state['text']
            attributes = state['attributes']
            if ((text is not None and not isinstance(text, str))
                    or not isinstance(attributes, dict)
                    or any(key in attributes for key in ('json_text', '_json_graph',
                        '_canonical_text', '_canonical_digest'))):
                raise TypeError('invalid retained JSON pickle state')
            if text is not None:
                self._install_graph(None, text)
            else:
                restored = RetainedObject.from_value(state['graph'])
                self._install_graph(restored._json_graph, restored._canonical_text)
            self.__dict__.update(attributes)
        else:
            # Older versions stored the one dataclass field in __dict__.
            text = state['json_text']
            if not isinstance(text, str):
                raise TypeError('invalid retained JSON pickle text')
            self._install_graph(None, text)
            self.__dict__.update({key: value for key, value in state.items()
                                  if key != 'json_text'})


@dataclass(frozen=True)
class PreparedFinding:
    spec: TaskSpec
    finding: RetainedObject
    check: RetainedObject
    materials: RetainedObject
    origin_check: RetainedObject | None = None
    projection: RetainedObject | None = None

    def __post_init__(self) -> None:
        if (self.spec.finding_ref != self.finding.sha256 or self.spec.check_ref != self.check.sha256
                or self.spec.materials_ref != self.materials.sha256):
            raise ClosureContractError("FINDING_CLOSURE_ARTIFACT_BINDING_DRIFT")
        if self.spec.origin_check_ref != (self.origin_check.sha256 if self.origin_check else ""):
            raise ClosureContractError('FINDING_CLOSURE_ORIGIN_BINDING_DRIFT')
        if self.spec.projection_ref != (self.projection.sha256 if self.projection else ''):
            raise ClosureContractError('FINDING_CLOSURE_PROJECTION_BINDING_DRIFT')


@dataclass(frozen=True)
class ConsumedRuling:
    stage: str
    check: RetainedObject
    materials: RetainedObject
    inputs: RetainedObject
    model_identity: RetainedObject
    prompt: str
    raw_completion: str
    origin_check: RetainedObject
    checkpoint: RetainedObject

    def document(self) -> RetainedObject:
        return RetainedObject.from_value({'schema_version':'consumed-ruling.v1',
            'stage':self.stage,'check':self.check.read_view(),'materials':self.materials.read_view(),
            'inputs':self.inputs.read_view(),'model_identity':self.model_identity.read_view(),
            'prompt':self.prompt,'raw_completion':self.raw_completion,
            'completion_sha256':sha(self.raw_completion),'origin_check':self.origin_check.read_view(),
            'checkpoint':self.checkpoint.read_view()})


