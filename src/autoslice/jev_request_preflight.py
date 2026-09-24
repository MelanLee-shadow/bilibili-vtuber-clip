"""Prepare an experimental Jev HTTP request before any dispatch reservation.

This module performs no network/file I/O and grants no model, subtitle or
publication permission. The caller retains its existing budget, retry,
response-validation and ambiguous-outcome rules.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from urllib.request import Request


class JevRequestPreflightError(ValueError):
    """A secret-free local failure that occurs before dispatch intent exists."""


@dataclass(frozen=True)
class PreparedJevRequest:
    request: Request = field(repr=False)
    _credential: bytes = field(repr=False)

    def credential_echoed(self, response: bytes) -> bool:
        """Check the same credential snapshot that actually formed this request."""
        return self._credential in response


def prepare_jev_request(
    body: bytes, *, endpoint: str, environment: Mapping[str, str] | None = None,
) -> PreparedJevRequest:
    """Validate credentials and construct HTTP headers before caller persistence.

    Do not strip or repair invalid credentials. Construction failures are local,
    not HTTP attempts. A later error after durable dispatch intent remains
    potentially sent and must never be retried merely because no reply arrived.
    """
    env = os.environ if environment is None else environment
    key = env.get("TYPESAFE_API_KEY")
    if not isinstance(key, str) or not key:
        raise JevRequestPreflightError("TYPESAFE_CREDENTIAL_MISSING")
    if any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise JevRequestPreflightError("TYPESAFE_CREDENTIAL_INVALID")
    if not isinstance(body, bytes) or not body or len(body) > 64_000:
        raise JevRequestPreflightError("REQUEST_OVER_EXISTING_LOCAL_BODY_CAP")
    if endpoint != "https://api.typesafe.ai/v1/systemone":
        raise JevRequestPreflightError("TYPESAFE_ENDPOINT_MISMATCH")
    try:
        request = Request(
            endpoint, data=body,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
    except Exception:
        # Never copy arbitrary constructor exceptions, which may echo headers.
        raise JevRequestPreflightError("TYPESAFE_REQUEST_CONSTRUCTION_FAILED") from None
    return PreparedJevRequest(request=request, _credential=key.encode("ascii"))
