"""Conservative reference text from a validated Mac metadata sidecar.

Container format: RFC 1740 Appendix B/C; Apple's copyfile apple_double_header.
Only the observed v2 FinderInfo + empty-resource-fork shape is supported. This is
not an xattr importer or an interpretation of arbitrary encoded resource data.
The caller must independently require and scan the ordinary companion document.
"""

from __future__ import annotations

import os
import re
import struct

MAGIC = b"\x00\x05\x16\x07"


def metadata_reference_text(raw: bytes, filename: str) -> tuple[str, str] | None:
    """Return (conservative text, companion basename), or None for ordinary data.

    Validate bytes before classifying: a ._ prefix alone never excludes a file.
    Every contiguous UTF-8 path in the binary container is retained for the
    existing reference matcher. Binary separators are replaced, never silently
    deleted (which could join text on either side or conceal reference boundaries).
    """
    if not raw.startswith(MAGIC):
        return None
    if (
        not filename.startswith("._")
        or len(filename) <= 2
        or os.path.basename(filename) != filename
        or filename[2:].startswith("._")
    ):
        raise ValueError("AppleDouble must have a distinct same-directory data companion")
    if len(raw) < 50:
        raise ValueError("AppleDouble header or entry table is truncated")
    _magic, version, _filler, count = struct.unpack_from(">II16sH", raw)
    if version != 0x20000 or count != 2:
        raise ValueError("Unsupported AppleDouble version or entry count")
    entries: dict[int, tuple[int, int]] = {}
    for pos in (26, 38):
        identity, offset, length = struct.unpack_from(">III", raw, pos)
        if identity in entries or identity not in {2, 9}:
            raise ValueError("Unsupported, duplicate or data-fork AppleDouble entry")
        if offset < 50 or offset > len(raw) or length > len(raw) - offset:
            raise ValueError("AppleDouble entry escapes its container")
        entries[identity] = (offset, length)
    if set(entries) != {2, 9}:
        raise ValueError("AppleDouble FinderInfo/resource entries are missing")
    start, length = entries[9]
    if length < 32 or start + length != len(raw):
        raise ValueError("AppleDouble FinderInfo extent is incomplete or has trailing bytes")
    if entries[2] != (len(raw), 0):
        raise ValueError("Nonempty or misplaced resource fork is not supported")
    if any(raw[50:start]):
        raise ValueError("Undeclared AppleDouble padding is not empty")

    # surrogateescape is lossless, unlike errors=ignore. Replace opaque/control
    # bytes with boundaries only in positively identified BINARY metadata.
    # Ordinary UTF-8 authority documents remain strict and unchanged.
    text = raw.decode("utf-8", errors="surrogateescape")
    if text.encode("utf-8", errors="surrogateescape") != raw:
        raise ValueError("AppleDouble byte preservation failed")
    text = re.sub(r"[\x00-\x20\x7f-\x9f\udc80-\udcff]", "\n", text)
    return text, filename[2:]
