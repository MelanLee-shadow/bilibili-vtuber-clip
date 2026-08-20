import hashlib
import json
import shutil
from pathlib import Path

import pytest

from src.autoslice.qixi_cue21_diagnostic_evidence import (
    QixiCue21DiagnosticEvidenceError,
    validate_qixi_cue21_diagnostic_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
NAME = "auto_113022_354_496.cue21_evidence"


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "baselines"
    shutil.copytree(ROOT / "assets/lidousha/reviewed_subtitle_baselines" / NAME, root / NAME)
    manifest = root / NAME / "canonical-evidence.v1.json"
    return root, {
        "path": f"{NAME}/canonical-evidence.v1.json",
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "reason": "diagnostic only",
    }


def _validate(root: Path, descriptor: dict[str, str]) -> None:
    validate_qixi_cue21_diagnostic_evidence(
        descriptor, evidence_root=root, candidate_id="auto_113022_354_496",
        source_basename="22966160_20260817-11-30-22.mp4",
        source_sha256="212eb59bee50dda1601a3f30e3c7b9a307715058fb472646ddcfa200615c6a97",
        cue_start_ms=65690, cue_end_ms=68090, absolute_source_start_ms=354420,
    )


def test_replays_the_sealed_qixi_diagnostic_evidence(tmp_path: Path) -> None:
    root, descriptor = _fixture(tmp_path)
    _validate(root, descriptor)


@pytest.mark.parametrize(
    "kind", ["missing", "extra", "raw_drift", "symlink", "hash", "command", "model_version"]
)
def test_rejects_diagnostic_evidence_drift(tmp_path: Path, kind: str) -> None:
    root, descriptor = _fixture(tmp_path)
    evidence = root / NAME
    manifest = evidence / "canonical-evidence.v1.json"
    if kind == "raw_drift":
        with (evidence / "gemini-3.6-flash.raw.json").open("ab") as handle:
            handle.write(b" ")
    elif kind == "symlink":
        target = evidence / "gemini-3.6-flash.raw.json"
        target.rename(evidence / "raw-target.json")
        target.symlink_to("raw-target.json")
    elif kind == "hash":
        descriptor["sha256"] = "0" * 64
    elif kind == "command":
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["extraction"]["command"] = "ffmpeg unexpected"
        manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        descriptor["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    elif kind == "model_version":
        raw_path = evidence / "gemini-3.7-flash.raw.json"
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        raw["modelVersion"] = "gemini-unknown"
        raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["raw_responses"][0]["sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        payload["raw_responses"][0]["bytes"] = raw_path.stat().st_size
        manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        descriptor["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    else:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if kind == "missing":
            payload["extraction"].pop("target_mp3")
        else:
            payload["unexpected"] = True
        manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        descriptor["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(QixiCue21DiagnosticEvidenceError):
        _validate(root, descriptor)
