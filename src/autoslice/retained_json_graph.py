"""Detached JSON graphs behind the unchanged retained canonical-text API.

An audit may repeat the same immutable history many times. Its canonical JSON
still contains every occurrence, but retaining that entire string is unnecessary
until a caller explicitly asks for text. Returned JSON values never share mutable
containers, including between two occurrences of the same private graph node.
"""
from __future__ import annotations

import json
from typing import TypeAlias, cast

JsonValue: TypeAlias = None | bool | int | float | str | list['JsonValue'] | dict[str, 'JsonValue']


class CanonicalStorage:
    """Private slots are not fields of the public RetainedObject dataclass."""

    __slots__ = ('_json_graph', '_canonical_text', '_canonical_digest')
    _json_graph: dict[str, JsonValue] | None
    _canonical_text: str | None
    _canonical_digest: str | None

    def _install_graph(self, graph: dict[str, JsonValue] | None, text: str | None) -> None:
        object.__setattr__(self, '_json_graph', graph)
        object.__setattr__(self, '_canonical_text', text)
        object.__setattr__(self, '_canonical_digest', None)


class CanonicalText:
    """A no-default descriptor, so dataclass fields/asdict remain unchanged."""

    def __get__(self, instance: CanonicalStorage | None, owner: type[object] | None = None) -> str:
        if instance is None:
            raise AttributeError('json_text')
        if instance._canonical_text is not None:
            return instance._canonical_text
        if instance._json_graph is None:
            raise ValueError('retained JSON representation is uninitialized')
        # Explicit text consumers retain the exact original codec. Do not cache
        # a potentially GiB-sized string beside its small shared source graph.
        return json.dumps(instance._json_graph, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)


def needs_legacy_key_codec(value: JsonValue) -> bool:
    """Keep exotic string-key subclasses on the original complete codec.

    A string subclass may preserve its own hash/equality through str(key).
    The original encoder can then emit repeated textual keys which its decoder
    normalizes. Ordinary persisted JSON uses plain keys and stays graph-backed.
    """
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, (dict, list)) and id(item) not in seen:
            seen.add(id(item))
            if isinstance(item, dict):
                if any(type(key) is not str for key in item):
                    return True
                pending.extend(item.values())
            else:
                pending.extend(item)
    return False


def normalize_graph_atoms(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Normalize a caller-detached graph like the original JSON round trip.

    json_value already owns and validates these containers. Scalar subclasses
    still use the original encoder/decoder, while ordinary immutable strings
    may be reused. Validate integer serialization now, preserving construction
    failure for oversized decimal integers. UTF-8 encoding remains the digest
    owner's responsibility, so lone surrogates keep their original timing.
    """
    seen: set[int] = set()

    def normalize(item: JsonValue) -> JsonValue:
        kind = type(item)
        if item is None or kind in (str, bool, float):
            return item
        if kind is int:
            str(item)
            return item
        if isinstance(item, dict):
            identity = id(item)
            if identity not in seen:
                seen.add(identity)
                for key, child in item.items():
                    item[key] = normalize(child)
            return item
        if isinstance(item, list):
            identity = id(item)
            if identity not in seen:
                seen.add(identity)
                for index, child in enumerate(item):
                    item[index] = normalize(child)
            return item
        return cast(JsonValue, json.loads(json.dumps(item, ensure_ascii=False,
                    sort_keys=True, separators=(',', ':'), allow_nan=False)))

    try:
        normalize(value)
        return value
    finally:
        # Recursive functions form a cycle; leave it holding no input graph.
        seen.clear()


def share_equal_graph_nodes(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Intern equal private JSON nodes after validation and atom normalization.

    Ordinary consumers return fresh mutable trees, so repeated evidence can
    arrive as different objects even when every value is identical. Retaining
    one representative is safe only inside this private immutable owner:
    ``detached_json_tree`` still recreates each public mutable occurrence.
    Tokens include scalar types and floating-point spelling, preserving the
    exact canonical distinction between true, 1, 1.0, 0.0 and -0.0.
    """
    nodes: dict[object, tuple[int, JsonValue]] = {}
    visited: dict[int, tuple[int, JsonValue]] = {}

    def intern(item: JsonValue) -> tuple[int, JsonValue]:
        kind = type(item)
        if isinstance(item, (dict, list)):
            previous = visited.get(id(item))
            if previous is not None:
                return previous
            if isinstance(item, dict):
                children = [(key, intern(item[key])) for key in sorted(item)]
                signature: object = (dict, tuple((key, child[0]) for key, child in children))
                for key, child in children:
                    item[key] = child[1]
            else:
                items = [intern(child) for child in item]
                signature = (list, tuple(child[0] for child in items))
                for index, child in enumerate(items):
                    item[index] = child[1]
            result = nodes.get(signature)
            if result is None:
                result = (len(nodes), item)
                nodes[signature] = result
            visited[id(item)] = result
            return result
        signature = (kind, repr(item) if kind is float else item)
        result = nodes.get(signature)
        if result is None:
            result = (len(nodes), item)
            nodes[signature] = result
        return result

    try:
        return cast(dict[str, JsonValue], intern(value)[1])
    finally:
        # The recursive closure must not keep either graph alive until GC.
        nodes.clear()
        visited.clear()


def detached_json_tree(value: JsonValue) -> JsonValue:
    """Recreate every mutable occurrence without recopying immutable strings."""
    if isinstance(value, dict):
        return {key: detached_json_tree(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [detached_json_tree(child) for child in value]
    return value


def detached_json_graph(value: JsonValue) -> JsonValue:
    """Give a read-only consumer a detached graph in canonical key order.

    Each call owns its containers; repeated immutable histories share nodes
    within that call. Consumers needing independent mutable occurrences must
    keep using ``detached_json_tree``.
    """
    memo: dict[int, JsonValue] = {}

    def copy(item: JsonValue) -> JsonValue:
        if not isinstance(item, (dict, list)):
            return item
        if id(item) in memo:
            return memo[id(item)]
        result: JsonValue = ({key: copy(item[key]) for key in sorted(item)}
                             if isinstance(item, dict)
                             else [copy(child) for child in item])
        memo[id(item)] = result
        return result

    try:
        return copy(value)
    finally:
        memo.clear()


def canonical_graph_equal(left: JsonValue, right: JsonValue) -> bool:
    """Exact equality of canonical JSON, without hashing or expanding aliases.

    Normalized graphs contain only plain JSON values. Number types and signed
    floating zero are significant in the original canonical text; dict insertion
    order is not. A visited pair only reuses comparisons of the same two private,
    stable containers during this call, never a hash-only equality decision.
    """
    pending: list[tuple[JsonValue, JsonValue]] = [(left, right)]
    seen: set[tuple[int, int]] = set()
    while pending:
        a, b = pending.pop()
        if type(a) is not type(b):
            return False
        if a is b:
            continue
        if isinstance(a, dict):
            if not isinstance(b, dict) or a.keys() != b.keys():
                return False
            pair = (id(a), id(b))
            if pair not in seen:
                seen.add(pair)
                pending.extend((a[key], b[key]) for key in a)
        elif isinstance(a, list):
            if not isinstance(b, list) or len(a) != len(b):
                return False
            pair = (id(a), id(b))
            if pair not in seen:
                seen.add(pair)
                pending.extend(zip(a, b))
        elif isinstance(a, float):
            if repr(a) != repr(b):
                return False
        elif a != b:
            return False
    return True


def restore_legacy_instance_state(instance: object, state: object) -> None:
    """Restore the standard dict/slot pickle state without invoking __init__."""
    attributes: object = state
    slots: object = None
    if isinstance(state, tuple) and len(state) == 2:
        attributes, slots = state
    if attributes is not None:
        if not isinstance(attributes, dict):
            raise TypeError('invalid retained subclass attribute state')
        vars(instance).update(attributes)
    if slots is not None:
        if not isinstance(slots, dict):
            raise TypeError('invalid retained subclass slot state')
        for name, value in slots.items():
            object.__setattr__(instance, name, value)
