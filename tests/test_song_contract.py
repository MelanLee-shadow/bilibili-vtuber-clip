"""WP-9: the audio prompt and the completeness gates consume one definition.

No provider calls. Historical receipts remain immutable; a missing external
published receipt is reported as a skip, never replaced by synthetic evidence.
Run the same file on c9cc5cc40 to expose the old literals/import omissions.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib
import json
import os
import re
from fractions import Fraction
from pathlib import Path

import pytest

from src.autoslice import agy_lrc_alignment as agy
from src.autoslice import song_common as common
from src.autoslice import song_omission_shape as shape
from src.autoslice import song_performance as performance
from src.autoslice.song_alignment import is_timing_model_report

ROOT = Path(__file__).resolve().parents[1]
WORDS = "zero one two three four five six seven eight nine".split()


def _prompt(mode="agy"):
    return agy._prompt(
        candidate_id="c", attempt_id="a", source_sha256="1" * 64,
        lrc_sha256="2" * 64, duration_ms=90_000, delivery_mode=mode,
        canonical_lrc_text='{"lrc_index":0,"text":"untrusted lyric"}\n',
    )


def _count(token):
    return WORDS.index(token) if token in WORDS else int(token)


def _parsed_contract(prompt):
    text = " ".join(prompt.split())

    def match(pattern):
        found = re.search(pattern, text)
        assert found is not None, pattern
        return found.groups()

    sung, = match(r"live arrangement, at least (\d+)% of the heard/performed rows")
    run, = match(r"no more than (\w+) consecutive rows are the narrow")
    blocks, = match(r"There may be at most (\w+) such spoken block")
    duration, ratio, span = match(
        r"summed voiced duration must be at most (\d+) seconds and (\d+)% of all "
        r"lyric-vocal duration, and its first-to-last span must be at most (\d+) seconds"
    )
    heard, coverage, length = match(
        r"Code requires at least (\d+) heard lyric rows, at least (\d+)% canonical coverage "
        r"and at least (\d+) seconds of performance"
    )
    repeated, small, holes = match(
        r"REPEATED_SECTION \(at least (\d+) rows sung contiguously elsewhere\), "
        r"SMALL_SKIP \(at most (\d+) rows between heard rows; at most (\d+) such holes\)"
    )
    sections, section_size, omitted, omitted_ratio = match(
        r"At most (\d+) hole may be larger than (\d+) rows or touch the LRC end; "
        r"all holes together stay within (\d+) rows and (\d+)% of lyrics"
    )
    assert int(section_size) == int(small)
    labels, = match(r"Allowed classification labels: ([A-Z_, ]+)\.")
    return {
        "MIN_READY_SUNG_LYRIC_RATIO": Fraction(int(sung), 100),
        "MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS": _count(run),
        "MAX_READY_SPOKEN_BLOCKS": _count(blocks),
        "MAX_READY_SPOKEN_LYRIC_DURATION_MS": int(duration) * 1000,
        "MAX_READY_SPOKEN_LYRIC_DURATION_RATIO": Fraction(int(ratio), 100),
        "MAX_READY_SPOKEN_BLOCK_SPAN_MS": int(span) * 1000,
        "MIN_LIVE_ARRANGEMENT_HEARD_ROWS": int(heard),
        "MIN_LIVE_ARRANGEMENT_HEARD_RATIO": Fraction(int(coverage), 100),
        "MIN_LIVE_ARRANGEMENT_DURATION_MS": int(length) * 1000,
        "MIN_REPEATED_SECTION_ROWS": int(repeated),
        "MAX_SMALL_SKIP_ROWS": int(small), "MAX_SMALL_SKIPS": int(holes),
        "MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS": int(sections),
        "MAX_LIVE_ARRANGEMENT_OMITTED_ROWS": int(omitted),
        "MAX_LIVE_ARRANGEMENT_OMITTED_RATIO": Fraction(int(omitted_ratio), 100),
        "LIVE_ARRANGEMENT_CLASSIFICATIONS": set(labels.split(", ")),
    }


@pytest.mark.parametrize("mode", ["agy", "gemini_api"])
def test_prompt_contract_round_trip(mode):
    for name, value in _parsed_contract(_prompt(mode)).items():
        owner = shape if name in {"MIN_REPEATED_SECTION_ROWS", "MAX_SMALL_SKIP_ROWS", "MAX_SMALL_SKIPS"} else common
        expected = getattr(owner, name)
        if isinstance(value, Fraction):
            expected = Fraction(str(expected))
        assert value == expected, name


def test_existing_prompt_bytes_and_both_delivery_modes_stay_unchanged():
    # Same historical full-prompt pin as the untouched test_song_repair control.
    assert hashlib.sha256(_prompt().encode()).hexdigest() == (
        "db2464a4547fda9f7993dbc4a67cb9cb630a2ae58301800a1c6ea9b80ed2f0e2"
    )
    def rules(prompt):
        return prompt.split("6. `live_performance`", 1)[1].split("8.", 1)[0]

    assert rules(_prompt()) == rules(_prompt("gemini_api"))


def test_new_common_values_flow_from_owner_to_prompt_and_checker(monkeypatch):
    # Change only the owner, then exercise the ordinary imports and renderer.
    values = {
        "MIN_READY_SUNG_LYRIC_RATIO": 0.75,
        "MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS": 4,
        "MAX_READY_SPOKEN_BLOCKS": 2,
        "MAX_READY_SPOKEN_LYRIC_DURATION_MS": 9_000,
        "MAX_READY_SPOKEN_LYRIC_DURATION_RATIO": Fraction(1, 4),
        "MAX_READY_SPOKEN_BLOCK_SPAN_MS": 14_000,
        "MIN_LIVE_ARRANGEMENT_HEARD_ROWS": 9,
        "MIN_LIVE_ARRANGEMENT_HEARD_RATIO": 0.65,
        "MIN_LIVE_ARRANGEMENT_DURATION_MS": 32_000,
        "MAX_LIVE_ARRANGEMENT_OMITTED_BLOCKS": 2,
        "MAX_LIVE_ARRANGEMENT_OMITTED_ROWS": 11,
        "MAX_LIVE_ARRANGEMENT_OMITTED_RATIO": 0.25,
    }
    try:
        with monkeypatch.context() as change:
            for name, value in values.items():
                change.setattr(common, name, value)
            for module in (shape, performance, agy):
                importlib.reload(module)
            parsed = _parsed_contract(_prompt())
            for name, value in values.items():
                assert parsed[name] == (Fraction(str(value)) if isinstance(parsed[name], Fraction) else value)
            # 25% is now permitted, 25% plus one millisecond is still rejected.
            rows = _vocal_rows(["sung"] * 4 + ["spoken"] * 3 + ["sung"] * 5)
            assert performance._validate_lyric_vocal_observations(rows, require_ready=True) == (True, False, False)
            rows[4]["live_end_ms"] += 1
            with pytest.raises(ValueError, match="voiced duration"):
                performance._validate_lyric_vocal_observations(rows, require_ready=True)
    finally:
        for module in (shape, performance, agy):
            importlib.reload(module)


def _vocal_rows(roles, *, spoken_ms=1000, sung_ms=1000, gap_ms=100):
    rows, start = [], 1000
    for index, role in enumerate(roles):
        end = start + (spoken_ms if role == "spoken" else sung_ms)
        rows.append({
            "lrc_index": index, "text": f"lyric-{index}", "heard": True,
            "live_start_ms": start, "live_end_ms": end,
            "lyric_vocal_subject": common.HOST_LYRIC_SUBJECT,
            "lidousha_role": "PERFORMING_THIS_LYRIC_SPOKEN" if role == "spoken" else "SINGING_THIS_LYRIC",
            "same_live_vocal_source_as_lidousha": True,
            "other_singer_or_harmony_audible": False, "recorded_or_playback_vocal_audible": False,
        })
        start = end + gap_ms
    return rows


@pytest.mark.parametrize("field,delta", [
    ("MAX_READY_SPOKEN_LYRIC_DURATION_RATIO", 0),
    ("MAX_READY_SPOKEN_LYRIC_DURATION_RATIO", 1),
    ("MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS", 0),
    ("MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS", 1),
    ("MAX_READY_SPOKEN_LYRIC_DURATION_MS", 0),
    ("MAX_READY_SPOKEN_LYRIC_DURATION_MS", 1),
    ("MAX_READY_SPOKEN_BLOCK_SPAN_MS", 0),
    ("MAX_READY_SPOKEN_BLOCK_SPAN_MS", 1),
    ("MAX_READY_SPOKEN_BLOCKS", 0), ("MAX_READY_SPOKEN_BLOCKS", 1),
    ("MIN_READY_SUNG_LYRIC_RATIO", 0), ("MIN_READY_SUNG_LYRIC_RATIO", 1),
])
def test_checker_accepts_only_the_rendered_live_performance_domain(field, delta):
    contract = _parsed_contract(_prompt())
    if field == "MAX_READY_SPOKEN_LYRIC_DURATION_RATIO":
        # Eight sung and two spoken rows: exactly the rendered 20% row/time limits.
        rows = _vocal_rows(["sung"] * 4 + ["spoken"] * 2 + ["sung"] * 4)
        rows[4]["live_end_ms"] += delta
        assert Fraction(2000, 10000) == contract[field]
    elif field == "MAX_READY_CONSECUTIVE_SPOKEN_LYRIC_ROWS":
        rows = _vocal_rows(["sung"] * 15 + ["spoken"] * (contract[field] + delta) + ["sung"] * 15)
    elif field == "MAX_READY_SPOKEN_LYRIC_DURATION_MS":
        rows = _vocal_rows(["sung"] * 4 + ["spoken"] + ["sung"] * 4,
                           spoken_ms=contract[field] + delta, sung_ms=10_000)
    elif field == "MAX_READY_SPOKEN_BLOCK_SPAN_MS":
        rows = _vocal_rows(["sung"] * 4 + ["spoken"] * 2 + ["sung"] * 4,
                           spoken_ms=1000, sung_ms=2000)
        # Move the second spoken interval; voiced duration and all other gates stay valid.
        shift = contract[field] + delta - (rows[5]["live_end_ms"] - rows[4]["live_start_ms"])
        for row in rows[5:]:
            row["live_start_ms"] += shift
            row["live_end_ms"] += shift
    elif field == "MAX_READY_SPOKEN_BLOCKS":
        rows = _vocal_rows(["sung"] * 4 + ["spoken", "sung"] * (contract[field] + delta) + ["sung"] * 4)
    else:
        rows = _vocal_rows(["sung"] * 4 + ["spoken"] * (2 + delta) + ["sung"] * 4,
                           spoken_ms=100, sung_ms=1000)
        assert Fraction(8, 10) == contract[field]
    if delta:
        with pytest.raises(ValueError):
            performance._validate_lyric_vocal_observations(rows, require_ready=True)
    else:
        assert performance._validate_lyric_vocal_observations(rows, require_ready=True) == (True, False, False)


def _hole_result(holes, *, count=80, repeated=False):
    rows = [{"text": f"uniquelyric{index:03d}"} for index in range(count)]
    omitted = [index for hole in holes for index in hole]
    if repeated:
        for offset, index in enumerate(omitted):
            rows[index] = dict(rows[4 + offset])
    return shape.derive_omission_shape(
        lyric_rows=rows, omitted_indices=omitted,
        performed=[(i, i * 2000, i * 2000 + 1000) for i in range(count) if i not in omitted],
        raw_index=list(range(count)), asr_anchor_cues=None,
    )


@pytest.mark.parametrize("extra", [0, 1])
@pytest.mark.parametrize("kind", ["small_size", "small_count", "repeated_minimum", "omitted_rows", "omitted_ratio"])
def test_checker_accepts_only_the_rendered_omission_domain(kind, extra, monkeypatch):
    contract = _parsed_contract(_prompt())
    repeated = False
    if kind == "small_size":
        holes = [range(10, 10 + contract["MAX_SMALL_SKIP_ROWS"] + extra)]
    elif kind == "small_count":
        holes = [range(i, i + 1) for i in range(5, 5 + 10 * (contract["MAX_SMALL_SKIPS"] + extra), 10)]
    elif kind == "repeated_minimum":
        size = contract["MIN_REPEATED_SECTION_ROWS"] - extra
        holes, repeated = [range(80 - size, 80)], True
    elif kind == "omitted_rows":
        size = contract["MAX_LIVE_ARRANGEMENT_OMITTED_ROWS"] + extra
        holes, repeated = [range(80 - size, 80)], True
    else:
        # Raise only the independent row cap so this case isolates the ratio cap.
        monkeypatch.setattr(shape, "MAX_LIVE_ARRANGEMENT_OMITTED_ROWS", 80)
        size = int(80 * contract["MAX_LIVE_ARRANGEMENT_OMITTED_RATIO"]) + extra
        holes, repeated = [range(80 - size, 80)], True
    if extra:
        with pytest.raises(ValueError):
            _hole_result(holes, repeated=repeated)
    else:
        result = _hole_result(holes, repeated=repeated)
        assert result.classification == "COMPLETE_LIVE_ARRANGEMENT"


def test_new_omission_values_reach_prompt_and_actual_shape_checker(monkeypatch):
    monkeypatch.setattr(shape, "MIN_REPEATED_SECTION_ROWS", 3)
    monkeypatch.setattr(shape, "MAX_SMALL_SKIP_ROWS", 1)
    monkeypatch.setattr(shape, "MAX_SMALL_SKIPS", 2)
    parsed = _parsed_contract(_prompt())
    assert (parsed["MIN_REPEATED_SECTION_ROWS"], parsed["MAX_SMALL_SKIP_ROWS"], parsed["MAX_SMALL_SKIPS"]) == (3, 1, 2)
    assert _hole_result([range(77, 80)], repeated=True).ranges[0]["kind"] == "TRAILING_REPEATED_SECTION"
    for holes, repeated in [([range(78, 80)], True), ([range(10, 12)], False),
                            ([range(5, 6), range(15, 16), range(25, 26)], False)]:
        with pytest.raises(ValueError):
            _hole_result(holes, repeated=repeated)


def _completeness(rows, classification="FULL_STUDIO_SEQUENCE"):
    rows = copy.deepcopy(rows)
    for row in rows:
        row["confidence"] = 0.98
    end = max(row["live_end_ms"] for row in rows if row["heard"])
    transition = end + 1000
    return performance.derive_live_arrangement_completeness(
        observations=rows, source_duration_ms=transition + 1000,
        post_song_talk_start_ms=transition,
        live_arrangement={
            "classification": classification, "observed_live_song_opening": True,
            "observed_live_song_ending": True, "post_song_transition_kind": "HOST_TALK",
            "post_song_transition_ms": transition, "notes": "contract boundary control",
        },
    )


@pytest.mark.parametrize("field", [
    "MIN_LIVE_ARRANGEMENT_HEARD_ROWS", "MIN_LIVE_ARRANGEMENT_HEARD_RATIO", "MIN_LIVE_ARRANGEMENT_DURATION_MS",
])
@pytest.mark.parametrize("outside", [False, True])
def test_completeness_checker_uses_rendered_evidence_minima(field, outside):
    contract = _parsed_contract(_prompt())
    if field == "MIN_LIVE_ARRANGEMENT_HEARD_ROWS":
        count = contract[field] - int(outside)
        rows = _vocal_rows(["sung"] * count, sung_ms=5000)
        rows[-1]["live_end_ms"] = rows[0]["live_start_ms"] + 40_000
        reason = "canonical (?:observations|lyric evidence)"
    elif field == "MIN_LIVE_ARRANGEMENT_DURATION_MS":
        rows = _vocal_rows(["sung"] * 8, sung_ms=3000)
        rows[-1]["live_end_ms"] = rows[0]["live_start_ms"] + contract[field] - int(outside)
        reason = "performed span is too short"
    else:
        rows = _vocal_rows(["sung"] * 20, sung_ms=3000)
        omitted = int(len(rows) * (1 - contract[field])) + int(outside)
        for offset, index in enumerate(range(len(rows) - omitted, len(rows))):
            rows[index].update(
                text=rows[4 + offset]["text"], heard=False, live_start_ms=None, live_end_ms=None,
                lyric_vocal_subject="NO_AUDIBLE_LYRIC_VOCAL", lidousha_role="SILENT_OR_NOT_AUDIBLE",
                same_live_vocal_source_as_lidousha=False,
            )
        reason = "too little canonical lyric evidence"
    if outside:
        with pytest.raises(ValueError, match=reason):
            _completeness(rows)
    else:
        assert _completeness(rows)["heard_line_count"] >= contract["MIN_LIVE_ARRANGEMENT_HEARD_ROWS"]


@pytest.mark.parametrize("label", sorted(common.LIVE_ARRANGEMENT_CLASSIFICATIONS))
def test_checker_accepts_rendered_labels_as_observations_only(label):
    result = _completeness(_vocal_rows(["sung"] * 20, sung_ms=2000), label)
    assert result["classification"] == "FULL_STUDIO_SEQUENCE"
    assert result.get("model_classification_disagreement") == (label if label != result["classification"] else None)


@pytest.mark.parametrize("label", ["", "full_studio_sequence", "COMPLETE_UNCHECKED", None])
def test_checker_rejects_labels_outside_the_rendered_range(label):
    assert label not in _parsed_contract(_prompt())["LIVE_ARRANGEMENT_CLASSIFICATIONS"]
    with pytest.raises(ValueError, match="values are invalid"):
        _completeness(_vocal_rows(["sung"] * 20, sung_ms=2000), label)


def _literal_sites(source, filename):
    """Semantic threshold operands, not unrelated uses of 1, 2, 3 or 5."""
    tree = ast.parse(source, filename=filename)
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.body and isinstance(node.body[0], ast.Expr)
                  and isinstance(node.body[0].value, ast.Constant)}
    pattern = re.compile(
        r"at least \d+% (?:of the heard/performed rows|canonical coverage)|"
        r"no more than \w+ consecutive rows|at most \w+ such spoken|"
        r"at most \d+ seconds and \d+%|first-to-last span must be at most \d+ seconds|"
        r"at least \d+ rows sung contiguously|at most \d+ rows between heard rows|"
        r"\d+ such holes|at least \d+ heard lyric rows|at least \d+ seconds of performance|"
        r"\d+ hole may be larger than|all holes together stay within \d+ rows"
    )
    threshold_names = {"singing_rows", "longest_spoken_run", "spoken_blocks",
                       "spoken_duration_ms", "longest_spoken_block_span_ms"}
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                common_owned = target.id.startswith(("MIN_READY_", "MAX_READY_", "MIN_LIVE_ARRANGEMENT_", "MAX_LIVE_ARRANGEMENT_"))
                shape_owned = target.id in {"MIN_REPEATED_SECTION_ROWS", "MAX_SMALL_SKIP_ROWS", "MAX_SMALL_SKIPS"}
                if ((common_owned and filename != "src/autoslice/song_common.py")
                        or (shape_owned and filename != "src/autoslice/song_omission_shape.py")):
                    sites.append(f"{filename}:{node.lineno}: threshold defined outside its owner")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if pattern.search(" ".join(node.value.split())):
                sites.append(f"{filename}:{node.lineno}: prompt threshold literal")
        if isinstance(node, ast.Compare):
            names = {part.id for part in ast.walk(node) if isinstance(part, ast.Name)}
            # The repetition rule compares a run length with a minimum. An
            # index bound or a text-identity cardinality is a different rule.
            repeated_length = (
                len(node.ops) == 1 and isinstance(node.ops[0], (ast.Gt, ast.GtE))
                and isinstance(node.left, ast.Call) and isinstance(node.left.func, ast.Name)
                and node.left.func.id == "len" and len(node.left.args) == 1
                and isinstance(node.left.args[0], ast.Name) and node.left.args[0].id == "texts"
            )
            if names & threshold_names or repeated_length:
                if any(isinstance(part, ast.Constant) and type(part.value) in (int, float)
                       for part in ast.walk(node)):
                    sites.append(f"{filename}:{node.lineno}: checker threshold literal")
    return sites


def test_no_production_module_spells_song_thresholds_outside_definitions():
    sites = []
    for path in sorted((ROOT / "src" / "autoslice").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        sites.extend(_literal_sites(path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))))
    assert sites == [], "\n".join(sites)
    source = (ROOT / "src/autoslice/song_performance.py").read_text(encoding="utf-8")
    imports = {alias.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)
               and node.module == "src.autoslice.song_common" for alias in node.names}
    assert "MAX_READY_SPOKEN_LYRIC_DURATION_RATIO" in imports


@pytest.mark.parametrize("source", [
    'prompt = "at most 12 seconds and 20% of all lyric-vocal duration"',
    "reject = spoken_duration_ms * 5 > total_lyric_vocal_duration_ms",
    "repeated = len(texts) >= 2",
    "MAX_READY_SPOKEN_LYRIC_DURATION_MS = 12_000",
    'prompt = "no more than 9 consecutive rows"',
])
def test_spelling_guard_rejects_the_old_responsible_mechanisms(source):
    assert _literal_sites(source, "canary.py")


@pytest.mark.parametrize("source", [
    "valid = 0 < int(index) <= len(texts) and position < len(befores)",
    "valid = 0 < index <= len(texts)",
    "drift = len(texts) != 1",
    "has_next = cue_offset + 1 < len(texts)",
])
def test_spelling_guard_accepts_unrelated_text_lengths(source):
    assert _literal_sites(source, "canary.py") == []


def test_pipeline_doc_points_to_definitions_without_a_second_numeric_rule():
    doc = (ROOT / "docs/pipeline/50-song-lane.md").read_text(encoding="utf-8")
    for name in ("MIN_REPEATED_SECTION_ROWS", "MAX_SMALL_SKIP_ROWS", "MAX_SMALL_SKIPS"):
        assert f"song_omission_shape.{name}" in doc
    omission = doc.split("洞＝连续一段", 1)[1].split("洞必须是真没唱", 1)[0]
    assert not re.search(r"[≥≤>]\s*\d+\s*行|最多\s*\d+\s*个", omission)


@pytest.mark.parametrize("name,expected", [
    ("kuaile_shouzhang_v01", "FULL_STUDIO_SEQUENCE"),
    ("shimian_feixing_v02", "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"),
    ("shimian_feixing_v03", "COMPLETE_LIVE_WITH_SPOKEN_INTERLUDE"),
])
def test_real_witnesses_keep_ordinary_entry_and_cold_consumer_verdicts(tmp_path, name, expected):
    from test_song_omission_shape import _consumed, _repair

    result, _rows, _cues = _repair(tmp_path, name)
    assert result.repaired, result.attempts
    assert result.song_boundary["completion_basis"] == expected
    _consumed(tmp_path, result)


def test_real_rejected_variant_stays_rejected_and_variant_order_is_unchanged(tmp_path):
    from test_song_omission_shape import _consumed, _shimian_variant_runs, _sung_hole_rejections

    rejected = tmp_path / "rejected"
    selected = tmp_path / "selected"
    rejected.mkdir()
    selected.mkdir()
    result = _shimian_variant_runs(rejected, (1,))
    assert not result.repaired and _sung_hole_rejections(result)
    result = _shimian_variant_runs(selected, (1, 2, 3))
    assert result.repaired, result.attempts
    assert result.lyrics_alignment["external_lrc"] == "fixture://shimian/v02"
    _consumed(selected, result)


@pytest.mark.parametrize("title", ["历历万乡", "海海海"])
def test_published_receipt_completeness_is_unchanged(title):
    # An administrative OCI3 run can point to its archived receipts. This is
    # read-only; absolute producer paths are resolved only inside that archive.
    root = Path(os.environ.get("SONG_CONTRACT_RECEIPT_ROOT", str(ROOT / "reports")))
    matches = []
    for path in root.rglob("*.lyrics-alignment-report.json"):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("song_title") == title:
            matches.append((path, report))
    if not matches:
        pytest.skip(f"{title}: real published alignment receipt unavailable under {root}; provide SONG_CONTRACT_RECEIPT_ROOT")
    # Prefer the producer report next to its bound raw observation.
    path, report = min(matches, key=lambda item: "REVIEW-PACKAGE" in str(item[0]))
    artifacts = report["audio_alignment_artifacts"]
    raw_candidates = list(path.parent.rglob(Path(artifacts["raw_output_path"]).name))
    raw_candidates += list(root.rglob(Path(artifacts["raw_output_path"]).name))
    expected_sha = artifacts["raw_output_sha256"].removeprefix("sha256:")
    raw_path = next((p for p in raw_candidates if hashlib.sha256(p.read_bytes()).hexdigest() == expected_sha), None)
    assert raw_path is not None, f"{title}: hash-bound raw observation is missing"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    heard = [row for row in raw["observations"] if row["heard"]]
    assert performance._validate_lyric_vocal_observations(heard, require_ready=True) == (True, False, False)
    for producer in (False, True):
        kwargs = dict(spot_checks=raw["spot_checks"], live_performance=raw["live_performance"]) if producer else {}
        derived = performance.derive_live_arrangement_completeness(
            observations=copy.deepcopy(raw["observations"]), live_arrangement=raw["live_arrangement"],
            post_song_talk_start_ms=raw["post_song_talk_start_ms"],
            source_duration_ms=artifacts["source_duration_ms"],
            timing_alignment=report.get("alignment") if is_timing_model_report(report) else None,
            **kwargs,
        )
        assert derived == report["arrangement_completeness"], title
