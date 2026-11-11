"""A line that carries the words of dropped source sentences covers their time; splits cut at the sources' edges."""
from fakes import FakeRemote, IdCpa, make_ctx
from src.autoslice.manual_lane import normalize, subtitle_track
from src.autoslice.manual_lane.srt_text import format_srt
from src.autoslice.manual_lane.step3_doubts import write_subtitles
from src.autoslice.manual_lane.step3_text_classes import merged_timing, partitioned_cues


def cues(*items):
    return [{'id': i, 'start_s': a, 'end_s': b, 'text': t} for i, (a, b, t) in enumerate(items, 1)]


def rows(bcut, **final):
    """One decided row per cue; `final` maps cue id (as cN) to its final text, '' = dropped."""
    out = []
    for c in bcut:
        text = final.get(f"c{c['id']}", c['text'])
        out.append({'id': f"d{c['id']:03d}", 'cue_id': c['id'], 'state': 'ADOPT', 'current': c['text'],
                    'final_text': text, 'dropped': text == '', 'confidence': '高', 'affects_meaning': False})
    return out


def spans(timing):
    return {k: (v['start_s'], v['end_s']) for k, v in timing.items()}


# --- the relations -----------------------------------------------------------------------------------------------
SOURCE = cues((0., 2., '今天我们去公园玩'), (3., 4., '然后看到了小狗'), (5., 7., '特别可爱'), (9., 10., '回家吃饭'))


def test_line_extends_to_the_last_dropped_sentence_it_carries():
    final = rows(SOURCE, c1='今天我们去公园玩，然后看到了小狗，特别可爱', c2='', c3='')
    assert spans(merged_timing(SOURCE, final)) == {1: (0., 7.)}


def test_a_really_deleted_sentence_does_not_stretch_its_neighbour():
    final = rows(SOURCE, c3='')          # 特别可爱 is in no line
    assert merged_timing(SOURCE, final) == {}
    final = rows(SOURCE, c1='今天我们去公园玩，然后看到了小狗', c2='', c3='')   # 特别可爱 deleted, 小狗 merged
    assert spans(merged_timing(SOURCE, final)) == {1: (0., 4.)}


def test_extension_stops_at_the_next_kept_line_and_final_cues_do_not_overlap():
    final = rows(SOURCE, c1='今天我们去公园玩，然后看到了小狗', c2='')
    timing = merged_timing(SOURCE, final)
    assert spans(timing) == {1: (0., 4.)}
    by_id = {d['cue_id']: d for d in final}
    out = normalize.shape_decided_cues([dict(c, text=by_id[c['id']]['final_text'], **timing.get(c['id'], {}))
                                        for c in SOURCE if not by_id[c['id']]['dropped']])
    assert [(c['start_s'], c['end_s']) for c in out] == [(0., 4.), (5., 7.), (9., 10.)]
    assert all(a['end_s'] <= b['start_s'] for a, b in zip(out, out[1:]))


def test_text_merged_forward_moves_the_later_line_start_back():
    final = rows(SOURCE, c2='', c3='然后看到了小狗，特别可爱')
    assert spans(merged_timing(SOURCE, final)) == {3: (3., 7.)}


def test_a_chance_common_character_is_not_a_merge():
    source = cues((0., 2., '我们去公园'), (3., 4., '天气不错的'), (5., 7., '特别可爱的'))
    # the 的 of the dropped sentence matches the line's leading 的, one of five content characters: not half
    final = rows(source, c2='', c3='的特别可爱')
    assert merged_timing(source, final) == {}


def test_a_rewritten_line_keeps_its_own_source_time():
    final = rows(SOURCE, c1='完全不同的一句话', c2='')
    assert merged_timing(SOURCE, final) == {}


def test_unmerged_clip_is_untouched():
    final = rows(SOURCE)
    assert merged_timing(SOURCE, final) == {}


def test_lines_with_parts_keep_their_part_times():
    bcut = cues((0., 2., '你好吗'), (2.5, 4., '我很好'), (6., 8., '再见了'))
    final = rows(bcut, c1='你好吗我很好', c2='')
    answers = {'d001': {'parts': [{'text': '你好吗', 'first_cue': 1, 'last_cue': 1},
                                  {'text': '我很好', 'first_cue': 2, 'last_cue': 2}]},
               'd002': {}, 'd003': {}}
    out = partitioned_cues(bcut, final, answers, [], 10.)
    assert [(c['start_s'], c['end_s'], c['text']) for c in out] == [
        (0., 2., '你好吗'), (2.5, 4., '我很好'), (6., 8., '再见了')]
    assert merged_timing(bcut, final, skip={1}) == {}


def test_mixed_parts_and_merged_lines_use_the_same_timing():
    bcut = cues((0., 2., '你好吗'), (2.5, 4., '我很好'), (6., 8., '再见了'), (9., 10., '明天见'))
    final = rows(bcut, c1='你好吗', c3='再见了，明天见', c4='')
    answers = {'d001': {'parts': [{'text': '你好吗', 'first_cue': 1, 'last_cue': 1}]},
               'd002': {}, 'd003': {}, 'd004': {}}
    out = partitioned_cues(bcut, final, answers, [], 10.)
    assert [(c['start_s'], c['end_s'], c['text']) for c in out if c['text'] != '你好吗'] == [
        (2.5, 4., '我很好'), (6., 10., '再见了，明天见')]


# --- a clip of invented small talk (weather and the commute), source sentences 1-14 -------------------------------
LONG = cues((83.24, 86.36, '这个汤最后煮成了这个颜色好奇怪啊'), (90., 91.12, '他们也在排队'), (91.12, 92.92, '早高峰谁不排队呢'),
            (93.73, 95.37, '我今天出门时候'), (96.69, 99.41, '虽然天气预报说会晴天啊'), (99.41, 100.33, '但是'),
            (101.4, 103.36, '但是其实那个'), (103.8, 104.4, '呃'), (104.6, 107.64, '其实乌运呢，比昨天厚很多很多'),
            (107.84, 109., '所以'), (109.75, 112.69, '所以明天未必是一种晴天'), (112.69, 115.79, '嘛，而且伞还忘在家里了'),
            (116.83, 119.87, '有点烦，人有点急'), (119.87, 121.63, '急死了。有点'))
LONG_FINAL = rows(LONG, c2='她们也在排队，早高峰谁不排队呢', c3='',
                  c4='我今天出门时候，虽然天气预报说会晴天啊，但是，但是其实那个，呃，其实乌云呢，比昨天厚很多很多',
                  c5='', c6='', c7='', c8='', c9='',
                  c10='所以，所以明天未必是一种晴天嘛，而且伞还落在家里了', c11='', c12='',
                  c13='有点烦人，有点急，急死了，有点', c14='')
CHECKED_PAGE = [(90., 92.92), (93.73, 100.33), (101.4, 107.64), (107.84, 115.79), (116.83, 121.63)]


def test_merged_sentences_cover_all_their_sources():
    assert spans(merged_timing(LONG, LONG_FINAL)) == {
        2: (90., 92.92), 4: (93.73, 107.64), 10: (107.84, 115.79), 13: (116.83, 121.63)}


def test_final_cues_follow_the_source_edges(tmp_path):
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    _, final, _ = write_subtitles(ctx, LONG, LONG_FINAL, preserve_decisions=True)
    shaped = subtitle_track.parse_blocks(final.read_text(encoding='utf-8'))
    assert [(c['start_s'], c['end_s']) for c in shaped[1:]] == CHECKED_PAGE
    assert all(a['end_s'] <= b['start_s'] for a, b in zip(shaped, shaped[1:]))
    assert shaped[3]['text'].replace('\n', '') == '但是其实那个，呃，其实乌云呢，比昨天厚很多很多'


# --- nothing merged: bytes as before --------------------------------------------------------------------------------
def test_unmerged_clip_writes_what_the_old_rule_wrote(tmp_path):
    bcut = cues((0., 2., '你好'), (3., 5., '他们打算在10月21号一起看《晚风中的花园》，然后回大学校园找Mujica的朋友'),
                (6., 7., '再见'))
    final = rows(bcut, c3='再见啦')
    ctx = make_ctx(tmp_path, remote=FakeRemote(), cpa=IdCpa())
    _, path, _ = write_subtitles(ctx, bcut, final, preserve_decisions=True)
    old = normalize.shape_decided_cues([dict(c, text=d['final_text']) for c, d in zip(bcut, final)])
    assert path.read_text(encoding='utf-8') == format_srt(old)
    assert merged_timing(bcut, final) == {}


def test_split_without_sources_still_interpolates_as_before():
    audit = normalize._audit()
    text = '他们打算在10月21号一起看《晚风中的花园》，然后回大学校园找Mujica的朋友'
    assert normalize.split_cue(text, 0., 6., audit) == normalize.split_cue(text, 0., 6., audit, None)
    # segments that do not describe the text are ignored
    bad = [{'start_s': 0., 'end_s': 6., 'chars': 3}]
    assert normalize.split_cue(text, 0., 6., audit, bad) == normalize.split_cue(text, 0., 6., audit)
