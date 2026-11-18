"""Sequential HTTP execution with durable attempts and an isolated pytest worker.

This is a local trusted-account runner, not a Python/filesystem sandbox. The same
module is bundled into exports; direct pytest delegates to the identical worker.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import quote, quote_plus

import httpx

from .models import (AssertionResult, CaseState, EnvironmentProfile, RunResult,
                     RunStatus, StepEvidence, json_pointer, strict_json_equal,
                     substitute_variables, utc_now)
from .security import (SecurityError, authorize_operation, build_url,
                       httpx_client_options, missing_preconditions, redact,
                       resolve_credentials)
from .store import IntegrityError, Store, StoreError, read_json


class ResponseLimitError(ValueError):
    pass


def executor_environment(profile: EnvironmentProfile, store: Store) -> dict[str, str]:
    """Do not inherit pytest/Python/proxy/loader configuration or unrelated secrets."""
    env = {"QA_AGENT_HOME": str(store.root), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    # PATH isn't needed: every executable is absolute. Windows needs SystemRoot.
    if os.name == "nt" and "SystemRoot" in os.environ:
        env["SystemRoot"] = os.environ["SystemRoot"]
    resolve_credentials(profile, {name: "validation-only" for name in profile.credentials.values()})
    for name in profile.credentials.values():
        if name.startswith(("PYTEST", "PYTHON", "LD_", "DYLD_")) or name in {"QA_AGENT_HOME", "QA_ENTORNO", "QA_RUNTIME_RUN", "QA_RUNTIME_PARENT_FD", "PATH", "HOME", "SHELL", "SystemRoot"}:
            raise SecurityError("Credential variable conflicts with execution configuration")
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _json(raw: bytes):
    def constant(_):
        raise ValueError("Non-finite JSON response")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate response JSON key")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=constant, object_pairs_hook=unique)


def evaluate_assertions(assertions, status: int, raw: bytes, variables: dict):
    """Pure comparisons; absent pointers and undecodable JSON never become null."""
    document = None
    decode_error = False
    if any(item.kind in {"present", "equal"} for item in assertions):
        try:
            document = _json(raw)
        except (ValueError, UnicodeError):
            decode_error = True
    results = []
    insufficient = False
    for assertion in assertions:
        kind = assertion.kind
        message = None
        if kind == "status":
            expected, observed = assertion.status, status
            passed = status == expected
        elif kind == "empty_body":
            expected, observed = 0, len(raw)
            passed = len(raw) == 0
        else:
            expected = True if kind == "present" else substitute_variables(assertion.value, variables)
            if decode_error:
                observed, passed = None, False
                message = "Response is not unambiguous JSON; observation unavailable"
                insufficient = True
            else:
                try:
                    value = json_pointer(document, assertion.pointer)
                except KeyError:
                    observed, passed, message = None, False, "JSON Pointer is absent (not JSON null)"
                    if kind == "present":
                        observed = False
                else:
                    observed = True if kind == "present" else deepcopy(value)
                    passed = kind == "present" or strict_json_equal(expected, observed)
        results.append(AssertionResult(kind=kind, expected=expected, observed=observed,
                                       passed=passed, message=message))
    # An actual mismatch dominates unavailable JSON in another assertion.
    mismatch = any(not item.passed and not (item.message or "").startswith("Response is not") for item in results)
    state = CaseState.FAILED if mismatch else CaseState.INCONCLUSIVE if insufficient else CaseState.PASSED
    return results, state


def _save_case(store, result, case_result, secrets):
    result.cases = [case_result if old.case_id == case_result.case_id else old for old in result.cases]
    store.save_run(result, secrets=secrets)


def execute_case(run_id: str, case_id: str, suite_root: Path, *, store: Store | None = None):
    """Worker entry point. Only a verified historical snapshot may execute here."""
    store = store or Store()
    result = store.load_run(run_id)
    root = store.run_dir(run_id)
    if suite_root.resolve() != (root / "suite").resolve():
        raise IntegrityError("Worker must execute the verified run snapshot")
    plan = store.load_run_plan(run_id)
    case = next(case for case in plan.cases if case.id == case_id and case.executable)
    current = next(case for case in result.cases if case.case_id == case_id)
    if current.attempted or current.state != CaseState.GENERATED:
        raise IntegrityError("A case cannot be replayed within a run")
    profile = EnvironmentProfile.model_validate(read_json(root / "environment.json"))
    secrets = []
    try:
        headers = resolve_credentials(profile)
        secrets = list(headers.values())
        missing = missing_preconditions(case, profile)
        if missing:
            raise SecurityError("Missing preconditions: " + ", ".join(missing))
        operations = {operation.operation_id: operation for operation in plan.operations}
        for step in case.steps:
            authorize_operation(profile, operations[step.operation_id])
    except SecurityError as exc:
        current.state, current.reason = CaseState.BLOCKED, redact(str(exc), secrets)
        _save_case(store, result, current, secrets)
        return current
    variables = deepcopy(case.data)
    with httpx.Client(**httpx_client_options(profile), transport=httpx.HTTPTransport(retries=0, trust_env=False)) as client:
        for index, step in enumerate(case.steps):
            operation = operations[step.operation_id]
            try:
                path = substitute_variables(step.path, variables)
                query = substitute_variables(step.query, variables)
                body = substitute_variables(step.body, variables)
                url = build_url(profile, operation, path)
                kwargs = {"params": query, "headers": dict(headers)}
                if step.body_present:
                    kwargs["content"] = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
                    kwargs["headers"]["Content-Type"] = "application/json"
                request = {"method": operation.method, "url": url, "query": query,
                           "headers": kwargs["headers"], "body_present": step.body_present}
                if step.body_present:
                    request["body"] = body
            except (ValueError, KeyError) as exc:
                current.state = CaseState.TECHNICAL_ERROR if current.attempted else CaseState.BLOCKED
                current.reason = redact(str(exc), secrets)
                _save_case(store, result, current, secrets)
                return current
            evidence = StepEvidence(
                run_id=run_id, case_id=case.id, requirement_ids=case.requirement_ids,
                script=current.script, script_hash=current.script_hash, plan_hash=result.plan_hash,
                product_version=result.product_version, step_index=index, operation_id=operation.operation_id,
                state=CaseState.INCONCLUSIVE, request=request,
                expected=[assertion.model_dump(mode="json") for assertion in step.assertions],
            )
            # Durable aggregate + start event BEFORE opening the HTTP stream.
            current = current.model_copy(update={"attempted": True, "state": CaseState.INCONCLUSIVE})
            _save_case(store, result, current, secrets)
            start_path = store.append_evidence(evidence, secrets=secrets)
            current.evidence_paths.append(str(start_path))
            result.evidence_paths.append(str(start_path))
            _save_case(store, result, current, secrets)
            started = time.monotonic()
            try:
                with client.stream(operation.method, url, **kwargs) as response:
                    evidence.http_status = response.status_code
                    evidence.response = {"headers": dict(response.headers)}
                    if 300 <= response.status_code < 400:
                        raise SecurityError("Redirect rejected; no follow-up request was sent")
                    raw = bytearray()
                    for chunk in response.iter_bytes(chunk_size=min(65536, profile.max_response_bytes + 1)):
                        if len(raw) + len(chunk) > profile.max_response_bytes:
                            raise ResponseLimitError("Response exceeds configured byte limit; evidence incomplete")
                        raw.extend(chunk)
                raw = bytes(raw)
                body_text = raw.decode("utf-8", errors="replace")
                if raw:
                    try:
                        # Redact decoded values BEFORE JSON serialization: literal
                        # replacement misses quotes, backslashes and Unicode escapes.
                        # This is a detached display copy; comparisons/captures below
                        # deliberately continue to consume the original raw bytes.
                        body_text = json.dumps(redact(json.loads(raw), secrets), ensure_ascii=False)
                    except (ValueError, UnicodeError):
                        body_text = redact(body_text, secrets)
                evidence.response.update({"body_text": body_text, "body_bytes": len(raw)})
                evidence.assertions, evidence.state = evaluate_assertions(step.assertions, response.status_code, raw, variables)
                evidence.observed = [item.observed for item in evidence.assertions]
                if evidence.state == CaseState.PASSED and step.captures:
                    try:
                        document = _json(raw)
                        evidence.captures = {name: deepcopy(json_pointer(document, pointer)) for name, pointer in step.captures.items()}
                        variables.update(deepcopy(evidence.captures))
                    except (ValueError, UnicodeError, KeyError):
                        evidence.state = CaseState.INCONCLUSIVE
                        evidence.error = "Required capture absent or response is not unambiguous JSON"
            except ResponseLimitError as exc:
                evidence.state, evidence.error = CaseState.INCONCLUSIVE, str(exc)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                evidence.state, evidence.error = CaseState.TECHNICAL_ERROR, redact(str(exc), secrets)
            evidence.duration_ms = (time.monotonic() - started) * 1000
            final_path = store.append_evidence(evidence, secrets=secrets)
            current.evidence_paths.append(str(final_path))
            result.evidence_paths.append(str(final_path))
            # Store applies the same field-aware redaction to the final event
            # and its embedded copy; do not pre-redact authoritative metadata.
            current.steps.append(evidence)
            current.state, current.reason = evidence.state, redact(evidence.error, secrets)
            _save_case(store, result, current, secrets)
            if evidence.state != CaseState.PASSED:
                break
    return store.load_run(run_id).cases[next(i for i, item in enumerate(result.cases) if item.case_id == case_id)]


def _terminate(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def _execute(suite_id: str, environment_id: str, store: Store, scripts: list[str] | None = None) -> RunResult:
    from .generate import verify_generated
    suite, manifest, plan = store.verify_suite(suite_id)
    verify_generated(suite, plan)
    profile = store.load_profile(environment_id)
    # Missing credentials are a per-case precondition, not an attempt. Forward
    # only variables that exist, allowing the worker to record a blocked case.
    secrets = [os.environ[name] for name in profile.credentials.values() if os.environ.get(name)]
    # A trusted source plan/profile cannot embed a credential value: snapshots
    # are hash-preserving and therefore cannot be redacted after copying.
    payload = [*(suite / name for name in manifest.files), suite / "manifest.json"]
    snapshot_data = b"".join(path.read_bytes() for path in payload) + profile.model_dump_json().encode()
    variants = {variant.encode() for secret in secrets for variant in (
        secret, json.dumps(secret, ensure_ascii=False)[1:-1],
        json.dumps(secret, ensure_ascii=True)[1:-1], quote(secret, safe=""), quote_plus(secret),
    )}
    if any(variant in snapshot_data for variant in variants):
        raise SecurityError("Credential value is embedded in suite/profile; remove it before execution")
    env = executor_environment(profile, store)
    run = store.create_run(suite_id, environment_id)
    root = store.run_dir(run.run_id) / "suite"
    store.verify_run_integrity(run.run_id)
    verify_generated(root, plan)
    selected = [entry.script for entry in manifest.cases if scripts is None or entry.script in scripts]
    run.status = RunStatus.RUNNING
    store.save_run(run, secrets=secrets)
    env["QA_RUNTIME_RUN"] = run.run_id
    # -I excludes cwd/PYTHONPATH/user-site. Only verified root is inserted by the
    # fixed bootstrap; explicit config/confcutdir/paths disable ancestor config.
    # POSIX lifetime pipe also stops the worker if its parent is SIGKILLed;
    # attempts already on disk remain inconclusive, never silently retried.
    parent_read = parent_write = None
    if os.name == "posix":
        parent_read, parent_write = os.pipe()
        env["QA_RUNTIME_PARENT_FD"] = str(parent_read)
    bootstrap = (
        "import os,sys,threading\n"
        "def parent_watch():\n"
        "    os.read(int(os.environ['QA_RUNTIME_PARENT_FD']), 1)\n"
        "    os._exit(130)\n"
        "if 'QA_RUNTIME_PARENT_FD' in os.environ:\n"
        "    threading.Thread(target=parent_watch, daemon=True).start()\n"
        "sys.path.insert(0, sys.argv.pop(1))\n"
        "import pytest\n"
        "raise SystemExit(pytest.main(sys.argv[1:]))\n"
    )
    args = [sys.executable, "-I", "-B", "-c", bootstrap, str(root), "-c", str(root / "pytest.ini"),
            "--rootdir", str(root), "--confcutdir", str(root), "-p", "no:cacheprovider", "-q", "--tb=no", *selected]
    process = None
    interrupted = False
    error = None
    previous_term = None
    if threading.current_thread() is threading.main_thread():
        previous_term = signal.getsignal(signal.SIGTERM)
        def on_term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, on_term)
    try:
        if selected:
            process = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       start_new_session=(os.name == "posix"),
                                       pass_fds=(parent_read,) if parent_read is not None else ())
            code = process.wait(timeout=profile.process_timeout_seconds)
            if code not in (0, 1):
                interrupted = True
                error = "pytest worker stopped with exit code " + str(code)
        else:
            code = 0
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        interrupted = True
        error = "pytest execution interrupted or exceeded process timeout"
    except OSError:
        interrupted = True
        error = "Could not start isolated pytest worker"
    finally:
        if process is not None:
            _terminate(process)
        for descriptor in (parent_read, parent_write):
            if descriptor is not None:
                os.close(descriptor)
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)
    run = store.load_run(run.run_id)
    # Missing worker results are not successful tests, including pytest collection
    # crashes and a malicious ambient plugin that could otherwise skip tests.
    unexecuted = [case for case in run.cases if case.script in selected and case.state == CaseState.GENERATED]
    counts = {case.id: len(case.steps) for case in plan.cases}
    unfinished = [case for case in run.cases if case.script in selected and case.attempted
                  and case.state == CaseState.INCONCLUSIVE
                  and (not case.steps or (case.steps[-1].state == CaseState.PASSED and len(case.steps) < counts[case.case_id]))]
    run.status = RunStatus.INTERRUPTED if interrupted or unexecuted or unfinished else RunStatus.COMPLETED
    run.error = error or ("Worker did not produce all selected case results" if unexecuted or unfinished else None)
    run.ended_at = utc_now()
    store.save_run(run, secrets=secrets)
    return store.load_run(run.run_id)


def execute_suite(suite_id: str, environment_id: str, *, store: Store | None = None) -> RunResult:
    try:
        return _execute(suite_id, environment_id, store or Store())
    except (StoreError, SecurityError) as exc:
        raise type(exc)("Execution refused; verify suite integrity, explicit approval and local profile") from None
    except ValueError:
        raise IntegrityError("Invalid suite/profile data; regenerate and review local configuration") from None
    except Exception:
        raise RuntimeError("Execution failed; inspect local configuration and persisted run history") from None


def direct_results(root: Path, scripts: list[str]) -> RunResult:
    """Supported direct pytest path: explicit approval, new run, verified snapshot."""
    store = Store()
    try:
        suite_id = store.suite_id_for_path(root)
    except Exception:
        raise IntegrityError("Direct pytest requires a valid registered generated suite") from None
    environment = os.environ.get("QA_ENTORNO")
    if not environment:
        raise SecurityError("QA_ENTORNO is required for direct pytest")
    try:
        return _execute(suite_id, environment, store, scripts)
    except Exception:
        raise RuntimeError("Direct execution refused; verify registration, explicit approval and profile") from None
