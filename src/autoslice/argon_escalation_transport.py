"""Explicit one-shot native HTTPS implementation of the Argon Provider contract.

No environment/credential discovery, redirects, proxy discovery, retry loop,
paid admission or key rotation. The authorized caller injects a key resolver.
Tests may inject the connection boundary; the normal endpoint is fixed HTTPS.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import http.client
import math
import re
import ssl
import socket
import threading
import time
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request

from .argon_escalation_contract import MODEL, MAX_REQUEST_BYTES, canonical, require, strict_json
from .argon_escalation_storage import MAX_ARTIFACT_BYTES
from .argon_escalation_witness import HttpResponse, ProviderTransportError
from .gemini_backup_policy import validate_key_acceptance_metadata

API_ORIGIN = "https://generativelanguage.googleapis.com"
API_BASE = API_ORIGIN + "/v1beta/"
_HOST = "generativelanguage.googleapis.com"
_ROUTE_FIELDS = {
    "configured_key_count",
    "accepted_key_ordinal",
    "accepted_key_tier",
    "paid_backup_policy",
}
_JOB = re.compile(r"batches/[A-Za-z0-9._~-]{1,512}")
_CHUNK = 65536


class ConnectionFactory(Protocol):
    def __call__(
        self, host: str, *, timeout: float, context: ssl.SSLContext
    ) -> http.client.HTTPConnection: ...


class ArgonHttpTransport:
    """Caller-selected credentials, one physical HTTP transaction per request.

    connection_factory is a trusted injection seam, not a user-supplied endpoint
    or fallback. Production defaults to certificate-verified HTTPSConnection.
    timeout_s bounds blocking socket I/O; remaining-time checks cover each phase
    and body read. OS hostname resolution and an injected resolver/factory are
    synchronous caller dependencies, not cancellable background work.
    """

    def __init__(
        self,
        key_resolver: Callable[[Mapping], str],
        *,
        connection_factory: ConnectionFactory | None = None,
        max_response_bytes: int = MAX_ARTIFACT_BYTES,
    ):
        require(callable(key_resolver), "KEY_RESOLVER_REQUIRED")
        require(
            type(max_response_bytes) is int and 0 < max_response_bytes <= MAX_ARTIFACT_BYTES,
            "RESPONSE_BYTE_LIMIT_INVALID",
        )
        self._key_resolver = key_resolver
        self._factory = connection_factory or http.client.HTTPSConnection
        self._max_response_bytes = max_response_bytes

    @staticmethod
    def _validate_request(method: str, path: str, body: bytes | None) -> None:
        require(
            isinstance(path, str)
            and 0 < len(path) <= 20000
            and all(33 <= ord(c) <= 126 for c in path),
            "HTTP_PATH_INVALID",
        )
        parsed = urlsplit(path)
        require(
            not parsed.scheme
            and not parsed.netloc
            and not parsed.fragment
            and not path.startswith("/")
            and "\\" not in path,
            "HTTP_PATH_INVALID",
        )
        if method == "POST":
            require(
                path in {f"models/{MODEL}:batchGenerateContent", f"models/{MODEL}:generateContent"},
                "HTTP_PATH_INVALID",
            )
            require(
                isinstance(body, bytes) and 0 < len(body) < MAX_REQUEST_BYTES,
                "HTTP_REQUEST_BODY_INVALID",
            )
        elif method == "GET":
            require(body is None, "HTTP_GET_BODY_FORBIDDEN")
            if _JOB.fullmatch(path):
                require(path.rsplit("/", 1)[-1] not in {".", ".."}, "HTTP_PATH_INVALID")
                return
            require(parsed.path == "batches" and bool(parsed.query), "HTTP_PATH_INVALID")
            try:
                pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                raise ValueError("HTTP_QUERY_INVALID") from None
            query = dict(pairs)
            require(
                len(query) == len(pairs)
                and set(query) <= {"pageSize", "pageToken"}
                and "pageSize" in query
                and query["pageSize"].isascii()
                and query["pageSize"].isdigit()
                and int(query["pageSize"]) > 0
                and ("pageToken" not in query or bool(query["pageToken"])),
                "HTTP_QUERY_INVALID",
            )
        else:
            raise ValueError("HTTP_METHOD_INVALID")

    def request(
        self, method: str, path: str, body: bytes | None, *, route: dict, timeout_s: float
    ) -> HttpResponse:
        self._validate_request(method, path, body)
        require(
            type(timeout_s) in (int, float) and math.isfinite(timeout_s) and 0 < timeout_s <= 600,
            "HTTP_TIMEOUT_INVALID",
        )
        require(isinstance(route, Mapping) and set(route) == _ROUTE_FIELDS, "HTTP_ROUTE_INVALID")
        route_copy = strict_json(canonical(route))
        require(validate_key_acceptance_metadata(**route_copy, allow_free_chain_unservable=True) is None,
                "HTTP_ROUTE_INVALID")
        return self._exchange(method, path, body, route_copy, timeout_s)

    def model_metadata(self, *, timeout_s: float) -> HttpResponse:
        """One fixed models.get with no inference body or paid-use receipt."""
        return self._exchange("GET", f"models/{MODEL}", None, {}, timeout_s)

    def _exchange(self, method, path, body, route_copy, timeout_s):
        require(
            type(timeout_s) in (int, float) and math.isfinite(timeout_s) and 0 < timeout_s <= 600,
            "HTTP_TIMEOUT_INVALID",
        )
        deadline = time.monotonic() + timeout_s

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TimeoutError()
            return value

        # No fallback/rotation occurs even when resolution or dispatch fails.
        # Never include the resolved value in exceptions, return values or logs.
        try:
            key = self._key_resolver(route_copy)
        except Exception:
            raise ValueError("HTTP_KEY_RESOLUTION_FAILED") from None
        require(
            isinstance(key, str) and 0 < len(key) <= 4096 and all(33 <= ord(c) <= 126 for c in key),
            "HTTP_KEY_INVALID",
        )
        request = Request(
            API_BASE + path,
            data=body,
            method=method,
            headers={
                "x-goog-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "autoslice-argon-escalation/1",
                "Connection": "close",
            },
        )
        if body is not None:
            request.add_header("Content-Length", str(len(body)))
        status, response, connection, timer = None, None, None, None
        expired = threading.Event()
        socket_holder = [None]

        def expire_socket() -> None:
            # Scoped to this one request/socket. This is not a job poller or a
            # service, and it is cancelled/joined before request() returns.
            expired.set()
            sock = socket_holder[0]
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        chunks: list[bytes] = []
        size = 0
        try:
            context = ssl.create_default_context()
            connection = self._factory(_HOST, timeout=remaining(), context=context)
            connection.set_debuglevel(0)
            timer = threading.Timer(remaining(), expire_socket)
            timer.name = "argon-http-deadline"
            timer.start()
            connection.connect()
            # Retain the connected socket: getresponse may detach it from the
            # connection on Connection: close while its response file is alive.
            sock = connection.sock
            require(sock is not None, "HTTP_CONNECTION_SOCKET_MISSING")
            socket_holder[0] = sock
            connection.auto_open = 0  # Never reconnect implicitly during POST.
            sock.settimeout(remaining())
            connection.request(
                request.get_method(),
                request.selector,
                body=request.data,
                headers=dict(request.header_items()),
            )
            sock.settimeout(remaining())
            response = connection.getresponse()
            status = response.status
            lengths = [v.strip() for k, v in response.getheaders() if k.lower() == "content-length"]
            transfer = [
                v.lower().strip()
                for k, v in response.getheaders()
                if k.lower() == "transfer-encoding"
            ]
            if (
                len(lengths) > 1
                or any(not v.isascii() or not v.isdigit() for v in lengths)
                or (transfer and transfer != ["chunked"])
                or (transfer and lengths)
            ):
                raise ProviderTransportError("HTTP_FRAMING_INVALID", http_status=status)
            expected = int(lengths[0]) if lengths else None
            if expected is not None and expected > self._max_response_bytes:
                raise ProviderTransportError("HTTP_RESPONSE_TOO_LARGE", http_status=status)
            # read1 retains every returned prefix and does not wait to fill an
            # entire large read buffer before the next remaining-time check.
            # read1 closes its file immediately on reaching Content-Length;
            # its last positive chunk can also release the underlying socket.
            while not response.isclosed():
                sock.settimeout(remaining())
                chunk = response.read1(min(_CHUNK, self._max_response_bytes - size + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise ProviderTransportError(
                        "HTTP_RESPONSE_TOO_LARGE",
                        http_status=status,
                        partial_response=b"".join(chunks)[: self._max_response_bytes],
                    )
            remaining()
            raw = b"".join(chunks)
            if expected is not None and len(raw) != expected:
                raise ProviderTransportError(
                    "HTTP_RESPONSE_INCOMPLETE", http_status=status, partial_response=raw
                )
            # No error processing or redirect handler: all actual status codes,
            # including 3xx/4xx/5xx, return their exact body to the lifecycle owner.
            return HttpResponse(status, raw)
        except ProviderTransportError:
            raise
        except TimeoutError:
            raise ProviderTransportError(
                "HTTP_TIMEOUT", http_status=status, partial_response=b"".join(chunks)
            ) from None
        except http.client.IncompleteRead as error:
            raw = b"".join(chunks) + error.partial
            raise ProviderTransportError(
                "HTTP_TIMEOUT" if expired.is_set() else "HTTP_RESPONSE_INCOMPLETE",
                http_status=status,
                partial_response=raw[: self._max_response_bytes],
            ) from None
        except (OSError, http.client.HTTPException):
            raise ProviderTransportError(
                "HTTP_TIMEOUT"
                if expired.is_set()
                else "HTTP_CONNECTION_ERROR"
                if status is None
                else "HTTP_RESPONSE_INCOMPLETE",
                http_status=status,
                partial_response=b"".join(chunks),
            ) from None
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
