# Contrato público · To-do List API 1.0

API local en `http://127.0.0.1:8000`, sin autenticación y con datos sintéticos.
El contrato de máquina se obtiene mediante `GET /openapi.json`. No incluye
interfaz gráfica ni almacenamiento persistente entre reinicios.

## Estado inicial y aislamiento

Cada inicio restaura exactamente estas tareas:

```json
[
  {"id": 1, "title": "Preparar demo", "completed": false},
  {"id": 2, "title": "Revisar contrato", "completed": true}
]
```

El siguiente ID es 3. Los IDs son enteros positivos, crecen durante la vida de
la instancia y no se reutilizan al eliminar tareas. Ejecutar las pruebas
secuencialmente. Cada escenario que modifique datos debe crear su propia tarea,
capturar el ID devuelto y usarlo en sus siguientes peticiones.

## Requisitos observables

| ID | Comportamiento exigido |
|---|---|
| TD-01 | Crear asigna un ID entero positivo, establece completed=false y guarda el título sin espacios externos. |
| TD-02 | Listar devuelve todas las tareas ordenadas por ID ascendente. El filtro completed devuelve solo coincidencias; sin coincidencias devuelve []. |
| TD-03 | Consultar una tarea existente devuelve su representación actual. Un ID positivo inexistente produce 404. |
| TD-04 | Actualizar completed o title conserva los cambios para consultas siguientes. Los campos omitidos mantienen su valor. |
| TD-05 | Eliminar una tarea existente devuelve 204 sin cuerpo. Consultarla, actualizarla o eliminarla nuevamente produce 404. |
| TD-06 | El título debe ser texto de 1 a 80 caracteres después de recortar espacios. Un cuerpo inválido produce 422 y conserva el estado previo. |

## Operaciones HTTP

Toda representación contiene exactamente `id` (entero positivo), `title`
(texto) y `completed` (booleano JSON). POST y PATCH reciben objetos JSON con
`Content-Type: application/json`.

| Método y ruta | operationId | Entrada | Éxito | Errores |
|---|---|---|---|---|
| POST /tasks | createTask | Objeto con title obligatorio, único campo permitido | 201, tarea creada | 422 |
| GET /tasks | listTasks | Query completed opcional: texto literal true o false | 200, array de tareas | 422 |
| GET /tasks/{id} | getTask | ID entero positivo | 200, tarea actual | 404, 422 |
| PATCH /tasks/{id} | updateTask | title, completed o ambos; al menos uno | 200, tarea actualizada | 404, 422 |
| DELETE /tasks/{id} | deleteTask | ID entero positivo | 204, cero bytes de cuerpo | 404, 422 |

## Validación y errores

- No se admiten campos desconocidos, valores `null`, tipos incorrectos ni PATCH `{}`.
- `title` debe ser una cadena JSON; se recortan espacios antes de medir de 1 a
  80 caracteres. Una cadena de solo espacios es inválida.
- En PATCH, `completed` solo admite los booleanos JSON `true` y `false`, no
  cadenas ni números.
- En la query, `completed` solo admite los textos exactos `true` y `false`,
  sensibles a mayúsculas. Por ejemplo, `True`, `1`, `yes` y el valor vacío son
  inválidos. Omitir el parámetro significa no aplicar filtro.
- Los IDs deben tener representación entera positiva; un ID mal formado,
  cero o negativo produce 422.
- Primero se valida toda la entrada y luego se comprueba la existencia. Así,
  un PATCH inválido sobre un ID positivo inexistente produce 422, no 404.
- Una petición rechazada no crea tareas, consume IDs ni modifica datos.

Error 404:

```json
{"error":{"code":"TASK_NOT_FOUND"}}
```

Error 422, incluidos errores de validación del framework y JSON mal formado:

```json
{"error":{"code":"VALIDATION_ERROR"}}
```

## Ejemplo de uso

Crear mediante `POST /tasks` con `{"title":"  Ensayar  "}` devuelve 201 y una
tarea con título `Ensayar` y `completed=false`. Guardar el `id` recibido.
Enviar `PATCH /tasks/{id}` con `{"completed":true}` debe conservar el título
y el cambio de estado. Consultar `GET /tasks/{id}` verifica su persistencia.
Tras `DELETE /tasks/{id}`, la respuesta es 204 vacía y las operaciones posteriores
sobre ese ID devuelven 404. Este ejemplo usa el ID capturado, no uno fijo.
