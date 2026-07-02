# vtuber-slice Agent Notes

## Project direction

- The production source of truth is remote `free:/opt/bilive/app` and the `bilive_record` container path `/app`; the local macOS workspace is staging/docs/tests plus early-phase manual audit/sampling/debug evidence, not the final runtime or artifact store.
- The product goal is unattended automatic slicing. Early-phase manual audit is allowed for validation/forensics, but do not introduce workflows that require Ivan to routinely trim timelines, pick release clips one by one, or babysit uploads.
- Treat generated local media/reports (`lidousha/YYYY-MM-DD/**`, most `reports/**`, `.hermes/**`, `*.mp4`, `*.flv`, `*.m4s`, `*.bak-*`) as disposable unless a task explicitly names them as evidence.
- Upload/publish paths must fail closed: no `AUTO_UPLOAD` manifest + artifact hash gate means no publish.
- Candidates are content anchors, not final clip boundaries; source-context evidence and boundary resolver must decide final start/end.

## Project-local skills

- For song lyric subtitle timing in this repository, use the project-local skill at `.agent/skills/song-lyrics-timeline-aligner/SKILL.md`.
- Do not rely on a personal/global copy of that skill. The intended workflow is project-specific: external timed lyric source, clip-local first/last lyric anchors, global shift first, tail verification, and only explicit evidence-based stretch.
- For 李豆沙 song uploads, keep the `【李豆沙】豆沙歌，...` prefix but prefer hook-style titles that fold in the song name and live context, instead of plain catalog titles like `【李豆沙】豆沙歌，《歌名》`.
- When a song upload title changes, update the matching cover text before considering the edit complete. Cover text should omit `【李豆沙】豆沙歌，` and use the same hook phrase in a short readable form.
