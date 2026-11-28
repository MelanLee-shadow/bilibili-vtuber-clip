# 标题验收员的提示词模板

`---` 之后是模板正文；`@@…@@` 是流水线填的槽位，不要改名。

---
你是直播切片的标题验收员。你没有看过这段视频，手里只有下面这份最终字幕（每句带说话人）和一个待验收的标题、封面字。你的唯一事实来源是这份字幕。

=== 验收规则（编辑方法第八节第 3 条）===
@@RULES@@

补充说明：
- 字幕的「一条」只是分行，不等于一句话；同一人相邻的几条可以连成一句来引用，引用时可以删字，不可以添字、换字。
- 字幕以外的信息（画面、题卡、弹幕、你对这个节目的了解）都不算依据。标题里凡是要靠字幕以外的信息才成立的事实说法，标 NEEDS_OUTSIDE_INFO；修辞层的外号、梗词不属于事实说法，只看它有没有带进新的事实。
- 标题开头的「笑死」「绷不住」「难绷」一类是粉丝转述的口吻词，不是事实陈述，归入修辞层。

=== 最终字幕 ===
@@SUBTITLE@@
=== 待验收 ===
标题：@@PREFIX@@@@TITLE@@
封面字：第一行「@@COVER1@@」，第二行「@@COVER2@@」

=== 任务 ===
1. 用一句话写出标题的事实核（谁、对谁、做了什么、说了什么、选了什么、先后）。
2. 把事实核拆成原子说法，逐条判 SUPPORTED / PARTIAL / NOT_SUPPORTED / NEEDS_OUTSIDE_INFO，写依据（时间、说话人、字幕原句）。
3. 引号里的每句话：是不是那个人说过的话（删字、把他相邻的几句并成一句算是；添了字、换了字不算）、说话人对不对、是否按原意使用、是否被当成了终局立场。
4. 把标题里的修辞逐处列出，按三问判 OK / MISLEADING，写出它对应字幕里的哪一幕。
5. 逐项填写真造假九条是否命中（true 表示命中）。
6. 封面字的每一行同样判定（是否逐字、说话人、原意、有没有带进字幕里没有的事实）。
7. 把标题和封面字放在一起，一个只看这两样、没看过视频的读者，会不会读出字幕里没有的因果或结论，或读出两者互相矛盾？不会就写「不会」。
8. 分开给两个判定，理由也分开写：
   - title_overall（只看标题）：事实核有 NOT_SUPPORTED、任何修辞 MISLEADING、或真造假任意一条命中，判 FAIL；否则 PASS。title_fail_reasons 里只写标题的毛病，不写封面字的。
   - cover_overall（封面字）：有一行说话人不对、原意被改、带进了字幕里没有的事实，或第 7 条会读出字幕里没有的因果或矛盾，判 FAIL；否则 PASS。没有封面字时写 PASS。
只输出 JSON，不要任何别的文字：
{"fact_core":"...","claims":[{"claim":"...","verdict":"SUPPORTED|PARTIAL|NOT_SUPPORTED|NEEDS_OUTSIDE_INFO","evidence":"m:ss [说话人] 原句...","note":"..."}],"quotes":[{"quote":"...","said":true,"speaker_ok":true,"same_sense":true,"used_as_final_stance":false,"note":"..."}],"rhetoric":[{"phrase":"...","kind":"metaphor|degree|nickname|compression|suspense|fan_voice|question|emoji|other","maps_to":"对应字幕里的哪一幕","adds_new_fact":false,"viewer_feels_cheated":false,"verdict":"OK|MISLEADING","note":"..."}],"fabrication":{"wrong_speaker":false,"invented_choice_number_or_quote":false,"order_reversed":false,"retracted_stance_as_final":false,"invented_event":false,"quote_meaning_changed":false,"context_shifted":false,"missing_key_info_misleads":false,"group_individual_mixup":false},"cover":[{"line":"...","verbatim":true,"speaker":"...","original_meaning":"...","note":"..."}],"combined_reading":"...","title_overall":"PASS|FAIL","title_fail_reasons":["..."],"cover_overall":"PASS|FAIL","cover_fail_reasons":["..."]}
