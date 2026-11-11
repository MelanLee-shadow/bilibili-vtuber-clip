"""Hash the existing canonical JSON bytes in bounded encoder batches.

The byte contract remains JSONEncoder(ensure_ascii=False, sort_keys=True,
separators=(',', ':')). Batches let CPython use its C encoder without allocating
the complete expanded history. Memoization is confined to one hash call.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Callable

_BATCH_BYTES = 65_536
_MEMO_ENTRIES = 4096
_MEMO_BYTES = 8 * 1024 * 1024


def canonical_sha256(value: object, *, allow_nan: bool = False,
                     default: Callable[[object], object] | None = None) -> str:
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=allow_nan, default=default)
    digest = hashlib.sha256()
    buffer = bytearray()
    sizes = {}
    encoded = {}
    measuring = set()
    emitting = set()
    retained = 0

    def feed(raw):
        if len(buffer) + len(raw) > _BATCH_BYTES:
            digest.update(buffer)
            buffer.clear()
        if len(raw) >= _BATCH_BYTES:
            digest.update(raw)
        else:
            buffer.extend(raw)

    def upper(node):
        """Saturating UTF-8 upper bound, without unfolding shared children."""
        kind = type(node)
        identity = id(node) if kind in (dict, list, tuple) else None
        if identity is not None and identity in sizes:
            return sizes[identity][1]
        if kind is str:
            size = 6 * len(node) + 2  # Includes all JSON escapes and UTF-8 widths.
        elif node is None:
            size = 4
        elif kind is bool:
            size = 5
        elif kind is int:
            size = len(str(node)) + 1
        elif kind is float:
            size = len(repr(node)) + 1 if math.isfinite(node) else 16 if allow_nan else None
        elif kind in (dict, list, tuple):
            if identity in measuring:
                return None
            if kind is dict and any(type(key) is not str for key in node):
                return None
            measuring.add(identity)
            size = 2
            try:
                for key, child in (node.items() if kind is dict else enumerate(node)):
                    child_size = upper(child)
                    if child_size is None:
                        size = None
                        break
                    size += child_size + 1 + (6 * len(key) + 3 if kind is dict else 0)
                    if size > _BATCH_BYTES:
                        size = _BATCH_BYTES + 1
                        break
            finally:
                measuring.remove(identity)
        else:
            size = None
        if identity is not None:
            # Scalar bounds are constant-time. Retaining thousands of them
            # used to fill this cache before later shared histories arrived.
            # Turn over bounded container observations instead of repeatedly
            # walking every occurrence of an uncached history subtree.
            if len(sizes) >= _MEMO_ENTRIES:
                sizes.pop(next(iter(sizes)))
            sizes[identity] = (node, size)
        return size

    def write_node(node):
        nonlocal retained
        identity, kind = id(node), type(node)
        if identity in encoded:
            feed(encoded[identity][1])
            return
        size = upper(node)
        if size is None:
            # Preserve standard handling of key types, subclasses, invalid
            # values and nonfinite floats rather than defining a second codec.
            for fragment in encoder.iterencode(node):
                feed(fragment.encode('utf-8'))
            return
        if size <= _BATCH_BYTES or kind not in (dict, list, tuple):
            raw = encoder.encode(node).encode('utf-8')
            if (len(raw) <= _BATCH_BYTES and retained + len(raw) <= _MEMO_BYTES
                    and len(encoded) < _MEMO_ENTRIES):
                encoded[identity] = (node, raw)
                retained += len(raw)
            feed(raw)
            return
        dictionary = kind is dict
        feed(b'{' if dictionary else b'[')
        batch = {} if dictionary else []
        budget, first = 2, True

        def flush():
            nonlocal batch, budget, first
            if not batch:
                return
            if not first:
                feed(b',')
            feed(encoder.encode(batch).encode('utf-8')[1:-1])
            first = False
            batch = {} if dictionary else []
            budget = 2

        for key, child in (sorted(node.items()) if dictionary else enumerate(node)):
            child_size = upper(child)
            cost = None if child_size is None else child_size + 1 + (6 * len(key) + 3 if dictionary else 0)
            if cost is None or cost + 2 > _BATCH_BYTES or child_size > _BATCH_BYTES // 4:
                flush()
                if not first:
                    feed(b',')
                if dictionary:
                    feed(encoder.encode(key).encode('utf-8'))
                    feed(b':')
                emit(child)
                first = False
            else:
                if budget + cost > _BATCH_BYTES:
                    flush()
                if dictionary:
                    batch[key] = child
                else:
                    batch.append(child)
                budget += cost
        flush()
        feed(b'}' if dictionary else b']')

    def emit(node):
        container = type(node) in (dict, list, tuple)
        identity = id(node)
        if container and identity in emitting:
            raise ValueError('Circular reference detected')
        if container:
            emitting.add(identity)
        try:
            write_node(node)
        finally:
            if container:
                emitting.remove(identity)

    try:
        emit(value)
        digest.update(buffer)
        return digest.hexdigest()
    finally:
        sizes.clear()
        encoded.clear()
