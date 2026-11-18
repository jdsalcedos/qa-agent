# Flujo CLI verificable y evaluación de flags

Desde la raíz de `qa-agent`, después de seguir la [instalación](../README.md#instalación). Requiere Bash y `jq`. Usa la API sintética incluida y ejecuta los bloques en orden, con una revisión humana antes de aprobar.

## Inventario exacto

`O` = obligatorio; `?` = opcional. No hay defaults implícitos de plan, suite, salida, responsable ni entorno.

| Comando | Todos sus flags y efecto | Positivo / negativo observable |
|---|---|---|
| Todos, incluida raíz | `-h`, `--help`: ayuda y salida 0 | Bucle de ayuda abajo; no crea artefactos |
| `disenar` | `--requisitos O`: Markdown identificado; `--contrato ?`: OpenAPI JSON local, default `None`; `--datos ?`: objeto sintético JSON/YAML, default `None`; `--salida O`: archivo del plan | Sección IA: `plan_path`, `cases`, `attempts:0`; fuentes inválidas se rechazan. Omitir contrato permite diseño parcial; omitir datos no aporta datos sintéticos |
| `generar` | `--plan O`: archivo Plan; `--salida O`: directorio **nuevo** | Core: `suite_id`, `plan_hash`, aprobación pendiente; archivo inexistente/inválido o salida existente: 1 |
| `aprobar` | `--suite O`: ID registrado o directorio; `--responsable O`: identidad humana; `--confirmar O`: switch sin valor, default interno `False`, pero omitirlo es error | Core: aprobación con responsable y hashes; omitir identidad/confirmación: 2; suite inexistente/alterada: 1 |
| `ejecutar` | `--suite O`: ID o directorio; `--entorno O`: ID de perfil registrado, **no ruta** | Core: JSON `run_id/status/result_path/evidence_paths`; perfil/suite ausentes o aprobación/huellas inválidas: 1, sin HTTP objetivo |
| `cerrar` | Exactamente uno: `--corrida ID` o `--plan ARCHIVO`; ambos default `None` | Corrida: cierre histórico; plan: cero intentos. Ninguno/ambos: 2; fuente inexistente: 1 |
| `perfil` | Exactamente uno: `--archivo JSON/YAML` o `--entorno ID`; ambos default `None` | Archivo: registra/reemplaza ese ID; entorno: consulta sin escritura. Ninguno/ambos: 2; perfil inválido/ausente: 1 |

Solo `ejecutar` envía HTTP al objetivo y puede mutarlo; solo `disenar` contacta DeepSeek y consume créditos. Los demás trabajan localmente. Código 0 significa comando completado, **no** pruebas aprobadas; 1 es error operativo/entrada, 2 argparse, 130 interrupción. No hay `--dry-run`, `--reset`, `--home`, `--key` ni `--model`.

## 1. Terminal B: sandbox nuevo y ayudas

```bash
ROOT="$PWD"
command -v jq
FLOW="$(mktemp -d "${TMPDIR:-/tmp}/qa-cli-flow.XXXXXX")"
export QA_AGENT_HOME="$FLOW/home"
mkdir "$QA_AGENT_HOME"
cp "$ROOT/examples/cli-flow/plan.json" "$FLOW/plan.json"
cp "$ROOT/examples/cli-flow/local.json" "$FLOW/local.json"
qa() { "$ROOT/.venv/bin/python" -m qa_agent "$@"; }
printf 'export FLOW=%q\n' "$FLOW"  # copiar esta línea a terminal A
qa --help
for cmd in disenar generar aprobar ejecutar cerrar perfil; do qa "$cmd" --help; done
```

El plan manual `examples/cli-flow/plan.json` conserva los seis casos de `acceptance/manual_cases.json` en orden CP-02/01/03/04/05/06, junto a la huella del OpenAPI y los requisitos de la demo en `examples/cli-flow/todo-api/`. `skill_hashes` vacío identifica el origen manual. Los títulos límite están en los datos de CP-06.

## 2. Terminal A: instancia desechable

Desde la raíz, pega primero la línea `export FLOW=...` producida arriba:

```bash
ROOT="$PWD"
TODO_VARIANT=reference "$ROOT/.venv/bin/python" -m uvicorn \
  app.main:app --app-dir examples/cli-flow/todo-api \
  --host 127.0.0.1 --port 8000 --workers 1 2>&1 | tee "$FLOW/api.log"
```

Sin `--reload`, un trabajador. Puedes omitir este arranque si **tu propia** instancia reference en 8000 está fresca; usa su access log en la comprobación de cierre. Si el puerto está ocupado, no mates procesos ajenos. Reinicia únicamente tu instancia con Ctrl+C y el mismo comando antes de repetir una corrida.

Terminal B: comprobar semilla mediante **un GET explícito**, sin mutaciones:

```bash
curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:8000/tasks > "$FLOW/seed.json" &&
jq -e '. == [{"id":1,"title":"Preparar demo","completed":false},{"id":2,"title":"Revisar contrato","completed":true}]' "$FLOW/seed.json"
```

Debe salir `true` y código 0. El siguiente ID debe ser 3, garantizado por el reinicio sin tráfico mutante previo. **Solo tras comprobarlo**, certifica y registra:

```bash
jq '.preconditions.seed_ready = true' "$FLOW/local.json" > "$FLOW/ready.json"
qa perfil --archivo "$FLOW/ready.json"
qa perfil --entorno cli-flow-local
```

El fixture trae `seed_ready:false`; `true` es una declaración **del operador**, no una sonda ni un reset. Registrar no comprueba el servidor. La aprobación tampoco congela el perfil: revisa loopback/puerto/operaciones/versión antes de cada corrida. Reimportar el mismo ID sobrescribe su perfil local.

## 3. Generar, revisar y probar rechazos offline

```bash
qa cerrar --plan "$FLOW/plan.json" > "$FLOW/zero.json"
less "$FLOW/plan.cierre.md"  # cero intentos, Evidencia insuficiente
qa generar --plan "$FLOW/plan.json" --salida "$FLOW/suite" > "$FLOW/generated.json"
SUITE_ID="$(jq -er '.suite_id | select(type == "string" and startswith("suite-"))' "$FLOW/generated.json")"
jq . "$FLOW/generated.json"
```

Generar registra archivos pero **no aprueba, ejecuta ni instala**. Revisa plan, criterios, manifiesto, seis `test_*.py`, `conftest.py`, `_qa_runtime/`, `pytest.ini` y `requirements.lock`. CP-03 debe crear, capturar ID, PATCH y GET con igualdad booleana `true`.

Pruebas negativas: cada `echo` muestra el código esperado. Todas son locales, no necesitan clave ni producen tráfico al objetivo:

```bash
qa disenar; echo "rc=$?; esperado 2 (faltan requisitos/salida)"
qa generar --salida "$FLOW/unused"; echo "rc=$?; esperado 2"
qa aprobar --suite "$SUITE_ID" --responsable Revisor; echo "rc=$?; esperado 2"
qa aprobar --suite "$SUITE_ID" --confirmar; echo "rc=$?; esperado 2"
qa ejecutar --suite "$SUITE_ID"; echo "rc=$?; esperado 2"
qa cerrar; echo "rc=$?; esperado 2"
qa cerrar --plan "$FLOW/plan.json" --corrida inexistente; echo "rc=$?; esperado 2"
qa perfil; echo "rc=$?; esperado 2"
qa perfil --archivo "$FLOW/ready.json" --entorno cli-flow-local; echo "rc=$?; esperado 2"
qa ejecutar --suite "$SUITE_ID" --entorno cli-flow-local --dry-run; echo "rc=$?; esperado 2"
qa generar --plan "$FLOW/no-existe.json" --salida "$FLOW/unused"; echo "rc=$?; esperado 1"
printf '[]\n' > "$FLOW/invalid.json"
qa generar --plan "$FLOW/invalid.json" --salida "$FLOW/unused"; echo "rc=$?; esperado 1"
qa generar --plan "$FLOW/plan.json" --salida "$FLOW/suite"; echo "rc=$?; esperado 1"
qa perfil --archivo "$FLOW/invalid.json"; echo "rc=$?; esperado 1"
qa perfil --archivo "$FLOW/no-existe.json"; echo "rc=$?; esperado 1"
qa perfil --entorno inexistente; echo "rc=$?; esperado 1"
jq '.base_url = "http://192.0.2.1:8000"' "$FLOW/ready.json" > "$FLOW/denied.json"
qa perfil --archivo "$FLOW/denied.json"; echo "rc=$?; esperado 1 (no loopback)"
jq '.base_url = "http://127.0.0.1:8001"' "$FLOW/ready.json" > "$FLOW/denied.json"
qa perfil --archivo "$FLOW/denied.json"; echo "rc=$?; esperado 1 (puerto no permitido)"
qa aprobar --suite inexistente --responsable Revisor --confirmar; echo "rc=$?; esperado 1"
qa ejecutar --suite inexistente --entorno cli-flow-local; echo "rc=$?; esperado 1"
qa ejecutar --suite "$SUITE_ID" --entorno cli-flow-local; echo "rc=$?; esperado 1 (sin aprobación)"
qa cerrar --plan "$FLOW/no-existe.json"; echo "rc=$?; esperado 1"
qa cerrar --corrida inexistente; echo "rc=$?; esperado 1"
test ! -d "$QA_AGENT_HOME/runs" && echo 'Sin corridas ni intentos'
```

**Pausa humana.** Solo si has revisado todos los archivos y aceptas las mutaciones de esta suite:

```bash
printf 'Nombre del responsable humano: '; read -r RESPONSABLE
qa aprobar --suite "$FLOW/suite" --responsable "$RESPONSABLE" --confirmar
qa ejecutar --suite "$SUITE_ID" --entorno inexistente; echo "rc=$?; esperado 1 (perfil ausente)"
```

Prueba de hashes sin tráfico: alterar un script aprobado debe bloquear antes de MCP. Backup **fuera** de suite; restaura bytes exactos:

```bash
cp "$FLOW/suite/test_0000.py" "$FLOW/script.bak"
printf '\n# alteracion deliberada\n' >> "$FLOW/suite/test_0000.py"
qa ejecutar --suite "$SUITE_ID" --entorno cli-flow-local; echo "rc=$?; esperado 1"
cp "$FLOW/script.bak" "$FLOW/suite/test_0000.py"
test ! -d "$QA_AGENT_HOME/runs" && echo 'Sin corridas ni intentos'
```

También puedes repetir backup/edición/ejecución/restauración con el objetivo de CP-02 en `$FLOW/plan.json` o `created_at` de `suite/manifest.json`: esperado 1. Cambiar solo whitespace/orden de claves del **plan** no invalida su hash semántico; no es una prueba de alteración. Una edición definitiva exige regenerar en otro directorio y nueva aprobación, nunca editar registro/approval/hashes para autorizarla.

## 4. Ejecutar una sola vez, inspeccionar y cerrar

```bash
qa ejecutar --suite "$FLOW/suite" --entorno cli-flow-local > "$FLOW/execution.json"
RC=$?; echo "CLI rc=$RC"
# Continuar solo si RC=0 y el JSON es válido; no repetir automáticamente.
RUN_ID="$(jq -er '.run_id | select(type == "string" and test("^run-[a-f0-9]{32}$"))' "$FLOW/execution.json")"
jq -e 'has("status") and has("result_path") and (.evidence_paths | type == "array")' "$FLOW/execution.json"
RUN_DIR="$QA_AGENT_HOME/runs/$RUN_ID"
jq '{run_id,status,cases:[.cases[]|{case_id,state,attempted}]}' "$RUN_DIR/resultados.json"
jq '.cases[] | select(.case_id=="CP-03") | .steps[] | {operation_id,http_status,captures,assertions}' "$RUN_DIR/resultados.json"
```

JSON stdout real, no extracción desde logs. Esperado reference: `completed`, seis casos `passed`, 15 pasos HTTP, CP-03 captura ID y confirma PATCH/GET. `completed` por sí solo **no** acredita éxito. Evidencias incluyen eventos de inicio y terminal: no confundir cantidad de archivos con peticiones. Revisar rutas de `evidence_paths`, hashes y observaciones.

Antes de cerrar, nadie debe enviar más peticiones al target. Con el access log de terminal A:

```bash
HTTP_BEFORE="$(grep -Ec '"(GET|POST|PATCH|DELETE) ' "$FLOW/api.log")"
cp "$RUN_DIR/resultados.json" "$FLOW/results.before.json"
find "$RUN_DIR/evidence" -type f | sort > "$FLOW/evidence.before"
qa cerrar --corrida "$RUN_ID" > "$FLOW/closed.json"
REPORT="$(jq -er '.report_path' "$FLOW/closed.json")"
less "$REPORT"
qa cerrar --corrida "$RUN_ID"  # reconstrucción, sin IA ni HTTP
HTTP_AFTER="$(grep -Ec '"(GET|POST|PATCH|DELETE) ' "$FLOW/api.log")"
test "$HTTP_BEFORE" = "$HTTP_AFTER" && echo 'Cero nuevas peticiones'
cmp "$FLOW/results.before.json" "$RUN_DIR/resultados.json"
find "$RUN_DIR/evidence" -type f | sort > "$FLOW/evidence.after"
cmp "$FLOW/evidence.before" "$FLOW/evidence.after"
```

Usando servidor existente, sustituye la ruta del access log; sin acceso al log no declares comprobado el contador. También puedes apagar **tu** servidor antes del cierre: el informe sigue funcionando. Cerrar usa snapshot histórico, no reejecuta. No borres historial si hay fallos. Ante timeout/error tras intento, revisa evidencia y reinicia externamente antes de una nueva ejecución; no reintentes mutaciones a ciegas.

## 5. Camino IA separado, opcional y con coste

Solo para el operador que ya exportó `DEEPSEEK_API_KEY` mediante un mecanismo seguro; no imprimirla ni escribirla en historial, argumentos o fixtures. Cada invocación válida siguiente puede hacer **una llamada facturable**; ejecutar la segunda **en lugar de**, no además de, la primera si solo quieres diseño parcial:

```bash
qa disenar --requisitos "$ROOT/examples/todo-requisitos.md" \
  --contrato "$ROOT/examples/cli-flow/todo-api/openapi.json" \
  --datos "$ROOT/examples/datos-sinteticos.json" --salida "$FLOW/ai-plan.json"
# Alternativa parcial, omite ambos flags opcionales:
qa disenar --requisitos "$ROOT/examples/todo-requisitos.md" --salida "$FLOW/ai-partial.json"
```

Para aislar el efecto de cada opcional, retira solo `--datos` o solo `--contrato` del primer comando, con salida distinta: inspecciona preguntas, operaciones y `automation_pending_reason`; **no hace falta pagar estas variantes para validar el parser**. Con clave ya exportada, estos negativos se rechazan antes de invocar al proveedor: coste 0, código 1, sin plan nuevo (sin clave, el primer error será precisamente la clave ausente):

```bash
qa disenar --requisitos "$FLOW/no-existe.md" --salida "$FLOW/rejected.json"
qa disenar --requisitos "$ROOT/examples/todo-requisitos.md" --contrato "$FLOW/no-existe.json" --salida "$FLOW/rejected.json"
qa disenar --requisitos "$ROOT/examples/todo-requisitos.md" --datos "$FLOW/invalid.json" --salida "$FLOW/rejected.json"
```

Los tests con mocks validan estos rechazos sin red. Falta de clave: error de diseño, 1, sin archivo. No pongas clave solo para probar errores. No se garantiza disponibilidad/saldo/propuesta válida. Un plan IA exige revisión semántica propia; no reutilizar automáticamente la aprobación del fixture manual.

`DEEPSEEK_MODEL` (`deepseek-chat`), `DEEPSEEK_ENDPOINT` (endpoint HTTPS oficial), `QA_AGENT_HOME` (default `qa-agent/data`), `QA_ENTORNO` (pytest directo) y credenciales referenciadas en perfiles son **variables de entorno, no flags**. `.env` no se carga solo. La CLI usa `--entorno`, no `QA_ENTORNO`. Timeouts, destinos, operaciones, versión y semilla pertenecen al perfil.

## Clasificación para decidir, sin eliminar nada

| Grupo | Flags | Tradeoff / alternativa propuesta, no implementada |
|---|---|---|
| Core imprescindible | `generar --plan/--salida`; `aprobar --suite/--responsable/--confirmar`; `ejecutar --suite/--entorno`; `cerrar --corrida`; `perfil --archivo` | Preservan trazabilidad, aislamiento, destino y consentimiento. **No quitar aprobación ni confirmación** sin decisión explícita de producto |
| Útiles opcionales | Ayuda; `perfil --entorno`; `cerrar --plan`; `disenar --requisitos/--contrato/--salida` | Consulta, cierre sin ejecución y diseño asistido. Los flags de diseño requeridos siguen siendo obligatorios dentro de ese comando aunque IA sea opcional para el core |
| Candidato condicionado | `disenar --datos` | No se usa en el core manual, pero sí aporta límites/payloads sintéticos al diseñador. Mover datos a una configuración o requisitos reduciría un flag, perdiendo separación/reutilización y soporte JSON/YAML estructurado; **no es redundante** |
| Posible simplificación de uso, no eliminación segura | `--salida`, `--entorno` | Defaults/config reducirían tecleo, pero introducirían selección implícita, riesgo de sobreescritura o destino equivocado. Mejor mantener explícitos por ahora |

Ningún flag es eliminable sin tradeoff; retirar todo diseño IA sería recortar una capacidad, no limpiar flags redundantes.

### Evidencia offline y límites

Validación local: fixtures contra Pydantic/contrato público/fixture manual (mismo hash, seis casos/15 pasos), siete ayudas reales y bloques Bash con `bash -n`. Tests `test_cli.py`, `test_store.py`, `test_security.py`, `test_generate.py`, `test_report.py`, `test_acceptance_plan.py`: **200 passed**; selección de fuentes/datos inválidos en `test_design.py`: **9 passed** (mocks/archivos temporales; reportes prohíben clientes HTTP). No se ejecutó este flujo contra la API ni se contactó DeepSeek al preparar la guía. La prueba MCP real y las mutaciones descritas quedan para el operador autorizado.
