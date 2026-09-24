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


def _surface(tmp_path: Path) -> tuple[dict, Path, Path, dict]:
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
        "candidate_id": "candidate",
        CONFIG_KEY: {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": "candidate",
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
