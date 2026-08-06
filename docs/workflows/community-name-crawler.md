# Official streamer registry and daily community names

This workflow deliberately separates two clocks that answer different
questions:

- `crawl_streamer_registry.py` asks **who is an official streamer and which
  official surfaces identify that person?** Membership changes slowly, so the
  production schedule refreshes it weekly.
- `crawl_community_names.py` asks **which new nickname, fan name, or event meme
  is the Bilibili clipping community using for an already anchored person?**
  News and community language can change after one incident, so this stage runs
  daily and incrementally.

Neither output proves that a name occurred in a subtitle cue. Neither crawler
edits subtitles.

## Official registry

The profile-owned `streamer_registry_sources` config currently combines:

1. the existing PSPLive official roster video and reviewed compatibility
   overrides;
2. VirtuaReal's official member API; and
3. official VirtuaReal Bilibili new-member announcements, verified by numeric
   owner MID, owner name, title anchor, publication time, and member space MID.

The announcement source fills recent members that may not yet be reflected by
the slower website catalog. Members merge by stable Bilibili MID, retain
organization provenance, and expire after 35 days so four missed weekly runs
fail closed instead of silently presenting an old roster as current.

Run a read-only live snapshot:

```bash
python3 scripts/crawl_streamer_registry.py \
  --now 2026-08-06T01:50:00+00:00 \
  > /tmp/streamer_registry.json
```

Production writes `/opt/bilive/autoslice/state/streamer_registry.json` every
Sunday at 06:07 UTC. The legacy PSP snapshot refreshes Sunday at 06:12 UTC for
backward compatibility; it is no longer a daily job.
The weekly job performs at most three attempts per official endpoint (nine
requests total) so an intermittent Bilibili 412 or truncated website response
does not discard an otherwise valid refresh; all attempts still use the same
strict identity and minimum-count gates.

## Daily community relation lane

The daily stage reads the last-good official registry and fairly rotates 12
cold members. Up to four near-threshold candidates may receive an additional
hot query without displacing cold coverage. With the current 112-member
registry, every member is attempted within ten daily runs.

Each selected member gets one Bilibili video-search request. A durable cursor
prefers the shortest official CJK surface (for example, `犬绒` before
`犬绒Mofu`), rotates `pubdate` → `totalrank` → `click`, then walks pages
1 → 2 → 3 before widening to the canonical surface and `canonical + 切片`.
Failures do not advance the cursor. This keeps new events visible while slowly
backfilling established community language over a two-year evidence window.
Every bounded result title is visible to the structured judge in newest/oldest
interleaved order; the larger description/tag fields are evenly sampled to keep
its daily prompt bounded. Repeated target co-occurrence across titles whose
other participants change is treated as stronger identity evidence than one
ambiguous multi-person title.
The judge may propose exact metadata substrings and one of four relation types:

| Relation | Meaning |
| --- | --- |
| `alias_of` | the community uses the surface to call the person |
| `fan_name_of` | the surface names the person's fans, not the person |
| `meme_of` | an incident/persona/appearance/object meme associated with the person |
| `associated_with` | a useful association whose type is still unclear |

The judge has no mutation tools. Its output is rejected unless the surface is a
safe 2–24-character atom and appears byte-for-byte in every cited BVID's
metadata. Multiple registry members without an explicit one-to-one link must
not be guessed. Raw title, description, tag, and comment text never enters the
subtitle prompt or the accepted snapshot; the durable state keeps only stable
BVID/MID/date/field facts and a raw-content SHA-256.

Comments are a limited corroboration surface for metadata-proposed candidates,
not a standalone discovery or acceptance path. The crawler reads at most one
20-comment page for six videos and never fetches nested replies.

### Acceptance and persistence

A community relation is automatically accepted only with all of:

- score at least 8;
- at least four videos;
- at least three stable uploader MIDs;
- evidence on at least two dates;
- at least two medium/strong metadata links; and
- no uploader contributing more than half of the videos.

One uploader can publish any number of clips and still only create a
`candidate`. An official self-post has a narrower path, but still needs a
second uploader and a second video. A surface owned by another official entity
becomes `conflict`. Accepted mappings persist when search results age out; a
registry identity conflict is the only automatic downgrade path.

An event/persona `meme_of` has a deliberately faster, same-day path because the
event itself may last only one news cycle: score at least 5, two strong-title
videos from two independent uploaders, and balanced contributions. Its weaker
acceptance does not grant stronger authority: it remains occurrence-neutral
context and can never become a mechanical subtitle replacement.

This is why a person nickname such as `鼠鼠`, a food-shaped community moniker
such as `蒜蓉蘑菇`, and an event/persona name such as `白色奶龙` do not share one
flat `aliases` array. The crawler may discover all three, but it must preserve
their relation type and evidence status.

Only `accepted` rows enter `community_names.json`, with the explicit policy
`COMMUNITY_RELATION_EXISTS_NOT_CUE_OCCURRENCE_OR_MUTATION_AUTHORITY`. Candidate,
rejected/conflict rows and raw source text stay out of the subtitle prompt.

## Resource and failure contract

The production job runs daily at 06:27 UTC with `flock -n` and these caps:

- 24 real HTTP requests total;
- 12 cold + up to 4 hot member searches;
- one page / 50 search rows per member, with persistent query/order/page cursors;
- up to 6 one-page comment checks;
- 2 retry slots for intermittent Bilibili 412/network failures;
- 15 seconds and 512 KiB per response;
- 18-hour fresh cache and 7-day stale-on-error cache.

Stale cache hits do not invent a new source identity. One member's failure does
not block healthy members. If every selected search fails, the job updates only
failure state and refuses to replace the last-good prompt snapshot. State is
mode 0600; the prompt-safe snapshot is mode 0444. No cookie, WBI credential,
raw response, title, description, or comment is committed or exported.
Accepted/conflict decisions persist; noisy candidate/rejected state is capped
at 2,048 mappings so an unattended daily job cannot grow without bound.

Production command shape:

```bash
set -a
source /opt/bilive/autoslice/cpa.env
set +a
python3 scripts/crawl_community_names.py \
  --registry /opt/bilive/autoslice/state/streamer_registry.json \
  --cache-dir /opt/bilive/autoslice/cache/community-name-crawler \
  --state /opt/bilive/autoslice/state/community_name_state.json \
  --write /opt/bilive/autoslice/state/community_names.json
```

For a bounded canary, add repeated `--entity` values. `--no-llm` verifies
network/cursor behavior without proposing new relations.
