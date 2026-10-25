"""Clip profile: the one definition of a delivered clip's form.

公开规则:04Z：「在这种与李豆沙相关的，李豆沙不是主角的切片里，不需要加李豆沙片头」
公开规则：给出格式的检查格式的应该来自同一个定义…两个输出而已

A clip's form used to be implied by ``lane`` (talk/song) alone, so a related
clip in which the host is not the protagonist inherited the talk intro and the
default talk collection. The form is now declared once, in the producer spec
as ``spec["clip_profile"]``; a record or upload manifest only transcribes the
declared document. Everything that needs the form derives it here:

* ``requires_intro(profile)`` — whether the delivered video carries the
 channel intro. The intro belongs only to talk clips whose protagonist is the
 host; songs (SONG-07「不放片头直接进歌」) and non-protagonist related clips are
 derived members of the same rule, not separate exemptions. Callers gate
 their existing ``require_branding_intro`` call on it, so the channel manifest
 switch and the ``AUTOSLICE_BRANDING_INTRO=off`` test/emergency hatch stay
 where they are.
* ``season_for(profile)`` — the collection the archive joins.
* ``intro_binding_problem(profile, binding, ...)`` — the delivery-gate check
 that a burned intro binding agrees with the form.

Only ``host_is_protagonist`` and ``delivery_kind`` feed these derivations.
``input_kind`` and ``watched_media`` are declared here so later consumers read
one definition; no derivation in this module consumes them. ``watched_media``
keeps its existing single declaration site, the spec's top-level
``watched_media`` field; the spec's ``clip_profile`` block must not repeat it.

A spec, record or manifest without a declaration derives the historical lane
default in memory. The default is never written back, so existing bytes and
their hash bindings are unchanged. The account-bound collection/section IDs
live only in ``SEASON_IDS``.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Protocol, TypedDict


CLIP_PROFILE_SCHEMA = "clip-profile.v1"
CLIP_PROFILE_FIELD = "clip_profile"


class ClipProfileHolder(Protocol):
    """Read only the declared field; unrelated spec/record fields stay opaque."""

    def __contains__(self, key: object, /) -> bool: ...
    def __getitem__(self, key: str, /) -> object: ...
    def get(self, key: str, /) -> object: ...


class ClipProfileDeclaration(TypedDict):
    schema_version: str
    host_is_protagonist: bool
    input_kind: str
    delivery_kind: str


class ClipProfileDocument(ClipProfileDeclaration):
    watched_media: bool | None


INPUT_KINDS = ("full_recording", "approved_window", "external_vod")
DELIVERY_KINDS = ("talk", "song", "independent_related")

# The host-protagonist value each delivery kind admits.  Any other
# combination has no governing rule yet and is refused instead of guessed.
_PROTAGONIST_BY_DELIVERY_KIND = MappingProxyType(
    {"talk": True, "song": True, "independent_related": False}
)
# Delivery kinds each producer lane may declare.  The talk producer is the
# ordinary path for related clips (GOAL-09); the song lane only makes songs.
_LANE_DELIVERY_KINDS = MappingProxyType(
    {"talk": frozenset({"talk", "independent_related"}), "song": frozenset({"song"})}
)

_SPEC_DECLARATION_FIELDS = frozenset(
    {"schema_version", "host_is_protagonist", "input_kind", "delivery_kind"}
)
_DOCUMENT_FIELDS = _SPEC_DECLARATION_FIELDS | {"watched_media"}

INTRO_REQUIRED_CODE = "BRANDING_INTRO_REQUIRED_FOR_PROFILE"
INTRO_FORBIDDEN_CODE = "BRANDING_INTRO_FORBIDDEN_FOR_PROFILE"

# Collection titles and the maintainer account's collection/section IDs, keyed
# by delivery kind.  The IDs are account-bound (public export: deployer
# supplied); the title is what the public archive view must display.
SEASON_TITLES = MappingProxyType(
    {"talk": "小李切片", "song": "小李歌唱", "independent_related": "李豆沙相关"}
)
_SEASON_IDS_ENV = "AUTOSLICE_SEASON_IDS"


def season_ids() -> dict[str, dict[str, int]]:
    """入集校验的合集/小节 ID——账号专属，强制部署方自填，无默认值。"""
    import json
    import os

    raw = os.environ.get(_SEASON_IDS_ENV)
    if not raw:
        raise SystemExit(
            "AUTOSLICE_SEASON_IDS 未配置：发布入集校验需要你自己账号的合集/小节 ID。\n"
            '格式：{"talk": {"season_id": 0, "section_id": 0}, '
            '"song": {"season_id": 0, "section_id": 0}, '
            '"independent_related": {"season_id": 0, "section_id": 0}}\n'
            "获取：在创作中心手动把任一稿件加入目标合集，再用 "
            "scripts/bili_archive_tool.py view 回读该稿件的 season_id/section_id。"
        )
    return json.loads(raw)


class ClipProfileError(ValueError):
    """A declared clip profile is malformed or has no governing rule."""


@dataclass(frozen=True)
class ClipProfile:
    host_is_protagonist: bool
    input_kind: str
    delivery_kind: str
    watched_media: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host_is_protagonist, bool):
            raise ClipProfileError("clip_profile.host_is_protagonist must be a boolean")
        if self.input_kind not in INPUT_KINDS:
            raise ClipProfileError(
                f"clip_profile.input_kind must be one of {list(INPUT_KINDS)}: {self.input_kind!r}"
            )
        if self.delivery_kind not in DELIVERY_KINDS:
            raise ClipProfileError(
                f"clip_profile.delivery_kind must be one of {list(DELIVERY_KINDS)}: "
                f"{self.delivery_kind!r}"
            )
        if self.watched_media is not None and not isinstance(self.watched_media, bool):
            raise ClipProfileError("clip_profile.watched_media must be a boolean or null")
        if _PROTAGONIST_BY_DELIVERY_KIND[self.delivery_kind] is not self.host_is_protagonist:
            raise ClipProfileError(
                f"clip_profile delivery_kind {self.delivery_kind!r} has no rule for "
                f"host_is_protagonist={self.host_is_protagonist}"
            )


@dataclass(frozen=True)
class SeasonRoute:
    """One collection route; ``lane`` is the manifest season block's key."""

    lane: str
    season_title: str

    @property
    def ids(self) -> dict[str, int]:
        return season_ids()[self.lane]

    @property
    def season_id(self) -> int:
        return self.ids["season_id"]

    @property
    def section_id(self) -> int:
        return self.ids["section_id"]


def default_clip_profile(lane: str, *, watched_media: bool | None = None) -> ClipProfile:
    """The historical form of an undeclared clip, derived from its lane."""

    if lane not in _LANE_DELIVERY_KINDS:
        raise ClipProfileError(f"no default clip profile for lane {lane!r}")
    return ClipProfile(
        host_is_protagonist=True,
        input_kind="full_recording",
        delivery_kind=lane,
        watched_media=watched_media,
    )


def render_clip_profile(profile: ClipProfile) -> ClipProfileDocument:
    """The transcribed document form recorded by records and manifests."""

    return {
        **spec_clip_profile_declaration(profile),
        "watched_media": profile.watched_media,
    }


def spec_clip_profile_declaration(profile: ClipProfile) -> ClipProfileDeclaration:
    """Reconstruct the spec declaration when replaying an already sealed record."""

    return {
        "schema_version": CLIP_PROFILE_SCHEMA,
        "host_is_protagonist": profile.host_is_protagonist,
        "input_kind": profile.input_kind,
        "delivery_kind": profile.delivery_kind,
    }


def _from_fields(document: object, *, fields: frozenset[str], label: str) -> ClipProfile:
    if not isinstance(document, Mapping):
        raise ClipProfileError(f"{label} must be an object")
    if set(document) != fields:
        raise ClipProfileError(
            f"{label} fields must be exactly {sorted(fields)}; got {sorted(map(str, document))}"
        )
    if document.get("schema_version") != CLIP_PROFILE_SCHEMA:
        raise ClipProfileError(f"{label} schema_version must be {CLIP_PROFILE_SCHEMA!r}")
    protagonist = document["host_is_protagonist"]
    input_kind = document["input_kind"]
    delivery_kind = document["delivery_kind"]
    watched = document.get("watched_media")
    if not isinstance(protagonist, bool):
        raise ClipProfileError("clip_profile.host_is_protagonist must be a boolean")
    if not isinstance(input_kind, str):
        raise ClipProfileError("clip_profile.input_kind must be a string")
    if not isinstance(delivery_kind, str):
        raise ClipProfileError("clip_profile.delivery_kind must be a string")
    if watched is not None and not isinstance(watched, bool):
        raise ClipProfileError("clip_profile.watched_media must be a boolean or null")
    return ClipProfile(protagonist, input_kind, delivery_kind, watched)


def parse_clip_profile(document: object) -> ClipProfile:
    """Inverse of ``render_clip_profile``; only its value domain is accepted."""

    return _from_fields(document, fields=_DOCUMENT_FIELDS, label="clip_profile document")


def _require_lane(profile: ClipProfile, lane: str) -> ClipProfile:
    allowed = _LANE_DELIVERY_KINDS.get(lane)
    if allowed is None:
        raise ClipProfileError(f"no clip profile rule for lane {lane!r}")
    if profile.delivery_kind not in allowed:
        raise ClipProfileError(
            f"clip_profile delivery_kind {profile.delivery_kind!r} cannot be produced by the "
            f"{lane} lane"
        )
    return profile


def clip_profile_from_spec(spec: ClipProfileHolder, *, lane: str) -> ClipProfile:
    """Read the one declaration from a producer spec (lane default if absent)."""

    watched = spec.get("watched_media")
    if CLIP_PROFILE_FIELD not in spec:
        # Undeclared specs keep their historical behavior; watched_media is
        # only mirrored when it already has the established boolean form.
        return default_clip_profile(lane, watched_media=watched if isinstance(watched, bool) else None)
    block = spec[CLIP_PROFILE_FIELD]
    if isinstance(block, Mapping) and "watched_media" in block:
        raise ClipProfileError(
            "spec.clip_profile must not repeat watched_media; spec.watched_media is its declaration"
        )
    declared = _from_fields(block, fields=_SPEC_DECLARATION_FIELDS, label="spec.clip_profile")
    if watched is not None and not isinstance(watched, bool):
        raise ClipProfileError("spec.watched_media must be a boolean when a clip_profile is declared")
    return _require_lane(
        ClipProfile(
            host_is_protagonist=declared.host_is_protagonist,
            input_kind=declared.input_kind,
            delivery_kind=declared.delivery_kind,
            watched_media=watched,
        ),
        lane,
    )


def clip_profile_from_document_holder(holder: ClipProfileHolder, *, lane: str) -> ClipProfile:
    """Read a transcribed profile from a record or manifest (lane default if absent)."""

    if CLIP_PROFILE_FIELD not in holder:
        return default_clip_profile(lane)
    return _require_lane(parse_clip_profile(holder[CLIP_PROFILE_FIELD]), lane)


def spec_clip_profile_transcription(spec: ClipProfileHolder, *, lane: str) -> ClipProfileDocument | None:
    """Document a record transcribes from its spec; None keeps legacy bytes."""

    if CLIP_PROFILE_FIELD not in spec:
        return None
    return render_clip_profile(clip_profile_from_spec(spec, lane=lane))


def holder_clip_profile_transcription(holder: ClipProfileHolder, *, lane: str) -> ClipProfileDocument | None:
    """Document a manifest transcribes from its record; None keeps legacy bytes."""

    if CLIP_PROFILE_FIELD not in holder:
        return None
    return render_clip_profile(clip_profile_from_document_holder(holder, lane=lane))


def requires_intro(profile: ClipProfile) -> bool:
    """The one intro predicate: the host's own talk clip carries the intro."""

    return profile.host_is_protagonist and profile.delivery_kind == "talk"


def intro_binding_problem(
    profile: ClipProfile,
    binding: object,
    *,
    intro_mandatory: bool,
) -> str | None:
    """Delivery gate: the burned intro binding must agree with the form.

    A form that takes the intro must carry a ``PREPENDED`` binding whenever the
    channel policy makes it mandatory.  A form that takes none must carry no
    binding at all — the producer records ``None`` when no context is given.
    """

    if requires_intro(profile):
        prepended = isinstance(binding, Mapping) and binding.get("status") == "PREPENDED"
        return INTRO_REQUIRED_CODE if intro_mandatory and not prepended else None
    return INTRO_FORBIDDEN_CODE if binding is not None else None


def season_for(profile: ClipProfile) -> SeasonRoute:
    """The one collection route for a delivered clip."""

    return SeasonRoute(lane=profile.delivery_kind, season_title=SEASON_TITLES[profile.delivery_kind])


CLIP_PROFILE_INVALID_CODE = "CLIP_PROFILE_INVALID"


def record_intro_gate(
    record: ClipProfileHolder, *, lane: str
) -> tuple[ClipProfile | None, str | None, str]:
    """Package-audit form of the delivery gate over one record.

    Returns ``(form, problem_code, detail)``.  A form that takes the intro
    returns no problem here: the current-roster audit owns that branch.
    """

    try:
        form = clip_profile_from_document_holder(record, lane=lane)
    except ClipProfileError as exc:
        return None, CLIP_PROFILE_INVALID_CODE, str(exc)
    if requires_intro(form):
        return form, None, ""
    burned = record.get("burned_preview")
    binding = burned.get("branding_intro") if isinstance(burned, Mapping) else None
    problem = intro_binding_problem(form, binding, intro_mandatory=False)
    detail = f"delivery_kind={form.delivery_kind}; binding={binding!r}" if problem else ""
    return form, problem, detail


def season_block(*, title_lane: str, choice: str, holder: ClipProfileHolder) -> dict[str, str] | None:
    """Render the upload manifest's frozen season block from the clip form.

    ``holder`` is the record (lane default when it declares no profile);
    ``choice`` is ``auto``, ``none`` or an explicit talk/song that must agree
    with the derived route.
    """

    if choice == "none":
        return None
    if choice not in ("auto", "talk", "song"):
        raise ValueError(f"unknown season choice {choice!r}")
    route = season_for(clip_profile_from_document_holder(holder, lane=title_lane))
    declared = CLIP_PROFILE_FIELD in holder
    if choice != "auto" and choice != route.lane:
        if declared:
            raise ValueError(
                f"--season {choice} contradicts the clip_profile route {route.lane!r}"
                " — the declared clip form decides the season"
            )
        raise ValueError(
            f"--season {choice} contradicts the title-derived lane {title_lane!r}"
            " — the title decides the season; fix the title instead"
        )
    source = f"{choice}:clip-profile" if declared else f"{choice}:title-prefix"
    return {"lane": route.lane, "season_title": route.season_title, "source": source}


def season_block_problems(manifest: ClipProfileHolder, *, title_lane: str) -> list[str]:
    """Check a frozen season block against the manifest's transcribed form."""

    block = manifest.get("season")
    if not isinstance(block, Mapping):
        return []
    try:
        route = season_for(clip_profile_from_document_holder(manifest, lane=title_lane))
    except ClipProfileError as exc:
        return [f"manifest clip_profile is invalid: {exc}"]
    if block.get("lane") == route.lane:
        return []
    if CLIP_PROFILE_FIELD in manifest:
        return ["manifest season lane contradicts its clip profile"]
    return ["manifest season lane contradicts the frozen title"]
