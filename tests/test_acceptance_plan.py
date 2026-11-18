"""Offline tests for the independent manual acceptance fixture, not runtime E2E."""
import importlib.util
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from qa_agent.models import strict_json_equal
from qa_agent.store import load_plan, plan_hash, save_plan

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_todo.py"
spec = importlib.util.spec_from_file_location("todo_acceptance", SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def test_manual_plan_is_offline_and_canonical(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Plan materialization must not use network/subprocess/LLM")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    before = set(sys.modules)
    plan = acceptance.manual_plan()
    assert not any(name.startswith(("qa_agent.design", "qa_agent.llm", "qa_agent.runtime"))
                   for name in set(sys.modules) - before)
    assert [case.id for case in plan.cases] == acceptance.ORDER
    assert {req.id for req in plan.requirements} == {f"TD-0{i}" for i in range(1, 7)}
    assert {op.operation_id for op in plan.operations} == {
        "createTask", "listTasks", "getTask", "updateTask", "deleteTask"}
    assert all(case.executable and case.mandatory for case in plan.cases)
    assert all(case.preconditions == ["seed_ready"] for case in plan.cases)
    path = save_plan(tmp_path / "manual.json", plan)
    assert plan_hash(load_plan(path)) == plan_hash(plan)
    assert plan.exit_criteria.mandatory_high_must_pass
    assert plan.exit_criteria.verdicts_require_evidence
    assert plan.exit_criteria.no_open_critical_defects


def cases():
    return {case.id: case for case in acceptance.manual_plan().cases}


def test_seed_filter_is_exact_ordered_array():
    case = cases()["CP-02"]
    step, = case.steps
    assert step.query == {"completed": "true"}
    assert step.assertions[1].pointer == ""
    assert strict_json_equal(step.assertions[1].value, [acceptance.SEED[1]])


def test_creation_trim_and_initial_completion():
    step = cases()["CP-01"].steps[0]
    assert step.body_present and step.body == {"title": "  Ensayar  "}
    assert [(a.pointer, a.value) for a in step.assertions if a.kind == "equal"] == [
        ("/title", "Ensayar"), ("/completed", False)]
    assert any(a.kind == "present" and a.pointer == "/id" for a in step.assertions)


def test_cp03_captures_id_and_asserts_persisted_boolean():
    case = cases()["CP-03"]
    assert case.requirement_ids == ["TD-03", "TD-04"]
    assert [s.operation_id for s in case.steps] == ["createTask", "updateTask", "getTask"]
    assert case.steps[0].captures == {"task_id": "/id"}
    for step in case.steps[1:]:
        assert step.path == {"id": {"var": "task_id"}}
        assert any(a.kind == "equal" and a.pointer == "/completed" and a.value is True
                   for a in step.assertions)
        assert any(a.kind == "equal" and a.pointer == "/title" and a.value == "Persistencia"
                   for a in step.assertions)


def test_delete_uses_zero_bytes_and_two_stable_404_errors():
    steps = cases()["CP-04"].steps
    assert [s.operation_id for s in steps] == ["createTask", "deleteTask", "getTask", "deleteTask"]
    assert [a.kind for a in steps[1].assertions] == ["status", "empty_body"]
    assert steps[1].assertions[0].status == 204
    for step in steps[2:]:
        assert step.assertions[0].status == 404
        assert step.assertions[1].pointer == "/error/code"
        assert step.assertions[1].value == "TASK_NOT_FOUND"


def test_invalid_create_compares_entire_before_after_lists():
    steps = cases()["CP-05"].steps
    assert steps[0].operation_id == steps[2].operation_id == "listTasks"
    assert steps[0].captures == {"before": ""}
    assert steps[1].body == {"title": "   "}
    assert steps[1].assertions[0].status == 422
    assert steps[1].assertions[1].value == "VALIDATION_ERROR"
    assert steps[2].assertions[1].pointer == ""
    assert steps[2].assertions[1].value == {"var": "before"}


def test_title_boundary_rejects_without_modifying():
    case = cases()["CP-06"]
    assert len(case.data["title80"]) == 80
    assert len(case.data["title81"]) == 81
    assert case.steps[0].assertions[0].status == 201
    assert case.steps[1].assertions[0].status == 422
    assert case.steps[1].assertions[1].value == "VALIDATION_ERROR"
    assert case.steps[2].assertions[1].value == {"var": "title80"}


def test_missing_runtime_fails_immediately_without_spawning(monkeypatch):
    def missing(module):
        raise ModuleNotFoundError(module)
    monkeypatch.setattr(acceptance.importlib, "import_module", missing)
    monkeypatch.setattr(acceptance.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("Must not launch anything"))
    with pytest.raises(RuntimeError, match="no polling") as error:
        acceptance.preflight()
    assert "qa_agent.runtime.execute_suite" in str(error.value)
    assert "qa_agent.mcp_client.execute_via_mcp" in str(error.value)
    assert "qa_agent.cli.main" in str(error.value)


def test_network_fence_denies_remote_before_actual_connect(tmp_path):
    guard = acceptance.FIXTURE / "network_guard" / "sitecustomize.py"
    code = (
        "import runpy, socket\n"
        f"runpy.run_path({str(guard)!r})\n"
        "try:\n"
        "    with socket.socket() as sock: sock.connect(('203.0.113.1', 443))\n"
        "except PermissionError: print('blocked-before-connect')\n"
        "else: raise AssertionError('Remote network not fenced')\n")
    audit = tmp_path / "audit.log"
    result = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                            text=True, timeout=5,
                            env={"QA_ACCEPTANCE_NETWORK_LOG": str(audit), "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "blocked-before-connect"
    assert "socket.connect" in audit.read_text()


def test_plan_only_command_needs_no_runtime_or_credentials():
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--plan-only"],
                            capture_output=True, text=True, timeout=10,
                            env={"DEEPSEEK_API_KEY": "must-never-be-used", "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert "NOT AI-designed" in result.stdout
    assert "zero attempts" in result.stdout


@pytest.fixture
def partial_journal():
    """Standalone run retains six cases; observed steps have start+end events."""
    from types import SimpleNamespace
    from qa_agent.models import (CaseResult, CaseState, RunResult, RunStatus,
                                 StepEvidence, SuiteCase, SuiteManifest, utc_now)
    plan = acceptance.manual_plan()
    digest = plan_hash(plan)
    mappings = [SuiteCase(case_id=case.id, requirement_ids=case.requirement_ids,
                          script=f"test_{i:04d}.py", script_hash="a" * 64)
                for i, case in enumerate(plan.cases)]
    manifest = SuiteManifest(suite_id="suite-unit", plan_hash=digest,
                             source_plan_path=str(SCRIPT), cases=mappings,
                             files={entry.script: entry.script_hash for entry in mappings})
    terminal = StepEvidence(
        run_id="run-unit", case_id="CP-02", requirement_ids=["TD-02"],
        script=mappings[0].script, script_hash=mappings[0].script_hash,
        plan_hash=digest, product_version="reference", step_index=0,
        operation_id="listTasks", state=CaseState.PASSED, http_status=200,
        assertions=[{"kind": "status", "passed": True, "expected": 200, "observed": 200}])
    start = terminal.model_copy(update={"state": CaseState.INCONCLUSIVE,
                                       "http_status": None, "assertions": []})
    recorded = [CaseResult(case_id=m.case_id, requirement_ids=m.requirement_ids,
                           script=m.script, script_hash=m.script_hash)
                for m in mappings]
    recorded[0] = recorded[0].model_copy(update={"state": CaseState.PASSED, "attempted": True,
                                               "steps": [terminal], "evidence_paths": ["start", "end"]})
    run = RunResult(run_id="run-unit", suite_id=manifest.suite_id, plan_hash=digest,
                    environment_id="unit", product_version="reference", status=RunStatus.COMPLETED,
                    ended_at=utc_now(), cases=recorded)
    events = {"start": start, "end": terminal}
    store = SimpleNamespace(load_evidence=lambda run_id, path: events[path],
                            verify_run_integrity=lambda run_id: None)
    return store, manifest, run, events


def test_accepts_start_and_terminal_journal_for_partial_run(partial_journal):
    store, manifest, run, _ = partial_journal
    acceptance.validate_run(store, manifest, run, "reference", selected=["CP-02"])
    assert len(run.cases) == 6
    assert len(run.cases[0].evidence_paths) == 2 * len(run.cases[0].steps)
    assert all(not case.attempted for case in run.cases[1:])


def test_terminal_only_evidence_does_not_require_start_snapshot(partial_journal):
    store, manifest, run, _ = partial_journal
    run.cases[0].evidence_paths = ["end"]
    acceptance.validate_run(store, manifest, run, "reference", selected=["CP-02"])


@pytest.mark.parametrize("paths", [["start"], ["end", "end"]])
def test_journal_needs_exactly_one_matching_terminal_observation(partial_journal, paths):
    store, manifest, run, _ = partial_journal
    run.cases[0].evidence_paths = paths
    with pytest.raises(AssertionError, match="exactly one identical persisted"):
        acceptance.validate_run(store, manifest, run, "reference", selected=["CP-02"])


def test_partial_run_rejects_any_attempt_in_unselected_case(partial_journal):
    from qa_agent.models import CaseState
    store, manifest, run, _ = partial_journal
    run.cases[1] = run.cases[1].model_copy(update={"attempted": True, "state": CaseState.INCONCLUSIVE})
    with pytest.raises(AssertionError, match="Unselected standalone cases"):
        acceptance.validate_run(store, manifest, run, "reference", selected=["CP-02"])


def test_partial_run_cannot_discard_unselected_plan_members(partial_journal):
    store, manifest, run, _ = partial_journal
    run.cases = run.cases[:1]
    with pytest.raises(AssertionError, match="full planned membership"):
        acceptance.validate_run(store, manifest, run, "reference", selected=["CP-02"])
