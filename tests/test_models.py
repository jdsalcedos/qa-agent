from copy import deepcopy

import pytest
from pydantic import ValidationError

from qa_agent.models import (
    Case, CaseResult, CaseState, EqualAssertion, Operation, Plan, Requirement,
    Step, json_pointer, strict_json_equal, substitute_variables, validate_pointer,
)


def minimal_plan():
    return Plan(requirements=[Requirement(id="R-1", text="Read a resource")], contract_hash="a" * 64,
                operations=[Operation(operation_id="read", method="GET", path="/resources", responses={"200": {}})],
                cases=[Case(id="CP-1", requirement_ids=["R-1"], objective="Read", expected="200",
                            steps=[Step(operation_id="read", assertions=[{"kind": "status", "status": 200}])])])


def test_plan_roundtrip_and_partial():
    plan = minimal_plan()
    assert Plan.model_validate_json(plan.model_dump_json()) == plan
    assert plan.cases[0].executable
    partial = Plan(requirements=plan.requirements, cases=[Case(id="P", requirement_ids=["R-1"], objective="UI", expected="Visible", automation_pending_reason="Browser unsupported")])
    assert not partial.cases[0].executable
    assert partial.contract_hash is None


@pytest.mark.parametrize("change", [
    lambda p: p["cases"][0].update(steps=[]),
    lambda p: p["cases"][0]["steps"][0].update(assertions=[]),
    lambda p: p["cases"][0]["steps"][0].update(operation_id="invented"),
    lambda p: p["cases"][0].update(requirement_ids=["missing"]),
    lambda p: p.update(contract_hash=None),
    lambda p: p["cases"].append(deepcopy(p["cases"][0])),
    lambda p: p["operations"].append(deepcopy(p["operations"][0])),
    lambda p: p["requirements"].append(deepcopy(p["requirements"][0])),
    lambda p: p.update(version="2.0"),
    lambda p: p.update(approved=True),
    lambda p: p["cases"][0]["steps"][0].update(query={"unknown": "x"}),
    lambda p: p["cases"][0]["steps"][0].update(body={"x": 1}),
])
def test_invalid_plan_shapes_and_references(change):
    value = minimal_plan().model_dump(mode="json")
    change(value)
    with pytest.raises(ValidationError):
        Plan.model_validate(value)


@pytest.mark.parametrize("assertion", [{"status": True}, {"status": "200"}, {"status": 600}, {"igual": "/x"}, {"kind": "equal", "pointer": "/x"}, {"kind": "unknown"}, {"status": 200, "presente": "/id"}, {"kind": "empty_body", "value": True}])
def test_invalid_assertions(assertion):
    with pytest.raises(ValidationError):
        Step(operation_id="read", assertions=[assertion])


def test_spec_spanish_input_and_null_equality():
    case = Case.model_validate({"id": "CP-01", "requisito": "R-1", "objetivo": "Read", "prioridad": "alta", "obligatorio": True,
                               "precondiciones": [], "datos": {}, "esperado": "OK", "pasos": [{"operacion": "read", "assert": [{"status": 200}, {"presente": "/id"}, {"igual": "", "valor": None}, {"cuerpo_vacio": True}], "capturar": {"root": ""}}]})
    assert case.priority == "high"
    assert isinstance(case.steps[0].assertions[2], EqualAssertion)
    assert case.steps[0].assertions[2].value is None
    assert "pasos" not in case.model_dump()


@pytest.mark.parametrize("pointer,expected", [("", {"a/b": {"~": [False, None]}, "": 2}), ("/a~1b/~0/0", False), ("/a~1b/~0/1", None), ("/", 2)])
def test_json_pointer(pointer, expected):
    assert strict_json_equal(json_pointer({"a/b": {"~": [False, None]}, "": 2}, pointer), expected)


@pytest.mark.parametrize("pointer", ["/missing", "/items/01", "/items/-", "/items/3", "/items/0/x"])
def test_missing_pointer_is_not_null(pointer):
    with pytest.raises(KeyError):
        json_pointer({"items": [None]}, pointer)


@pytest.mark.parametrize("pointer", ["id", "#", "/x~", "/x~2"])
def test_invalid_pointer(pointer):
    with pytest.raises(ValueError):
        validate_pointer(pointer)


@pytest.mark.parametrize("left,right,equal", [(True, 1, False), (False, 0, False), (1, 1.0, False), ([1, 2], [2, 1], False), ({"a": True}, {"a": 1}, False), ({"a": None}, {}, False), ({"a": [1, None]}, {"a": [1, None]}, True)])
def test_strict_comparison(left, right, equal):
    assert strict_json_equal(left, right) is equal


@pytest.mark.parametrize("value", [False, None, 4, 1.5, [], [1], {"a": 1}])
def test_variable_substitution_preserves_types_and_copy(value):
    result = substitute_variables({"x": {"var": "v"}}, {"v": value})
    assert strict_json_equal(result["x"], value)
    if isinstance(value, (list, dict)):
        assert result["x"] is not value
    with pytest.raises(KeyError):
        substitute_variables({"var": "missing"}, {})


def test_capture_order_case_scope_and_collision():
    first = {"operation_id": "read", "assertions": [{"status": 200}], "captures": {"id": "/id"}}
    second = {"operation_id": "read", "path": {"id": {"var": "id"}}, "assertions": [{"igual": "/id", "valor": {"var": "id"}}]}
    case = dict(id="CP", requirement_ids=["R"], objective="o", expected="e")
    assert Case(**case, steps=[first, second]).executable
    with pytest.raises(ValidationError, match="Undefined/forward"):
        Case(**case, steps=[second, first])
    with pytest.raises(ValidationError, match="collide"):
        Case(**case, data={"id": 1}, steps=[first])
    with pytest.raises(ValidationError):
        Case(**case, steps=[second])


def test_negative_payload_is_not_validated_against_schema():
    plan = minimal_plan().model_dump(mode="json")
    plan["operations"][0].update(method="POST", request_body={"content": {"application/json": {"schema": {"type": "object"}}}})
    plan["cases"][0]["steps"][0]["body"] = [False, "x" * 81, {"unexpected": None}]
    plan["cases"][0]["steps"][0]["body_present"] = True
    validated = Plan.model_validate(plan)
    assert validated.cases[0].steps[0].body == [False, "x" * 81, {"unexpected": None}]


@pytest.mark.parametrize("state,attempted", [(CaseState.PASSED, False), (CaseState.FAILED, False), (CaseState.TECHNICAL_ERROR, False), (CaseState.INCONCLUSIVE, False), (CaseState.BLOCKED, True), (CaseState.GENERATED, True), (CaseState.DESIGNED, True)])
def test_attempt_state_invariants(state, attempted):
    with pytest.raises(ValidationError):
        CaseResult(case_id="CP", requirement_ids=["R"], state=state, attempted=attempted)


def test_absent_body_and_explicit_json_null_survive_roundtrip():
    absent = Step(operation_id="read", assertions=[{"status": 200}])
    explicit_null = Step(operation_id="read", body=None, assertions=[{"status": 422}])
    assert not absent.body_present
    assert explicit_null.body_present
    assert Step.model_validate_json(absent.model_dump_json()).body_present is False
    assert Step.model_validate_json(explicit_null.model_dump_json()).body_present is True
    with pytest.raises(ValidationError):
        Step(operation_id="read", body={"x": 1}, body_present=False, assertions=[{"status": 200}])


@pytest.mark.parametrize("path", ["/x/{id", "/x/id}", "/x/{}"])
def test_malformed_route_placeholders_are_rejected(path):
    with pytest.raises(ValidationError):
        Operation(operation_id="read", method="GET", path=path)


@pytest.mark.parametrize("value", [1, 1.0, False, "true", None])
def test_empty_body_spanish_alias_requires_literal_true(value):
    with pytest.raises(ValidationError):
        Step(operation_id="read", assertions=[{"cuerpo_vacio": value}])
    assert Step(operation_id="read", assertions=[{"cuerpo_vacio": True}]).assertions[0].kind == "empty_body"
