"""Hash the existing canonical JSON bytes without retaining the whole text."""
from __future__ import annotations

import hashlib
import json


def canonical_sha256(value: object, *, allow_nan: bool = False) -> str:
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=allow_nan)
    digest = hashlib.sha256()
    buffer = bytearray()
    for fragment in encoder.iterencode(value):
        raw = fragment.encode("utf-8")
        if len(buffer) + len(raw) > 65_536:
            digest.update(buffer)
            buffer.clear()
        if len(raw) >= 65_536:
            digest.update(raw)
        else:
            buffer.extend(raw)
    digest.update(buffer)
    return digest.hexdigest()
