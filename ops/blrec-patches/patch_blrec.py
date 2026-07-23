#!/usr/bin/env python3
"""Apply the pinned production fixes to blrec 2.0.0b4.

The image is pinned, and every replacement is fail-closed: an upstream layout
change must be reviewed instead of silently leaving only part of the recorder
patched.
"""

from __future__ import annotations

import compileall
import hashlib
import os
import shutil
import tempfile
from pathlib import Path


DEFAULT_SITE_PACKAGES = Path(
    os.environ.get(
        "BLREC_SITE_PACKAGES",
        "/usr/local/lib/python3.10/site-packages",
    )
)

EXPECTED_VERSION_LINE = "__version__ = '2.0.0-beta.4'"
TARGET_FILES = (
    "blrec/bili/danmaku_client.py",
    "blrec/hls/operators/segment_dumper.py",
    "blrec/core/stream_param_holder.py",
    "blrec/core/operators/stream_url_resolver.py",
    "blrec/bili/live.py",
    "blrec/core/stream_recorder.py",
    "blrec/postprocess/remux.py",
)

# Filled from the pinned 2.0.0b4 wheel after applying every patch below.
POST_PATCH_SHA256 = {
    "blrec/bili/danmaku_client.py": (
        "4a03156cd08a1e65a66e8d0713915b7d4776ea1d600291f86bc9f8adbd2ea1e1"
    ),
    "blrec/hls/operators/segment_dumper.py": (
        "849b21f13a9421b502d330ffadbfe8e897510d7d52ec83133f83ae283fbe31e6"
    ),
    "blrec/core/stream_param_holder.py": (
        "fcf551dc483e718f28dd13a71ad3d9a247145918d04da18095d74c2b0a1dd24a"
    ),
    "blrec/core/operators/stream_url_resolver.py": (
        "e9940075aca8536da32af979f95f513c5e642ad0d69e9d101ee5e9f896d6369b"
    ),
    "blrec/bili/live.py": (
        "e58b93c5f6cec6e25be857822f5d9fa0e730698f285933746995865f1a0a8d1f"
    ),
    "blrec/core/stream_recorder.py": (
        "8066d1bafaa1b38aa9a0ad26ff9e6416d4dda3f397cefced88db2480391a159a"
    ),
    "blrec/postprocess/remux.py": (
        "2b86ce3272bbca4358213fa1924ace3e35aa65d2d71e3e047fa685d4d4065024"
    ),
}


def verify_version(site_packages: Path) -> None:
    init_path = site_packages / "blrec/__init__.py"
    if EXPECTED_VERSION_LINE not in init_path.read_text():
        raise SystemExit(
            f"refusing to patch an unpinned blrec version: {init_path}"
        )


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text()
    if new in text:
        print(f"{label} already patched")
        return
    if old not in text:
        raise SystemExit(f"failed to locate patch target for {label}: {path}")
    path.write_text(text.replace(old, new, 1))
    print(f"patched {label}")


def patch_danmaku_client(site_packages: Path) -> None:
    path = site_packages / "blrec/bili/danmaku_client.py"
    replace_once(
        path,
        "self._api_platform: ApiPlatform = 'web'",
        "self._api_platform: ApiPlatform = 'android'",
        "blrec danmaku api platform to android",
    )
    replace_once(
        path,
        '"uid": self._uid,',
        '"uid": 0,',
        "blrec danmaku websocket auth uid to anonymous",
    )


def patch_segment_dumper(site_packages: Path) -> None:
    path = site_packages / "blrec/hls/operators/segment_dumper.py"
    old = """        prev_video_profile = prev_profile['streams'][0]
        prev_audio_profile = prev_profile['streams'][1]
        assert prev_video_profile['codec_type'] == 'video'
        assert prev_audio_profile['codec_type'] == 'audio'

        curr_video_profile = curr_profile['streams'][0]
        curr_audio_profile = curr_profile['streams'][1]
        assert curr_video_profile['codec_type'] == 'video'
        assert curr_audio_profile['codec_type'] == 'audio'
"""
    new = """        def stream_by_type(profile: dict, codec_type: str) -> Optional[dict]:
            return next(
                (
                    stream
                    for stream in profile.get('streams', [])
                    if stream.get('codec_type') == codec_type
                ),
                None,
            )

        prev_video_profile = stream_by_type(prev_profile, 'video')
        prev_audio_profile = stream_by_type(prev_profile, 'audio')
        curr_video_profile = stream_by_type(curr_profile, 'video')
        curr_audio_profile = stream_by_type(curr_profile, 'audio')

        if (
            prev_video_profile is None
            or prev_audio_profile is None
            or curr_video_profile is None
            or curr_audio_profile is None
        ):
            logger.warning(
                'Init section stream layout changed or incomplete; must split file: '
                f'prev_streams={[s.get("codec_type") for s in prev_profile.get("streams", [])]}, '
                f'curr_streams={[s.get("codec_type") for s in curr_profile.get("streams", [])]}'
            )
            return True
"""
    replace_once(
        path,
        old,
        new,
        "blrec segment_dumper single-stream init tolerance",
    )


def patch_quality_fallback(site_packages: Path) -> None:
    holder_path = site_packages / "blrec/core/stream_param_holder.py"
    replace_once(
        holder_path,
        """__all__ = ('StreamParamHolder',)


@attr.s(auto_attribs=True, frozen=True, slots=True)
""",
        """__all__ = ('StreamParamHolder',)


# Prefer the configured quality, but never retry an unavailable exact quality
# forever. qn=401 is Dolby-specific and is intentionally skipped for AVC.
QUALITY_FALLBACK_LADDER = (20000, 10000, 400, 250, 150, 80)


@attr.s(auto_attribs=True, frozen=True, slots=True)
""",
        "blrec quality fallback ladder declaration",
    )
    replace_once(
        holder_path,
        """    def fall_back_quality(self) -> None:
        self._real_quality_number = 10000
""",
        """    def fall_back_quality(self) -> Optional[QualityNumber]:
        current = self._real_quality_number or self._quality_number
        try:
            index = QUALITY_FALLBACK_LADDER.index(current)
        except ValueError:
            next_quality = next(
                (quality for quality in QUALITY_FALLBACK_LADDER if quality < current),
                None,
            )
        else:
            next_quality = (
                QUALITY_FALLBACK_LADDER[index + 1]
                if index + 1 < len(QUALITY_FALLBACK_LADDER)
                else None
            )

        if next_quality is None:
            self._quality_exhausted = True
            return None
        self._quality_exhausted = False
        self._real_quality_number = next_quality
        return next_quality
""",
        "blrec ordered lower-quality fallback",
    )
    replace_once(
        holder_path,
        """        self._real_quality_number: Optional[QualityNumber] = None
        self._api_platform: ApiPlatform = api_platform
""",
        """        self._real_quality_number: Optional[QualityNumber] = None
        self._quality_exhausted: bool = False
        self._api_platform: ApiPlatform = api_platform
""",
        "blrec quality exhaustion state",
    )
    replace_once(
        holder_path,
        """    def reset(self) -> None:
        self._real_quality_number = None
        self._api_platform = 'web'
""",
        """    def reset(self) -> None:
        self._real_quality_number = None
        self._quality_exhausted = False
        self._api_platform = 'web'
""",
        "blrec quality exhaustion reset",
    )
    replace_once(
        holder_path,
        """    @property
    def use_alternative_stream(self) -> bool:
""",
        """    @property
    def quality_exhausted(self) -> bool:
        return self._quality_exhausted

    @property
    def use_alternative_stream(self) -> bool:
""",
        "blrec quality exhaustion property",
    )

    resolver_path = site_packages / "blrec/core/operators/stream_url_resolver.py"
    replace_once(
        resolver_path,
        """        except NoStreamQualityAvailable:
            qn = self._stream_param_holder.quality_number
            if qn == 10000:
                logger.warning('The original stream quality (10000) is not available')
            else:
                logger.info(
                    f'The specified stream quality ({qn}) is not available, '
                    'will using the original stream quality (10000) instead.'
                )
                self._stream_param_holder.fall_back_quality()
""",
        """        except NoStreamQualityAvailable:
            unavailable_qn = self._stream_param_holder.real_quality_number
            fallback_qn = self._stream_param_holder.fall_back_quality()
            if fallback_qn is None:
                logger.warning(
                    f'The lowest stream quality ({unavailable_qn}) is not available'
                )
            else:
                logger.warning(
                    f'The stream quality ({unavailable_qn}) is not available; '
                    f'falling back to ({fallback_qn}).'
                )
""",
        "blrec resolver lower-quality fallback",
    )
    replace_once(
        resolver_path,
        """    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(
            exc,
            (
                NoStreamAvailable,
                NoStreamCodecAvailable,
                NoStreamFormatAvailable,
                NoStreamQualityAvailable,
                NoAlternativeStreamAvailable,
            ),
        ):
            return True
        else:
            return False
""",
        """    def _should_retry(self, exc: Exception) -> bool:
        if isinstance(exc, NoStreamQualityAvailable):
            return not self._stream_param_holder.quality_exhausted
        if isinstance(
            exc,
            (
                NoStreamAvailable,
                NoStreamCodecAvailable,
                NoStreamFormatAvailable,
            ),
        ):
            return True
        else:
            return False
""",
        "blrec bounded quality and route retry",
    )


def patch_cdn_route_rotation(site_packages: Path) -> None:
    path = site_packages / "blrec/bili/live.py"
    replace_once(
        path,
        """        self._room_info: RoomInfo
        self._user_info: UserInfo
        self._no_flv_stream: bool
""",
        """        self._room_info: RoomInfo
        self._user_info: UserInfo
        self._no_flv_stream: bool
        self._stream_route_cursors: Dict[str, int] = {}
""",
        "blrec CDN route cursor",
    )
    replace_once(
        path,
        """        if not select_alternative:
            return urls[0]

        try:
            return urls[1]
        except IndexError:
            raise NoAlternativeStreamAvailable(stream_format, stream_codec, qn)
""",
        """        if not urls:
            raise NoAlternativeStreamAvailable(stream_format, stream_codec, qn)

        # Primary probes never consume recovery routes. After a segment failure,
        # the resolver keeps select_alternative enabled and walks each remaining
        # route exactly once for this process/source generation.
        route_key = f'{stream_format}:{stream_codec}:{qn}'
        if not select_alternative:
            return urls[0]
        route_index = self._stream_route_cursors.get(route_key, 1)
        if route_index >= len(urls):
            raise NoAlternativeStreamAvailable(stream_format, stream_codec, qn)
        self._stream_route_cursors[route_key] = route_index + 1
        return urls[route_index]
""",
        "blrec bounded all-CDN route rotation",
    )

    resolver_path = site_packages / "blrec/core/operators/stream_url_resolver.py"
    replace_once(
        resolver_path,
        """    def rotate_routes(self) -> None:
        self.use_alternative_stream = not self.use_alternative_stream
""",
        """    def rotate_routes(self) -> None:
        # Once recovery begins, every subsequent URL resolution advances the
        # bounded route cursor in Live instead of toggling back to primary.
        self.use_alternative_stream = True
""",
        "blrec route recovery generation latch",
    )
def patch_strict_stream_format(site_packages: Path) -> None:
    path = site_packages / "blrec/core/stream_recorder.py"
    replace_once(
        path,
        """            if stream_format == 'flv':
                self._logger.warning(
                    'The specified stream format (flv) is not available, '
                    'falling back to stream format (fmp4).'
                )
                stream_format = 'fmp4'
""",
        """            if stream_format == 'flv':
                self._logger.warning(
                    'The supervised stream profile requires flv, but the API '
                    'reports no flv stream; keeping the configured format.'
                )
""",
        "blrec strict FLV supervised profile",
    )
    replace_once(
        path,
        """                else:
                    self._logger.warning(
                        'The specified stream format (fmp4) is not available '
                        f'in {self.fmp4_stream_timeout} seconcds, '
                        'falling back to stream format (flv).'
                    )
                    stream_format = 'flv'
""",
        """                else:
                    self._logger.warning(
                        'The supervised stream profile could not confirm fmp4 '
                        f'in {self.fmp4_stream_timeout} seconds; keeping fmp4 '
                        'so the external supervisor can make the format decision.'
                    )
""",
        "blrec strict fMP4 supervised profile",
    )


def patch_atomic_remux(site_packages: Path) -> None:
    path = site_packages / "blrec/postprocess/remux.py"
    replace_once(
        path,
        """) -> Observable[Union[RemuxingProgress, RemuxingResult]]:
    SIZE_PATTERN: Final = re.compile(r'size=\\s*(?P<number>\\d+)(?P<unit>[a-zA-Z]?B)')
""",
        """) -> Observable[Union[RemuxingProgress, RemuxingResult]]:
    source_path = video_path(in_path) if in_path.endswith('.m3u8') else in_path
    incomplete_marker = f'{source_path}.incomplete.json'
    if os.path.exists(incomplete_marker):
        raise RuntimeError(
            f'Refusing to publish a source marked incomplete: {incomplete_marker}'
        )

    SIZE_PATTERN: Final = re.compile(r'size=\\s*(?P<number>\\d+)(?P<unit>[a-zA-Z]?B)')
""",
        "blrec incomplete source remux refusal",
    )
    replace_once(
        path,
        """                cmd = f'ffmpeg -i "{in_path}"'
                if metadata_path is not None:
                    cmd += f' -i "{metadata_path}" -map_metadata 1'
                cmd += ' -codec copy'
""",
        """                temporary_out_path = f'{out_path}.tmp'
                if os.path.exists(out_path):
                    observer.on_error(
                        FileExistsError(
                            f'Refusing to overwrite completed remux output: {out_path}'
                        )
                    )
                    return
                try:
                    os.remove(temporary_out_path)
                except FileNotFoundError:
                    pass

                cmd = f'ffmpeg -i "{in_path}"'
                if metadata_path is not None:
                    cmd += f' -i "{metadata_path}" -map_metadata 1'
                cmd += ' -codec copy'
""",
        "blrec remux no-clobber temporary output",
    )
    replace_once(
        path,
        """                cmd += f' "{out_path}" -y'
""",
        """                cmd += f' -f mp4 "{temporary_out_path}" -y'
""",
        "blrec remux explicit temporary MP4",
    )
    replace_once(
        path,
        """                    result = RemuxingResult(process.returncode, ''.join(out_lines))
                    observer.on_next(result)
                    observer.on_completed()
""",
        """                    result = RemuxingResult(process.returncode, ''.join(out_lines))
                    if not result.is_failed():
                        try:
                            os.replace(temporary_out_path, out_path)
                        except Exception as e:
                            observer.on_error(e)
                            return
                    observer.on_next(result)
                    observer.on_completed()
""",
        "blrec atomic remux publication",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_stage(stage: Path, verify_hashes: bool) -> None:
    if not verify_hashes:
        return
    for relative_path in TARGET_FILES:
        if not compileall.compile_file(
            str(stage / relative_path),
            quiet=1,
            force=True,
        ):
            raise SystemExit(f"patched module does not compile: {relative_path}")
    if set(POST_PATCH_SHA256) != set(TARGET_FILES):
        raise SystemExit("post-patch hash manifest is incomplete")
    for relative_path, expected in POST_PATCH_SHA256.items():
        actual = _sha256(stage / relative_path)
        if actual != expected:
            raise SystemExit(
                f"post-patch hash mismatch for {relative_path}: {actual}"
            )


def _patch_stage(stage: Path) -> None:
    patch_danmaku_client(stage)
    patch_segment_dumper(stage)
    patch_quality_fallback(stage)
    patch_cdn_route_rotation(stage)
    patch_strict_stream_format(stage)
    patch_atomic_remux(stage)


def apply_all(
    site_packages: Path = DEFAULT_SITE_PACKAGES,
    *,
    verify_hashes: bool = True,
) -> None:
    """Validate every edit in a staging tree before replacing live modules."""
    verify_version(site_packages)
    with tempfile.TemporaryDirectory(
        prefix=".blrec-patch-stage-",
        dir=site_packages,
    ) as temporary_dir:
        root = Path(temporary_dir)
        patched_root = root / "patched"
        original_root = root / "original"
        for relative_path in TARGET_FILES:
            source = site_packages / relative_path
            patched = patched_root / relative_path
            original = original_root / relative_path
            patched.parent.mkdir(parents=True, exist_ok=True)
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, patched)
            shutil.copy2(source, original)

        _patch_stage(patched_root)
        _validate_stage(patched_root, verify_hashes)

        try:
            for relative_path in TARGET_FILES:
                source = site_packages / relative_path
                patched = patched_root / relative_path
                if source.read_bytes() != patched.read_bytes():
                    os.replace(patched, source)
        except Exception:
            for relative_path in TARGET_FILES:
                original = original_root / relative_path
                if original.exists():
                    os.replace(original, site_packages / relative_path)
            raise


if __name__ == "__main__":
    apply_all()
