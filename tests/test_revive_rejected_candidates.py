from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

from src.autoslice.selection_scorecard import normalize_selection_scorecard


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "revive_rejected_candidates.py"
CANDIDATE_ID = "auto_192000_909_1014"


def _scorecard() -> dict[str, object]:
    normalized = normalize_selection_scorecard(
        {
            "tier": 2,
            "tier_basis": "personal_stance",
            "tier_reason": "观众追问模型表情后，主播连续表达鲜明态度。",
            "tier_evidence_cues": [305, 308, 311],
            "dimensions": {
                "lidousha_centrality": 3,
                "stance_intensity": 4,
                "audience_salience": 3,
                "relationship_interaction": 2,
                "persona_reversal": 3,
                "comedic_payoff": 3,
                "self_contained": 4,
            },
            "uncertainty_penalty": 0,
            "fatigue_penalty": 0,
        },
        start_cue=305,
        end_cue=311,
    )
    assert normalized is not None
    return normalized


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "2026-07-25.json"
    state_path.write_text(
        json.dumps(
            {
                "picks": [
                    {
                        "candidate_id": CANDIDATE_ID,
                        "status": "candidate_rejected",
                        "selection_scorecard": None,
                        "rejection_reason": "selection_scorecard_missing_or_invalid",
                        "failure_kind": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    spec_path = tmp_path / f"spec_{CANDIDATE_ID}.json"
    spec_path.write_text(
        json.dumps(
            {
                "candidate_id": CANDIDATE_ID,
                "selection_scorecard": _scorecard(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return state_path, spec_path


def _run(state_path: Path, spec_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--state",
            str(state_path),
            "--candidate",
            CANDIDATE_ID,
            "--reason",
            "restore omitted scorecard from the original candidate spec",
            "--fix-commit",
            "test-fix",
            "--restore-selection-scorecard-from-spec",
            str(spec_path),
            *extra,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_restores_valid_scorecard_and_records_spec_hash(tmp_path: Path) -> None:
    state_path, spec_path = _fixture(tmp_path)

    result = _run(state_path, spec_path, "--apply")

    assert result.returncode == 0, result.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    row = state["picks"][0]
    assert row["status"] == "failed"
    assert row["failure_recoverable"] is True
    assert row["selection_scorecard"] == _scorecard()
    restoration = row["revivals"][-1]["selection_scorecard_restoration"]
    assert restoration["schema_version"] == "selection-scorecard-restoration.v1"
    assert restoration["source_spec_path"] == str(spec_path)
    assert restoration["source_spec_sha256"] == (
        "sha256:" + hashlib.sha256(spec_path.read_bytes()).hexdigest()
    )
    assert restoration["restored_at"]


def test_dry_run_does_not_change_state(tmp_path: Path) -> None:
    state_path, spec_path = _fixture(tmp_path)
    before = state_path.read_bytes()

    result = _run(state_path, spec_path)

    assert result.returncode == 0, result.stderr
    assert "DRY-RUN" in result.stdout
    assert state_path.read_bytes() == before


def test_refuses_candidate_mismatched_spec(tmp_path: Path) -> None:
    state_path, spec_path = _fixture(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["candidate_id"] = "auto_other"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    before = state_path.read_bytes()

    result = _run(state_path, spec_path, "--apply")

    assert result.returncode == 2
    assert "candidate mismatch" in result.stderr
    assert state_path.read_bytes() == before


def test_refuses_invalid_scorecard_spec(tmp_path: Path) -> None:
    state_path, spec_path = _fixture(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["selection_scorecard"] = None
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    before = state_path.read_bytes()

    result = _run(state_path, spec_path, "--apply")

    assert result.returncode == 2
    assert "valid scorecard" in result.stderr
    assert state_path.read_bytes() == before
