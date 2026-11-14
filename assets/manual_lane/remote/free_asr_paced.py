#!/usr/bin/env python3
"""free_asr_paced.py CUT ASR_DIR STATE_DIR MIN_INTERVAL_S COOLDOWN_S ATTEMPTS RATE_LIMIT_ATTEMPTS PROVIDER... :
whole-clip BCUT / jianying recognition through the deployed repo's scripts/free_asr_client.py, paced (U152).

公开规则11-02: 「剪映被限流可以等等。另外，也确实不应该发的太猛。」 The client retries only inside one call; this is the
one pace for every clip and process on the host:
- one call per provider at a time (an flock per provider under STATE_DIR), at least MIN_INTERVAL_S after that
 provider's previous call ended, whichever clip made it;
- a rate-limited answer (HTTP 429, BCUT code -509) sets that provider's next call COOLDOWN_S ahead; after
 RATE_LIMIT_ATTEMPTS rate-limited answers the provider is given up for this clip (no short-interval re-sends);
- any other failure is retried up to ATTEMPTS calls in all, 10 s x attempt apart (the old shell loop's steps).
The providers run side by side (each paced on its own). For each provider that succeeds: ASR_DIR/<p>.srt and <p>.json,
written as free_asr_client.main writes them; every attempt is logged to ASR_DIR/<p>.log. The repo comes from
MANUAL_LANE_REMOTE_REPO; nothing is written into it. Prints FREE_ASR <p> OK|FAILED|RATE_LIMITED per provider.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True
RATE_LIMITED = re.compile(r"\b429\b|code -509|Too Many Requests", re.IGNORECASE)


class Pacer:
    """Serial calls per provider with a minimum gap and a cooldown after a rate limit.  The provider's state (when its
    next call may start) is a small JSON file next to its lock, so every process on the host shares it.  The clock and
    sleep are injected for the tests."""

    def __init__(self, state_dir: Path, *, min_interval_s: float, cooldown_s: float,
                 clock=time.time, sleep=time.sleep):
        self.state_dir = Path(state_dir)
        self.min_interval_s, self.cooldown_s = float(min_interval_s), float(cooldown_s)
        self.clock, self.sleep = clock, sleep
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _state(self, provider: str) -> Path:
        return self.state_dir / f"{provider}.json"

    def call(self, provider: str, fn):
        """Run fn() as this provider's next call: wait for the provider's turn, call, record when the next may start.
        Returns (result, error); a rate-limited error pushes the next call COOLDOWN_S ahead."""
        with open(self.state_dir / f"{provider}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                path = self._state(provider)
                try:
                    not_before = float(json.loads(path.read_text(encoding="utf-8")).get("not_before", 0))
                except (OSError, ValueError):
                    not_before = 0.0
                wait = not_before - self.clock()
                if wait > 0:
                    self.sleep(wait)
                try:
                    result, error = fn(), None
                except Exception as exc:  # noqa: BLE001 - every provider error is returned to the caller's retry rule
                    result, error = None, exc
                last = "OK" if error is None else "RATE_LIMITED" if rate_limited(error) else "ERROR"
                gap = self.cooldown_s if last == "RATE_LIMITED" else self.min_interval_s
                path.write_text(json.dumps({"not_before": self.clock() + gap, "last": last}), encoding="utf-8")
                return result, error
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def rate_limited(error: BaseException) -> bool:
    return bool(RATE_LIMITED.search(f"{type(error).__name__}: {error}"))


def run_provider(provider: str, fn, pacer: Pacer, *, attempts: int, rate_limit_attempts: int, log=print):
    """Call one provider until it answers, under the pacer.  Returns (result or None, status): OK, FAILED (ATTEMPTS
    other failures) or RATE_LIMITED (RATE_LIMIT_ATTEMPTS rate-limited answers: the provider is given up)."""
    failures = limited = 0
    while True:
        result, error = pacer.call(provider, fn)
        if error is None:
            log(f"{provider}: OK")
            return result, "OK"
        if rate_limited(error):
            limited += 1
            log(f"{provider}: rate limited ({limited}/{rate_limit_attempts}): {error}")
            if limited >= rate_limit_attempts:
                return None, "RATE_LIMITED"
            continue   # the pacer holds the next call COOLDOWN_S off
        failures += 1
        log(f"{provider}: failed ({failures}/{attempts}): {type(error).__name__}: {error}")
        if failures >= attempts:
            return None, "FAILED"
        pacer.sleep(10 * failures)


def main(argv: list[str]) -> int:
    cut, asr_dir, state_dir = Path(argv[1]), Path(argv[2]), Path(argv[3])
    min_interval_s, cooldown_s, attempts, rl_attempts = float(argv[4]), float(argv[5]), int(argv[6]), int(argv[7])
    providers = argv[8:]
    sys.path.insert(0, os.environ["MANUAL_LANE_REMOTE_REPO"])
    from scripts import (
        free_asr_client as fac,
    )

    asr_dir.mkdir(parents=True, exist_ok=True)
    sound = fac.extract_audio_mp3(cut)
    pacer = Pacer(state_dir, min_interval_s=min_interval_s, cooldown_s=cooldown_s)
    statuses: dict[str, str] = {}

    def one(provider: str) -> None:
        with open(asr_dir / f"{provider}.log", "w", encoding="utf-8") as logf:
            def log(line: str) -> None:
                logf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
                logf.flush()

            result, status = run_provider(provider, lambda: fac.transcribe(sound, provider), pacer,
                                          attempts=attempts, rate_limit_attempts=rl_attempts, log=log)
        if result is not None:
            (asr_dir / f"{provider}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
            (asr_dir / f"{provider}.srt").write_text(fac.to_srt(result, 0), encoding="utf-8")
        statuses[provider] = status

    threads = [threading.Thread(target=one, args=(p,)) for p in providers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for p in providers:
        print(f"FREE_ASR {p} {statuses.get(p, 'FAILED')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
