import hashlib
import json
from pathlib import Path

from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)


ROOT = Path(__file__).resolve().parents[1]
CID = "auto_220021_561_670"
AUTHORITY = ROOT / "assets/lidousha/fastlane_c3_deployable_baseline_authority"


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_c3_integration_binds_accepted_proposal_and_exact_baseline() -> None:
    envelope = json.loads((AUTHORITY / f"{CID}.accepted.v1.json").read_text())
    seal = envelope.pop("acceptance_sha256")
    assert seal == _canonical(envelope)
    assert envelope["accepted"] is True
    assert envelope["forbidden_operations"] == ["provider", "state", "ssh", "deploy", "upload", "new_content"]
    proposal = envelope["proposal"]
    interval = envelope["exact_interval"]
    assert _sha(ROOT / proposal["path"]) == proposal["file_sha256"]
    assert _sha(ROOT / interval["path"]) == interval["file_sha256"]
    assert not any("/private" in json.dumps(item) or "/opt" in json.dumps(item) for item in (envelope,))
    baseline = load_candidate_reviewed_subtitle_baseline(
        ROOT / "assets/lidousha/reviewed_subtitle_baselines", CID, repo_root=ROOT
    )
    assert baseline is not None
    assert baseline.config["sha256"] == "d70a96c402df8313264ef6ca145d69d5dbb74e7a2c48eb512e78f3f417e8eecc"
    assert baseline.config["absolute_source_end_ms"] - baseline.config["absolute_source_start_ms"] == 109040
