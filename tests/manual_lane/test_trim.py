"""Cutting unrelated gift thanks out of the middle of a clip: the pure computations of manual_lane/trim.py."""
import json
from pathlib import Path

import pytest

from src.autoslice.manual_lane import compilation as comp
from src.autoslice.manual_lane import trim

THANKS = ['谢谢', '感谢', '多谢', 'ありがと']
FIXTURES = Path(__file__).parent / 'fixtures' / 'trim'


def cues(*items):
    return [{'id': i, 'start_s': a, 'end_s': b, 'text': t} for i, (a, b, t) in enumerate(items, 1)]


def answer(*spans):
    return {'remove': [{'start_cue': a, 'end_cue': b, 'gift_said': 'x', 'why': 'y'} for a, b in spans]}


# five cues, the third one is a gift thanks far from its neighbours
SPARSE = cues((0., 2., '开头'), (2.5, 4., '正题'), (10., 12., '谢谢小花的灯牌'), (20., 22., '继续正题'), (30., 32., '结尾'))


# --- prompt ------------------------------------------------------------------------------------------------------
def test_prompt_fills_the_three_placeholders_and_keeps_the_json_braces():
    out = trim.prompt('选题理由XYZ', '12.0s SC 某人 你好', cues((3.14, 65.04, '第一句'), (66., 67., '第二句')))
    assert '选题理由XYZ' in out and '12.0s SC 某人 你好' in out
    assert '{hook}' not in out and '{events}' not in out and '{cues}' not in out
    assert '{"remove": [{"start_cue": 整数, "end_cue": 整数' in out


def test_prompt_cue_line_is_number_seconds_text_with_newlines_as_spaces():
    out = trim.prompt('h', 'e', cues((3.14, 65.04, 'a\nb'), (59.96, 66., 'c')))
    assert out.endswith('1 3.1-65.0 a b\n2 60.0-66.0 c\n')


def test_prompt_second_ask_is_marked_as_in_the_trial():
    one = trim.prompt('h', 'e', cues((0., 1., 'a')))
    assert trim.prompt('h', 'e', cues((0., 1., 'a')), run=2) == one.rstrip('\n') + '\n（第 2 遍）\n'


def test_prompt_empty_events_become_none_marker():
    assert '- SC / 上舰 / 礼物记录（时间 类型 发送人 内容）：\n（无记录）\n' in trim.prompt('h', '', cues((0., 1., 'a')))


# --- parse_answer ------------------------------------------------------------------------------------------------
def test_parse_answer_returns_spans_by_start():
    assert trim.parse_answer(answer((7, 9), (2, 3), (5, 5)), 10) == [(2, 3), (5, 5), (7, 9)]


def test_parse_answer_empty_remove_is_no_spans():
    assert trim.parse_answer({'remove': []}, 10) == []


@pytest.mark.parametrize('bad, why', [
    (answer((0, 3)), 'start below 1'),
    (answer((4, 11)), 'end beyond the last cue'),
    (answer((6, 5)), 'start after end'),
    ({'remove': [{'start_cue': True, 'end_cue': 3}]}, 'bool is not an integer'),
    ({'remove': [{'start_cue': 2, 'end_cue': 3.0}]}, 'float is not an integer'),
    ({'remove': [{'start_cue': '2', 'end_cue': 3}]}, 'string is not an integer'),
    ({'remove': [{'start_cue': 2}]}, 'end_cue missing'),
    (answer((2, 5), (5, 7)), 'spans overlap'),
    ({'remove': {'start_cue': 2, 'end_cue': 3}}, 'remove is not a list'),
    ({'remove': ['2-3']}, 'item is not an object'),
    ({}, 'no remove'),
    ([], 'answer is not an object'),
])
def test_parse_answer_rejects(bad, why):
    with pytest.raises(ValueError):
        trim.parse_answer(bad, 10)


def test_parse_answer_error_names_the_item():
    with pytest.raises(ValueError, match=r'remove\[1\].*5..2'):
        trim.parse_answer(answer((1, 2), (5, 2)), 10)


# --- agreed ------------------------------------------------------------------------------------------------------
def test_agreed_two_equal_answers_stay_as_they_are():
    assert trim.agreed([(3, 5), (9, 9)], [(3, 5), (9, 9)]) == [(3, 5), (9, 9)]


def test_agreed_drops_a_span_only_one_answer_proposes():
    assert trim.agreed([(3, 5), (9, 9)], [(3, 5)]) == [(3, 5)]
    assert trim.agreed([(3, 5)], []) == []


def test_agreed_partly_overlapping_spans_keep_only_the_shared_cues():
    assert trim.agreed([(3, 6)], [(5, 9)]) == [(5, 6)]


def test_agreed_intersection_broken_in_the_middle_gives_two_spans():
    assert trim.agreed([(3, 9)], [(3, 4), (8, 12)]) == [(3, 4), (8, 9)]


def test_agreed_joins_adjacent_cue_numbers_into_one_span():
    assert trim.agreed([(3, 4), (5, 6)], [(3, 6)]) == [(3, 6)]


# --- guard -------------------------------------------------------------------------------------------------------
LINES = cues((0., 2., '开头'), (3., 4., '正题'), (5., 6., '谢谢小花的灯牌'), (7., 8., '谢谢你'), (9., 10., '继续正题'),
             (11., 12., '结尾'))


def guard(spans, cue_list=LINES, **kw):
    kw = {'thanks_words': THANKS, 'clip_len_s': 13., 'min_keep_s': 1.} | kw
    return trim.guard(spans, cue_list, **kw)


def test_guard_lets_a_thanks_span_through():
    assert guard([(3, 4)]) == ([(3, 4)], [])


def test_guard_edge_span_with_the_first_cue_is_vetoed():
    passed, vetoed = guard([(1, 3)])
    assert passed == [] and [(v['span'], v['veto']) for v in vetoed] == [((1, 3), 'EDGE')]


def test_guard_edge_span_with_the_last_cue_is_vetoed():
    passed, vetoed = guard([(4, 6)])
    assert passed == [] and [(v['span'], v['veto']) for v in vetoed] == [((4, 6), 'EDGE')]


def test_guard_no_thanks_when_no_cue_of_the_span_has_a_thanks_word():
    passed, vetoed = guard([(2, 2)])
    assert passed == [] and [(v['span'], v['veto']) for v in vetoed] == [((2, 2), 'NO_THANKS')]


def test_guard_thanks_word_in_one_cue_of_the_span_is_enough():
    assert guard([(2, 3)])[0] == [(2, 3)]


def test_guard_thanks_words_are_compared_after_norm_text_on_both_sides():
    lines = cues((0., 2., '开头'), (3., 4., '谢，谢 AB'), (9., 10., '继续正题'), (11., 12., '结尾'))
    assert guard([(2, 2)], cue_list=lines, thanks_words=['谢谢 ab'])[0] == [(2, 2)]


def test_guard_empty_thanks_word_is_an_error():
    with pytest.raises(ValueError, match='empty word'):
        guard([(3, 3)], thanks_words=['谢谢', '，'])


def test_guard_edge_is_reported_before_no_thanks():
    passed, vetoed = guard([(1, 2)])        # holds the first cue and has no thanks word
    assert [v['veto'] for v in vetoed] == ['EDGE']


def test_guard_too_short_vetoes_every_span_that_passed():
    lines = cues((0., 1., '开头'), (1.5, 2., '谢谢小花'), (3., 4., '继续'), (5., 6., '谢谢小草'), (7., 8., '结尾'))
    passed, vetoed = guard([(2, 2), (4, 4)], cue_list=lines, clip_len_s=9., min_keep_s=100.)
    assert passed == [] and [(v['span'], v['veto']) for v in vetoed] == [((2, 2), 'TOO_SHORT'), ((4, 4), 'TOO_SHORT')]
    assert guard([(2, 2), (4, 4)], cue_list=lines, clip_len_s=9., min_keep_s=1.)[0] == [(2, 2), (4, 4)]


def test_guard_too_short_counts_only_the_spans_that_passed_the_other_rules():
    lines = cues((0., 1., '开头'), (1.5, 2., '谢谢小花'), (3., 4., '继续'), (5., 6., '正题'), (7., 8., '结尾'))
    passed, vetoed = guard([(2, 2), (3, 4)], cue_list=lines, clip_len_s=9., min_keep_s=7.)
    # only 2..2 passes; cutting it keeps about 8.5 s of 9 s
    assert passed == [(2, 2)] and [v['veto'] for v in vetoed] == ['NO_THANKS']


# --- keep_windows ------------------------------------------------------------------------------------------------
def test_keep_windows_single_hole_uses_the_fixed_margins_when_the_gaps_are_wide():
    assert trim.keep_windows(SPARSE, [(3, 3)], 33.) == [(0., round(4. + comp.TAIL_MARGIN_S, 3)),
                                                         (round(20. - comp.HEAD_MARGIN_S, 3), 33.)]


def test_keep_windows_two_holes_give_three_windows():
    lines = cues((0., 2., 'a'), (10., 12., 'b'), (20., 22., 'c'), (30., 32., 'd'), (40., 42., 'e'), (50., 52., 'f'))
    assert trim.keep_windows(lines, [(2, 2), (4, 4)], 60.) == [
        (0., round(2. + comp.TAIL_MARGIN_S, 3)), (round(20. - comp.HEAD_MARGIN_S, 3), round(22. + comp.TAIL_MARGIN_S, 3)),
        (round(40. - comp.HEAD_MARGIN_S, 3), 60.)]


def test_keep_windows_narrow_gap_cuts_at_the_middle_of_the_gap():
    lines = cues((0., 2., 'a'), (2.2, 4., 'b'), (4.2, 6., 'hole'), (6.2, 8., 'c'), (9., 10., 'd'))
    assert trim.keep_windows(lines, [(3, 3)], 11.) == [(0., 4.1), (6.1, 11.)]


def test_keep_windows_touching_cues_cut_at_their_shared_time():
    lines = cues((0., 2., 'a'), (2., 4., 'b'), (4., 6., 'hole'), (6., 8., 'c'), (9., 10., 'd'))
    assert trim.keep_windows(lines, [(3, 3)], 11.) == [(0., 4.), (6., 11.)]


def test_keep_windows_overlapping_cues_do_not_cut_into_the_kept_cue():
    lines = cues((0., 2., 'a'), (2., 4.2, 'b'), (4., 6., 'hole'), (6., 8., 'c'), (9., 10., 'd'))
    assert trim.keep_windows(lines, [(3, 3)], 11.)[0] == (0., 4.2)       # the average 4.1 would cut cue 2 short


def test_keep_windows_without_spans_is_the_whole_clip():
    assert trim.keep_windows(SPARSE, [], 33.) == [(0., 33.)]


@pytest.mark.parametrize('spans', [[(1, 2)], [(4, 5)], [(2, 3), (4, 4)], [(2, 4), (3, 3)], [(2, 9)], [(0, 2)]])
def test_keep_windows_rejects_edge_touching_overlapping_and_out_of_range_spans(spans):
    with pytest.raises(ValueError):
        trim.keep_windows(SPARSE, spans, 33.)


def test_keep_windows_rejects_windows_that_cross():
    lines = cues((0., 5., 'a'), (1., 6., 'hole'), (2., 3., 'b'), (9., 10., 'c'))      # b starts before a ends
    with pytest.raises(ValueError):
        trim.keep_windows(lines, [(2, 2)], 11.)


@pytest.mark.parametrize('gap_before, gap_after', [(5., 5.), (0.2, 0.3), (0., 0.)])
def test_keep_windows_cut_points_equal_those_of_cut_window_ms(gap_before, gap_after):
    first = cues((1., 2., 'a'), (2.5, 4., 'b'))
    start = 4. + gap_before
    hole = [(start, start + 2., 'x'), (start + 2.1, start + 3., 'y')]
    after = start + 3. + gap_after
    lines = cues(*[(a, b, t) for a, b, t in [(c['start_s'], c['end_s'], c['text']) for c in first] + hole],
                 (after, after + 2., 'c'), (after + 2.5, after + 4., 'd'))
    windows = trim.keep_windows(lines, [(3, 4)], after + 5.)
    assert round(windows[0][1] * 1000) == comp.cut_window_ms(lines, 1, 2)[1]
    assert round(windows[1][0] * 1000) == comp.cut_window_ms(lines, 5, 6)[0]


# --- moved -------------------------------------------------------------------------------------------------------
def test_moved_cues_before_the_hole_stay_and_cues_after_it_move_earlier_by_the_hole():
    windows = trim.keep_windows(SPARSE, [(3, 3)], 33.)
    out = trim.moved(SPARSE, [(3, 3)], windows)
    assert [(c['start_s'], c['end_s'], c['text']) for c in out] == [
        (0., 2., '开头'), (2.5, 4., '正题'), (4.47, 6.47, '继续正题'), (14.47, 16.47, '结尾')]


def test_moved_two_holes_move_the_later_cues_by_both():
    lines = cues((0., 2., 'a'), (10., 12., 'b'), (20., 22., 'c'), (30., 32., 'd'), (40., 42., 'e'), (50., 52., 'f'))
    spans = [(2, 2), (4, 4)]
    windows = trim.keep_windows(lines, spans, 60.)
    out = trim.moved(lines, spans, windows)
    assert [c['text'] for c in out] == ['a', 'c', 'e', 'f']
    # c is the first cue after the first hole (window 2 starts at 19.88, window 1 is 2.35 long)
    assert (out[1]['start_s'], out[1]['end_s']) == (round(2.35 + 0.12, 3), round(2.35 + 2.12, 3))
    # e: 40 - 39.88 + (2.35 + 2.47) = 4.94
    assert out[2]['start_s'] == pytest.approx(4.94, abs=1e-9)
    assert out[3]['start_s'] == pytest.approx(50. - 39.88 + 4.82, abs=1e-9)


def test_moved_renumbers_ids_from_one_and_keeps_other_keys():
    lines = [dict(c, speaker='李豆沙', id=100 + i) for i, c in enumerate(SPARSE)]
    out = trim.moved(lines, [(3, 3)], trim.keep_windows(lines, [(3, 3)], 33.))
    assert [c['id'] for c in out] == [1, 2, 3, 4]
    assert all(c['speaker'] == '李豆沙' for c in out)
    assert 'id' in lines[0] and lines[0]['id'] == 100          # the input is not touched


def test_moved_without_spans_returns_the_same_times():
    out = trim.moved(SPARSE, [], [(0., 33.)])
    assert [(c['start_s'], c['end_s']) for c in out] == [(c['start_s'], c['end_s']) for c in SPARSE]


def test_moved_cue_not_whole_inside_a_kept_window_is_an_error():
    with pytest.raises(ValueError, match=r'cue 2 \(2.5..4.0\)'):
        trim.moved(SPARSE, [(3, 3)], [(0., 3.), (19.88, 33.)])


def test_moved_cue_between_the_windows_is_an_error():
    with pytest.raises(ValueError, match='cue 3'):
        trim.moved(SPARSE, [], [(0., 9.), (19.88, 33.)])


# --- source_pieces -----------------------------------------------------------------------------------------------
def test_source_pieces_are_milliseconds_of_the_source_recording():
    assert trim.source_pieces([(0., 4.35), (19.88, 33.)], 289041) == [(289041, 293391), (308921, 322041)]



def load(cid):
    return json.loads((FIXTURES / f'{cid}.json').read_text(encoding='utf-8'))


def test_real_clip_1529440_agreed_and_guarded_spans_are_exactly_what_reviewer_deleted_and_the_clock_stays_consistent():
    clip = load('semantictalk_1529440_1694570')
    lines, n = clip['cues'], len(clip['cues'])
    first, second = (trim.parse_answer(clip['answers'][k], n) for k in ('P1', 'P2'))
    spans, vetoed = trim.guard(trim.agreed(first, second), lines, thanks_words=THANKS,
                               clip_len_s=clip['clip_len_s'], min_keep_s=10.)
    assert vetoed == [] and spans == [(51, 57)]
    assert {x for a, b in spans for x in range(a, b + 1)} == set(clip['reviewer_deleted'])

    windows = trim.keep_windows(lines, spans, clip['clip_len_s'])
    out = trim.moved(lines, spans, windows)
    assert len(out) == n - len(clip['reviewer_deleted'])
    assert all(a['end_s'] <= b['start_s'] for a, b in zip(out, out[1:]))
    assert out[-1]['end_s'] <= sum(end - start for start, end in windows)
    assert [c['text'] for c in out[49:51]] == [lines[49]['text'], lines[57]['text']]
    assert windows == [(0., 106.63), (121.87, clip['clip_len_s'])]


# --- events_text -------------------------------------------------------------------------------------------------
def test_events_text_lines_are_time_kind_sender_content_by_time():
    records = [{'t_s': 65.04, 'type': 'gift', 'sender': '甲', 'text': '', 'gift_name': '小花花', 'count': 3},
               {'t_s': 12.0, 'type': 'sc', 'sender': '乙', 'text': '加油'},
               {'t_s': 30.26, 'type': 'guard', 'sender': '丙', 'text': '', 'gift_name': '舰长', 'count': 1},
               {'t_s': 40.0, 'text': '普通弹幕'}]                           # no type: an ordinary comment, not a record
    assert trim.events_text(records) == '12.0s SC 乙 加油\n30.3s 上舰 丙 舰长×1\n65.0s 礼物 甲 小花花×3'


def test_events_text_gift_with_text_and_default_count_and_missing_sender():
    assert trim.events_text([{'t_s': 1.0, 'type': 'gift', 'text': '谢谢', 'gift_name': '灯牌'}]) == '1.0s 礼物  灯牌×1 谢谢'


def test_events_text_is_empty_without_records_so_the_prompt_says_none():
    assert trim.events_text([]) == '' and trim.events_text([{'t_s': 1.0, 'text': '弹幕'}]) == ''
    assert '（无记录）' in trim.prompt('h', trim.events_text([]), cues((0., 1., 'a')))


# --- moved_bcut --------------------------------------------------------------------------------------------------
WINDOWS = [(0.0, 10.0), (14.0, 30.0)]


def test_moved_bcut_moves_whole_cues_cuts_edge_cues_and_drops_the_ones_in_the_hole():
    bcut = cues((1., 3., '前'), (9., 11., '跨出口'), (11., 13., '洞里'), (13., 15., '跨入口'), (20., 22., '后'))
    bcut[0]['extra'] = 'kept'
    out = trim.moved_bcut(bcut, WINDOWS)
    assert out == [{'id': 1, 'start_s': 1.0, 'end_s': 3.0, 'text': '前', 'extra': 'kept'},
                   {'id': 2, 'start_s': 9.0, 'end_s': 10.0, 'text': '跨出口'},        # cut to the window end
                   {'id': 3, 'start_s': 10.0, 'end_s': 11.0, 'text': '跨入口'},       # cut to the window start, moved up
                   {'id': 4, 'start_s': 16.0, 'end_s': 18.0, 'text': '后'}]


def test_moved_bcut_of_nothing_is_empty_and_whole_clip_window_changes_nothing():
    assert trim.moved_bcut([], WINDOWS) == []
    bcut = cues((1., 3., 'a'), (4., 6., 'b'))
    assert [(c['start_s'], c['end_s']) for c in trim.moved_bcut(bcut, [(0.0, 10.0)])] == [(1.0, 3.0), (4.0, 6.0)]


# --- shifted -----------------------------------------------------------------------------------------------------
def test_shifted_moves_every_cue_earlier_and_keeps_other_keys():
    out = trim.shifted([{'id': 7, 'start_s': 2.0, 'end_s': 3.5, 'text': 'a', 'speaker': 'X'}], 0.5, 10.0)
    assert out == [{'id': 7, 'start_s': 1.5, 'end_s': 3.0, 'text': 'a', 'speaker': 'X'}]


def test_shifted_clamps_an_edge_within_the_tolerance_and_refuses_beyond_it():
    out = trim.shifted([{'start_s': 0.0, 'end_s': 1.0, 'text': 'a'}, {'start_s': 9.0, 'end_s': 10.07, 'text': 'b'}], 0.03, 10.0)
    assert [(c['start_s'], c['end_s']) for c in out] == [(0.0, 0.97), (8.97, 10.0)]    # -0.03 -> 0, 10.04 -> 10.0
    with pytest.raises(ValueError, match='cue 1 .*outside the cut'):
        trim.shifted([{'start_s': 0.0, 'end_s': 1.0, 'text': 'a'}], 0.2, 10.0)
    with pytest.raises(ValueError, match='cue 2 '):
        trim.shifted([{'start_s': 1.0, 'end_s': 2.0, 'text': 'a'}, {'start_s': 9.0, 'end_s': 10.2, 'text': 'b'}], 0.0, 10.0)


# --- carried_doubts ----------------------------------------------------------------------------------------------
from src.autoslice.manual_lane import doubt_policy

CARRY_FINAL = cues((0., 2., '开头'), (3., 5., '正题甲'), (6., 8., '谢谢小花'), (9., 11., '谢谢你'), (12., 14., '正题乙'), (15., 17., '结尾'))
CARRY_SPANS = [(3, 4)]
CARRY_WINDOWS = [(0.0, 5.5), (11.5, 18.0)]
CARRY_NEW = cues((0., 2., '开头'), (3., 5., '正题甲'), (6.5, 8.5, '正题乙'), (9.5, 11.5, '结尾'))


def doubt(did, cue, **kw):
    start = cue[0]
    return {'id': did, 'cue_id': None, 'start_s': start, 'end_s': cue[1], 'current': 'cur', 'state': 'KEEP',
            'final_text': 'fin', 'confidence': '高', 'affects_meaning': False, 'basis': f'依据{did}', **kw}


def carry(doubts, **kw):
    args = dict(final_cues=CARRY_FINAL, bcut_cues=[], clip_start_ms=0, spans=CARRY_SPANS, windows=CARRY_WINDOWS,
                new_cues=CARRY_NEW)
    return trim.carried_doubts(doubts, **(args | kw))


def test_carried_doubts_drop_those_on_cut_cues_and_move_the_others_with_their_cue():
    rows = carry([doubt('a', (3.1, 4.9)), doubt('b', (6.1, 7.9)), doubt('c', (9.2, 10.8)), doubt('d', (12.1, 13.9))])
    assert [(r['id'], r['start_s'], r['end_s']) for r in rows] == [('trim-a', 3.0, 5.0), ('trim-d', 6.5, 8.5)]
    assert all(r['cue_id'] is None and r['source'] == 'trim' for r in rows)


def test_carried_doubt_keeps_text_state_alternatives_and_basis_of_the_original():
    orig = doubt('a', (3.1, 4.9), current='原', final_text='改', state='ADOPT', basis='理由',
                 candidates=[{'text': '原'}, {'text': '改'}, {'text': '另'}, {'text': '另'}], speaker='X', speaker_confidence='高')
    row = carry([orig])[0]
    assert (row['current'], row['final_text'], row['state'], row['basis'], row['alternatives']) == ('原', '改', 'ADOPT', '理由', ['原', '另'])
    assert (row['speaker'], row['speaker_confidence']) == ('X', '高')
    assert carry([dict(orig, alternatives=['甲'])])[0]['alternatives'] == ['甲']


def test_carried_doubt_that_holds_the_package_back_still_does_and_the_others_still_do_not():
    originals = [doubt('u', (3.1, 4.9), state='UNRESOLVED'),                               # undecided, meaning effect false
                 doubt('l', (3.2, 4.8), confidence='低', affects_meaning=None),            # low, effect unknown: holds
                 doubt('m', (3.3, 4.7), confidence='低', affects_meaning=True),            # low, changes the meaning: holds
                 doubt('f', (3.4, 4.6), confidence='低', affects_meaning=False),           # low, free
                 doubt('k', (12.1, 13.9)),                                                  # decided
                 doubt('x', (12.2, 13.8), state='UNRESOLVED', affects_meaning=True),       # undecided and meaning: holds
                 doubt('z', (6.1, 7.9), confidence='低', affects_meaning=True)]            # on a cut cue: gone
    rows = carry(originals)
    old_hold = {f"trim-{d['id']}" for d in originals[:-1] if doubt_policy.is_hold(d)}
    assert old_hold == {'trim-l', 'trim-m', 'trim-x'}
    assert {r['id'] for r in rows if doubt_policy.is_hold(r)} == old_hold
    assert {r['id'] for r in rows} == {'trim-u', 'trim-l', 'trim-m', 'trim-f', 'trim-k', 'trim-x'}
    assert all((r['confidence'], r['affects_meaning']) in {('低', True), ('高', False)} for r in rows)


def test_carried_doubt_on_no_final_cue_follows_its_own_time_and_one_in_the_hole_is_dropped():
    rows = carry([doubt('gap', (5.1, 5.4)), doubt('hole', (8.4, 8.9)), doubt('late', (11.6, 11.9), dropped=True)])
    assert [(r['id'], r['start_s'], r['end_s'], r.get('dropped')) for r in rows] == [
        ('trim-gap', 5.1, 5.4, None), ('trim-late', 5.6, 5.9, True)]
    assert [r['start_s'] for r in carry([doubt('late', (11.6, 11.9))], shift_s=0.25)] == [5.35]


def test_carried_doubt_that_owns_two_split_lines_spans_both():
    final = cues((0., 2., '开头'), (3., 4., '甲一'), (4., 5., '甲二'), (6., 8., '谢谢'), (12., 14., '正题乙'))
    new = cues((0., 2., '开头'), (3., 4., '甲一'), (4., 5., '甲二'), (6., 8., '正题乙'))
    row = trim.carried_doubts([doubt('s', (3.0, 5.0))], final_cues=final, bcut_cues=[], clip_start_ms=0, spans=[(4, 4)],
                              windows=[(0.0, 5.5), (11.5, 14.5)], new_cues=new)[0]
    assert (row['start_s'], row['end_s']) == (3.0, 5.0)


def test_carried_doubts_refuse_inconsistent_inputs():
    with pytest.raises(ValueError, match='new cues'):
        carry([doubt('a', (3.1, 4.9))], new_cues=CARRY_NEW[:-1])
    with pytest.raises(ValueError, match='no time'):
        carry([{'id': 'n', 'cue_id': 99}])
