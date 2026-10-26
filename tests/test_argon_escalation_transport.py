"""Native HTTP bytes over an isolated loopback, plus public Argon recovery.

No external DNS/connect is permitted, including when an error is swallowed by
an upper layer. All credentials are fictional and injected, never discovered.
"""

from __future__ import annotations

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
import socket
import ssl
import threading
import time

import pytest

from test_argon_escalation_witness import FakeProvider, advance, case as case, finish
from src.autoslice import argon_escalation_storage as storage
from src.autoslice.argon_escalation_contract import KeyPolicy, MODEL, canonical, sha
from src.autoslice.argon_escalation_transport import ArgonHttpTransport, API_BASE
from src.autoslice.argon_escalation_witness import ProviderTransportError


@pytest.fixture(autouse=True)
def external_network_and_credentials_forbidden(monkeypatch, request):
    violations = []
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    resolve, getenv = socket.getaddrinfo, os._Environ.__getitem__

    def check_address(address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            violations.append("external connect")
            raise AssertionError("External network forbidden")

    def guarded_connect(self, address):
        check_address(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        check_address(address)
        return connect_ex(self, address)

    def guarded_resolve(host, *args, **kwargs):
        if host != "127.0.0.1":
            violations.append("external DNS")
            raise AssertionError("External DNS forbidden")
        return resolve(host, *args, **kwargs)

    def guarded_env(self, key):
        if key.startswith("GEMINI_") or key == "GOOGLE_API_KEY":
            violations.append("real credential environment")
            raise AssertionError("Credential environment forbidden")
        return getenv(self, key)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_resolve)
    monkeypatch.setattr(os._Environ, "__getitem__", guarded_env)
    yield
    request.node.user_properties.append(("external_network_attempts", len(violations)))
    assert not violations, "Forbidden access occurred even if the caller caught it"
    assert not [t for t in threading.enumerate() if t.name == "argon-http-deadline"]


class WireServer(ThreadingHTTPServer):
    daemon_threads = False  # server_close joins all request threads at teardown.


@pytest.fixture
def wire(request):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # Never log a request/header/key.

        def do_POST(self):
            self.respond()

        def do_GET(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            record = {
                "method": self.command,
                "path": self.path,
                "body": body,
                "headers": dict(self.headers),
            }
            self.server.requests.append(record)
            plan = self.server.plan(record)
            action = plan.get("action")
            if action == "disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.close_connection = True
                return
            if action == "trickle_headers":
                try:
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Trickle: ")
                    for _ in range(20):
                        self.connection.sendall(b"x")
                        time.sleep(0.03)
                    self.connection.sendall(b"\r\nContent-Length: 0\r\n\r\n")
                except OSError:
                    pass
                self.close_connection = True
                return
            if action == "timeout_headers":
                time.sleep(0.4)
            raw, status = plan.get("body", b""), plan.get("status", 200)
            try:
                self.send_response(status)
                for name, value in plan.get("headers", {}).items():
                    self.send_header(name, value)
                framing = plan.get("framing", "length")
                if framing == "chunked":
                    self.send_header("Transfer-Encoding", "chunked")
                elif framing != "close":
                    self.send_header(
                        "Content-Length", str(len(raw) + (7 if framing == "short" else 0))
                    )
                self.send_header("Connection", "close")
                self.end_headers()
                if action == "timeout_body":
                    self.wfile.write(raw[:7])
                    self.wfile.flush()
                    time.sleep(0.4)
                    self.wfile.write(raw[7:])
                elif framing == "chunked":
                    for i in range(0, len(raw), 7):
                        part = raw[i : i + 7]
                        self.wfile.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(raw)
                self.wfile.flush()
            except (OSError, ConnectionError):
                pass  # Expected when a timeout/disconnect test closes its socket.
            finally:
                self.close_connection = True

    server = WireServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.connections = []
    server.plan = lambda _: {"body": b"synthetic raw\x00\xff\r\n"}

    def factory(host, *, timeout, context):
        assert host == "generativelanguage.googleapis.com"
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        server.connections.append({"host": host, "timeout": timeout})
        return http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

    server.factory = factory
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()
        request.node.user_properties.append(("loopback_http_requests", len(server.requests)))
        request.node.user_properties.append(
            ("loopback_http_posts", sum(r["method"] == "POST" for r in server.requests))
        )


def transport(wire, **kwargs):
    resolutions = []

    def resolver(route):
        resolutions.append(deepcopy(route))
        return "fictional-key-" + str(route["accepted_key_ordinal"])

    return ArgonHttpTransport(resolver, connection_factory=wire.factory, **kwargs), resolutions


def native_plan(wire, handle):
    fake = FakeProvider(handle)

    def plan(request):
        path = request["path"]
        if path.endswith(":batchGenerateContent"):
            op = fake.op("BATCH_STATE_PENDING")
        elif path.endswith(":generateContent"):
            op = fake.payload
        elif path.startswith("/v1beta/batches?"):
            op = {"operations": [fake.op()]}
        else:
            op = fake.op()
        # Intentional formatting differences must survive transport and hashing.
        return {"status": 200, "body": json.dumps(op, indent=2).encode() + b"\n"}

    wire.plan = plan
    return fake, plan


def outcome_rows(engine, handle):
    return [
        storage.get_json(p)
        for p in (engine.root / handle["task_id"] / "calls").glob("*.outcome.json")
    ]


def test_native_transport_public_success_exact_request_response_and_receipts(case, wire):
    engine, spec, handle, _ = case
    fake, _ = native_plan(wire, handle)
    provider, resolutions = transport(wire)
    result = finish(case, provider, key_policy=KeyPolicy(3, (1,)))
    assert result["status"] == "OBSERVED" and result["job_name"] == fake.name
    posts = [r for r in wire.requests if r["method"] == "POST"]
    assert len(posts) == 1
    request = posts[0]
    assert request["path"] == "/v1beta/models/" + MODEL + ":batchGenerateContent"
    assert result['request_sha256'] == handle['batch_request_sha256']
    assert any(json.loads(path.read_text()).get('wire_request_sha256') == sha(request['body'])
        for path in (engine.root / handle['task_id'] / 'calls').glob('*.start.json'))
    assert request["headers"]["X-goog-api-key"] == "fictional-key-2"
    assert request["headers"]["Content-type"] == "application/json"
    assert int(request["headers"]["Content-length"]) == len(request["body"])
    assert request["headers"]["Accept-encoding"] == "identity"
    assert all(r["accepted_key_ordinal"] == 2 for r in resolutions)
    assert result["accounting"]["http_attempts"] == 2
    assert result["accounting"]["inference_reservations"] == 1
    assert all(r["response_complete"] for r in outcome_rows(engine, handle))
    count = len(wire.requests)
    assert advance(case, provider)["status"] == "OBSERVED"
    assert (
        engine.validate_evidence(handle["task_id"], result, expected=spec)["status"] == "OBSERVED"
    )
    assert len(wire.requests) == count
    for p in (engine.root / handle["task_id"]).rglob("*.json"):
        assert b"fictional-key-" not in p.read_bytes()


@pytest.mark.parametrize(
    "status", [200, 202, 301, 302, 303, 307, 308, 400, 401, 403, 408, 429, 500, 503]
)
def test_native_http_status_and_complete_bytes_no_redirect_or_retry(wire, status):
    raw = b"  raw bytes\x00\xff\r\n"
    wire.plan = lambda _: {
        "status": status,
        "body": raw,
        "headers": {"Location": "http://external.invalid/steal"},
    }
    provider, resolutions = transport(wire)
    body = '{  "x" : "原字节" } \n'.encode()
    response = provider.request(
        "POST",
        f"models/{MODEL}:batchGenerateContent",
        body,
        route=KeyPolicy(1).select("a" * 64),
        timeout_s=2,
    )
    assert response.status == status and response.body == raw
    assert len(wire.requests) == len(resolutions) == 1
    assert wire.requests[0]["body"] == body


@pytest.mark.parametrize("framing", ["length", "chunked", "close"])
def test_native_body_framing_round_trip(wire, framing):
    raw = b"x" * 100003 + b"\x00\xff"
    wire.plan = lambda _: {"body": raw, "framing": framing}
    provider, _ = transport(wire)
    response = provider.request(
        "GET", "batches/offline-001", None, route=KeyPolicy(1).select("a" * 64), timeout_s=5
    )
    assert response.body == raw and response.status == 200


@pytest.mark.parametrize(
    "failure", ["disconnect", "timeout_headers", "timeout_body", "short", "500", "408", "302"]
)
def test_transport_unknown_submit_recovers_query_only_and_preserves_original_key(
    case, wire, failure
):
    engine, _, handle, _ = case
    _, normal = native_plan(wire, handle)

    def plan(request):
        result = normal(request)
        if request["method"] == "POST":
            if failure in {"disconnect", "timeout_headers", "timeout_body"}:
                result["action"] = failure
            elif failure == "short":
                result["framing"] = "short"
            else:
                result.update(
                    status=int(failure),
                    body=b'{"error":"synthetic"}',
                    headers={"Location": "http://external.invalid/never"},
                )
        return result

    wire.plan = plan
    provider, resolutions = transport(wire)
    first = advance(case, provider, key_policy=KeyPolicy(3, (1,)), request_timeout_s=0.2)
    assert first["status"] == "SUBMIT_UNKNOWN"
    rows = outcome_rows(engine, handle)
    assert len(rows) == 1
    if failure in {"timeout_body", "short"}:
        assert rows[0]["http_status"] == 200 and rows[0]["response_complete"] is False
        assert rows[0]["response_sha256"]
    second = advance(case, provider, key_policy=KeyPolicy(3, (1, 2)), allow_realtime_fallback=True)
    assert second["status"] == "OBSERVED"
    assert [r["method"] for r in wire.requests] == ["POST", "GET", "GET"]
    assert all(r["accepted_key_ordinal"] == 2 for r in resolutions)
    assert second["accounting"]["inference_reservations"] == 1


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_transport_http_rejection_remains_failed_without_another_call(case, wire, status):
    raw = b'{"error":{"code":7,"message":"synthetic denial"}}'
    wire.plan = lambda _: {"status": status, "body": raw}
    provider, _ = transport(wire)
    result = advance(case, provider)
    assert result["status"] == "FAILED"
    assert storage.get_blob(case[0].root / case[2]["task_id"], result["response_sha256"]) == raw
    assert advance(case, provider, allow_realtime_fallback=True)["status"] == "FAILED"
    assert len(wire.requests) == 1


@pytest.mark.parametrize("failure", ["short", "timeout_body", "503"])
def test_transport_get_failure_retains_job_and_resumes_same_get(case, wire, failure):
    engine, _, handle, _ = case
    fake, normal = native_plan(wire, handle)
    provider, _ = transport(wire)
    assert advance(case, provider)["status"] == "PENDING"

    def failed(request):
        result = normal(request)
        if failure == "short":
            result["framing"] = "short"
        elif failure == "timeout_body":
            result["action"] = "timeout_body"
        else:
            result["status"] = 503
        return result

    wire.plan = failed
    result = advance(case, provider, request_timeout_s=0.2)
    assert result["status"] == "READBACK_UNKNOWN" and result["job_name"] == fake.name
    wire.plan = normal
    assert advance(case, provider)["status"] == "OBSERVED"
    assert [r["method"] for r in wire.requests] == ["POST", "GET", "GET"]
    assert wire.requests[1]["path"] == wire.requests[2]["path"]
    assert len(outcome_rows(engine, handle)) == 3


def test_complete_json_in_incomplete_http_list_cannot_be_consumed(case, wire):
    _, normal = native_plan(wire, case[2])
    provider, _ = transport(wire)
    wire.plan = lambda request: {**normal(request), "action": "disconnect"}
    advance(case, provider)
    wire.plan = lambda request: {**normal(request), "framing": "short"}
    result = advance(case, provider)
    assert result["status"] == "SUBMIT_UNKNOWN" and result["reason"] == "RECOVERY_READ_UNAVAILABLE"
    assert result["job_name"] is None
    wire.plan = normal
    assert advance(case, provider)["status"] == "OBSERVED"
    assert len([r for r in wire.requests if r["method"] == "POST"]) == 1


@pytest.mark.parametrize("location", ["response", "metadata.output"])
def test_native_transport_operation_contradiction_rejected_through_consumer(case, wire, location):
    engine, spec, handle, _ = case
    fake, normal = native_plan(wire, handle)
    provider, _ = transport(wire)
    advance(case, provider)
    op = fake.op()
    if location == "metadata.output":
        op["metadata"]["output"] = op.pop("response")
    op["error"] = {"code": 7, "message": "synthetic operation error"}
    raw = canonical(op)
    wire.plan = lambda _: {"body": raw}
    result = advance(case, provider)
    assert result["status"] == "RESPONSE_REJECTED" and result["job_name"] == fake.name
    assert storage.get_blob(engine.root / handle["task_id"], result["response_sha256"]) == raw
    assert (
        engine.validate_evidence(handle["task_id"], result, expected=spec)["status"]
        == "RESPONSE_REJECTED"
    )
    assert advance(case, provider, allow_realtime_fallback=True)["status"] == "RESPONSE_REJECTED"
    assert len(wire.requests) == 2


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("https://external.invalid/", "POST", b"{}"),
        ("//external.invalid/", "GET", None),
        ("batches/../secret", "GET", None),
        ("batches/a?key=bad", "GET", None),
        ("batches?pageSize=100&api_key=bad", "GET", None),
        ("batches?pageSize=100&pageSize=200", "GET", None),
        ("batches/offline-001#other", "GET", None),
        (f"models/{MODEL}:generateContent", "POST", "not bytes"),
        ("batches/offline-001", "GET", b"{}"),
        ("batches/offline-001", "DELETE", None),
    ],
)
def test_invalid_path_method_or_body_never_resolves_key_or_sends(wire, path, method, body):
    provider, resolutions = transport(wire)
    with pytest.raises(ValueError):
        provider.request(method, path, body, route=KeyPolicy(1).select("a" * 64), timeout_s=2)
    assert not resolutions and not wire.requests


@pytest.mark.parametrize("failure", ["short", "timeout_body", "disconnect"])
def test_realtime_transport_unknown_cannot_repost_even_when_policy_is_omitted(case, wire, failure):
    fake, normal = native_plan(wire, case[2])
    fake.state = "BATCH_STATE_EXPIRED"
    provider, _ = transport(wire)
    assert finish(case, provider)["status"] == "FAILED"

    def plan(request):
        result = normal(request)
        if request["path"].endswith(":generateContent"):
            if failure == "short":
                result["framing"] = "short"
            else:
                result["action"] = failure
        return result

    wire.plan = plan
    result = advance(case, provider, allow_realtime_fallback=True, request_timeout_s=0.2)
    assert result["status"] == "SUBMIT_UNKNOWN" and result["mode"] == "realtime"
    count = len(wire.requests)
    assert advance(case, provider)["status"] == "SUBMIT_UNKNOWN"
    assert (
        len(wire.requests) == count
        and len([r for r in wire.requests if r["method"] == "POST"]) == 2
    )


def test_default_https_boundary_verifies_certificate_and_disables_debug(monkeypatch):
    seen = []

    def connect(connection):
        seen.append(
            (
                connection.host,
                connection.port,
                connection._context.verify_mode,
                connection._context.check_hostname,
                connection.debuglevel,
            )
        )
        raise OSError("synthetic failure; never open a socket")

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", connect)
    provider = ArgonHttpTransport(lambda _: "fictional-only")
    with pytest.raises(ProviderTransportError, match="HTTP_CONNECTION_ERROR"):
        provider.request(
            "GET", "batches/offline-001", None, route=KeyPolicy(1).select("a" * 64), timeout_s=2
        )
    assert seen == [("generativelanguage.googleapis.com", 443, ssl.CERT_REQUIRED, True, 0)]
    assert API_BASE == "https://generativelanguage.googleapis.com/v1beta/"


def test_continuous_header_trickle_does_not_extend_request_deadline(wire):
    wire.plan = lambda _: {"action": "trickle_headers"}
    provider, _ = transport(wire)
    started = time.monotonic()
    with pytest.raises(ProviderTransportError, match="HTTP_TIMEOUT"):
        provider.request(
            "GET", "batches/offline-001", None, route=KeyPolicy(1).select("a" * 64), timeout_s=0.2
        )
    assert time.monotonic() - started < 0.5
    assert len(wire.requests) == 1


def test_native_realtime_success_keeps_batch_inner_bytes_and_both_ledgers(case, wire):
    fake, _ = native_plan(wire, case[2])
    fake.state = "BATCH_STATE_EXPIRED"
    provider, _ = transport(wire)
    finish(case, provider)
    result = advance(case, provider, allow_realtime_fallback=True)
    assert result["mode"] == "realtime" and result["status"] == "OBSERVED"
    posts = [r for r in wire.requests if r["method"] == "POST"]
    assert len(posts) == 2
    inner = json.loads(posts[0]["body"])["batch"]["inputConfig"]["requests"]["requests"][0][
        "request"
    ]
    assert posts[1]["body"] == canonical(inner)
    assert result["accounting"]["inference_reservations"] == 2
    assert result["accounting"]["histories"][0]["job_name"] == fake.name
    assert advance(case, provider)["status"] == "OBSERVED"
    assert len(wire.requests) == 3


def test_native_paid_route_uses_only_injected_policy_and_resolver(case, wire):
    from test_argon_escalation_witness import paid_policy

    native_plan(wire, case[2])
    provider, resolved = transport(wire)
    policy = paid_policy(case[2]["task_id"])
    result = finish(case, provider, key_policy=policy)
    assert result["status"] == "OBSERVED"
    assert resolved[0]["paid_backup_policy"] == policy.paid_stamp
    assert resolved[0]["accepted_key_tier"] == "paid_backup"
    assert all(r["headers"]["X-goog-api-key"] == "fictional-key-4" for r in wire.requests)
    assert result["usage_receipt"]["actual_charge_usd"] is None


def test_native_pagination_and_cold_object_restart_preserve_one_job(case, wire):
    from src.autoslice.argon_escalation_witness import ArgonEscalationWitness

    fake, normal = native_plan(wire, case[2])

    def plan(request):
        if request["method"] == "POST":
            return {"action": "disconnect"}
        if request["path"] == "/v1beta/batches?pageSize=100":
            return {"body": canonical({"operations": [], "nextPageToken": "a+/=&b"})}
        return normal(request)

    wire.plan = plan
    provider, _ = transport(wire)
    assert advance(case, provider)["status"] == "SUBMIT_UNKNOWN"
    restored = ArgonEscalationWitness(case[0].root)
    second_transport, _ = transport(wire)
    result = restored.advance(case[2]["task_id"], expected=case[1], provider=second_transport)
    assert result["status"] == "OBSERVED" and result["job_name"] == fake.name
    assert [r["path"] for r in wire.requests][1:] == [
        "/v1beta/batches?pageSize=100",
        "/v1beta/batches?pageSize=100&pageToken=a%2B%2F%3D%26b",
        "/v1beta/" + fake.name,
    ]
    assert len([r for r in wire.requests if r["method"] == "POST"]) == 1


@pytest.mark.parametrize("reason", ["duplicate_length", "oversize", "incomplete_403"])
def test_unusable_http_responses_preserve_status_but_never_complete_submission(case, wire, reason):
    engine, _, handle, _ = case
    _, normal = native_plan(wire, handle)

    def plan(request):
        result = normal(request)
        if request["method"] == "POST":
            if reason == "duplicate_length":
                result["headers"] = {"Content-Length": str(len(result["body"]))}
            elif reason == "oversize":
                result["body"] = b"x" * 10000
            else:
                result.update(status=403, framing="short", body=b'{"error":"synthetic"}')
        return result

    wire.plan = plan
    provider, _ = transport(wire, max_response_bytes=8000)
    result = advance(case, provider)
    assert result["status"] == "SUBMIT_UNKNOWN"
    row = outcome_rows(engine, handle)[0]
    assert row["http_status"] == (403 if reason == "incomplete_403" else 200)
    assert row["response_complete"] is False and row["transport_reason"]
    assert advance(case, provider)["status"] == "OBSERVED"
    assert len([r for r in wire.requests if r["method"] == "POST"]) == 1


@pytest.mark.parametrize("kind", ["route", "resolver_error", "header_injection"])
def test_invalid_key_boundary_is_safe_and_has_no_http_dispatch(wire, kind):
    seen = []

    def resolver(route):
        seen.append(route)
        if kind == "resolver_error":
            raise RuntimeError("fictional-secret-do-not-log")
        return "fictional-key\r\nInjected: bad"

    provider = ArgonHttpTransport(resolver, connection_factory=wire.factory)
    route = KeyPolicy(1).select("a" * 64)
    if kind == "route":
        route["api_key"] = "must-not-be-resolved"
    with pytest.raises(ValueError) as captured:
        provider.request("GET", "batches/offline-001", None, route=route, timeout_s=2)
    assert "fictional" not in str(captured.value)
    assert not wire.requests and len(seen) == (0 if kind == "route" else 1)
