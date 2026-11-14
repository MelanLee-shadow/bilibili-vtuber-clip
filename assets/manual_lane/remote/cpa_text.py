#!/usr/bin/env python3
"""cpa_text.py PROMPT_FILE OUT_JSON [--model gpt-6.1-sol] [--effort medium] [--max-tokens 16000] [--stream]

The manual-lane CPA text client.  Same request shape and streaming aggregation as the title judge's
cpa_text.py (lidousha-title-cover judge-prompt.md): /responses, gpt-6.1-sol, effort medium, stream on.
Non-streaming long outputs hit the ~100-125 s Cloudflare/sudocode limit and come back as 524.

Deviation from the title judge's copy, per the 10-31 CPA diagnosis (cpa-503-20261031/RESULT.md):
after a 524 or a 503 auth_unavailable the client waits 150 s before any retry.  Re-sending at once only
gets a local 503 and keeps the credential cooling.

TASK-26: an answer counts only when the provider says it is whole.  The 11-02 probe (sudocode-chat route, gpt-6.1-sol,
medium, stream) showed response.created, response.in_progress, response.output_item.added, response.content_part.added,
response.output_text.delta x N, response.output_text.done, response.content_part.done, response.output_item.done,
response.completed; the terminal response had status "completed", no incomplete_details, and its message item status
"completed".  CPA (CLIProxyAPI) ends an answer the provider cut with response.incomplete (status "incomplete",
incomplete_details.reason max_output_tokens or content_filter, item status "incomplete") and an upstream failure after
the stream started with `event: error` or response.failed; a stream that just stops has no terminal event.  Each of
those is incomplete: the attempt is retried like an empty answer, and when the tries run out no text is written
(exit 1, `reason` says why).  Half an answer never reaches a parser.

Run on OCI3 by the root wrapper that loads cpa.env (CPA_BASE_URL / CPA_API_KEY); the key is never printed.
Writes OUT_JSON = {model, effort, stream, prompt_sha256, complete, reason, completion, text, partial_chars, usage,
attempts, budget_attempts, elapsed_seconds, http_log}; attempts counts every recorded request, while
budget_attempts excludes only definite, text-free quota refusals.
"""
import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

COOLDOWN_SECONDS = 150.0
IDLE_TIMEOUT_SECONDS = 600
RETRYABLE_STATUS = (408, 429)
TERMINAL_EVENTS = ("response.completed", "response.incomplete", "response.failed", "error")


def build_body(prompt, model="gpt-6.1-sol", effort="medium", max_tokens=16000, stream=True):
    body = {"model": model, "input": prompt, "reasoning": {"effort": effort}, "max_output_tokens": max_tokens}
    if stream:
        body["stream"] = True
    return body


def collect_stream_text(lines):
    """Aggregate a /responses SSE stream.  Returns (text, usage, completion); `completion` is the provider's word on
    the end of the answer (completion_of).  The text is the deltas, or the terminal response's text when no delta came."""
    buf, done_text, terminal, items = "", None, None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "response.output_text.delta":
            buf += event.get("delta", "")
        elif kind == "response.output_text.done":
            done_text = (done_text or "") + (event.get("text") or "")
        elif kind == "response.output_item.done":
            items.append(event.get("item") or {})
        elif kind in TERMINAL_EVENTS:
            terminal = event
    response = (terminal or {}).get("response") or {}
    if terminal and terminal.get("type") == "error" and not response:
        response = {"error": terminal.get("error") or {k: v for k, v in terminal.items() if k != "type"}}
    text = buf or text_from_payload(response)
    completion = completion_of(response, terminal=(terminal or {}).get("type"), text=text, done_text=done_text,
                               items=items)
    return text, response.get("usage"), completion


def completion_of(response, *, terminal="response.completed", text=None, done_text=None, items=()):
    """Whether `response` is a whole answer, by the provider's own signals: {complete, reason, terminal, status,
    incomplete_details, error, item_statuses}.  Whole only when the terminal event is response.completed, the status
    is "completed" with no incomplete_details or error, every message item is "completed", the streamed text equals
    the final text the provider sent (output_text.done, the terminal response), and the text is not empty.
    A non-streamed payload passes its own object with the default terminal."""
    text = text_from_payload(response) if text is None else text
    message_items = [i for i in [*items, *(response.get("output") or [])]
                     if isinstance(i, dict) and i.get("type") == "message"]
    item_statuses = [i.get("status") for i in message_items]
    final_text = text_from_payload(response)
    info = {"terminal": terminal, "status": response.get("status"),
            "incomplete_details": response.get("incomplete_details"), "error": response.get("error"),
            "item_statuses": item_statuses}
    detail = json.dumps(info["incomplete_details"] or info["error"], ensure_ascii=False)
    if terminal is None:
        reason = "NO_TERMINAL_EVENT: the stream ended without response.completed"
    elif terminal != "response.completed":
        reason = f"TERMINAL {terminal}: {detail}"
    elif info["status"] != "completed":
        reason = f"STATUS {info['status']}: {detail}"
    elif info["incomplete_details"] or info["error"]:
        reason = f"INCOMPLETE_DETAILS: {detail}"
    elif any(s != "completed" for s in item_statuses):
        reason = f"ITEM_STATUS: {item_statuses}"
    elif (done_text is not None and done_text != text) or (final_text and final_text != text):
        reason = f"TEXT_MISMATCH: streamed {len(text)} chars, provider final {len(done_text or final_text)}"
    elif not text.strip():
        reason = "EMPTY_ANSWER"
    else:
        reason = None
    return {"complete": reason is None, "reason": reason, **info}


def text_from_payload(payload):
    text = payload.get("output_text") or ""
    if not text:
        for item in payload.get("output") or []:
            if item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content") or [])
    return text


def wait_seconds(attempt, status=None, retry_after=None):
    # A missing reply is retried immediately once; a server's Retry-After is a lower bound.
    delay = 0.0 if attempt == 1 else min(60, 2 ** min(attempt - 1, 6)) + random.random()
    if status in (524, 503):
        delay = max(delay, COOLDOWN_SECONDS + random.random())
    if retry_after:
        try:
            requested = float(retry_after)
        except ValueError:
            try:
                requested = parsedate_to_datetime(retry_after).timestamp() - time.time()
            except (TypeError, ValueError, OverflowError):
                requested = 0.0
        delay = max(delay, requested)
    return delay


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def previous_output(path, prompt, a):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("CPA_PREVIOUS_OUTPUT_INVALID")
    if (value.get("prompt_sha256") != hashlib.sha256(prompt.encode()).hexdigest()
            or value.get("model") != a.model or value.get("effort") != a.effort
            or value.get("max_output_tokens", 16000) != a.max_tokens
            or value.get("stream") is not a.stream):
        return None
    return value


def quota_refusal(row):
    """Only a finished, text-free provider error with a definite quota code/type is exempt."""
    if row.get("status") != "FINISHED" or row.get("complete") is True or row.get("chars", 0) != 0:
        return False
    http = row.get("http")
    if type(http) is not int or not (http == 200 or 400 <= http <= 599):
        return False
    if http == 200 and row.get("complete") is not False:
        return False
    error = row.get("provider_error")
    if not isinstance(error, dict):
        raw = row.get("reason", "")
        prefixes = ("TERMINAL error: ", "TERMINAL response.failed: ")
        if not isinstance(raw, str) or not raw.startswith(prefixes):
            raw = row.get("body", "")
        else:
            raw = raw.split(": ", 1)[1]
        try:
            error = json.loads(raw)
            if isinstance(error, dict) and isinstance(error.get("error"), dict):
                error = error["error"]
        except (TypeError, ValueError):
            return False
    return isinstance(error, dict) and (error.get("code") in {"organization_spend_limit_exceeded", "insufficient_quota"}
                                       or error.get("type") == "insufficient_quota")


def initial_journal(path, previous, request_key):
    if path.exists():
        ledger = json.loads(path.read_text(encoding="utf-8"))
        if ledger.get("request_key") != request_key or not isinstance(ledger.get("http_log"), list):
            raise ValueError("CPA_RETRY_JOURNAL_INVALID")
    else:
        history = [] if previous is None else previous.get("http_log")
        count = 0 if previous is None else previous.get("attempts")
        if not isinstance(history, list) or type(count) is not int or len(history) != count:
            raise ValueError("CPA_LEGACY_ATTEMPTS_UNKNOWN")
        ledger = {"request_key": request_key, "http_log": history, "next_not_before": 0}
        write_json(path, ledger)
    log = ledger["http_log"]
    if any(not isinstance(row, dict) or row.get("attempt") != i for i, row in enumerate(log, 1)):
        raise ValueError("CPA_RETRY_JOURNAL_INVALID")
    for row in log:
        row["quota_refusal"] = quota_refusal(row)
    return ledger


def archive_output(path, journal_dir):
    out_path = Path(path)
    if journal_dir and out_path.exists():
        old = out_path.read_bytes()
        archived = Path(journal_dir) / (hashlib.sha256(old).hexdigest() + ".legacy-output.json")
        if not archived.exists():
            archived.write_bytes(old)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file")
    ap.add_argument("out_json")
    ap.add_argument("--model", default="gpt-6.1-sol")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--tries", type=int, default=10)
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--journal-dir")
    ap.add_argument("--logical-id")
    a = ap.parse_args(argv)
    if not 1 <= a.tries <= 10:
        raise SystemExit("tries must be between 1 and 10")
    base = os.environ.get("CPA_BASE_URL", "").rstrip("/")
    key = os.environ.get("CPA_API_KEY", "")
    if not base or not key:
        raise SystemExit("CPA_BASE_URL / CPA_API_KEY not set (load cpa.env first)")
    prompt = Path(a.prompt_file).read_text(encoding="utf-8")
    body = json.dumps(build_body(prompt, a.model, a.effort, a.max_tokens, a.stream)).encode()
    request_key = a.logical_id or hashlib.sha256(Path(a.out_json).name.encode() + b"\0" + body).hexdigest()
    if len(request_key) != 64 or any(c not in "0123456789abcdef" for c in request_key):
        raise SystemExit("invalid logical request id")
    if a.journal_dir:
        Path(a.journal_dir).mkdir(parents=True, exist_ok=True)
        lock = open(Path(a.journal_dir) / (request_key + ".lock"), "a", encoding="utf-8")
    else:
        lock = contextlib.nullcontext()
    with lock:
        if a.journal_dir:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("CPA_REQUEST_IN_FLIGHT")
        return dispatch(a, prompt, body, request_key, base, key)


def dispatch(a, prompt, body, request_key, base, key):
    t0 = time.time()
    previous = previous_output(a.out_json, prompt, a) if a.journal_dir else None
    if previous and previous.get("complete") is True and str(previous.get("text") or "").strip():
        print(f"{a.out_json}: REUSED terminal answer")
        return 0
    journal_path = Path(a.journal_dir) / (request_key + ".json") if a.journal_dir else None
    terminal_path = Path(a.journal_dir) / (request_key + ".terminal.json") if a.journal_dir else None
    if terminal_path and terminal_path.exists():
        terminal = previous_output(terminal_path, prompt, a)
        if not terminal or terminal.get("complete") is not True or not str(terminal.get("text") or "").strip():
            raise SystemExit("CPA_TERMINAL_IDENTITY_MISMATCH")
        archive_output(a.out_json, a.journal_dir)
        write_json(a.out_json, terminal)
        print(f"{a.out_json}: REUSED terminal answer")
        return 0
    ledger = initial_journal(journal_path, previous, request_key) if journal_path else {"http_log": [], "next_not_before": 0}
    log, text, usage, completion = ledger["http_log"], "", None, None
    if any(row.get("complete") is True for row in log):
        raise SystemExit("CPA_TERMINAL_ANSWER_MISSING")
    for run_attempt in range(1, a.tries + 1):
        if sum(not quota_refusal(row) for row in log) >= a.tries:
            break
        attempt = len(log) + 1
        delay = max(0, ledger.get("next_not_before", 0) - time.time())
        if delay:
            time.sleep(delay)
        log.append({"attempt": attempt, "status": "STARTED", "billing": "UNKNOWN",
                    "wire_sha256": hashlib.sha256(body).hexdigest()})
        if journal_path:
            write_json(journal_path, ledger)
        req = urllib.request.Request(f"{base}/responses", data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "Idempotency-Key": request_key, "X-Client-Request-Id": request_key[:32],
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        wait = None
        try:
            with urllib.request.urlopen(req, timeout=IDLE_TIMEOUT_SECONDS) as resp:
                if a.stream:
                    text, usage, completion = collect_stream_text(resp)
                else:
                    payload = json.loads(resp.read().decode("utf-8"))
                    text, usage, completion = text_from_payload(payload), payload.get("usage"), completion_of(payload)
            log[-1].update(status="FINISHED", http=200, chars=len(text), complete=completion["complete"],
                           reason=completion["reason"])
            if isinstance(completion.get("error"), dict):
                log[-1]["provider_error"] = {k: completion["error"].get(k) for k in ("code", "type")}
            if completion["complete"]:
                break
            wait = wait_seconds(attempt)
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", "replace")
            msg = body_text[:300]
            log[-1].update(status="FINISHED", http=exc.code, body=msg)
            try:
                error = json.loads(body_text)
                error = error.get("error", error) if isinstance(error, dict) else None
                if isinstance(error, dict):
                    log[-1]["provider_error"] = {k: error.get(k) for k in ("code", "type")}
            except ValueError:
                pass
            completion = {"complete": False, "reason": f"HTTP {exc.code}"}
            retry = exc.code in RETRYABLE_STATUS or exc.code >= 500 or (
                exc.code == 400 and "group_capability_unavailable" in msg)
            if not retry:
                break
            wait = wait_seconds(attempt, exc.code, exc.headers.get("Retry-After"))
        except (OSError, http.client.HTTPException, ValueError) as exc:  # network, timeout, cut or garbled stream
            log[-1].update(status="OUTCOME_UNKNOWN", error=repr(exc)[:200])
            completion = {"complete": False, "reason": f"ERROR {type(exc).__name__}"}
            wait = wait_seconds(attempt)
        log[-1]["quota_refusal"] = quota_refusal(log[-1])
        ledger["next_not_before"] = time.time() + (wait or 0) if run_attempt < a.tries else 0
        if journal_path:
            write_json(journal_path, ledger)
    complete = bool(completion and completion["complete"])
    for row in log:
        row["quota_refusal"] = quota_refusal(row)
    budget_attempts = sum(not quota_refusal(row) for row in log)
    reason = (completion or {}).get("reason") or "CPA_TEXT_RETRY_EXHAUSTED"
    if not complete and log and quota_refusal(log[-1]):
        reason = "CPA_QUOTA_REFUSED: " + reason
    out = {"model": a.model, "effort": a.effort, "max_output_tokens": a.max_tokens, "stream": a.stream,
           "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "complete": complete,
           "reason": None if complete else reason,
           "completion": completion, "text": text if complete else "", "partial_chars": 0 if complete else len(text),
           "usage": usage, "attempts": len(log), "budget_attempts": budget_attempts,
           "elapsed_seconds": round(time.time() - t0, 1), "http_log": log}
    if terminal_path and complete:
        write_json(terminal_path, out)
    archive_output(a.out_json, a.journal_dir)
    write_json(a.out_json, out)
    if journal_path:
        write_json(journal_path, ledger)
    print(f"{a.out_json}: {'OK' if complete else 'FAILED ' + str(out['reason'])} attempts={len(log)} "
          f"{out['elapsed_seconds']}s")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
