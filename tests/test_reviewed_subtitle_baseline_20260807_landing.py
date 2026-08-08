"""Landing checks for the 2026-08-07 three-candidate Ivan truth harvest.

Covers the reviewed-subtitle-baseline + speaker-override pair for
auto_203735_555_680, auto_200736_298_383, and auto_220747_488_680, all
generated from reports/ivan_truth_harvest/2026-08-07/*.truth-diff.v2.json.

The content/registry assertions are self-contained.  The byte-authority checks
also read Ivan's pristine forensics archive when it is mounted; only those
checks skip when the external archive is unavailable.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.apply_speaker_turn_overrides import (
    apply_overrides,
    parse_labelled_srt,
    validate_bound_speaker_override_document,
)
from src.autoslice.jingting_chunker import parse_srt_cues
from src.autoslice.reviewed_subtitle_baseline_registry import (
    load_candidate_reviewed_subtitle_baseline,
)
from src.autoslice.subtitle_validation import validate_srt_file

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT / "assets" / "lidousha" / "reviewed_subtitle_baselines"
OVERRIDE_ROOT = REPO_ROOT / "assets" / "lidousha" / "speaker_overrides"
TRUTH_ROOT = REPO_ROOT / "reports" / "ivan_truth_harvest" / "2026-08-07"
FORENSICS_ROOT = Path(
    "/Users/ivan/Project/vtuber-slice-forensics/2026-08-07-pristine/out/2026-08-07"
)

CANDIDATES = {
    "auto_203735_555_680": {"cues": 61, "text_changed": 0, "marked": 6},
    "auto_200736_298_383": {"cues": 44, "text_changed": 7, "marked": 6},
    "auto_220747_488_680": {"cues": 98, "text_changed": 18, "marked": 8},
}

_SPEAKER_PREFIX_RX = re.compile(
    r"^\[(?:李豆沙|连线)(?:\s+[+-]?\d+(?:\.\d+)?)?\]\s*"
)
_MARKER_RX = re.compile(r"(?<!\S)[AB](?!\S)")
_CUE_HEADER_RX = re.compile(
    rb"(?m)^(\d+)\r?\n"
    rb"(\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3})\r?$"
)

requires_forensics = pytest.mark.skipif(
    not FORENSICS_ROOT.is_dir(),
    reason=f"pristine forensics archive is unavailable: {FORENSICS_ROOT}",
)


def _load_truth_diff(cid: str) -> dict:
    return json.loads((TRUTH_ROOT / f"{cid}.truth-diff.v2.json").read_text(encoding="utf-8"))


def _pristine_srt_path(cid: str) -> Path:
    return (
        FORENSICS_ROOT
        / cid
        / "replacement_recuts"
        / f"{cid}.recut.speaker-final.srt"
    )


def _cue_headers(path: Path) -> list[tuple[bytes, bytes]]:
    return _CUE_HEADER_RX.findall(path.read_bytes())


def _synthetic_machine_srt(truth_diff: dict) -> str:
    """Rebuild the pristine machine-labelled SRT the truth diff was harvested from."""
    blocks = []
    for cue in truth_diff["cues"]:
        blocks.append(
            f"{cue['cue']}\n{cue['timing']}\n[{cue['machine_label']}] {cue['machine_text']}\n"
        )
    return "\n".join(blocks) + "\n"


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_truth_diff_summary_matches_expected_counts(cid, expected):
    truth_diff = _load_truth_diff(cid)
    assert truth_diff["summary"]["cues"] == expected["cues"]
    assert truth_diff["summary"]["text_changed"] == expected["text_changed"]
    assert truth_diff["summary"]["marked"] == expected["marked"]


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_reviewed_baseline_registry_accepts_committed_asset(cid, expected):
    loaded = load_candidate_reviewed_subtitle_baseline(BASELINE_ROOT, cid)
    assert loaded is not None
    # load_candidate_reviewed_subtitle_baseline recomputes sha256 from the actual
    # file bytes and raises if it does not match the manifest -- reaching this
    # point already proves manifest sha256 == actual reviewed.srt sha256.
    assert loaded.config["schema_version"] == "subtitle-redelivery-baseline.v2"
    assert loaded.config["exact_interval_replay"] is True


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_reviewed_srt_sha256_matches_manifest_directly(cid, expected):
    manifest = json.loads((BASELINE_ROOT / f"{cid}.subtitle-baseline.v1.json").read_text(encoding="utf-8"))
    srt_path = BASELINE_ROOT / manifest["path"]
    actual_sha256 = hashlib.sha256(srt_path.read_bytes()).hexdigest()
    assert actual_sha256 == manifest["sha256"]


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_reviewed_srt_has_no_speaker_prefix_or_ab_marker_and_matches_cue_count(cid, expected):
    srt_path = BASELINE_ROOT / f"{cid}.reviewed.srt"
    cues = parse_srt_cues(srt_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (BASELINE_ROOT / f"{cid}.subtitle-baseline.v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(cues) == expected["cues"]
    for cue in cues:
        assert not _SPEAKER_PREFIX_RX.search(cue.text), cue.text
        assert not _MARKER_RX.search(cue.text), cue.text
    assert [int(cue.index) for cue in cues] == list(range(1, expected["cues"] + 1))
    assert validate_srt_file(
        srt_path,
        media_duration_ms=(
            manifest["absolute_source_end_ms"]
            - manifest["absolute_source_start_ms"]
        ),
    )["status"] == "PASS"


@requires_forensics
@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_reviewed_srt_cue_headers_and_count_match_pristine_byte_for_byte(cid, expected):
    reviewed_path = BASELINE_ROOT / f"{cid}.reviewed.srt"
    pristine_path = _pristine_srt_path(cid)

    reviewed_headers = _cue_headers(reviewed_path)
    pristine_headers = _cue_headers(pristine_path)

    assert len(reviewed_headers) == len(pristine_headers) == expected["cues"]
    assert reviewed_headers == pristine_headers


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_reviewed_srt_text_matches_truth_diff_per_cue(cid, expected):
    truth_diff = _load_truth_diff(cid)
    srt_path = BASELINE_ROOT / f"{cid}.reviewed.srt"
    cues = {
        int(cue.index): cue
        for cue in parse_srt_cues(srt_path.read_text(encoding="utf-8"))
    }
    assert len(cues) == len(truth_diff["cues"])
    for tc in truth_diff["cues"]:
        expected_text = tc["truth_text"] if tc["text_changed"] else tc["machine_text"]
        assert cues[tc["cue"]].text == expected_text, tc["cue"]


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_speaker_override_document_is_self_consistent(cid, expected):
    override_path = OVERRIDE_ROOT / f"{cid}.speaker.v1.json"
    document = json.loads(override_path.read_text(encoding="utf-8"))
    assert len(document["overrides"]) == expected["marked"]
    validate_bound_speaker_override_document(
        override_path,
        candidate_id=cid,
        expected_source_media_sha256=document["source_media_sha256"],
        expected_text_final_srt_sha256=document["text_final_srt_sha256"],
    )
    truth_diff = _load_truth_diff(cid)
    assert document["source_srt_sha256"] == truth_diff["source_machine_sha256"]


@requires_forensics
@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_speaker_override_source_srt_sha256_matches_pristine(cid, expected):
    override_path = OVERRIDE_ROOT / f"{cid}.speaker.v1.json"
    document = json.loads(override_path.read_text(encoding="utf-8"))
    pristine_path = _pristine_srt_path(cid)
    pristine_sha256 = hashlib.sha256(pristine_path.read_bytes()).hexdigest()
    sidecar = json.loads(pristine_path.with_suffix(".json").read_text(encoding="utf-8"))
    truth_diff = _load_truth_diff(cid)

    validate_bound_speaker_override_document(
        override_path,
        candidate_id=cid,
        expected_source_media_sha256=sidecar["source_media_sha256"],
        expected_text_final_srt_sha256=sidecar["text_final_srt_sha256"],
    )
    assert (
        document["source_srt_sha256"]
        == pristine_sha256
        == sidecar["automatic_labelled_srt_sha256"]
        == truth_diff["source_machine_sha256"]
    )


@pytest.mark.parametrize("cid, expected", CANDIDATES.items())
def test_speaker_override_applies_cleanly_against_reconstructed_machine_srt(cid, expected, tmp_path):
    truth_diff = _load_truth_diff(cid)
    synthetic = tmp_path / f"{cid}.machine.srt"
    synthetic.write_text(_synthetic_machine_srt(truth_diff), encoding="utf-8")
    source_cues = parse_labelled_srt(synthetic)
    assert len(source_cues) == expected["cues"]

    document = json.loads((OVERRIDE_ROOT / f"{cid}.speaker.v1.json").read_text(encoding="utf-8"))
    result_cues = apply_overrides(source_cues, document)

    marked_cues = {c["cue"] for c in truth_diff["cues"] if c["marked"]}
    resulting_source_indices = {cue.source_index for cue in result_cues}
    assert marked_cues <= resulting_source_indices

    by_source_cue: dict[int, list] = {}
    for cue in result_cues:
        by_source_cue.setdefault(cue.source_index, []).append(cue)
    for cue_no in marked_cues:
        truth_cue = next(c for c in truth_diff["cues"] if c["cue"] == cue_no)
        segments = by_source_cue[cue_no]
        assert [c.speaker for c in segments] == [s["label"] for s in truth_cue["truth_segments"]]
        assert [c.text for c in segments] == [s["text"] for s in truth_cue["truth_segments"]]
