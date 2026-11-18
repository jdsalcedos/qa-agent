# Contrato público de la fundación (v1.0)

Ownership exclusivo fundación: `pyproject.toml`, `.gitignore`, `qa_agent/{__init__,models,store,security,openapi}.py`, `tests/test_{models,store,security,openapi}.py`, este documento. **Los otros workers consumen estas APIs, no las reimplementan ni editan.** Importar `qa_agent` no carga CLI/runtime/diseño ni hace I/O. No hay llamadas a DeepSeek.

## Entorno

Python **3.14.7** (`.venv`), Pydantic **2.14.3**, pytest **9.2.0**, HTTPX **0.28.1**, SDK MCP oficial **2.3.0** (¡API v2!), PyYAML **6.0.3**, Jinja2 **3.1.6**, pytest-asyncio **1.5.0** (extra test); hatchling **1.33.0**. Entry point declarado `qa-agent = qa_agent.cli:main`; la CLI pertenece al worker 3. Ejecutar con `.venv/bin/python`, no el Python del sistema anterior. Instalar con `.venv/bin/python -m pip install -e '.[test]'` desde `qa-agent/`.

## Convenciones de modelos (`qa_agent.models`)

Todos son Pydantic v2, `extra='forbid'`; campos sin `=...` abajo son requeridos. JSON canónico **inglés**. Usar `model_dump(mode='json', by_alias=True)` / `model_validate(...)`; fechas ISO-8601 UTC. `Identifier`: `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`; no paths. `Digest`: SHA256 hexadecimal minúscula, 64 caracteres. `JsonValue`: JSON sin NaN/Infinity, preserva tipos. Listas mantienen orden. `NonEmpty`: string no vacío. Los modelos son mutables: actualizar un resultado con `model_copy(update=...)` y revalidarlo al persistir, evitando estados intermedios inválidos.

Enums (nombres de miembros MAYÚSCULOS):
- `Priority`: `HIGH='high'`, `MEDIUM='medium'`, `LOW='low'`.
- `CaseState`: `DESIGNED`, `GENERATED`, `BLOCKED`, `PASSED`, `FAILED`, `TECHNICAL_ERROR`, `INCONCLUSIVE` con valores minúsculos correspondientes.
- `RunStatus`: `PENDING`, `RUNNING`, `COMPLETED`, `INTERRUPTED`, `FAILED`, valores minúsculos.

### Diseño y contrato HTTP

- `Requirement(id: Identifier, text: NonEmpty)`.
- `Parameter(name: NonEmpty, location: 'path'|'query', required: bool=False, schema_: dict[str,JsonValue]={})`. La clave JSON es **`schema`**; constructor acepta `schema=` o `schema_=`.
- `Operation(operation_id: NonEmpty, method: 'GET'|'POST'|'PUT'|'PATCH'|'DELETE'|'HEAD'|'OPTIONS'|'TRACE', path: NonEmpty, parameters: list[Parameter]=[], request_body: dict[str,JsonValue]|None=None, responses: dict[str,JsonValue]={})`. `request_body` contiene el objeto requestBody OpenAPI completo. `responses` conserva los objetos por status. Ruta absoluta sin origen, query, escapes `%`, `..` ni fragmentos. Parámetros path obligatorios coinciden con placeholders.
- `StatusAssertion(kind: Literal['status']='status', status: int[100..599])` (int estricto, no bool).
- `PresentAssertion(kind: Literal['present']='present', pointer: str)`.
- `EqualAssertion(kind: Literal['equal']='equal', pointer: str, value: JsonValue)`; `value` **obligatorio**, puede ser null o `{'var':'x'}`.
- `EmptyBodyAssertion(kind: Literal['empty_body']='empty_body')`: cero bytes, no decodificar JSON.
- `Assertion`: unión discriminada por `kind` de los cuatro modelos anteriores.
- `Step(operation_id: NonEmpty, path: dict[str,JsonValue]={}, query: dict[str,JsonValue]={}, body: JsonValue=None, body_present: bool=False, assertions: list[Assertion] [mínimo 1], captures: dict[str,str]={})`. Capturas: nombre -> JSON Pointer; **todas obligatorias**, alcance caso; capturar con deepcopy. **`body_present` distingue ausencia de JSON null** y se infiere automáticamente al construir un Step: si se suministra `body` (incluso None), es True. Se serializa el flag explícito, preservándolo al guardar/cargar. Runtime: si body_present=False, no enviar cuerpo; si True, serializar `body` como JSON, incluso `b'null'` cuando body=None (HTTPX `json=None` puede omitirlo: usar content explícito en ese caso). No usar `body is not None` para decidir presencia. Un body no-null con flag False es inválido. No validar el payload contra schema: los negativos deben conservar sus tipos/valores inválidos.
- `Case(id: Identifier, requirement_ids: list[Identifier] [mínimo 1], objective: NonEmpty, priority: Priority='high', mandatory: bool=True, preconditions: list[NonEmpty]=[], data: dict[str,JsonValue]={}, expected: NonEmpty, steps: list[Step]=[], automation_pending_reason: str|None=None)`. Primer requisito es principal. `case.executable` (propiedad bool): pasos presentes y sin motivo pendiente. Sin pasos requiere motivo; no excluir estos casos del denominador planificado. Variables se declaran inicialmente en `case.data`; capturas sin colisiones y solo utilizables en pasos posteriores. Datos del perfil sirven para precondiciones/ensayo del ejecutor: no son declaraciones implícitas de variables del plan.
- `ExitCriteria(mandatory_high_must_pass: bool=True, verdicts_require_evidence: bool=True, no_open_critical_defects: bool=True)`.
- `Plan(version: Literal['1.0']='1.0', requirements: list[Requirement]=[], contract_hash: Digest|None=None, operations: list[Operation]=[], cases: list[Case]=[], exit_criteria: ExitCriteria=ExitCriteria(), pending_questions: list[str]=[], skill_hashes: dict[str,Digest]={})`. **Operaciones resueltas se incrustan en plan**: ejecución guardada no necesita OpenAPI/LLM. Ejecutables requieren contract_hash y operationIds conocidos. IDs/referencias únicos y válidos. Plan parcial sin contrato: solo casos pendientes. `PlanDraft = Plan` (alias, mismos controles).

Compatibilidad de entrada del ejemplo de la spec: Case admite `requisito` -> lista de uno, `objetivo/prioridad/obligatorio/precondiciones/datos/esperado/pasos`; prioridades `alta/media/baja`. Step admite `operacion/assert/capturar`. Assertions abreviadas admitidas: `{'status':201}`, `{'presente':'/id'}`, `{'igual':'/x','valor':...}`, `{'cuerpo_vacio':true}`. Serializar siempre en inglés/catálogo canónico. El alias `cuerpo_vacio` exige el booleano literal **true**: `1`, `1.0`, false y strings son inválidos. No hay campo aprobación dentro del plan.

Helpers:
```python
utc_now() -> datetime
validate_pointer(pointer: str) -> str              # ValueError por formato inválido
json_pointer(document: JsonValue, pointer: str) -> JsonValue  # KeyError si falta
strict_json_equal(left: JsonValue, right: JsonValue) -> bool
variable_names(value: JsonValue) -> set[str]
substitute_variables(value: JsonValue, variables: dict[str,JsonValue]) -> JsonValue
```
Pointers RFC6901: raíz `''`, escapes `~0/~1`; listas sin ceros iniciales ni `-`. Igualdad **true != 1 y 1 != 1.0**, listas ordenadas, null != ausencia. Sustitución deep-copy de objetos exactamente `{'var':'nombre'}`, sin interpolación de strings ni eval; KeyError si no declarada.

### Perfil, suite y aprobación

- `AllowedTarget(host: NonEmpty, port: int[1..65535], ips: list[NonEmpty] [mínimo 1])`.
- `EnvironmentProfile(id: Identifier, base_url: NonEmpty, allowed_targets: list[AllowedTarget] [mínimo 1], allowed_operations: list[NonEmpty] [mínimo 1], product_version: NonEmpty, data: dict[str,JsonValue]={}, preconditions: dict[str,bool]={}, credentials: dict[str,NonEmpty]={}, request_timeout_seconds: float(0..120]=10.0, process_timeout_seconds: float(0..3600]=120.0, max_response_bytes: int(0..10000000]=1000000)`. Credenciales: **nombre de header -> nombre de variable de entorno**, nunca su valor. Precondiciones: flags declarativos certificados por operador, ninguna sonda HTTP/shell. MVP solo IP numérica loopback explícita; **no `localhost` ni DNS** (evita rebinding sin transport propio). La IP exacta y puerto deben estar allowlisted. Cambio de versión del producto no cambia aprobación; el perfil es configuración local confiable, no datos del plan. La aprobación cubre suite/plan, no congela perfiles; revisar perfil antes de cada ejecución.
- `SuiteCase(case_id: Identifier, requirement_ids: list[Identifier], script: NonEmpty, script_hash: Digest)`.
- `SuiteManifest(version: Literal['1.0']='1.0', suite_id: Identifier, plan_hash: Digest, source_plan_path: NonEmpty, cases: list[SuiteCase]=[], files: dict[str,Digest]={}, created_at: datetime=utc_now())`. `source_plan_path` **absoluto**. `cases` en orden de los casos ejecutables del plan. `files` contiene **todos los archivos payload, incluido plan.json, scripts, runtime/conftest/config/lock; excluye manifest.json** para evitar hash circular. Hash de script debe coincidir con files. Scripts únicos. No symlinks/traversal. Registro externo fija huella del manifiesto, por lo que editar hashes juntos no reautoriza.
- `Approval(suite_id: Identifier, plan_hash: Digest, manifest_hash: Digest, approved_by: NonEmpty, approved_at: datetime=utc_now())`. Se crea solo mediante acto explícito del CLI/operador, nunca desde LLM.

### Ejecución y evidencia

- `AssertionResult(kind: 'status'|'present'|'equal'|'empty_body', passed: bool, expected: JsonValue=None, observed: JsonValue=None, message: str|None=None)`.
- `StepEvidence(run_id: Identifier, case_id: Identifier, requirement_ids: list[Identifier], script: NonEmpty, script_hash: Digest, plan_hash: Digest, product_version: NonEmpty, step_index: int>=0, operation_id: NonEmpty, started_at: datetime=utc_now(), duration_ms: float>=0=0.0, state: CaseState, http_status: int[100..599]|None=None, request: dict[str,JsonValue]={}, response: dict[str,JsonValue]={}, expected: JsonValue=None, observed: JsonValue=None, assertions: list[AssertionResult]=[], captures: dict[str,JsonValue]={}, error: str|None=None)`.
- `CaseResult(case_id: Identifier, requirement_ids: list[Identifier], script: str|None=None, script_hash: Digest|None=None, state: CaseState='generated', attempted: bool=False, steps: list[StepEvidence]=[], evidence_paths: list[str]=[], reason: str|None=None, critical_defect: bool=False)`. Estados observados exigen attempted=True; designed/generated/blocked exigen False. No inventar respuesta/HTTP si no existe. Marcar inicio durable **antes** de HTTP: usar estado inconclusive + attempted=True mientras está en curso; al recuperar tras interrupción conserva que hubo intento. `steps` puede contener evidencias depuradas completas; paths corresponden a archivos persistidos.
- `RunResult(run_id: Identifier, suite_id: Identifier, plan_hash: Digest, environment_id: Identifier, product_version: NonEmpty, status: RunStatus='pending', cases: list[CaseResult]=[], started_at: datetime=utc_now(), ended_at: datetime|None=None, result_path: str|None=None, evidence_paths: list[str]=[], error: str|None=None)`.

## OpenAPI offline (`qa_agent.openapi`)

```python
class OpenAPIContract(Model):
    contract_hash: str
    operations: list[Operation] = []
    pending_questions: list[str] = []
    def operation(self, operation_id: str) -> Operation: ... # KeyError si falta
class ContractIssue(ValueError): ...
load_openapi(path: Path) -> OpenAPIContract
parse_openapi(document: dict) -> OpenAPIContract
resolve_local_refs(value: Any, document: dict, stack: tuple[str,...]=()) -> Any
validate_plan(plan: Plan, contract: OpenAPIContract|None=None) -> Plan
```
Refs exclusivamente `#/components/schemas/...`, expandidas sin red/lectura externa; faltantes/cíclicas/externas/siblings ambiguos excluyen operación y generan pending_questions. La resolución es **estructural por contexto OpenAPI/Schema**: `example`, `Example.value`, defaults, enum/const y ejemplos de schema son datos literales, aunque contengan `{'$ref': ...}`. Los nombres en mapas (`properties`, esquemas, ejemplos nombrados, etc.) tampoco son keywords: una propiedad llamada `$ref` es válida. Las referencias reales dentro de los schemas de esas propiedades sí se resuelven/validan. Un Reference Object real en un mapa de ejemplos sigue sujeto a la política offline. `resolve_local_refs` admite raíz schema, documento OpenAPI, operación con operationId o array de parámetros; no interpreta objetos de aplicación como raíces de contrato. IDs duplicados y rutas equivalentes se excluyen ambos. Parámetro local puede sobrescribir heredado (OpenAPI). Otros parámetros/cuerpos no JSON quedan pendientes. El hash cubre documento JSON canónico completo. `validate_plan` con contrato comprueba hash/operaciones exactas; sin contrato revalida integridad interna. El diseñador debe trasladar pending_questions y marcar casos afectados pendientes; no rellenar expectativas sin fuente.

## Persistencia (`qa_agent.store`)

Excepciones: `StoreError(ValueError)`, `IntegrityError(StoreError)`, `ApprovalError(StoreError)`.

```python
canonical_json(value: Any) -> bytes               # modelos o JSON; ordena claves, no arrays
hash_json(value: Any) -> str
hash_file(path: Path) -> str                      # bytes exactos, no symlinks
plan_hash(plan: Plan) -> str                      # Plan revalidado, defaults y alias canónicos
read_json(path: Path) -> Any                      # rechaza claves duplicadas/NaN
atomic_write_json(path: Path, value: Any) -> Path
atomic_write_bytes(path: Path, value: bytes) -> Path
load_plan(path: Path) -> Plan
save_plan(path: Path, plan: Plan) -> Path
load_profile(path: Path) -> EnvironmentProfile    # JSON o YAML safe_load + seguridad
safe_suite_file(root: Path, relative: str) -> Path # valida confinamiento y symlinks
suite_files(root: Path) -> dict[str,str]           # inventario hashes sin manifest/cache

Store(root: Path|None=None)
store.root: Path                                  # absoluto
store.save_plan(plan: Plan, plan_id: str|None=None) -> Path # ID nuevo, no overwrite
store.save_profile(profile: EnvironmentProfile) -> Path
store.load_profile(environment_id: str) -> EnvironmentProfile
store.register_suite(suite_path: Path, manifest: SuiteManifest) -> SuiteManifest
store.verify_suite(suite_id: str, *, require_approval: bool=True) -> tuple[Path,SuiteManifest,Plan]
store.approve_suite(suite_id: str, *, approved_by: str) -> Approval
store.suite_id_for_path(suite_path: Path) -> str
store.run_dir(run_id: str) -> Path
store.create_run(suite_id: str, environment_id: str) -> RunResult
store.save_run(result: RunResult, *, secrets: Iterable[str]=()) -> Path
store.load_run(run_id: str) -> RunResult
store.load_run_plan(run_id: str) -> Plan
store.verify_run_integrity(run_id: str) -> None
store.load_evidence(run_id: str, evidence_path: Path|str) -> StepEvidence # absoluto o relativo a run_dir
store.append_evidence(evidence: StepEvidence, *, secrets: Iterable[str]=()) -> Path
store.list_runs() -> list[str]
```

Default root: `$QA_AGENT_HOME` o `qa-agent/data` (no cwd). Registro en `suites/<suite_id>/registration.json`, aprobación al lado; perfil en `environments/<id>/profile.json`; planes en `plans/<id>/plan.json`; corridas `runs/<run_id>/{plan.json,environment.json,resultados.json,integrity.json,evidence/,suite/}`. El índice de integridad guarda SHA256 de cada artefacto histórico y se actualiza al persistir resultados/evidencia. `load_run`, `load_run_plan` y `load_evidence` verifican hashes; pérdida/corrupción se señala con IntegrityError, nunca se convierte en observaciones ficticias. Un fallo entre dos reemplazos atómicos puede dejar el índice atrasado: se detecta como integridad incompleta, preservando los archivos ya escritos. Informes usan `store.run_dir(id)/'cierre.md'`. `save_run` reemplaza snapshot agregado atómicamente; evidencia por paso se agrega con UUID, nunca reutilizar paths; sobreviven a interrupción aunque resultado agregado esté atrasado. Cada llamada create_run crea historial propio y copia suite exacta verificada. El runtime debe ejecutar **snapshot** `run_dir/'suite'`, no el original mutable. Cierre histórico usa load_run_plan y resultados, **no** verify_suite original (puede haberse cambiado después).

`register_suite` requiere output ya completo con `plan.json`; calcula `files = suite_files(output)` antes de crear manifest; escribe manifest y registro. No reutilizar suite_id (FileExistsError). `verify_suite` verifica todo payload, manifiesto contra registro, copia del plan **y archivo fuente original**. Modificar o quitar el original invalida ejecución/aprobación, aunque copia no cambie. Cambiar solo whitespace/orden de claves JSON no invalida: huella semántica canónica. El filesystem/usuario local son confiables; esto no es sandbox ni defensa contra dueño del registro. Copia + rehash reduce TOCTOU, no garantiza aislamiento ante un atacante concurrente con permisos sobre snapshot. Config/plugin injection y entorno subprocess son responsabilidad runtime.

### Endurecimientos compatibles tras revisión

Sin cambios de firmas públicas:
- `append_evidence` revalida el modelo completo antes de índices o escrituras; `model_copy` no permite saltarse restricciones (`step_index >= 0`, HTTP int estricto, etc.). Un rechazo no agrega archivos.
- `save_run` exige script y hash exactos del manifiesto en cada CaseResult (None/None para casos sin script), además de validar cada StepEvidence embebida contra corrida, caso contenedor, requisitos, plan, script/hash, versión, índice y operación.
- Cada `CaseResult.evidence_paths` debe apuntar a evidencia registrada de **esa corrida y ese caso**; cada referencia de RunResult debe pertenecer a la corrida. Una StepEvidence embebida debe coincidir íntegramente con una de las referencias persistidas del caso, después de aplicar la misma redacción. Primero `append_evidence`, luego agregar path y paso al resultado, y finalmente `save_run(..., secrets=...)`. Los paths sin paso embebido siguen siendo válidos (p. ej. evento de inicio).
- El estado provisional previo al HTTP sigue admitido: `state='inconclusive', attempted=True, steps=[], evidence_paths=[]` (representa in_progress sin agregar un enum nuevo). También puede persistirse/embeberse un evento de inicio sin respuesta, sin HTTP observado y sin aserciones evaluadas. No se exige inventar una respuesta para guardar el intento.
- La redacción de secretos escalares también se aplica al comparar contenido embebido/persistido; pasar el mismo conjunto de secretos a append/save. Iterables de secretos de un solo uso se materializan dentro de save_run.
- La redacción de `StepEvidence`, `AssertionResult`, `CaseResult` y `RunResult` es **field-aware**, incluida la recursión por assertions/steps/cases. Se depuran exclusivamente `request`, `response`, `expected`, `observed`, `captures` y textos no-null `message`, `error`, `reason`. Se preservan IDs, referencias, scripts, hashes, estados, kinds, HTTP status, índices, duración, fechas, flags `attempted`/`passed`/`critical_defect` y demás metadatos autoritativos. Los campos tipados opcionales mantienen None, aunque el secreto sea `'null'`. `append_evidence` y `save_run` revalidan también el objeto **ya depurado** antes de escribir.
- Pasar el **modelo tipado** a `redact` si contiene metadatos; no convertirlo primero con model_dump. Dict/list/escalares genéricos siguen tratándose como payload libre, sin inferir campos autoritativos por su nombre. Esto mantiene compatibilidad de firmas y permite ocultar un eco numérico 200 sin destruir `http_status=200`, o un eco true sin cambiar `attempted=True`/`passed=True`.

## Seguridad (`qa_agent.security`)

```python
class SecurityError(ValueError): ...
REDACTED = '[REDACTED]'
validate_base_url(profile: EnvironmentProfile) -> str # base sin trailing slash
authorize_operation(profile: EnvironmentProfile, operation: Operation|str) -> None
build_url(profile: EnvironmentProfile, operation: Operation, path_values: Mapping[str,JsonValue]) -> str
resolve_credentials(profile: EnvironmentProfile, environ: Mapping[str,str]|None=None) -> dict[str,str]
redact(value, secrets: Iterable[str]=())              # estructura separada, JSON-safe
missing_preconditions(case: Case, profile: EnvironmentProfile) -> list[str]
httpx_client_options(profile: EnvironmentProfile) -> dict
```
Usar HTTPX `params=step.query`, nunca concatenar query, ni URL del plan/respuesta/captura. `build_url` autoriza operación y ruta contractual, rechaza path traversal/separadores/escapes y codifica valor escalar. `httpx_client_options` fija timeout finito, `follow_redirects=False`, `trust_env=False`; cero retries mutantes (configurar transporte sin retries). Destino numeric-loopback evita DNS; rechazar redirects, no seguirlos. Resolver credenciales **solo desde variables nombradas**, no inspeccionar todo env. Pasar `headers.values()` como `secrets` a **todas** persistencias/logs/excepciones/LLM/stdout/stderr. `redact` oculta keys sensibles y reflejos de valores suministrados (incluye percent-encoding); también depura escalares JSON coincidentes con un secreto interpretable como escalar JSON, con **tipo exacto** (`'123456'` oculta el entero 123456; `'1'` no oculta true). No puede adivinar secretos desconocidos. Comparar valores raw antes de depurar. Retener semántica missing/null/redacted; truncación debe dar inconcluso si elimina evidencia necesaria. Límites de respuesta se aplican en runtime.

## Ejemplo mínimo válido completo de plan

El hash repetido es sintético para ejemplo; producción usa `load_openapi(...).contract_hash`.

```json
{
  "version": "1.0",
  "requirements": [{"id": "R-01", "text": "Consultar recurso"}],
  "contract_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operations": [{"operation_id": "getResource", "method": "GET", "path": "/resources/{id}", "parameters": [{"name": "id", "location": "path", "required": true, "schema": {"type": "integer"}}], "responses": {"200": {"description": "OK"}}}],
  "cases": [{"id": "CP-01", "requirement_ids": ["R-01"], "objective": "Recuperar el recurso", "expected": "HTTP 200", "data": {"id": 1}, "steps": [{"operation_id": "getResource", "path": {"id": {"var": "id"}}, "assertions": [{"kind": "status", "status": 200}]}]}]
}
```

Perfil JSON válido (sin secretos):
```json
{"id":"local","base_url":"http://127.0.0.1:8765","allowed_targets":[{"host":"127.0.0.1","port":8765,"ips":["127.0.0.1"]}],"allowed_operations":["getResource"],"product_version":"reference","preconditions":{"seed_ready":true},"credentials":{"Authorization":"QA_TEST_AUTH"}}
```

Flujo concreto de generación/store/aprobación (sin ejecutar HTTP):
```python
from pathlib import Path
from uuid import uuid4
from qa_agent.models import SuiteManifest, SuiteCase
from qa_agent.store import Store, load_plan, plan_hash, save_plan, suite_files

store = Store()  # mismo QA_AGENT_HOME en CLI, MCP y pytest directo
source = Path('plan.json').resolve()
plan = load_plan(source)
output = Path('suite').resolve()
output.mkdir()
save_plan(output / 'plan.json', plan)
# Worker generador escribe scripts seguros, common runtime, config, lock ANTES:
# (output / 'test_cp_01.py').write_text(...)
files = suite_files(output)
manifest = SuiteManifest(
    suite_id='suite-' + uuid4().hex, plan_hash=plan_hash(plan),
    source_plan_path=str(source), files=files,
    cases=[SuiteCase(case_id='CP-01', requirement_ids=['R-01'],
                     script='test_cp_01.py', script_hash=files['test_cp_01.py'])])
store.register_suite(output, manifest)
# SOLO tras revisión y acción humana explícita:
approval = store.approve_suite(manifest.suite_id, approved_by='operador')
suite_path, verified_manifest, verified_plan = store.verify_suite(manifest.suite_id)
# store.save_profile(profile); run = store.create_run(manifest.suite_id, 'local')
# store.append_evidence(evidence, secrets=credential_headers.values())
# store.save_run(updated_run, secrets=credential_headers.values())
```

## Firmas que implementan los otros workers

```python
# generate.py / runtime.py / mcp_server.py (worker 2)
generate_suite(plan_path: Path, output: Path) -> SuiteManifest
execute_suite(suite_id: str, environment_id: str) -> RunResult
# MCP stdio oficial: ejecutar_plan(suite_id: str, entorno_id: str)
# Respuesta: run_id, status, result_path, evidence_paths; no rutas arbitrarias input.

# design.py / report.py / cli.py (worker 3)
design_plan(requirements: Path, openapi: Path|None, data: Path|None, llm: Designer) -> Plan
# Designer.propose(prompt: str) -> PlanDraft
close_run(run_id: str) -> Path
close_plan(plan_path: Path) -> Path
```

MCP SDK **2.3.0** requiere consultar documentación v2, no asumir imports v1. Documentación v2.3.0 consultada con find-docs: `from mcp import Client, StdioServerParameters`; `async with Client(StdioServerParameters(...)) as client:` negocia protocolo; FastMCP fue renombrado **MCPServer** y el import v1 `mcp.server.fastmcp` ya no existe. `stdio_client` se exporta desde `mcp`. El cliente subprocess hereda solo un entorno allowlisted: pasar QA_AGENT_HOME y las variables de credenciales necesarias explícitamente, sin volcar todo el entorno. Runtime es responsable de tests stdio reales, pytest separado/confinado, redacción de salida subprocess y aprobación también en pytest directo. Python arbitrario editado/lanzado manualmente no puede convertirse en sandbox mediante un check dentro del propio script. Reporte no debe inferir aprobación de liberación desde aprobación humana de suite: son decisiones distintas. No afirmar E2E por pruebas de fundación.
