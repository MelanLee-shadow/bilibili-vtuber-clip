"""New user diagnosis represented as synthetic public-path controls, not old-run replay."""

import hashlib
import json
import socket
import subprocess
import sys

import pytest

from tests.test_span_phoneme_resolution import Observers, model_for, run_case, srt
from tests.test_native_context_witness import _request, _receipt, native_digest
from src.autoslice import native_context_witness as native
from src.autoslice.span_phoneme_resolution import validated_resolution


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOSLICE_BASE", str(tmp_path / "state"))

    def no(*a, **kw):
        pytest.fail("No external provider or network allowed")

    monkeypatch.setattr(socket.socket, "connect", no)
    monkeypatch.setattr(socket, "create_connection", no)


@pytest.mark.parametrize(
    "audio,choice", [(False, "PROPOSED"), (True, "CURRENT"), (True, "NEITHER"), (True, "PROPOSED")]
)
def test_every_public_span_judgment_has_compiled_outcome(audio, choice):
    model = model_for(audio=audio, choice=choice)
    _, audit, _, _, _ = run_case(model=model)
    seen = []

    def visit(value):
        if isinstance(value, dict):
            judge = value.get("judge")
            if (
                value.get("schema_version") == "acoustic-witness-adjudication.v1"
                and isinstance(judge, dict)
                and isinstance(judge.get("span_review"), dict)
            ):
                seen.append(value)
            for x in value.values():
                visit(x)
        elif isinstance(value, list):
            for x in value:
                visit(x)

    visit(audit)
    assert seen
    for entry in seen:
        assert bool(entry.get("span_context_resolution")) != bool(
            entry.get("span_termination_blocked_reason")
        )
        if entry.get("span_context_resolution"):
            validated_resolution(entry["span_context_resolution"])
        assert entry["judge"]["needs_audio_requested"] is audio


def evidence_model():
    """Locate each provider's actual quote, including disagreement; choose no invented sound."""
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        result = {
            "choice": "PROPOSED",
            "candidate_id": "PROPOSAL",
            "needs_audio": True,
            "current_utterance_supported": False,
            "current_utterance_support_reason": "synthetic unsupported unit",
            "reason": "synthetic context prefers the supported option",
        }
        if "\nSPAN_REVIEW_INPUT\n" in prompt:
            data = json.loads(
                prompt.split("\nSPAN_REVIEW_INPUT\n")[1].split("\nEND_SPAN_REVIEW_INPUT\n")[0]
            )
            align = []
            for o in data["observations"]:
                text = o["text"]
                blind = o["kind"] == "blind_pinyin"
                if blind:
                    units = text.split()
                    start = units.index("shuo") + 1
                    end = units.index("ba")
                    quote = " ".join(units[start:end])
                else:
                    start = text.index("说") + 1
                    end = text.index("吧")
                    quote = text[start:end]
                align.append(
                    {"observation_id": o["observation_id"], "span": [start, end], "quote": quote}
                )
            binding = data["binding"]
            result["span_review"] = {
                "binding_sha256": binding["binding_sha256"],
                "request_sha256": data["request_sha256"],
                "context_basis": "Synthetic interlocutor context supports the acoustically attested candidate.",
                "all_audio_questions_located": True,
                "target_needs_audio": True,
                "alignments": align,
                "audio_questions": [
                    {
                        "codepoint_span": binding["codepoint_span"],
                        "reason": "Which supported alternative?",
                        "observation_ids": [o["observation_id"] for o in data["observations"]],
                    }
                ],
            }
        return json.dumps(result, ensure_ascii=False)

    answer.prompts = prompts
    return answer


def majority_case(
    *, new="你的坏话", heard="shuo ni de huai hua ba", mai="说你的坏话吧", moss="说人坏话吧"
):
    observer = Observers(heard=heard, transcripts={"mai": mai, "moss": moss})
    return run_case(
        text="说能发挥吧", old="能发挥", new=new, verifier=observer, model=evidence_model()
    )


def test_current_rejected_by_all_accepts_acoustically_supported_plurality(tmp_path):
    output, audit, _, _, _ = majority_case()
    assert output == srt("说你的坏话吧"), audit.get("witness_judge")
    assert audit["policy_branch"] == "CONTEXT_PHONEME_FIT"
    value = validated_resolution(audit["span_context_resolution"])
    assert value["decision_rule"] == "UNSUPPORTED_CURRENT_ACOUSTIC_PLURALITY_WITH_CONTEXT"
    assert not value["support_summary"]["current_supported"]
    assert any(not x["compatible"] for x in value["comparisons"])  # dissent is retained
    path = tmp_path / "adjudication.json"
    path.write_text(json.dumps(audit, ensure_ascii=False))
    command = [
        sys.executable,
        "-B",
        "-c",
        "import json,sys; from src.autoslice.span_phoneme_resolution import valid_adjudication; "
        'a=json.load(open(sys.argv[1])); assert valid_adjudication(a); print("COLD_VALIDATED")',
        str(path),
    ]
    child = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    assert "COLD_VALIDATED" in child.stdout


@pytest.mark.parametrize(
    "variant",
    [
        "current_supported",
        "no_candidate_support",
        "losing_plurality",
        "partial_missing_current",
        "missing_route",
    ],
)
def test_no_acoustic_authority_cannot_be_manufactured(variant):
    options = {}
    if variant == "current_supported":
        options["moss"] = "说能发挥吧"
    elif variant == "no_candidate_support":
        options["new"] = "花言巧语"
    elif variant == "losing_plurality":
        options.update(heard="shuo ren huai hua ba", mai="说人坏话吧", moss="说你的坏话吧")
    elif variant == "partial_missing_current":
        options["moss"] = "说能发吧"
    if variant == "missing_route":
        observer = Observers(
            heard="shuo ni de huai hua ba",
            transcripts={"mai": "说你的坏话吧", "moss": "说人坏话吧"},
        )
        observer.missing = "moss"
        output, audit, *_ = run_case(
            text="说能发挥吧",
            old="能发挥",
            new="你的坏话",
            verifier=observer,
            model=evidence_model(),
        )
    else:
        output, audit, *_ = majority_case(**options)
    assert output == srt("说能发挥吧")
    assert not audit["repaired"] and "span_context_resolution" not in audit


@pytest.fixture
def recorded_native(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    source.write_bytes(b"synthetic stable physical source")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / "native"
    calls = []

    def factory(**kwargs):
        provider = kwargs["provider"]

        def observe(*, start_ms, end_ms):
            calls.append(provider)
            r = _receipt(
                source_sha256=source_sha,
                start_ms=start_ms,
                end_ms=end_ms,
                provider=provider,
                transcript="说你的坏话吧",
            )
            r["model"] = native.NATIVE_MODELS[provider]
            r["receipt_sha256"] = native_digest(
                {k: v for k, v in r.items() if k != "receipt_sha256"}
            )
            return r

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", factory)

    def build():
        return native.build_independent_context_witnesses(
            fallback=lambda r: {}, source_media=source, output_dir=root, providers=("mai", "moss")
        )

    request = _request()
    rows = build().observe_independent_witnesses(request)
    assert len(rows) == 2 and all(r["status"] == "OBSERVED" for r in rows)
    return build, source, root, calls, request


def test_cold_same_clock_different_candidate_reuses_native_without_provider(
    recorded_native, monkeypatch
):
    build, source, root, calls, original = recorded_native
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    changed = _request(evidence_id="f" * 64)
    assert changed["request_sha256"] != original["request_sha256"]

    def forbidden(**kw):
        raise AssertionError("cold metadata variant must not dispatch")

    monkeypatch.setattr(native, "build_native_foreign_witness", forbidden)
    cold = build()
    rows = cold.observe_cached_independent_witnesses(changed)
    assert len(rows) == 2
    assert all(
        r["served_from_cache"] and r["request_sha256"] == changed["request_sha256"] for r in rows
    )
    assert calls == ["mai", "moss"] and all(p.read_bytes() == b for p, b in before.items())
    assert len(cold.observe_independent_witnesses(changed)) == 2
    assert calls == ["mai", "moss"]


@pytest.mark.parametrize(
    "damage", ["source", "target", "clock", "native_sha", "candidate_contamination", "symlink"]
)
def test_metadata_variant_reuse_rejects_wrong_identity(recorded_native, damage):
    build, source, root, calls, original = recorded_native
    changed = _request(evidence_id="f" * 64)
    if damage == "source":
        source.write_bytes(b"other physical source")
    elif damage == "target":
        changed = _request(evidence_id="f" * 64, matched_end_ms=4010)
    elif damage == "clock":
        changed = _request(
            evidence_id="f" * 64, offset_ms=9990, matched_start_ms=2510, matched_end_ms=4010
        )
    else:
        for path in root.glob("*/native-context/*.json"):
            value = json.loads(path.read_bytes())
            if damage == "native_sha":
                value["native_observation"]["input_audio_sha256"] = "d" * 64
            elif damage == "candidate_contamination":
                value["request"]["current_cue"] = "sighted leaked candidate"
            elif damage == "symlink":
                other = path.with_suffix(".original")
                path.rename(other)
                path.symlink_to(other)
                continue
            path.write_text(json.dumps(value))
    assert build().observe_cached_independent_witnesses(changed) == []
    assert calls == ["mai", "moss"]


def test_candidate_only_rebinding_is_not_new_hearing_material(recorded_native):
    from src.autoslice.acoustic_witness_adjudication import retry_judge_evidence

    build, _, _, calls, original = recorded_native
    first = build().observe_cached_independent_witnesses(original)
    changed = _request(evidence_id="9" * 64)
    second = build().observe_cached_independent_witnesses(changed)
    assert len(first) == len(second) == 2
    assert retry_judge_evidence(original, first[0]) == retry_judge_evidence(changed, second[0])
    assert calls == ["mai", "moss"]


def test_fresh_process_replays_candidate_independent_cache(recorded_native, tmp_path):
    build, source, root, calls, _ = recorded_native
    request = _request(evidence_id="8" * 64)
    request_file = tmp_path / "new-candidate-blind-request.json"
    request_file.write_text(json.dumps(request))
    script = """import sys,json,os
from pathlib import Path
def guard(event,args):
 if event in ('socket.connect','socket.getaddrinfo'):raise AssertionError('No network')
 if event=='open' and isinstance(args[0],str) and args[0].endswith('.env'):raise AssertionError('No key read')
sys.addaudithook(guard)
from src.autoslice.native_context_witness import build_independent_context_witnesses
request=json.loads(Path(sys.argv[3]).read_bytes())
c=build_independent_context_witnesses(fallback=lambda r:{},source_media=Path(sys.argv[1]),output_dir=Path(sys.argv[2]),providers=('mai','moss'))
rows=c.observe_cached_independent_witnesses(request)
assert len(rows)==2 and all(r['served_from_cache'] for r in rows)
assert all(r['request_sha256']==request['request_sha256'] for r in rows)
print(json.dumps({'pid':os.getpid(),'routes_attempted':len(rows),'served_from_cache':True,'response_sha256':[r['response_sha256'] for r in rows]}))
"""
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    child = subprocess.run(
        [sys.executable, "-B", "-c", script, str(source), str(root), str(request_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["routes_attempted"] == 2
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert calls == ["mai", "moss"]
    (tmp_path / "COLD-NATIVE-RESULT.json").write_text(child.stdout)


def test_duplicate_dissent_does_not_add_route_votes():
    from copy import deepcopy

    class Copies(Observers):
        def observe_independent_witnesses(self, request):
            rows = super().observe_independent_witnesses(request)
            for number in range(3):
                copy = deepcopy(rows[-1])
                copy["diagnostic_alias"] = f"same-acquisition-copy-{number}"
                rows.append(copy)
            return rows

    verifier = Copies(
        heard="shuo ni de huai hua ba", transcripts={"mai": "说你的坏话吧", "moss": "说人坏话吧"}
    )
    output, audit, *_ = run_case(
        text="说能发挥吧", old="能发挥", new="你的坏话", verifier=verifier, model=evidence_model()
    )
    assert output == srt("说你的坏话吧")
    value = validated_resolution(audit["span_context_resolution"])
    assert value["support_summary"]["independent_route_count"] == 3
    assert value["support_summary"]["proposed_route_votes"] == 2
    assert len([r for r in value["comparisons"] if not r["compatible"]]) >= 1


def test_candidate_variant_retains_conflicting_acquisitions_from_same_route(
    recorded_native, monkeypatch, tmp_path
):
    """Two real fixture writer runs, not hand-edited envelopes; no majority by copying."""
    _, source, first_root, calls, request = recorded_native
    second_root = tmp_path / "second-native"
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()

    def provider_factory(**kwargs):
        provider = kwargs["provider"]

        def observe(*, start_ms, end_ms):
            calls.append(provider)
            row = _receipt(
                source_sha256=source_sha,
                start_ms=start_ms,
                end_ms=end_ms,
                provider=provider,
                transcript="说人坏话吧",
            )
            row["model"] = native.NATIVE_MODELS[provider]
            row["response_sha256"] = hashlib.sha256(
                (provider + "different raw reply").encode()
            ).hexdigest()
            row["receipt_sha256"] = native_digest(
                {k: v for k, v in row.items() if k != "receipt_sha256"}
            )
            return row

        return observe

    monkeypatch.setattr(native, "build_native_foreign_witness", provider_factory)
    other = native.build_independent_context_witnesses(
        fallback=lambda r: {},
        source_media=source,
        output_dir=second_root,
        providers=("mai", "moss"),
    )
    assert len(other.observe_independent_witnesses(request)) == 2
    assert len(calls) == 4
    new_request = _request(evidence_id="7" * 64)
    consumer = native.build_independent_context_witnesses(
        fallback=lambda r: {},
        source_media=source,
        output_dir=tmp_path / "consumer",
        providers=("mai", "moss"),
        cached_observation_roots=(first_root, second_root),
    )
    cached = consumer.observe_cached_independent_witnesses(new_request)
    assert len(cached) == 4
    rows = consumer.observe_independent_witnesses(new_request)
    assert len(rows) == 4
    assert {r["exact_transcript"] for r in rows} == {"说你的坏话吧", "说人坏话吧"}
    assert all(r["served_from_cache"] for r in rows) and len(calls) == 4
