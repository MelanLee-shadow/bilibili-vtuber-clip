"""Hash-attested chunked jingting refinement over SSH."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
from typing import Callable, Sequence

from scripts.gemini_slice_jingting import (
    AGY_MODEL,
    agy_prompt,
    looks_like_srt,
    strip_markdown_fence,
    validate_same_timing,
)
from src.autoslice.danmaku_evidence import (
    danmaku_in_window,
    format_danmaku_lines,
)
from src.autoslice.jingting_chunker import (
    JingtingChunk,
    merge_refined_chunks,
    plan_jingting_chunks,
    repair_sparse_refined_chunk,
)
from src.autoslice.source_context_executor import (
    AgyChunkAttestation,
    AgyExecutionResult,
    AgyRunnerError,
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _RemoteJingtingRunner:
    def __init__(
        self,
        host: str,
        *,
        danmaku_items=None,
        context_start_ms: int = 0,
        topic_entity_context_provider: Callable[[], str] | None = None,
        song_name_candidates: Sequence[str] = (),
    ) -> None:
        self.host = host
        self.danmaku_items = danmaku_items
        self.context_start_ms = context_start_ms
        self.topic_entity_context_provider = topic_entity_context_provider
        self.song_name_candidates = song_name_candidates
        self.chunk_print_timeout = "15m"
        self.chunk_poll_deadline_seconds = 1500
        self.chunk_poll_interval_seconds = 20
        self.attempts_per_chunk = 2

    @staticmethod
    def _run(
        cmd: list[str],
        *,
        timeout: int = 2400,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{cmd[0]} failed rc={completed.returncode}: "
                f"{completed.stderr[-400:]}"
            )
        return completed

    def _encode_chunk_clip(
        self,
        media_path: Path,
        chunk: JingtingChunk,
        out_path: Path,
    ) -> None:
        self._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{chunk.media_start_ms / 1000:.3f}",
                "-i",
                str(media_path),
                "-t",
                f"{chunk.media_duration_ms / 1000:.3f}",
                "-vf",
                "scale=1280:-2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(out_path),
            ],
            timeout=1800,
        )

    def _chunk_danmaku_lines(
        self,
        chunk: JingtingChunk,
    ) -> list[str] | None:
        if not self.danmaku_items:
            return None
        window_start = self.context_start_ms + chunk.media_start_ms
        window_end = self.context_start_ms + chunk.media_end_ms
        in_window = danmaku_in_window(
            self.danmaku_items,
            window_start,
            window_end,
            max_items=60,
        )
        return (
            format_danmaku_lines(in_window, base_ms=window_start)
            if in_window
            else None
        )

    def _stage_chunk_inputs(
        self,
        job_dir: str,
        chunk_clip: Path,
        chunk_srt_text: str,
        prompt: str,
    ) -> None:
        self._run(["ssh", self.host, f"mkdir -p {shlex.quote(job_dir)}"])
        with tempfile.TemporaryDirectory(prefix="ssh_agy_chunk_") as tmp:
            prompt_file = Path(tmp) / "prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            draft_file = Path(tmp) / "draft.srt"
            draft_file.write_text(
                chunk_srt_text
                if chunk_srt_text.endswith("\n")
                else chunk_srt_text + "\n",
                encoding="utf-8",
            )
            self._run(
                ["scp", "-q", str(chunk_clip), f"{self.host}:{job_dir}/input.mp4"]
            )
            self._run(
                ["scp", "-q", str(draft_file), f"{self.host}:{job_dir}/draft.srt"]
            )
            self._run(
                ["scp", "-q", str(prompt_file), f"{self.host}:{job_dir}/prompt.md"]
            )
            expected_hashes = [
                _sha256_file(chunk_clip),
                _sha256_file(draft_file),
            ]
        hash_probe = self._run(
            [
                "ssh",
                self.host,
                (
                    f"sha256sum {shlex.quote(job_dir)}/input.mp4 "
                    f"{shlex.quote(job_dir)}/draft.srt"
                ),
            ],
            timeout=120,
        )
        remote_hashes = [
            line.split()[0]
            for line in hash_probe.stdout.splitlines()
            if line.split()
        ]
        if remote_hashes != expected_hashes:
            raise AgyRunnerError(
                "AGY_INPUT_HASH_MISMATCH",
                f"remote AGY chunk input hash mismatch; see {self.host}:{job_dir}",
            )

    def _run_chunk_agy(
        self,
        job_dir: str,
        chunk_clip: Path,
        chunk_srt_text: str,
        chunk: JingtingChunk,
    ) -> str:
        topic_context = (
            str(self.topic_entity_context_provider() or "")
            if self.topic_entity_context_provider is not None
            else ""
        )
        prompt = agy_prompt(
            chunk_srt_text,
            danmaku_lines=self._chunk_danmaku_lines(chunk),
            topic_entity_context=topic_context,
            song_name_candidates=self.song_name_candidates,
        )
        self._stage_chunk_inputs(job_dir, chunk_clip, chunk_srt_text, prompt)
        short_prompt = (
            f"Open {job_dir}/prompt.md with view_file and follow it exactly. "
            f"Use only {job_dir}/prompt.md, {job_dir}/input.mp4, "
            f"{job_dir}/draft.srt, and {job_dir}/output.srt. "
            "Do not inspect any other file or directory. Do not use shell or terminal."
        )
        agy_inner = (
            f"/root/.local/bin/agy --sandbox --dangerously-skip-permissions --add-dir {shlex.quote(job_dir)} "
            f"--model {shlex.quote(AGY_MODEL)} -p {shlex.quote(short_prompt)} "
            f"--print-timeout {self.chunk_print_timeout}"
        )
        agy_cmd = (
            f"cd {shlex.quote(job_dir)} && "
            f"script -qec {shlex.quote(agy_inner)} /dev/null "
            f"> {shlex.quote(job_dir)}/agy.stdout "
            f"2> {shlex.quote(job_dir)}/agy.stderr; "
            f"echo rc=$? > {shlex.quote(job_dir)}/agy.rc"
        )
        self._run(
            [
                "ssh",
                self.host,
                f"nohup bash -c {shlex.quote(agy_cmd)} >/dev/null 2>&1 & echo started",
            ]
        )

        deadline = time.time() + self.chunk_poll_deadline_seconds
        rc_line = ""
        while time.time() < deadline:
            probe = subprocess.run(
                [
                    "ssh",
                    self.host,
                    f"cat {shlex.quote(job_dir)}/agy.rc 2>/dev/null",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
            )
            rc_line = probe.stdout.strip()
            if rc_line:
                break
            time.sleep(self.chunk_poll_interval_seconds)
        if not rc_line:
            subprocess.run(
                [
                    "ssh",
                    self.host,
                    f"pkill -f {shlex.quote(job_dir)} || true",
                ],
                check=False,
                capture_output=True,
                timeout=60,
            )
            raise AgyRunnerError(
                "AGY_TIMEOUT",
                "remote agy chunk did not finish within "
                f"{self.chunk_poll_deadline_seconds}s; see {self.host}:{job_dir}",
            )
        if rc_line != "rc=0":
            raise AgyRunnerError(
                "AGY_FAILED_RC",
                f"remote agy failed {rc_line}; see {self.host}:{job_dir}/agy.stderr",
            )

        fetched = subprocess.run(
            ["ssh", self.host, f"cat {shlex.quote(job_dir)}/output.srt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        corrected = (
            strip_markdown_fence(fetched.stdout)
            if fetched.returncode == 0
            else ""
        )
        if not looks_like_srt(corrected):
            raise AgyRunnerError(
                "AGY_EMPTY_OUTPUT",
                "remote agy exited rc=0 but produced no valid output.srt; "
                f"see {self.host}:{job_dir}",
            )
        try:
            validate_same_timing(chunk_srt_text, corrected)
        except RuntimeError as timing_error:
            try:
                corrected, sparse_audit = repair_sparse_refined_chunk(
                    chunk_srt_text,
                    corrected,
                )
            except ValueError:
                raise timing_error
            print(
                "[agy] bounded sparse-cue self-heal: "
                + json.dumps(
                    sparse_audit,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                flush=True,
            )
            validate_same_timing(chunk_srt_text, corrected)
        return corrected

    @staticmethod
    def _attestation(
        chunk: JingtingChunk,
        chunk_clip: Path,
        chunk_srt_text: str,
        corrected: str,
        *,
        provider: str,
    ) -> AgyChunkAttestation:
        draft_bytes = (
            chunk_srt_text
            if chunk_srt_text.endswith("\n")
            else chunk_srt_text + "\n"
        ).encode("utf-8")
        return AgyChunkAttestation(
            chunk_index=chunk.chunk_index,
            media_start_ms=chunk.media_start_ms,
            media_end_ms=chunk.media_end_ms,
            media_sha256=_sha256_file(chunk_clip),
            draft_srt_sha256=hashlib.sha256(draft_bytes).hexdigest(),
            refined_srt_sha256=hashlib.sha256(
                corrected.encode("utf-8")
            ).hexdigest(),
            executed_provider=provider,
            timing_validated=True,
            audio_input_attested=True,
        )

    def _fallback_chunk(
        self,
        chunk: JingtingChunk,
        chunk_clip: Path,
        chunk_srt_text: str,
    ) -> tuple[str, AgyChunkAttestation]:
        from scripts.gemini_slice_jingting import run_gemini_api

        with tempfile.TemporaryDirectory(prefix="ssh_agy_api_fb_") as fb_tmp:
            fb_draft = Path(fb_tmp) / "draft.srt"
            fb_out = Path(fb_tmp) / "out.srt"
            fb_draft.write_text(
                chunk_srt_text
                if chunk_srt_text.endswith("\n")
                else chunk_srt_text + "\n",
                encoding="utf-8",
            )
            run_gemini_api(
                str(chunk_clip),
                str(fb_draft),
                str(fb_out),
            )
            corrected = fb_out.read_text(encoding="utf-8")
        if not looks_like_srt(corrected):
            raise RuntimeError("GEMINI_API_FALLBACK_EMPTY")
        validate_same_timing(chunk_srt_text, corrected)
        return corrected, self._attestation(
            chunk,
            chunk_clip,
            chunk_srt_text,
            corrected,
            provider="gemini_api",
        )

    def __call__(
        self,
        media_path: Path,
        draft_srt_path: Path,
        output_srt_path: Path,
    ) -> AgyExecutionResult:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        srt_text = draft_srt_path.read_text(encoding="utf-8")
        chunks = plan_jingting_chunks(srt_text)
        if not chunks:
            raise AgyRunnerError(
                "AGY_NO_DRAFT_CUES",
                f"draft SRT has no parseable cues: {draft_srt_path}",
            )

        refined_pairs: list[tuple[JingtingChunk, str]] = []
        attestations: list[AgyChunkAttestation] = []
        api_fallback_chunks = 0
        with tempfile.TemporaryDirectory(prefix="ssh_agy_clips_") as clips_tmp:
            for chunk in chunks:
                chunk_clip = Path(clips_tmp) / (
                    f"chunk_{chunk.chunk_index:02d}.mp4"
                )
                self._encode_chunk_clip(media_path, chunk, chunk_clip)
                chunk_srt_text = chunk.chunk_srt_text()
                last_error: Exception | None = None
                corrected = ""
                for attempt in range(1, self.attempts_per_chunk + 1):
                    job_dir = (
                        f"/opt/bilive/jingting_jobs/ssh-{media_path.stem}-{stamp}"
                        f"-c{chunk.chunk_index:02d}a{attempt}"
                    )
                    try:
                        corrected = self._run_chunk_agy(
                            job_dir,
                            chunk_clip,
                            chunk_srt_text,
                            chunk,
                        )
                        last_error = None
                        break
                    except (AgyRunnerError, RuntimeError) as exc:
                        last_error = exc
                if last_error is None:
                    attestation = self._attestation(
                        chunk,
                        chunk_clip,
                        chunk_srt_text,
                        corrected,
                        provider="agy",
                    )
                else:
                    try:
                        corrected, attestation = self._fallback_chunk(
                            chunk,
                            chunk_clip,
                            chunk_srt_text,
                        )
                        api_fallback_chunks += 1
                        last_error = None
                    except Exception:
                        pass
                if last_error is not None:
                    raise last_error
                refined_pairs.append((chunk, corrected))
                attestations.append(attestation)

        merged = merge_refined_chunks(srt_text, refined_pairs)
        validate_same_timing(srt_text, merged)
        normalized_merged = merged if merged.endswith("\n") else merged + "\n"
        output_srt_path.write_text(normalized_merged, encoding="utf-8")
        return AgyExecutionResult(
            provider="agy",
            model=AGY_MODEL,
            agy_rc=0,
            provider_fallback_used=bool(api_fallback_chunks),
            provider_request_id=(
                f"{self.host}:jingting-chunked:{stamp}:{len(chunks)}chunks"
                + (
                    f":api_fb={api_fallback_chunks}"
                    if api_fallback_chunks
                    else ""
                )
            ),
            requested_provider="agy",
            executed_provider=(
                "agy+gemini_api"
                if api_fallback_chunks and api_fallback_chunks < len(chunks)
                else ("gemini_api" if api_fallback_chunks else "agy")
            ),
            source_media_sha256=_sha256_file(media_path),
            draft_srt_sha256=hashlib.sha256(
                srt_text.encode("utf-8")
            ).hexdigest(),
            refined_srt_sha256=hashlib.sha256(
                normalized_merged.encode("utf-8")
            ).hexdigest(),
            timing_validated=True,
            audio_input_attested=True,
            chunk_count=len(chunks),
            agy_chunk_count=len(chunks) - api_fallback_chunks,
            api_fallback_chunk_count=api_fallback_chunks,
            chunk_attestations=tuple(attestations),
        )


def build_ssh_agy_runner(
    host: str,
    *,
    danmaku_items=None,
    context_start_ms: int = 0,
    topic_entity_context_provider: Callable[[], str] | None = None,
    song_name_candidates: Sequence[str] = (),
):
    """Build a chunked, hash-attested second-listen runner."""

    return _RemoteJingtingRunner(
        host,
        danmaku_items=danmaku_items,
        context_start_ms=context_start_ms,
        topic_entity_context_provider=topic_entity_context_provider,
        song_name_candidates=song_name_candidates,
    )
