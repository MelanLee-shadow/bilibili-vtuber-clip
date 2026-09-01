from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from src.autoslice import agy_gemini_client


VISUAL_SONG_SCHEMA_VERSION = "visual-song-inventory.v1"
VISUAL_SONG_PROMPT_VERSION = "lidousha-numbered-song-list.v1"
DEFAULT_MODEL = "Gemini 3.6 Flash (High)"
_RESULT_KEYS = {"song_title", "start_ms", "end_ms", "evidence", "confidence"}


@dataclass(frozen=True)
class VisualSongConfig:
    """Stable inputs that own the cache identity and frame sampling policy."""

    # Ten seconds keeps at least ~18 observations over a typical three-minute
    # song while remaining cheap because only the fixed title-list ROI is kept.
    sample_every_seconds: int = 10
    roi_x: float = 0.72
    roi_y: float = 0.06
    roi_width: float = 0.28
    roi_height: float = 0.58
    tile_width: int = 520
    sheet_columns: int = 3
    sheet_rows: int = 3
    model: str = DEFAULT_MODEL
    prompt_version: str = VISUAL_SONG_PROMPT_VERSION
    timeout_seconds: int = 900

    def validate(self) -> None:
        if self.sample_every_seconds < 5:
            raise ValueError("sample_every_seconds must be at least 5")
        if not (0 <= self.roi_x < 1 and 0 <= self.roi_y < 1):
            raise ValueError("ROI origin must be normalized")
        if not (0 < self.roi_width <= 1 and 0 < self.roi_height <= 1):
            raise ValueError("ROI size must be normalized")
        if self.roi_x + self.roi_width > 1.000001 or self.roi_y + self.roi_height > 1.000001:
            raise ValueError("ROI exceeds the source frame")
        if min(self.tile_width, self.sheet_columns, self.sheet_rows, self.timeout_seconds) <= 0:
            raise ValueError("contact-sheet dimensions and timeout must be positive")

    def cache_payload(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class VisualSongCandidate:
    song_title: str
    start_ms: int
    end_ms: int
    evidence: Mapping[str, object]
    confidence: float

    @property
    def list_index(self) -> int | None:
        value = self.evidence.get("list_index")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return None

    def to_manifest(self) -> dict[str, object]:
        return {
            "song_title": self.song_title,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "evidence": dict(self.evidence),
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class VisualSongDiscoveryResult:
    status: str  # READY | FAILED
    candidates: tuple[VisualSongCandidate, ...]
    cache_path: str | None
    content_fingerprint: str | None
    config_sha256: str
    cache_hit: bool = False
    error: str | None = None
    # Provider diagnostics are deliberately structured and bounded.  They are
    # useful for the state writer (AGY absent/quota/error versus Gemini key
    # ladder exhaustion) but must never contain a key value or raw provider
    # response/error text.
    provider_diagnostics: Mapping[str, object] | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": VISUAL_SONG_SCHEMA_VERSION,
            "status": self.status,
            "candidates": [candidate.to_manifest() for candidate in self.candidates],
            "cache_path": self.cache_path,
            "content_fingerprint": self.content_fingerprint,
            "config_sha256": self.config_sha256,
            "cache_hit": self.cache_hit,
            "error": self.error,
            "provider_diagnostics": (
                dict(self.provider_diagnostics)
                if self.provider_diagnostics is not None
                else None
            ),
        }


CommandRunner = Callable[..., subprocess.CompletedProcess]


class VisualSongProviderError(RuntimeError):
    """A provider failure with only safe, machine-readable context."""

    def __init__(
        self,
        category: str,
        *,
        returncode: int | None = None,
        error_type: str | None = None,
    ) -> None:
        self.category = str(category)[:96]
        self.returncode = returncode
        self.error_type = error_type
        super().__init__(self.category)


_MAX_PROVIDER_FAILURE_ROWS = 32


def _bounded_category(value: object, *, fallback: str = "UNKNOWN") -> str:
    """Keep diagnostics to controlled, secret-free category strings."""

    text = str(value or "").strip()
    if not text or len(text) > 96 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", text):
        return fallback
    return text


def _bounded_error_detail(exc: BaseException) -> str:
    """Retain useful local validation detail without persisting raw secrets."""

    detail = str(exc).strip()
    # Provider exceptions are always represented by their structured category;
    # arbitrary exception text can contain command output or credentials.
    if isinstance(exc, VisualSongProviderError):
        return _bounded_category(exc.category, fallback="VISUAL_PROVIDER_FAILED")
    if re.search(r"(?i)(api[_ -]?key|secret|token|password|credential)", detail):
        return type(exc).__name__
    detail = re.sub(r"\s+", " ", detail)
    if len(detail) > 180:
        detail = detail[:177] + "..."
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _failure_row(
    *,
    provider: str,
    category: str,
    count: int = 1,
    error_type: str | None = None,
    returncode: int | None = None,
    key_tier: str | None = None,
    key_ordinal: int | None = None,
    attempt_round: int | None = None,
) -> dict[str, object]:
    """Build one bounded provider diagnostic row (never raw exception text)."""

    row: dict[str, object] = {
        "provider": provider,
        "category": _bounded_category(category),
        "count": max(1, min(int(count), 99)),
    }
    if error_type:
        row["error_type"] = _bounded_category(error_type, fallback="ProviderError")
    if returncode is not None and isinstance(returncode, int) and not isinstance(returncode, bool):
        row["returncode"] = returncode
    if key_tier:
        row["key_tier"] = _bounded_category(key_tier)
    if key_ordinal is not None and isinstance(key_ordinal, int) and not isinstance(key_ordinal, bool):
        row["key_ordinal"] = max(0, min(key_ordinal, 99))
    if attempt_round is not None and isinstance(attempt_round, int) and not isinstance(attempt_round, bool):
        row["attempt_round"] = max(0, min(attempt_round, 99))
    return row


def _summarize_provider_failures(
    *,
    agy_rows: Sequence[Mapping[str, object]],
    gemini_rows: Sequence[Mapping[str, object]],
    configured_key_count: int = 0,
    gemini_attempt_count: int = 0,
    paid_gate_reason: str | None = None,
) -> dict[str, object]:
    """Return a compact failure/count/gate receipt for state and operators."""

    def safe_row(row: Mapping[str, object]) -> dict[str, object]:
        """Copy only the fixed, non-secret fields emitted by ``_failure_row``."""

        safe = _failure_row(
            provider=_bounded_category(row.get("provider"), fallback="unknown"),
            category=_bounded_category(row.get("category")),
            count=row.get("count", 1)
            if isinstance(row.get("count", 1), int)
            and not isinstance(row.get("count", 1), bool)
            else 1,
        )
        error_type = row.get("error_type")
        if error_type:
            safe["error_type"] = _bounded_category(
                error_type, fallback="ProviderError"
            )
        returncode = row.get("returncode")
        if isinstance(returncode, int) and not isinstance(returncode, bool):
            safe["returncode"] = max(-9999, min(returncode, 9999))
        key_tier = row.get("key_tier")
        if key_tier:
            safe["key_tier"] = _bounded_category(key_tier)
        for field in ("key_ordinal", "attempt_round"):
            value = row.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                safe[field] = max(0, min(value, 99))
        return safe

    def failure_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        if len(rows) <= _MAX_PROVIDER_FAILURE_ROWS:
            selected = rows
        else:
            # Keep the bounded prefix and the terminal attempt, which is the
            # most useful view when a provider ladder unexpectedly loops.
            selected = (*rows[: _MAX_PROVIDER_FAILURE_ROWS - 1], rows[-1])
        return [safe_row(row) for row in selected]

    def categories(rows: Sequence[Mapping[str, object]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            category = _bounded_category(row.get("category"))
            counts[category] = min(99, counts.get(category, 0) + 1)
        return dict(sorted(counts.items()))

    agy = {
        "attempt_count": min(99, len(agy_rows)),
        "failure_categories": categories(agy_rows),
        "failures": failure_rows(agy_rows),
    }
    if agy_rows:
        agy["last"] = safe_row(agy_rows[-1])
    gemini: dict[str, object] = {
        "attempt_count": min(99, max(gemini_attempt_count, len(gemini_rows))),
        "configured_key_count": max(0, min(int(configured_key_count), 3)),
        "failure_categories": categories(gemini_rows),
        "failures": failure_rows(gemini_rows),
    }
    if gemini_rows:
        gemini["last"] = safe_row(gemini_rows[-1])
    if paid_gate_reason:
        gemini["paid_gate_reason"] = _bounded_category(paid_gate_reason)
    return {
        "agy": agy,
        "gemini": gemini,
        "failure_count": min(99, len(agy_rows) + len(gemini_rows)),
    }


def _provider_failure_error(diagnostics: Mapping[str, object]) -> str:
    """Render only bounded categories/counts/gate reason into state ``error``."""

    agy = diagnostics.get("agy") if isinstance(diagnostics.get("agy"), Mapping) else {}
    gemini = diagnostics.get("gemini") if isinstance(diagnostics.get("gemini"), Mapping) else {}
    agy_categories = agy.get("failure_categories") if isinstance(agy, Mapping) else {}
    gemini_categories = gemini.get("failure_categories") if isinstance(gemini, Mapping) else {}
    agy_category = next(iter(agy_categories), "AGY_UNKNOWN") if isinstance(agy_categories, Mapping) else "AGY_UNKNOWN"
    gemini_category = next(iter(gemini_categories), "GEMINI_UNKNOWN") if isinstance(gemini_categories, Mapping) else "GEMINI_UNKNOWN"
    attempts = gemini.get("attempt_count", 0) if isinstance(gemini, Mapping) else 0
    configured = gemini.get("configured_key_count", 0) if isinstance(gemini, Mapping) else 0
    gate = gemini.get("paid_gate_reason", "NOT_RECORDED") if isinstance(gemini, Mapping) else "NOT_RECORDED"
    return (
        "GEMINI_VISUAL_SONG_DISCOVERY_FAILED: "
        f"agy={_bounded_category(agy_category)}; "
        f"gemini={_bounded_category(gemini_category)}; "
        f"gemini_attempts={max(0, min(int(attempts), 99))}; "
        f"configured_keys={max(0, min(int(configured), 3))}; "
        f"paid_gate={_bounded_category(gate)}"
    )[:512]


def _canonical_json_sha256(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def quick_media_fingerprint(path: Path, *, sample_bytes: int = 1024 * 1024) -> str:
    """Hash size plus bounded head/middle/tail samples instead of a multi-GB file."""

    metadata = path.stat()
    if not path.is_file() or metadata.st_size <= 0:
        raise ValueError(f"media is not a non-empty regular file: {path}")
    digest = hashlib.sha256()
    digest.update(f"size={metadata.st_size}\n".encode("ascii"))
    offsets = (0, max(0, metadata.st_size // 2 - sample_bytes // 2), max(0, metadata.st_size - sample_bytes))
    with path.open("rb") as source:
        for offset in dict.fromkeys(offsets):
            source.seek(offset)
            chunk = source.read(min(sample_bytes, metadata.st_size - offset))
            digest.update(f"offset={offset};bytes={len(chunk)}\n".encode("ascii"))
            digest.update(chunk)
    return digest.hexdigest()


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def parse_visual_song_response(text: str, *, duration_ms: int) -> tuple[VisualSongCandidate, ...]:
    """Validate AGY output without accepting prose, extra fields, or loose times."""

    payload = json.loads(_strip_json_fence(text))
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "songs"}:
        raise ValueError("response must contain exactly schema_version and songs")
    if payload.get("schema_version") != VISUAL_SONG_SCHEMA_VERSION:
        raise ValueError("unexpected visual-song schema_version")
    songs = payload.get("songs")
    if not isinstance(songs, list):
        raise ValueError("songs must be a JSON array")

    parsed: list[VisualSongCandidate] = []
    identities: set[tuple[int | None, str, int, int]] = set()
    for position, item in enumerate(songs):
        if not isinstance(item, dict) or set(item) != _RESULT_KEYS:
            raise ValueError(f"songs[{position}] must contain exactly {sorted(_RESULT_KEYS)}")
        title = item.get("song_title")
        start_ms = item.get("start_ms")
        end_ms = item.get("end_ms")
        evidence = item.get("evidence")
        confidence = item.get("confidence")
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ValueError(f"songs[{position}].song_title is invalid")
        if (
            isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or not 0 <= start_ms < end_ms <= duration_ms
        ):
            raise ValueError(f"songs[{position}] has an invalid interval")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError(f"songs[{position}].evidence must be a non-empty object")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError(f"songs[{position}].confidence must be in [0,1]")
        list_index = evidence.get("list_index")
        if list_index is not None and (
            isinstance(list_index, bool) or not isinstance(list_index, int) or not 1 <= list_index <= 99
        ):
            raise ValueError(f"songs[{position}].evidence.list_index is invalid")
        normalized_title = "".join(title.lower().split())
        identity = (list_index, normalized_title, start_ms, end_ms)
        if identity in identities:
            continue
        identities.add(identity)
        parsed.append(
            VisualSongCandidate(
                song_title=title.strip(),
                start_ms=start_ms,
                end_ms=end_ms,
                evidence=evidence,
                confidence=round(float(confidence), 4),
            )
        )
    return tuple(sorted(parsed, key=lambda item: (item.start_ms, item.end_ms, item.song_title)))


def _timestamp_label(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}  ({milliseconds} ms)"


def build_contact_sheets(
    media_path: Path,
    output_dir: Path,
    *,
    duration_ms: int,
    config: VisualSongConfig,
    command_runner: CommandRunner = subprocess.run,
) -> tuple[Path, ...]:
    """Extract the fixed right-side ROI once, then label and tile sampled frames."""

    config.validate()
    output_dir.mkdir(parents=True, exist_ok=True)
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    crop = (
        f"crop=w='trunc(iw*{config.roi_width}/2)*2':h='trunc(ih*{config.roi_height}/2)*2':"
        f"x='trunc(iw*{config.roi_x}/2)*2':y='trunc(ih*{config.roi_y}/2)*2',"
        f"fps=1/{config.sample_every_seconds},scale={config.tile_width}:-2"
    )
    completed = command_runner(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(media_path),
            "-vf",
            crop,
            str(frames_dir / "frame_%05d.png"),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=max(300, min(1800, duration_ms // 1000 + 300)),
    )
    frames = sorted(frames_dir.glob("frame_*.png"))
    if completed.returncode != 0 or not frames:
        # ffmpeg stderr can contain paths or provider-adjacent command output;
        # the optional lane records only this bounded category.
        raise VisualSongProviderError(
            "VISUAL_CONTACT_SHEET_EXTRACTION_FAILED",
            returncode=completed.returncode,
        )

    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - deployed preflight owns Pillow
        raise RuntimeError("Pillow is required for timestamped contact sheets") from exc

    label_height = 34
    per_sheet = config.sheet_columns * config.sheet_rows
    sheets: list[Path] = []
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:  # Pillow < 10 compatibility
        font = ImageFont.load_default()
    for page, first in enumerate(range(0, len(frames), per_sheet), start=1):
        page_frames = frames[first : first + per_sheet]
        with Image.open(page_frames[0]) as probe:
            tile_height = probe.height + label_height
        sheet = Image.new(
            "RGB",
            (config.tile_width * config.sheet_columns, tile_height * config.sheet_rows),
            (18, 36, 79),
        )
        draw = ImageDraw.Draw(sheet)
        for slot, frame_path in enumerate(page_frames):
            with Image.open(frame_path) as frame:
                frame_rgb = frame.convert("RGB")
                x = slot % config.sheet_columns * config.tile_width
                y = slot // config.sheet_columns * tile_height
                sheet.paste(frame_rgb, (x, y + label_height))
            timestamp_ms = min(duration_ms, (first + slot) * config.sample_every_seconds * 1000)
            draw.rectangle((x, y, x + config.tile_width, y + label_height), fill=(18, 36, 79))
            draw.text((x + 8, y + 6), _timestamp_label(timestamp_ms), fill=(255, 246, 214), font=font)
        path = output_dir / f"contact_{page:03d}.jpg"
        sheet.save(path, format="JPEG", quality=92, subsampling=0)
        sheets.append(path)
    return tuple(sheets)


def _prompt(sheet_names: Sequence[str], duration_ms: int) -> str:
    names = "\n".join(f"- {name}" for name in sheet_names)
    return f"""Inspect every timestamped contact sheet in this directory:
{names}

They are low-frequency samples of the fixed upper-right region of one Li Dousha
livestream segment ({duration_ms} ms). The overlay header is usually 要唱的歌 and
contains a cumulative numbered song list. Chinese, Japanese and Latin titles
must be copied exactly from the pixels. The little panda cursor and newly added
bottom row can identify the current/new song. Avatar hair may occlude a row in
one frame, so compare adjacent samples.

Find songs that are newly active or newly appended during THIS media segment.
Do not emit unchanged historical rows merely because they remain visible in all
samples. start_ms/end_ms should cover the best visual candidate PERFORMANCE
interval: use the neighboring timestamps where the panda/current-row state or
newest-row state changes, continuing until the next song transition when it is
visible. Do not shrink a multi-minute stable current-row run to one sampled
frame. If only one transition is visible, bound the search interval with the
adjacent sampled states and say why it is uncertain in evidence.reason. The
interval must be non-empty and inside 0..{duration_ms}. This is discovery/title
evidence only, not proof that the song was performed.

Allowed: view_file on prompt.md and the listed contact_*.jpg files; write_to_file
to relative visual_songs.json; view_file on visual_songs.json only after writing.
Forbidden: shell, terminal, browser, web, audio guessing, or any other path.

Write visual_songs.json as exactly:
{{"schema_version":"{VISUAL_SONG_SCHEMA_VERSION}","songs":[
  {{"song_title":"exact pixels","start_ms":0,"end_ms":30000,
    "evidence":{{"list_index":1,"frames":["contact_001.jpg @ 00:00:00"],"reason":"brief visual reason"}},
    "confidence":0.95}}
]}}
Each song object must have exactly those five keys. evidence must be a non-empty
object; list_index may be null only if no numbered row is readable. JSON only,
no markdown. An empty songs array is valid.
"""


def _run_agy_once(
    job_dir: Path,
    *,
    config: VisualSongConfig,
    agy_bin: Path,
    command_runner: CommandRunner,
) -> None:
    short_prompt = (
        f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
        f"Use only {job_dir}/prompt.md, {job_dir}/contact_*.jpg, and {job_dir}/visual_songs.json. "
        "Do not inspect any other file or use shell/terminal."
    )
    try:
        completed = command_runner(
            [
                str(agy_bin),
                "--sandbox",
                "--dangerously-skip-permissions",
                "--add-dir",
                str(job_dir),
                "--model",
                config.model,
                "-p",
                short_prompt,
                "--print-timeout",
                f"{max(1, config.timeout_seconds // 60)}m",
            ],
            cwd=str(job_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
        raise VisualSongProviderError(
            agy_gemini_client.classify_agy_launch_error(exc),
            error_type=type(exc).__name__,
        ) from exc
    if completed.returncode != 0:
        raise VisualSongProviderError(
            agy_gemini_client.classify_agy_failure(
                completed.returncode,
                completed.stdout or "",
                completed.stderr or "",
            ),
            returncode=completed.returncode,
        )


def _run_gemini_vision_once(
    job_dir: Path,
    sheets: Sequence[Path],
    *,
    duration_ms: int,
    provider_failures: list[dict[str, object]],
    provider_metadata: dict[str, object],
) -> None:
    """Same contact sheets, same answer file — Gemini instead of local AGY.

    这条 lane 此前**零兜底**，AGY 不在的机器上视觉召回整条死掉
    （只是 fail-open 成"没有候选"，看起来像这场没有歌）。现在缺席即降级。
    """

    prompt = (
        job_dir / "prompt.md"
    ).read_text(encoding="utf-8") + (
        "\nThe contact sheets are attached to this request in the listed order; "
        "there is no local filesystem. Reply with the visual_songs.json object "
        "itself, JSON only.\n"
    )
    model = agy_gemini_client.gemini_vision_model()
    parts = [(path.read_bytes(), "image/jpeg") for path in sheets]
    item_key = hashlib.sha256(
        b"".join(payload for payload, _mime in parts)
    ).hexdigest()
    configured_keys = agy_gemini_client.free_api_keys()
    provider_metadata["configured_key_count"] = len(configured_keys)
    if not configured_keys:
        provider_failures.append(
            _failure_row(
                provider="gemini_api",
                category="GEMINI_API_NO_CONFIGURED_KEY",
            )
        )

    def observe(key: str) -> str:
        raw = agy_gemini_client.generate_content(
            prompt=prompt,
            key=key,
            model=model,
            inline_parts=parts,
            timeout_seconds=300,
        ).strip()
        if not raw:
            raise ValueError("empty Gemini visual-song response")
        # Parse before accepting so a malformed answer rolls to the next key.
        parse_visual_song_response(raw, duration_ms=duration_ms)
        return raw

    def record(failure: agy_gemini_client.GeminiAttemptFailure) -> None:
        provider_failures.append(
            _failure_row(
                provider="gemini_api",
                category=failure.category,
                error_type=failure.error_type,
                key_tier=failure.key_tier,
                key_ordinal=failure.key_ordinal,
                attempt_round=failure.attempt_round,
            )
        )

    def record_paid_skipped(
        key_ordinal: int, attempt_round: int, gate_reason: str
    ) -> None:
        provider_failures.append(
            _failure_row(
                provider="gemini_api",
                category=f"PAID_BACKUP_SKIPPED:{_bounded_category(gate_reason)}",
                key_tier=agy_gemini_client.gemini_backup_policy.PAID_KEY_TIER,
                key_ordinal=key_ordinal,
                attempt_round=attempt_round,
            )
        )

    try:
        outcome = agy_gemini_client.run_gemini_key_ladder(
            item_key=item_key,
            observe=observe,
            purpose="visual_song_discovery",
            record_failure=record,
            record_paid_skipped=record_paid_skipped,
            # A missing paid key is still useful state information for this
            # fail-open lane; it is represented only as a bounded gate category.
            silent_when_paid_unconfigured=False,
        )
    except Exception as exc:
        provider_failures.append(
            _failure_row(
                provider="gemini_api",
                category=agy_gemini_client.classify_gemini_failure(exc),
                error_type=type(exc).__name__,
            )
        )
        provider_metadata["attempt_count"] = len(provider_failures)
        raise VisualSongProviderError(
            "GEMINI_VISUAL_SONG_DISCOVERY_FAILED",
            error_type=type(exc).__name__,
        ) from exc
    provider_metadata["configured_key_count"] = outcome.configured_key_count
    provider_metadata["attempt_count"] = len(provider_failures)
    provider_metadata["paid_gate_reason"] = outcome.paid_gate_reason
    if not outcome.accepted:
        raise VisualSongProviderError("GEMINI_VISUAL_SONG_DISCOVERY_FAILED")
    (job_dir / "visual_songs.json").write_text(outcome.observed, encoding="utf-8")


def discover_visual_songs(
    media_path: Path,
    cache_dir: Path,
    *,
    duration_ms: int,
    config: VisualSongConfig | None = None,
    agy_bin: Path | None = None,
    command_runner: CommandRunner = subprocess.run,
) -> VisualSongDiscoveryResult:
    """Fail-isolated visual inventory: cache hit or one bounded AGY High call."""

    selected = config or VisualSongConfig()
    config_sha = _canonical_json_sha256(selected.cache_payload())
    agy_failures: list[dict[str, object]] = []
    gemini_failures: list[dict[str, object]] = []
    gemini_metadata: dict[str, object] = {}
    provider_diagnostics: dict[str, object] | None = None
    try:
        selected.validate()
        if duration_ms <= 0:
            raise ValueError("duration_ms must be positive")
        content_fingerprint = quick_media_fingerprint(media_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / f"{media_path.stem}.{content_fingerprint[:16]}.{config_sha[:12]}.json"
        if cache_path.is_file():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                cache_matches = (
                    isinstance(payload, dict)
                    and payload.get("schema_version") == VISUAL_SONG_SCHEMA_VERSION
                    and payload.get("content_fingerprint") == content_fingerprint
                    and payload.get("config_sha256") == config_sha
                )
                if cache_matches:
                    candidates = parse_visual_song_response(
                        json.dumps(
                            {"schema_version": VISUAL_SONG_SCHEMA_VERSION, "songs": payload.get("songs")},
                            ensure_ascii=False,
                        ),
                        duration_ms=duration_ms,
                    )
                    return VisualSongDiscoveryResult(
                        status="READY",
                        candidates=candidates,
                        cache_path=str(cache_path),
                        content_fingerprint=content_fingerprint,
                        config_sha256=config_sha,
                        cache_hit=True,
                    )
            except (OSError, ValueError, json.JSONDecodeError):
                # The content-addressed filename is still useful; overwrite a
                # damaged cache only after a new validated AGY result exists.
                pass

        # 路径解析统一走 agy_gemini_client（兼容旧名 AUTOSLICE_AGY_BIN，不再
        # 写死 /root/...）。解析不到 = 正常降级，不是错误。
        executable = Path(
            agy_gemini_client.resolve_local_agy_binary(
                agy_bin, env_names=("AUTOSLICE_AGY_BIN", "AGY_BIN")
            )
        )
        agy_present = agy_gemini_client.local_agy_available(
            agy_bin, env_names=("AUTOSLICE_AGY_BIN", "AGY_BIN")
        )
        if not agy_present:
            agy_failures.append(
                _failure_row(
                    provider="agy",
                    category=agy_gemini_client.AGY_BINARY_ABSENT,
                )
            )
        with tempfile.TemporaryDirectory(prefix="visual_song_") as tmp:
            job_dir = Path(tmp)
            sheets = build_contact_sheets(
                media_path,
                job_dir,
                duration_ms=duration_ms,
                config=selected,
                command_runner=command_runner,
            )
            (job_dir / "prompt.md").write_text(
                _prompt([path.name for path in sheets], duration_ms), encoding="utf-8"
            )
            output = job_dir / "visual_songs.json"
            if agy_present:
                try:
                    _run_agy_once(
                        job_dir,
                        config=selected,
                        agy_bin=executable,
                        command_runner=command_runner,
                    )
                    if not output.is_file():
                        raise VisualSongProviderError("AGY_VISUAL_SONG_OUTPUT_MISSING")
                    candidates = parse_visual_song_response(
                        output.read_text(encoding="utf-8"), duration_ms=duration_ms
                    )
                except Exception as exc:
                    if isinstance(exc, VisualSongProviderError):
                        category = exc.category
                        returncode = exc.returncode
                        error_type = exc.error_type
                    elif isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
                        category = agy_gemini_client.AGY_TIMEOUT
                        returncode = None
                        error_type = type(exc).__name__
                    else:
                        category = "AGY_VISUAL_SONG_INVALID_OUTPUT"
                        returncode = None
                        error_type = type(exc).__name__
                    agy_failures.append(
                        _failure_row(
                            provider="agy",
                            category=category,
                            error_type=error_type,
                            returncode=returncode,
                        )
                    )
                    gemini_metadata["fallback_used"] = True
                    _run_gemini_vision_once(
                        job_dir,
                        sheets,
                        duration_ms=duration_ms,
                        provider_failures=gemini_failures,
                        provider_metadata=gemini_metadata,
                    )
                    candidates = parse_visual_song_response(
                        output.read_text(encoding="utf-8"), duration_ms=duration_ms
                    )
            else:
                _run_gemini_vision_once(
                    job_dir,
                    sheets,
                    duration_ms=duration_ms,
                    provider_failures=gemini_failures,
                    provider_metadata=gemini_metadata,
                )
                if not output.is_file():
                    raise VisualSongProviderError("GEMINI_VISUAL_SONG_OUTPUT_MISSING")
                candidates = parse_visual_song_response(
                    output.read_text(encoding="utf-8"), duration_ms=duration_ms
                )

        provider_diagnostics = _summarize_provider_failures(
            agy_rows=agy_failures,
            gemini_rows=gemini_failures,
            configured_key_count=int(gemini_metadata.get("configured_key_count", 0) or 0),
            gemini_attempt_count=int(gemini_metadata.get("attempt_count", 0) or 0),
            paid_gate_reason=(
                str(gemini_metadata["paid_gate_reason"])
                if gemini_metadata.get("paid_gate_reason")
                else None
            ),
        )

        cache_payload = {
            "schema_version": VISUAL_SONG_SCHEMA_VERSION,
            "content_fingerprint": content_fingerprint,
            "config_sha256": config_sha,
            "media_size": media_path.stat().st_size,
            "songs": [candidate.to_manifest() for candidate in candidates],
        }
        tmp_cache = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp_cache.write_text(json.dumps(cache_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_cache, cache_path)
        return VisualSongDiscoveryResult(
            status="READY",
            candidates=candidates,
            cache_path=str(cache_path),
            content_fingerprint=content_fingerprint,
            config_sha256=config_sha,
            provider_diagnostics=provider_diagnostics,
        )
    # This lane is optional recall enrichment.  Any provider/decoder/parser
    # defect must fail open so the independent ASR lane still runs.
    except Exception as exc:
        if provider_diagnostics is None and (agy_failures or gemini_failures or gemini_metadata):
            provider_diagnostics = _summarize_provider_failures(
                agy_rows=agy_failures,
                gemini_rows=gemini_failures,
                configured_key_count=int(gemini_metadata.get("configured_key_count", 0) or 0),
                gemini_attempt_count=int(gemini_metadata.get("attempt_count", 0) or 0),
                paid_gate_reason=(
                    str(gemini_metadata["paid_gate_reason"])
                    if gemini_metadata.get("paid_gate_reason")
                    else None
                ),
            )
        error = (
            _provider_failure_error(provider_diagnostics)
            if provider_diagnostics and (agy_failures or gemini_failures)
            else _bounded_error_detail(exc)
        )
        return VisualSongDiscoveryResult(
            status="FAILED",
            candidates=(),
            cache_path=None,
            content_fingerprint=locals().get("content_fingerprint"),
            config_sha256=config_sha,
            error=error,
            provider_diagnostics=provider_diagnostics,
        )


def normalize_visual_title(title: str) -> str:
    return re.sub(r"[\s《》「」『』·・_\-]+", "", title).lower()


def union_visual_song_candidates(
    recalled: Sequence[dict[str, object]],
    visual: Sequence[VisualSongCandidate],
    *,
    segment_tag: str,
) -> list[dict[str, object]]:
    """Attach visual titles to overlapping ASR songs and append unmatched ones."""

    combined = [dict(item) for item in recalled]
    consumed: set[int] = set()
    for item in combined:
        start = item.get("anchor_start_ms")
        end = item.get("anchor_end_ms")
        if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
            continue
        best: tuple[float, int, VisualSongCandidate] | None = None
        for index, candidate in enumerate(visual):
            if index in consumed:
                continue
            overlap = max(0, min(end, candidate.end_ms) - max(start, candidate.start_ms))
            shorter = max(1, min(end - start, candidate.end_ms - candidate.start_ms))
            overlap_ratio = overlap / shorter
            score = overlap_ratio
            if score >= 0.35 and (best is None or score > best[0]):
                best = (score, index, candidate)
        if best is not None:
            _, index, candidate = best
            consumed.add(index)
            item["title_hint"] = candidate.song_title
            item["visual_song_evidence"] = candidate.to_manifest()
            item["visual_song_matched_to_asr"] = True

    for index, candidate in enumerate(visual):
        if index in consumed:
            continue
        title_hash = hashlib.sha256(candidate.song_title.encode("utf-8")).hexdigest()[:8]
        combined.append(
            {
                "cid": f"songvis_{segment_tag}_{candidate.start_ms // 1000}_{title_hash}",
                "anchor_start_ms": candidate.start_ms,
                "anchor_end_ms": candidate.end_ms,
                "title_hint": candidate.song_title,
                "hook": f"画面右上歌单识别到《{candidate.song_title}》",
                "preview": candidate.song_title,
                "confidence": candidate.confidence,
                "lane": "visual_song_inventory",
                "visual_song_evidence": candidate.to_manifest(),
                "visual_song_matched_to_asr": False,
            }
        )
    return combined
