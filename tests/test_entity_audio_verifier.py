import io
import json
from pathlib import Path

from src.autoslice import entity_audio_verifier as verifier_module


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _request():
    return {
        "schema_version": "chat-entity-verification-request.v1",
        "request_sha256": "a" * 64,
        "evidence_id": "b" * 64,
        "matched_start_ms": 2_000,
        "matched_end_ms": 4_000,
        # These locate/audit the outer decision but must never enter AGY's prompt.
        "exact_text": "还没看，怎么有人说有母鸡卡的风险",
        "matched_audio_text": "还没看怎么有人说有母鸡卡的风险",
        "candidate_entities": [
            {"canonical": "梦限大", "surfaces": ["梦限大", "梦现代"], "readings": ["meng xian da"]},
            {"canonical": "Ave Mujica", "surfaces": ["Mujica", "母鸡卡"], "readings": ["mujica"]},
        ],
    }


def _context_request():
    request = _request()
    request.update(
        {
            "schema_version": "subtitle-span-acoustic-check-request.v1",
            "context_start_ms": 500,
            "context_end_ms": 7_500,
            "context_before": "一直在说欠了很多首歌",
            "context_after": "还欠两个",
            "candidate_entities": [
                {
                    "candidate_id": "CURRENT",
                    "canonical": "还没有歌杂呢",
                    "surfaces": [],
                    "readings": [],
                },
                {
                    "candidate_id": "PROPOSED",
                    "canonical": "还没有歌债呢",
                    "surfaces": [],
                    "readings": [],
                },
            ],
        }
    )
    return request


def test_audio_verifier_uses_black_frame_clip_and_neutral_prompt(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source video pixels and audio")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"black frame plus cropped audio")
            return _Completed()
        job_dir = Path(kwargs["cwd"])
        (job_dir / "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "梦限大",
                    "heard_syllables": "meng xian da",
                    "confidence": 0.98,
                    "reason": "three distinct syllables",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "梦限大"
    ffmpeg = commands[0]
    assert "color=c=black:s=320x240:r=10" in ffmpeg
    assert ffmpeg[ffmpeg.index("-map") + 1] == "1:v:0"
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
    assert "还没看" not in prompt
    assert "怎么有人说" not in prompt
    assert "viewer chat" in prompt
    assert verdict["source_media_sha256"] != verdict["audio_clip_sha256"]


def test_audio_verifier_low_confidence_is_uncertain(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")

    def fake_run(command, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"audio")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "RESOLVED",
                    "canonical_entity": "梦限大",
                    "heard_syllables": "unclear",
                    "confidence": 0.79,
                    "reason": "unclear",
                }
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    assert verify(_request())["status"] == "UNCERTAIN"


def test_context_verifier_crops_adjacent_audio_and_exposes_bounded_discourse(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"context audio")
            return _Completed()
        Path(kwargs["cwd"], "verdict.json").write_text(
            json.dumps(
                {
                    "schema_version": "entity-audio-observation.v1",
                    "status": "OBSERVED",
                    "target_audible": True,
                    "heard_syllables": "hai mei you ge zhai ne",
                    "current_fit": "PLAUSIBLE",
                    "proposed_fit": "SUPPORTED",
                    "confidence_current": 0.71,
                    "confidence_proposed": 0.91,
                    "reason": "reduced final is acoustically ambiguous; repeated debt context breaks tie",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _Completed()

    monkeypatch.setattr(verifier_module.subprocess, "run", fake_run)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-15",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_context_request())

    assert verdict["status"] == "OBSERVED"
    assert verdict["current_fit"] == "PLAUSIBLE"
    assert verdict["proposed_fit"] == "SUPPORTED"
    ffmpeg = commands[0]
    assert ffmpeg[ffmpeg.index("-ss") + 1] == "0.500"
    assert ffmpeg[ffmpeg.index("-t") + 1] == "7.000"
    prompt = (tmp_path / "out/entity_verdicts" / ("a" * 20) / "prompt.md").read_text(
        encoding="utf-8"
    )
    assert "一直在说欠了很多首歌" in prompt
    assert "还欠两个" in prompt
    assert "1500 ms" in prompt and "3500 ms" in prompt
    assert "Semantic plausibility must never" in prompt
    assert "INCOMPATIBLE" in prompt
    assert '"current_fit"' in prompt and '"proposed_fit"' in prompt
    assert '"candidate_id": "one exact candidate_id above, or null"' not in prompt
    assert '"canonical_entity": "one exact candidate sentence' not in prompt
    assert "Use RESOLVED only" not in prompt


def _agy_quota_run(command, **kwargs):
    if command[0] == "ffmpeg":
        Path(command[-1]).write_bytes(b"fake media payload")
        return _Completed()
    return _Completed(returncode=1, stderr="Error: Individual quota reached. Resets in 1h.")


class _FakeApiResponse:
    def __init__(self, observation):
        body = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": json.dumps(observation, ensure_ascii=False)}]}}]}
        )
        self._stream = io.StringIO(body)

    def read(self, *args, **kwargs):
        return self._stream.read(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _resolved_observation():
    return {
        "schema_version": "entity-audio-observation.v1",
        "status": "RESOLVED",
        "canonical_entity": "梦限大",
        "heard_syllables": "meng xian da",
        "confidence": 0.97,
        "reason": "three clear syllables",
    }


def _isolate_policy_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "policy-base"))
    for name in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEY_3",
        "GEMINI_KEY_BACKUP",
        "GEMINI_PAID_BACKUP_DEV_EXCEPTION",
        "GEMINI_PAID_BACKUP_DAILY_CAP",
        "ENTITY_AUDIO_GEMINI_API_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_provider_failed_verdict_is_never_served_from_cache(tmp_path, monkeypatch):
    """2026-07-14 配额期中毒实证：断供期的 PROVIDER_FAILED 判决被 manifest
    固化后，重试必须重听而不是永远命中缓存。"""
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)

    api_calls = []

    def failing_urlopen(request, timeout=0):
        api_calls.append(1)
        raise TimeoutError("quota outage")

    monkeypatch.setattr(verifier_module.urllib.request, "urlopen", failing_urlopen)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )
    first = verify(_request())
    assert first["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"
    assert api_calls  # 断供也确实尝试过

    # 供应商恢复：同一请求必须重听并 RESOLVED，而不是回放缓存的失败判决
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeApiResponse(_resolved_observation()),
    )
    second = verify(_request())
    assert second["status"] == "RESOLVED", second
    assert second["canonical_entity"] == "梦限大"

    # RESOLVED 判决可以缓存复用：第三次不再发起任何新调用
    calls_before = len(api_calls)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: (_ for _ in ()).throw(AssertionError("must hit cache")),
    )
    third = verify(_request())
    assert third["status"] == "RESOLVED"
    assert len(api_calls) == calls_before


def test_agy_quota_falls_back_to_gemini_api_free_key(tmp_path, monkeypatch):
    """Ivan 2026-07-14：付费/免费 API key 都能裁决音频——AGY 配额断供必须
    自动切到 Gemini API 直连，验收逻辑与 AGY 通道完全一致。"""
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_API_KEY", "free-key-1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    seen_requests = []

    def fake_urlopen(request, timeout=0):
        seen_requests.append(request)
        return _FakeApiResponse(_resolved_observation())

    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(verifier_module.urllib.request, "urlopen", fake_urlopen)
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["canonical_entity"] == "梦限大"
    assert verdict["provider"] == "gemini_api"
    assert verdict["model"] == "gemini-3.5-flash"
    assert verdict["key_tier"] == "free"
    assert seen_requests[0].headers.get("X-goog-api-key") == "free-key-1"
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    api_prompt = (job_dir / "prompt.gemini-api.md").read_text(encoding="utf-8")
    assert "attached audio clip" in api_prompt
    assert "还没看" not in api_prompt
    manifest = json.loads((job_dir / "verdict.manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider"] == "gemini_api"
    assert any(
        row["category"] == "AGY_QUOTA_EXHAUSTED" for row in manifest["provider_failures"]
    )


def test_api_paid_gate_blocks_without_dev_exception(tmp_path, monkeypatch):
    """免费 key 未配置且无 DEV_EXCEPTION：付费 key 在 3 strike 前必须被门拦，
    整体退 UNCERTAIN(PROVIDER_FAILED)，不允许任何未入帐付费调用。"""
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-key")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    api_calls = []
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda *a, **k: api_calls.append(1) or (_ for _ in ()).throw(AssertionError("no api call allowed")),
    )
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "UNCERTAIN"
    assert verdict["reason_code"] == "ENTITY_AUDIO_PROVIDER_FAILED"
    assert api_calls == []
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    failures = json.loads((job_dir / "provider-failures.json").read_text(encoding="utf-8"))
    assert any(
        str(row.get("category", "")).startswith("PAID_BACKUP_SKIPPED:FREE_CHAIN_STRIKES_0")
        for row in failures["failures"]
    )
    ledger_root = tmp_path / "policy-base/state/gemini-paid-backup"
    assert list(ledger_root.glob("strikes/*.json"))
    assert not list(ledger_root.glob("usage-*.jsonl"))


def test_api_paid_used_under_dev_exception_and_ledgered(tmp_path, monkeypatch):
    _isolate_policy_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GEMINI_KEY_BACKUP", "paid-key")
    monkeypatch.setenv("GEMINI_PAID_BACKUP_DEV_EXCEPTION", "1")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    monkeypatch.setattr(verifier_module.subprocess, "run", _agy_quota_run)
    monkeypatch.setattr(
        verifier_module.urllib.request,
        "urlopen",
        lambda request, timeout=0: _FakeApiResponse(_resolved_observation()),
    )
    verify = verifier_module.build_local_audio_entity_verifier(
        source_media=source,
        output_dir=tmp_path / "out",
        recording_date="2026-07-10",
        source_duration_ms=10_000,
        agy_bin="agy-test",
    )

    verdict = verify(_request())

    assert verdict["status"] == "RESOLVED"
    assert verdict["key_tier"] == "paid_backup"
    ledger_root = tmp_path / "policy-base/state/gemini-paid-backup"
    usage_files = list(ledger_root.glob("usage-*.jsonl"))
    assert len(usage_files) == 1
    rows = [json.loads(line) for line in usage_files[0].read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["purpose"] == "entity_audio_verdict"
    assert "paid-key" not in usage_files[0].read_text(encoding="utf-8")
    job_dir = tmp_path / "out/entity_verdicts" / ("a" * 20)
    manifest = json.loads((job_dir / "verdict.manifest.json").read_text(encoding="utf-8"))
    assert manifest["paid_backup_policy"]
