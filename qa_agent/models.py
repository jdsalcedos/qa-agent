"""Versioned, data-only contracts shared by design, generation and execution."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_assignment=True)


class Priority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class CaseState(StrEnum):
    DESIGNED = "designed"
    GENERATED = "generated"
    BLOCKED = "blocked"
    PASSED = "passed"
    FAILED = "failed"
    TECHNICAL_ERROR = "technical_error"
    INCONCLUSIVE = "inconclusive"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


def validate_pointer(pointer: str) -> str:
    if pointer != "" and not pointer.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with /")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid JSON Pointer escape")
    return pointer


def json_pointer(document: JsonValue, pointer: str) -> JsonValue:
    """Resolve RFC 6901 pointer; missing differs from a JSON null (KeyError)."""
    validate_pointer(pointer)
    current = document
    if not pointer:
        return current
    for part in pointer[1:].split("/"):
        token = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise KeyError(pointer)
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token) or int(token) >= len(current):
                raise KeyError(pointer)
            current = current[int(token)]
        else:
            raise KeyError(pointer)
    return current


def strict_json_equal(left: JsonValue, right: JsonValue) -> bool:
    """No coercion: true != 1 and 1 != 1.0. Lists are ordered."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_json_equal(v, right[k]) for k, v in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def variable_names(value: JsonValue) -> set[str]:
    if isinstance(value, dict):
        if set(value) == {"var"}:
            name = value["var"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Invalid variable reference")
            return {name}
        return set().union(*(variable_names(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(variable_names(v) for v in value))
    return set()


def substitute_variables(value: JsonValue, variables: dict[str, JsonValue]) -> JsonValue:
    """Deep-copy substitution preserving types; undefined variables raise KeyError."""
    if isinstance(value, dict):
        if set(value) == {"var"}:
            variable_names(value)
            return deepcopy(variables[value["var"]])
        return {k: substitute_variables(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_variables(v, variables) for v in value]
    return value


class StatusAssertion(Model):
    kind: Literal["status"] = "status"
    status: Annotated[StrictInt, Field(ge=100, le=599)]


class PresentAssertion(Model):
    kind: Literal["present"] = "present"
    pointer: str

    _pointer = field_validator("pointer")(validate_pointer)


class EqualAssertion(Model):
    kind: Literal["equal"] = "equal"
    pointer: str
    value: JsonValue  # Required, including when the expected value is null.

    _pointer = field_validator("pointer")(validate_pointer)


class EmptyBodyAssertion(Model):
    kind: Literal["empty_body"] = "empty_body"


Assertion = Annotated[StatusAssertion | PresentAssertion | EqualAssertion | EmptyBodyAssertion, Field(discriminator="kind")]


def normalize_assertion(value: Any) -> Any:
    if not isinstance(value, dict) or "kind" in value:
        return value
    if set(value) == {"status"}:
        return {"kind": "status", **value}
    if set(value) == {"presente"}:
        return {"kind": "present", "pointer": value["presente"]}
    if set(value) == {"igual", "valor"}:
        return {"kind": "equal", "pointer": value["igual"], "value": value["valor"]}
    if set(value) == {"cuerpo_vacio"} and value["cuerpo_vacio"] is True:
        return {"kind": "empty_body"}
    return value


class Step(Model):
    operation_id: NonEmpty
    path: dict[str, JsonValue] = Field(default_factory=dict)
    query: dict[str, JsonValue] = Field(default_factory=dict)
    body: JsonValue = None
    body_present: StrictBool = False
    assertions: list[Assertion] = Field(min_length=1)
    captures: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for old, new in (("operacion", "operation_id"), ("assert", "assertions"), ("capturar", "captures")):
            if old in value and new not in value:
                value[new] = value.pop(old)
        if "body_present" not in value:
            value["body_present"] = "body" in value
        if "assertions" in value:
            value["assertions"] = [normalize_assertion(a) for a in value["assertions"]]
        return value

    @model_validator(mode="after")
    def consistent_body(self) -> Step:
        if not self.body_present and self.body is not None:
            raise ValueError("Non-null body requires body_present")
        return self

    @field_validator("captures")
    @classmethod
    def valid_captures(cls, value: dict[str, str]) -> dict[str, str]:
        for name, pointer in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Invalid capture name")
            validate_pointer(pointer)
        return value


class Requirement(Model):
    id: Identifier
    text: NonEmpty


class Parameter(Model):
    name: NonEmpty
    location: Literal["path", "query"]
    required: StrictBool = False
    schema_: dict[str, JsonValue] = Field(default_factory=dict, alias="schema")
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Operation(Model):
    operation_id: NonEmpty
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"]
    path: NonEmpty
    parameters: list[Parameter] = Field(default_factory=list)
    request_body: dict[str, JsonValue] | None = None
    responses: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_shape(self) -> Operation:
        if not self.path.startswith("/") or self.path.startswith("//") or any(c in self.path for c in ("?", "#", "\\", "%")):
            raise ValueError("Operation must have an unencoded absolute HTTP path")
        if any(p in {".", ".."} for p in self.path.split("/")):
            raise ValueError("Dot segments are forbidden")
        pairs = [(p.location, p.name) for p in self.parameters]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Ambiguous operation parameters")
        route_without_placeholders = re.sub(r"\{[^{}]+\}", "", self.path)
        if "{" in route_without_placeholders or "}" in route_without_placeholders:
            raise ValueError("Malformed path placeholders")
        placeholders = set(re.findall(r"\{([^{}]+)\}", self.path))
        if placeholders != {p.name for p in self.parameters if p.location == "path"}:
            raise ValueError("Path placeholders must match path parameters")
        if any(not p.required for p in self.parameters if p.location == "path"):
            raise ValueError("Path parameters must be required")
        return self


class Case(Model):
    id: Identifier
    requirement_ids: list[Identifier] = Field(min_length=1)
    objective: NonEmpty
    priority: Priority = Priority.HIGH
    mandatory: StrictBool = True
    preconditions: list[NonEmpty] = Field(default_factory=list)
    data: dict[str, JsonValue] = Field(default_factory=dict)
    expected: NonEmpty
    steps: list[Step] = Field(default_factory=list)
    automation_pending_reason: str | None = None

    @model_validator(mode="before")
    @classmethod
    def aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for old, new in (("objetivo", "objective"), ("prioridad", "priority"), ("obligatorio", "mandatory"), ("precondiciones", "preconditions"), ("datos", "data"), ("esperado", "expected"), ("pasos", "steps")):
            if old in value and new not in value:
                value[new] = value.pop(old)
        if "requisito" in value and "requirement_ids" not in value:
            value["requirement_ids"] = [value.pop("requisito")]
        if "priority" in value:
            value["priority"] = {"alta": "high", "media": "medium", "baja": "low"}.get(value["priority"], value["priority"])
        return value

    @model_validator(mode="after")
    def executable_shape(self) -> Case:
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("Duplicate requirement reference")
        if not self.automation_pending_reason and not self.steps:
            raise ValueError("Executable cases require steps; otherwise provide automation_pending_reason")
        known = set(self.data)
        for step in self.steps:
            refs = variable_names(step.path) | variable_names(step.query) | variable_names(step.body)
            for assertion in step.assertions:
                if isinstance(assertion, EqualAssertion):
                    refs |= variable_names(assertion.value)
            if refs - known:
                raise ValueError(f"Undefined/forward variables: {sorted(refs - known)}")
            if known & set(step.captures):
                raise ValueError("Capture names must not collide with data or earlier captures")
            known.update(step.captures)
        return self

    @property
    def executable(self) -> bool:
        return bool(self.steps) and not self.automation_pending_reason


class ExitCriteria(Model):
    mandatory_high_must_pass: StrictBool = True
    verdicts_require_evidence: StrictBool = True
    no_open_critical_defects: StrictBool = True


class Plan(Model):
    version: Literal["1.0"] = "1.0"
    requirements: list[Requirement] = Field(default_factory=list)
    contract_hash: Digest | None = None
    operations: list[Operation] = Field(default_factory=list)
    cases: list[Case] = Field(default_factory=list)
    exit_criteria: ExitCriteria = Field(default_factory=ExitCriteria)
    pending_questions: list[str] = Field(default_factory=list)
    skill_hashes: dict[str, Digest] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references(self) -> Plan:
        for values in ([r.id for r in self.requirements], [c.id for c in self.cases], [o.operation_id for o in self.operations]):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate IDs")
        requirements = {r.id for r in self.requirements}
        operations = {o.operation_id: o for o in self.operations}
        for case in self.cases:
            if not set(case.requirement_ids) <= requirements:
                raise ValueError("Unknown requirement")
            if not case.executable:
                continue
            if self.contract_hash is None:
                raise ValueError("Executable plan requires contract_hash")
            for step in case.steps:
                if step.operation_id not in operations:
                    raise ValueError("Unknown operation")
                op = operations[step.operation_id]
                for location, values in (("path", step.path), ("query", step.query)):
                    params = {p.name: p for p in op.parameters if p.location == location}
                    if not set(values) <= params.keys():
                        raise ValueError(f"Unknown {location} parameter")
                    if location == "path" and not params.keys() <= values.keys():
                        raise ValueError("Missing path parameter")
                if step.body_present and op.request_body is None:
                    raise ValueError("Operation has no JSON request body")
        return self


# Drafts have the same guarantees; unsupported work must be marked pending explicitly.
PlanDraft = Plan


class AllowedTarget(Model):
    host: NonEmpty
    port: Annotated[StrictInt, Field(ge=1, le=65535)]
    ips: list[NonEmpty] = Field(min_length=1)


class EnvironmentProfile(Model):
    id: Identifier
    base_url: NonEmpty
    allowed_targets: list[AllowedTarget] = Field(min_length=1)
    allowed_operations: list[NonEmpty] = Field(min_length=1)
    product_version: NonEmpty
    data: dict[str, JsonValue] = Field(default_factory=dict)
    preconditions: dict[str, StrictBool] = Field(default_factory=dict)
    credentials: dict[str, NonEmpty] = Field(default_factory=dict)  # header -> environment variable NAME
    request_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 10.0
    process_timeout_seconds: Annotated[float, Field(gt=0, le=3600)] = 120.0
    max_response_bytes: Annotated[StrictInt, Field(gt=0, le=10_000_000)] = 1_000_000


class SuiteCase(Model):
    case_id: Identifier
    requirement_ids: list[Identifier]
    script: NonEmpty
    script_hash: Digest


class SuiteManifest(Model):
    version: Literal["1.0"] = "1.0"
    suite_id: Identifier
    plan_hash: Digest
    source_plan_path: NonEmpty
    cases: list[SuiteCase] = Field(default_factory=list)
    files: dict[str, Digest] = Field(default_factory=dict)  # all files except manifest.json
    created_at: datetime = Field(default_factory=utc_now)


class Approval(Model):
    suite_id: Identifier
    plan_hash: Digest
    manifest_hash: Digest
    approved_by: NonEmpty
    approved_at: datetime = Field(default_factory=utc_now)


class AssertionResult(Model):
    kind: Literal["status", "present", "equal", "empty_body"]
    passed: StrictBool
    expected: JsonValue = None
    observed: JsonValue = None
    message: str | None = None


class StepEvidence(Model):
    run_id: Identifier
    case_id: Identifier
    requirement_ids: list[Identifier]
    script: NonEmpty
    script_hash: Digest
    plan_hash: Digest
    product_version: NonEmpty
    step_index: Annotated[StrictInt, Field(ge=0)]
    operation_id: NonEmpty
    started_at: datetime = Field(default_factory=utc_now)
    duration_ms: Annotated[float, Field(ge=0)] = 0.0
    state: CaseState
    http_status: Annotated[StrictInt, Field(ge=100, le=599)] | None = None
    request: dict[str, JsonValue] = Field(default_factory=dict)
    response: dict[str, JsonValue] = Field(default_factory=dict)
    expected: JsonValue = None
    observed: JsonValue = None
    assertions: list[AssertionResult] = Field(default_factory=list)
    captures: dict[str, JsonValue] = Field(default_factory=dict)
    error: str | None = None


class CaseResult(Model):
    case_id: Identifier
    requirement_ids: list[Identifier]
    script: str | None = None
    script_hash: Digest | None = None
    state: CaseState = CaseState.GENERATED
    attempted: StrictBool = False
    steps: list[StepEvidence] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)
    reason: str | None = None
    critical_defect: StrictBool = False

    @model_validator(mode="after")
    def attempt_state(self) -> CaseResult:
        if self.state in {CaseState.PASSED, CaseState.FAILED, CaseState.TECHNICAL_ERROR, CaseState.INCONCLUSIVE} and not self.attempted:
            raise ValueError("Observed states require an attempt")
        if self.state in {CaseState.DESIGNED, CaseState.GENERATED, CaseState.BLOCKED} and self.attempted:
            raise ValueError("Pending/blocked cases cannot have an attempt")
        return self


class RunResult(Model):
    run_id: Identifier
    suite_id: Identifier
    plan_hash: Digest
    environment_id: Identifier
    product_version: NonEmpty
    status: RunStatus = RunStatus.PENDING
    cases: list[CaseResult] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    result_path: str | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    error: str | None = None
