from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import src.autoslice.producer_external_audio_acquisition as acquisition
import src.autoslice.producer_nested_acoustic as nested_acoustic
import scripts.produce_slice_package as package_driver
from src.autoslice.producer_external_audio_acquisition import (
    ACQUISITION_RESULT_SCHEMA,
    SOURCE_MAP_SCHEMA,
    _source_map as load_source_map,
    prepare_external_audio_reference,
)
from src.autoslice.producer_nested_external_audio import (
    CATALOG_ENV,
    resolve_external_audio_reference,
)
from tests.test_producer_nested_acoustic import _external_button_scene


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_map(tmp_path: Path, *, buttons: list[dict[str, str]] | None = None) -> Path:
    buttons = buttons or [
        {"label": "宝宝对不起", "url": "https://official.example/audio/apology.mp3"},
        {"label": "你最好了", "url": "https://official.example/audio/best.mp3"},
    ]
    path = tmp_path / "official-map.json"
    path.write_text(
        json.dumps({
            "schema_version": SOURCE_MAP_SCHEMA,
            "source_url": "https://official.example/soundboard",
            "buttons": buttons,
        }, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return path


def _seed_cache(
    map_path: Path, cache_root: Path, *, label: str, url: str, payload: bytes = b"official audio",
) -> Path:
    map_sha = _sha(map_path.read_bytes())
    route_sha = _sha(url.encode("utf-8"))
    route_dir = cache_root / map_sha / route_sha
    route_dir.mkdir(parents=True)
    audio_path = route_dir / "audio.mp3"
    audio_path.write_bytes(payload)
    audio_sha = _sha(payload)
    (route_dir / "decode.json").write_text(json.dumps({
        "status": "COMPLETE", "audio_sha256": f"sha256:{audio_sha}",
    }, sort_keys=True), encoding="utf-8")
    (route_dir / "record.json").write_text(json.dumps({
        "schema_version": ACQUISITION_RESULT_SCHEMA,
        "source_map_sha256": f"sha256:{map_sha}",
        "label": label,
        "source_uri": url,
        "audio_path": str(audio_path),
        "audio_sha256": f"sha256:{audio_sha}",
    }, sort_keys=True), encoding="utf-8")
    return route_dir


def test_profile_map_preserves_verified_public_map_bytes():
    path = Path("assets/lidousha/external_audio_sources.v1.json").resolve()
    _path, map_sha, document = load_source_map(path)
    assert map_sha == "5c1e73813e2879ae3e9c12fed758e29b5164aa6d08aaf3cf0f592e8396b70406"
    assert document["page_sha256"] == "d88308a3a966fbdef8835710a5e737482ea6021df2fd346ede7ed02b99dbcf42"
    assert document["button_count"] == len(document["buttons"]) == 30


def test_profile_map_selects_exact_visible_route_and_reuses_cache(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path)
    cache_root = tmp_path / "cache"
    url = "https://official.example/audio/apology.mp3"
    _seed_cache(map_path, cache_root, label="宝宝对不起", url=url)

    def no_network(*_args, **_kwargs):
        raise AssertionError("a valid current cache must not download")

    monkeypatch.setattr(acquisition, "urlopen", no_network)
    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=cache_root, cue_text="宝宝对不起",
    )

    assert result["status"] == "READY"
    assert result["label"] == "宝宝对不起"
    assert result["selection_basis"] == "CURRENT_CUE_EXACT_VISIBLE_LABEL"
    assert result["acquisition"]["status"] == "CACHE_HIT"
    assert result["acquisition"]["source_map_sha256"] == f"sha256:{_sha(map_path.read_bytes())}"
    assert result["source_role_authority"] is False
    assert Path(result["catalog_path"]).is_file()
    resolved = resolve_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        catalog_value=result["catalog_value"], cue_text="宝宝对不起",
    )
    assert resolved["status"] == "READY"
    assert resolved["source_cache_binding"]["source_uri"] == url


def test_ambiguous_controls_keep_unknown_without_guessing_route(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path)
    calls = []

    def no_network(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("ambiguous labels must not download")

    monkeypatch.setattr(acquisition, "urlopen", no_network)
    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起", "你最好了"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=tmp_path / "cache",
    )

    assert result["status"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "EXTERNAL_AUDIO_SOURCE_LABEL_AMBIGUOUS"
    assert calls == []


def test_cache_audio_hash_drift_is_rejected_without_redownload(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path)
    url = "https://official.example/audio/apology.mp3"
    route_dir = _seed_cache(map_path, tmp_path / "cache", label="宝宝对不起", url=url)
    record = json.loads((route_dir / "record.json").read_text(encoding="utf-8"))
    record["audio_sha256"] = "sha256:" + ("0" * 64)
    (route_dir / "record.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    def no_network(*_args, **_kwargs):
        raise AssertionError("cache drift must fail closed before download")

    monkeypatch.setattr(acquisition, "urlopen", no_network)
    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=tmp_path / "cache", cue_text="宝宝对不起",
    )

    assert result["status"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "EXTERNAL_AUDIO_CACHE_AUDIO_HASH_MISMATCH"
    assert result["acquisition_receipt_path"].endswith("failure.json")


def test_cache_url_drift_is_rejected_without_redownload(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path)
    url = "https://official.example/audio/apology.mp3"
    route_dir = _seed_cache(map_path, tmp_path / "cache", label="宝宝对不起", url=url)
    record = json.loads((route_dir / "record.json").read_text(encoding="utf-8"))
    record["source_uri"] = "https://official.example/audio/other.mp3"
    (route_dir / "record.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    monkeypatch.setattr(acquisition, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("cache URL drift must fail closed before download")
    ))
    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=tmp_path / "cache", cue_text="宝宝对不起",
    )

    assert result["status"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "EXTERNAL_AUDIO_CACHE_BINDING_MISMATCH"


def test_download_once_writes_decode_and_resolver_catalog(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path, buttons=[
        {"label": "宝宝对不起", "url": "https://official.example/audio/apology.mp3"},
    ])
    payload = b"one real route byte stream"
    calls: list[object] = []

    class Response:
        status = 200
        headers = {"Content-Type": "audio/mpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://official.example/audio/apology.mp3"

        def read(self, _size):
            if calls:
                return b""
            calls.append("read")
            return payload

    monkeypatch.setattr(acquisition, "urlopen", lambda *_args, **_kwargs: Response())

    def fake_run(command, **_kwargs):
        calls.append(tuple(command[:1]))
        if command[0] == "ffprobe":
            return SimpleNamespace(stdout=json.dumps({"streams": [{"codec_type": "audio"}]}))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(acquisition.subprocess, "run", fake_run)
    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": "宝宝对不起"},
        source_map_path=map_path, cache_root=tmp_path / "cache",
        cue_text="宝宝对不起",
    )
    assert result["status"] == "READY"
    assert result["acquisition"]["status"] == "DOWNLOADED"
    assert Path(result["catalog_path"]).is_file()
    assert Path(result["acquisition"]["download_receipt_path"]).is_file()
    assert Path(result["catalog_path"]).read_text(encoding="utf-8")
    assert calls.count("read") == 1
    assert ("ffprobe",) in calls and ("ffmpeg",) in calls

    monkeypatch.setattr(acquisition, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("second identical request must use cache")
    ))
    cached = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": "宝宝对不起"},
        source_map_path=map_path, cache_root=tmp_path / "cache",
        cue_text="宝宝对不起",
    )
    assert cached["status"] == "READY"
    assert cached["acquisition"]["status"] == "CACHE_HIT"


def test_redirect_path_drift_is_unknown_and_preserves_effective_url(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path, buttons=[
        {"label": "宝宝对不起", "url": "https://official.example/audio/apology.mp3"},
    ])

    class Response:
        status = 200
        headers = {"Content-Type": "audio/mpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://official.example/audio/other.mp3"

        def read(self, _size):
            return b"should not be accepted"

    calls = []
    monkeypatch.setattr(acquisition, "urlopen", lambda *_args, **_kwargs: (calls.append(1) or Response()))
    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=tmp_path / "cache", cue_text="宝宝对不起",
    )

    assert result["status"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "EXTERNAL_AUDIO_REDIRECT_URL_MISMATCH"
    assert calls == [1]
    receipt = json.loads(Path(result["acquisition_receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["source_uri"] == "https://official.example/audio/apology.mp3"
    assert receipt["effective_url"] == "https://official.example/audio/other.mp3"
    assert not list((tmp_path / "cache").rglob("audio.mp3"))


def test_unbound_audio_is_kept_without_redownload_or_overwrite(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path, buttons=[
        {"label": "宝宝对不起", "url": "https://official.example/audio/apology.mp3"},
    ])
    audio = b"operator-placed bytes"
    route_dir = _seed_cache(
        map_path, tmp_path / "cache", label="宝宝对不起",
        url="https://official.example/audio/apology.mp3", payload=audio,
    )
    (route_dir / "record.json").unlink()
    (route_dir / "decode.json").unlink()
    monkeypatch.setattr(acquisition, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("unbound bytes must not be downloaded over")
    ))

    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=tmp_path / "cache", cue_text="宝宝对不起",
    )

    assert result["status"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "EXTERNAL_AUDIO_CACHE_UNBOUND_AUDIO_PRESENT"
    assert (route_dir / "audio.mp3").read_bytes() == audio
    assert not (route_dir / "record.json").exists()


def test_unbound_part_is_kept_without_download(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path, buttons=[
        {"label": "宝宝对不起", "url": "https://official.example/audio/apology.mp3"},
    ])
    route_dir = tmp_path / "cache" / _sha(map_path.read_bytes()) / _sha(
        b"https://official.example/audio/apology.mp3"
    )
    route_dir.mkdir(parents=True)
    part = route_dir / "audio.mp3.part"
    part.write_bytes(b"partial operator bytes")
    monkeypatch.setattr(acquisition, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("unbound partial must not be downloaded over")
    ))

    result = prepare_external_audio_reference(
        {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
        source_map_path=map_path, cache_root=tmp_path / "cache", cue_text="宝宝对不起",
    )

    assert result["status"] == "UNKNOWN_KEEP"
    assert result["reason_code"] == "EXTERNAL_AUDIO_CACHE_UNBOUND_PART_PRESENT"
    assert part.read_bytes() == b"partial operator bytes"


def test_same_route_concurrent_calls_download_once(tmp_path, monkeypatch):
    map_path = _source_map(tmp_path, buttons=[
        {"label": "宝宝对不起", "url": "https://official.example/audio/apology.mp3"},
    ])
    calls = []

    class Response:
        status = 200
        headers = {"Content-Type": "audio/mpeg"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://official.example/audio/apology.mp3"

        def __init__(self):
            self.sent = False

        def read(self, _size):
            if self.sent:
                return b""
            self.sent = True
            return b"one route only"

    def fake_urlopen(*_args, **_kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(acquisition, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        acquisition.subprocess,
        "run",
        lambda command, **_kwargs: SimpleNamespace(
            stdout=json.dumps({"streams": [{"codec_type": "audio"}]})
            if command[0] == "ffprobe" else ""
        ),
    )
    results = []
    errors = []

    def worker():
        try:
            results.append(prepare_external_audio_reference(
                {"visible_ui": ["宝宝对不起"], "button_under_cursor": None},
                source_map_path=map_path, cache_root=tmp_path / "cache", cue_text="宝宝对不起",
            ))
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert {result["acquisition"]["status"] for result in results} == {"DOWNLOADED", "CACHE_HIT"}
    assert len(calls) == 1


def test_normal_external_scene_calls_profile_acquisition_when_catalog_unset(tmp_path, monkeypatch):
    inputs, spec, profile, refs, _document = _external_button_scene(tmp_path)
    monkeypatch.delenv(CATALOG_ENV, raising=False)
    catalog = tmp_path / "catalog.json"
    # The seam still uses the maintained resolver to validate this synthetic
    # test catalog; acquisition itself is the only patched boundary here.
    from tests.test_producer_nested_acoustic import _external_audio_catalog
    catalog = _external_audio_catalog(tmp_path)
    observed = {}

    def acquire(controls, **kwargs):
        observed["controls"] = controls
        observed.update(kwargs)
        return {
            "status": "READY",
            "catalog_value": f"{catalog}#sha256:{_sha(catalog.read_bytes())}",
            "acquisition": {"status": "CACHE_HIT"},
        }

    monkeypatch.setattr(nested_acoustic, "prepare_external_audio_reference", acquire)
    effective, audit = nested_acoustic.prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=refs,
        external_audio_source_map=tmp_path / "profile-map.json",
        external_audio_cache_root=tmp_path / "runtime-cache",
    )

    assert observed["source_map_path"] == tmp_path / "profile-map.json"
    assert observed["cache_root"] == tmp_path / "runtime-cache"
    assert effective["nested_media_acoustic"]["observations"][0]["source_cache_binding"]["kind"] == "OFFICIAL_EXTERNAL_AUDIO"
    assert audit["provider_calls"] == 0


def test_explicit_empty_catalog_does_not_fall_back_to_profile_map(tmp_path, monkeypatch):
    inputs, spec, profile, refs, _document = _external_button_scene(tmp_path)
    monkeypatch.setenv(CATALOG_ENV, "")

    def should_not_acquire(*_args, **_kwargs):
        raise AssertionError("an explicit catalog setting has priority, even when empty")

    monkeypatch.setattr(nested_acoustic, "prepare_external_audio_reference", should_not_acquire)
    effective, audit = nested_acoustic.prepare_final_scene_acoustic_spec(
        spec=spec, candidate_id="candidate", source_media=inputs["outer_media_path"],
        subtitle_path=inputs["final_srt_path"], duration_ms=1000,
        profile_path=profile, reference_dir=refs,
        external_audio_source_map=tmp_path / "profile-map.json",
        external_audio_cache_root=tmp_path / "runtime-cache",
    )

    assert "nested_media_acoustic" not in effective
    assert audit["observations"][0]["reason_code"] == "EXTERNAL_AUDIO_CATALOG_UNCONFIGURED"


def test_profile_wrapper_supplies_public_map_and_runtime_cache(tmp_path, monkeypatch):
    captured = {}

    def fake_prepare(**kwargs):
        captured.update(kwargs)
        return {}, {"provider_calls": 0}

    monkeypatch.setattr(package_driver, "prepare_final_scene_acoustic_spec", fake_prepare)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))
    package_driver.prepare_profile_nested_acoustic_spec(spec={"watched_media": True}, candidate_id="c")

    assert captured["external_audio_source_map"] == package_driver.profile_asset_file(
        "external_audio_sources"
    )
    assert captured["external_audio_cache_root"] == tmp_path / "runtime" / "cache" / "nested-external-audio"
    assert captured["external_audio_source_map"].is_file()


def test_profile_wrapper_keeps_optional_map_absent_for_unregistered_profile(tmp_path, monkeypatch):
    captured = {}
    voiceprint = Path("assets/lidousha/voiceprint_profile.v1.json").resolve()

    class ProfileWithoutExternalMap:
        asset_files = {}
        voiceprint_reference_subdirectory = "lidousha"

        @staticmethod
        def asset_file(key, *, repo_root):
            assert key == "voiceprint_profile"
            return voiceprint

    def fake_prepare(**kwargs):
        captured.update(kwargs)
        return {}, {"provider_calls": 0}

    monkeypatch.setattr(package_driver, "CHANNEL_PROFILE", ProfileWithoutExternalMap())
    monkeypatch.setattr(package_driver, "prepare_final_scene_acoustic_spec", fake_prepare)
    monkeypatch.setenv("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT", str(tmp_path / "runtime"))

    package_driver.prepare_profile_nested_acoustic_spec(
        spec={"watched_media": True}, candidate_id="nonexternal-profile"
    )

    assert captured["external_audio_source_map"] is None
    assert captured["external_audio_cache_root"] == tmp_path / "runtime" / "cache" / "nested-external-audio"
