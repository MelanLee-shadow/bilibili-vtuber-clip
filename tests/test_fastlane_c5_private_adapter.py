"""C5's sealed text map is a fixed input to the text-only speaker adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "assets/lidousha/reviewed_subtitle_baselines"
CID = "auto_113028_1271_1328"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_c5_private_speaker_adapter_input_is_exact_24_to_21_drop_map() -> None:
    diagnostic = BASE / f"{CID}.pipeline-diagnostic.srt"
    reviewed = BASE / f"{CID}.reviewed.srt"
    ledger_path = BASE / f"{CID}.operator-decisions.v3.json"
    diff_path = BASE / f"{CID}.operator-truth-diff.v2.json"

    assert _sha(diagnostic) == "5da5af9dba5ce3ff2fee7d585bda809eb3a9edb612a18868ace683cb94281043"
    assert _sha(reviewed) == "9f33f247deb405b409d50f294bbfab08db7d5f43086241dd736fac0498faf64b"
    assert _sha(ledger_path) == "6fac5913ee143619c840483a8355e6ba63690f9bf47ddd27b3d34b9fcb5ce1a2"
    assert _sha(diff_path) == "48142b7ebf737dde41472757030385794deb43ba9091289d1f4962885fd446b1"

    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    diff = json.loads(diff_path.read_text(encoding="utf-8"))
    rows = ledger["cue_decisions"]
    assert ledger["candidate_id"] == CID
    assert ledger["report_scope"] == "EXHAUSTIVE"
    assert len(rows) == len(diff["rows"]) == 24
    drops = [index for index, row in enumerate(rows, start=1) if row["disposition"] == "OPERATOR_DROP"]
    assert drops == [13, 14, 15]
    assert [(diff["rows"][index - 1]["start_ms"], diff["rows"][index - 1]["end_ms"]) for index in drops] == [
        (28700, 30460), (30460, 33240), (33240, 36140)
    ]
    retained = [row["release_cue_index"] for row in diff["rows"] if row["disposition"] != "OPERATOR_DROP"]
    assert retained == list(range(1, 22))
    assert all(row["disposition"] == "OPERATOR_UNCHANGED_FREEZE" for row in rows if row["cue"] not in drops)
    # The proposed C5-only adapter is allowed to clamp only this exact opening
    # cue against the frozen 9750-ms delivery start.  All other drift stays
    # outside the proposal and must fail closed.
    assert diff["rows"][4]["disposition"] == "OPERATOR_UNCHANGED_FREEZE"
    assert (diff["rows"][4]["start_ms"], diff["rows"][4]["end_ms"]) == (9560, 10080)
    assert 9560 < 9750 < 10080
    assert (9750 - 9560, 10080 - 9750) == (190, 330)
