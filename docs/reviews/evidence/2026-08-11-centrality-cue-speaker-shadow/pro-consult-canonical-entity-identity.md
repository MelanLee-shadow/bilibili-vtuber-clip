# ChatGPT Pro consultation: canonical person identity vs subtitle surface

We need an architecture decision for a production Chinese VTuber clipping pipeline. Please act as a skeptical senior reviewer. The immediate two clips already have exact human corrections and will be repaired independently; your answer should govern the reusable/systemic fix, not delay or weaken those candidate-scoped truths.

## Concrete operator truth from Ivan (2026-08-11)

1. In one single-host clip, a line around 0:16 must be `我对xxsk说`; every other occurrence of `星汐` in this clip must be `xxsk`. Ivan says that **in this context** `xxsk` is the canonical name for 星汐Seki, analogous to `lds` as a nickname/surface for 李豆沙. All unmentioned subtitle text is explicitly approved.
2. In a multi-speaker story, `南町nightin` is one person. `南町nightin`, `南町`, `NT`, and `nt` must not become different people. The full mixed surface may be written `南町nightin`; a later mention may be `南町`; `NT`/`nt` are aliases or heard surfaces, not distinct identities.
3. The machine title was semantically good but used `莉亚`; the correct proper-name orthography is `莉娅`.
4. Human edits to the story also contain within-cue speaker boundaries. Those exact candidate edits are operator truth and must be preserved through SRT/ASS/burn/package regeneration.

## Existing project state

- `assets/lidousha/psplive_roster.v1.md` has canonical row `星汐`, official surface `星汐Seki`, aliases including `xxsk`.
- `assets/lidousha/glossary.txt` says all those aliases refer to the same person, but it deliberately forbids blind global replacement.
- The same glossary says `南町nightin` is a valid continuous mixed proper-name surface only when both parts are spoken; a single `南町` must not be globally expanded.
- `assets/lidousha/entity_confusables.json` currently groups `南町`, `大N`, `大N老师`, `小N`, and `南町nightin` under canonical text `南町`.
- `assets/lidousha/session_relation_ledger.v1.json` separately stores a participant display name and surfaces.
- Upload tags intentionally emit searchable official names such as `南町` or `星汐`; subtitle display surface can differ.
- Pipeline policy already says glossary/roster membership proves an identity candidate, not occurrence or mutation authority. Each risky mention still needs local audio/context or human truth.
- BCUT owns timing; semantic/name repair changes text only.
- Ivan's exact reviewed subtitle bytes must outrank later automatic generalized repair. “All unmentioned text is correct” should freeze the whole final SRT, with only explicitly enumerated edits applied.

## Observed systemic failure

The pipeline has enough lexical facts, yet different stages flatten distinct concepts into one `canonical` string. Consequences:

- one person can fragment into `NT`, `南町`, and `南町nightin` in speaker/entity reasoning;
- a context-preferred nickname (`xxsk`) is rewritten to the official roster surface (`星汐`), even when operator truth wants the nickname;
- title generation uses stale orthography (`莉亚`) instead of the reviewed entity spelling (`莉娅`);
- later title/cover/tag stages do not consistently consume the exact reviewed entity decisions from final subtitles;
- a broad global replacement would create false positives, so that is not acceptable.

## Constraints

- No unsafe global text replacement.
- Separate stable person identity from mention-level written surface and from publication/search tag surface.
- Human candidate truth is immutable and hash-bound; future model or registry updates cannot silently mutate it.
- A roster/alias match may nominate an identity, but cannot by itself authorize changing a particular cue.
- Speaker diarization must group aliases of the same person without requiring every subtitle mention to use the same spelling.
- Titles and covers must consume reviewed canonical orthography when they name that person, while tags may intentionally use a search-oriented official surface.
- Migration must be incremental and compatible with existing JSON assets/receipts.

## Questions

1. What exact data model do you recommend? Please distinguish at least `entity_id`, official identity, context/session preferred display, mention surface, normalized alias, reviewed orthography, speaker-cluster identity, and publication/search surface. Say which fields are authoritative vs derived.
2. Should `canonical` remain a string, become an opaque stable ID, or be split? Give the minimal safe migration path for this repository.
3. How should candidate-scoped operator truth express:
   - whole-SRT exhaustive approval,
   - explicit cue text changes,
   - mixed-speaker within-cue spans,
   - context-preferred surface (`xxsk`),
   - reviewed spelling (`莉娅`), and
   - alias equivalence without global replacement (`NT`/`南町nightin`)?
4. How should final subtitle decisions flow into speaker grouping, title, cover text, and tags without letting any downstream stage re-decide them?
5. What deterministic invariants/tests would catch the current class of bug? Include negative tests for homophones/common words and for a legitimate single `南町` that must not expand to `南町nightin`.
6. Identify likely failure modes in the proposed design and give a ranked P0/P1 implementation plan.

Please provide a concrete schema sketch, decision table, migration steps, and test matrix. Be explicit where you disagree with the premise.
