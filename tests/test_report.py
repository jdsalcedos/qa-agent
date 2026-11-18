from pathlib import Path

import httpx
import pytest

from qa_agent.models import (AssertionResult, Case, CaseResult, CaseState, EnvironmentProfile,
                             Plan, Priority, Requirement, RunResult, RunStatus, StepEvidence,
                             SuiteCase, SuiteManifest)
from qa_agent.report import (calculate_metrics, close_plan, close_run, recommendation,
                             render_report)
from qa_agent.store import (Store, atomic_write_json, hash_file, plan_hash, save_plan,
                            suite_files)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Reports must not create HTTP clients")
    monkeypatch.setattr(httpx, "Client", forbidden)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


def make_plan(count=1, requirement_count=1):
    return Plan.model_validate({
        "requirements": [{"id": f"R-{i}", "text": f"Regla {i}"} for i in range(requirement_count)],
        "contract_hash": "a" * 64,
        "operations": [{"operation_id": "getResource", "method": "GET", "path": "/resources", "responses": {"200": {"description": "OK"}}}],
        "cases": [{"id": f"CP-{i}", "requirement_ids": [f"R-{i % requirement_count}"],
                   "objective": "Consultar", "expected": "HTTP 200", "steps": [
                       {"operation_id": "getResource", "assertions": [{"status": 200}]}]} for i in range(count)]})


def make_run(plan, states, *, status=RunStatus.COMPLETED):
    return RunResult(run_id="run-1", suite_id="suite-1", plan_hash=plan_hash(plan),
                     environment_id="local", product_version="synthetic", status=status,
                     cases=[CaseResult(case_id=case.id, requirement_ids=case.requirement_ids, state=state,
                                       attempted=state in {CaseState.PASSED, CaseState.FAILED,
                                                           CaseState.TECHNICAL_ERROR, CaseState.INCONCLUSIVE})
                            for case, state in zip(plan.cases, states)])


@pytest.fixture
def history(tmp_path):
    store = Store(tmp_path / "store")
    plan = make_plan()
    source = save_plan(tmp_path / "plan.json", plan)
    suite = tmp_path / "suite"
    suite.mkdir()
    save_plan(suite / "plan.json", plan)
    (suite / "test_cp_0.py").write_text("def test_cp_0(): pass\n")
    files = suite_files(suite)
    manifest = SuiteManifest(suite_id="suite-1", source_plan_path=str(source), plan_hash=plan_hash(plan),
                             files=files, cases=[SuiteCase(case_id="CP-0", requirement_ids=["R-0"],
                                                          script="test_cp_0.py", script_hash=files["test_cp_0.py"])])
    store.register_suite(suite, manifest)
    store.approve_suite("suite-1", approved_by="Human")
    store.save_profile(EnvironmentProfile.model_validate({
        "id": "local", "base_url": "http://127.0.0.1:8765", "allowed_targets": [
            {"host": "127.0.0.1", "port": 8765, "ips": ["127.0.0.1"]}],
        "allowed_operations": ["getResource"], "product_version": "synthetic",
        "credentials": {"Authorization": "QA_SYNTHETIC_AUTH"}}))
    run = store.create_run("suite-1", "local")
    return store, plan, source, run


def finish(history, state=CaseState.PASSED, *, critical=False):
    store, plan, source, run = history
    evidence = StepEvidence(run_id=run.run_id, case_id="CP-0", requirement_ids=["R-0"],
                            script=run.cases[0].script, script_hash=run.cases[0].script_hash,
                            plan_hash=run.plan_hash, product_version=run.product_version,
                            step_index=0, operation_id="getResource", state=state,
                            http_status=200 if state == CaseState.PASSED else 400,
                            expected=200, observed=200 if state == CaseState.PASSED else 400,
                            assertions=[AssertionResult(kind="status", passed=state == CaseState.PASSED,
                                                        expected=200, observed=200 if state == CaseState.PASSED else 400)])
    start = evidence.model_copy(update={"state": CaseState.INCONCLUSIVE, "assertions": [],
                                        "http_status": None, "observed": None})
    start_path = store.append_evidence(start)
    evidence_path = store.append_evidence(evidence)
    result = run.cases[0].model_copy(update={"state": state, "attempted": True,
                                           "steps": [evidence], "evidence_paths": [str(start_path), str(evidence_path)],
                                           "critical_defect": critical})
    run = run.model_copy(update={"cases": [result], "status": RunStatus.COMPLETED})
    store.save_run(run)
    return run, evidence_path


@pytest.mark.parametrize("reqs,cases", [(0, 0), (2, 0)])
def test_zero_denominators(reqs, cases):
    plan = make_plan(cases, reqs)
    metrics = calculate_metrics(plan)
    assert metrics["advance"] == "No aplica"
    assert metrics["approval_evaluable"] == "No aplica"
    assert metrics["approval_planned"] == "No aplica"
    assert metrics["design_coverage"] == ("No aplica" if not reqs else "0/2 (0.00 %)")
    assert recommendation(plan) == "Evidencia insuficiente"
    report = render_report(plan)
    assert "Cero intentos" in report
    assert "NaN" not in report


@pytest.mark.parametrize("states,advance,evaluable,planned,blocked", [
    ([CaseState.DESIGNED, CaseState.GENERATED], "0/2", "No aplica", "0/2", 0),
    ([CaseState.BLOCKED, CaseState.BLOCKED], "0/2", "No aplica", "0/2", 2),
    ([CaseState.TECHNICAL_ERROR, CaseState.INCONCLUSIVE], "2/2", "No aplica", "0/2", 0),
    ([CaseState.PASSED, CaseState.FAILED], "2/2", "1/2", "1/2", 0),
])
def test_metrics_state_matrix(states, advance, evaluable, planned, blocked):
    plan = make_plan(2)
    metrics = calculate_metrics(plan, make_run(plan, states))
    assert metrics["advance"].startswith(advance)
    assert metrics["approval_evaluable"].startswith(evaluable)
    assert metrics["approval_planned"].startswith(planned)
    assert metrics["blocked"] == blocked


def test_mixed_states_exact_denominators_multirequirement_and_unique_attempts():
    plan = make_plan(5, 2)
    plan.cases[0].requirement_ids = ["R-0", "R-1"]
    states = [CaseState.PASSED, CaseState.FAILED, CaseState.TECHNICAL_ERROR, CaseState.BLOCKED, CaseState.GENERATED]
    run = make_run(plan, states)
    metrics = calculate_metrics(plan, run)
    assert metrics == {"planned": 5, "initiated": 3, "passed": 1, "failed": 1, "blocked": 1,
                       "technical_error": 1, "inconclusive": 0, "pending": 1,
                       "advance": "3/5 (60.00 %)", "approval_evaluable": "1/2 (50.00 %)",
                       "approval_planned": "1/5 (20.00 %)", "design_coverage": "2/2 (100.00 %)",
                       "execution_coverage": "2/2 (100.00 %)"}


def test_pending_automation_stays_in_denominator():
    plan = make_plan()
    plan.cases.append(Case(id="CP-pending", requirement_ids=["R-0"], objective="Leer correo",
                           expected="Correo entregado", automation_pending_reason="Correo fuera del MVP"))
    assert calculate_metrics(plan)["planned"] == 2
    assert calculate_metrics(plan)["pending"] == 2


def test_close_plan_no_attempt_no_llm_no_artifact_mutation(tmp_path):
    plan = make_plan(2)
    source = save_plan(tmp_path / "plan.json", plan)
    before = hash_file(source)
    report = close_plan(source)
    text = report.read_text()
    assert "Evidencia insuficiente" in text
    assert "| Casos únicos iniciados | 0 |" in text
    assert "| Aprobación entre evaluables | No aplica |" in text
    assert hash_file(source) == before
    first = report.read_bytes()
    assert close_plan(source).read_bytes() == first


def test_success_report_sections_and_history_unchanged(history):
    store, plan, source, _ = history
    run, evidence = finish(history)
    result_path = store.run_dir(run.run_id) / "resultados.json"
    before = hash_file(result_path)
    report = close_run(run.run_id, store=store)
    text = report.read_text()
    assert "Recomendar liberación dentro del alcance probado" in text
    for title in ("Resumen del ciclo", "Trazabilidad por requisito", "Hallazgos por operación",
                  "Criterios previos", "Defectos pendientes", "Riesgos residuales", "Versión del producto"):
        assert title in text
    assert str(evidence) in text
    assert "responsable humano" in text
    assert hash_file(result_path) == before
    assert close_run(run.run_id, store=store).read_text() == text


@pytest.mark.parametrize("state,critical,decision", [
    (CaseState.PASSED, False, "Recomendar liberación dentro del alcance probado"),
    (CaseState.FAILED, False, "Aplazar liberación"),
    (CaseState.PASSED, True, "Aplazar liberación"),
])
def test_reference_only_results_reconstruct_support_without_modifying_history(history, state, critical, decision):
    store, *_ = history
    run, _ = finish(history, state, critical=critical)
    run.cases[0].steps = []
    store.save_run(run)  # A supported foundation representation, not a corrupted snapshot.
    directory = store.run_dir(run.run_id)
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    text = close_run(run.run_id, store=store).read_text()
    assert decision in text
    row = next(line for line in text.splitlines() if line.startswith("| getResource |"))
    assert ("Pasó / 200" if state == CaseState.PASSED else "Falló / 400") in row
    assert ('"passed": true' if state == CaseState.PASSED else '"passed": false') in row
    assert len(store.load_run(run.run_id).cases[0].evidence_paths) == 2
    assert store.load_run(run.run_id).cases[0].steps == []
    assert store.load_run(run.run_id).cases[0].state == state
    assert all(path.read_bytes() == content for path, content in before.items())


@pytest.mark.parametrize("reverse", [False, True])
def test_tables_select_terminal_evidence_instead_of_initial_event(history, reverse):
    store, plan, *_ = history
    run, _ = finish(history)
    paths = run.cases[0].evidence_paths
    if reverse:
        paths.reverse()
    run.cases[0].steps = [store.load_evidence(run.run_id, path) for path in paths]
    store.save_run(run)
    result_before = (store.run_dir(run.run_id) / "resultados.json").read_bytes()
    for text in (render_report(plan, run), close_run(run.run_id, store=store).read_text()):
        row = next(line for line in text.splitlines() if line.startswith("| getResource |"))
        assert "Pasó / 200" in row
        assert '"expected": 200' in row and '"observed": 200' in row and '"passed": true' in row
        assert "No observado" not in row and "Inconcluso" not in row
        assert all(path in row for path in paths)
    assert (store.run_dir(run.run_id) / "resultados.json").read_bytes() == result_before
    assert len(store.load_run(run.run_id).cases[0].steps) == 2


@pytest.mark.parametrize("reverse_paths", [False, True])
@pytest.mark.parametrize("reverse_embedded", [False, True])
def test_inconclusive_observation_is_not_replaced_by_empty_start(history, reverse_paths, reverse_embedded):
    store, plan, *_ = history
    run, _ = finish(history, CaseState.INCONCLUSIVE)
    paths = run.cases[0].evidence_paths
    events = [store.load_evidence(run.run_id, path) for path in paths]
    if reverse_paths:
        paths.reverse()
    run.cases[0].steps = list(reversed(events)) if reverse_embedded else events
    store.save_run(run)
    directory = store.run_dir(run.run_id)
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    for text in (render_report(plan, run), close_run(run.run_id, store=store).read_text()):
        row = next(line for line in text.splitlines() if line.startswith("| getResource |"))
        assert "Inconcluso / 400" in row
        assert '"expected": 200' in row and '"observed": 400' in row and '"passed": false' in row
        assert "No observado" not in row and "Pasó" not in row
        assert "Recomendación: Evidencia insuficiente" in text
        assert "| Pasó | 0 |" in text
        assert all(path in row for path in paths)
    assert all(path.read_bytes() == content for path, content in before.items())
    assert store.load_run(run.run_id) == run


def test_empty_inconclusive_start_does_not_invent_response(history):
    store, plan, *_ = history
    run, _ = finish(history, CaseState.INCONCLUSIVE)
    run.cases[0].evidence_paths = run.cases[0].evidence_paths[:1]
    run.cases[0].steps = [store.load_evidence(run.run_id, run.cases[0].evidence_paths[0])]
    store.save_run(run)
    for text in (render_report(plan, run), close_run(run.run_id, store=store).read_text()):
        row = next(line for line in text.splitlines() if line.startswith("| getResource |"))
        assert "Inconcluso / No observado" in row
        assert '"observed": 400' not in row and '"passed": true' not in row
        assert "Recomendación: Evidencia insuficiente" in text
        assert "| Pasó | 0 |" in text
    assert store.load_run(run.run_id) == run


@pytest.mark.parametrize("embedded", [False, True])
def test_start_event_without_referenced_terminal_never_confirms_success(history, embedded):
    store, *_ = history
    run, terminal_path = finish(history)
    run.cases[0].evidence_paths = run.cases[0].evidence_paths[:1]
    run.cases[0].steps = ([store.load_evidence(run.run_id, run.cases[0].evidence_paths[0])]
                         if embedded else [])
    store.save_run(run)
    before = terminal_path.read_bytes()
    text = close_run(run.run_id, store=store).read_text()
    assert "Recomendación: Evidencia insuficiente" in text
    assert "paso 0 sin todas las aserciones" in text
    row = next(line for line in text.splitlines() if line.startswith("| getResource |"))
    assert "Inconcluso / No observado" in row
    assert '"passed": true' not in row
    assert terminal_path.read_bytes() == before
    assert store.load_run(run.run_id).cases[0].state == CaseState.PASSED


def test_mandatory_high_failure_blocks_release(history):
    store, *_ = history
    run, _ = finish(history, CaseState.FAILED)
    text = close_run(run.run_id, store=store).read_text()
    assert "Aplazar liberación" in text
    assert "400" in text and "200" in text
    assert "Investigar discrepancia" in text


def test_critical_open_defect_blocks_even_when_assertions_pass(history):
    store, *_ = history
    run, _ = finish(history, critical=True)
    text = close_run(run.run_id, store=store).read_text()
    assert "Aplazar liberación" in text
    assert "Defecto crítico abierto" in text


@pytest.mark.parametrize("state", [CaseState.BLOCKED, CaseState.TECHNICAL_ERROR, CaseState.INCONCLUSIVE, CaseState.GENERATED])
def test_missing_mandatory_case_is_insufficient(state):
    plan = make_plan()
    assert recommendation(plan, make_run(plan, [state])) == "Evidencia insuficiente"


def test_passed_without_evidence_cannot_release(history):
    store, plan, _, run = history
    run = run.model_copy(update={"status": RunStatus.COMPLETED, "cases": [run.cases[0].model_copy(
        update={"state": CaseState.PASSED, "attempted": True})]})
    store.save_run(run)
    text = close_run(run.run_id, store=store).read_text()
    assert "Evidencia insuficiente" in text
    assert "veredicto sin evidencia" in text
    assert "guardados, no cumplimiento confirmado" in text


def test_false_failure_without_evidence_not_demonstrated(history):
    store, plan, _, run = history
    run = run.model_copy(update={"status": RunStatus.COMPLETED, "cases": [run.cases[0].model_copy(
        update={"state": CaseState.FAILED, "attempted": True})]})
    store.save_run(run)
    assert "Evidencia insuficiente" in close_run(run.run_id, store=store).read_text()


def test_report_uses_historical_snapshot_not_mutable_source(history):
    store, plan, source, _ = history
    run, _ = finish(history)
    plan.requirements[0].text = "CAMBIADO DESPUÉS"
    save_plan(source, plan)
    text = close_run(run.run_id, store=store).read_text()
    assert "CAMBIADO DESPUÉS" not in text
    assert "Recomendar liberación" in text


@pytest.mark.parametrize("damage", ["delete_evidence", "change_result", "delete_plan", "delete_integrity"])
def test_missing_or_corrupt_history_is_inconclusive_and_preserved(history, damage):
    store, plan, source, _ = history
    run, evidence = finish(history)
    directory = store.run_dir(run.run_id)
    if damage == "delete_evidence":
        evidence.unlink()
    elif damage == "change_result":
        (directory / "resultados.json").write_text("{}")
    elif damage == "delete_plan":
        (directory / "plan.json").unlink()
    else:
        (directory / "integrity.json").unlink()
    remaining = {str(p): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    text = close_run(run.run_id, store=store).read_text()
    assert "Evidencia insuficiente" in text
    assert "Inconcluso por integridad" in text
    assert "no se confirman" in text.lower()
    assert all(Path(path).read_bytes() == content for path, content in remaining.items())


def test_unindexed_evidence_detected(history):
    store, _, _, run = history
    atomic_write_json(store.run_dir(run.run_id) / "evidence" / "orphan.json", {})
    assert "Inconcluso por integridad" in close_run(run.run_id, store=store).read_text()


def test_missing_reference_cannot_confirm_verdict(history, monkeypatch):
    store, *_ = history
    run, _ = finish(history)
    run.cases[0].evidence_paths = ["evidence/not-found.json"]
    # Foundation rejects this on save; model a damaged legacy snapshot at the
    # read boundary to also exercise the report's independent evidence audit.
    monkeypatch.setattr(store, "load_run", lambda run_id: run)
    text = close_run(run.run_id, store=store).read_text()
    assert "Evidencia insuficiente" in text
    assert "evidencia ausente" in text


def test_no_implicit_resume_or_history_aggregation(history):
    store, _, _, run = history
    interrupted = run.model_copy(update={"status": RunStatus.INTERRUPTED, "cases": [run.cases[0].model_copy(
        update={"state": CaseState.INCONCLUSIVE, "attempted": True, "reason": "Interrumpido"})]})
    store.save_run(interrupted)
    close_run(run.run_id, store=store)
    assert store.load_run(run.run_id) == interrupted
    assert store.list_runs() == [run.run_id]


def test_report_redacts_credentials_reflections_and_markdown(history, monkeypatch):
    store, plan, source, _ = history
    monkeypatch.setenv("QA_SYNTHETIC_AUTH", "TARGET-CANARY")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "PROVIDER-CANARY")
    run, _ = finish(history)
    run.error = "TARGET-CANARY PROVIDER-CANARY password=ANOTHER-CANARY"
    run.cases[0].reason = "<script>alert(1)</script> | reflected TARGET-CANARY"
    store.save_run(run)
    text = close_run(run.run_id, store=store).read_text()
    assert "TARGET-CANARY" not in text and "PROVIDER-CANARY" not in text and "ANOTHER-CANARY" not in text
    assert "<script>" not in text and "&lt;script&gt;" in text


def test_unknown_run_not_created(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValueError):
        close_run("unknown", store=store)
    assert not store.run_dir("unknown").exists()


def test_run_id_traversal_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        close_run("../escape", store=Store(tmp_path))
