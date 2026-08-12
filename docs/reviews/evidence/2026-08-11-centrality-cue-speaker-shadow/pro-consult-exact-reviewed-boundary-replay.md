# ChatGPT Pro consultation: deterministic boundary authority for exact reviewed redelivery

We need a narrow production architecture decision for a Chinese VTuber clipping pipeline. Act as a skeptical senior reviewer. Optimize for deterministic replay, auditability, and never cutting operator-reviewed content. Do not re-decide the candidate's words, speakers, title, or story quality.

## Concrete incident

Candidate `auto_223750_578_734` has:

- one immutable source recording, SHA-256 `66e9ec0707fe8d7f38cf8a552e863dafbba34fb7eadaece496adb30d04b1ad1e`;
- an operator-reviewed delivery interval `[577780, 735090)` ms;
- an operator-reviewed SRT, SHA-256 `6d79fdab105d4c7b5edffec66fe526aa01839de342a7c96b8784f4d0100a8062`;
- `exact_interval_replay=true` and full text/speaker ownership;
- 52 reviewed cues; the final cue is `[154210, 157250)` and the reviewed video interval is 157310 ms long, leaving a 60 ms video-only tail.

Two clean productions from the same source and same semantic spec received different BCUT/fresh-ASR grids:

1. A successful run chose closure text `我以为都能吃掉啊`, source-local cue end 167000 ms, then a 60 ms tail, producing absolute interval end 735090 ms. Exact reviewed SRT replay passed.
2. A later run chose `我以为都能吃掉`, cue end 166120 ms, then a 400 ms tail, producing absolute end 734550 ms. This cut the final reviewed cue `[731990,735030)` and correctly failed with `REDELIVERY_BASELINE_CUE_CUT_BY_NEW_BOUNDARY`.
3. A subsequent clean retry happened to produce the first endpoint again and passed. That lucky retry is not an acceptable systemic fix.

The source/padded media bytes were identical. Only remote ASR/correction cue geometry drifted.

## Existing mechanisms and why they did not solve this incident

### Exact-reviewed terminal projection

The project has `reviewed_exact_interval_terminal_projection`, signed only after source retrieval and source hash verification. It currently requires:

- the last reviewed subtitle cue to end exactly at the reviewed interval duration;
- the fresh closure cue to end at most 250 ms before the endpoint;
- the next fresh cue to start exactly there and uniquely cross the reviewed endpoint.

This candidate has a legitimate 60 ms video-only tail after the last reviewed cue, so authority construction returns `BASELINE_TERMINAL_END_MISMATCH`. The bad fresh grid was also 940 ms before the reviewed endpoint, beyond the 250 ms projection drift.

### Frozen boundary receipt

The project also has a hash-bound frozen source/final boundary receipt. It validates:

- pristine record bytes;
- source SHA and basename;
- exact reviewed SRT SHA and absolute interval;
- selection hook and scorecard;
- source and final boundary PASS receipts and endpoint hashes.

However, projection to a current fresh grid additionally requires one eligible current cue whose exact closure-text SHA and effective end millisecond equal the frozen closure. The bad grid had neither, so the receipt loaded but was not applied; the code silently fell back to a new live semantic review.

### Exact subtitle replay

Once the media interval already contains the reviewed interval, exact subtitle replay permits a bounded video-only tail up to 400 ms. That gate is safe but runs too late to prevent the boundary resolver from cutting the reviewed interval first.

## Constraints

- Never hand-edit a frozen record, boundary receipt, recut, or package.
- Never turn an ordinary semantic endpoint into `exact_source_pin` merely because an old public package had that endpoint.
- Here, however, the current candidate has candidate-scoped human-reviewed SRT bytes and an explicit exact reviewed source interval. The distinction between text ownership and exact endpoint authority must be explicit.
- Fresh ASR remains useful as a semantic witness, but remote ASR segmentation is not deterministic enough to be the sole replay identity.
- Any new authority must bind source bytes, candidate, reviewed SRT bytes, absolute interval, selection authority, and policy version; must fail closed on drift; and must not silently generalize to ordinary first-pass candidates.
- Existing successful packages and one-cue-per-row historical candidates must remain valid.
- A production fix should prevent random retries from deciding whether reviewed content survives.

## Decision questions

1. Which surface should be the deterministic authority after a candidate-scoped human has reviewed the whole SRT and its exact source interval?
   - reviewed interval endpoint itself;
   - frozen final cue grid/ASR grid;
   - frozen verified recut media;
   - or a layered combination?
2. Does `exact_interval_replay=true` plus exact source interval and whole-SRT ownership legitimately authorize the media endpoint, or should a separate explicit `exact_endpoint_reviewed=true` grant be required? Explain the epistemic distinction.
3. Should the current terminal-projection contract be extended to allow a bounded video-only tail (here 60 ms)? If yes, state the exact maximum and required proof. Should the 250 ms fresh-grid drift remain, be removed when endpoint authority is explicit, or be replaced by a different witness check?
4. When the fresh ASR grid does not contain the same closure text/end, should production:
   - replay a frozen semantic verdict against a canonical reviewed grid;
   - bypass source-grid boundary selection but still run final-delivery semantic review on reviewed SRT;
   - resume from a hash-bound frozen recut;
   - or block for a new human endpoint review?
5. What exact typed schema and hashes should a P0 authority/receipt contain? Specify what is stable across WSL→free relocation and what must be revalidated current-side.
6. Give precise failure conditions for:
   - last reviewed cue ending before the video endpoint;
   - fresh ASR ending earlier/later;
   - next-topic speech crossing the reviewed endpoint;
   - source/hash/interval drift;
   - selection hook or scorecard change;
   - subtitle or speaker truth change;
   - policy version change.
7. Rank the safest minimal implementation paths:
   - narrow extension of current terminal projection;
   - canonical reviewed-grid frozen verdict replay;
   - hash-bound frozen-recut resume;
   - deterministic ASR-result caching;
   - explicit new human endpoint grant.
8. Provide a test matrix including this exact 60 ms tail / 940 ms fresh-grid drift incident, same source with three different ASR grids, WSL→free relocation, tampered source/SRT/record/spec, next-topic crossing, and historical legacy behavior.

Please reject any unsafe premise explicitly. End with a concrete P0/P1 recommendation and pseudocode for the decision gate.
