"""A completed Argon envelope does not waive current validation of saved gates."""
from copy import deepcopy
from src.autoslice.exact_final_witness_authority import history_audio_recovery_from_adjudication
from tests.test_argon_consumer_integration import scene, offline  # noqa: F401

def test_saved_pending_with_completed_argon_still_runs_current_acoustic_gate(scene):
    original, pending = scene.run()
    assert original == scene.srt
    assert pending["argon_escalation"]["status"] == "PENDING"
    assert history_audio_recovery_from_adjudication(pending) is not None
    frozen = deepcopy(pending)
    carried = {**scene.finding, "exact_release_adjudication": pending}
    output, audit = scene.run(scene.build(), finding=carried)
    # Exact 757f rejects this unresolved saved ordinary gate, even after the
    # independent Argon envelope completes. Do not return the stale prior audit.
    assert output == scene.srt and audit.get("repaired") is False
    assert audit.get("reason_code") == "HISTORY_CONVERGENCE_ACOUSTIC_WITNESS_REQUIRED"
    assert scene.ordinary_calls == 1
    assert [row["method"] for row in scene.provider.calls] == ["POST", "GET"]
    assert pending == frozen
