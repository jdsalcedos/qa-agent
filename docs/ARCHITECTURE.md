# Arquitectura contractual — Agente QA

El agente usa Python 3.14.7, Pydantic, pytest, HTTPX y MCP por stdio. Las pruebas automatizadas ejercitan la integración DeepSeek con transporte simulado. La [aceptación de la demo](../acceptance/README.md) describe cómo comprobar el flujo local con la API To-do incluida.

## Contratos

Paquete `qa_agent`: `models.py`, `store.py`, `openapi.py`, `design.py`, `generate.py`, `runtime.py`, `report.py`, `security.py`, `cli.py`, `mcp_client.py`, `mcp_server.py`, `skills/{design,close,steering}.md`, `templates/case.py.j2` y puente `templates/conftest.py`; tests en `tests/`. Contratos de modelos/persistencia/seguridad: [API_CONTRACT.md](API_CONTRACT.md); uso y firmas públicas actuales: [README](../README.md). Firmas implementadas: `design_plan(requirements: Path, openapi: Path|None, data: Path|None, llm: Designer)->Plan`; `generate_suite(plan_path: Path, output: Path, *, store=None)->SuiteManifest`; `execute_suite(suite_id: str, environment_id: str, *, store=None)->RunResult`; `execute_via_mcp(suite_id: str, environment_id: str, *, store=None)->dict` (cliente síncrono); `close_run(run_id: str, *, store=None)->Path`; `close_plan(plan_path: Path)->Path`. `Designer.propose(prompt: str)->PlanDraft`; transporte HTTPX inyectable para pruebas. DeepSeek compatible, default `deepseek-chat`, endpoint/model/key por env; dependencias fijadas.

Modelos Pydantic: `Plan(version, requirements, contract_hash, cases, exit_criteria, pending_questions)`; `Case(id, requirement_ids, objective, priority, mandatory, preconditions, data, expected, steps)`; `Step(operation_id,path,query,body,assertions,captures)`; `RunResult(run_id,suite_id,plan_hash,environment_id,product_version,status,cases,started_at,ended_at)`. JSON UTF-8, enums estables. Assertions: `status/present/equal/empty_body`; JSON Pointer, igualdad estricta, `{var}` tipado. Estados de caso `designed/generated/blocked/passed/failed/technical_error/inconclusive`; corrida `pending/running/completed/interrupted/failed`. Bloqueo antes de HTTP; error técnico tras intento; detener caso al primer fallo; conservar historial. Evidencia JSON/paso: request/response depurados, esperado/observado, HTTP, duración, fecha/estado. Métricas de avance, aprobación, aprobados/planeados y cobertura diseño/ejecución; cero denominador «No aplica», sin ejecución «Evidencia insuficiente».

Persistencia `data/{plans,suites,runs}/<id>/`: plan; suite: manifest (IDs, hashes, mappings), copia, `test_0000.py` y siguientes (índice del caso, ver manifest), módulo común, pytest.ini y lock. Solo suites registradas. `aprobar --suite ... --responsable ... --confirmar` liga aprobación explícita a suite, hash del plan y manifiesto; cambio invalida aprobación y obliga regenerar. CLI: `disenar --requisitos --contrato --salida`; `generar --plan --salida`; `aprobar --suite`; `ejecutar --suite --entorno`; `cerrar --corrida ID|--plan`.

## Seguridad / MCP

Allowlist host/puerto por perfil; nunca destino arbitrario de suite. Solo operationId del contrato; timeout petición/proceso, sin retry mutante, pytest subprocess. Secretos del env ejecutor y redactados en evidencia/logs; validar hashes plan/manifiesto/scripts y argumentos MCP antes de HTTP. Historial hasta limpieza manual. Fixtures HTTP locales; demo solo instancia desechable disponible, sin tocar procesos ajenos.

MCP SDK oficial: proceso `python -m qa_agent.mcp_server`, stdio real; stdout solo JSON-RPC, logs stderr. Cliente subprocess/handshake, `tools/list`, `tools/call ejecutar_plan({suite_id, entorno_id})`; respuesta `{run_id,status,result_path,evidence_paths}`; timeout y terminación. Sin simular llamada directa. Fijar y verificar versión SDK. Docs consultadas: `/modelcontextprotocol/python-sdk`.

## Trazabilidad: aceptación → tests

| Req/aceptación de specs | Tests obligatorios |
|---|---|
| MVP §1-2: entradas OpenAPI local refs, diseño parcial/preguntas, comandos/skills/config | unit parser refs local/externa; CLI smoke por subcomando; fake Designer; versionado skills |
| MVP §3: esquema, asserts, capturas/variables, pytest exportable | Pydantic valid/invalid; doble paso captura; generación determinista; pytest directo |
| MVP §3-4: MCP registro, precondición, estados, interrupción/historial | integración stdio list/call; bloqueado cero HTTP; fallos/interrupción; persistencia |
| MVP §4: evidencia completa, hash, cero asserts ejecutables | pruebas de contrato, hash inválido, evidencia redactada, rechazo sin asserts |
| MVP §5: tablas, métricas, decisión, cierre plan/run | unit fórmulas/denominador cero; regenerar sin HTTP; plantilla y trazabilidad |
| MVP §6.1-5: caso escrito dos pasos; diseño cero intentos; dos variantes; error/bloqueo; direct pytest=MCP | fixtures HTTP deterministas y demo opcional descartable; comparar veredictos/assertions |
| To-do TD-01..06 y CP-01..06 (criterios del evaluador) | fixture To-do conforme: seis casos; variante lost_completion: 5 pass, CP-03 falla GET; CP-02 primero; CP-04 204 vacío/404; CP-05 no muta; CP-06 límite 80/81 |
| To-do §1-4: semilla, OpenAPI operationIds, contrato errores, orden, metadato variante, precondición | tests fixture/export contract; orden estable; bloquear antes de primera petición; control CP-03 standalone |

## Paquetes de trabajo (ownership excluyente; secuencia)

1. **Fundación/modelos** — `pyproject.toml`, `qa_agent/{__init__,models,store,security}.py`, `tests/test_models.py`, `tests/test_store.py`. Define y congela firmas/modelos de arriba; validación OpenAPI/plan, hash/registro, allowlist y redacción. Exporta contrato antes del bloque 2. No runtime/CLI.
2. **Runtime/pytest/MCP** — `qa_agent/{runtime,generate,mcp_server}.py`, `qa_agent/templates/`, `tests/{test_runtime,test_generate,test_mcp}.py`. Consume modelos/store/security sin editarlos; generador, HTTPX, subprocess pytest y MCP stdio. No diseño/CLI/reportes.
3. **Diseño, DeepSeek, CLI y cierre** — `qa_agent/{design,report,cli,__main__}.py`, `qa_agent/skills/`, `README.md` y `tests/{test_design,test_report,test_cli}.py`. Usa los contratos de datos y ejecución. Los tests de diseño usan un proveedor simulado.

`Plan` usa un formato JSON versionado. La aprobación de una suite autoriza su ejecución. La decisión de liberación corresponde al equipo responsable. El runtime confía en el registro, los perfiles y el sistema de archivos locales y ejecuta Python con los permisos del usuario.
