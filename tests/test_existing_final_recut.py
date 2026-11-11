from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from src.autoslice.existing_final_recut import (
    AUTHORITY_KIND,
    CONFIG_KEY,
    SCHEMA_VERSION,
    select_accurate_recut_command,
)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _surface(tmp_path: Path, candidate: str = "candidate") -> tuple[dict, Path, Path, dict]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    padded = tmp_path / "padded.mp4"
    padded.write_bytes(b"exact padded bytes")
    recut = tmp_path / "existing.recut.mp4"
    recut.write_bytes(b"exact recut bytes")
    provenance = tmp_path / "existing.recut.provenance.json"
    document = {
        "schema_version": "lidousha-speaker-recut-provenance.v1",
        "final_recut": {
            "source_path": str(padded),
            "source_sha256": hashlib.sha256(padded.read_bytes()).hexdigest(),
            "start_ms": 1_000,
            "end_ms": 3_000,
            "output_path": str(recut),
            "output_sha256": hashlib.sha256(recut.read_bytes()).hexdigest(),
        },
    }
    provenance.write_text(json.dumps(document), encoding="utf-8")
    spec = {
        "candidate_id": candidate,
        CONFIG_KEY: {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": candidate,
            "path": str(recut),
            "sha256": _sha(recut.read_bytes()),
            "provenance_path": str(provenance),
            "provenance_sha256": _sha(provenance.read_bytes()),
            "source_media_sha256": _sha(padded.read_bytes()),
            "final_start_ms": 1_000,
            "final_end_ms": 3_000,
            "time_domain": "padded_local",
            "authority_kind": AUTHORITY_KIND,
        },
    }
    return spec, padded, recut, document


def test_existing_final_recut_is_hash_bound_hardlink_reuse(tmp_path: Path) -> None:
    spec, padded, recut, _document = _surface(tmp_path)
    audit: dict = {}
    legacy_calls: list[object] = []

    def legacy(**kwargs):
        legacy_calls.append(kwargs)
        raise AssertionError("existing final recut must not call ffmpeg command builder")

    builder = select_accurate_recut_command(
        spec=spec,
        padded=padded,
        chat_authority_audit=audit,
        legacy_command=legacy,
    )
    output = tmp_path / "destination" / "candidate.recut.mp4"
    output.parent.mkdir()
    command = builder(
        source_video=padded,
        output_media=output,
        start_ms=1_000,
        duration_ms=2_000,
    )
    subprocess.run(command, check=True)

    assert legacy_calls == []
    assert output.read_bytes() == recut.read_bytes()
    assert os.stat(output).st_ino == os.stat(recut).st_ino
    assert audit["existing_final_recut_receipt"] == {
        "schema_version": "existing-final-recut-consumption.v1",
        "status": "PASS",
        "candidate_id": "candidate",
        "media_sha256": _sha(recut.read_bytes()),
        "source_media_sha256": _sha(padded.read_bytes()),
        "provenance_sha256": _sha(Path(spec[CONFIG_KEY]["provenance_path"]).read_bytes()),
        "final_start_ms": 1_000,
        "final_end_ms": 3_000,
        "time_domain": "padded_local",
        "authority_kind": AUTHORITY_KIND,
        "materialization": "HARDLINK_REUSE",
        "ffmpeg_calls": 0,
        "provider_calls": 0,
    }


@pytest.mark.parametrize("candidate", ["candidate", "new-normal-member"])
def test_normal_hardlink_materialization_can_resume_without_replacing_bytes(
    tmp_path: Path, candidate: str,
) -> None:
    from src.autoslice import producer_package_finalization as finalization

    spec, padded, recut, _ = _surface(tmp_path, candidate)
    spec["pieces"] = [{"start_ms": 10_000, "end_ms": 14_000}]
    spec_path = tmp_path / "SPEC.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    padded_provenance = tmp_path / "padded.provenance.json"
    padded_provenance.write_text("{}", encoding="utf-8")
    out_root = tmp_path / "out"
    out_root.mkdir()
    before = recut.read_bytes()

    def unused(*args, **kwargs):
        raise AssertionError("no provider, burn or publication belongs in recut reuse")

    identity = None
    for attempt in range(2):
        # Reopen the ordinary persisted contract; no in-memory receipt is reused.
        cold_spec = json.loads(spec_path.read_text(encoding="utf-8"))
        audit = {}
        builder = select_accurate_recut_command(
            spec=cold_spec, padded=padded, chat_authority_audit=audit,
            legacy_command=unused,
        )
        commands = []

        def run_command(command):
            commands.append(command)
            subprocess.run(command, check=True)

        result = finalization._materialize_final_recut_body(
            spec=cold_spec, cid=candidate, out_root=out_root, padded=padded,
            padded_provenance_path=padded_provenance, piece_provenance_rows=[],
            final_start=1_000, final_end=3_000, sanitized=[], timing_qa={},
            text_override_path=None, chat_authority_audit=audit,
            adapters=finalization.ProducerFinalizationAdapters(
                accurate_recut_command=builder, run_command=run_command,
                write_source_range_srt=lambda cues, start, end, path: path.write_text(
                    "1\n00:00:00,000 --> 00:00:02,000\n这是恢复测试\n", encoding="utf-8",
                ),
                apply_text_override_document=unused, run_speaker_finalization=unused,
                burn_preview_subtitles=unused, stage_publish_draft=unused,
                generate_upload_tags=unused, delivery_root=unused,
            ),
        )
        output = result.media_path
        stat = output.stat()
        current = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if attempt == 0:
            identity = current
            assert commands == [["/bin/ln", str(recut), str(output)]]
        else:
            assert current == identity
            assert commands == [["/bin/true"]]
        assert os.path.samefile(recut, output)
        assert output.read_bytes() == before
        assert audit["existing_final_recut_receipt"]["provider_calls"] == 0
        assert audit["existing_final_recut_receipt"]["authority_kind"] == AUTHORITY_KIND
        assert result.subtitle_path.is_file()
        provenance = json.loads(output.with_suffix(".provenance.json").read_text())
        assert provenance["final_recut"]["output_sha256"] == _sha(before)[7:]


@pytest.mark.parametrize("kind", ["equal_copy", "symlink", "dangling_symlink", "mutated_link"])
def test_resumed_recut_does_not_adopt_foreign_or_changed_output(tmp_path, kind):
    spec, padded, recut, _ = _surface(tmp_path)
    output = tmp_path / "destination.mp4"
    builder = select_accurate_recut_command(
        spec=spec, padded=padded, chat_authority_audit={}, legacy_command=object())
    if kind == "equal_copy":
        output.write_bytes(recut.read_bytes())
    elif kind == "symlink":
        output.symlink_to(recut)
    elif kind == "dangling_symlink":
        output.symlink_to(tmp_path / "missing.mp4")
    else:
        os.link(recut, output)
        output.write_bytes(b"changed after builder creation")
    before_bytes = output.read_bytes() if output.exists() else None
    before = output.lstat()
    with pytest.raises(ValueError, match="already exists|sha256"):
        builder(source_video=padded, output_media=output,
                start_ms=1_000, duration_ms=2_000)
    after = output.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns,
    )
    assert (output.read_bytes() if output.exists() else None) == before_bytes


@pytest.mark.parametrize("kind", ["source", "provenance", "digest", "boundary", "source_path"])
def test_resumed_recut_revalidates_contract_before_reuse(tmp_path: Path, kind: str) -> None:
    spec, padded, recut, _ = _surface(tmp_path)
    output = tmp_path / "destination.mp4"
    os.link(recut, output)
    audit = {}
    builder = select_accurate_recut_command(
        spec=spec, padded=padded, chat_authority_audit=audit, legacy_command=object(),
    )
    source_video = padded
    if kind == "source":
        padded.write_bytes(b"changed source after builder creation")
    elif kind == "provenance":
        Path(spec[CONFIG_KEY]["provenance_path"]).write_text("{}")
    elif kind == "digest":
        spec[CONFIG_KEY]["sha256"] = "sha256:" + "0" * 64
    elif kind == "boundary":
        spec[CONFIG_KEY]["final_end_ms"] += 1
    else:
        source_video = tmp_path / "foreign.mp4"
        source_video.write_bytes(padded.read_bytes())
    before_bytes = output.read_bytes()
    before = output.stat()
    with pytest.raises(ValueError, match="already exists|sha256|boundary|source path"):
        builder(source_video=source_video, output_media=output,
                start_ms=1_000, duration_ms=2_000)
    after = output.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
    )
    assert output.read_bytes() == before_bytes
    assert "existing_final_recut_receipt" not in audit


def test_absent_existing_final_recut_keeps_legacy_builder() -> None:
    legacy = object()
    assert select_accurate_recut_command(
        spec={},
        padded=Path("unused"),
        chat_authority_audit={},
        legacy_command=legacy,
    ) is legacy


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda spec: spec[CONFIG_KEY].update(candidate_id="other"), "candidate"),
        (lambda spec: spec[CONFIG_KEY].update(sha256="sha256:" + "0" * 64), "media sha256"),
        (lambda spec: spec[CONFIG_KEY].update(provenance_sha256="sha256:" + "0" * 64), "provenance sha256"),
        (lambda spec: spec[CONFIG_KEY].update(source_media_sha256="sha256:" + "0" * 64), "source media sha256"),
        (lambda spec: spec[CONFIG_KEY].update(time_domain="delivery_local"), "padded_local"),
        (lambda spec: spec[CONFIG_KEY].update(authority_kind="PUBLICATION_AUTHORITY"), "authority kind"),
    ],
)
def test_existing_final_recut_rejects_contract_drift(
    tmp_path: Path, mutation, message: str
) -> None:
    spec, padded, _recut, _document = _surface(tmp_path)
    mutation(spec)
    with pytest.raises(ValueError, match=message):
        select_accurate_recut_command(
            spec=spec,
            padded=padded,
            chat_authority_audit={},
            legacy_command=object(),
        )


def test_existing_final_recut_rejects_boundary_and_existing_destination(
    tmp_path: Path,
) -> None:
    spec, padded, _recut, _document = _surface(tmp_path)
    builder = select_accurate_recut_command(
        spec=spec,
        padded=padded,
        chat_authority_audit={},
        legacy_command=object(),
    )
    output = tmp_path / "out.mp4"
    with pytest.raises(ValueError, match="boundary"):
        builder(
            source_video=padded,
            output_media=output,
            start_ms=1_000,
            duration_ms=1_999,
        )
    output.write_bytes(b"occupied")
    with pytest.raises(ValueError, match="already exists"):
        builder(
            source_video=padded,
            output_media=output,
            start_ms=1_000,
            duration_ms=2_000,
        )


def test_existing_final_recut_rejects_symlink(tmp_path: Path) -> None:
    spec, padded, recut, _document = _surface(tmp_path)
    linked = tmp_path / "linked.mp4"
    linked.symlink_to(recut)
    spec[CONFIG_KEY]["path"] = str(linked)
    with pytest.raises(ValueError, match="regular non-symlink"):
        select_accurate_recut_command(
            spec=spec,
            padded=padded,
            chat_authority_audit={},
            legacy_command=object(),
        )
