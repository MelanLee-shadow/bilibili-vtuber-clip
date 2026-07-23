# Independent review request: July 10 autoslice repeated failures

> **Historical consultation input.** It records the 2026-07-10 question, not
> current pipeline behavior or an executable specification. Current subtitle
> authority is [../pipeline/40-subtitle-text.md](../pipeline/40-subtitle-text.md).

Act as an adversarial systems reviewer. I am fixing an unattended Bilibili VTuber slicing pipeline. Please challenge the proposed root causes and give a concrete, implementation-ready design. Do not merely restate the incidents.

## User-visible incidents

1. A talk clip begins by reading the danmaku `等小李什么时候来看恋青呢`, but subtitles became `等小室什么时候来看`, followed by a reply using `恋死`. The response must preserve the referent `恋青`. Later the streamer separately reads `恋死看吗`; the final `吗` was dropped.
2. In the same clip, `倒是` became `到时`; current BanG Dream anime `梦限大` repeatedly became `Mujica`; clear audio `指神人的神` became `指神金的神`; Japanese `wakuwaku` became phonetic Chinese `哇哭哇哭`; `立希` became `Saki` even though both are franchise names.
3. Another clip thanks `十麻乃` for an SC. Exact source text is `如果能唱的到想点首小城夏天，唱不到就算了`, but final subtitles regressed to ASR-like `假如我能唱到...`.
4. A selected talk clip failed boundary repair without a delivery. Six song attempts produced zero deliveries.

## Verified implementation facts

- The first recording has a zero-byte danmaku XML but a healthy sibling JSONL containing the exact danmaku. The runner rejects empty XML and never passes JSONL independently. The package producer only loads JSONL/SC inside the `if XML exists` branch. Ordinary danmaku context is limited to 1 second before the cut; the relevant message appeared about 8 seconds before the padded piece.
- The SC XML/JSONL was healthy and AGY produced wording close to the exact SC. A later CPA reconciliation prompt hard-codes BCUT/ASR as the default authority and forbids adopting AGY changes to ordinary wording or sentence structure, so it regressed the SC quote.
- The correction stages fail open and a clip can be delivered without proving that matched authoritative chat text survived into the final SRT.
- Static glossary contains `小室`, `恋死`, and `Mujica`, but lacks `恋青`, `梦限大`, `立希`, and `wakuwaku`, causing a stale-lexicon prior. The current official franchise context is the July 2026 anime `バンドリ！ ゆめ∞みた` / `夢限大みゅーたいぷ`.
- The boundary self-repair searches only the already-cut context, up to 25 seconds after the current closure. If no clean closure exists there, the runner marks the item terminal and never widens source context.
- Five complete-song retries found the correct song/LRC family with high recall, but rejected it because multiple provider/cover LRC variants tied as separate identities; therefore audio-LRC alignment, host voice proof, recut, subtitles, and burn never ran. Another song used only an ASR preview to form LRC queries even though its selected hook already named the song correctly.

## Proposed direction to review

1. Introduce typed `ChatEvidence` parsed independently from XML or JSONL, using recording-start time rather than earliest event time. Keep negative pre-roll offsets. Use a 30-second danmaku pre-context and longer SC backlog.
2. Add a deterministic authority stage after ASR/LLM correction: align likely read-aloud cue windows to exact danmaku/SC text by timing plus text similarity, replace only proven quoted spans, preserve cue timing, emit a hash-bound audit, and fail closed when a high-confidence matched quote does not survive final subtitles. Do not blindly replace all nearby speech; replies are separate discourse turns.
3. Add a discourse/entity pass so an immediate response keeps the entity from the read message unless audio/context proves a contrast. Exact chat text outranks ASR for the read span; acoustic evidence plus discourse outranks a static glossary for replies.
4. Replace a flat glossary with a time-aware, source-backed term snapshot: canonical term, aliases/phonetics, franchise/entity graph, valid/active dates, recency, source URL/date, and negative/confusable candidates. Refresh bounded official-interest sources daily; cache results. Runtime ranking combines phonetic likelihood, current time, topic/entity context, and discourse. No global blind `Mujica -> 梦限大` replacement and no unbounded per-cue web search.
5. Let boundary repair widen the original-source post-context once or twice, then rerun transcription/audit before failing closed.
6. Cluster materially equivalent LRC variants by normalized title plus lyric-content similarity before applying the identity margin; seed queries from both the semantic hook and ASR preview. Keep full-boundary, live-performance, and host-voice proof gates unchanged.

## Questions

Please return:

1. A precedence lattice for exact chat, audio ASR, discourse, time-aware terms, static glossary, and human correction.
2. A safe matching/alignment algorithm for exact danmaku/SC read-aloud spans, including confidence thresholds, multi-cue splitting, and fail-closed conditions.
3. A bounded state machine for self-healing talk boundaries and songs, with retry budgets and terminal states.
4. Risks in the proposed time-aware terminology design, especially stale news, same-franchise confusables (`立希` vs `Saki`, `梦限大` vs `Mujica`), and prompt-injection-like chat text.
5. The smallest high-value regression suite that would have caught every incident above.
6. Any premise you reject, with a more defensible alternative.

Be specific enough that an engineer can turn the result into code and tests. Separate `confirmed`, `claimed`, and `unknown` where evidence is incomplete.
