# Decision request: highest-value safe next step for Li Dousha speaker accuracy

You are advising on a real semi-automatic Bilibili clipping pipeline. Give one decisive, implementation-ready recommendation, not a generic survey. The operator has explicitly asked the engineering agent to continue unattended overnight and to consult ChatGPT Pro for unclear decisions.

## Current authority and safety boundary

- Production is disabled. Do not recommend enabling, deploying, uploading, pushing, or weakening gates tonight.
- All new speaker work is development/shadow only. Automatic eligibility never authorizes upload.
- The approved centrality policy is already implemented: unresolved speaker/centrality null -> bounded human review; levels 0-1 auto-ineligible; level 2 manual-selection-only; levels 3-4 equally auto-eligible and never rank/tie-break; legacy v1 is shadow-only.
- A hash-bound receipt already fails closed on candidate boundary, media, ASR, speaker, cue-label, or content-rank drift.

## Speaker evidence already obtained

- Model: CPU CAM++ speaker embeddings.
- Existing production enrollment: 3 long references, 167.69s total, no registered cross-session/style coverage.
- Exact cross-session reviewed bank from 2026-07-22: 4 full-cue HOST clips, durations 780/1100/2140/840ms, total 4.86s. Seven mixed rows were excluded because their internal boundaries are explicitly approximate.
- Same-session 2026-08-08 reviewed material: 44 exact HOST full cues, 86.989s total, but it overlaps the evaluation set and therefore can only be development material, never holdout evidence.
- Clear evaluation truth: 188 cues = 46 HOST + 142 OTHER; 6 mixed excluded from clear metrics; 3 machine-only cues excluded. Short cues under 1500ms are 95/188 = 50.5%.
- Existing automatic speaker baseline: 157/188 correct but 18 false-HOST.
- Designated conservative 2-of-3 static strategy: H->H/O/U = 3/6/37; O->H/O/U = 1/73/68; false-HOST=1; host recall=.0652; classified coverage=.4415; UNKNOWN=.5585; verified accuracy=.9157. It fails every production gate except mixed hard-label=0.
- The 4.86s cross-session reviewed bank adds real signal but is insufficient. On the longer candidate, best AUC=.719 and zero-false-HOST oracle recall=.086. On the shorter candidate, best AUC=.850 and oracle recall=.545. Those oracle thresholds were chosen after truth and have no deployment authority.
- Frozen threshold 0.50 gives median-bank HOST recall 0 on both candidates. Raising/lowering one global threshold cannot separate the overlapping score distributions.
- A previous pyannote subwindow pilot also failed: mixed full correctness 3/13, boundary precision/recall about 30.8%, plus nonmixed over-segmentation.

## Safety/accounting fixes already completed

- Overlapping windows now map to disjoint time cells; UNKNOWN is not double-counted.
- NON_SPEECH and real gaps break smoothing runs; isolated HOST islands across silence cannot create false SOLO.
- SOLO requires a trusted solo source plus acoustic audit.
- Per-prototype scores and audio hashes are retained.
- Cue scoring is exact-SRT-bound; <300ms alone is barred from hard labels. Short cues may only hard-label through conservative consensus. Long cues require whole/subwindow agreement. Mixed/conflict only downgrades to UNKNOWN.
- Reviewed auxiliary bank scores remain separate from existing v1 predictions.

## Decision needed

Choose the highest-value work the agent can safely implement tonight without inventing new truth. In particular, decide among or replace these possibilities:

1. Build a reviewed-sample bank and a deterministic representative-selection layer (per-session medoids/diversity constraints), but do not select thresholds.
2. Replace max aggregation with a pre-registered robust embedding/score rule such as per-session centroid/medoid plus cross-bank consensus or min-of-medians.
3. Add impostor/cohort normalization or explicit OTHER prototypes, if and only if this can be done without leaking the 8/8 evaluation truth into claimed holdouts.
4. Reuse same-session high-confidence anchors as a second-stage diagnostic, with strict no-self-match and no confidence promotion.
5. Stop algorithm changes and spend the night only on gathering/hash-binding exact newer-session assets for future human labeling.

Answer these exact questions:

1. What single architecture should be the next development candidate? Specify the mathematical aggregation rule, bank structure, minimum clip/session requirements, abstention behavior, and how short/mixed cues are handled.
2. Which parts may be implemented and tested now using 8/8 as development truth, and which parts must remain frozen until two new locked cross-session holdouts exist?
3. Give a ranked list of at most three overnight tasks, each with a deterministic artifact and stop condition.
4. State the main leakage/false-confidence traps and one negative canary for each.
5. Is there any defensible path to production without new human-reviewed cross-session holdouts? Answer yes or no and justify.
6. Give conditional ETA language that is operationally honest.

Hard acceptance remains per candidate and pooled: false-HOST=0, host precision=1.0, host recall>=.85, classified coverage>=.85, clear UNKNOWN<=.15, verified accuracy>=.95, mixed hard-label=0, and two locked cross-session holdouts. Do not propose lowering these gates merely to ship.
