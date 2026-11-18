import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from qa_agent.models import Case, CaseState, Operation, Step, StatusAssertion, EqualAssertion, PresentAssertion, EmptyBodyAssertion
from qa_agent.runtime import evaluate_assertions, execute_suite
from qa_agent.security import SecurityError
from qa_agent.store import ApprovalError, IntegrityError, Store, save_plan
from runtime_support import local_http, one_case, prepare


def test_actual_subprocess_two_steps_capture_and_evidence(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port)
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.status.value == "completed"
        assert run.cases[0].state == CaseState.PASSED
        assert [item["path"] for item in requests] == ["/create", "/items/7"]
        assert run.cases[0].steps[0].captures["id"] == 7
        assert run.cases[0].steps[1].assertions[1].observed is True
        assert len(run.evidence_paths) == 4  # durable start + final, each step
        assert store.load_evidence(run.run_id, run.evidence_paths[0]).state == CaseState.INCONCLUSIVE
        assert (store.run_dir(run.run_id) / "suite" / "test_0000.py").exists()
        store.verify_run_integrity(run.run_id)
        second = execute_suite(manifest.suite_id, "local", store=store)
        assert second.run_id != run.run_id
        assert len(store.list_runs()) == 2


@pytest.mark.parametrize("kwargs,state", [({"preconditions": ["seed"]}, "blocked"), ({}, "passed")])
def test_preconditions_zero_http(tmp_path, kwargs, state):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(**kwargs)])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state.value == state
        assert len(requests) == (state == "passed")
        assert run.cases[0].attempted == (state == "passed")


def test_no_approval_no_http_and_no_run(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, approved=False)
        with pytest.raises(ApprovalError):
            execute_suite(manifest.suite_id, "local", store=store)
        assert requests == []
        assert store.list_runs() == []


@pytest.mark.parametrize("target", ["plan.json", "conftest.py", "_qa_runtime/runtime.py", "pytest.ini", "manifest.json"])
def test_tampering_rejected_before_http(tmp_path, target):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port)
        with (tmp_path / "suite" / target).open("a") as handle:
            handle.write("\n# tamper\n")
        with pytest.raises((IntegrityError, ValueError)):
            execute_suite(manifest.suite_id, "local", store=store)
        assert requests == []


def test_first_assertion_failure_stops_case_and_preserves_order(tmp_path):
    with local_http() as (port, requests):
        fail = one_case(steps=[Step(operation_id="read", assertions=[{"igual": "/value", "valor": 1}]), Step(operation_id="create", assertions=[{"status": 200}])])
        next_case = one_case().model_copy(update={"id": "AAA"})
        store, manifest, _, _ = prepare(tmp_path, port, cases=[fail, next_case])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert [case.case_id for case in run.cases] == ["C", "AAA"]
        assert [case.state.value for case in run.cases] == ["failed", "passed"]
        assert [item["path"] for item in requests] == ["/read", "/read"]
        assert run.cases[0].steps[0].assertions[0].observed is True
        assert run.cases[0].steps[0].assertions[0].expected == 1


@pytest.mark.parametrize("route,assertion,state", [
    ("/invalid", {"igual": "", "valor": {}}, "inconclusive"),
    ("/empty", {"cuerpo_vacio": True}, "passed"),
    ("/null", {"igual": "", "valor": None}, "passed"),
    ("/null", {"presente": "/absent"}, "failed"),
    ("/large", {"status": 200}, "inconclusive"),
    ("/redirect", {"status": 302}, "technical_error"),
    ("/reject", {"status": 422}, "passed"),
])
def test_response_classification(tmp_path, route, assertion, state):
    with local_http() as (port, requests):
        operation = Operation(operation_id="read", method="GET", path=route)
        store, manifest, _, _ = prepare(tmp_path, port, operations=[operation],
                                      cases=[one_case(steps=[Step(operation_id="read", assertions=[assertion])])],
                                      profile_changes={"max_response_bytes": 128})
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state.value == state
        assert len(requests) == 1
        assert run.cases[0].attempted


def test_missing_capture_at_origin_stops_subsequent_http(tmp_path):
    with local_http() as (port, requests):
        steps = [Step(operation_id="read", assertions=[{"status": 200}], captures={"missing": "/absent"}),
                 Step(operation_id="read", assertions=[{"status": 200}])]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.INCONCLUSIVE
        assert "capture" in run.cases[0].steps[0].error
        assert len(requests) == 1


def test_terminal_technical_error_before_second_http_completes_run(tmp_path):
    with local_http() as (port, requests):
        steps = [Step(operation_id="read", assertions=[{"status": 200}], captures={"items": "/items"}),
                 Step(operation_id="get", path={"id": {"var": "items"}}, assertions=[{"status": 200}])]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.TECHNICAL_ERROR
        assert run.cases[0].attempted
        assert len(run.cases[0].steps) == 1
        assert run.cases[0].steps[0].state == CaseState.PASSED
        assert "Path values" in run.cases[0].reason
        assert len(requests) == 1
        assert run.status.value == "completed"
        assert run.error is None
        assert store.load_run(run.run_id) == run


def test_typed_substitution_negative_payload(tmp_path):
    with local_http() as (port, requests):
        operation = Operation(operation_id="read", method="POST", path="/reject",
                              request_body={"content": {"application/json": {"schema": {"type": "object"}}}})
        step = Step(operation_id="read", body={"var": "payload"}, assertions=[{"status": 422}])
        # Deliberately wrong type for schema: do not coerce/correct negative data.
        case = one_case(steps=[step], data={"payload": [True, None, 1, 1.5, {"nested": False}]})
        store, manifest, _, _ = prepare(tmp_path, port, cases=[case], operations=[operation])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.PASSED
        actual = json.loads(requests[0]["body"])
        assert actual == [True, None, 1, 1.5, {"nested": False}]
        assert actual[0] is True and actual[1] is None


def test_body_absent_and_explicit_null_preserved(tmp_path):
    with local_http() as (port, requests):
        steps = [Step(operation_id="create", assertions=[{"status": 200}]), Step(operation_id="create", body=None, assertions=[{"status": 200}])]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.PASSED
        assert [item["body"] for item in requests] == [b"", b"null"]
        assert [step.request["body_present"] for step in run.cases[0].steps] == [False, True]


def test_same_approved_suite_reference_and_variant_observed_difference(tmp_path):
    with local_http() as (reference_port, reference_requests), local_http(response_value=False) as (variant_port, variant_requests):
        store, manifest, _, _ = prepare(tmp_path, reference_port)
        reference = execute_suite(manifest.suite_id, "local", store=store)
        profile = store.load_profile("local")
        profile.base_url = f"http://127.0.0.1:{variant_port}"
        profile.allowed_targets = [{"host": "127.0.0.1", "port": variant_port, "ips": ["127.0.0.1"]}]
        profile.product_version = "variant"
        store.save_profile(profile)
        variant = execute_suite(manifest.suite_id, "local", store=store)
        assert reference.cases[0].state == CaseState.PASSED
        assert variant.cases[0].state == CaseState.FAILED
        assert variant.cases[0].steps[-1].assertions[1].expected is True
        assert variant.cases[0].steps[-1].assertions[1].observed is False
        assert reference.plan_hash == variant.plan_hash == manifest.plan_hash
        assert reference.suite_id == variant.suite_id
        assert reference.product_version == "fixture" and variant.product_version == "variant"
        assert len(reference_requests) == len(variant_requests) == 2


def test_pending_cases_not_success_and_zero_http(tmp_path):
    pending = Case(id="manual", requirement_ids=["R"], objective="manual", expected="browser", automation_pending_reason="browser unsupported")
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[pending])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.status.value == "completed"
        assert run.cases[0].state == CaseState.DESIGNED
        assert not run.cases[0].attempted
        assert requests == []


def test_snapshot_is_executed_not_original_mutable_path(tmp_path, monkeypatch):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()])
        create = store.create_run
        def snapshot_then_change(*args):
            run = create(*args)
            (tmp_path / "suite" / "test_0000.py").write_text("raise RuntimeError('original must not execute')")
            return run
        monkeypatch.setattr(store, "create_run", snapshot_then_change)
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.PASSED
        assert len(requests) == 1
        store.verify_run_integrity(run.run_id)


def test_changed_source_invalidates_execution_before_http(tmp_path):
    with local_http() as (port, requests):
        store, manifest, plan, source = prepare(tmp_path, port, cases=[one_case()])
        plan.cases[0].objective = "modified after review"
        save_plan(source, plan)
        with pytest.raises(IntegrityError):
            execute_suite(manifest.suite_id, "local", store=store)
        assert requests == []


def test_unauthorized_operation_blocks_case_before_any_http(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()])
        profile = store.load_profile("local")
        profile.allowed_operations = ["other"]
        store.save_profile(profile)
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.BLOCKED
        assert not run.cases[0].attempted
        assert requests == []


def test_request_timeout_no_retry_and_durable_attempt(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, operations=[Operation(operation_id="read", method="POST", path="/slow")],
                                      cases=[one_case()], profile_changes={"request_timeout_seconds": 0.05})
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.TECHNICAL_ERROR
        assert run.cases[0].steps[0].http_status is None
        assert len(requests) == 1
        assert store.load_evidence(run.run_id, run.evidence_paths[0]).state == CaseState.INCONCLUSIVE


def test_process_timeout_preserves_in_progress_attempt_and_pending(tmp_path):
    with local_http() as (port, requests):
        cases = [one_case(), one_case().model_copy(update={"id": "later"})]
        store, manifest, _, _ = prepare(tmp_path, port, cases=cases,
                                      operations=[Operation(operation_id="read", method="POST", path="/slow")],
                                      profile_changes={"process_timeout_seconds": 0.65, "request_timeout_seconds": 5})
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.status.value == "interrupted"
        assert run.cases[0].attempted
        assert run.cases[0].state == CaseState.INCONCLUSIVE
        assert not run.cases[1].attempted
        assert run.cases[1].state == CaseState.GENERATED
        assert len(requests) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX SIGKILL worker guarantee")
def test_sigkill_worker_keeps_inconclusive_case_and_interrupted_run(tmp_path, monkeypatch):
    import qa_agent.runtime as runtime
    with local_http() as (port, requests):
        steps = [Step(operation_id="read", assertions=[{"status": 200}]),
                 Step(operation_id="read", assertions=[{"status": 200}])]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)],
                                      operations=[Operation(operation_id="read", method="POST", path="/slow")])
        popen = subprocess.Popen
        killers = []
        def launch(*args, **kwargs):
            process = popen(*args, **kwargs)
            def kill_after_first_request():
                deadline = time.monotonic() + 10
                while not requests and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if process.poll() is None:
                    process.kill()
            killer = threading.Thread(target=kill_after_first_request)
            killer.start()
            killers.append(killer)
            return process
        monkeypatch.setattr(runtime.subprocess, "Popen", launch)
        try:
            run = execute_suite(manifest.suite_id, "local", store=store)
        finally:
            for killer in killers:
                killer.join(timeout=12)
        assert run.status.value == "interrupted"
        assert run.cases[0].state == CaseState.INCONCLUSIVE
        assert run.cases[0].attempted
        assert not run.cases[0].steps
        assert len(requests) == 1
        assert store.load_evidence(run.run_id, run.evidence_paths[0]).state == CaseState.INCONCLUSIVE
        store.verify_run_integrity(run.run_id)


def test_credentials_reflections_redacted_everywhere(tmp_path, monkeypatch):
    secret = "canary-runtime-56789"
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    with local_http() as (port, requests):
        steps = [Step(operation_id="read", assertions=[{"status": 200}], captures={"raw": ""})]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)],
                                      operations=[Operation(operation_id="read", method="GET", path="/reflect")],
                                      profile_changes={"credentials": {"Authorization": "FIXTURE_AUTH"}})
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.PASSED
        assert requests[0]["auth"] == secret
        for path in store.root.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), path
        assert "[REDACTED]" in json.dumps(run.model_dump(mode="json"))


@pytest.mark.parametrize("route", ["/reflect", "/reflect_unicode"])
@pytest.mark.parametrize("secret", ['runtime-canary-"-sensitive', r"runtime-canary-\-sensitive", r"runtime-canary-\u2603-sensitive"])
def test_json_escaped_credentials_not_recoverable_from_persisted_evidence(tmp_path, monkeypatch, route, secret):
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    with local_http() as (port, requests):
        steps = [Step(operation_id="read", assertions=[{"status": 200}, {"presente": "/reflection"}],
                      captures={"reflected": "/reflection", "raw": ""}),
                 Step(operation_id="read", assertions=[{"igual": "/reflection", "valor": {"var": "reflected"}}])]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)],
                                      operations=[Operation(operation_id="read", method="GET", path=route)],
                                      profile_changes={"credentials": {"Authorization": "FIXTURE_AUTH"}})
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.status.value == "completed"
        assert run.cases[0].state == CaseState.PASSED
        assert len(requests) == 2 and all(request["auth"] == secret for request in requests)
        # Step two compares against the RAW captured value, not the display copy.
        assert run.cases[0].steps[1].assertions[0].passed
        assert run.cases[0].steps[0].captures["reflected"] == "[REDACTED]"
        assert run.cases[0].steps[0].captures["raw"]["reflection"] == "[REDACTED]"
        for step in run.cases[0].steps:
            decoded = json.loads(step.response["body_text"])
            assert decoded["reflection"] == "[REDACTED]"
            assert secret not in json.dumps(decoded, ensure_ascii=False)
            assert decoded["nested"]["cookie"] == "[REDACTED]"
            matching = [store.load_evidence(run.run_id, path) for path in run.evidence_paths]
            assert step in matching
        # Read actual JSON files too, not only typed Store objects or literal bytes.
        for path in store.run_dir(run.run_id).joinpath("evidence").glob("*.json"):
            evidence = json.loads(path.read_text())
            if "body_text" in evidence["response"]:
                assert json.loads(evidence["response"]["body_text"])["reflection"] == "[REDACTED]"
        persisted = json.loads(Path(run.result_path).read_text())
        assert all(json.loads(step["response"]["body_text"])["reflection"] == "[REDACTED]"
                   for step in persisted["cases"][0]["steps"])
        store.verify_run_integrity(run.run_id)


@pytest.mark.parametrize("secret", ['contains-quote-"-canary', 'url-canary/a?b'])
def test_embedded_credential_rejected_before_snapshot(tmp_path, monkeypatch, secret):
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(data={"forbidden": secret})],
                                      profile_changes={"credentials": {"Authorization": "FIXTURE_AUTH"}})
        with pytest.raises(SecurityError) as error:
            execute_suite(manifest.suite_id, "local", store=store)
        assert secret not in str(error.value)
        assert requests == []
        assert store.list_runs() == []


def test_credential_name_cannot_enable_pytest_plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_PLUGINS", "must_not_import")
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()], profile_changes={"credentials": {"Authorization": "PYTEST_PLUGINS"}})
        with pytest.raises(SecurityError):
            execute_suite(manifest.suite_id, "local", store=store)
        assert requests == []
        assert store.list_runs() == []


def test_missing_credentials_block_without_http(tmp_path, monkeypatch):
    monkeypatch.delenv("ABSENT_FIXTURE", raising=False)
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()], profile_changes={"credentials": {"Authorization": "ABSENT_FIXTURE"}})
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.BLOCKED
        assert requests == []


def test_ambient_pytest_env_and_ancestor_plugins_not_loaded(tmp_path, monkeypatch):
    marker = tmp_path / "plugin-ran"
    evil = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
    (tmp_path / "conftest.py").write_text(evil)
    (tmp_path / "evil_plugin.py").write_text(evil)
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --not-a-valid-flag\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--not-a-valid-flag")
    monkeypatch.setenv("PYTEST_PLUGINS", "evil_plugin")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()])
        run = execute_suite(manifest.suite_id, "local", store=store)
        assert run.cases[0].state == CaseState.PASSED
        assert not marker.exists()
        assert len(requests) == 1


def test_direct_pytest_same_comparisons_and_own_run(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port)
        normal = execute_suite(manifest.suite_id, "local", store=store)
        env = {"QA_AGENT_HOME": str(store.root), "QA_ENTORNO": "local", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        result = subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", str(tmp_path / "suite" / "test_0000.py")],
                                cwd=tmp_path / "suite", env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        other = store.load_run(next(identifier for identifier in store.list_runs() if identifier != normal.run_id))
        assert other.cases[0].state == normal.cases[0].state
        assert [step.assertions for step in other.cases[0].steps] == [step.assertions for step in normal.cases[0].steps]
        assert len(requests) == 4


@pytest.mark.skipif(os.name != "posix", reason="POSIX lifetime-pipe guarantee")
def test_parent_sigkill_stops_worker_and_preserves_attempt(tmp_path):
    with local_http() as (port, requests):
        steps = [Step(operation_id="read", assertions=[{"status": 200}]), Step(operation_id="read", assertions=[{"status": 200}])]
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case(steps=steps)],
                                      operations=[Operation(operation_id="read", method="POST", path="/slow")])
        bootstrap = "import sys;sys.path.insert(0,sys.argv[1]);from qa_agent.runtime import execute_suite;execute_suite(sys.argv[2],'local')"
        env = {"QA_AGENT_HOME": str(store.root)}
        parent = subprocess.Popen([sys.executable, "-I", "-B", "-c", bootstrap, str(Path(__file__).resolve().parents[1]), manifest.suite_id],
                                  env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not requests and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(requests) == 1
            parent.kill()
            parent.wait(timeout=5)
            time.sleep(1.2)
            assert len(requests) == 1
            run = store.load_run(store.list_runs()[0])
            assert run.cases[0].attempted
            assert run.cases[0].state == CaseState.INCONCLUSIVE
            assert run.evidence_paths
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)


def test_direct_pytest_requires_approval(tmp_path):
    with local_http() as (port, requests):
        store, _, _, _ = prepare(tmp_path, port, approved=False)
        result = subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", str(tmp_path / "suite" / "test_0000.py")],
                                cwd=tmp_path / "suite", env={"QA_AGENT_HOME": str(store.root), "QA_ENTORNO": "local", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                                capture_output=True, timeout=20)
        assert result.returncode != 0
        assert requests == []
        assert store.list_runs() == []


@pytest.mark.parametrize("pointer,observed", [("", {"a/b": [{"~key": None}]}), ("/a~1b/0/~0key", None)])
def test_json_pointer_root_escapes_and_null(pointer, observed):
    raw = b'{"a/b":[{"~key":null}]}'
    results, state = evaluate_assertions([EqualAssertion(pointer=pointer, value=observed), PresentAssertion(pointer=pointer)], 200, raw, {})
    assert state == CaseState.PASSED
    assert all(result.passed for result in results)


def test_status_mismatch_dominates_unavailable_json():
    results, state = evaluate_assertions([StatusAssertion(status=201), EqualAssertion(pointer="", value=None)], 200, b"broken", {})
    assert state == CaseState.FAILED
    assert len(results) == 2


@pytest.mark.parametrize("raw,passed", [(b"", True), (b" ", False), (b"null", False), (b"{}", False), (b"[]", False)])
def test_zero_bytes_comparator(raw, passed):
    results, _ = evaluate_assertions([EmptyBodyAssertion()], 204, raw, {})
    assert results[0].passed == passed


@pytest.mark.parametrize("actual,expected,passed", [(True, 1, False), (1, 1.0, False), ([1, 2], [2, 1], False), (None, None, True), ({"x": [True]}, {"x": [1]}, False)])
def test_strict_comparator(actual, expected, passed):
    results, _ = evaluate_assertions([EqualAssertion(pointer="", value=expected)], 200, json.dumps(actual).encode(), {})
    assert results[0].passed == passed
