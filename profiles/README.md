# Channel profiles

Each channel is deliberately split into two user-owned parts:

- `profiles/<profile-id>/profile.json` contains identity, routing, protocol
  tokens, title templates, and a strict inventory of required assets.
- `assets/<profile-id>/` contains the channel knowledge and creative material.

`lidousha` is the default profile and intentionally points at the existing
`assets/lidousha/` tree, so adopting profiles does not move or rewrite any
production asset. Proper nouns were never meant to fit inside the small JSON
manifest: the default profile routes them through its glossary, roster,
confusable/entity graph, timely-term, and song assets.

Select another committed profile with `AUTOSLICE_PROFILE=<profile-id>`. For a
local manifest outside the conventional directory, set
`AUTOSLICE_PROFILE_MANIFEST=/absolute/path/profile.json`.

Profile manifests are strict and fail on unknown keys, unsafe paths, missing
pipeline fields, or malformed protocol tokens. Start a new channel from the
neutral template instead of deleting Li Dousha knowledge in place:

```bash
profile_id=my_channel
mkdir -p "profiles/$profile_id"
cp -r assets/_template "assets/$profile_id"
cp profiles/_template/profile.json "profiles/$profile_id/profile.json"
# Replace replace_me and every placeholder identity/title value, then build
# the files and directories listed under assets.files/assets.directories.
python3 scripts/validate_channel_profile.py \
  --manifest "profiles/$profile_id/profile.json" --config-only
```

The asset directory is the customization surface:

| Knowledge or behavior | Profile-owned asset |
| --- | --- |
| Permanent names, memes, aliases | `glossary`, `psplive_roster` |
| ASR-confusable names and canonical spellings | `entity_confusables`, `text_normalization.canonical_surfaces` |
| Time-sensitive names/topics and discovery inputs | `timely_terms`, `timely_term_seeds`, `timely_term_sources`, `topic_entity_graph` |
| Known songs and lyric hints | `known_songs` |
| Subtitle decisions | `subtitle_correction_principles`, override/regression directories |
| Selection rubric calibration anchors | `selection_score_calibration` |
| Title voice and deterministic gates | `title_style`, `title_policy` |
| Upload tags and proper-noun search mappings | `upload_tag_policy` |
| Per-archive manual metadata that later edits must preserve | `manual_archive_metadata` (optional profile asset) |
| Persona and cover identity | `persona`, `cover_identity_prompt`, fonts, `cover_regenerator` |
| Official emote stickers usable as the cover subject | `emote_library` (optional profile asset; sticker media lives outside git under `assets/emote/`, override with `AUTOSLICE_EMOTE_DIR`) |
| Speaker identity | `voiceprint_profile` plus the runtime reference subdirectory |

Copying `assets/lidousha/` is useful only as a schema/example reference. A new
profile must replace channel-specific contents rather than inherit Li Dousha's
names, persona, voiceprint, or correction history.

`assets/_template/` 是自动派生的骨架；复制后按其 README 的**分层**填充——
身份四件套（词表/人设/标题风格/封面形象）由 agent 先问后写（模板内含问题
清单与真实示例），字体与通用配置默认全给，其余 crawler 代填/运行时自长。
注意：示例 profile `lidousha` 在全新 clone 里全量校验会因
`voiceprint_profile.v1.json` 缺失而 BLOCKED——声纹属生物特征，不随开源仓分发，
这是预期行为（先用 `--config-only`，或补齐你自己的声纹再全量校验）。

Validate a committed profile and all of its runtime paths before use:

```bash
python3 scripts/validate_channel_profile.py --profile <profile-id>
```

Use `--config-only` while initially scaffolding assets. Profile selection is
read when the Python process imports the pipeline, so set `AUTOSLICE_PROFILE`
before starting the runner rather than changing it inside a running process.

The profile owns channel identity, room/output paths, speaker labels and
aliases, title templates and policy, cover identity, per-channel text
canonicalization, asset/tool paths, voiceprint routing, and channel-specific
decision tokens.
Profile-scoped evidence schemas use `<profile-id>-...`; the default profile
therefore retains the historical `lidousha-...` values byte-for-byte.

Some persisted v1 evidence keys and compatibility environment aliases still
contain `lidousha` in their field names. They remain readable to avoid breaking
existing review packages; they are compatibility vocabulary, not the selected
host identity. A future evidence-schema major version can rename them with an
explicit migration instead of silently invalidating old hashes.
