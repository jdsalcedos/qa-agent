# Contrato RUNTIME implementado

## APIs de integración

```python
from pathlib import Path
from qa_agent.generate import generate_suite
from qa_agent.runtime import execute_suite
from qa_agent.mcp_client import execute_via_mcp

# Las tres funciones son síncronas; store es keyword-only y opcional.
generate_suite(plan_path: Path, output: Path, *, store: Store | None = None) -> SuiteManifest
execute_suite(suite_id: str, environment_id: str, *, store: Store | None = None) -> RunResult
execute_via_mcp(suite_id: str, environment_id: str, *, store: Store | None = None) -> dict
```

La CLI `ejecutar` debe llamar **execute_via_mcp**, no execute_suite directamente. El cliente arranca un proceso real del SDK oficial MCP **2.3.0**, negocia protocolo, descubre `ejecutar_plan` e invoca `{suite_id, entorno_id}`. Retorna exactamente `run_id`, `status`, `result_path`, `evidence_paths`, resolviendo la respuesta contra Store. El wrapper síncrono no se puede llamar desde un event loop activo. Los errores públicos son genéricos: no incluyen excepciones remotas ni credenciales. MCP no recibe paths, URLs, argumentos pytest ni scripts. Middleware estricto rechaza extras, tipos incorrectos y traversal antes del ejecutor. stdout del servidor se reserva para JSON-RPC; pytest y stderr del transporte no se transmiten sin depurar.

Import confirmado con documentación v2.3.0 mediante find-docs/Context7 e inspección de la instalación: `from mcp.server.mcpserver import MCPServer`; cliente `from mcp import Client, StdioServerParameters, stdio_client`. No se usa FastMCP v1. El bootstrap aislado importa el paquete desde una ruta fija confiable. La función herramienta es async pero ejecuta secuencialmente sobre el hilo principal para poder gestionar SIGTERM y finalizar su grupo pytest.

## Generación, exportación y pytest directo

Generar no hace HTTP, no contacta IA ni instala dependencias. Requiere salida nueva, sin sobrescribir suites. Produce:

- `test_0000.py`, etc.: una función pytest por caso ejecutable, con el ID como dato. El índice corresponde al orden original del plan; evita colisiones de IDs/filesystems.
- `conftest.py`: fixture común y orden de colección según manifiesto.
- `_qa_runtime/`: copias autocontenidas de modelos, Store, seguridad, runtime, generador y sus plantillas. No importa `qa_agent` ni necesita el source tree para su lógica.
- `plan.json`, `manifest.json`, `pytest.ini`, `requirements.lock` con versiones exactas de toda la clausura de dependencias del runner verificada en Python **3.14.7**. El export no necesita instalar MCP ni qa-agent.

El manifiesto relaciona requisitos/caso/script/hash y cubre todos los archivos payload. Store fija externamente la huella del manifiesto. Además del hash, runtime compara código/configuración/inventario con el generador confiable: registrar y aprobar un Python arbitrario no lo convierte en ejecutable MCP. Una actualización del runtime requiere regenerar/reaprobar suites antiguas para MCP; una exportación independiente conserva su runtime fijado.

Uso directo, con dependencias ya instaladas, registro/perfil/aprobación preparados y shell confiable:

```bash
QA_AGENT_HOME=/ruta/absoluta/estado QA_ENTORNO=local pytest -q /ruta/suite/test_0000.py
# También admite pytest -q /ruta/suite para seleccionar todos los scripts.
```

**La aprobación explícita también se exige aquí** (`store.approve_suite(id, approved_by='operador')`, tras revisión humana). La fixture crea una nueva corrida y ejecuta la selección en su propio snapshot verificado mediante el mismo subprocess pytest que MCP. Los no seleccionados conservan estado generated; los no automatizables, designed. El pytest exterior solo presenta los resultados guardados: skip para bloqueo, fail para fallo/técnico/inconcluso; no sustituir estos estados por el exit code de pytest.

Exportable no significa sin estado: se necesitan el registro externo confiable, aprobación, perfil, `QA_AGENT_HOME` explícito y el archivo fuente original del plan indicado por el manifiesto. Cambiar/mover/eliminar ese plan invalida ejecución. Para trasladar una suite, mantener o restablecer explícitamente registro/ruta fuente y obtener aprobación mediante el Store incluido; nunca editar hashes para conservar aprobación. El default de Store dentro del export no apunta al estado del proyecto original.

## Ejecución, evidencia e historial

1. Verificar suite, fuente, aprobación y código generado; cargar perfil numérico loopback autorizado. Rechazar credenciales incrustadas en payload/perfil, porque los snapshots firmados no pueden redactarse modificando sus bytes.
2. `Store.create_run` crea historial independiente y copia exacta; revalidar el snapshot y ejecutar **ese**, no la suite mutable original.
3. Comprobar precondiciones declarativas y autorización de todas las operaciones antes de cada caso. Credenciales ausentes bloquean, sin HTTP. El perfil histórico queda fijado en `environment.json`: cambiar el perfil externo no modifica una corrida en curso. Flags son certificaciones del operador, no sondas ni scripts.
4. Antes de cada HTTP, persistir agregado `attempted=True/state=inconclusive`, evidencia inicial atómica y referencias. Después, añadir evidencia final en otro archivo UUID y actualizar agregado. Hay dos eventos por paso completado, no dos intentos. `CaseResult.steps` contiene únicamente el evento final; paths contienen inicio y final. Fechas, duración, HTTP real, operación, requisito, script/hash, versión, esperado, observado, capturas y errores quedan trazables.
5. Evaluar todos los asserts del paso; detener el caso al primer paso no aprobado. Otros casos continúan en orden. Nunca repetir automáticamente una mutación.

HTTPX: timeout finito de perfil, transporte `retries=0`, sin proxy de entorno, sin redirects; cualquier 3xx es rechazo técnico, sin segunda conexión. El límite de bytes se verifica mientras se consume la respuesta decodificada; excederlo produce inconcluso, no aprobado por el status. El timeout de proceso limita también respuestas que gotean indefinidamente.

Semántica: sustitución tipada/deepcopy, captura local y detached, JSON Pointer RFC6901, igualdad estricta (booleano != entero != float), listas ordenadas, raíz vacía. `body_present=False` omite body; explícito `body=None` envía `b'null'`. Cuerpo vacío significa cero bytes, sin parsear JSON. Presencia de null es presencia; ausencia queda identificada con mensaje propio. JSON ilegible/ambiguo o captura obligatoria ausente es **inconcluso**; un mismatch observado de otra aserción domina como **failed**. Error de transporte/redirect es **technical_error** tras intento. Pendientes/bloqueados no cuentan como éxitos ni intentos. RunStatus completed significa que el worker terminó los casos seleccionados, no que todos pasaron. Un caso terminal technical_error por parámetro path no escalar después de un paso aprobado sigue siendo una corrida completed (sin segunda petición); la detección de pasos sin finalizar solo considera casos todavía inconclusive.

El límite del proceso/SIGINT/SIGTERM termina el grupo propio, guarda status interrupted, conserva casos concluidos y deja no iniciados pendientes. En POSIX una tubería de vida mata al worker si el padre desaparece incluso por SIGKILL; los intentos ya persistidos sobreviven, sin segunda petición. SIGKILL no permite actualizar status final: puede quedar running con caso inconclusive y evento de inicio. No hay reanudación/reintentos automáticos. Los fallos atómicos entre artefacto e índice se detectan como integridad incompleta; no se inventan observaciones para repararlos.

## Secretos y límites de confianza

Solo se resuelven variables nombradas en `profile.credentials`. Se redactan estructuras separadas y reflejos antes de evidencia/agregados; se compara raw antes de depurar. Para respuestas JSON, `response.body_text` es una representación normalizada de una copia decodificada y depurada **antes** de serializarla: comillas, backslashes y escapes Unicode no permiten reconstruir la credencial con `json.loads`. No es una copia byte-a-byte; `body_bytes` conserva el tamaño recibido. Aserciones y capturas usan exclusivamente los bytes originales. Evidencias finales y steps embebidos reciben la misma redacción field-aware de Store, sin pre-redactar metadatos autoritativos. Variables del subprocess se allowlistean: no se heredan PYTHONPATH, PYTEST_ADDOPTS, PYTEST_PLUGINS, proxies, loader flags ni secretos ajenos. Se rechazan nombres de credencial que colisionen con controles del runner. El cliente neutraliza también HOME/PATH/etc que el SDK hereda por defecto. No hay dumps del entorno.

El worker usa intérprete absoluto `-I -B`, bootstrap fijo, cwd del snapshot, configuración explícita, `--confcutdir`, desactivación de autoload, paths verificados y ningún shell/argumento derivado del plan. stdout/stderr pytest van a DEVNULL: se pierde detalle de depuración del framework a cambio de no emitir cuerpos/excepciones sin depurar. Los errores públicos conservan una clase útil cuando es posible, pero usan texto genérico.

**No es sandbox frente al dueño de la cuenta/registro ni a cambios concurrentes con esos permisos.** Dependencias instaladas y configuración del intérprete son confiables. El pytest exterior lanzado manualmente puede cargar plugins/código antes de nuestra fixture; debe ejecutarse en entorno confiable. No se promete proteger un script editado y ejecutado voluntariamente. Garantías de grupos/tubería verificadas en POSIX; Windows no validado. El byte-limit no es una cuota absoluta de memoria del descompresor HTTPX. No hay concurrencia de escritores en una misma corrida ni limpieza automática de historial. Informes/cierre pertenecen al otro bloque.

## Evidencia de verificación

Desde `qa-agent/`, entorno existente sin modificar versiones:

```text
.venv/bin/python -m pytest -q tests/test_generate.py tests/test_runtime.py tests/test_mcp.py --tb=short
66 passed in 40.80s

.venv/bin/python -m pytest -q
398 passed in 40.66s
```

Incluyen subprocess pytest real; doble paso/captura; equivalencia exacta de aserciones/capturas/veredictos entre MCP y pytest directo; misma suite aprobada contra fixtures reference/variant con true esperado/false observado; descubrimiento/ejecución MCP stdio reales; bloqueo sin aprobación/credencial/precondición; manipulación de payload/fuente; snapshot frente a original mutado; fallo estricto; body ausente/null; JSON Pointer; captura ausente; negativos 422 sin coerción; redirects; response-size; timeout petición/proceso sin retry; SIGKILL del padre con cero peticiones posteriores; inyección de plugins/env/config; canarios secretos ausentes en artefactos; seis regresiones que decodifican body_text persistido con comillas/backslashes/escapes Unicode, comprueban equivalencia evento/step y reutilizan una captura original para igualdad; error técnico terminal previo al segundo HTTP con corrida completed. Las siete reproducciones de P1/P2 fallaron antes del fix y pasaron después (`7 passed, 49 deselected in 5.89s`, selección `-k 'json_escaped_credentials or terminal_technical_error'` sobre `tests/test_runtime.py`). Otra regresión envía SIGKILL al worker tras el primer HTTP y confirma caso inconclusive/corrida interrupted, sin segunda petición. Solo fixtures HTTP numéricas loopback propias y desechables. **Cero llamadas a DeepSeek y ninguna demostración contra todo-api.** No hay bloqueos fundacionales pendientes.
