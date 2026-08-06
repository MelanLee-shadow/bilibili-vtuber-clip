# Timely-term crawler

## Purpose and authority boundary

`scripts/crawl_timely_terms.py` builds the source-backed candidate snapshot used
by subtitle entity verification. Its default window is anchored to the runtime
date and covers nine months backward and six months forward.

The snapshot is **not subtitle authority**. It may raise a current anime,
project, character-family, or ACG-event name as a candidate, but it must never
override incompatible audio or exact structured SC/danmaku. Runtime prompt
rendering remains recording-date filtered and capped at 64 active terms.

Raw page bodies, RSS descriptions, and headlines never enter a correction
prompt. RSS can only:

1. attach a dated article source to an already known structured anime title; or
2. emit a locally configured event name such as `AnimeJapan` or `Anime Expo`.

This avoids turning untrusted news/chat text into instructions or an unbounded
per-cue web-search loop.

## Sources

- **AniList GraphQL** is the bounded 15-month recall lane. Three pages of at
  most 50 popular anime plus one page of 50 manga/light-novel projects are
  fetched by start-date range. Canonical/native/
  romaji titles, synonyms, relations, dates, popularity, record update time,
  and stable item URL are structured fields. `updatedAt` is used as the catalog
  publication/update date; records updated after the pinned `--now` date are
  excluded.
- **Bangumi `/calendar`** is a one-request Chinese-name enrichment lane for the
  current weekly schedule. A Chinese canonical candidate is emitted only when
  the Bangumi Japanese name exactly equals an AniList title surface. Its
  `published_at` records the date the structured calendar was observed, not an
  inferred franchise-announcement date.
- **Anime News Network RSS** supplies dated news evidence and configured ACG
  event observations. Arbitrary headline entity extraction is intentionally
  disabled.
- **TV Tokyo official anime RSS** is a first-party schedule/news enrichment
  lane. Legacy item links are upgraded to HTTPS only after their host matches
  the configured allowlist.
- `assets/lidousha/timely_term_seeds.json` preserves a small reviewed seed lane
  for Chinese names/readings/confusables that catalogs do not provide. Seeds do
  not replace automated discovery; they are merged with it and remain subject
  to source/active-date validation.

`assets/lidousha/timely_term_sources.json` is strict local configuration. Adding
a feed or event requires an explicit code/config review; remote pages cannot
add new endpoints or event-watch rules.

## Bounded operation and caching

Defaults:

- request budget: 14 network calls, including one public WBI bootstrap and one
  bounded transient-failure retry reserve;
- AniList: 3 × 50 anime plus 1 × 50 manga/light-novel records maximum;
- per-response limit: 2 MiB;
- HTTP timeout: 15 seconds;
- fresh cache TTL: 18 hours;
- stale-on-error cache: at most 7 days;
- cache disk budget: 64 entries and 64 MiB;
- output cap: 240 snapshot terms;
- snapshot TTL: 2 days;
- prompt cap: 64 date-active terms.

Endpoints and redirects must stay on the credential-free HTTPS allowlist.
Cache entries are content-hashed and written atomically with mode `0600`.
Adapters are failure-isolated: one failed feed does not erase results from
healthy sources. A run exits `1` when it produced a valid partial snapshot and
reports adapter errors; it exits `2` when no valid snapshot can be produced.
The Bilibili community lane uses signed `/x/web-interface/wbi/search/type`
requests with no cookie or account token. HTTP 412/429, code `-352`, and
`v_voucher` responses are treated as risk control and are not immediately
retried; the next daily run retains the last-good snapshot.

## Commands

Network dry-run pinned to a reproducible processing time:

```bash
python3 scripts/crawl_timely_terms.py \
  --dry-run \
  --now 2026-07-12T12:00:00-04:00 \
  > /tmp/timely_terms.json
```

Validate the candidate through the existing strict consumer schema:

```bash
python3 scripts/gemini_slice_jingting.py \
  --validate-timely-terms /tmp/timely_terms.json
```

Atomically update a staged snapshot (this does not deploy it):

```bash
python3 scripts/crawl_timely_terms.py \
  --write assets/lidousha/timely_terms.json
```

Repeat from cache without network:

```bash
python3 scripts/crawl_timely_terms.py --offline --dry-run
```

Blind evaluation must exclude reviewed seed terms so Ivan's corrections cannot
leak back through the terminology prior:

```bash
python3 scripts/crawl_timely_terms.py \
  --offline \
  --exclude-reviewed-seed \
  --write /tmp/timely_terms.machine-only.json
AUTOSLICE_HUMAN_TRUTH_MODE=withheld \
AUTOSLICE_BLIND_TIMELY_TERMS=/tmp/timely_terms.machine-only.json \
python3 scripts/free_session_autoslice.py --once
```

The production deploy installs a daily 06:17 bounded refresh into
`/opt/bilive/autoslice/state/timely_terms.json`. The runner prefers that runtime
snapshot over the committed fallback without modifying the deployed Git tree.
This daily clock is for news/community freshness. Official streamer membership
is intentionally not refreshed here; its slower registry and the separate
daily community nickname/meme lane are documented in
[`community-name-crawler.md`](community-name-crawler.md).

## Topic -> work -> character subgraph

`scripts/crawl_topic_entity_graph.py` is the second, narrower stage. It consumes
the validated timely-term snapshot, discovers bounded Bangumi subject IDs, and
emits `assets/lidousha/topic_entity_graph.json` with stable topic, work, and
character nodes. Character Chinese canon, Japanese/native surfaces, kana or
romaji readings, role, and provenance come only from structured catalog fields;
community aliases are topic-routing evidence, not character-name authority.

At subtitle time the runner uses this order:

1. Resolve a topic or explicit work from BCUT draft, selection hook, structured
   SC/danmaku, and available screen text.
2. If an explicit work matched, expose only that work's characters. If only a
   franchise/topic matched, expose the bounded union of its child works. No
   match or an unrelated-topic ambiguity exposes no character graph.
3. Give the scoped names/readings to AGY and CPA as candidates.
4. Convert names that actually appear in the corrected SRT into a narrow
   forced-choice group. Raw audio selects the entity; only after that selection
   may the graph repair a wrong/non-Chinese surface. A correct spoken Chinese
   short name such as `立希` remains short instead of being expanded with an
   unspoken surname.

Generate the graph from the current runtime snapshot:

```bash
python3 scripts/crawl_topic_entity_graph.py \
  --timely-terms /opt/bilive/autoslice/state/timely_terms.json \
  --cache-dir /opt/bilive/autoslice/cache/topic-entity-crawler \
  --write /opt/bilive/autoslice/state/topic_entity_graph.json
```

The production deploy installs this bounded refresh daily at 06:37, after the
06:17 timely-term refresh. A partial crawl with any enrichment diagnostic
refuses to replace the last good graph. The runner SHA-binds the selected graph. In
`AUTOSLICE_HUMAN_TRUTH_MODE=withheld`, the committed/reviewed graph is disabled;
a blind run must explicitly provide a machine-only graph via
`AUTOSLICE_BLIND_TOPIC_ENTITY_GRAPH`, and its embedded input snapshot hash must
match `AUTOSLICE_BLIND_TIMELY_TERMS`, or the run proceeds with no graph.

`--write` validates the complete payload before replacement, fsyncs the new
file, and is idempotent: identical bytes report `"changed": false`.

## Known coverage limits

- AniList does not provide authoritative Chinese localization for every title;
  Bangumi enriches current weekly anime only. Reviewed seed terms remain useful
  for high-value Chinese canon and audio confusables.
- RSS is not a historical archive. The first run gets the catalog's full
  15-month title window, but news evidence accumulates through the cache only
  from future scheduled runs.
- Event discovery is conservative and starts with configured major-event names.
  It will miss a newly created event until its stable name is reviewed into the
  watch list or a future structured event source is added.
- No automatic homophone/confusable invention is attempted. Incorrect negative
  candidates are more dangerous than an empty list; audio verification and
  reviewed error truth should add those relationships.
