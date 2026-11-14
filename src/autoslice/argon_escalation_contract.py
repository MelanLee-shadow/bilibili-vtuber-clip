"""Independent Argon evidence contracts. No credentials, subtitle writes or network.

Geometry uses integer milliseconds on an explicitly declared source clock. A
finite source file may start at a nonzero source time; that origin is caller
provenance, not something inferred from its filename or its transcript.
"""

from __future__ import annotations

import hashlib
import re


SHA_PATTERN = re.compile(r"[0-9a-f]{64}")


class ContractError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContractError(reason)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def valid_sha(value: object) -> bool:
    return isinstance(value, str) and SHA_PATTERN.fullmatch(value) is not None


def stat_identity(info) -> tuple:
    # Access time can legitimately change because of this read. Only identity,
    # content size and write/change timestamps belong to the stability check.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


