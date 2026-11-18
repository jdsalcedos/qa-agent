from copy import deepcopy

import pytest

from qa_agent.models import Case, Plan, Requirement, Step
from qa_agent.openapi import ContractIssue, load_openapi, parse_openapi, resolve_local_refs, validate_plan
from qa_agent.store import atomic_write_json


def document():
    return {"openapi": "3.0.3", "info": {"title": "Test", "version": "1"},
            "components": {"schemas": {"Resource": {"type": "object", "properties": {"id": {"type": "integer"}}}}},
            "paths": {"/resources/{id}": {"parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                                          "get": {"operationId": "read", "parameters": [{"name": "filter", "in": "query", "schema": {"type": "boolean"}}], "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Resource"}}}}}}}}}


def response_schema(doc):
    return doc["paths"]["/resources/{id}"]["get"]["responses"]["200"]["content"]["application/json"]


def test_local_refs_preserve_operations_and_no_network(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("No network allowed"))
    doc = document()
    path = atomic_write_json(tmp_path / "openapi.json", doc)
    contract = load_openapi(path)
    assert not contract.pending_questions
    op = contract.operation("read")
    assert op.method == "GET" and op.path == "/resources/{id}"
    assert [(p.name, p.location, p.required) for p in op.parameters] == [("id", "path", True), ("filter", "query", False)]
    assert op.responses["200"]["content"]["application/json"]["schema"]["properties"]["id"] == {"type": "integer"}
    assert "$ref" in response_schema(doc)["schema"]  # projection did not mutate source
    with pytest.raises(KeyError):
        contract.operation("missing")


@pytest.mark.parametrize("reference", ["https://invalid.example/schema.json", "other.json#/components/schemas/A", "file:///etc/passwd", "#/components/schemas/Missing", "#/paths/~1other"])
def test_external_and_missing_refs_are_questions_not_operations(reference):
    doc = document()
    response_schema(doc)["schema"] = {"$ref": reference}
    contract = parse_openapi(doc)
    assert not contract.operations
    assert contract.pending_questions


def test_cycles_and_ref_siblings_are_missing_information():
    doc = document()
    doc["components"]["schemas"]["Resource"] = {"$ref": "#/components/schemas/Resource"}
    assert "Cyclic" in parse_openapi(doc).pending_questions[0]
    doc = document()
    response_schema(doc)["schema"]["description"] = "ambiguous sibling"
    assert "ambiguous" in parse_openapi(doc).pending_questions[0]
    with pytest.raises(ContractIssue):
        resolve_local_refs({"$ref": "external.json"}, {})


def test_ambiguous_operation_id_and_routes_excluded():
    doc = document()
    doc["paths"]["/other/{id}"] = deepcopy(doc["paths"]["/resources/{id}"])
    contract = parse_openapi(doc)
    assert not contract.operations and contract.pending_questions
    doc = document()
    doc["paths"]["/resources/{other}"] = deepcopy(doc["paths"]["/resources/{id}"])
    doc["paths"]["/resources/{other}"]["get"]["operationId"] = "readOther"
    doc["paths"]["/resources/{other}"]["parameters"][0]["name"] = "other"
    assert not parse_openapi(doc).operations


def test_operation_override_and_duplicate_params():
    doc = document()
    doc["paths"]["/resources/{id}"]["get"]["parameters"].append({"name": "id", "in": "path", "required": True, "schema": {"type": "string"}})
    contract = parse_openapi(doc)
    assert contract.operations[0].parameters[0].schema_ == {"type": "string"}
    doc["paths"]["/resources/{id}"]["get"]["parameters"].append({"name": "id", "in": "path", "required": True})
    assert not parse_openapi(doc).operations


@pytest.mark.parametrize("mutation", [
    lambda d: d["paths"]["/resources/{id}"]["get"].pop("operationId"),
    lambda d: d["paths"]["/resources/{id}"]["get"].update(responses={}),
    lambda d: d["paths"]["/resources/{id}"]["parameters"][0].update(required=False),
    lambda d: d["paths"]["/resources/{id}"]["get"].update(requestBody={"content": {"text/plain": {}}}),
    lambda d: d["paths"]["/resources/{id}"]["get"]["parameters"].append({"in": "header", "name": "X"}),
])
def test_unsupported_or_incomplete_operations_are_pending(mutation):
    doc = document()
    mutation(doc)
    contract = parse_openapi(doc)
    assert not contract.operations and contract.pending_questions


def test_contract_validation_and_intentionally_invalid_query():
    contract = parse_openapi(document())
    plan = Plan(requirements=[Requirement(id="R", text="r")], contract_hash=contract.contract_hash, operations=deepcopy(contract.operations),
                cases=[Case(id="C", requirement_ids=["R"], objective="o", expected="422", steps=[Step(operation_id="read", path={"id": -1}, query={"filter": "not-a-boolean"}, assertions=[{"status": 422}])])])
    assert validate_plan(plan, contract).cases[0].steps[0].query == {"filter": "not-a-boolean"}
    plan.contract_hash = "b" * 64
    with pytest.raises(ValueError, match="hash"):
        validate_plan(plan, contract)
    plan.contract_hash = contract.contract_hash
    plan.operations[0].responses = {"999": {}}
    with pytest.raises(ValueError, match="differs"):
        validate_plan(plan, contract)


def test_missing_contract_and_source_keys(tmp_path):
    assert parse_openapi({}).pending_questions
    assert parse_openapi({"openapi": "3.1.0"}).pending_questions
    path = tmp_path / "duplicate.json"
    path.write_text('{"openapi":"3.0.0","paths":{},"paths":{}}')
    with pytest.raises(ValueError, match="Duplicate JSON key"):
        load_openapi(path)


def test_duplicate_id_stays_ambiguous_even_if_one_definition_invalid():
    doc = document()
    doc["paths"]["/other/{id}"] = deepcopy(doc["paths"]["/resources/{id}"])
    doc["paths"]["/other/{id}"]["get"]["responses"] = {}
    result = parse_openapi(doc)
    assert not result.operations
    assert any("ambiguous" in question for question in result.pending_questions)


def test_ref_literals_in_schema_data_and_property_names_are_preserved():
    doc = document()
    literal = {"$ref": "literal application data", "schema": {"$ref": "not a schema"}}
    schema = doc["components"]["schemas"]["Resource"]
    schema["properties"]["$ref"] = {"type": "string", "example": literal}
    schema["properties"]["nested"] = {"$ref": "#/components/schemas/Child"}
    schema.update(example=deepcopy(literal), examples=[deepcopy(literal)], default=deepcopy(literal), enum=[deepcopy(literal)], const=deepcopy(literal))
    doc["components"]["schemas"]["Child"] = {"type": "integer"}
    contract = parse_openapi(doc)
    assert not contract.pending_questions
    resolved = contract.operations[0].responses["200"]["content"]["application/json"]["schema"]
    assert resolved["properties"]["$ref"] == {"type": "string", "example": literal}
    assert resolved["properties"]["nested"] == {"type": "integer"}
    assert resolved["example"] == resolved["default"] == resolved["const"] == literal
    assert resolved["examples"] == resolved["enum"] == [literal]


def test_media_parameter_and_named_examples_resolve_only_reference_objects():
    doc = document()
    literal = {"$ref": "literal application data"}
    media = response_schema(doc)
    media["example"] = deepcopy(literal)
    media["examples"] = {"$ref": {"value": deepcopy(literal)}, "normal": {"value": {"schema": deepcopy(literal)}}}
    param = doc["paths"]["/resources/{id}"]["get"]["parameters"][0]
    param.update(example=deepcopy(literal), examples={"$ref": {"value": deepcopy(literal)}})
    contract = parse_openapi(doc)
    assert not contract.pending_questions
    resolved_media = contract.operations[0].responses["200"]["content"]["application/json"]
    assert resolved_media["example"] == literal
    assert resolved_media["examples"]["$ref"]["value"] == literal
    assert resolved_media["examples"]["normal"]["value"]["schema"] == literal
    # A true Reference Object in examples remains subject to offline-only policy.
    media["examples"]["external"] = {"$ref": "https://invalid.example/example.json"}
    assert not parse_openapi(doc).operations


def test_structural_ref_inside_schema_map_is_still_rejected():
    doc = document()
    doc["components"]["schemas"]["Resource"]["properties"]["$ref"] = {"$ref": "other.json#/A"}
    contract = parse_openapi(doc)
    assert not contract.operations and contract.pending_questions
