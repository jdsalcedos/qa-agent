"""Request and response schemas for the public task contract."""

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema

Title = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=80),
    Field(description="Texto de 1 a 80 caracteres tras recortar espacios externos."),
]


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: Annotated[int, Field(gt=0)]
    title: Title
    completed: StrictBool


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    # None represents omission internally, never an accepted JSON value. Hide that
    # sentinel (and its default) from the public request schema.
    title: Title | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=lambda schema: schema.pop("default", None)
    )
    completed: StrictBool | SkipJsonSchema[None] = Field(
        default=None, json_schema_extra=lambda schema: schema.pop("default", None)
    )

    @field_validator("title", "completed", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Los campos presentes no admiten null")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("PATCH requiere al menos un campo")
        return self


class ValidationErrorCode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["VALIDATION_ERROR"]


class ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ValidationErrorCode


class NotFoundErrorCode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["TASK_NOT_FOUND"]


class NotFoundErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: NotFoundErrorCode
