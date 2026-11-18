# Guía del agente QA y la API To-do

## 1. Dos proyectos, responsabilidades distintas

Guía práctica en español · Revisión documental: 18 de noviembre de 2026.

El objetivo es comprobar reglas de negocio por HTTP y conservar evidencia revisable. No se trata de que una IA declare que el producto funciona: la IA propone pruebas; las personas revisan y autorizan; el código compara respuestas y calcula resultados.

| Proyecto | Responsabilidad |
| --- | --- |
| qa-agent/ | Lee requisitos y OpenAPI local, propone un plan, genera pytest, exige aprobación, ejecuta por MCP stdio y produce evidencia y cierre. |
| examples/cli-flow/todo-api/ | Sirve la API sintética de tareas. Valida peticiones y mantiene estado en memoria. |
| Operador humano | Autoriza el servicio desechable, verifica la semilla y el destino, revisa el plan y los scripts y decide sobre la liberación. |

### El recorrido completo

```text
Requisitos + OpenAPI local + datos sintéticos
                  |
       DeepSeek propone plan -> revisión humana
                  |
       Generar suite -> revisar -> aprobar
                  |
       CLI -> MCP stdio -> pytest -> HTTP -> todo-api
                  |                          |
             Evidencia <---------------------+
                  |
       Cierre determinista -> decisión humana
```

MCP es el canal local entre procesos que descubre y llama la herramienta ejecutar_plan. No es el transporte de las tareas: las peticiones a /tasks viajan por HTTP desde el runtime de pruebas. La CLI arranca el servidor MCP stdio; no hace falta iniciarlo a mano.

### Qué utiliza IA y qué no

Solo disenar contacta DeepSeek para proponer un Plan JSON. El contexto incluye requisitos identificados, operaciones resueltas y datos sintéticos; no incluye perfiles ni todo el entorno. El diseño no llama a la API objetivo. Generar, aprobar, validar hashes, ejecutar comparadores, calcular métricas y cerrar son operaciones deterministas, sin IA.

No se consumió IA real en la validación disponible ni al crear esta guía: el usuario no autorizó ese consumo. Los tests de integración DeepSeek usan transportes simulados. El fixture de aceptación de seis casos es MANUAL, no una salida de IA.

### Cómo leer los ejemplos

Los comandos indican su directorio de trabajo. Se presupone la instalación descrita en ambos README y sus .venv existentes (Python 3.14.7 verificado). No mezcle el harness automático de la página 5 con el servidor manual de las páginas 6–7. Esta guía no ejecuta esos caminos de red: documenta cómo puede hacerlo un operador autorizado.

<!-- pagebreak -->

## 2. La API: contrato, memoria y defecto demostrable

Una Task contiene exactamente id (entero positivo), title (texto) y completed (booleano JSON). No hay base de datos, autenticación, interfaz gráfica ni Docker. Cada arranque restaura esta semilla y el próximo ID es 3:

```json
[
  {"id": 1, "title": "Preparar demo", "completed": false},
  {"id": 2, "title": "Revisar contrato", "completed": true}
]
```

Los IDs crecen durante la vida del proceso y no se reutilizan al borrar. Un reinicio pierde todos los cambios. Use un proceso, un worker y sin --reload: varios procesos no comparten esa memoria.

| Método y ruta | Operación | Resultado correcto |
| --- | --- | --- |
| POST /tasks | createTask | 201 y tarea nueva; recibe solo title. |
| GET /tasks | listTasks | 200 y lista por ID ascendente; filtro completed opcional. |
| GET /tasks/{id} | getTask | 200 y representación actual. |
| PATCH /tasks/{id} | updateTask | 200; cambia title, completed o ambos. |
| DELETE /tasks/{id} | deleteTask | 204 con cero bytes de cuerpo. |

### Validación observable

El título se recorta (trim de espacios externos) y debe medir de 1 a 80 caracteres. POST asigna completed=false. PATCH exige al menos un campo y conserva los omitidos. completed en el cuerpo acepta solo true/false JSON; en el filtro query acepta únicamente los textos exactos true o false. La ausencia del filtro devuelve todas las tareas; sin coincidencias devuelve [].

Campos desconocidos, null, tipos erróneos, título vacío tras trim, PATCH {} e ID no entero positivo producen 422. Una petición inválida no modifica estado ni consume IDs. Se valida antes de comprobar existencia: PATCH inválido de un ID positivo ausente devuelve 422, no 404. Una operación válida sobre un ID inexistente devuelve 404; tras borrar, GET/PATCH/DELETE devuelven 404.

```json
{"error":{"code":"TASK_NOT_FOUND"}}
{"error":{"code":"VALIDATION_ERROR"}}
```

Las líneas anteriores son los cuerpos de 404 y 422, respectivamente. GET /openapi.json expone el contrato; no es una sexta operación de negocio. No hay páginas Swagger/ReDoc publicadas.

### reference frente a lost_completion

Las variantes comparten contrato y semilla. TODO_VARIANT se lee al construir la aplicación; para cambiarla, detener y reiniciar. La comparación exige estados iniciales equivalentes y product_version correcto en el perfil.

En CP-03 se crea una tarea propia, se captura su ID, se hace PATCH con completed=true y luego GET del mismo ID. En reference ambos devuelven 200 y true. En lost_completion, PATCH devuelve 200/true, pero GET devuelve 200/false. Es un fallo LÓGICO de persistencia, no un error HTTP: comprobar solo el status dejaría pasar el defecto.

<!-- pagebreak -->

## 3. Planes multipaso, capturas y skills

Un plan vincula requisitos con casos, operaciones OpenAPI resueltas, hash del contrato, criterios de salida y preguntas pendientes. Cada caso declara objetivo, prioridad, obligatoriedad, precondiciones, datos, esperado y pasos. El orden importa: cada caso mutante crea su tarea; no se adivinan IDs existentes.

Fragmento de steps para revisar persistencia (no es un plan completo):

```json
[
  {
    "operation_id": "createTask",
    "body": {"title": "Ensayar"},
    "assertions": [
      {"kind": "status", "status": 201},
      {"kind": "present", "pointer": "/id"}
    ],
    "captures": {"task_id": "/id"}
  },
  {
    "operation_id": "updateTask",
    "path": {"id": {"var": "task_id"}},
    "body": {"completed": true},
    "assertions": [
      {"kind": "status", "status": 200},
      {"kind": "equal", "pointer": "/completed", "value": true}
    ]
  },
  {
    "operation_id": "getTask",
    "path": {"id": {"var": "task_id"}},
    "assertions": [
      {"kind": "status", "status": 200},
      {"kind": "equal", "pointer": "/completed", "value": true}
    ]
  }
]
```

Las capturas son locales al caso; {"var":"task_id"} conserva el tipo JSON y solo puede usarse en pasos posteriores. Las variables iniciales se declaran en case.data, no implícitamente en el perfil. JSON Pointer /id selecciona un campo; la raíz es "". body_present distingue no enviar cuerpo de enviar JSON null; al construir un paso se infiere si se proporcionó body.

El catálogo de asserts es cerrado: status exacto, present, equal JSON estricto y empty_body (cero bytes). true, 1 y 1.0 son distintos; el orden de listas importa. No hay eval ni comparador genérico de tipo/rango: la presencia de /id NO prueba entero positivo. Los negativos conservan deliberadamente sus payloads inválidos. Falta de datos, contrato, comparador o capacidad debe producir automation_pending_reason y preguntas, no éxitos ficticios.

### Skills versionadas del producto

En qa_agent/skills/, design.md guía la propuesta sustentada en fuentes; steering.md impone trazabilidad, tratamiento de entradas como datos y separación entre propuesta y evidencia; close.md fija la disciplina de cierre sin inventar observaciones. Sus hashes quedan asociados al plan. Son instrucciones versionadas, no permisos para ejecutar shell ni una aprobación humana. El schema valida estructura y referencias, no demuestra corrección semántica: revise cada expectativa contra su requisito.

<!-- pagebreak -->

## 4. Controles, evidencia y significado de los estados

### Antes de enviar HTTP

La suite debe estar registrada y aprobada explícitamente. La aprobación liga suite_id + plan_hash + manifest_hash. Se verifican manifiesto, scripts, configuración y plan fuente original; modificar o quitar el original bloquea nuevas ejecuciones. Cambios semánticos exigen regenerar en otro directorio y aprobar de nuevo; cambiar solo whitespace u orden de claves JSON no cambia el hash canónico.

El perfil autoriza IP loopback NUMÉRICA, host, puerto y operationId exactos: 127.0.0.1, no localhost ni DNS. El plan no elige el destino. La aprobación no congela perfiles; revise destino, versión, permisos y datos antes de cada corrida. Redirects y proxies del entorno están deshabilitados; hay límites de respuesta y timeouts por petición y proceso. No se reintentan mutaciones automáticamente: un timeout exige revisar evidencia y restaurar datos antes de repetir.

Las precondiciones son certificaciones declarativas del operador, no sondas HTTP ni shell. examples/local.json trae preconditions={} y NO certifica seed_ready. Los datos sintéticos de diseño tampoco lo certifican. Solo se marca true tras confirmar externamente la semilla requerida.

### Estados de caso: no todo es “pasó/falló”

| Estado | Significado |
| --- | --- |
| designed / generated | Diseñado o generado, todavía sin intento. |
| blocked | No cumple una puerta previa; cero intentos HTTP. |
| passed / failed | Aserciones observadas satisfechas / incumplidas. |
| technical_error | Hubo intento, pero un error técnico impidió evaluar normalmente. |
| inconclusive | No hay evidencia suficiente o quedó un intento interrumpido. |

El inicio se persiste antes del HTTP. No se inventa status si nunca hubo respuesta. La corrida tiene estado propio (pending/running/completed/interrupted/failed): “completed” no significa que todos los casos pasaron. Tampoco el exit 0 de la CLI MCP equivale a suite verde; consulte los resultados JSON.

### Historial y cierre

Conserve data/runs/RUN_ID/: plan.json, environment.json, resultados.json, integrity.json, evidence/, snapshot suite/ y cierre.md. Cada paso conserva trazabilidad, esperado/observado, request/response depurados y HTTP solo cuando existió. El cierre reconstruye desde el snapshot histórico, sin red ni IA; corrupción o piezas faltantes producen Evidencia insuficiente, no una reconstrucción imaginaria.

Avance = casos únicos iniciados / todos los planificados, incluidos pendientes. Aprobación evaluable = pasó / (pasó + falló); también se informa pasó / planificados. Técnicos e inconclusos iniciados no son evaluables; bloqueados no son intentos. Denominador cero: No aplica. Cobertura de diseño y ejecución se calcula por requisitos, no por cantidad de eventos.

Un fallo obligatorio alto o crítico demostrado exige Aplazar liberación. Brechas en criterios obligatorios exigen Evidencia insuficiente. Solo criterios satisfechos permiten recomendar liberación dentro del alcance probado; la decisión sigue siendo humana. Credenciales: el perfil referencia nombres de variables, nunca valores. La redacción no detecta todo secreto desconocido. Usuario, registro y filesystem son confiables: esto NO es sandbox de Python ni autorización para ejecutar suites ajenas sin revisión.

<!-- pagebreak -->

## 5. Demostración reproducible SIN IA

Este es el camino recomendado para comprender el sistema sin clave ni consumo de DeepSeek. Usa acceptance/manual_cases.json, un fixture de seis casos curado MANUALMENTE. No demuestra que una IA descubra el defecto ni se introduce la información de variantes en el diseñador.

### Primero: validar el fixture, sin red

Desde la raíz del clon, después de seguir la [instalación](../README.md#instalación):

```bash
.venv/bin/python -B scripts/validate_todo.py --plan-only
```

Esta opción valida el plan manual offline: no necesita servidor objetivo ni runtime en ejecución. No equivale a correr pruebas contra el producto.

### Después: aceptación real con instancias desechables

El siguiente comando sí autoriza peticiones HTTP LOCALES al producto. Ejecútelo solo con permiso para la demostración. Desde qa-agent/:

```bash
# El destino debe ser NUEVO; cambie el nombre en cada repetición.
.venv/bin/python -B scripts/validate_todo.py \
  --artifacts data/guia-aceptacion-01
```

El programa arranca y termina sus propias instancias desechables con el intérprete del agente, un worker y un puerto loopback libre. Reinicia cada instancia con la misma semilla, usa un registro temporal y copia los artefactos al destino solicitado. Gestiona la aprobación del fixture manual de aceptación.

Compara MCP stdio real con pytest directo, ejecuta CP-03 individual y controles de precondición ausente y conexión fallida. CP-02 se ejecuta primero para preservar la observación de la semilla. No reutilice un Store archivado como registro activo: puede contener paths absolutos temporales ya eliminados. Consulte las rutas relativas de summary.json o repita con un destino nuevo.

### Resultados esperados de la aceptación

Para la suite To-do completa, el programa verifica estos resultados:

| Variante | MCP stdio real | pytest directo |
| --- | --- | --- |
| reference | 6/6 passed | 6/6 passed |
| lost_completion | 5/6 passed; CP-03 failed | 5/6 passed; CP-03 failed |

En CP-03 falló la comparación del GET (false observado contra true esperado), no el status HTTP. Con precondición ausente hubo seis blocked y cero intentos; sin escucha hubo seis technical_error con intento, sin fabricar respuestas. El cierre de reference recomendó liberación en alcance; el de la variante, Aplazar liberación.

Al terminar, consulte `summary.json` en el directorio generado para localizar las corridas y su evidencia. El protocolo describe qué comprueba cada una. [Aceptación independiente](../acceptance/README.md)

<!-- pagebreak -->

## 6. Alternativa con IA: diseñar, revisar y aprobar

Este camino envía entradas sintéticas a DeepSeek y puede consumir créditos. Use solo requisitos, contratos y datos aptos para enviar al proveedor. La CLI lee la clave desde la terminal.

### Terminal A: servidor manual, solo para ejecutar más tarde

Desde la raíz del clon, mantenga el proceso en primer plano:

```bash
TODO_VARIANT=reference .venv/bin/python -B -m uvicorn \
  app.main:app --app-dir examples/cli-flow/todo-api \
  --host 127.0.0.1 --port 8000 --workers 1
```

Puede iniciarlo justo antes de ejecutar, en la página 7: diseñar usa el contrato local ya disponible y no necesita la API viva. Para detener use Ctrl+C. Nunca ejecute este servidor junto al camino del harness.

### Terminal B: clave y propuesta, desde qa-agent/

Exporte la clave desde una shell confiable sin escribir su valor en el comando ni guardarlo en el proyecto. Ejemplo bash con entrada oculta:

```bash
read -r -s -p 'Clave DeepSeek: ' DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
mkdir -p data/guia
export QA_AGENT_HOME="$PWD/data/guia/store"
.venv/bin/python -B -m qa_agent disenar \
  --requisitos examples/todo-requisitos.md \
  --contrato examples/cli-flow/todo-api/openapi.json \
  --datos examples/datos-sinteticos.json \
  --salida data/guia/plan.json
unset DEEPSEEK_API_KEY
```

El endpoint predeterminado es https://api.deepseek.com/chat/completions y el modelo deepseek-chat (configurable por DEEPSEEK_MODEL). No se admiten hosts de proveedor alternativos. El plan resultante es una propuesta: revise fuentes, orden, datos, precondiciones, negativos, límites, capturas, asserts, pendientes y criterios de salida. No espere necesariamente seis casos ni copie la tabla reservada de evaluación como entrada.

### Generar y aprobar, únicamente después de revisar

Desde qa-agent/, en la misma shell y con el mismo QA_AGENT_HOME:

```bash
.venv/bin/python -B -m qa_agent generar \
  --plan data/guia/plan.json --salida data/guia/suite
# DETENERSE: revisar plan, manifest, test_*.py, conftest,
# runtime, configuracion y requirements.lock de la suite.
# Solo si el revisor acepta todo lo anterior:
.venv/bin/python -B -m qa_agent aprobar \
  --suite data/guia/suite \
  --responsable 'Nombre del revisor' --confirmar
```

suite debe ser un directorio nuevo. Generar no instala paquetes, no envía HTTP ni aprueba. Si cambia el plan o los archivos, regenere en otro destino y vuelva a revisar/aprobar; conserve el plan fuente. No ejecute el bloque de aprobación a ciegas. La aprobación autoriza esa suite, no certifica la semilla ni libera el producto.

<!-- pagebreak -->

## 7. Perfil veraz, ejecución y cierre manual

Continúe desde qa-agent/ en la Terminal B con QA_AGENT_HOME de la página anterior. La Terminal A debe servir la variante reference en 127.0.0.1:8000. Antes de ejecutar, reinicie la API y confirme externamente los datos requeridos por el plan; una consulta autorizada puede verificar GET /tasks contra la semilla de la página 2. No certifique seed_ready solo por haber copiado datos sintéticos.

### Crear una COPIA del perfil, sin alterar el ejemplo original

El script pregunta si el operador ya confirmó la semilla externamente. Solo responde SI después de esa verificación; en otro caso aborta. Desde qa-agent/:

```bash
.venv/bin/python -B - <<'PY'
import json
from pathlib import Path
print('¿Semilla externa confirmada? Escriba SI:', flush=True)
with open('/dev/tty') as tty:  # terminal POSIX, no el heredoc
    ok = tty.readline().strip()
if ok != 'SI':
    raise SystemExit('Sin certificacion; no registrar perfil')
p = json.loads(Path('examples/local.json').read_text())
p['preconditions'] = {'seed_ready': True}
p['product_version'] = 'reference'
Path('data/guia').mkdir(parents=True, exist_ok=True)
with Path('data/guia/perfil.json').open('x') as f:
    json.dump(p, f, ensure_ascii=False, indent=2)
PY
.venv/bin/python -B -m qa_agent perfil \
  --archivo data/guia/perfil.json
.venv/bin/python -B -m qa_agent perfil --entorno local
```

El archivo nuevo conserva el ID local, puerto 8000 y allowlist de cinco operaciones del ejemplo. Si cambia el puerto del servidor, cambie también base_url y allowed_targets del perfil antes de registrarlo. Reimportar el mismo ID reemplaza su configuración; no prueba que la instancia esté lista. El perfil de To-do no necesita credenciales. En otra API, credentials mapearía headers a nombres de variables exportadas solo al ejecutor.

### Ejecutar por MCP y guardar el run_id devuelto

```bash
.venv/bin/python -B -m qa_agent ejecutar \
  --suite data/guia/suite --entorno local
# Sustituya RUN_ID por el valor real devuelto:
.venv/bin/python -B -m qa_agent cerrar --corrida RUN_ID
```

El cliente inicia MCP stdio, descubre ejecutar_plan, llama con IDs y pytest ejecuta el snapshot verificado. Los casos son secuenciales y cada caso se detiene al primer fallo. El cierre queda en $QA_AGENT_HOME/runs/RUN_ID/cierre.md y puede regenerarse con el servidor ya detenido, sin nuevas pruebas ni clave.

Alternativa pytest directo: después de REINICIAR la API y recertificar las precondiciones, ejecute QA_ENTORNO=local .venv/bin/python -B -m pytest -q data/guia/suite. Mantenga el mismo QA_AGENT_HOME. Crea una corrida propia con las mismas puertas y comparadores. Elegir un único test deja el resto pendiente, no produce métricas de suite completa.

Para lost_completion, detenga A y reinicie con TODO_VARIANT=lost_completion (mismo comando de arranque). Cree otra copia verificada del perfil con product_version=lost_completion y registre ese perfil; no modifique plan/suite para favorecer el resultado. Reinicie entre MCP y pytest y entre variantes. Compare estados y aserciones, no IDs de corrida ni duraciones.

<!-- pagebreak -->

## 8. Límites, checklist y fuentes

### Antes de confiar en un resultado

| Pregunta | Evidencia que debe buscar |
| --- | --- |
| ¿Qué se probó? | Requisito, caso, operationId, datos y aserciones en el plan. |
| ¿Contra qué instancia? | Perfil, puerto, product_version y semilla confirmada. |
| ¿Qué se autorizó? | Aprobación humana ligada a hashes y revisión del destino actual. |
| ¿Se ejecutó de verdad? | run_id, inicio persistido y evidencia por paso, no solo un diseño. |
| ¿Qué ocurrió? | esperado/observado, HTTP cuando exista y estado correcto del caso. |
| ¿Alcanza para liberar? | Criterios previos, pendientes, fallos y brechas del cierre. Decisión humana. |

El MVP está limitado a HTTP/JSON local, un entorno por corrida, sin navegador, correo, base de datos ni fuzzing general. No convierte Python arbitrario en código seguro. No instale ni ejecute suites de terceros sin revisión; pytest manual puede cargar configuración externa antes de validar. Para amenazas fuertes se necesita aislamiento del sistema operativo.

La evidencia disponible ejercitó macOS/POSIX y Python 3.14.7; no acredita todas las plataformas. Los tests simulados no prueban disponibilidad, coste ni calidad semántica de DeepSeek. Se verificó instalación limpia desde wheel en una comprobación adicional documentada, no una garantía para cualquier entorno. La retención y limpieza de historial son manuales: preserve evidencia ante incidencias.

### Fuentes del repositorio

- Instalación, CLI, aprobación y perfiles. [README](../README.md)
- Componentes y flujo MCP stdio/pytest. [Arquitectura](ARCHITECTURE.md)
- Plan, capturas, aserciones y persistencia. [Contrato de datos](API_CONTRACT.md)
- Entradas, comandos y controles de la demo. [Aceptación](../acceptance/README.md)
- Operaciones y reglas HTTP. [Requisitos](../examples/cli-flow/todo-api/docs/requirements.md) · [OpenAPI](../examples/cli-flow/todo-api/openapi.json)

### Este documento y su reproducción

La guía está escrita en Markdown. Puede generar una copia PDF local con las instrucciones del [script](../scripts/generate_guide_pdf.py).
