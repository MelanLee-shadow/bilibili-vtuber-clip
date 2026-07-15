"""Validated per-channel configuration for the reusable autoslice pipeline.

Channel profiles collect identity, asset, delivery, and compatibility policy
that used to be spread through the production scripts.  Loading a profile does
not itself change behavior: the default ``lidousha`` profile deliberately
resolves to the historical paths and protocol tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


CHANNEL_PROFILE_SCHEMA_VERSION = "vtuber-slice.channel-profile.v1"
DEFAULT_CHANNEL_PROFILE_ID = "lidousha"
PROFILE_ENV = "AUTOSLICE_PROFILE"
PROFILE_MANIFEST_ENV = "AUTOSLICE_PROFILE_MANIFEST"

_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ROOM_ID_RE = re.compile(r"^[0-9]{1,32}$")
_PROTOCOL_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")

_REQUIRED_ASSET_FILES = frozenset(
    {
        "clip_opening_address",
        "entity_confusables",
        "glossary",
        "branding_intro_manifest",
        "known_songs",
        "persona",
        "slice_selection_metric",
        "subtitle_correction_principles",
        "timely_terms",
        "topic_entity_graph",
        "title_style",
        "voiceprint_profile",
    }
)
_REQUIRED_ASSET_DIRECTORIES = frozenset(
    {
        "fonts",
        "speaker_overrides",
        "subtitle_regressions",
        "subtitle_text_overrides",
    }
)
_REQUIRED_TOOLS = frozenset({"cover_regenerator"})


class ChannelProfileError(ValueError):
    """A profile is missing, malformed, unsafe, or internally inconsistent."""


@dataclass(frozen=True)
class ChannelProfile:
    profile_id: str
    display_name: str
    room_id: str
    output_directory: str
    host_speaker_label: str
    guest_speaker_label: str
    asset_root: Path
    asset_files: Mapping[str, Path]
    asset_directories: Mapping[str, Path]
    fingerprint_asset_keys: tuple[str, ...]
    fingerprint_directory_keys: tuple[str, ...]
    voiceprint_reference_subdirectory: str
    song_title_prefix: str
    song_hook_template: str
    song_plain_template: str
    decisions: Mapping[str, str]
    tools: Mapping[str, Path]
    manifest_path: Path
    manifest_sha256: str
    repo_root: Path

    @property
    def delivery_root(self) -> Path:
        return self.repo_root / self.output_directory

    def delivery_root_for(self, repo_root: Path) -> Path:
        """Rebase delivery under a test/eval repo while keeping profile policy."""

        return repo_root / self.output_directory

    def _rebase_repo_path(self, path: Path, repo_root: Path | None) -> Path:
        if repo_root is None:
            return path
        return repo_root / path.relative_to(self.repo_root)

    def asset_file(self, key: str, *, repo_root: Path | None = None) -> Path:
        try:
            path = self.asset_files[key]
        except KeyError as exc:
            raise ChannelProfileError(f"profile {self.profile_id!r} has no asset file {key!r}") from exc
        return self._rebase_repo_path(path, repo_root)

    def asset_directory(self, key: str, *, repo_root: Path | None = None) -> Path:
        try:
            path = self.asset_directories[key]
        except KeyError as exc:
            raise ChannelProfileError(
                f"profile {self.profile_id!r} has no asset directory {key!r}"
            ) from exc
        return self._rebase_repo_path(path, repo_root)

    def decision(self, key: str) -> str:
        try:
            return self.decisions[key]
        except KeyError as exc:
            raise ChannelProfileError(f"profile {self.profile_id!r} has no decision {key!r}") from exc

    def tool(self, key: str, *, repo_root: Path | None = None) -> Path:
        try:
            path = self.tools[key]
        except KeyError as exc:
            raise ChannelProfileError(f"profile {self.profile_id!r} has no tool {key!r}") from exc
        return self._rebase_repo_path(path, repo_root)

    def format_song_title(self, song_title: str, *, hook: str | None = None) -> str:
        if hook:
            return self.song_hook_template.format(song_title=song_title, hook=hook)
        return self.song_plain_template.format(song_title=song_title)

    def fingerprint_paths(self, *, repo_root: Path | None = None) -> tuple[Path, ...]:
        manifest = self.manifest_path
        if repo_root is not None:
            try:
                manifest = repo_root / manifest.relative_to(self.repo_root)
            except ValueError:
                pass
        paths: list[Path] = [manifest]
        paths.extend(
            self.asset_file(key, repo_root=repo_root) for key in self.fingerprint_asset_keys
        )
        for key in self.fingerprint_directory_keys:
            root = self.asset_directory(key, repo_root=repo_root)
            if root.is_dir():
                paths.extend(path for path in root.rglob("*") if path.is_file())
            else:
                paths.append(root)
        return tuple(paths)

    def missing_runtime_paths(self) -> tuple[Path, ...]:
        required = [*self.asset_files.values(), *self.tools.values()]
        required.extend(self.asset_directories.values())
        return tuple(path for path in required if not path.exists())


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ChannelProfileError(f"{label} must be an object")
    return value


def _strict_keys(
    value: Mapping[str, object],
    *,
    label: str,
    required: set[str] | frozenset[str],
) -> None:
    missing = sorted(set(required) - set(value))
    unknown = sorted(set(value) - set(required))
    if missing:
        raise ChannelProfileError(f"{label} missing required keys: {', '.join(missing)}")
    if unknown:
        raise ChannelProfileError(f"{label} has unknown keys: {', '.join(unknown)}")


def _string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChannelProfileError(f"{label} must be a non-empty string")
    return value.strip()


def _safe_component(value: object, *, label: str) -> str:
    text = _string(value, label=label)
    if not _SAFE_ID_RE.fullmatch(text):
        raise ChannelProfileError(f"{label} must match {_SAFE_ID_RE.pattern}")
    return text


def _safe_relative_path(value: object, *, label: str) -> Path:
    text = _string(value, label=label)
    path = Path(text)
    if path.is_absolute() or path == Path(".") or ".." in path.parts:
        raise ChannelProfileError(f"{label} must be a safe repository-relative path")
    return path


def _string_map(value: object, *, label: str) -> dict[str, str]:
    raw = _mapping(value, label=label)
    result: dict[str, str] = {}
    for key, item in raw.items():
        safe_key = _safe_component(key, label=f"{label} key")
        result[safe_key] = _string(item, label=f"{label}.{safe_key}")
    return result


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ChannelProfileError(f"{label} must be a non-empty list")
    result = tuple(_safe_component(item, label=f"{label} entry") for item in value)
    if len(result) != len(set(result)):
        raise ChannelProfileError(f"{label} must not contain duplicates")
    return result


def _resolve_repo_path(repo_root: Path, raw: str, *, label: str) -> Path:
    relative = _safe_relative_path(raw, label=label)
    resolved = (repo_root / relative).resolve()
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise ChannelProfileError(f"{label} escapes the repository root") from exc
    return resolved


def resolve_channel_profile_manifest(
    repo_root: Path,
    *,
    profile_id: str | None = None,
    manifest_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    repo_root = repo_root.resolve()
    env = os.environ if environ is None else environ
    explicit_manifest = manifest_path or (
        Path(env[PROFILE_MANIFEST_ENV]) if env.get(PROFILE_MANIFEST_ENV) else None
    )
    if explicit_manifest is not None:
        if not explicit_manifest.is_absolute():
            explicit_manifest = repo_root / explicit_manifest
        return explicit_manifest.resolve()
    selected = profile_id or env.get(PROFILE_ENV) or DEFAULT_CHANNEL_PROFILE_ID
    selected = _safe_component(selected, label="channel profile id")
    return (repo_root / "profiles" / selected / "profile.json").resolve()


def load_channel_profile(
    repo_root: Path,
    *,
    profile_id: str | None = None,
    manifest_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ChannelProfile:
    repo_root = repo_root.resolve()
    env = os.environ if environ is None else environ
    resolved_manifest = resolve_channel_profile_manifest(
        repo_root,
        profile_id=profile_id,
        manifest_path=manifest_path,
        environ=env,
    )
    try:
        manifest_bytes = resolved_manifest.read_bytes()
    except OSError as exc:
        raise ChannelProfileError(f"cannot read channel profile {resolved_manifest}: {exc}") from exc
    try:
        document = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ChannelProfileError(f"invalid channel profile JSON {resolved_manifest}: {exc}") from exc
    root = _mapping(document, label="channel profile")
    top_keys = {
        "schema_version",
        "profile_id",
        "identity",
        "assets",
        "runtime",
        "titles",
        "decisions",
        "tools",
    }
    _strict_keys(root, label="channel profile", required=top_keys)
    if root.get("schema_version") != CHANNEL_PROFILE_SCHEMA_VERSION:
        raise ChannelProfileError(
            f"unsupported channel profile schema {root.get('schema_version')!r}; "
            f"expected {CHANNEL_PROFILE_SCHEMA_VERSION!r}"
        )
    loaded_profile_id = _safe_component(root.get("profile_id"), label="profile_id")
    selected_profile_id = profile_id or env.get(PROFILE_ENV)
    if selected_profile_id and loaded_profile_id != selected_profile_id:
        raise ChannelProfileError(
            f"selected profile {selected_profile_id!r} loaded manifest for {loaded_profile_id!r}"
        )

    identity = _mapping(root.get("identity"), label="identity")
    _strict_keys(
        identity,
        label="identity",
        required={
            "display_name",
            "room_id",
            "output_directory",
            "host_speaker_label",
            "guest_speaker_label",
        },
    )
    room_id = _string(identity.get("room_id"), label="identity.room_id")
    if not _ROOM_ID_RE.fullmatch(room_id):
        raise ChannelProfileError("identity.room_id must contain only decimal digits")

    assets = _mapping(root.get("assets"), label="assets")
    _strict_keys(
        assets,
        label="assets",
        required={
            "root",
            "files",
            "directories",
            "pipeline_fingerprint_files",
            "pipeline_fingerprint_directories",
        },
    )
    asset_root = _resolve_repo_path(
        repo_root,
        _string(assets.get("root"), label="assets.root"),
        label="assets.root",
    )
    file_names = _string_map(assets.get("files"), label="assets.files")
    directory_names = _string_map(assets.get("directories"), label="assets.directories")
    missing_file_keys = sorted(_REQUIRED_ASSET_FILES - set(file_names))
    missing_directory_keys = sorted(_REQUIRED_ASSET_DIRECTORIES - set(directory_names))
    if missing_file_keys:
        raise ChannelProfileError(
            "assets.files missing pipeline keys: " + ", ".join(missing_file_keys)
        )
    if missing_directory_keys:
        raise ChannelProfileError(
            "assets.directories missing pipeline keys: " + ", ".join(missing_directory_keys)
        )
    asset_files = {
        key: (asset_root / _safe_relative_path(value, label=f"assets.files.{key}")).resolve()
        for key, value in file_names.items()
    }
    asset_directories = {
        key: (asset_root / _safe_relative_path(value, label=f"assets.directories.{key}")).resolve()
        for key, value in directory_names.items()
    }
    for label, paths in (
        ("assets.files", asset_files),
        ("assets.directories", asset_directories),
    ):
        for key, path in paths.items():
            try:
                path.relative_to(asset_root)
            except ValueError as exc:
                raise ChannelProfileError(f"{label}.{key} escapes assets.root") from exc
    fingerprint_asset_keys = _string_list(
        assets.get("pipeline_fingerprint_files"),
        label="assets.pipeline_fingerprint_files",
    )
    fingerprint_directory_keys = _string_list(
        assets.get("pipeline_fingerprint_directories"),
        label="assets.pipeline_fingerprint_directories",
    )
    unknown_fingerprint_files = sorted(set(fingerprint_asset_keys) - set(asset_files))
    unknown_fingerprint_directories = sorted(
        set(fingerprint_directory_keys) - set(asset_directories)
    )
    if unknown_fingerprint_files:
        raise ChannelProfileError(
            "pipeline_fingerprint_files references unknown assets: "
            + ", ".join(unknown_fingerprint_files)
        )
    if unknown_fingerprint_directories:
        raise ChannelProfileError(
            "pipeline_fingerprint_directories references unknown assets: "
            + ", ".join(unknown_fingerprint_directories)
        )

    runtime = _mapping(root.get("runtime"), label="runtime")
    _strict_keys(
        runtime,
        label="runtime",
        required={"voiceprint_reference_subdirectory"},
    )
    voiceprint_reference_subdirectory = _safe_component(
        runtime.get("voiceprint_reference_subdirectory"),
        label="runtime.voiceprint_reference_subdirectory",
    )

    titles = _mapping(root.get("titles"), label="titles")
    _strict_keys(
        titles,
        label="titles",
        required={"song_prefix", "song_hook_template", "song_plain_template"},
    )
    song_title_prefix = _string(titles.get("song_prefix"), label="titles.song_prefix")
    song_hook_template = _string(
        titles.get("song_hook_template"), label="titles.song_hook_template"
    )
    song_plain_template = _string(
        titles.get("song_plain_template"), label="titles.song_plain_template"
    )
    try:
        hook_probe = song_hook_template.format(song_title="SONG", hook="HOOK")
        plain_probe = song_plain_template.format(song_title="SONG")
    except (KeyError, ValueError) as exc:
        raise ChannelProfileError(f"invalid song title template: {exc}") from exc
    if "SONG" not in hook_probe or "HOOK" not in hook_probe or "SONG" not in plain_probe:
        raise ChannelProfileError("song title templates must preserve their declared fields")

    decisions = _string_map(root.get("decisions"), label="decisions")
    required_decisions = {
        "host_vocal_present",
        "host_vocal_absent",
        "verified_host_singing",
        "host_not_singing_reason",
        "lyric_vocal_subject",
    }
    missing_decisions = sorted(required_decisions - set(decisions))
    if missing_decisions:
        raise ChannelProfileError("decisions missing keys: " + ", ".join(missing_decisions))
    for key in required_decisions:
        if not _PROTOCOL_TOKEN_RE.fullmatch(decisions[key]):
            raise ChannelProfileError(f"decisions.{key} must be an uppercase protocol token")

    tool_names = _string_map(root.get("tools"), label="tools")
    missing_tools = sorted(_REQUIRED_TOOLS - set(tool_names))
    if missing_tools:
        raise ChannelProfileError("tools missing keys: " + ", ".join(missing_tools))
    tools = {
        key: _resolve_repo_path(repo_root, value, label=f"tools.{key}")
        for key, value in tool_names.items()
    }

    return ChannelProfile(
        profile_id=loaded_profile_id,
        display_name=_string(identity.get("display_name"), label="identity.display_name"),
        room_id=room_id,
        output_directory=_safe_component(
            identity.get("output_directory"), label="identity.output_directory"
        ),
        host_speaker_label=_string(
            identity.get("host_speaker_label"), label="identity.host_speaker_label"
        ),
        guest_speaker_label=_string(
            identity.get("guest_speaker_label"), label="identity.guest_speaker_label"
        ),
        asset_root=asset_root,
        asset_files=MappingProxyType(asset_files),
        asset_directories=MappingProxyType(asset_directories),
        fingerprint_asset_keys=fingerprint_asset_keys,
        fingerprint_directory_keys=fingerprint_directory_keys,
        voiceprint_reference_subdirectory=voiceprint_reference_subdirectory,
        song_title_prefix=song_title_prefix,
        song_hook_template=song_hook_template,
        song_plain_template=song_plain_template,
        decisions=MappingProxyType(decisions),
        tools=MappingProxyType(tools),
        manifest_path=resolved_manifest,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        repo_root=repo_root,
    )
