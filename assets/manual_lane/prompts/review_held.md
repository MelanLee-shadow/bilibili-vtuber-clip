# 指定疑点的最终处置

只处置材料中列出的「本次只处置的疑点 ID」；它们可能是既有复审此前未判的缺项，也可能是复审后仍保留、补充了加长窗口听写的疑点。不重新全片找错，不改变其他疑点、标题或标签。原有候选、来源、basis 和选择是可重新判断的先前结论。结合提供的全片字幕、说话人、现有上下文及可能提供的新听写判断，不声称听过或看过未提供的音视频。

场次：{session}{compilation_context}

唯一不放行的规则：{hold_rule}
- 对每个指定稳定 id 给出 doubt_decisions：在当前文字或必要修改之后，剩余不确定性的 confidence、affects_meaning 和具体 reason。文字适合时可以保持不变；不预设解除或保留 hold。
- 若确有必要改该疑点对应的字幕，在 fixes.subtitles 用最终字幕序号写整句；只改指定疑点对应的句子。说话人修改仍遵守以下规则：
{speaker_item}
- 与字幕裁决同一条代词规则：{pronoun_rule}
- 既有封面图判读只是提供的历史记录，本次文字请求并未独立看图；没有音视频时不能声称新听或核验切点。

只输出一个 JSON 对象：
{"fixes": {"subtitles": [{"cue_id": 序号, "text": "改后的整条字幕文字", "reason": "为什么这样改，依据是什么"}], "speakers": [{"cue_id": 序号, "speaker": "{host} 或 {guest}", "evidence": "自称 或 只能出自一方", "quote": "本句中的逐字证据"}]}, "doubt_decisions": [{"id": "指定的稳定 id", "confidence": "高 或 低", "affects_meaning": true 或 false, "reason": "剩余不确定性及可核对依据"}], "notes": [], "review_md": "仅对指定疑点的处置和依据"}

没有必要修改的项写空数组。材料：
{materials}
