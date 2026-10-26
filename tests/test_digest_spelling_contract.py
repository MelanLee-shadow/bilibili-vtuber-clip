"""SHA-256 spelling belongs to evidence_identity, across real receipt boundaries."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from PIL import Image
import pytest

from src.autoslice import cpa_frame_witness
from src.autoslice.title_cover_candidates import witness_verdict
from src.autoslice.title_cover_staging import _probe


@pytest.fixture
def real_batch(tmp_path, monkeypatch):
    """Replace only HTTP, leaving the real producer, persistence and reader."""
    answer = {"face": [100, 100, 600, 700], "reason": "local HTTP fixture"}
    raw = json.dumps({"output_text": json.dumps(answer)}).encode()
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return raw

    def local_http(request, **_kwargs):
        assert request.full_url == "https://digest-fixture.invalid/v1/responses"
        body = json.loads(request.data)
        sent.append([
            base64.b64decode(item["image_url"].split(",", 1)[1])
            for item in body["input"][0]["content"] if item["type"] == "input_image"
        ])
        return Response()

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("AUTOSLICE_BASE", str(runtime))
    monkeypatch.setenv("AUTOSLICE_CPA_USAGE_FILE", str(tmp_path / "http-usage.jsonl"))
    monkeypatch.setattr(cpa_frame_witness.urllib.request, "urlopen", local_http)

    def produce(paths, question, **kwargs):
        return cpa_frame_witness.batch_jpeg_vision_probe(
            paths, question,
            api_base="https://digest-fixture.invalid/v1", api_key="offline-fixture",
            **kwargs,
        )

    return produce, answer, raw, sent


@pytest.mark.parametrize("image_count", [1, 2, 4])
def test_real_batch_witness_roundtrip(tmp_path, real_batch, image_count):
    _, answer, raw, sent = real_batch
    paths = []
    for index in range(image_count):
        path = tmp_path / f"source-{index}.png"
        Image.new("RGB", (73 + index, 51 + index), (index * 50, 50, 170)).save(path)
        paths.append(path)
    directory = tmp_path / "candidate"
    directory.mkdir()
    verdict, witness, snapshots = _probe(
        tmp_path, directory, "geometry", paths, "Inspect these exact pixels",
        probe=cpa_frame_witness.batch_jpeg_vision_probe,
        base_url="https://digest-fixture.invalid/v1", api_key="offline-fixture",
    )
    assert verdict == answer
    assert sent == [[(tmp_path / path).read_bytes() for path in snapshots]]
    assert (directory / "geometry.provider-response.json").read_bytes() == raw
    persisted = json.loads((directory / "geometry.response.json").read_text())
    assert persisted == witness
    assert witness_verdict(
        persisted, [tmp_path / path for path in snapshots],
        provider_response=directory / "geometry.provider-response.json",
    ) == answer


def test_shared_sha256_value_domain():
    from src.autoslice.evidence_identity import same_sha256, sha256_hex, sha256_ref

    digest = "0123456789abcdef" * 4
    legal = (digest, "sha256:" + digest)
    for left in legal:
        assert sha256_hex(left) == digest
        assert sha256_ref(left) == legal[1]
        for right in legal:
            assert same_sha256(left, right) is True
    assert same_sha256(digest, "f" * 64) is False
    invalid = (
        None, False, 64, b"a" * 64, [], {}, "", "a" * 63, "a" * 65,
        digest.upper(), "sha256:" + digest.upper(), "g" * 64,
        "SHA256:" + digest, "sha512:" + digest, "sha256:sha256:" + digest,
        " " + digest, digest + "\n", "sha256: " + digest,
    )
    for bad in invalid:
        assert same_sha256(bad, digest) is False
        assert same_sha256(digest, bad) is False
        assert same_sha256(bad, bad) is False
        with pytest.raises(ValueError):
            sha256_hex(bad)
        with pytest.raises(ValueError):
            sha256_ref(bad)


def test_historical_prefixed_receipt_still_binds_original_material(tmp_path, real_batch):
    produce, answer, _, _ = real_batch
    path = tmp_path / "image.jpg"
    path.write_bytes(b"historical image bytes")
    response = tmp_path / "raw.json"
    witness = produce([path], "Inspect", raw_response_path=response)
    # The older injected/archived prefixed representation remains readable.
    prefixed = deepcopy(witness)
    for row in prefixed["images"]:
        row["image_sha256"] = "sha256:" + row["image_sha256"]
    binding = json.dumps({
        "question": prefixed["question"],
        "image_sha256s": [row["image_sha256"] for row in prefixed["images"]],
        "model": prefixed["model"],
    }, sort_keys=True)
    prefixed["prompt_sha256"] = "sha256:" + hashlib.sha256(binding.encode()).hexdigest()
    prefixed["response_sha256"] = "sha256:" + prefixed["response_sha256"]
    assert witness_verdict(prefixed, [path], provider_response=response) == answer
    # Reformatting recorded image material without rebinding its prompt fails.
    prefixed["prompt_sha256"] = witness["prompt_sha256"]
    with pytest.raises(ValueError, match="COVER_CPA_PROMPT_HASH_MISMATCH"):
        witness_verdict(prefixed, [path], provider_response=response)


@pytest.mark.parametrize("change,error", [
    ("provider", "COVER_CPA_IMAGE_OBSERVATION_REQUIRED"),
    ("status", "COVER_CPA_IMAGE_OBSERVATION_REQUIRED"),
    ("image_digest", "COVER_CPA_IMAGE_HASH_MISMATCH"),
    ("image_invalid", "COVER_CPA_IMAGE_HASH_MISMATCH"),
    ("image_order", "COVER_CPA_IMAGE_HASH_MISMATCH"),
    ("image_extra", "COVER_CPA_IMAGE_HASH_MISMATCH"),
    ("image_missing", "COVER_CPA_IMAGE_HASH_MISMATCH"),
    ("response_invalid", "COVER_CPA_RESPONSE_HASH_REQUIRED"),
    ("response_digest", "COVER_CPA_ORIGINAL_RESPONSE_HASH_DRIFT"),
    ("prompt_digest", "COVER_CPA_PROMPT_HASH_MISMATCH"),
    ("question", "COVER_CPA_REQUEST_IDENTITY_REQUIRED"),
    ("model", "COVER_CPA_REQUEST_IDENTITY_REQUIRED"),
    ("answer", "COVER_CPA_ANSWER_NOT_ORIGINAL_RESPONSE"),
    ("response_missing", "COVER_CPA_ORIGINAL_RESPONSE_REQUIRED"),
    ("response_bytes", "COVER_CPA_ORIGINAL_RESPONSE_HASH_DRIFT"),
    ("image_bytes", "COVER_CPA_IMAGE_HASH_MISMATCH"),
])
def test_witness_rejection_controls(tmp_path, real_batch, change, error):
    produce, _, _, _ = real_batch
    paths = [tmp_path / "first.jpg", tmp_path / "second.jpg"]
    for index, path in enumerate(paths):
        path.write_bytes(f"exact image {index}".encode())
    response = tmp_path / "raw.json"
    witness = produce(paths, "Inspect", raw_response_path=response)
    if change in {"provider", "status", "question", "model", "answer"}:
        witness[change] = "" if change != "answer" else "{}"
    elif change == "image_digest":
        witness["images"][0]["image_sha256"] = "0" * 64
    elif change == "image_invalid":
        witness["images"][0]["image_sha256"] = "A" * 64
    elif change == "image_order":
        witness["images"].reverse()
    elif change == "image_extra":
        witness["images"].append(None)
    elif change == "image_missing":
        witness["images"].pop()
    elif change == "response_invalid":
        witness["response_sha256"] = "sha256:" + "g" * 64
    elif change == "response_digest":
        witness["response_sha256"] = "0" * 64
    elif change == "prompt_digest":
        witness["prompt_sha256"] = "0" * 64
    elif change == "response_missing":
        response = None
    elif change == "response_bytes":
        response.write_bytes(b"modified provider response")
    elif change == "image_bytes":
        paths[0].write_bytes(b"modified image")
    with pytest.raises(ValueError, match=error):
        witness_verdict(witness, paths, provider_response=response)


# Landing snapshot: lower these ceilings when inline spellings are removed.
# Never increase them; evidence_identity is the sole new spelling definition.
INLINE_SPELLING_CEILINGS = {'concat': 1378, 'fstring': 86}
INLINE_SPELLINGS = {
    "concat": re.compile(r'''["']sha256:["']\s*\+'''),
    "fstring": re.compile(r'''f["']sha256:\{'''),
}


def test_inline_digest_spelling_ratchet():
    root = Path(__file__).resolve().parents[1]
    counts = dict.fromkeys(INLINE_SPELLINGS, 0)
    for folder in ("src", "scripts"):
        for path in (root / folder).rglob("*"):
            if (not path.is_file() or "__pycache__" in path.parts
                    or path == root / "src/autoslice/evidence_identity.py"):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            for name, pattern in INLINE_SPELLINGS.items():
                counts[name] += len(pattern.findall(source))
    assert all(counts[name] <= ceiling for name, ceiling in INLINE_SPELLING_CEILINGS.items()), (
        f"New inline SHA-256 spelling: {counts}; use evidence_identity instead of raising "
        f"the landing ceilings {INLINE_SPELLING_CEILINGS}"
    )
