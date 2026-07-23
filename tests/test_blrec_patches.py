from __future__ import annotations

import importlib.util
from pathlib import Path


PATCH_SCRIPT = (
    Path(__file__).parents[1] / "ops" / "blrec-patches" / "patch_blrec.py"
)


def load_patch_module():
    spec = importlib.util.spec_from_file_location("patch_blrec_under_test", PATCH_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_fixture_tree(root: Path) -> None:
    files = {
        "blrec/__init__.py": """
__prog__ = 'blrec'
__version__ = '2.0.0-beta.4'
""",
        "blrec/bili/danmaku_client.py": """
self._api_platform: ApiPlatform = 'web'
payload = {
    "uid": self._uid,
}
""",
        "blrec/hls/operators/segment_dumper.py": """
        prev_video_profile = prev_profile['streams'][0]
        prev_audio_profile = prev_profile['streams'][1]
        assert prev_video_profile['codec_type'] == 'video'
        assert prev_audio_profile['codec_type'] == 'audio'

        curr_video_profile = curr_profile['streams'][0]
        curr_audio_profile = curr_profile['streams'][1]
        assert curr_video_profile['codec_type'] == 'video'
        assert curr_audio_profile['codec_type'] == 'audio'
""",
        "blrec/core/stream_param_holder.py": """
from typing import Optional
__all__ = ('StreamParamHolder',)


@attr.s(auto_attribs=True, frozen=True, slots=True)
class StreamParams:
    pass

class StreamParamHolder:
    def __init__(self, api_platform):
        self._real_quality_number: Optional[QualityNumber] = None
        self._api_platform: ApiPlatform = api_platform

    def reset(self) -> None:
        self._real_quality_number = None
        self._api_platform = 'web'

    @property
    def use_alternative_stream(self) -> bool:
        return self._use_alternative_stream

    def fall_back_quality(self) -> None:
        self._real_quality_number = 10000
""",
        "blrec/core/operators/stream_url_resolver.py": """
    def rotate_routes(self) -> None:
        self.use_alternative_stream = not self.use_alternative_stream

    def _should_retry(self, exc: Exception) -> bool:
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

        except NoStreamQualityAvailable:
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
        "blrec/bili/live.py": """
        self._room_info: RoomInfo
        self._user_info: UserInfo
        self._no_flv_stream: bool

        if not select_alternative:
            return urls[0]

        try:
            return urls[1]
        except IndexError:
            raise NoAlternativeStreamAvailable(stream_format, stream_codec, qn)
""",
        "blrec/core/stream_recorder.py": """
            if stream_format == 'flv':
                self._logger.warning(
                    'The specified stream format (flv) is not available, '
                    'falling back to stream format (fmp4).'
                )
                stream_format = 'fmp4'

                else:
                    self._logger.warning(
                        'The specified stream format (fmp4) is not available '
                        f'in {self.fmp4_stream_timeout} seconcds, '
                        'falling back to stream format (flv).'
                    )
                    stream_format = 'flv'
""",
        "blrec/postprocess/remux.py": """
def remux_video(
    in_path: str,
    out_path: str,
) -> Observable[Union[RemuxingProgress, RemuxingResult]]:
    SIZE_PATTERN: Final = re.compile(r'size=\\s*(?P<number>\\d+)(?P<unit>[a-zA-Z]?B)')

                cmd = f'ffmpeg -i "{in_path}"'
                if metadata_path is not None:
                    cmd += f' -i "{metadata_path}" -map_metadata 1'
                cmd += ' -codec copy'
                cmd += f' "{out_path}" -y'

                    result = RemuxingResult(process.returncode, ''.join(out_lines))
                    observer.on_next(result)
                    observer.on_completed()
""",
    }
    for relative_path, text in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.lstrip("\n"))


def test_all_patches_apply_and_are_idempotent(tmp_path: Path) -> None:
    module = load_patch_module()
    write_fixture_tree(tmp_path)

    module.apply_all(tmp_path, verify_hashes=False)
    first_pass = {
        path.relative_to(tmp_path): path.read_text()
        for path in tmp_path.rglob("*.py")
    }
    module.apply_all(tmp_path, verify_hashes=False)
    second_pass = {
        path.relative_to(tmp_path): path.read_text()
        for path in tmp_path.rglob("*.py")
    }

    assert first_pass == second_pass
    holder = (tmp_path / "blrec/core/stream_param_holder.py").read_text()
    resolver = (
        tmp_path / "blrec/core/operators/stream_url_resolver.py"
    ).read_text()
    live = (tmp_path / "blrec/bili/live.py").read_text()
    segment = (
        tmp_path / "blrec/hls/operators/segment_dumper.py"
    ).read_text()
    remux = (tmp_path / "blrec/postprocess/remux.py").read_text()
    stream_recorder = (tmp_path / "blrec/core/stream_recorder.py").read_text()

    assert "QUALITY_FALLBACK_LADDER" in holder
    assert "next_quality" in holder
    assert "self._quality_exhausted = True" in holder
    assert "def quality_exhausted" in holder
    assert "fallback_qn = self._stream_param_holder.fall_back_quality()" in resolver
    assert "not self._stream_param_holder.quality_exhausted" in resolver
    assert "route_index >= len(urls)" in live
    assert "return urls[route_index]" in live
    assert "_stream_route_cursors" in live
    assert "self.use_alternative_stream = True" in resolver
    retry_block = resolver.split("def _should_retry", 1)[1].split(
        "def _before_retry", 1
    )[0]
    assert "NoAlternativeStreamAvailable" not in retry_block
    assert "stream_by_type" in segment
    assert "keeping the configured format" in stream_recorder
    assert "keeping fmp4" in stream_recorder
    assert "Refusing to publish a source marked incomplete" in remux
    assert "temporary_out_path" in remux
    assert "Refusing to overwrite completed remux output" in remux
    assert "os.replace(temporary_out_path, out_path)" in remux


def test_wrong_blrec_version_is_rejected_before_any_patch(tmp_path: Path) -> None:
    module = load_patch_module()
    write_fixture_tree(tmp_path)
    init_path = tmp_path / "blrec/__init__.py"
    init_path.write_text("__version__ = '9.9.9'\n")
    original = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*.py")
    }

    try:
        module.apply_all(tmp_path, verify_hashes=False)
    except SystemExit as exc:
        assert "unpinned blrec version" in str(exc)
    else:
        raise AssertionError("unexpected version was patched")

    current = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*.py")
    }
    assert current == original
