"""CPA vision dispatch and model-isolated cache for cover candidates and thumbnails."""

import base64
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request

from PIL import Image

from pick_frame import _definite_quota_error


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def output_text(payload):
    text = payload.get("output_text") or ""
    if not text:
        for item in payload.get("output") or []:
            if item.get("type") == "message":
                text += "".join(c.get("text", "") for c in item.get("content") or [])
    return text.strip()


def b64_jpeg(path, width=1280):
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _read_cached_vision(raw_path, key_path, expected_key):
    if os.path.exists(raw_path) and os.path.exists(key_path):
        try:
            with open(key_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if saved == expected_key:
                with open(raw_path, encoding="utf-8") as fh2:
                    return output_text(json.load(fh2))
        except (OSError, ValueError):
            return None
    return None


def _send_vision_payload(base, api_key, model, effort, content, max_tokens=1600):
    body = {"model": model, "reasoning": {"effort": effort}, "max_output_tokens": max_tokens,
            "input": [{"role": "user", "content": content}]}
    req = urllib.request.Request(f"{base}/responses", data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": "Bearer " + api_key, "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _validate_vision_payload(payload, call):
    call["usage"] = payload.get("usage") or {}
    statuses = [i.get("status") for i in payload.get("output") or [] if i.get("type") == "message"]
    if (payload.get("status") != "completed" or payload.get("incomplete_details") or payload.get("error")
            or any(s != "completed" for s in statuses) or not output_text(payload)):
        call["incomplete"] = {"status": payload.get("status"), "incomplete_details": payload.get("incomplete_details")}
        raise ValueError(f"CPA vision answer not complete: status={payload.get('status')}")


def vision(job, name, question, images, out_dir, *, calls):
    """One CPA vision reading (images: [(path, width)]), kept as OUT/<name>.response.json; an unchanged question on
    unchanged images for the SAME model is read from there, not asked again.  On definite quota exhaustion of
    `judge_model`, falls back to `fallback_model` (keeping model-scoped `.key.json` separate so recovery to primary
    never reuses a fallback answer).  Returns (answer text, reused)."""
    primary_model = job["judge_model"]
    fb_model = str(job.get("fallback_model", "gemini-4-neon") or "").strip()
    fb_effort = str(job.get("fallback_effort") or "low")
    raw_path = os.path.join(out_dir, f"{name}.response.json")
    key_path = raw_path + ".key.json"
    q_sha = hashlib.sha256(question.encode()).hexdigest()
    img_shas = [sha(p) for p, _ in images]
    key = {"question": q_sha, "images": img_shas, "model": primary_model}
    cached_text = _read_cached_vision(raw_path, key_path, key)
    if cached_text is not None:
        return cached_text, True
    base = os.environ.get("CPA_BASE_URL", "").strip().rstrip("/")
    api_key = os.environ.get("CPA_API_KEY", "")
    content = [{"type": "input_text", "text": question}] + [
        {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64_jpeg(p, w)} for p, w in images]
    call = {"kind": "vision", "candidate": name, "model": primary_model, "effort": "low", "rc": 1}
    calls.append(call)
    started = time.monotonic()
    quota_reason = None
    try:
        try:
            payload = _send_vision_payload(base, api_key, primary_model, "low", content)
            quota = _definite_quota_error(payload.get("error")) if isinstance(payload, dict) else None
            if quota:
                quota_reason = f"200 {quota}"
            else:
                _validate_vision_payload(payload, call)
                call["rc"] = 0
                with open(raw_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                with open(key_path, "w", encoding="utf-8") as fh:
                    json.dump(key, fh)
                return output_text(payload), False
        except urllib.error.HTTPError as exc:
            err_bytes = exc.read()
            quota = _definite_quota_error(err_bytes)
            if not quota:
                raise urllib.error.HTTPError(exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(err_bytes)) from None
            quota_reason = f"HTTP {exc.code} {quota}"
        if not fb_model or fb_model == primary_model:
            raise RuntimeError(f"CPA_QUOTA_REFUSED: {quota_reason}")
        fb_key = {"question": q_sha, "images": img_shas, "model": fb_model}
        fb_raw_path = os.path.join(out_dir, f"{name}.{fb_model}.response.json")
        fb_key_path = fb_raw_path + ".key.json"
        fb_cached = _read_cached_vision(fb_raw_path, fb_key_path, fb_key) or _read_cached_vision(raw_path, key_path, fb_key)
        if fb_cached is not None:
            calls.pop()
            return fb_cached, True
        call["model"] = fb_model
        call["effort"] = fb_effort
        call["fallback_from"] = primary_model
        call["fallback_reason"] = f"CPA_QUOTA_REFUSED: {quota_reason}"
        fb_min_tokens = int(job.get("fallback_min_output_tokens") or 32768)
        try:
            fb_payload = _send_vision_payload(base, api_key, fb_model, fb_effort, content, max(1600, fb_min_tokens))
            _validate_vision_payload(fb_payload, call)
        except urllib.error.HTTPError as fb_exc:
            fb_body = fb_exc.read()
            fb_code = _definite_quota_error(fb_body) or f"HTTP {fb_exc.code}"
            call["fallback_note"] = f"原模型 {primary_model} 额度不足（{quota_reason}），改用 {fb_model} 仍失败（{fb_code}）"
            raise RuntimeError(
                f"CPA_FALLBACK_FAILED: 原模型 {primary_model} 额度不足（{quota_reason}）；回退模型 {fb_model} 也不可用（{fb_code}）"
            ) from fb_exc
        except Exception as fb_exc:
            fb_why = f"{type(fb_exc).__name__}: {str(fb_exc)[:120]}"
            call["fallback_note"] = f"原模型 {primary_model} 额度不足（{quota_reason}），改用 {fb_model} 仍失败（{fb_why}）"
            raise RuntimeError(
                f"CPA_FALLBACK_FAILED: 原模型 {primary_model} 额度不足（{quota_reason}）；回退模型 {fb_model} 也不可用（{fb_why}）"
            ) from fb_exc
        call["fallback_note"] = f"原模型 {primary_model} 额度不足（{quota_reason}），改用 {fb_model}"
        call["rc"] = 0
        for p_raw, p_key in ((fb_raw_path, fb_key_path), (raw_path, key_path)):
            with open(p_raw, "w", encoding="utf-8") as fh:
                json.dump(fb_payload, fh, ensure_ascii=False)
            with open(p_key, "w", encoding="utf-8") as fh:
                json.dump(fb_key, fh)
        return output_text(fb_payload), False
    finally:
        if call in calls:
            call["elapsed_s"] = round(time.monotonic() - started, 1)
