"""Official emote stickers as an alternate cover subject (维护者).

The library is a committed per-channel manifest (``emote_library`` profile
asset); the sticker media ships with this repo under the profile tree
(``assets/<profile>/emote/``; override the root via ``AUTOSLICE_EMOTE_DIR``).  Policy, enforced by the callers
in ``cover_generation``/``publish_staging``:

- The cover subject DEFAULTS to the live-frame character redraw.  An emote may
  REPLACE it only on a strong, articulated reason (the clip's reaction matches
  a sticker so well that the sticker is clearly more expressive), decided by
  the art-direction judge and never by the deterministic baseline.
- ``companion`` mode (emote AND character in one frame) is reserved for
  分身/复数小李 memes or drawing a panda-class sticker to depict kmx —
  kmx (读 kimo熊) is the NAME OF HER FANS, not a mascot; the sticker stands
  in for the audience instead of drawing generic crowd people.
- Song covers never use emotes.
- Sticker redraws must stay light: same pose/expression/design, only polished
  and integrated; like the character redraw, the sticker owns most of the frame.

Everything in this module fails OPEN back to the default character redraw (a
missing/mismatched sticker downgrades the ART DIRECTION only); the cover IMAGE
itself stays fail-closed in the staging pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from src.autoslice.channel_profile import ChannelProfileError, load_channel_profile
from src.autoslice.surface_canon import CHANNEL_PROFILE


ROOT = Path(__file__).resolve().parents[2]

EMOTE_MEDIA_ROOT_ENV = "AUTOSLICE_EMOTE_DIR"
_EMOTE_MEDIA_REPO_DEFAULT = Path("assets") / CHANNEL_PROFILE.profile_id / "emote"
_EMOTE_SCHEMA_PREFIX = "lidousha-emote-library"
_EMOTE_MODES = ("replace", "companion")
# A strong reason must be articulated, not a bare "好看"; length is a cheap
# proxy that rejects empty/one-word justifications without judging content.
_EMOTE_MIN_REASON_CHARS = 6
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class EmoteEntry:
    """One official sticker: identity, media pointers, and selection semantics."""

    id: str
    label: str
    subject: str          # "lidousha" (chibi 小李) | "panda_creature" (兽形本体)
    file: str             # small original sticker, relative to the media root
    hd_file: str          # AI-upscaled redraw used as the CPA reference image
    hd_sha256: str
    baked_text: str       # caption baked into the sticker art ("" = none)
    description: str      # what the sticker shows (pose/expression/props)
    use_when: str         # strong-match scenarios for the selection judge


@dataclass(frozen=True)
class EmoteLibrary:
    enabled: bool
    entries: tuple[EmoteEntry, ...]
    # Manifest-declared media roots probed AFTER the env override and BEFORE
    # the in-repo default (the free runner's tree-out media lives here, same
    # pattern as the branding intro's runtime_media_paths — deploying the
    # manifest is enough, no service-env injection).
    runtime_roots: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.enabled and bool(self.entries)

    def get(self, emote_id: str) -> EmoteEntry | None:
        for entry in self.entries:
            if entry.id == emote_id:
                return entry
        return None

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(entry.id for entry in self.entries)


EMPTY_EMOTE_LIBRARY = EmoteLibrary(enabled=False, entries=())


def _safe_media_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value.strip())
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return str(relative)


def load_emote_library(repo_root: Path | None = None) -> EmoteLibrary:
    """Parse the selected profile's emote manifest.  Fail-OPEN: any missing,
    malformed, or unsafe manifest yields the empty library (⇒ the cover always
    uses the default character redraw)."""

    root = repo_root or ROOT
    try:
        profile = load_channel_profile(root)
        manifest_path = profile.asset_file("emote_library", repo_root=root)
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ChannelProfileError, OSError, json.JSONDecodeError, ValueError):
        return EMPTY_EMOTE_LIBRARY
    if not isinstance(payload, dict):
        return EMPTY_EMOTE_LIBRARY
    schema = payload.get("schema_version")
    if not (isinstance(schema, str) and schema.startswith(_EMOTE_SCHEMA_PREFIX)):
        return EMPTY_EMOTE_LIBRARY
    raw_entries = payload.get("emotes")
    if not isinstance(raw_entries, list):
        return EMPTY_EMOTE_LIBRARY
    entries: list[EmoteEntry] = []
    seen_ids: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            return EMPTY_EMOTE_LIBRARY
        emote_id = raw.get("id")
        label = raw.get("label")
        hd_sha256 = raw.get("hd_sha256")
        file_rel = _safe_media_relative(raw.get("file"))
        hd_rel = _safe_media_relative(raw.get("hd_file"))
        if not (
            isinstance(emote_id, str)
            and emote_id.strip()
            and emote_id not in seen_ids
            and isinstance(label, str)
            and label.strip()
            and file_rel
            and hd_rel
            and isinstance(hd_sha256, str)
            and _SHA256_RE.fullmatch(hd_sha256)
        ):
            return EMPTY_EMOTE_LIBRARY
        seen_ids.add(emote_id)
        subject = raw.get("subject")
        entries.append(
            EmoteEntry(
                id=emote_id.strip(),
                label=label.strip(),
                subject=subject if subject in ("lidousha", "panda_creature") else "lidousha",
                file=file_rel,
                hd_file=hd_rel,
                hd_sha256=hd_sha256,
                baked_text=str(raw.get("baked_text") or "").strip(),
                description=str(raw.get("description") or "").strip(),
                use_when=str(raw.get("use_when") or "").strip(),
            )
        )
    media_root = payload.get("media_root")
    runtime_roots: tuple[str, ...] = ()
    if isinstance(media_root, dict):
        raw_roots = media_root.get("runtime_roots")
        if isinstance(raw_roots, list):
            runtime_roots = tuple(
                item.strip() for item in raw_roots if isinstance(item, str) and item.strip()
            )
    return EmoteLibrary(
        enabled=bool(payload.get("enabled", True)),
        entries=tuple(entries),
        runtime_roots=runtime_roots,
    )


def emote_media_roots(
    repo_root: Path | None = None, *, runtime_roots: tuple[str, ...] = ()
) -> tuple[Path, ...]:
    """Candidate media roots in probe order: explicit env override, then the
    manifest-declared runtime roots (free's tree-out install), then the in-repo
    default used on dev machines."""

    root = repo_root or ROOT
    candidates: list[Path] = []
    env_value = os.environ.get(EMOTE_MEDIA_ROOT_ENV, "").strip()
    if env_value:
        candidates.append(Path(env_value))
    for raw in runtime_roots:
        declared = Path(raw)
        candidates.append(declared if declared.is_absolute() else root / declared)
    candidates.append(root / _EMOTE_MEDIA_REPO_DEFAULT)
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_emote_reference(
    entry: EmoteEntry,
    *,
    repo_root: Path | None = None,
    runtime_roots: tuple[str, ...] = (),
) -> tuple[Path | None, str]:
    """Locate and integrity-check the sticker's HD reference image.

    Probes every candidate media root in order and returns ``(path, "")`` for
    the first whose on-disk bytes match the committed manifest sha256, else
    ``(None, all attempts)`` — callers must then fall back to the default
    character redraw (never ship a sticker whose bytes drifted)."""

    attempts: list[str] = []
    for media_root in emote_media_roots(repo_root, runtime_roots=runtime_roots):
        candidate = media_root / entry.hd_file
        if not candidate.is_file():
            attempts.append(f"EMOTE_MEDIA_MISSING: {candidate}")
            continue
        try:
            actual = _sha256_file(candidate)
        except OSError as exc:
            attempts.append(f"EMOTE_MEDIA_UNREADABLE: {candidate}: {type(exc).__name__}: {exc}")
            continue
        if actual != entry.hd_sha256:
            attempts.append(
                f"EMOTE_MEDIA_SHA_MISMATCH: {candidate} sha256:{actual} != manifest sha256:{entry.hd_sha256}"
            )
            continue
        return candidate, ""
    return None, "; ".join(attempts)


def emote_catalog_prompt_block(library: EmoteLibrary) -> str:
    """The art-direction judge's sticker catalog: one line per emote so the
    judge can cite an id, plus the strong-reason policy it must obey."""

    if not library:
        return ""
    lines = [
        "\n表情包(可选,默认不用):下面是官方表情包清单。**默认必须用人物重绘**(以参考帧当场形象为原型);"
        "只有当这条切片里她的反应与某个表情包高度贴合、用它明显比常规人物重绘更传神时,才允许选表情包;拿不准一律 null。"
        "选了表情包(mode=\"replace\")封面主体就是该表情包,与人物重绘互斥。"
        f"mode=\"companion\"(表情包与人物同框)只允许两类强理由:{CHANNEL_PROFILE.cover_identity.emote_companion_lore_zh};其余一律 \"replace\"。"
        "歌切封面永不使用表情包。"
    ]
    for entry in library.entries:
        parts = [f"- {entry.id} {entry.label}: {entry.description}"]
        if entry.use_when:
            parts.append(f"(适用: {entry.use_when})")
        if entry.subject == "panda_creature":
            parts.append("(兽形熊猫本体,非人形小李)")
        lines.append(" ".join(parts))
    lines.append(
        '输出 JSON 里额外加一个字段 "emote": null 或 {"id":"清单里的id","mode":"replace"|"companion","reason":"一句话说明为什么强贴合"}。'
    )
    return "\n".join(lines) + "\n"


def normalize_emote_choice(
    payload_value: object, *, library: EmoteLibrary, is_song: bool
) -> tuple[str, str, str]:
    """Validate the judge's ``emote`` field into ``(id, mode, reason)``.

    Anything short of a fully-formed strong-reason pick — unknown id, bad mode,
    unarticulated reason, song cover, disabled library — normalizes to the
    empty choice (default character redraw)."""

    if not library or is_song or not isinstance(payload_value, dict):
        return "", "", ""
    emote_id = payload_value.get("id")
    mode = payload_value.get("mode")
    reason = payload_value.get("reason")
    if not (isinstance(emote_id, str) and library.get(emote_id.strip()) is not None):
        return "", "", ""
    if mode not in _EMOTE_MODES:
        return "", "", ""
    if not (isinstance(reason, str) and len(reason.strip()) >= _EMOTE_MIN_REASON_CHARS):
        return "", "", ""
    return emote_id.strip(), mode, reason.strip()


def compose_companion_reference(
    frame_path: Path, emote_path: Path, output_path: Path
) -> Path:
    """Build the companion-mode reference image: the live frame with the sticker
    pasted as a bottom-right INSET (the single-image CPA edit endpoint gets both
    identities in one reference; the prompt tells the model the inset's role)."""

    from PIL import Image, ImageOps

    with Image.open(frame_path) as frame_img:
        frame = frame_img.convert("RGB")
        inset_height = max(1, int(round(frame.height * 0.30)))
        with Image.open(emote_path) as emote_img:
            emote = emote_img.convert("RGBA")
            scale = inset_height / emote.height
            emote = emote.resize(
                (max(1, int(round(emote.width * scale))), inset_height), Image.LANCZOS
            )
        inset = ImageOps.expand(emote, border=6, fill=(255, 255, 255, 255))
        margin = max(8, frame.width // 96)
        position = (
            frame.width - inset.width - margin,
            frame.height - inset.height - margin,
        )
        frame.paste(inset, position, inset)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        frame.save(output_path)
    return output_path
