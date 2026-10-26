"""Durable complete review responses; callers still parse and authorize them."""

from __future__ import annotations

from src.autoslice.evidence_identity import file_sha256 as _evidence_file_sha256
import hashlib
import json
import os
import shlex
import stat
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from src.autoslice.qixi_transaction_core import (
    QixiTransactionCoreError,
    create_staged_inode,
    safe_parent,
    stable_regular_snapshot,
)
from src.autoslice.llm_client import LlmConfig, cached_completion, completion_identity

SCHEMA = "completed-review-response.v1"
RULING_POLICY = "original-answer-reuse.v1"
_active_response = ContextVar("completed_review_response", default=False)
_owner_context = ContextVar('ruling_owner_context', default=None)


@contextmanager
def ruling_material_scope(material: Mapping | None, *, cache_allowed=True, retain=None):
    """Carry an enclosing cache's material and retention admission downstream.

    Implementation snapshots are telemetry, while the actual answering model
    is bound separately. No nested raw cache may weaken the owner's key or
    turn a declined retention decision into permission to persist.
    """
    previous = _owner_context.get()
    semantic = {key: value for key, value in (material or {}).items()
                if key not in {'implementation', 'transport_implementation', 'model_identity'}}
    state = {'materials': [*(previous['materials'] if previous else []), semantic],
             'allowed': bool(cache_allowed and (previous['allowed'] if previous else True)),
             'guards': [*(previous['guards'] if previous else []), *([retain] if retain else [])],
             'volatile': previous['volatile'] if previous else {}}
    token = _owner_context.set(state)
    try:
        yield
    finally:
        _owner_context.reset(token)
        if previous is None:
            state['volatile'].clear()


def ruling_cache_allowed() -> bool:
    state = _owner_context.get()
    return state is None or state['allowed']


def ruling_owner_materials() -> list:
    state = _owner_context.get()
    return state['materials'] if state else []


def response_in_progress() -> bool:
    """Nested structured adapters must use the enclosing question's identity."""
    return _active_response.get()


def inherit_ruling_context(wrapper: Callable, source: Callable) -> Callable:
    """Keep a recording/routing wrapper on the source's ordinary raw lane."""
    for name in ('cpa_cache_identity', 'provider_runtime_binding', 'provider_diagnostics',
                 'completed_review_root', 'completed_review_binding', 'semantic_policy_version',
                 'cpa_resource_pressure', '_semantic_attempt_recorder', 'terminal_retry_capable',
                 'timeout_seconds', 'terminal_retry_wallclock_seconds'):
        if hasattr(source, name):
            setattr(wrapper, name, getattr(source, name))
    return wrapper


def _semantic_binding(binding: Mapping[str, object]) -> dict:
    """Execution locations and transport snapshots describe an observation, not a question."""
    result = dict(binding)
    result.pop("parser_sha256", None)
    provider = result.get("provider")
    if isinstance(provider, Mapping):
        runtime = provider.get("runtime") or {}
        result["provider"] = {"routing": provider.get("routing"),
            **({"endpoint_sha256": provider["endpoint_sha256"]} if provider.get("endpoint_sha256") else {}),
            # The answering service is still an identity guard. Its endpoint
            # is independent of credential/deployment/command locations.
            **{key: runtime[key] for key in ("provider_endpoint_host", "provider_endpoint_path")
               if key in runtime},
            # A remote adapter without a captured endpoint still cannot
            # promote another host's observation into this provider's reply.
            **({'provider_runtime_host': runtime['provider_runtime_host']}
               if runtime.get('provider_runtime_host') and not provider.get('endpoint_sha256')
               and not runtime.get('provider_endpoint_host') else {})}
    inputs = binding.get('inputs') or {}
    materials = _finding_materials(inputs)
    if materials is not None:
        # The producer supplied a complete typed (P,M), not a rendered prompt
        # with a few fields deleted. Raw request/response bytes stay in binding.
        from .finding_context import materials_for_model
        provider = result.get('provider') or {}
        materials = materials_for_model(materials, provider.get('routing') or {})
        return {'schema_version': result.get('schema_version'), 'stage': result.get('stage'),
                'semantic_policy': result.get('semantic_policy'), 'provider': provider,
                'finding_materials': materials,
                **({'owner_materials': result['owner_materials']} if result.get('owner_materials') else {})}
    return result


def _request_material_binding(binding, prompt):
    """One identity computation for live creation and all cold/legacy reads."""
    from .prompt_materials import canonical_materials, prompt_material_sha256
    result = _semantic_binding(binding)
    if 'owner_materials' in result:
        result['owner_materials'] = canonical_materials(result['owner_materials'])
    if 'finding_materials' not in result:
        for key in ('inputs',):
            if key in result:
                result[key] = canonical_materials(result[key])
        result['request_sha256'] = prompt_material_sha256(prompt)
    return result


def ruling_response(*, stage: str, prompt: str, llm_call: Callable,
                    inputs: Mapping[str, object], parser_path: Path,
                    response_schema=None, name="ruling", consumed_ruling_sink=None) -> tuple[str, bool]:
    """Return original bytes; the caller must rerun its current validation.

    Deliberately omit a format-retry validator from the transport context:
    schema/contract failure is a local error after one returned answer.
    """
    from .structured_output import call_json
    capture = None
    invocation = None
    try:
        if os.environ.get('AUTOSLICE_FINAL_REVIEW_MODE') == 'per_finding' or _finding_materials(inputs) is not None:
            from .finding_closure_shell import (
            RulingInvocation, current_ruling_capture, note_checkpoint_ruling, bind_ruling_inputs,
            ensure_provider_calls_allowed,
            )
            from .finding_closure_ports import RetainedObject
            capture = current_ruling_capture()
            if capture is not None:capture.raise_failure()
            retained_inputs=RetainedObject.from_value(inputs)
            retained_inputs=bind_ruling_inputs(retained_inputs,capture.finding if capture else None)
            inputs=retained_inputs.value()
            invocation = RulingInvocation(stage,prompt,retained_inputs,
                parser_path,response_schema,name)
            def retained(raw: str) -> bool:
                owner = _owner_context.get()
                return ruling_cache_allowed() and (owner is None or all(guard(raw) for guard in owner['guards']))
            if capture is not None and capture.router is not None:
                routed = capture.router.run_ruling(invocation,llm_call,retained)
                if routed is not None:
                    raw,hit,routed_checkpoint = routed
                    record=note_checkpoint_ruling(invocation=invocation,raw=raw,
                        identity=getattr(llm_call,'cpa_cache_identity',{}),retention_allowed=retained(raw),
                        checkpoint=routed_checkpoint)
                    if record is not None and consumed_ruling_sink is not None:consumed_ruling_sink(record)
                    recorder=getattr(llm_call,'_semantic_attempt_recorder',None)
                    if hit and callable(getattr(recorder,'note_cache_replay',None)):
                        recorder.note_cache_replay(prompt=prompt,completion=raw,
                            model_identity=completion_identity(getattr(llm_call,'cpa_cache_identity',{}),raw))
                    return raw,hit
        checkpoint = CompletedReviewCheckpoint.for_call(
            stage=stage, prompt=prompt, llm_call=llm_call, inputs=inputs, parser_path=parser_path)
        if checkpoint is None:
            checkpoint = CompletedReviewCheckpoint.for_ruling(
                stage=stage, prompt=prompt, llm_call=llm_call, inputs=inputs, parser_path=parser_path)
        def send(actual: str) -> str:
            if invocation is not None:ensure_provider_calls_allowed()
            return call_json(llm_call,actual,response_schema=response_schema,name=name,reuse=False)
        if checkpoint is None:
            raw=send(prompt)
            if capture is not None and invocation is not None:
                note_checkpoint_ruling(invocation=invocation,raw=raw,
                    identity=getattr(llm_call,'cpa_cache_identity',{}),retention_allowed=False,checkpoint=None)
            return raw, False
        raw = checkpoint.response(send, preserve_raw=True)
        if invocation is not None and (capture is not None or consumed_ruling_sink is not None):
            record=note_checkpoint_ruling(invocation=invocation,raw=raw,
                identity=getattr(llm_call,'cpa_cache_identity',{}),retention_allowed=retained(raw),checkpoint=checkpoint)
            if record is not None and consumed_ruling_sink is not None:consumed_ruling_sink(record)
        recorder = getattr(llm_call, '_semantic_attempt_recorder', None)
        if checkpoint.hit and callable(getattr(recorder, 'note_cache_replay', None)):
            recorder.note_cache_replay(prompt=prompt, completion=raw,
                model_identity=getattr(llm_call, 'cpa_cache_identity', {}))
        return raw, checkpoint.hit
    except BaseException as exc:
        if capture is not None:
            capture.failures.append(exc)
        raise


class CompletedReviewCheckpointError(ValueError):
    """A saved response is ambiguous, damaged or no longer bound."""
    failure_layer = 'CONTRACT_ERROR'


def _canonical(value: object) -> bytes:
    def encode(item: object) -> object:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        if isinstance(item, Path):
            return str(item)
        raise TypeError("unsupported completed review binding")

    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=encode).encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _directory(path: Path, *, create: bool) -> bool:
    if not path.is_absolute() or ".." in path.parts:
        raise CompletedReviewCheckpointError("COMPLETED_REVIEW_UNSAFE_PATH")
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if not create:
            # Check the existing ancestors even on a miss.
            return _directory(path.parent, create=False) and False
        _directory(path.parent, create=True)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise CompletedReviewCheckpointError("COMPLETED_REVIEW_UNSAFE_DIRECTORY")
    safe_parent(path / "entry")
    return True


class CompletedReviewCheckpoint:
    """One full request/provider binding, with immutable response observations.

    No mutable pointer is needed: a complete observation is installed directly
    under its binding. More than one response fails closed rather than choosing
    a convenient verdict. Incomplete/provider failures are never installed.
    """

    def __init__(self, root: Path, binding: Mapping[str, object], prompt: str):
        self.binding = json.loads(_canonical(binding))
        self.binding.setdefault('request_sha256', _sha(prompt.encode()))
        self.prompt = prompt
        self.root = root
        self.semantic_binding = _semantic_binding(self.binding)
        self.legacy_directory = root / _sha(_canonical(self.semantic_binding))
        self.semantic_binding = _request_material_binding(self.binding, prompt)
        self.directory = root / _sha(_canonical(self.semantic_binding))
        self.raw: str | None = None
        self.hit = False

    @classmethod
    def for_ruling(cls, *, stage: str, prompt: str, llm_call: Callable,
                   inputs: Mapping[str, object], parser_path: Path,
                   root: Path | None = None):
        """Raw answer storage for a model-bound ruling, including invalid answers.

        Unlike the producer transport checkpoint this also serves the existing
        model-bound acoustic/proposal adapters. It grants no consumer authority.
        """
        if not ruling_cache_allowed():
            return None
        routing = getattr(llm_call, "cpa_cache_identity", None)
        base = ((os.environ.get('AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT') if _finding_materials(inputs) is not None else None)
                or os.environ.get("AUTOSLICE_BASE") or os.environ.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT"))
        raw_root = root or getattr(llm_call, "completed_review_root", None)
        if raw_root is None and base:
            raw_root = Path(base) / "cache" / "completed-reviews"
        if raw_root is None or not isinstance(routing, Mapping) or not routing.get("models") or not routing.get("effort"):
            return None
        return cls(Path(raw_root), {
            "schema_version": SCHEMA, "stage": stage,
            "semantic_policy": getattr(llm_call, "semantic_policy_version", RULING_POLICY),
            "request_sha256": _sha(prompt.encode()), "inputs": dict(inputs),
            **({'owner_materials': ruling_owner_materials()} if ruling_owner_materials() else {}),
            "provider": {"routing": dict(routing),
                         **({'runtime': dict(llm_call.provider_runtime_binding)}
                            if isinstance(getattr(llm_call, 'provider_runtime_binding', None), Mapping) else {})},
            "parser_sha256": _evidence_file_sha256(parser_path),
        }, prompt)

    @classmethod
    def for_call(
        cls, *, stage: str, prompt: str, llm_call: Callable[[str], str],
        inputs: Mapping[str, object], parser_path: Path,
        root: Path | None = None,
    ) -> CompletedReviewCheckpoint | None:
        if not ruling_cache_allowed():
            return None
        if stage == "boundary-semantic-review" and not isinstance(inputs.get("producer_binding"), Mapping):
            # This checkpoint belongs to the ordinary producer source router.
            # Other boundary consumers keep their existing authority/replay lane.
            return None
        routing = getattr(llm_call, "cpa_cache_identity", None)
        runtime = getattr(llm_call, "provider_runtime_binding", None)
        if not isinstance(routing, Mapping) or not isinstance(runtime, Mapping):
            return None
        if (not routing.get("transport") or not routing.get("models") or not routing.get("effort")
                or not runtime.get("provider_transport")
                or not runtime.get("provider_credential_source")
                or runtime.get("provider_credential_source") == "UNBOUND"):
            return None
        # Bind the exact captured command environment, not a later reread of
        # cpa.env or ambient variables. Only a digest leaves this config; keys,
        # URL userinfo/query and other provider options never enter the receipt.
        transport_digest = None
        endpoint_digest = None
        if getattr(llm_call, "__module__", None) == "src.autoslice.llm_client":
            configs = [cell.cell_contents for cell in (getattr(llm_call, "__closure__", None) or ())
                       if isinstance(cell.cell_contents, LlmConfig)]
            if len(configs) != 1:
                return None
            config = configs[0]
            if config.transport == "command":
                env = config.command_child_env
                if not isinstance(env, Mapping) or not env.get("CPA_BASE_URL") or not env.get("CPA_API_KEY"):
                    # SSH adapters have no local snapshot of the remote CPA
                    # config; they retain their existing uncached execution.
                    return None
                try:
                    scripts = [Path(part) for part in shlex.split(config.command_template or "")
                               if Path(part).name == "llm_via_cpa.sh"]
                    if len(scripts) != 1 or not scripts[0].is_absolute():
                        return None
                    framing = stable_regular_snapshot(scripts[0], label="completed review CPA framing")
                except (OSError, ValueError, QixiTransactionCoreError):
                    return None
                if framing is None:
                    return None
                transport_digest = _sha(_canonical({
                    "command": config.command_template,
                    "env": {key: value for key, value in env.items() if key.startswith("CPA_")},
                    "request_framing_sha256": framing.sha256,
                }))
                endpoint = urlsplit(env["CPA_BASE_URL"])
                endpoint_digest = _sha(_canonical({"scheme": endpoint.scheme,
                    "host": endpoint.hostname, "port": endpoint.port, "path": endpoint.path}))
            else:
                # Direct adapters read credentials from ambient env at call
                # time, so this consumer has no captured config to replay.
                return None
        raw_root = root or getattr(llm_call, "completed_review_root", None)
        if raw_root is None:
            runtime_root = os.environ.get("AUTOSLICE_FINAL_REVIEW_RUNTIME_ROOT") or os.environ.get("AUTOSLICE_BASE")
            if not runtime_root:
                return None
            raw_root = Path(runtime_root) / "cache" / "completed-reviews"
        binding = {
            "schema_version": SCHEMA, "stage": stage,
            "semantic_policy": getattr(llm_call, "semantic_policy_version", RULING_POLICY),
            "request_sha256": _sha(prompt.encode("utf-8")),
            "provider": {"routing": dict(routing), "runtime": dict(runtime),
                         "endpoint_sha256": endpoint_digest,
                         "bound_transport_sha256": transport_digest},
            "inputs": dict(inputs),
            **({'owner_materials': ruling_owner_materials()} if ruling_owner_materials() else {}),
            "parser_sha256": _evidence_file_sha256(parser_path),
        }
        return cls(Path(raw_root), binding, prompt)

    def load(self) -> str | None:
        owner = _owner_context.get()
        if owner and str(self.directory) in owner['volatile']:
            self.raw = cached_completion(owner['volatile'][str(self.directory)],
                                         self.binding.get('provider', {}).get('routing', {}))
            self.hit = True
            return self.raw
        try:
            directories = []
            for candidate in dict.fromkeys((self.directory, self.legacy_directory)):
                if (_directory(candidate, create=False)
                        and [*candidate.glob('*.json'), *candidate.glob('.*.tmp')]):
                    directories.append(candidate)
            migrated = self._legacy_material_directory(exclude=directories)
            if migrated is not None:
                directories.append(migrated)
            if not directories:
                return None
            if len(directories) > 1:
                raise CompletedReviewCheckpointError('COMPLETED_REVIEW_AMBIGUOUS')
            directory = directories[0]
            # A complete staged inode is recoverable even if the process died
            # before its final name was installed. Partial/damaged bytes never
            # become a verdict, and duplicate names for identical bytes agree.
            paths = sorted([*directory.glob("*.json"), *directory.glob(".*.tmp")])
            if not paths:
                return None
            observations: dict[str, bytes] = {}
            for path in paths:
                snapshot = stable_regular_snapshot(path, label="completed review")
                named_sha = path.name.split(".")[1] if path.name.startswith(".") else path.stem
                if snapshot is None or named_sha != _sha(snapshot.payload):
                    raise CompletedReviewCheckpointError("COMPLETED_REVIEW_BYTES_DRIFT")
                observations[named_sha] = snapshot.payload
            if len(observations) != 1:
                raise CompletedReviewCheckpointError("COMPLETED_REVIEW_AMBIGUOUS")
            value = json.loads(next(iter(observations.values())))
            if not isinstance(value, dict):
                raise CompletedReviewCheckpointError("COMPLETED_REVIEW_BINDING_DRIFT")
            raw = value.get("response")
            original_prompt = value.get('request')
            saved_binding = value.get('binding')
            from .prompt_materials import prompt_material_sha256, same_prompt_materials, canonical_materials
            if (not isinstance(original_prompt, str) or not isinstance(saved_binding, Mapping)
                    or saved_binding.get('request_sha256') != _sha(original_prompt.encode())):
                raise CompletedReviewCheckpointError("COMPLETED_REVIEW_BINDING_DRIFT")
            saved_semantic = _request_material_binding(saved_binding, original_prompt)
            same_materials = ('finding_materials' in self.semantic_binding
                or same_prompt_materials(original_prompt, self.prompt))
            if isinstance(raw, str) and owner and not all(guard(raw) for guard in owner['guards']):
                raise CompletedReviewCheckpointError('COMPLETED_REVIEW_RETENTION_REJECTED')
            if (value.get("schema_version") != SCHEMA
                    or not isinstance(value.get("binding"), Mapping)
                    or saved_semantic != self.semantic_binding
                    or not same_materials or not isinstance(raw, str)
                    or not raw.strip() or value.get("response_sha256") != _sha(raw.encode("utf-8"))):
                raise CompletedReviewCheckpointError("COMPLETED_REVIEW_BINDING_DRIFT")
            self.raw = cached_completion(raw, self.binding.get('provider', {}).get('routing', {}))
            self.hit = True
            from .cpa_runtime import record_cpa_event
            record_cpa_event('cache_hit',{'kind':'completed_review','stage':self.binding.get('stage')})
            return self.raw
        except (OSError, QixiTransactionCoreError, TypeError, json.JSONDecodeError) as exc:
            raise CompletedReviewCheckpointError("COMPLETED_REVIEW_UNREADABLE") from exc

    def _legacy_material_directory(self, *, exclude=()):
        """Read old byte-keyed entries when an evidence bag merely reordered.

        No caller registration/migration write is needed. Candidate observations
        retain their original bytes and are rechecked by load(); ambiguity is
        never resolved by picking a convenient answer or asking again.
        """
        # Only the documented producer bags need old-key discovery. Ordinary
        # byte-identical old entries use legacy_directory above; never scan a
        # growing response store for unrelated brand-new questions.
        if 'finding_materials' in self.semantic_binding:
            # v2 has never used a prompt-derived directory. A cache miss is a
            # direct lookup, not a scan through every unrelated old question.
            return None
        from .prompt_materials import prompt_material_view, has_material_bag
        if (not any(has_material_bag(block) for block in prompt_material_view(self.prompt))
                or not _directory(self.root, create=False)):
            return None
        from .prompt_materials import canonical_materials, prompt_material_sha256
        matches = []
        for directory in sorted(self.root.iterdir()):
            if directory in exclude:
                continue
            if len(directory.name) != 64 or any(c not in '0123456789abcdef' for c in directory.name):
                continue
            try:
                valid_directory = _directory(directory, create=False)
            except (OSError, CompletedReviewCheckpointError, QixiTransactionCoreError):
                # Not one of this question's known locations. No readable
                # binding here can nominate it; direct bound paths stay strict.
                continue
            if not valid_directory:
                continue
            for path in [*directory.glob('*.json'), *directory.glob('.*.tmp')]:
                try:
                    snapshot = stable_regular_snapshot(path, label='legacy completed review')
                    value = json.loads(snapshot.payload) if snapshot is not None else None
                except (OSError, QixiTransactionCoreError, ValueError, UnicodeError):
                    # A partial record has no established question binding.
                    # If another complete record nominates this directory,
                    # load() rechecks EVERY sibling strictly, including this one.
                    continue
                if snapshot is None:
                    continue
                binding = value.get('binding') if isinstance(value, Mapping) else None
                prompt = value.get('request') if isinstance(value, Mapping) else None
                if not isinstance(binding, Mapping) or not isinstance(prompt, str):
                    continue
                semantic = _request_material_binding(binding, prompt)
                if semantic == self.semantic_binding:
                    matches.append(directory)
                    break
        if len(matches) > 1:
            raise CompletedReviewCheckpointError('COMPLETED_REVIEW_AMBIGUOUS')
        return matches[0] if matches else None

    def response(self, llm_call: Callable[[str], str], *, preserve_raw: bool = True) -> str:
        from .transcription_stage_cache import _locked
        _directory(self.root, create=True)
        with _locked(self.root, "dispatch-locks", self.directory.name) as locked_path:
            if locked_path is None:
                raise CompletedReviewCheckpointError("COMPLETED_REVIEW_LOCK_UNAVAILABLE")
            token = _active_response.set(True)
            try:
                return self._response_locked(llm_call, preserve_raw=preserve_raw)
            finally:
                _active_response.reset(token)

    def _response_locked(self, llm_call: Callable[[str], str], *, preserve_raw: bool) -> str:
        raw = self.load()
        hit = raw is not None
        if raw is None:
            from .structured_output import current_contract, response_contract
            contract = current_contract()
            # The raw-answer generator, including callers using response()
            # directly, owns the one-question rule. Validation runs downstream.
            with response_contract(contract.get('schema'), json_mode=contract.get('json_mode', False),
                                   name=contract.get('name', 'autoslice_response')):
                raw = llm_call(self.prompt)
        self.raw = raw
        # Preserve returned bytes BEFORE a local parser/contract can reject them.
        # A contract error must not create another opportunity to ask the model.
        if not self.hit and preserve_raw:
            self.save()
        self.hit = hit
        return raw

    def save(self) -> None:
        """Install a complete raw response; acceptance belongs to the consumer."""
        if not isinstance(self.raw, str) or not self.raw.strip():
            raise CompletedReviewCheckpointError("COMPLETED_REVIEW_RESPONSE_MISSING")
        routing = self.binding.get('provider', {}).get('routing', {})
        answered = completion_identity(routing, self.raw)
        if answered != routing:
            provider = {**self.binding['provider'], 'routing': answered}
            if 'provider_model' in provider.get('runtime', {}):
                provider['runtime'] = {**provider['runtime'], 'provider_model': answered['models']}
            binding = {**self.binding, 'provider': provider}
            actual = type(self)(self.root, binding, self.prompt)
            actual.raw = self.raw
            actual.save()
            return
        owner = _owner_context.get()
        if owner and not all(guard(self.raw) for guard in owner['guards']):
            # Keep only the already-returned bytes in this invocation's RAM.
            # In particular do not hash, serialize or persist a credential echo.
            owner['volatile'][str(self.directory)] = self.raw
            return
        # Re-observing the same answer through a relocated transport keeps the
        # original immutable observation (including its original telemetry).
        proposed_raw = self.raw
        existing_raw = self.load()
        self.raw = proposed_raw
        if existing_raw is not None and existing_raw == proposed_raw:
            return
        payload = _canonical({
            "schema_version": SCHEMA, "binding": self.binding,
            "request": self.prompt, "response": self.raw,
            "response_sha256": _sha(self.raw.encode("utf-8")),
        })
        try:
            _directory(self.directory, create=True)
            target = self.directory / (_sha(payload) + ".json")
            existing = stable_regular_snapshot(target, label="completed review")
            if existing is not None:
                if existing.payload != payload:
                    raise CompletedReviewCheckpointError("COMPLETED_REVIEW_BYTES_DRIFT")
                # A same-byte writer can race with a different complete
                # observation. Validate the whole binding before returning.
                if self.load() is None:
                    raise CompletedReviewCheckpointError("COMPLETED_REVIEW_SAVE_FAILED")
                return
            temporary = self.directory / ("." + _sha(payload) + "." + uuid.uuid4().hex + ".tmp")
            create_staged_inode(temporary, payload=payload, mode=0o600, label="completed review")
            try:
                os.link(temporary, target)
            except FileExistsError:
                installed = stable_regular_snapshot(target, label="completed review")
                if installed is None or installed.payload != payload:
                    raise CompletedReviewCheckpointError("COMPLETED_REVIEW_BYTES_DRIFT")
            temporary.unlink()
            descriptor = os.open(self.directory, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            # Another complete response must block this consumer too, rather
            # than waiting for a later restart to discover the disagreement.
            if self.load() is None:
                raise CompletedReviewCheckpointError("COMPLETED_REVIEW_SAVE_FAILED")
        except (OSError, QixiTransactionCoreError) as exc:
            raise CompletedReviewCheckpointError("COMPLETED_REVIEW_SAVE_FAILED") from exc


def _finding_materials(inputs):
    if not isinstance(inputs, Mapping):
        return None
    if inputs.get('finding_materials') is not None:
        return inputs['finding_materials']
    for key in ('request', 'material'):
        if isinstance(inputs.get(key), Mapping) and inputs[key].get('finding_materials') is not None:
            return inputs[key]['finding_materials']
    return None
