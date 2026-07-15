# Channel profiles

`profiles/<profile-id>/profile.json` is the channel-specific configuration
entry point. `lidousha` is the default profile and intentionally points at the
existing `assets/lidousha/` tree so adopting profiles does not move or rewrite
any production asset.

Select another committed profile with `AUTOSLICE_PROFILE=<profile-id>`. For a
local manifest outside the conventional directory, set
`AUTOSLICE_PROFILE_MANIFEST=/absolute/path/profile.json`.

Profile manifests are strict and fail on unknown keys, unsafe paths, missing
pipeline fields, or malformed protocol tokens. A new channel should copy the
default manifest, choose its own display name, romanized/English prompt name,
room and output directory, then replace every referenced asset and voiceprint
binding before running the unattended pipeline.

Validate a committed profile and all of its runtime paths before use:

```bash
python3 scripts/validate_channel_profile.py --profile <profile-id>
```

Use `--config-only` while initially scaffolding assets. Profile selection is
read when the Python process imports the pipeline, so set `AUTOSLICE_PROFILE`
before starting the runner rather than changing it inside a running process.

The profile owns channel identity, room/output paths, speaker labels and
aliases, title templates, cover identity, per-channel text canonicalization,
asset/tool paths, voiceprint routing, and channel-specific decision tokens.
Profile-scoped evidence schemas use `<profile-id>-...`; the default profile
therefore retains the historical `lidousha-...` values byte-for-byte.

Some persisted v1 evidence keys and compatibility environment aliases still
contain `lidousha` in their field names. They remain readable to avoid breaking
existing review packages; they are compatibility vocabulary, not the selected
host identity. A future evidence-schema major version can rename them with an
explicit migration instead of silently invalidating old hashes.
