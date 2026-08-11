# ChatGPT Pro decision: canonical person identity vs subtitle surface

## Consultation identity

- Thread: <https://chatgpt.com/c/6a7b7511-28e4-83ea-aaac-fe180a0261ac>
- Visible mode: `Pro`
- Prompt SHA-256 after the recorded `CRLF_or_CR_to_LF_then_trimEnd`
  normalization: `924b4a64a220522afe1f0ed4f39f7de5346dd8c9b0a013438b2ab7abefcf27a9`
- Prompt verification: the complete visible user turn was reconstructed from the rendered DOM,
  including Markdown headings, lists, inline code, and emphasis; its normalized SHA-256 matched
  the durable run record exactly.
- Completion evidence: visible `Worked for 15m 58s`; no `Stop generating` control.
- Visible response text SHA-256 under the same normalization:
  `c20a4f5731bc2d6c4fd4e500259014f92c1dbb0f412983dbf7e66902b9f6dcab`
- Durable run record:
  `/Users/ivan/.codex/chatgpt-pro-consult-runs/2026-08-11T19-15-44-297Z-924b4a64a220522a.json`

## Decision

Split the overloaded `canonical` string completely. The only stable identity key should be an
opaque, immutable `entity_id`. Every human-readable name is a typed, scoped surface. Identity
equivalence and text mutation authority are separate facts.

The minimum surface types are:

- official identity surface;
- session/candidate preferred display surface;
- exact mention surface;
- normalized alias used only for identity resolution;
- candidate-reviewed orthography;
- speaker-label surface;
- title/cover surface; and
- publication/search surface.

The authoritative candidate truth is a pair of immutable sibling artifacts:

1. the exact approved final SRT bytes; and
2. a semantic sidecar bound to the candidate, exact SRT SHA-256, source interval, and upstream
   evidence hashes.

The sidecar owns mention-to-entity decisions, reviewed written surfaces, within-cue speaker spans,
speaker-cluster identity bindings, and per-channel projections. Downstream stages consume the
frozen projection. They do not re-resolve aliases or choose a different spelling.

## Authority and derivation

| Field | Authority | Allowed effect |
| --- | --- | --- |
| `entity_id` | identity registry; immutable | joins references to one person |
| official identity surface | official roster evidence | display/search candidate only |
| normalized alias | typed registry evidence | may nominate `entity_id`; never rewrites text |
| context preferred surface | reviewed session/candidate authority | chooses display only inside its bound scope |
| mention surface | exact transcript bytes or operator truth | preserves what this mention should show |
| reviewed orthography | operator/candidate authority | required spelling in explicitly named output channels |
| speaker-cluster identity | reviewed acoustic/operator evidence | groups speech spans; does not choose subtitle spelling |
| title/cover projection | frozen candidate sidecar | deterministic name surface for title and cover |
| publication/search surface | publication policy plus reviewed identity | may intentionally differ for discoverability |

No registry alias, roster membership, confusable, or model inference is sufficient on its own to
change a cue. A confusable can only create a repair proposal with local evidence. An alias is an
identity-resolution input, not a respell pair.

## Exact application to the two approved candidates

### `auto_230125_1157_1229`

- The stable identity remains the person represented globally by 星汐/星汐Seki.
- `xxsk` is the candidate-preferred and exact reviewed surface for this clip; it is not a global
  canonical rename.
- `我对xxsk说` and every reviewed `xxsk` mention are immutable whole-SRT truth.
- The single-host lane remains `uniform_host`; no speaker-identity claim is created.
- Title/cover for this candidate must consume `xxsk`; searchable publication tags may still use an
  approved official surface when policy calls for it.

### `auto_223750_578_734`

- `南町nightin`, `南町`, `NT`, and `nt` may resolve to the same stable person identity.
- Equivalence does not authorize expanding a legitimate single `南町` into `南町nightin` or
  collapsing every `NT` globally.
- Ivan's exact within-cue A/B edits are the speaker-span truth for this candidate.
- The reviewed title/cover orthography is `莉娅`; `莉亚` must fail the candidate's deterministic
  title/cover projection, without becoming a global homophone replacement rule.
- Because Ivan approved all unmentioned subtitle text, the story subtitle's retained `莉亚` is not
  silently changed by the title/cover orthography decision.

## Deterministic invariants

1. An `entity_id` is opaque and immutable; display strings cannot be used as joins.
2. Every projection binds `candidate_id`, exact SRT SHA-256, source interval, and policy/schema
   version. Any byte or boundary change makes it stale.
3. An alias resolves identity but never emits a replacement operation.
4. A reviewed mention surface is reproduced byte-for-byte in the final SRT unless a newer explicit
   operator receipt supersedes it.
5. Speaker grouping uses `entity_id`/cluster bindings and cannot normalize subtitle text.
6. Title and cover consume the frozen `title_cover` projection and fail closed on a conflicting
   named surface.
7. Tags consume a separate publication projection; they cannot mutate subtitles, title, cover, or
   speaker labels.
8. A single `南町` remains `南町`; `NT` and `nt` do not expand outside the bound candidate/session.
9. Common-word and homophone collisions do not resolve without local evidence.
10. Legacy v1 `canonical` data is read through adapters and never gains new rewrite authority.

## Ranked implementation plan

### P0

1. Freeze both approved candidate SRTs and their semantic/operator sidecars; regenerate all derived
   SRT/ASS/burn/package artifacts from them.
2. Add an additive candidate identity-projection schema and validator with opaque `entity_id`, typed
   surfaces, exact input hashes, and downstream channel projections.
3. Make title/cover consume candidate-reviewed orthography and fail on conflicting surfaces.
4. Keep alias equivalence out of `respell_pairs`; preserve local mention surfaces by default.

### P1

1. Migrate roster/session participants to stable IDs through compatibility adapters.
2. Separate legal aliases from acoustic/ASR confusables in the registry and repair pipeline.
3. Bind diarization clusters to entity IDs independently from subtitle display surfaces.
4. Replace stage-specific name rediscovery with one frozen candidate semantic bundle.
5. Add migration and negative test matrices before removing any legacy string field.

## Explicit non-blocking rule

The systemic migration must not delay the two exact operator-approved repairs. Candidate-scoped
truth is already stronger than the generalized model/registry path and should proceed through
regeneration, audit, and the separately authorized publication fast lane.
