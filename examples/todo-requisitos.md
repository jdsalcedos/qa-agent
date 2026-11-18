# Requisitos observables To-do — entrada del diseñador

Fuente: docs/SPEC_API_Todo_List.md §2. Solo reglas públicas; no incluye la tabla del evaluador ni información de variantes.

| ID | Comportamiento exigido |
|---|---|
| TD-01 | Al crear una tarea, se asigna un ID entero positivo y completed toma el valor false. El título se guarda sin espacios al inicio ni al final. |
| TD-02 | La lista devuelve todas las tareas en orden de ID ascendente. Cuando se indica completed, devuelve solo las que coinciden. Sin coincidencias devuelve una lista vacía. |
| TD-03 | Consultar una tarea existente devuelve su representación actual. Consultar un ID positivo inexistente devuelve 404. |
| TD-04 | Actualizar completed o title conserva los cambios para consultas siguientes. Los campos omitidos mantienen su valor. |
| TD-05 | Al eliminar una tarea existente se recibe 204 sin cuerpo. Desde ese momento, consultarla, actualizarla o eliminarla de nuevo devuelve 404. |
| TD-06 | Un título debe ser texto de 1 a 80 caracteres tras recortar espacios. La API rechaza los cuerpos inválidos con 422 y conserva el estado previo. |
