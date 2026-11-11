"""Real encoding on the ordinary long-range fallback; provider transport stub only."""
import io
import wave

import pytest

from src.autoslice import entity_audio_verifier as audio, gemini_range_context as gemini
from src.autoslice.discovery_channels import prefetch_fullclip, fullclip_scan_complete
from src.autoslice import supplement_audio_budget
from tests.test_native_range_fallback import setup_path


@pytest.mark.parametrize('end_ms',[31_000,47_125,227_440])
def test_full_block_fallback_encodes_over_thirty_seconds_and_cold_reuses(tmp_path,monkeypatch,end_ms):
    extract, crop = gemini._extract_exact_mp3, gemini._crop_black_frame_audio
    duration_ms=end_ms+1000
    stream=io.BytesIO()
    with wave.open(stream,'wb') as wav:
        wav.setparams((1,2,8000,0,'NONE','not compressed'))
        wav.writeframes(b'\x00\x00'*(duration_ms*8))
    source, output, identity, counts, build=setup_path(tmp_path,monkeypatch,
        source_bytes=stream.getvalue(),duration_ms=duration_ms,http_status=524)
    # Keep the real source/geometry, budget, codec, persisted artifacts and
    # reader. The fixture substitutes only native/provider responses here.
    monkeypatch.setattr(gemini,'_extract_exact_mp3',extract)
    monkeypatch.setattr(gemini,'_crop_black_frame_audio',crop)
    seconds,milliseconds=divmod(end_ms,1000)
    minutes,seconds=divmod(seconds,60)
    text=f'1\n00:00:01,000 --> 00:{minutes:02d}:{seconds:02d},{milliseconds:03d}\n整句范围听写\n\n'
    rows=prefetch_fullclip(text,verifier=build(),source=identity,source_offset_ms=0)
    assert fullclip_scan_complete(rows),rows
    assert counts=={'native_failed':1,'gemini':1}
    attachment,=output.glob('gemini-range-acquisition/**/input.mp4')
    assert audio._validate_cropped_audio_media(attachment,expected_duration_ms=end_ms-1000)==(True,'')
    supplement_audio_budget._REGISTRY.pop(str(source.resolve()),None)
    cold=prefetch_fullclip(text,verifier=build(),source=identity,source_offset_ms=0)
    assert fullclip_scan_complete(cold) and counts=={'native_failed':1,'gemini':1}
    assert audio._crop_black_frame_audio(source_media=source,audio_path=tmp_path/'narrow.mp4',
        start_ms=0,end_ms=31_000)[0] is False


@pytest.mark.parametrize('start,end',[(1000,1000),(-1,1000),(1000,0)])
def test_renderer_keeps_invalid_interval_guards(tmp_path,start,end):
    assert audio._render_black_frame_audio(source_media=tmp_path/'missing.wav',
        audio_path=tmp_path/'out.mp4',start_ms=start,end_ms=end)[0] is False
