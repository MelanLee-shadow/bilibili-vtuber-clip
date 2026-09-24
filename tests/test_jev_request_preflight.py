"""Synthetic local preflight tests; no provider request is made."""
import pytest
from src.autoslice import jev_request_preflight as m

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
BODY = b'{"state":"synthetic"}'


@pytest.mark.parametrize("value", [None, "", " ", "key\nsecond", "key\rsecond", "key\t", " key", "key ", "密钥", 123])
def test_invalid_key_fails_before_request_construction(monkeypatch, value):
    calls = []
    monkeypatch.setattr(m, "Request", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises(m.JevRequestPreflightError) as err:
        m.prepare_jev_request(BODY, endpoint=ENDPOINT, environment={"TYPESAFE_API_KEY": value})
    assert calls == []
    assert str(err.value) in {"TYPESAFE_CREDENTIAL_MISSING", "TYPESAFE_CREDENTIAL_INVALID"}


def test_no_request_when_key_absent(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(m.JevRequestPreflightError, match="TYPESAFE_CREDENTIAL_MISSING"):
        m.prepare_jev_request(BODY, endpoint=ENDPOINT)


@pytest.mark.parametrize("body", [b"", "not-bytes", b"x" * 64001])
def test_original_body_cap_is_kept(body):
    with pytest.raises(m.JevRequestPreflightError, match="REQUEST_OVER_EXISTING_LOCAL_BODY_CAP"):
        m.prepare_jev_request(body, endpoint=ENDPOINT, environment={"TYPESAFE_API_KEY": "unit-only"})


def test_endpoint_is_not_redirected():
    with pytest.raises(m.JevRequestPreflightError, match="TYPESAFE_ENDPOINT_MISMATCH"):
        m.prepare_jev_request(BODY, endpoint="https://example.invalid/", environment={"TYPESAFE_API_KEY": "unit-only"})


def test_request_bytes_and_headers_unchanged_and_repr_secret_free():
    prepared = m.prepare_jev_request(BODY, endpoint=ENDPOINT, environment={"TYPESAFE_API_KEY": "unit-only"})
    assert prepared.request.data == BODY
    assert prepared.request.full_url == ENDPOINT
    assert prepared.request.get_method() == "POST"
    assert prepared.request.get_header("Authorization") == "Bearer unit-only"
    assert "unit-only" not in repr(prepared)


def test_echo_check_keeps_request_snapshot_when_environment_changes(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "initial-unit-secret")
    prepared = m.prepare_jev_request(BODY, endpoint=ENDPOINT)
    monkeypatch.setenv("TYPESAFE_API_KEY", "different-unit-secret")
    assert prepared.credential_echoed(b"echo initial-unit-secret")
    assert not prepared.credential_echoed(b"clean response")


def test_constructor_failure_does_not_echo_credentials(monkeypatch):
    def fail(*a, **kw):
        raise ValueError("echo unit-only-secret " + repr(kw))
    monkeypatch.setattr(m, "Request", fail)
    with pytest.raises(m.JevRequestPreflightError) as err:
        m.prepare_jev_request(BODY, endpoint=ENDPOINT, environment={"TYPESAFE_API_KEY": "unit-only-secret"})
    assert str(err.value) == "TYPESAFE_REQUEST_CONSTRUCTION_FAILED"
    assert "unit-only-secret" not in str(err.value)
