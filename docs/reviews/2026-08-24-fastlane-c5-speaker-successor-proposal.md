# C5 text-only speaker-successor technical proposal — `accepted=false`

This proposal is a candidate-private technical bridge only.  It does not
accept a package, grant an upload, create a state/journal/registry entry, or
change any candidate artifact.  Root acceptance is required before a future
private receipt may be created.

## Scope and immutable inputs

The sole text authority remains Ivan's exhaustive ruling #5.  The sealed
diagnostic has 24 cues (`5da5af9dba5ce3ff2fee7d585bda809eb3a9edb612a18868ace683cb94281043`)
and the reviewed release SRT has 21
(`9f33f247deb405b409d50f294bbfab08db7d5f43086241dd736fac0498faf64b`).
Only source indices 13--15, `[28700, 36140)` ms, are dropped; every other cue
is `OPERATOR_UNCHANGED_FREEZE`.  The ledger and truth-diff hashes are,
respectively,
`6fac5913ee143619c840483a8355e6ba63690f9bf47ddd27b3d34b9fcb5ce1a2` and
`48142b7ebf737dde41472757030385794deb43ba9091289d1f4962885fd446b1`.

The proposed adapter consumes, without modifying, the live historical C5
package chain observed on `free`:

- historical record: SHA-256 `09646c35fdb2afd6a35cc4cf7c5aa4fbb61461f455a1866e9de3c8f35952ab95`;
- embedded/standalone `READY` speaker manifest: SHA-256
  `cbed92f720d52d4da93f2b3fd58d1acaa02aefae0dc55a3a6cece13c3de248ec`;
- historical speaker SRT and ASS: SHA-256
  `a014b0ece4a12234be58f08afb52bbb41a9ec7260d737b06a4ebfe8191932d20`
  and `493e6aa6603b7a9dfe7ecb86e8b67d79cddc65ba1666ef2f499afb540106d94d`;
- historical recut media: SHA-256
  `ce0142eca7921ddd7ca85412c0b11bf040b32e60021482b3b8cfe234ef36b312`.

## Strict private adapter contract

The existing `reviewed-baseline-text-only-speaker-successor.v1` is the only
implementation candidate.  Before emitting any successor bytes it must prove:

1. the record, candidate ID/date, embedded manifest and standalone manifest
   bind exactly; the manifest is `READY`, production-ready, non-symlink, and
   its speaker SRT/ASS/media hashes match;
2. all 24 diagnostic cue times/texts, speaker rows, decision metadata, labels,
   and the old 24-to-24 speaker grid agree exactly;
3. the ledger and truth diff produce exactly the sealed 24-to-21 map, with
   drops only at 13--15; and
4. each retained line copies the pre-existing speaker label by source-cue map.
   It may not classify a speaker, create a label, alter a label, alter text,
   or use provider evidence.

Any hash, candidate/date, cue count/time/text, label, manifest metadata, media
binding, or symlink drift is a fail-closed technical refusal.  Boundary,
media, title, cover and all non-ruling text remain frozen.

## Proposed receipt shape

A later **create-only private** receipt should bind the deployment seal,
`accepted=false`, `upload_allowed=false`, the seven hashes above, the exact
drop set `[13,14,15]`, the 21 retained source indices, and the resulting
speaker SRT/ASS/manifest hashes.  It must contain no provider transcript,
human-review field, or formal-state handle.  No such receipt is created or
signed by this proposal.

## Pre-C4 full-dry diagnostic — terminal for this proposal

On deployed `ade4f2005aaa67326fb3b0ac3a4252d4dc5ecd78`, a private-only
full-dry was run under
`/opt/bilive/autoslice/private-fastlane-preflight/c5-full-dry-20260824T222100Z/`.
Its stdout receipt has SHA-256
`9379f53c6e23056926ab7170fba915f7e2c8a5d094d8fe674503864d2bf4c1e1` and
its sanitized diagnostic receipt is
`sha256:519ea70f110358fa34aa3b043b20bc89a411425be8f12561b220e4d04f0439ff`.
It fail-closed before speaker/source-fact/provider/package work with
`REDELIVERY_DELIVERY_PROJECTION_STRADDLER`.

This is a frozen-boundary projection issue, not an operator-drop or
speaker-evidence problem: the historical record's final boundary begins at
`9750` ms, while retained source/release cue 5 is `[9560,10080)`.  It crosses
the start by 190 ms.  The only dropped cues (13--15) are fully inside the
delivery interval.

### Conditional C5-only start-clamp proposal

The only proposed exception is a delegated-root technical projection for this
one source cue: keep the frozen final boundary `[9750,67524)`, cue 5 text,
speaker label and end `10080` unchanged, and project its *delivery* geometry
as `[0,330)`.  It must bind the ruling line, the seven historical artifacts
above, C5/date, the old cue `[9560,10080)`, and every output hash.  It must
reject any different candidate, boundary, cue, start/end, text, label, media,
or a drop set other than `[13,14,15]`; it is not a generic straddler rule.

The private evidence supports review of this exception but does not accept it:
the `9500--10300` ms mono audio probe (SHA-256
`904b77ad9b60c5480872c979796f8ead2449157a858a3814f9f608386ab02342`) has
detected silence from `9679.667` through `10028.687` ms.  Thus the frozen
delivery boundary at `9750` begins 70 ms into that silent span, not during a
new audible utterance.  The full-window cue remains the existing filler
`呃`; no ASR/provider result was requested.  Frames at 9500, 9750 and 10080
ms have SHA-256 `ddf0ea59a65b735ea144da0ac298be75127b34dacea448584f5c7e81655401e3`,
`913ac4067707450560aa50ab3d7feb7f458f92378005c4210f1f71f1286e5079`, and
`bedfe3b21dfe69d438f6baf866c1b5641428ac31982f4c58ba6fd70e4ed25096`.
They show the same continuous host scene; they do not establish a new
semantic boundary.

The proposal remains `accepted=false`, creates no formal receipt, and cannot
advance C5 to package/audit until root accepts its exact adapter contract.
