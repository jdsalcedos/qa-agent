# Aceptación independiente To-do (fixture MANUAL)

**Este plan es manual/estático: no fue generado por el diseñador IA, no se ejecuta
`disenar`, no se consulta un LLM y no es evidencia de calidad del diseño con IA.**
`manual_cases.json` transcribe explícitamente los seis escenarios CP-01..06
solicitados. El harness se implementó con asistencia de código; «manual» describe
el origen del plan de prueba frente al pipeline generativo, no una atribución de
autoría humana. Las expectativas proceden de los requisitos públicos y del
contrato; no se importa ni lee el evaluador/oráculo de `todo-api/`.

## Entradas y alcance

- Los [requisitos de la API sintética](../examples/cli-flow/todo-api/docs/requirements.md) incluyen TD-01 a TD-06 y la semilla inicial.
- El agente resuelve las cinco operaciones del [contrato OpenAPI](../examples/cli-flow/todo-api/openapi.json) localmente y guarda su huella en el plan.
- El programa carga `app.main:app` con Uvicorn y ejecuta las variantes sintéticas `reference` y `lost_completion`.

Semilla certificada al arrancar cada proceso:
`[{"id":1,"title":"Preparar demo","completed":false},
{"id":2,"title":"Revisar contrato","completed":true}]`.

| Orden | Caso | Comprobación |
|---|---|---|
| 1 | CP-02 | Filtro textual `true`; raíz exactamente igual a `[tarea 2]` |
| 2 | CP-01 | Creación, ID presente, recorte y `completed=false` |
| 3 | CP-03 | ID capturado, PATCH y GET con título conservado y `completed=true` |
| 4 | CP-04 | Borrado 204 de cero bytes; GET y DELETE 404 con código estable |
| 5 | CP-05 | Título de espacios 422; captura y comparación de listas completas |
| 6 | CP-06 | Aceptar 80 caracteres; rechazar PATCH de 81 y conservar título |

Los seis son obligatorios; CP-02/06 tienen prioridad media. `seed_ready` es una
precondición declarativa de cada caso, certificada por este operador/harness.
Cada mutación crea su propia tarea; solo CP-02 conoce el ID de la semilla.

## Comandos (desde la raíz del repositorio)

Usa el entorno de la [instalación del agente](../README.md#instalación) con los extras `test` y `demo`. La ejecución requiere POSIX para compartir sockets y administrar grupos de procesos.

```bash
# Disponible incluso antes del runtime/CLI; no inicia servidores ni hace HTTP.
.venv/bin/python -B scripts/validate_todo.py --plan-only

# Pruebas offline del fixture/harness.
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider tests/test_acceptance_plan.py

# El destino debe ser nuevo.
.venv/bin/python -B scripts/validate_todo.py --artifacts data/aceptacion-01
```

Sin `--artifacts`, todos los artefactos se eliminan al salir. Con esa opción se
copian también en caso de fallo: revisar `server.log`, `mcp.log`, `pytest.log` y
`close.log`. `summary.json` solo se escribe si pasan todas las comprobaciones.
La copia es evidencia histórica: sus paths absolutos apuntaban al Store temporal;
no constituye un registro movible/reutilizable ni debe ejecutarse sin regenerar.

## Ejecución y controles

1. Preflight inmediato de generador, runtime, cliente/servidor MCP, CLI y cierre.
   Si faltan, falla con diagnóstico: **no espera ni sondea trabajo de workers**.
2. Guarda un plan, genera **una sola suite**, verifica cero corridas y aprueba
   explícitamente únicamente esa suite para esta aceptación autorizada. Esa
   aprobación no es una decisión de liberación.
3. Para cada variante lanza tres servidores distintos con el intérprete del agente:
   suite completa por **MCP stdio real**, suite completa con pytest directo y
   CP-03 individual con pytest. Cada uno parte de la misma semilla.
4. Resultado requerido de la suite completa: reference **6 passed**;
   lost_completion **5 passed y CP-03 failed**, en GET, pese a PATCH/GET 200.
   Compara veredictos y aserciones MCP/pytest. En CP-03 individual compara las
   aserciones PATCH/GET: el ID capturado legítimamente cambia (3 frente a 4).
5. Verifica resultado persistido, evidencias por paso, hashes de plan/script,
   trazabilidad y booleanos esperado/observado. Cierra y vuelve a cerrar por
   CLI **con el servidor ya terminado y red deshabilitada**: no nuevas corridas
   ni cambios a resultados. Exige aplazamiento y observado false para el fallo;
   el control reference parcial no autoriza liberar el plan completo.
6. Reutiliza la misma suite para un perfil sin `seed_ready` (bloqueado, sin pasos
   intentados) y otro con un puerto reservado sin escucha (error técnico, con
   intento/evidencia y sin inventar HTTP). Los cierra offline también.

## Aislamiento y seguridad

- Nuevo socket loopback enlazado a puerto libre y heredado por Uvicorn; no hay
  ventana de cerrar/reabrir puerto. Un trabajador, sin reload. Readiness acotado
  a 12 s exclusivamente sobre el servidor propio.
- `QA_AGENT_HOME`, HOME, perfiles, suites y corridas temporales. Entorno mínimo:
  no hereda claves DeepSeek/OpenAI, proxies ni configuración pytest del usuario.
- Fence por audit hooks para impedir conexiones externas incluso si hubiera
  configuración accidental. El cierre niega además las conexiones loopback;
  cualquier intento denegado deja auditoría y hace fallar la aceptación.
- `PYTHONDONTWRITEBYTECODE=1` y `-B`: no escribe código, cachés ni estado en
  la API de ejemplo. No instala dependencias, no usa servidores
  existentes y no toca procesos ajenos. Todo subprocess propio se termina en
  `finally`, incluido el grupo propio ante timeout.

## Dependencias y límites de la evidencia

APIs diferidas: `generate_suite(..., store=...)`, `execute_suite(..., store=...)`,
`mcp_client.execute_via_mcp(..., store=...)`; CLI `python -m qa_agent cerrar
--corrida ID`, cierre fallback offline y puente pytest con `QA_ENTORNO`. El
harness comprueba esas superficies, no las reimplementa ni finge MCP.

**El éxito de `--plan-only` y los tests offline NO demuestra aceptación E2E.**
La ejecución completa genera un `summary.json` con los resultados y las rutas de las corridas.
El diario puede guardar eventos de inicio y fin por paso. El programa verifica la
observación terminal por contenido. El pytest individual conserva
los seis casos del plan, dejando cinco generados y sin intento.

No comprueba el diseño con IA, la pregunta por requisito ambiguo ni la integración
DeepSeek; no hace ninguna
llamada DeepSeek. La suite es una selección, no cobertura exhaustiva (faltan,
entre otros, filtro inválido, PATCH vacío, tipos inválidos y actualización tras
borrado). Los controles declarativos de bloqueo verifican estado sin intentos;
no son una captura de paquetes ni una prueba de sandbox de Python arbitrario.
