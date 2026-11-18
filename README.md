# Agente QA para APIs HTTP

Agente en Python para convertir requisitos en casos de prueba revisables, exportar una suite pytest, ejecutarla contra una API local y generar un informe de cierre con evidencia. Incluye una API To-do con datos sintéticos y un plan de seis casos para recorrer el flujo desde un clon limpio.

## Cómo funciona

| Etapa | Qué hace |
| --- | --- |
| `disenar` | Propone un plan a partir de requisitos y, opcionalmente, OpenAPI y datos sintéticos. Esta etapa puede usar DeepSeek. |
| `generar` | Convierte los casos automatizables en scripts pytest y registra un manifiesto con las huellas de los archivos. |
| `aprobar` | Registra la aprobación humana del plan y de la suite revisados. Si cambian, se debe regenerar y aprobar otra vez. |
| `perfil` | Registra el destino local permitido, las operaciones, la versión y las precondiciones de la prueba. |
| `ejecutar` | Llama a `ejecutar_plan` mediante MCP por stdio. El servidor verifica la aprobación y el perfil, y ejecuta pytest. |
| `cerrar` | Reconstruye el informe a partir de la corrida guardada. El informe conserva la trazabilidad, los resultados y la recomendación de liberación. |

## Instalación

Necesitas Python **3.14.7**, Git, Bash, `curl` y `jq`. Los comandos de la demostración están escritos para macOS o Linux.

```bash
git clone https://github.com/jdsalcedos/qa-agent.git
cd qa-agent
python3.14 --version
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test,demo]'
python -m qa_agent --help
```

El extra `demo` instala el servidor de la API de ejemplo. `python -m pytest -q` verifica el código del agente. El comando `qa-agent` también está disponible dentro del entorno virtual.

## Demostración completa con el plan incluido

Esta ruta parte de un plan manual y funciona sin cuenta ni clave de IA. Ejecuta cada bloque por separado. La API guarda las tareas en memoria y recupera la semilla cada vez que la reinicias.

### 1. Levantar la API en la primera terminal

Desde la raíz del clon, ejecuta lo siguiente y deja el proceso en primer plano.

```bash
TODO_VARIANT=reference .venv/bin/python -m uvicorn app.main:app \
  --app-dir examples/cli-flow/todo-api --host 127.0.0.1 --port 8000 --workers 1
```

### 2. Preparar el plan y comprobar los datos iniciales

En una segunda terminal, desde la misma raíz, activa el entorno y crea un directorio nuevo para esta práctica.

```bash
source .venv/bin/activate
mkdir -p data
FLOW="$(mktemp -d "$PWD/data/demo.XXXXXX")"
export QA_AGENT_HOME="$FLOW/store"
cp examples/cli-flow/plan.json "$FLOW/plan.json"

curl --noproxy '*' --fail --silent --show-error http://127.0.0.1:8000/tasks \
  | jq -e '. == [{"id":1,"title":"Preparar demo","completed":false},{"id":2,"title":"Revisar contrato","completed":true}]'
```

Continúa cuando el comando devuelva `true` y hayas confirmado que la API es tu instancia recién iniciada. Conserva el plan original en esa ruta mientras uses la suite.

### 3. Generar, revisar y aprobar la suite

```bash
python -m qa_agent generar --plan "$FLOW/plan.json" --salida "$FLOW/suite"
```

Antes de aprobar, revisa `plan.json`, `manifest.json`, `test_*.py`, `conftest.py`, `pytest.ini`, `requirements.lock` y `_qa_runtime/` dentro de la suite. El plan prueba el listado y su filtro, la creación, la actualización y la consulta posterior, la eliminación y la validación de títulos. La suite modifica únicamente los datos de la instancia desechable que acabas de iniciar.

Tras revisar los casos y aceptar esas operaciones, registra tu aprobación.

```bash
printf 'Nombre del responsable de la revisión: '
read -r RESPONSABLE
python -m qa_agent aprobar --suite "$FLOW/suite" --responsable "$RESPONSABLE" --confirmar
```

### 4. Registrar el perfil y ejecutar por MCP

`seed_ready` registra la verificación de la semilla que hiciste en el paso 2. El perfil de ejemplo limita la ejecución a cinco operaciones en `127.0.0.1:8000`.

```bash
jq '.preconditions.seed_ready = true' examples/cli-flow/local.json > "$FLOW/perfil.json"
python -m qa_agent perfil --archivo "$FLOW/perfil.json"
python -m qa_agent perfil --entorno cli-flow-local
python -m qa_agent ejecutar --suite "$FLOW/suite" --entorno cli-flow-local > "$FLOW/ejecucion.json"
```

El JSON de ejecución contiene `run_id`, `status`, `result_path` y `evidence_paths`. Con la API de referencia recién iniciada se esperan seis casos `passed`. Revisa el estado de cada caso, ya que `completed` indica que terminó la corrida y también puede aparecer si hubo aserciones fallidas.

```bash
RUN_ID="$(jq -er '.run_id' "$FLOW/ejecucion.json")"
RUN_DIR="$QA_AGENT_HOME/runs/$RUN_ID"
jq '{status, casos: [.cases[] | {case_id, state, attempted}]}' "$RUN_DIR/resultados.json"
```

### 5. Generar y leer el cierre

```bash
python -m qa_agent cerrar --corrida "$RUN_ID"
cat "$RUN_DIR/cierre.md"
```

El directorio de la corrida contiene el plan y la suite usados, el perfil de la ejecución, `resultados.json`, `integrity.json`, `evidence/` y `cierre.md`. El cierre puede reconstruirse con la API apagada. Para repetir la demo, reinicia tu instancia para recuperar la semilla y usa un directorio de trabajo nuevo.

## Diseñar un plan con DeepSeek

Para probar también el diseño, prepara el directorio del paso 2 y ejecuta este bloque **antes de generar la suite**. Escribe la clave cuando la terminal la solicite. El comando envía a DeepSeek los requisitos, el contrato y los datos sintéticos del ejemplo y puede generar un cargo en tu cuenta.

```bash
read -r -s -p 'Clave de DeepSeek: ' DEEPSEEK_API_KEY
printf '\n'
export DEEPSEEK_API_KEY
python -m qa_agent disenar \
  --requisitos examples/todo-requisitos.md \
  --contrato examples/cli-flow/todo-api/openapi.json \
  --datos examples/datos-sinteticos.json \
  --salida "$FLOW/plan-ia.json"
unset DEEPSEEK_API_KEY
```

Si el diseño terminó correctamente, revisa los requisitos cubiertos, el orden de los casos, las operaciones, las aserciones y las preguntas pendientes. En el paso 3 usa `--plan "$FLOW/plan-ia.json"` y un directorio de suite nuevo si ya generaste otra. El diseño es una propuesta y requiere su propia revisión y aprobación. La cantidad de casos y el resultado pueden diferir del plan manual.

Para otra API de pruebas local, usa tus requisitos Markdown con identificadores como `R-01`, su OpenAPI JSON y un perfil ajustado al destino, operaciones y datos de esa instancia. Pasa credenciales al ejecutor mediante variables de entorno locales referenciadas por el perfil. Mantén las claves, perfiles privados y artefactos de ejecución fuera del repositorio. La CLI lee las variables exportadas en la terminal.

## Pruebas y documentación

- `python -m pytest -q` ejecuta las pruebas del agente. El diseño con IA se verifica con respuestas simuladas.
- `python scripts/validate_todo.py --plan-only` valida el plan manual localmente.
- `python scripts/validate_todo.py --artifacts data/aceptacion-01` ejecuta la aceptación automatizada del ejemplo en instancias desechables. Usa un directorio de artefactos nuevo para cada corrida.
- Los contratos documentan los modelos, la aprobación, las evidencias y el informe. [Arquitectura](docs/ARCHITECTURE.md) · [Contrato de datos](docs/API_CONTRACT.md) · [Contrato de ejecución](docs/RUNTIME_CONTRACT.md)
