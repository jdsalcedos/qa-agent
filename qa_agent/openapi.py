"""Offline OpenAPI JSON projection. Missing/ambiguous information is explicit.

Only local #/components/schemas/... references are expanded. No resolver here
can make an HTTP request, read another document or evaluate schema code.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import re
from typing import Any

from pydantic import Field, ValidationError

from .models import Model, Operation, Parameter, Plan, json_pointer
from .store import hash_json, read_json


class OpenAPIContract(Model):
    contract_hash: str
    operations: list[Operation] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)

    def operation(self, operation_id: str) -> Operation:
        for operation in self.operations:
            if operation.operation_id == operation_id:
                return operation
        raise KeyError(operation_id)


class ContractIssue(ValueError):
    pass


def resolve_local_refs(value: Any, document: dict, stack: tuple[str, ...] = ()) -> Any:
    """Expand structural references, never interpret application/example data."""
    context = "schema"
    if isinstance(value, dict):
        if "openapi" in value:
            context = "document"
        elif "operationId" in value:
            context = "operation"
    elif isinstance(value, list):
        context = "parameter_array"
    return _resolve_refs(value, document, stack, context)


def _resolve_refs(value: Any, document: dict, stack: tuple[str, ...], context: str) -> Any:
    if len(stack) > 64:
        raise ContractIssue("Local reference chain exceeds the supported depth")
    maps = {"schema_map": "schema", "response_map": "response", "media_map": "media",
            "example_map": "example", "header_map": "parameter", "parameter_map": "parameter",
            "request_body_map": "request_body", "path_map": "path_item"}
    if context in maps:
        if not isinstance(value, dict):
            return deepcopy(value)
        # Map keys (including the literal name '$ref') are names, not keywords.
        return {name: _resolve_refs(item, document, stack, maps[context]) for name, item in value.items()}
    if isinstance(value, list):
        child = "parameter" if context == "parameter_array" else "schema"
        return [_resolve_refs(item, document, stack, child) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        reference = value["$ref"]
        if not isinstance(reference, str) or not reference.startswith("#/components/schemas/"):
            raise ContractIssue("External or unsupported reference; provide a local components/schemas reference")
        if len(value) != 1:
            raise ContractIssue("Reference with siblings is ambiguous; provide an explicit schema")
        if reference in stack:
            raise ContractIssue("Cyclic local schema reference; provide a finite supported schema")
        try:
            target = json_pointer(document, reference[1:])
        except (KeyError, ValueError):
            raise ContractIssue("Local schema reference is missing or invalid") from None
        return _resolve_refs(target, document, (*stack, reference), "schema")
    schema_fields = {key: "schema" for key in (
        "items", "additionalItems", "additionalProperties", "unevaluatedItems", "unevaluatedProperties",
        "not", "if", "then", "else", "contains", "propertyNames", "contentSchema")}
    schema_fields.update({key: "schema_array" for key in ("allOf", "anyOf", "oneOf", "prefixItems")})
    schema_fields.update({key: "schema_map" for key in ("properties", "patternProperties", "dependentSchemas", "$defs", "definitions")})
    fields = {
        "schema": schema_fields,
        "operation": {"parameters": "parameter_array", "requestBody": "request_body", "responses": "response_map"},
        "parameter": {"schema": "schema", "content": "media_map", "examples": "example_map"},
        "request_body": {"content": "media_map"},
        "response": {"content": "media_map", "headers": "header_map"},
        "media": {"schema": "schema", "examples": "example_map"},
        "example": {},  # Example.value is arbitrary application JSON.
        "document": {"paths": "path_map", "components": "components"},
        "components": {"schemas": "schema_map", "parameters": "parameter_map", "responses": "response_map", "requestBodies": "request_body_map", "headers": "header_map", "examples": "example_map"},
        "path_item": {**{method: "operation" for method in ("get", "post", "put", "patch", "delete", "head", "options", "trace")}, "parameters": "parameter_array"},
    }.get(context, {})
    # In particular: example/default/enum/const/schema.examples and extensions are data.
    return {key: _resolve_refs(item, document, stack, fields[key]) if key in fields else deepcopy(item)
            for key, item in value.items()}


def parse_openapi(document: dict) -> OpenAPIContract:
    digest = hash_json(document)
    questions = []
    operations = []
    if not isinstance(document, dict) or not str(document.get("openapi", "")).startswith("3."):
        return OpenAPIContract(contract_hash=digest, pending_questions=["Provide an OpenAPI 3.x JSON document"])
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return OpenAPIContract(contract_hash=digest, pending_questions=["Missing OpenAPI paths object"])
    methods = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    raw_ids = Counter(raw.get("operationId") for item in paths.values() if isinstance(item, dict)
                      for method, raw in item.items() if method in methods and isinstance(raw, dict)
                      and isinstance(raw.get("operationId"), str))
    raw_routes = Counter((method.upper(), re.sub(r"\{[^{}]+\}", "{}", path))
                         for path, item in paths.items() if isinstance(item, dict)
                         for method in item if method in methods)
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            questions.append(f"{path}: missing path item object")
            continue
        if "$ref" in path_item:
            questions.append(f"{path}: path item references are unsupported; provide explicit operations")
            continue
        for method, raw in path_item.items():
            if method not in methods:
                continue
            label = f"{method.upper()} {path}"
            try:
                if not isinstance(raw, dict):
                    raise ContractIssue("Missing operation object")
                operation_id = raw.get("operationId")
                if not isinstance(operation_id, str) or not operation_id.strip():
                    raise ContractIssue("Missing unambiguous operationId")
                raw = _resolve_refs(raw, document, (), "operation")
                inherited = _resolve_refs(path_item.get("parameters", []), document, (), "parameter_array")
                local = raw.get("parameters", [])
                if not isinstance(inherited, list) or not isinstance(local, list):
                    raise ContractIssue("Parameters must be arrays")
                # OpenAPI explicitly allows operation parameters to override inherited ones.
                parameters = {}
                for group in (inherited, local):
                    seen = set()
                    for parameter in group:
                        if not isinstance(parameter, dict) or parameter.get("in") not in {"path", "query"}:
                            raise ContractIssue("Only explicit path/query parameters are supported")
                        key = (parameter["in"], parameter.get("name"))
                        if key in seen:
                            raise ContractIssue("Duplicate/ambiguous parameters")
                        seen.add(key)
                        parameters[key] = Parameter(name=parameter.get("name"), location=parameter["in"],
                                                    required=parameter.get("required", False),
                                                    schema=parameter.get("schema", {}))
                request_body = raw.get("requestBody")
                if request_body is not None:
                    if not isinstance(request_body, dict) or "application/json" not in request_body.get("content", {}):
                        raise ContractIssue("Provide an application/json request body contract")
                responses = raw.get("responses")
                if not isinstance(responses, dict) or not responses:
                    raise ContractIssue("Missing documented responses")
                operations.append(Operation(operation_id=operation_id, method=method.upper(), path=path,
                                            parameters=list(parameters.values()), request_body=request_body,
                                            responses=responses))
            except (ContractIssue, ValidationError, TypeError, ValueError) as exc:
                # Validation errors may echo untrusted input; report category, not raw data.
                detail = str(exc) if isinstance(exc, ContractIssue) else "Invalid or unsupported operation shape"
                questions.append(f"{label}: {detail}")
    safe = []
    for operation in operations:
        if raw_ids[operation.operation_id] > 1 or raw_routes[(operation.method, re.sub(r"\{[^{}]+\}", "{}", operation.path))] > 1:
            questions.append(f"{operation.operation_id}: ambiguous operationId or templated route; clarify contract")
        else:
            safe.append(operation)
    return OpenAPIContract(contract_hash=digest, operations=safe, pending_questions=list(dict.fromkeys(questions)))


def load_openapi(path: Path) -> OpenAPIContract:
    return parse_openapi(read_json(path))


def validate_plan(plan: Plan, contract: OpenAPIContract | None = None) -> Plan:
    """Validate structural references, not payload schemas (negative tests are valid)."""
    plan = Plan.model_validate_json(plan.model_dump_json(by_alias=True))
    if contract is not None:
        if plan.contract_hash != contract.contract_hash:
            raise ValueError("Plan contract hash mismatch")
        known = {operation.operation_id: operation for operation in contract.operations}
        for operation in plan.operations:
            if operation.operation_id not in known or hash_json(operation) != hash_json(known[operation.operation_id]):
                raise ValueError("Plan operation is missing, ambiguous or differs from source contract")
    return plan
