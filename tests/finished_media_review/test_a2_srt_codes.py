"""A2 lists the release check's own codes; the general SRT_RELEASE_VALIDATION_FAILED only when no error names one
(it used to be added to every failed check: list.extend returns None)."""

from src.autoslice.finished_media_review import checks


def test_a_failed_release_check_lists_its_codes_and_not_the_general_one(tmp_path):
    path = tmp_path / "final.srt"
    path.write_text("1\n00:00:00,000 --> 00:00:01,500\n这是真情实感\n\n2\n00:00:07,000 --> 00:00:07,680\n对\n\n"
                    "3\n00:00:08,020 --> 00:00:09,760\n这不是哭啊\n", encoding="utf-8")
    assert checks.srt_shape_problems(path) == ["SRT_SINGLE_CJK_CHARACTER"]


def test_a2_reports_the_exceptional_reading_rate(tmp_path):
    path = tmp_path / 'final.srt'
    path.write_text('1\n00:02:51,640 --> 00:02:53,320\n人们本以为，lmsm那一句\n'
                    '李豆沙本来就是猪啊是一句玩笑话\n', encoding='utf-8')
    assert 'SRT_CUE_READ_TOO_FAST' in checks.srt_shape_problems(path)


def test_a2_displays_packed_flag_and_owner_flag(tmp_path):
    from types import SimpleNamespace
    from src.autoslice.finished_media_review.receipt import auto_problems_and_findings, A_IDS

    path = tmp_path / 'final.srt'
    path.write_text('1\n00:00:01,000 --> 00:00:01,405\n然后自己在那\n', encoding='utf-8')
    ctx = SimpleNamespace(files={'srt': path}, member={'owner_cue_numbers': [1]},
                          text=lambda _key: path.read_text(encoding='utf-8'))
    result = checks.a2_srt(ctx)
    assert result['status'] == 'FLAG'
    assert result['reason_codes'] == ['SRT_CUE_READ_FAST_OWNER']
    rows = [result if i == 'A2' else {'id': i, 'status': 'PASS'} for i in A_IDS]
    _, findings = auto_problems_and_findings({'schema_version': 'finished-media-auto-checks.v1', 'checks': rows})
    assert 'A2:FLAG:SRT_CUE_READ_FAST_OWNER' in findings


def test_last_fast_cue_uses_subtitle_clock_duration(tmp_path):
    from types import SimpleNamespace

    path = tmp_path / 'final.srt'
    path.write_text('1\n00:00:00,000 --> 00:00:00,405\n然后自己在那\n', encoding='utf-8')
    ctx = SimpleNamespace(files={'srt': path}, member={'subtitle_duration_ms': 1405},
                          text=lambda _key: path.read_text(encoding='utf-8'))
    assert checks.a2_srt(ctx)['reason_codes'] == ['SRT_CUE_READ_TOO_FAST']
    ctx.member['subtitle_duration_ms'] = 1404
    assert checks.a2_srt(ctx)['reason_codes'] == ['SRT_CUE_READ_FAST_PACKED']
