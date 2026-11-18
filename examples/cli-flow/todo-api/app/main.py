"""In-memory task API. Run with ``uvicorn app.main:app --workers 1``."""

import os
import re
from typing import Annotated, Literal

from fastapi import FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BeforeValidator
from pydantic.json_schema import SkipJsonSchema

from app.models import (
    NotFoundErrorResponse,
    Task,
    TaskCreate,
    TaskUpdate,
    ValidationErrorResponse,
)


class TaskNotFound(Exception):
    """A validated positive identifier is absent from this instance."""


def parse_id(value: object) -> int:
    # Pydantic's default int coercion also accepts strings like '1.0'. Paths
    # instead require an integer spelling, while OpenAPI remains type integer.
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("El ID debe ser un entero positivo")
    return int(value)


TaskId = Annotated[int, Path(gt=0), BeforeValidator(parse_id)]
CompletedFilter = Annotated[
    Literal["true", "false"] | SkipJsonSchema[None],
    Query(description="Solo se admiten los textos literales true y false."),
]

VALIDATION_RESPONSE = {
    422: {"model": ValidationErrorResponse, "description": "Entrada inválida"}
}
ITEM_RESPONSES = {
    **VALIDATION_RESPONSE,
    404: {"model": NotFoundErrorResponse, "description": "Tarea inexistente"},
}


def create_app(variant: str | None = None) -> FastAPI:
    """Build an independently seeded app; resolve its profile only at startup."""
    selected = os.environ.get("TODO_VARIANT", "reference") if variant is None else variant
    if selected not in ("reference", "lost_completion"):
        raise ValueError(
            f"TODO_VARIANT inválido: {selected!r}. "
            "Valores admitidos: reference, lost_completion"
        )

    application = FastAPI(
        title="To-do List API",
        version="1.0.0",
        description="API local de tareas. Contrato TD-01 a TD-06.",
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    tasks = {
        1: Task(id=1, title="Preparar demo", completed=False),
        2: Task(id=2, title="Revisar contrato", completed=True),
    }
    next_id = 3

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR"}})

    @application.exception_handler(TaskNotFound)
    async def task_not_found(request: Request, exc: TaskNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": "TASK_NOT_FOUND"}})

    def require_task(task_id: int) -> Task:
        try:
            return tasks[task_id]
        except KeyError:
            raise TaskNotFound from None

    @application.post(
        "/tasks",
        operation_id="createTask",
        status_code=201,
        response_model=Task,
        responses=VALIDATION_RESPONSE,
    )
    async def create_task(body: TaskCreate) -> Task:
        nonlocal next_id
        task = Task(id=next_id, title=body.title, completed=False)
        tasks[next_id] = task
        next_id += 1
        return task

    @application.get(
        "/tasks",
        operation_id="listTasks",
        response_model=list[Task],
        responses=VALIDATION_RESPONSE,
    )
    async def list_tasks(completed: CompletedFilter = None) -> list[Task]:
        return [
            task
            for _, task in sorted(tasks.items())
            if completed is None or task.completed == (completed == "true")
        ]

    @application.get(
        "/tasks/{id}",
        operation_id="getTask",
        response_model=Task,
        responses=ITEM_RESPONSES,
    )
    async def get_task(id: TaskId) -> Task:
        return require_task(id)

    @application.patch(
        "/tasks/{id}",
        operation_id="updateTask",
        response_model=Task,
        responses=ITEM_RESPONSES,
    )
    async def update_task(id: TaskId, body: TaskUpdate) -> Task:
        previous = require_task(id)
        updated = previous.model_copy(update=body.model_dump(exclude_unset=True))
        if selected == "lost_completion" and not previous.completed and updated.completed:
            # Preserve all other accepted changes, but not this transition.
            tasks[id] = updated.model_copy(update={"completed": False})
        else:
            tasks[id] = updated
        return updated

    @application.delete(
        "/tasks/{id}",
        operation_id="deleteTask",
        status_code=204,
        response_class=Response,
        responses=ITEM_RESPONSES,
    )
    async def delete_task(id: TaskId) -> Response:
        require_task(id)
        del tasks[id]
        return Response(status_code=204)

    return application


app = create_app()
