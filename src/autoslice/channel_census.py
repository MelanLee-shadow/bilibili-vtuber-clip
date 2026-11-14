"""Channel census: coarse per-window scorecard grades -> where-to-look clues.

Offline generator and optional recall input.
Transcription/window labeling is available through the offline CLI. Only recall
consumes the generated asset; scoring and release gates do not read it.

The census measures recorded streams with the *same* dimensions that
``selection_scorecard`` uses for candidates, so "what makes a good slice" keeps
exactly one definition.  A census never produces a Tier, a score, or a
``candidate_quality`` verdict.  Its only product besides a report is a
``CensusClueSet``: observation questions telling the recall prompt where to
look and how to recognize a payoff.  维护者 (H47) forbids turning the
census into category weights, penalties, exclusions or quotas; that ban is
structural here: no clue type has a field able to carry such a number.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from typing import Literal, TypeAlias, cast, get_args

from src.autoslice.selection_scorecard import DIMENSION_WEIGHTS

CLUE_SET_SCHEMA = "channel-census-clues.v1"
CENSUS_CLUES_ASSET_KEY = "selection_census_clues"
CENSUS_CONFIG_ASSET_KEY = "channel_census_config"
CENSUS_CONFIG_SCHEMA = "channel-census-config.v1"
CensusJSON: TypeAlias = None | bool | int | float | str | list["CensusJSON"] | dict[str, "CensusJSON"]

# A five-minute window is not a candidate: whether a stranger can follow a
# clip depends on the final boundary, so ``self_contained`` is never graded.
WINDOW_UNGRADABLE_DIMENSIONS: frozenset[str] = frozenset({"self_contained"})
# Derived from the scorecard, never restated (one definition of the dimensions).
CENSUS_WINDOW_DIMENSIONS: tuple[str, ...] = tuple(
    name for name in DIMENSION_WEIGHTS if name not in WINDOW_UNGRADABLE_DIMENSIONS
)


GradeBasis = Literal["CENSUS_LABEL", "LEGACY_LABEL_PROXY"]
# Where in the stream the host is (location axis).  Value lives only in the
# dimensions; legacy content-value labels (ADDRESS/RELATION/YURI) are retired.
HostActivity = Literal[
    "SINGING",
    "GAMEPLAY",
    "MEDIA",
    "VIEWER_QA",
    "FREE_TALK",
    "COLLAB",
    "STREAM_OPERATIONS",
    "NO_SPEECH",
    "OTHER",
    "UNSPECIFIED",  # legacy adapter only: old primary was a value label
]
# Closed set of "where to look / how to recognize" clue kinds.  There is
# deliberately no member that excludes, demotes, caps or prioritizes anything.
ClueKind = Literal[
    "CROSS_ACTIVITY_EMBEDDED",
    "DIMENSION_LOOKFOR",
    "ENTITY_RECOGNITION",
    "ACTIVITY_COVERAGE",
]
CensusCluesStatus = Literal["ABSENT", "INVALID", "PROFILE_MISMATCH", "VALID"]

# ``lint_clue_text`` rejects clue text carrying a selection directive.  The
# fixed preamble is stored separately because it must say "不加减分".
FORBIDDEN_DIRECTIVE_PHRASES: tuple[str, ...] = (
    "加分",
    "扣分",
    "减分",
    "排除",
    "不选",
    "少选",
    "配额",
    "限额",
    "最多",
    "降档",
    "降级",
    "优先选",
)
CLUE_PREAMBLE_ZH = (
    "以下只是去哪找、怎样认出看点的线索，不进入评分算术，不按活动或题材加减分、"
    "排除或限额；普查频率不能代替本次候选的片内证据。"
)


@dataclass(frozen=True)
class AudienceInterest:
    """Machine index of an interest the channel metric already describes.

    ``label_zh`` must occur verbatim in the metric's audience-salience
    paragraph; the metric stays the editorial authority.
    """

    interest_id: str
    label_zh: str
    description_zh: str


@dataclass(frozen=True)
class CensusConfig:
    """``assets/<profile>/channel_census_config.v1.json``."""

    profile_id: str
    audience_interests: tuple[AudienceInterest, ...]
    metric_dimension_section_heading: str
    metric_audience_paragraph_marker: str


@dataclass(frozen=True)
class EvidenceRatio:
    """Descriptive window count only; rendered in the report, never the prompt."""

    numerator: int
    denominator: int
    definition_zh: str


@dataclass(frozen=True)
class CensusClue:
    """A where-to-look prior.  No field can express a weight or an exclusion."""

    clue_id: str
    kind: ClueKind
    dimension_pointers: tuple[str, ...]
    host_activities: tuple[HostActivity, ...]
    observation_question: str
    recognition_cues: tuple[str, ...]
    evidence: EvidenceRatio
    example_window_ids: tuple[str, ...]
    measurement_basis: GradeBasis

    def __post_init__(self) -> None:
        if self.kind not in get_args(ClueKind):
            raise ValueError("INVALID_CLUE_KIND")
        if self.measurement_basis not in get_args(GradeBasis):
            raise ValueError("INVALID_MEASUREMENT_BASIS")
        if any(d not in CENSUS_WINDOW_DIMENSIONS for d in self.dimension_pointers):
            raise ValueError("INVALID_DIMENSION_POINTER")
        if any(a not in get_args(HostActivity) for a in self.host_activities):
            raise ValueError("INVALID_HOST_ACTIVITY")
        if any(type(n) is not int or n < 0 for n in
               (self.evidence.numerator, self.evidence.denominator)):
            raise ValueError("INVALID_EVIDENCE_COUNT")
        for text in (self.observation_question, *self.recognition_cues):
            _validate_prompt_text(text)


@dataclass(frozen=True)
class ClueProvenance:
    census_id: str
    census_date: str
    input_sha256: str
    generator_version: str
    label_prompt_sha256: str
    window_count: int
    valid_window_count: int


@dataclass(frozen=True)
class CensusClueSet:
    """``assets/<profile>/selection_census_clues.v1.json`` (whole-file replace).

    Not registered in ``pipeline_fingerprint_files`` nor in the Song proof
    closure: regenerating it must not drift any proof fingerprint.
    """

    schema_version: str
    profile_id: str
    provenance: ClueProvenance
    preamble_zh: str
    clues: tuple[CensusClue, ...]


@dataclass(frozen=True)
class RecallClueProvenance:
    """Stored on new scorecards like ``semantic_recall_chat_evidence``; no gate."""

    schema_version: str
    census_id: str
    asset_sha256: str
    status: CensusCluesStatus


def extract_metric_section(metric_text: str, *, heading: str) -> str:
    """Return an exact Markdown section, through subheadings but not its next peer."""

    lines = metric_text.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.strip() == heading]
    if len(matches) != 1 or not re.match(r"^#{1,6} ", heading):
        raise ValueError("CENSUS_METRIC_SECTION_NOT_UNIQUE")
    start = matches[0]
    level = len(heading.split(" ", 1)[0])
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if match := re.match(r"^(#{1,6})\s", lines[i]):
            if len(match[1]) <= level:
                end = i
                break
    return "".join(lines[start:end])


def lint_census_config(config: CensusConfig, *, metric_text: str) -> None:
    section = extract_metric_section(metric_text, heading=config.metric_dimension_section_heading)
    lines = section.splitlines()
    starts = [i for i, line in enumerate(lines)
              if line.startswith(config.metric_audience_paragraph_marker)]
    if not config.metric_audience_paragraph_marker or len(starts) != 1:
        raise ValueError("CENSUS_AUDIENCE_PARAGRAPH_NOT_UNIQUE")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines))
                if re.match(r"^(?:\d+[.)]\s|#{1,6}\s)", lines[i])), len(lines))
    paragraph = "\n".join(lines[start:end])
    ids: set[str] = set()
    for interest in config.audience_interests:
        if (not interest.interest_id.strip() or interest.interest_id in ids or
                not interest.label_zh.strip() or not interest.description_zh.strip()):
            raise ValueError("INVALID_CENSUS_AUDIENCE_INTEREST")
        ids.add(interest.interest_id)
        if interest.label_zh not in paragraph:
            raise ValueError(f"CENSUS_INTEREST_NOT_IN_METRIC: {interest.label_zh}")


def parse_census_config(raw: object, *, profile_id: str) -> CensusConfig:
    row = _json_object(raw)
    if row.get("schema_version") != CENSUS_CONFIG_SCHEMA:
        raise ValueError("INVALID_CENSUS_CONFIG_SCHEMA")
    if row.get("profile_id") != profile_id:
        raise ValueError("CENSUS_PROFILE_MISMATCH")
    interests = row.get("audience_interests")
    if not isinstance(interests, list):
        raise ValueError("INVALID_CENSUS_AUDIENCE_INTERESTS")
    return CensusConfig(profile_id, tuple(
        AudienceInterest(_json_text(item, "interest_id"), _json_text(item, "label_zh"),
                         _json_text(item, "description_zh")) for item in interests
    ), _json_text(row, "metric_dimension_section_heading"),
        _json_text(row, "metric_audience_paragraph_marker"))


def _json_object(raw: object) -> dict[str, CensusJSON]:
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise ValueError("INVALID_CENSUS_JSON_OBJECT")
    return cast(dict[str, CensusJSON], raw)


def _json_text(raw: object, key: str, *, allow_empty: bool = False) -> str:
    value = _json_object(raw).get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"INVALID_CENSUS_TEXT: {key}")
    return value


def _json_strings(raw: object, key: str) -> tuple[str, ...]:
    value = _json_object(raw).get(key)
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"INVALID_CENSUS_STRING_ARRAY: {key}")
    result = cast(list[str], value)
    if len(set(result)) != len(result):
        raise ValueError(f"DUPLICATE_CENSUS_STRING: {key}")
    return tuple(result)


def lint_clue_text(text: str) -> tuple[str, ...]:
    """Return the forbidden directive phrases found in clue text (empty = clean)."""

    normalized = "".join(unicodedata.normalize("NFKC", text).split())
    return tuple(phrase for phrase in FORBIDDEN_DIRECTIVE_PHRASES if phrase in normalized)


def render_census_clue_block(clue_set: CensusClueSet) -> str:
    """Prompt-facing text: preamble + one question per clue; contains no numbers."""

    if clue_set.schema_version != CLUE_SET_SCHEMA or clue_set.preamble_zh != CLUE_PREAMBLE_ZH:
        raise ValueError("INVALID_CLUE_SET")
    lines = ["## 去哪找：频道普查线索（自动生成）", "", CLUE_PREAMBLE_ZH, ""]
    for clue in clue_set.clues:
        for text in (clue.observation_question, *clue.recognition_cues):
            _validate_prompt_text(text)
        lines.append(f"- {clue.observation_question}")
    block = "\n".join(lines) + "\n"
    if any(ch.isdigit() for ch in block):
        raise ValueError("DIGIT_IN_CLUE_BLOCK")
    return block


def load_census_recall_input(
    path: Path | None, *, profile_id: str,
) -> tuple[CensusClueSet | None, CensusCluesStatus, str]:
    """Read once: the returned digest identifies exactly the parsed input bytes."""

    if path is None:
        return None, "ABSENT", ""
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return None, "ABSENT", ""
    except OSError:
        return None, "INVALID", ""
    digest = hashlib.sha256(payload).hexdigest()
    try:
        row = _json_object(json.loads(payload))
        _clue_keys(row, CensusClueSet)
        if row["schema_version"] != CLUE_SET_SCHEMA:
            raise ValueError("INVALID_CLUE_SET_SCHEMA")
        asset_profile = _json_text(row, "profile_id")
        if asset_profile != profile_id:
            return None, "PROFILE_MISMATCH", digest
        if row["preamble_zh"] != CLUE_PREAMBLE_ZH:
            raise ValueError("INVALID_CLUE_PREAMBLE")
        prov = _json_object(row["provenance"])
        _clue_keys(prov, ClueProvenance)
        provenance = ClueProvenance(
            _json_text(prov, "census_id"), _json_text(prov, "census_date"),
            _clue_sha256(prov, "input_sha256"), _json_text(prov, "generator_version"),
            _clue_sha256(prov, "label_prompt_sha256"), _clue_count(prov, "window_count"),
            _clue_count(prov, "valid_window_count"),
        )
        if provenance.valid_window_count > provenance.window_count:
            raise ValueError("INVALID_CLUE_WINDOW_COUNTS")
        raw_clues = row["clues"]
        if not isinstance(raw_clues, list):
            raise ValueError("INVALID_CLUES")
        clues: list[CensusClue] = []
        for raw_clue in raw_clues:
            clue = _json_object(raw_clue)
            _clue_keys(clue, CensusClue)
            evidence = _json_object(clue["evidence"])
            _clue_keys(evidence, EvidenceRatio)
            ratio = EvidenceRatio(_clue_count(evidence, "numerator"),
                                  _clue_count(evidence, "denominator"),
                                  _json_text(evidence, "definition_zh"))
            if ratio.numerator > ratio.denominator:
                raise ValueError("INVALID_CLUE_RATIO")
            clues.append(CensusClue(
                _json_text(clue, "clue_id"), cast(ClueKind, _json_text(clue, "kind")),
                _json_strings(clue, "dimension_pointers"),
                cast(tuple[HostActivity, ...], _json_strings(clue, "host_activities")),
                _json_text(clue, "observation_question"), _json_strings(clue, "recognition_cues"),
                ratio, _json_strings(clue, "example_window_ids"),
                cast(GradeBasis, _json_text(clue, "measurement_basis")),
            ))
        if len({c.clue_id for c in clues}) != len(clues):
            raise ValueError("DUPLICATE_CLUE_ID")
        result = CensusClueSet(CLUE_SET_SCHEMA, asset_profile, provenance, CLUE_PREAMBLE_ZH, tuple(clues))
        render_census_clue_block(result)
        return result, "VALID", digest
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return None, "INVALID", digest


def _clue_keys(
    row: dict[str, CensusJSON],
    cls: type[CensusClueSet | ClueProvenance | CensusClue | EvidenceRatio],
) -> None:
    # Exact schemas also reject smuggled weight/exclusion fields at every level.
    if set(row) != {field.name for field in fields(cls)}:
        raise ValueError("INVALID_CLUE_FIELDS")


def _clue_count(row: dict[str, CensusJSON], key: str) -> int:
    value = row.get(key)
    if type(value) is not int or value < 0:
        raise ValueError("INVALID_CLUE_COUNT")
    return value


def _clue_sha256(row: dict[str, CensusJSON], key: str) -> str:
    value = _json_text(row, key)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("INVALID_CLUE_SHA256")
    return value


def _validate_prompt_text(text: str) -> None:
    if hits := lint_clue_text(text):
        raise ValueError("CLUE_TEXT_DIRECTIVE: " + ",".join(hits))
    if any(ch.isdigit() for ch in text):
        raise ValueError("DIGIT_IN_CLUE_BLOCK")
    if any(ch in text for ch in "\r\n"):
        raise ValueError("MULTILINE_CLUE_TEXT")


