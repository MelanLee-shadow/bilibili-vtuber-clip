from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping
import urllib.error

import pytest

from src.autoslice.gemini_file_api import (
    GeminiFileApiError,
    UrllibGeminiFileTransport,
    call_file_model,
    configured_free_keys,
    run_free_key_file_call,
)


MODEL = "gemini-3.8-flash"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeTransport:
    def __init__(
        self,
        *,
        states: list[str | None] | None = None,
        text: str = '{"status":"PASS"}',
        fail: tuple[str, int | None, bool] | None = None,
        delete_fails: bool = False,
    ):
        self.states = list(states or ["ACTIVE"])
        self.text = text
        self.fail = fail
        self.delete_fails = delete_fails
        self.calls: list[dict[str, object]] = []
        self.uploaded = b""

    @staticmethod
    def _file(state: str | None, mime: str) -> dict[str, object]:
        resource: dict[str, object] = {"name": "files/test-resource"}
        if state is not None:
            resource.update(
                {
                    "uri": (
                        "https://generativelanguage.googleapis.com/"
                        "v1beta/files/test-resource"
                    ),
                    "mimeType": mime,
                    "state": state,
                }
            )
        return {"file": resource}

    def _maybe_fail(self, phase: str) -> None:
        if self.fail is None or self.fail[0] != phase:
            return
        _phase, status, ambiguous = self.fail
        raise GeminiFileApiError(
            "GEMINI_API_QUOTA_EXHAUSTED"
            if status == 429
            else "GEMINI_API_NETWORK_ERROR",
            "synthetic failure",
            phase=phase,
            http_status=status,
            ambiguous=ambiguous,
        )

    def start_upload(self, **kwargs: object) -> str:
        self._maybe_fail("upload_start")
        self.calls.append({"phase": "upload_start", **kwargs})
        return "https://generativelanguage.googleapis.com/upload/session/abc"

    def upload(self, **kwargs: object) -> Mapping[str, object]:
        self._maybe_fail("upload_bytes")
        descriptor = int(kwargs["descriptor"])
        size = int(kwargs["size"])
        duplicate = os.dup(descriptor)
        try:
            with os.fdopen(duplicate, "rb") as handle:
                self.uploaded = handle.read()
        finally:
            pass
        assert len(self.uploaded) == size
        self.calls.append(
            {
                "phase": "upload_bytes",
                "size": size,
                "upload_url": kwargs["upload_url"],
            }
        )
        return self._file(self.states.pop(0), "video/mp4")

    def get_file(self, **kwargs: object) -> Mapping[str, object]:
        self._maybe_fail("file_poll")
        self.calls.append({"phase": "file_poll", **kwargs})
        return self._file(self.states.pop(0), "video/mp4")

    def generate(self, **kwargs: object) -> Mapping[str, object]:
        self._maybe_fail("generate_content")
        self.calls.append({"phase": "generate_content", **kwargs})
        return {
            "candidates": [
                {"content": {"parts": [{"text": self.text}]}}
            ]
        }

    def delete_file(self, **kwargs: object) -> None:
        self.calls.append({"phase": "file_delete", **kwargs})
        if self.delete_fails:
            raise GeminiFileApiError(
                "GEMINI_API_NETWORK_ERROR",
                "synthetic delete failure",
                phase="file_delete",
                ambiguous=True,
            )


def test_exact_file_upload_poll_generate_and_cleanup(tmp_path: Path):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact-final-video-bytes" * 100)
    fake = FakeTransport(states=[None, "PROCESSING", "ACTIVE"])

    outcome = call_file_model(
        media_path=media,
        expected_sha256=_sha(media),
        expected_bytes=media.stat().st_size,
        mime_type="video/mp4",
        prompt="review exact video",
        key="free-key",
        model=MODEL,
        video_fps=5.0,
        timeout_seconds=30,
        poll_timeout_seconds=30,
        poll_interval_seconds=0,
        transport=fake,
        sleeper=lambda _seconds: None,
    )

    assert outcome.text == '{"status":"PASS"}'
    assert fake.uploaded == media.read_bytes()
    assert [row["phase"] for row in fake.calls] == [
        "upload_start",
        "upload_bytes",
        "file_poll",
        "file_poll",
        "generate_content",
        "file_delete",
    ]
    assert outcome.evidence["media_sha256"] == _sha(media)
    assert outcome.evidence["media_bytes"] == media.stat().st_size
    assert outcome.evidence["mime_type"] == "video/mp4"
    assert outcome.evidence["active_state"] == "ACTIVE"
    assert outcome.evidence["poll_count"] == 2
    assert outcome.evidence["http_request_count"] == 6
    assert outcome.evidence["cleanup_status"] == "DELETED"
    serialized = json.dumps(outcome.evidence)
    assert "free-key" not in serialized
    assert "test-resource" not in serialized


def test_local_hash_drift_is_provider_free(tmp_path: Path):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact bytes")
    fake = FakeTransport()

    with pytest.raises(
        GeminiFileApiError, match="differs from its exact request binding"
    ):
        call_file_model(
            media_path=media,
            expected_sha256="0" * 64,
            expected_bytes=media.stat().st_size,
            mime_type="video/mp4",
            prompt="review",
            key="free-key",
            model=MODEL,
            video_fps=5.0,
            timeout_seconds=30,
            poll_timeout_seconds=30,
            transport=fake,
        )

    assert fake.calls == []


def test_cleanup_failure_is_recorded_without_discarding_valid_observation(
    tmp_path: Path,
):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact bytes")
    fake = FakeTransport(delete_fails=True)

    outcome = call_file_model(
        media_path=media,
        expected_sha256=_sha(media),
        expected_bytes=media.stat().st_size,
        mime_type="video/mp4",
        prompt="review",
        key="free-key",
        model=MODEL,
        video_fps=5.0,
        timeout_seconds=30,
        poll_timeout_seconds=30,
        transport=fake,
    )

    assert outcome.evidence["cleanup_status"] == (
        "DELETE_FAILED_EXPIRES_48H"
    )


def test_free_key_ladder_rotates_only_explicit_auth_or_quota(
    tmp_path: Path,
):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact bytes")
    transports = [
        FakeTransport(fail=("upload_start", 429, False)),
        FakeTransport(),
    ]

    outcome = run_free_key_file_call(
        media_path=media,
        expected_sha256=_sha(media),
        expected_bytes=media.stat().st_size,
        mime_type="video/mp4",
        prompt="review",
        model=MODEL,
        video_fps=5.0,
        environment={
            "GEMINI_API_KEY": "free-one",
            "GEMINI_API_KEY_2": "free-two",
            "GEMINI_KEY_BACKUP": "paid-must-not-be-read",
        },
        timeout_seconds=30,
        poll_timeout_seconds=30,
        transport_factory=lambda: transports.pop(0),
        sleeper=lambda _seconds: None,
    )

    assert outcome.evidence["key_tier"] == "free"
    assert outcome.evidence["key_name"] == "GEMINI_API_KEY_2"
    assert outcome.evidence["key_ordinal"] == 2
    assert outcome.evidence["configured_free_key_count"] == 2
    assert outcome.evidence["paid_backup_used"] is False
    assert outcome.evidence["prior_failures"] == [
        {
            "key_tier": "free",
            "key_name": "GEMINI_API_KEY",
            "key_ordinal": 1,
            "reason_code": "GEMINI_API_QUOTA_EXHAUSTED",
            "phase": "upload_start",
            "ambiguous": False,
            "cleanup_status": "NOT_STARTED",
            "http_status": 429,
        }
    ]
    assert "paid-must-not-be-read" not in json.dumps(outcome.evidence)


def test_ambiguous_failure_never_rotates_to_another_key(tmp_path: Path):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact bytes")
    first = FakeTransport(fail=("generate_content", None, True))
    second = FakeTransport()
    transports = [first, second]

    with pytest.raises(GeminiFileApiError) as raised:
        run_free_key_file_call(
            media_path=media,
            expected_sha256=_sha(media),
            expected_bytes=media.stat().st_size,
            mime_type="video/mp4",
            prompt="review",
            model=MODEL,
            video_fps=5.0,
            environment={
                "GEMINI_API_KEY": "free-one",
                "GEMINI_API_KEY_2": "free-two",
            },
            timeout_seconds=30,
            poll_timeout_seconds=30,
            transport_factory=lambda: transports.pop(0),
            sleeper=lambda _seconds: None,
        )

    assert raised.value.ambiguous is True
    assert len(transports) == 1
    assert [row["phase"] for row in first.calls][-1] == "file_delete"


def test_configured_free_keys_deduplicates_and_ignores_paid_key():
    assert configured_free_keys(
        {
            "GEMINI_API_KEY": "same",
            "GEMINI_API_KEY_2": "same",
            "GEMINI_API_KEY_3": "third",
            "GEMINI_KEY_BACKUP": "paid",
        }
    ) == [
        ("GEMINI_API_KEY", "same"),
        ("GEMINI_API_KEY_3", "third"),
    ]


class _Response:
    def __init__(self, *, headers: Mapping[str, str], body: bytes = b"{}"):
        self.headers = dict(headers)
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        return self.body


def test_upload_start_keeps_key_in_header_and_rejects_foreign_upload_url():
    seen: dict[str, object] = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _Response(
            headers={"x-goog-upload-url": "https://evil.example/upload"}
        )

    transport = UrllibGeminiFileTransport(urlopen=fake_urlopen)
    with pytest.raises(
        GeminiFileApiError, match="outside the accepted Google HTTPS origin"
    ):
        transport.start_upload(
            key="secret-key",
            size=123,
            mime_type="video/mp4",
            display_name="autoslice-test",
            timeout_seconds=30,
        )

    assert "secret-key" not in str(seen["url"])
    assert seen["headers"]["X-goog-api-key"] == "secret-key"
    assert seen["body"] == {"file": {"display_name": "autoslice-test"}}


def test_explicit_http_429_is_typed_without_secret_leak():
    error = urllib.error.HTTPError(
        "https://generativelanguage.googleapis.com/upload/v1beta/files",
        429,
        "quota",
        {},
        None,
    )

    def fake_urlopen(_request, timeout=None):
        del timeout
        raise error

    transport = UrllibGeminiFileTransport(urlopen=fake_urlopen)
    with pytest.raises(GeminiFileApiError) as raised:
        transport.start_upload(
            key="never-print-this",
            size=123,
            mime_type="video/mp4",
            display_name="autoslice-test",
            timeout_seconds=30,
        )

    assert raised.value.reason_code == "GEMINI_API_QUOTA_EXHAUSTED"
    assert raised.value.http_status == 429
    assert raised.value.ambiguous is False
    assert "never-print-this" not in str(raised.value)


def test_postflight_media_drift_overrides_provider_failure(tmp_path: Path):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact bytes before dispatch")

    class MutatingTransport(FakeTransport):
        def generate(self, **kwargs: object) -> Mapping[str, object]:
            self.calls.append({"phase": "generate_content", **kwargs})
            media.write_bytes(b"different bytes during dispatch")
            raise GeminiFileApiError(
                "GEMINI_API_NETWORK_ERROR",
                "synthetic provider failure",
                phase="generate_content",
                ambiguous=True,
            )

    with pytest.raises(GeminiFileApiError) as raised:
        call_file_model(
            media_path=media,
            expected_sha256=_sha(media),
            expected_bytes=media.stat().st_size,
            mime_type="video/mp4",
            prompt="review",
            key="free-key",
            model=MODEL,
            video_fps=5.0,
            timeout_seconds=30,
            poll_timeout_seconds=30,
            transport=MutatingTransport(),
        )

    assert raised.value.reason_code == "GEMINI_API_LOCAL_FILE_DRIFT"
    assert raised.value.phase == "local_file_postflight"
    assert raised.value.ambiguous is True


def test_generate_content_binds_static_video_fps_in_request(monkeypatch):
    seen: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size: int = -1) -> bytes:
            return json.dumps(
                {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "src.autoslice.gemini_file_api.urllib.request.urlopen", fake_urlopen
    )
    transport = UrllibGeminiFileTransport(urlopen=fake_urlopen)
    result = transport.generate(
        key="secret-key",
        model=MODEL,
        file_uri=(
            "https://generativelanguage.googleapis.com/v1beta/files/test-resource"
        ),
        mime_type="video/mp4",
        prompt="inspect temporal details",
        video_fps=5.0,
        timeout_seconds=30,
    )

    assert result["candidates"][0]["content"]["parts"][0]["text"] == "ok"
    part = seen["body"]["contents"][0]["parts"][0]
    assert part["file_data"]["mime_type"] == "video/mp4"
    assert part["video_metadata"] == {"fps": 5.0}
    assert seen["headers"]["X-goog-api-key"] == "secret-key"
    assert "secret-key" not in str(seen["url"])


def test_video_requires_explicit_valid_fps_before_provider_call(tmp_path: Path):
    media = tmp_path / "final.mp4"
    media.write_bytes(b"exact bytes")
    fake = FakeTransport()

    for invalid in (None, 0.0, 24.1, True):
        with pytest.raises(
            GeminiFileApiError,
            match="video FPS",
        ):
            call_file_model(
                media_path=media,
                expected_sha256=_sha(media),
                expected_bytes=media.stat().st_size,
                mime_type="video/mp4",
                prompt="review",
                key="free-key",
                model=MODEL,
                video_fps=invalid,
                timeout_seconds=30,
                poll_timeout_seconds=30,
                transport=fake,
            )

    assert fake.calls == []


def test_audio_rejects_video_sampling_metadata_before_provider_call(tmp_path: Path):
    media = tmp_path / "full.wav"
    media.write_bytes(b"RIFF exact audio bytes")
    fake = FakeTransport()

    with pytest.raises(GeminiFileApiError, match="video FPS"):
        call_file_model(
            media_path=media,
            expected_sha256=_sha(media),
            expected_bytes=media.stat().st_size,
            mime_type="audio/wav",
            prompt="listen",
            key="free-key",
            model=MODEL,
            video_fps=5.0,
            timeout_seconds=30,
            poll_timeout_seconds=30,
            transport=fake,
        )

    assert fake.calls == []
