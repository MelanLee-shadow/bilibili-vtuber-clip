"""Credential-free WBI parameter signing shared by bounded Bilibili crawlers."""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Mapping
import urllib.parse


_KEY_RX = re.compile(r"^[0-9A-Za-z_-]{32,64}$")
_MIXIN_TABLE = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


class WbiProtocolError(ValueError):
    """Bilibili's public signing material did not match the WBI protocol."""


def mixin_key(payload: Mapping[str, object]) -> str:
    """Derive the WBI mixin key from a validated public nav payload."""

    data = payload.get("data")
    wbi = data.get("wbi_img") if isinstance(data, Mapping) else None
    if not isinstance(wbi, Mapping):
        raise WbiProtocolError(
            f"Bilibili WBI bootstrap returned code {payload.get('code')}"
        )
    parts: list[str] = []
    for field in ("img_url", "sub_url"):
        value = str(wbi.get(field) or "")
        key = value.rsplit("/", 1)[-1].split(".", 1)[0]
        if not _KEY_RX.fullmatch(key):
            raise WbiProtocolError("Bilibili WBI bootstrap returned an invalid key URL")
        parts.append(key)
    raw = "".join(parts)
    result = "".join(raw[index] for index in _MIXIN_TABLE if index < len(raw))[:32]
    if len(result) != 32:
        raise WbiProtocolError(
            "Bilibili WBI bootstrap returned incomplete signing material"
        )
    return result


def signed_params(
    params: Mapping[str, object], *, mixin_key: str, signed_at: dt.datetime
) -> dict[str, object]:
    """Add the protocol-required timestamp and checksum to request parameters."""

    signed = dict(params)
    signed["wts"] = int(signed_at.timestamp())
    blocked = frozenset("!'()*")
    query = urllib.parse.urlencode(
        {
            key: "".join(char for char in str(value) if char not in blocked)
            for key, value in sorted(signed.items())
        }
    )
    signed["w_rid"] = hashlib.md5(  # noqa: S324 - protocol-required checksum
        (query + mixin_key).encode("utf-8"), usedforsecurity=False
    ).hexdigest()
    return signed
