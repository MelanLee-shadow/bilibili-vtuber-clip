# vtuber-slice Agent Notes

## Project direction

- The autoslice production source of truth is the committed deployment at `free:/opt/bilive/autoslice/repo` plus its live `state/`, `out/`, and `reports/` directories. The recorder source remains `free:/opt/bilive/app` and the `bilive_record` container path `/app`. The local macOS workspace is source staging/docs/tests plus review mirrors, not the final runtime or artifact store.
- The product goal is unattended automatic slicing. Early-phase manual audit is allowed for validation/forensics, but do not introduce workflows that require Ivan to routinely trim timelines, pick release clips one by one, or babysit uploads.
- Treat generated local media/reports (`lidousha/YYYY-MM-DD/**`, most `reports/**`, `.hermes/**`, `*.mp4`, `*.flv`, `*.m4s`, `*.bak-*`) as disposable unless a task explicitly names them as evidence.
- Upload/publish paths must fail closed: no `AUTO_UPLOAD` manifest + artifact hash gate means no publish.
- Candidates are content anchors, not final clip boundaries; source-context evidence and boundary resolver must decide final start/end.

## Project-local skills

- For song lyric subtitle timing in this repository, use the project-local skill at `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`.
- Do not rely on a personal/global copy of that skill. The intended workflow is project-specific: external timed lyric source, clip-local first/last lyric anchors, global shift first, tail verification, and only explicit evidence-based stretch.
- For 李豆沙 song uploads, keep the `【李豆沙】豆沙歌，...` prefix but prefer hook-style titles that fold in the song name and live context, instead of plain catalog titles like `【李豆沙】豆沙歌，《歌名》`.
- When a song upload title changes, update the matching cover text before considering the edit complete. Cover text should omit `【李豆沙】豆沙歌，` and use the same hook phrase in a short readable form.

## Unattended runner (2026-07-06)

- Post-stream automation lives in `scripts/free_session_autoslice.py`, deployed at `free:/opt/bilive/autoslice/` (cron */10, flock). It auto-produces top-5 talk clips + up to 2 song clips (ranked by danmaku volume) after each stream ends. Kill switch: `touch /opt/bilive/autoslice/DISABLED`. It has no upload path; publishing stays a separate Ivan-authorized step (and anything published must be committed — see memory `authorized-upload-must-commit`).
