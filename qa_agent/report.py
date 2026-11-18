"""Deterministic, read-only reconstruction of QA conclusions from local history."""
from __future__ import annotations

from collections import Counter
import html
import json
import os
from pathlib import Path

from .design import load_skills
from .models import CaseResult, CaseState, EnvironmentProfile, Plan, Priority, RunResult, RunStatus
from .security import redact
from .store import (IntegrityError, Store, atomic_write_bytes, load_plan,
                    plan_hash, read_json)


LABELS = {CaseState.DESIGNED: "Diseñado, pendiente", CaseState.GENERATED: "Script generado, pendiente de ejecución",
          CaseState.BLOCKED: "Bloqueado", CaseState.PASSED: "Pasó", CaseState.FAILED: "Falló",
          CaseState.TECHNICAL_ERROR: "Error técnico", CaseState.INCONCLUSIVE: "Inconcluso"}


def ratio(numerator: int, denominator: int) -> str:
    return "No aplica" if denominator == 0 else f"{numerator}/{denominator} ({100 * numerator / denominator:.2f} %)"


def _results(plan: Plan, run: RunResult | None) -> dict[str, CaseResult]:
    results = {result.case_id: result for result in run.cases} if run else {}
    return {case.id: results.get(case.id, CaseResult(case_id=case.id, requirement_ids=case.requirement_ids,
                                                   state=CaseState.DESIGNED)) for case in plan.cases}


def calculate_metrics(plan: Plan, run: RunResult | None = None) -> dict:
    """Population is every unique planned case, including automation-pending cases."""
    results = _results(plan, run)
    counts = Counter(result.state.value for result in results.values())
    initiated = {case_id for case_id, result in results.items() if result.attempted}
    designed = {req for case in plan.cases for req in case.requirement_ids}
    executed = {req for case in plan.cases if case.id in initiated for req in case.requirement_ids}
    planned = len(plan.cases)
    passed, failed = counts["passed"], counts["failed"]
    return {"planned": planned, "initiated": len(initiated), "passed": passed, "failed": failed,
            "blocked": counts["blocked"], "technical_error": counts["technical_error"],
            "inconclusive": counts["inconclusive"], "pending": counts["designed"] + counts["generated"],
            "advance": ratio(len(initiated), planned), "approval_evaluable": ratio(passed, passed + failed),
            "approval_planned": ratio(passed, planned),
            "design_coverage": ratio(len(designed), len(plan.requirements)),
            "execution_coverage": ratio(len(executed), len(plan.requirements))}


def recommendation(plan: Plan, run: RunResult | None = None, *, evidence_gaps: tuple[str, ...] = ()) -> str:
    results = _results(plan, run)
    criteria = plan.exit_criteria
    required = [case for case in plan.cases if case.mandatory and case.priority == Priority.HIGH]
    def supported(result):
        if any(gap.startswith(result.case_id + ":") for gap in evidence_gaps):
            return False
        return (not criteria.verdicts_require_evidence
                or bool(result.evidence_paths and (result.steps or result.state not in {CaseState.PASSED, CaseState.FAILED})))

    # A demonstrated blocker takes precedence, but an unsupported verdict cannot
    # establish a functional defect. Original saved states are never overwritten.
    if ((criteria.no_open_critical_defects and any(r.critical_defect and supported(r) for r in results.values()))
            or (criteria.mandatory_high_must_pass and any(results[c.id].state == CaseState.FAILED and supported(results[c.id]) for c in required))):
        return "Aplazar liberación"
    if not run or not plan.cases or not any(r.attempted for r in results.values()):
        return "Evidencia insuficiente"
    if (evidence_gaps or run.status != RunStatus.COMPLETED
            or any(r.attempted and not supported(r) for r in results.values())
            or any(r.critical_defect and not supported(r) for r in results.values())):
        return "Evidencia insuficiente"
    if criteria.mandatory_high_must_pass and any(results[c.id].state != CaseState.PASSED for c in required):
        return "Evidencia insuficiente"
    return "Recomendar liberación dentro del alcance probado"


def _reconstruct_evidence(plan: Plan, run: RunResult, store: Store) -> tuple[RunResult, tuple[str, ...]]:
    """Audit persisted references and hydrate a detached report view, never history."""
    gaps = []
    verified = {}
    if [(r.case_id, r.requirement_ids) for r in run.cases] != [(c.id, c.requirement_ids) for c in plan.cases]:
        gaps.append("Trazabilidad/membresía de resultados no coincide con el plan")
    for case in plan.cases:
        result = next((r for r in run.cases if r.case_id == case.id), None)
        if result is None:
            continue
        observed = result.state in {CaseState.PASSED, CaseState.FAILED}
        if not result.attempted:
            continue
        persisted = []
        verified[case.id] = persisted
        for path in result.evidence_paths:
            try:
                evidence = store.load_evidence(run.run_id, path)
                if (evidence.case_id != case.id or evidence.requirement_ids != case.requirement_ids
                        or evidence.plan_hash != run.plan_hash or evidence.product_version != run.product_version
                        or evidence.script != result.script or evidence.script_hash != result.script_hash
                        or evidence.step_index >= len(case.steps)
                        or evidence.operation_id != case.steps[evidence.step_index].operation_id):
                    raise IntegrityError("Evidence traceability mismatch")
                persisted.append(evidence)
            except (OSError, ValueError):
                gaps.append(f"{case.id}: evidencia ausente o trazabilidad inválida")
        if plan.exit_criteria.verdicts_require_evidence and not persisted:
            gaps.append(f"{case.id}: veredicto sin evidencia persistida verificable")
        if observed and persisted and plan.exit_criteria.verdicts_require_evidence:
            if result.state == CaseState.PASSED:
                for index, step in enumerate(case.steps):
                    # Runtime preserves an inconclusive start event BEFORE HTTP
                    # as well as the terminal evidence; start events are not a
                    # duplicate verdict and must not invalidate a completed step.
                    matching = [e for e in persisted if e.step_index == index and e.state == CaseState.PASSED]
                    if (len(matching) != 1 or matching[0].state != CaseState.PASSED
                            or len(matching[0].assertions) != len(step.assertions)
                            or any(not a.passed for a in matching[0].assertions)):
                        gaps.append(f"{case.id}: paso {index} sin todas las aserciones satisfechas y guardadas")
            elif not any(e.state == CaseState.FAILED and any(not a.passed for a in e.assertions) for e in persisted):
                gaps.append(f"{case.id}: fallo sin discrepancia de aserción guardada")
    historical = run.model_copy(update={"cases": [
        result.model_copy(update={"steps": verified.get(result.case_id, [])}) for result in run.cases]})
    return historical, tuple(dict.fromkeys(gaps))


def render_report(plan: Plan, run: RunResult | None = None, *, evidence_gaps: tuple[str, ...] = (),
                  secrets: tuple[str, ...] = ()) -> str:
    skills, skill_hashes = load_skills()  # Both close skill and common steering are actually consumed.
    metrics = calculate_metrics(plan, run)
    results = _results(plan, run)

    def cell(value):
        value = redact(value, secrets)
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return html.escape(value, quote=False).replace("|", "\\|").replace("\n", "<br>").replace("`", "&#96;")

    decision = recommendation(plan, run, evidence_gaps=evidence_gaps)
    lines = ["# Cierre QA", "", f"**Recomendación: {decision}.**",
             "La decisión de liberación corresponde al responsable humano; aprobar la suite no aprueba la liberación.", "",
             f"- Corrida: {cell(run.run_id) if run else 'Sin ejecución'}",
             f"- Suite: {cell(run.suite_id) if run else 'No generada/verificada en este cierre de plan'}",
             f"- Ambiente: {cell(run.environment_id) if run else 'No aplica'}",
             f"- Versión del producto: {cell(run.product_version) if run else 'No aplica'}",
             f"- Estado de corrida: {cell(run.status.value) if run else 'Diseñado, pendiente'}",
             f"- Plan SHA256: {plan_hash(plan)}",
             f"- Inicio / fin: {cell(str(run.started_at)) if run else 'No aplica'} / {cell(str(run.ended_at)) if run and run.ended_at else 'No aplica'}",
             f"- Skills del diseño: {cell(plan.skill_hashes)}",
             f"- Skills del cierre (versionadas): {cell({name: skill_hashes[name] for name in ('close', 'steering')})}",
             "", "## Criterios previos", ""]
    lines.extend(f"- {name}: {'sí' if enabled else 'no'}" for name, enabled in plan.exit_criteria.model_dump().items())
    lines += ["", "## Resumen del ciclo", "", "| Métrica | Valor |", "|---|---|"]
    names = {"planned": "Casos planificados", "initiated": "Casos únicos iniciados", "passed": "Pasó",
             "failed": "Falló", "blocked": "Bloqueados", "technical_error": "Error técnico",
             "inconclusive": "Inconclusos", "pending": "Pendientes", "advance": "Avance (iniciados/planificados)",
             "approval_evaluable": "Aprobación entre evaluables", "approval_planned": "Aprobados/planificados",
             "design_coverage": "Cobertura de diseño", "execution_coverage": "Cobertura de ejecución"}
    lines.extend(f"| {label} | {metrics[key]} |" for key, label in names.items())
    lines += ["", "Los pendientes de automatización permanecen en el denominador. Cobertura de ejecución no significa cumplimiento.",
              "", "## Trazabilidad por requisito", "", "| Requisito | Regla | Casos / estado | Iniciados |", "|---|---|---|---|"]
    for requirement in plan.requirements:
        cases = [case for case in plan.cases if requirement.id in case.requirement_ids]
        lines.append(f"| {cell(requirement.id)} | {cell(requirement.text)} | "
                     + cell("; ".join(f"{c.id}: {LABELS[results[c.id].state]}" for c in cases) or "Sin caso")
                     + f" | {sum(results[c.id].attempted for c in cases)} |")
    lines += ["", "## Casos y alcance real", "", "| Caso | Requisitos | Estado guardado | Script / hash | Motivo / evidencia |", "|---|---|---|---|---|"]
    for case in plan.cases:
        result = results[case.id]
        lines.append(f"| {cell(case.id)} | {cell(case.requirement_ids)} | {LABELS[result.state]} | "
                     f"{cell(result.script or 'No generado')} / {cell(result.script_hash or 'No aplica')} | "
                     f"{cell({'reason': result.reason or case.automation_pending_reason, 'evidence': result.evidence_paths})} |")
    lines += ["", "## Hallazgos por operación", "", "| Operación | Caso / requisito | Estado / HTTP | Esperado | Observado | Evidencia |", "|---|---|---|---|---|---|"]
    for case in plan.cases:
        result = results[case.id]
        for index, step in enumerate(case.steps):
            events = [e for e in result.steps if e.step_index == index]
            # A durable pre-HTTP start already has request/expected metadata.
            # Observations or completion diagnostics distinguish its terminal
            # event, including an inconclusive response/capture/timeout.
            terminal = [e for e in events if e.http_status is not None or e.response or e.assertions
                        or e.error or e.duration_ms > 0 or e.observed is not None or e.captures]
            evidence = (terminal or events or [None])[-1]
            lines.append(f"| {cell(step.operation_id)} | {cell(case.id)} / {cell(case.requirement_ids)} | "
                         f"{cell(LABELS[evidence.state]) if evidence else 'Sin evidencia de paso agregada'} / "
                         f"{evidence.http_status if evidence and evidence.http_status is not None else 'No observado'} | "
                         f"{cell([{'kind': a.kind, 'expected': a.expected} for a in evidence.assertions] if evidence else [a.model_dump() for a in step.assertions])} | "
                         f"{cell([{'kind': a.kind, 'observed': a.observed, 'passed': a.passed, 'message': a.message} for a in evidence.assertions]) if evidence else 'No observado'} | {cell(result.evidence_paths)} |")
    lines += ["", "## Defectos pendientes y acciones de mantenibilidad", "",
              "| Caso | Impacto | Evidencia | Corrección sugerida | Prueba de verificación |", "|---|---|---|---|---|"]
    findings = [c for c in plan.cases if results[c.id].state != CaseState.PASSED or results[c.id].critical_defect]
    for case in findings:
        result = results[case.id]
        impact = "Defecto crítico abierto" if result.critical_defect else LABELS[result.state]
        action = {CaseState.FAILED: "Investigar discrepancia y corregir la regla; causa no demostrada",
                  CaseState.BLOCKED: "Satisfacer y certificar precondiciones antes de una nueva corrida",
                  CaseState.TECHNICAL_ERROR: "Revisar transporte/ejecutor; no atribuir fallo funcional",
                  CaseState.INCONCLUSIVE: "Completar evidencia en una nueva corrida autorizada"}.get(result.state, "Completar contrato/datos/automatización y revisar diseño")
        lines.append(f"| {cell(case.id)} | {cell(impact)} | {cell(result.evidence_paths or ['Sin evidencia observada'])} | "
                     f"{cell(action)} | {cell('Reejecutar ' + case.id + ' tras revisión/aprobación, contrastando todas sus aserciones')} |")
    if not findings:
        lines.append("| — | No hay defectos registrados dentro del alcance | Ver evidencia por caso | Mantener pruebas de regresión | Repetir sobre datos iniciales equivalentes |")
    lines += ["", "## Riesgos residuales, desviaciones y limitaciones", ""]
    lines.extend(f"- Integridad/evidencia incompleta — inconcluso: {cell(gap)}" for gap in evidence_gaps)
    if evidence_gaps:
        lines.append("- Las métricas y estados anteriores son los guardados, no cumplimiento confirmado: las brechas invalidan su uso como evidencia de liberación.")
    lines.extend(f"- Pregunta pendiente: {cell(question)}" for question in plan.pending_questions)
    if run and run.error:
        lines.append(f"- Incidencia de corrida: {cell(run.error)}")
    if not metrics["initiated"]:
        lines.append("- Cero intentos: Evidencia insuficiente. El diseño no demuestra comportamiento real.")
    uncovered = [r.id for r in plan.requirements if not any(r.id in c.requirement_ids for c in plan.cases)]
    lines += [f"- Requisitos sin caso: {cell(uncovered)}.",
              "- No se infiere cobertura exhaustiva de reglas ni capacidades de navegador/correo/BD. Un HTTP correcto no prueba persistencia.",
              "- Perfiles y filesystem locales confiables; no existe sandbox para Python arbitrario ni defensa contra el dueño del registro.",
              "- Este cierre no contacta al objetivo, no ejecuta suites, no reintenta y no cambia veredictos ni resultados guardados.",
              "- Política de cierre aplicada: " + cell(skills["close"].splitlines()[0]),
              "- Steering aplicado: " + cell(skills["steering"].splitlines()[0]), ""]
    return redact("\n".join(lines), secrets)


def close_plan(plan_path: Path) -> Path:
    plan_path = Path(plan_path)
    plan = load_plan(plan_path)
    secrets = tuple(filter(None, (os.environ.get("DEEPSEEK_API_KEY"),)))
    return atomic_write_bytes(plan_path.with_suffix(".cierre.md"), render_report(plan, secrets=secrets).encode())


def close_run(run_id: str, *, store: Store | None = None) -> Path:
    store = store or Store()
    directory = store.run_dir(run_id)  # Validates ID before writing anywhere.
    path = directory / "cierre.md"
    try:
        store.verify_run_integrity(run_id)
        plan = store.load_run_plan(run_id)
        run = store.load_run(run_id)
        profile = EnvironmentProfile.model_validate(read_json(directory / "environment.json"))
        secrets = tuple(filter(None, [os.environ.get("DEEPSEEK_API_KEY"),
                                     *(os.environ.get(name) for name in profile.credentials.values())]))
        historical, gaps = _reconstruct_evidence(plan, run, store)
        report = render_report(plan, historical, evidence_gaps=gaps, secrets=secrets)
    except (IntegrityError, OSError, ValueError):
        if not directory.is_dir():
            raise ValueError("Corrida inexistente") from None
        # No bypass via raw load: damaged history is not a source of true verdicts.
        _, hashes = load_skills()
        report = ("# Cierre QA\n\n**Recomendación: Evidencia insuficiente.**\n\n"
                  "Estado: Inconcluso por integridad histórica incompleta o corrupta.\n"
                  "No se confirman métricas, observaciones ni veredictos de archivos no verificables.\n"
                  "Ambiente, versión, alcance y criterios previos: no verificables. Intentos: desconocidos, no cero.\n\n"
                  "| Métrica | Valor |\n|---|---|\n| Avance | No aplica |\n"
                  "| Aprobación entre evaluables | No aplica |\n| Aprobados/planificados | No aplica |\n"
                  "| Cobertura de diseño / ejecución | No aplica |\n\n"
                  "## Trazabilidad por requisito\n\n| Requisito | Estado |\n|---|---|\n| No verificable | Inconcluso |\n\n"
                  "## Hallazgos por operación\n\n| Operación | Observación |\n|---|---|\n| No verificable | No reconstruida |\n\n"
                  "## Riesgo y acción\n\n"
                  "Impacto: no es posible evaluar criterios obligatorios. Evidencia: historial local no verificable.\n"
                  "Preservar plan, resultados, snapshot y evidencias existentes sin modificarlos.\n"
                  "Corrección sugerida: recuperar una copia íntegra o solicitar una nueva corrida autorizada.\n"
                  "Verificación: comprobar integridad y regenerar cierre; nunca reconstruir respuestas faltantes.\n"
                  "No se contactó al objetivo ni se agregaron intentos. La decisión final es humana.\n"
                  f"Skill cierre SHA256: {hashes['close']}; steering SHA256: {hashes['steering']}.\n")
    return atomic_write_bytes(path, report.encode("utf-8"))
