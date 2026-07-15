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
default manifest, choose its own identity/output directory, and replace every
referenced asset and voiceprint binding before running the unattended pipeline.
