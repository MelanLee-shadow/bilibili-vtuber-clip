# 2026-07-10 subtitle authority / self-healing review

> **Historical consultation result.** Preserve it as evidence; do not treat its
> old code paths, open findings or model/runtime claims as current. Current
> rules live in [../pipeline/40-subtitle-text.md](../pipeline/40-subtitle-text.md)
> and [../pipeline/41-semantic-repair.md](../pipeline/41-semantic-repair.md).

## Consultation provenance

- Visible model label: `Pro`
- Thread: <https://chatgpt.com/c/6a517799-4644-83ea-a2d3-64479f8661e2>
- Durable run record: `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-07-10T22-51-19-591Z-9ec4c5e3a9f29444.json`
- Readback state: `read_complete`

## Material review findings accepted

1. The recurring defect is non-monotone evidence handling: later generative
   stages were allowed to overwrite stronger structured or human evidence.
2. Structured chat is exact text authority only for a demonstrably read-aloud
   span. Proximity alone is insufficient. The implementation therefore requires
   time/text alignment plus independent support from a pre-CPA audio-derived
   transcript, and records that this is a proxy rather than token-level acoustic
   posterior proof.
3. Matched spans must become immutable locks and be re-audited against the final
   text SRT, speaker SRT/ASS binding, and burned artifact hashes.
4. Viewer text is untrusted data: it is sanitized, cannot issue instructions,
   and is never allowed to fetch URLs.
5. A reply may inherit an entity from the message being answered, but must not
   copy the whole message. This distinction is now stated explicitly in the
   correction principles and CPA reconciliation contract.
6. Recency priors must be evaluated as of the recording date, not the later
   processing date. Source publication dates are filtered to prevent future
   leakage.
7. Multiple provider LRC files for the same song are evidence variants, not
   automatically different song identities. The immediate repair clusters a
   same-title lyric family by content similarity; a fuller SongWork/LyricFamily
   registry remains a future hardening direction.
8. Boundary recovery must widen the original source context, remain bounded,
   and rerun the complete finalization chain instead of extending a derivative.

## Deliberately not overclaimed

- The matching thresholds are regression-tested engineering defaults, not
  calibrated probabilities.
- Raw ASR/AGY agreement is an audio-derived transcript proxy, not forced
  alignment or token posterior evidence.
- `timely_terms.json` is a bounded, source-backed candidate snapshot. It does
  not yet implement a fully autonomous news crawler, and it never overrides
  contradictory audio or structured source text.
