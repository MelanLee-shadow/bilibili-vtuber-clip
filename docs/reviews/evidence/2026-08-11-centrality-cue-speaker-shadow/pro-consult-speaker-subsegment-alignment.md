# ChatGPT Pro consultation: cue-to-speaker-subsegment alignment

We need a narrow production architecture decision for a Chinese VTuber clipping pipeline. Please act as a skeptical senior reviewer and specify a deterministic, fail-closed contract. The operator-approved subtitle and speaker truth is already fixed; do not re-decide the words, names, or speakers.

## Concrete incident

- Candidate `auto_223750_578_734` has an operator-reviewed plain SRT with 52 cues.
- Its operator-reviewed speaker-final SRT has 55 rows because three mixed-speaker cues are each split into ordered A/B subsegments at reviewed within-cue boundaries.
- The speaker-final SRT is hash-bound and uses only `[李豆沙]` and `[连线]` labels.
- The current production helper accepts speaker evidence only when `len(speaker_rows) == len(plain_cues)` and every row text equals its same-index plain cue text after whitespace compaction.
- Therefore the valid 55-row speaker truth is discarded as `None`; source-fact review sees `speaker_transcript_sha256=null` and incorrectly treats the approved title/addressee claims as unsupported.
- We must not bypass the source-fact gate or weaken it with a generic “close enough” comparison.

## Relevant current behavior

The plain transcript sent to the judge must remain byte-identical: one text line per non-empty reviewed cue. A second, speaker-labelled transcript is optional. Today it renders numbered rows such as:

```text
1 [连线] 所以你以后别再被骗了知道吗
2 [李豆沙] 大家好我是李豆沙
```

For this candidate, one plain cue can legitimately correspond to multiple ordered speaker subsegments, for example a cue whose reviewed text is the concatenation of:

```text
[李豆沙] 莉亚
[连线] 活着
```

The source-fact judge needs the speaker/addressee evidence without collapsing a mixed cue to one speaker and without changing the plain transcript.

## Existing authority and constraints

- Both SRTs are immutable candidate-scoped human truth, each bound by exact SHA-256 and source interval.
- Plain cue timing is the reviewed delivery cue grid.
- Speaker-final segments carry reviewed start/end times, labels, text, and order. A mixed cue may contain two or more segments.
- No fuzzy semantic matching, edit-distance guessing, label voting, or model call is allowed in the aligner.
- Whitespace-only normalization is currently allowed for text equality; broad punctuation or character normalization is not.
- `uniform_host` candidates legitimately have no speaker-final SRT and remain on the existing `None`/unverifiable path.
- If present speaker evidence is malformed, drifted, ambiguous, cross-cue, or incomplete, the aligner must reject it rather than fabricate attribution.
- Historical one-row-per-cue packages should keep working byte-for-byte.

## Decision questions

1. What exact deterministic algorithm should align N reviewed plain cues to M reviewed speaker subsegments when M may exceed N?
2. Should timing containment, positive overlap, text concatenation, or a combination be authoritative? Specify the order of checks and exact failure conditions.
3. How should boundary equality and tiny timestamp rounding differences be treated? Recommend a concrete tolerance, if any, and explain why it cannot admit cross-cue ambiguity.
4. How should empty cues, gaps, overlapping speaker segments, segments crossing cue boundaries, duplicated text, and zero-duration segments be handled?
5. What should the rendered speaker transcript look like so evidence can cite both the parent cue and ordered subsegments while preserving backward compatibility for one-segment cues?
6. Which hashes/provenance should the source-fact receipt bind: the speaker-final SRT bytes, an alignment document, the rendered transcript, or all of them?
7. Give a minimal safe migration plan and a test matrix, including:
   - legacy 1:1 success;
   - one cue split into A/B success;
   - several adjacent mixed cues;
   - whitespace-only text equivalence;
   - punctuation/character drift rejection;
   - gap/overlap/cross-boundary/ambiguous ownership rejection;
   - speaker-final hash drift rejection;
   - real 52-cue/55-segment candidate acceptance;
   - `uniform_host` no-speaker behavior unchanged.

Please provide a concrete schema/notation, pseudocode, invariants, and ranked P0/P1 implementation plan. Explicitly call out any premise you reject. The answer should optimize for auditability and zero false attribution, not maximum coverage.
