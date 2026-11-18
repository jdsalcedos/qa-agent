#!/usr/bin/env python3
"""Independent manual acceptance fixture; never invokes an AI designer.

Run with qa-agent/.venv/bin/python. No installation, oracle imports or shared
servers. All generated state is temporary unless --artifacts is requested.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import runpy
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "acceptance"
DEMO_API = ROOT / "examples" / "cli-flow" / "todo-api"
ORDER = ["CP-02", "CP-01", "CP-03", "CP-04", "CP-05", "CP-06"]
SEED = [{"id": 1, "title": "Preparar demo", "completed": False},
        {"id": 2, "title": "Revisar contrato", "completed": True}]
# An explicit source checkout path, not the caller's PYTHONPATH.
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def manual_plan():
    """Materialize static manually specified cases with the public contract.

    No evaluator/oracle or defect description is consulted here. The cases are
    a hand-authored acceptance fixture, NOT output of the AI design pipeline.
    """
    from qa_agent.models import Plan
    from qa_agent.openapi import load_openapi, validate_plan
    contract = load_openapi(DEMO_API / "openapi.json")
    require(not contract.pending_questions, "Public OpenAPI has unresolved operations")
    public = (DEMO_API / "docs" / "requirements.md").read_text(encoding="utf-8")
    requirements = [{"id": key, "text": text.strip()} for key, text in
                    re.findall(r"^\| (TD-\d{2}) \| (.*?) \|$", public, re.MULTILINE)]
    require(len(requirements) == 6, "Expected the six public TD requirements")
    cases = json.loads((FIXTURE / "manual_cases.json").read_text(encoding="utf-8"))
    plan = Plan(requirements=requirements, contract_hash=contract.contract_hash,
                operations=contract.operations, cases=cases)
    require([case.id for case in plan.cases] == ORDER, "CP-02 must run on untouched seed")
    return validate_plan(plan, contract)


def preflight():
    """Fail immediately: do not wait for files being implemented elsewhere."""
    required = {"generate": "generate_suite", "runtime": "execute_suite",
                "mcp_client": "execute_via_mcp", "cli": "main", "report": "close_run"}
    missing = []
    modules = {}
    for module, symbol in required.items():
        try:
            loaded = importlib.import_module("qa_agent." + module)
            if not callable(getattr(loaded, symbol, None)):
                raise ImportError(f"missing callable {symbol}")
            modules[module] = loaded
        except (ImportError, OSError) as exc:
            missing.append(f"qa_agent.{module}.{symbol}: {exc}")
    for module in ("qa_agent.__main__", "qa_agent.mcp_server"):
        if importlib.util.find_spec(module) is None:
            missing.append(module)
    for module in ("fastapi", "uvicorn"):
        if importlib.util.find_spec(module) is None:
            missing.append(f"{module} (instalar el extra demo)")
    if missing:
        raise RuntimeError("Acceptance dependencies unavailable (no polling):\n  " + "\n  ".join(missing))
    return modules


def stop_owned(process):
    """Only the process group created by this harness, including descendants."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
    # The leader may exit before a descendant; kill any remaining owned group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def command(args, *, env, log, timeout=180):
    with log.open("wb") as output:
        process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        finally:
            stop_owned(process)


@contextmanager
def disposable_api(variant, env, directory):
    """Reserve and pass the listening socket: no close/rebind free-port race."""
    import httpx
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    process = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        child_env = {**env, "TODO_VARIANT": variant, "PYTHONDONTWRITEBYTECODE": "1"}
        with (directory / "server.log").open("wb") as output:
            process = subprocess.Popen(
                [sys.executable, "-B", "-m", "uvicorn",
                 "app.main:app", "--fd", str(listener.fileno()), "--workers", "1", "--no-access-log"],
                cwd=DEMO_API, env=child_env, pass_fds=(listener.fileno(),),
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 12
            with httpx.Client(timeout=0.4, trust_env=False) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Disposable API exited; see {directory / 'server.log'}")
                    try:
                        response = client.get(base + "/tasks")
                        require(response.status_code == 200 and response.json() == SEED,
                                "Disposable API must start with exactly the public seed")
                        break
                    except httpx.TransportError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Disposable API readiness exceeded 12 seconds")
                        time.sleep(0.05)
            yield base, port
    finally:
        if process is not None:
            stop_owned(process)
        listener.close()


def profile(store, name, base, port, variant, *, seed_ready=True):
    from qa_agent.models import EnvironmentProfile
    value = EnvironmentProfile(
        id=name, base_url=base,
        allowed_targets=[{"host": "127.0.0.1", "port": port, "ips": ["127.0.0.1"]}],
        allowed_operations=[op.operation_id for op in manual_plan().operations],
        product_version=variant, preconditions={"seed_ready": seed_ready},
        request_timeout_seconds=2, process_timeout_seconds=90)
    store.save_profile(value)
    return value


def via_mcp(store, manifest, environment_id, env, directory):
    # This child invokes the PUBLIC MCP CLIENT, not execute_suite. Its own
    # stdio server and pytest descendants are confined to our process group.
    response_path = directory / "mcp-response.json"
    code = (
        "import asyncio, inspect, json, sys\n"
        "from qa_agent.mcp_client import execute_via_mcp\n"
        "from qa_agent.store import Store\n"
        "value = execute_via_mcp(sys.argv[1], sys.argv[2], store=Store())\n"
        "if inspect.isawaitable(value): value = asyncio.run(value)\n"
        "with open(sys.argv[3], 'w') as f: json.dump(value, f)\n")
    rc = command([sys.executable, "-B", "-c", code, manifest.suite_id, environment_id,
                  str(response_path)], env=env, log=directory / "mcp.log")
    require(rc == 0, f"Real MCP client failed ({rc}); see {directory / 'mcp.log'}")
    response = json.loads(response_path.read_text())
    require(isinstance(response, dict) and response.get("run_id"), "MCP must return a run_id")
    result = store.load_run(response["run_id"])
    require(response.get("status") == result.status.value, "MCP status differs from durable result")
    require(response.get("result_path") and "evidence_paths" in response, "MCP omitted artifact references")
    return result


def validate_run(store, manifest, result, variant, *, selected=None):
    ids = selected or ORDER
    require(result.suite_id == manifest.suite_id and result.plan_hash == manifest.plan_hash,
            "Run must identify the unchanged suite/plan")
    require(result.product_version == variant, "Run must record target product variant")
    require([case.case_id for case in result.cases] == ORDER, "Run must preserve full planned membership/order")
    require(result.ended_at is not None, "Run must be finalized")
    expected = {case_id: ("failed" if variant == "lost_completion" and case_id == "CP-03" else "passed")
                for case_id in ids}
    mappings = {case.case_id: case for case in manifest.cases}
    for case in result.cases:
        if case.case_id not in ids:
            require(case.state.value == "generated" and not case.attempted
                    and not case.steps and not case.evidence_paths,
                    "Unselected standalone cases must remain generated, without attempts")
            continue
        require(case.state.value == expected[case.case_id] and case.attempted,
                f"{variant}/{case.case_id}: expected {expected[case.case_id]}, got {case.state.value}")
        mapping = mappings[case.case_id]
        require(case.script == mapping.script and case.script_hash == mapping.script_hash,
                "Result lost script traceability")
        require(case.requirement_ids == mapping.requirement_ids, "Result lost requirement traceability")
        require(case.steps and case.evidence_paths, "Every verdict needs persisted step evidence")
        persisted = [store.load_evidence(result.run_id, path) for path in case.evidence_paths]
        for event in persisted:
            require(event.case_id == case.case_id and event.step_index < len(case.steps),
                    "Journal event must trace a recorded case step")
        for step in case.steps:
            # Runtime durably journals an inconclusive start BEFORE HTTP, then
            # the terminal observation. Neither path count nor zip is a bijection.
            matches = [event for event in persisted if event.model_dump() == step.model_dump()]
            require(len(matches) == 1, "Each terminal step needs exactly one identical persisted observation")
            require(step.plan_hash == manifest.plan_hash and step.script_hash == mapping.script_hash,
                    "Evidence lost plan/script hashes")
            require(step.product_version == variant and step.case_id == case.case_id,
                    "Evidence lost case/product traceability")
            require(step.http_status is not None and step.assertions, "Missing observed HTTP/assertions")
            require(all(a.passed for a in step.assertions) == (step.state.value == "passed"),
                    "Step state disagrees with its comparisons")
    cp3 = next((case for case in result.cases if case.case_id == "CP-03" and case.attempted), None)
    if cp3:
        require([s.operation_id for s in cp3.steps] == ["createTask", "updateTask", "getTask"],
                "CP-03 must record PATCH and subsequent GET")
        require(cp3.steps[0].captures.get("task_id") is not None, "Creation ID must be captured")
        require(cp3.steps[1].http_status == cp3.steps[2].http_status == 200, "Persistence bug is not HTTP failure")
        completion = [a for a in cp3.steps[2].assertions if a.kind == "equal" and a.expected is True]
        require(len(completion) == 1 and completion[0].observed is (variant == "reference"),
                "CP-03 GET must expose expected true versus observed boolean")
    store.verify_run_integrity(result.run_id)


def comparison(result):
    """Exclude run IDs/timestamps/captured IDs but retain actual comparisons."""
    return {case.case_id: (case.state.value, [
        (step.operation_id, step.http_status,
         [(a.kind, a.passed, a.expected, a.observed) for a in step.assertions])
        for step in case.steps]) for case in result.cases}


def close_offline(store, run, env, directory, *, expected_text):
    # No target process is alive. The child denies ALL Internet sockets and DNS;
    # even a swallowed network attempt is detected by the audit log below.
    from qa_agent.store import hash_file
    before_runs = store.list_runs()
    before = hash_file(store.run_dir(run.run_id) / "resultados.json")
    offline = {**env, "QA_ACCEPTANCE_NETWORK": "none"}
    rc = command([sys.executable, "-B", "-m", "qa_agent", "cerrar", "--corrida", run.run_id],
                 env=offline, log=directory / "close.log", timeout=30)
    require(rc == 0, f"Offline close failed; see {directory / 'close.log'}")
    report = store.run_dir(run.run_id) / "cierre.md"
    text = report.read_text(encoding="utf-8")
    require(expected_text.casefold() in text.casefold(), "Close recommendation mismatch")
    require("TD-04" in text, "Close must trace persistence requirement")
    if any(case.case_id == "CP-03" and case.state.value == "failed" for case in run.cases):
        require("CP-03" in text and re.search(r"false|falso", text, re.IGNORECASE),
                "Failed persistence close must cite CP-03 and the observed false value")
    require(before_runs == store.list_runs() and before == hash_file(store.run_dir(run.run_id) / "resultados.json"),
            "Close must not create attempts or mutate results")
    return hash_file(report)


def acceptance(work, modules):
    from qa_agent.store import Store, hash_json, save_plan
    store = Store(work / "store")
    env = dict(os.environ)
    source = save_plan(work / "manual-plan.json", manual_plan())
    require(not store.list_runs(), "Generation must start without attempts")
    os.environ["QA_ACCEPTANCE_NETWORK"] = "none"
    try:
        manifest = modules["generate"].generate_suite(source, work / "suite", store=store)
    finally:
        os.environ.pop("QA_ACCEPTANCE_NETWORK", None)
    require(not store.list_runs(), "Generation must not execute any cases")
    # User explicitly authorized this acceptance fixture only. Never approve
    # arbitrary discovered suites or infer release approval from this action.
    store.approve_suite(manifest.suite_id, approved_by="authorized-manual-todo-acceptance")
    original = hash_json(manifest)
    results = {}
    for variant in ("reference", "lost_completion"):
        for route in ("mcp", "pytest", "pytest-cp03"):
            directory = work / f"{variant}-{route}"
            directory.mkdir()
            name = f"{variant}-{route}"
            with disposable_api(variant, env, directory) as (base, port):
                profile(store, name, base, port, variant)
                if route == "mcp":
                    result = via_mcp(store, manifest, name, env, directory)
                else:
                    before = set(store.list_runs())
                    scripts = [case.script for case in manifest.cases
                               if route == "pytest" or case.case_id == "CP-03"]
                    rc = command([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                  *[str(work / "suite" / script) for script in scripts]],
                                 env={**env, "QA_ENTORNO": name}, log=directory / "pytest.log")
                    require(rc == (1 if variant == "lost_completion" else 0),
                            f"Unexpected direct pytest exit {rc}; see {directory / 'pytest.log'}")
                    created = set(store.list_runs()) - before
                    require(len(created) == 1, "Direct pytest must persist exactly one new run")
                    result = store.load_run(created.pop())
                validate_run(store, manifest, result, variant,
                             selected=["CP-03"] if route == "pytest-cp03" else None)
                results[name] = result
            # Server has been reaped before report construction/regeneration.
            expected = ("Aplazar liberación" if variant == "lost_completion" else
                        "Evidencia insuficiente" if route == "pytest-cp03" else "liberación")
            close_offline(store, result, env, directory, expected_text=expected)
            close_offline(store, result, env, directory, expected_text=expected)
        full = comparison(results[f"{variant}-mcp"])
        require(full == comparison(results[f"{variant}-pytest"]), "MCP/direct comparisons differ")
        standalone = comparison(results[f"{variant}-pytest-cp03"])["CP-03"]
        # The full suite creates CP-01 first, so its captured CP-03 ID differs
        # from the standalone ID. Compare the state and all PATCH/GET assertions.
        require(full["CP-03"][0] == standalone[0] and full["CP-03"][1][1:] == standalone[1][1:],
                "Standalone CP-03 persistence comparisons differ")
        if variant == "reference":
            text = (store.run_dir(results[f"{variant}-mcp"].run_id) / "cierre.md").read_text().casefold()
            require("aplazar liberación" not in text and "evidencia insuficiente" not in text,
                    "Reference close must recommend release within tested scope")
    # Controls use the SAME approved suite, not a silently regenerated plan.
    # Bound-but-not-listening socket cannot receive HTTP and prevents port reuse.
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        for control in ("blocked", "technical"):
            directory = work / control
            directory.mkdir()
            profile(store, control, f"http://127.0.0.1:{port}", port, "reference",
                    seed_ready=control != "blocked")
            control_env = {**env, "QA_ACCEPTANCE_NETWORK": "none"} if control == "blocked" else env
            result = via_mcp(store, manifest, control, control_env, directory)
            require(len(result.cases) == 6, "Control must preserve all planned cases")
            for case in result.cases:
                require(case.state.value == ("blocked" if control == "blocked" else "technical_error"),
                        "Control state incorrect")
                require(case.attempted == (control == "technical"), "Control attempted flag incorrect")
                require(all(step.http_status is None for step in case.steps), "No synthetic HTTP response")
                if control == "blocked":
                    require(not case.steps and not case.evidence_paths, "Blocked case must send zero requests")
                else:
                    require(case.steps and case.evidence_paths, "Technical attempt must retain evidence")
            close_offline(store, result, env, directory, expected_text="Evidencia insuficiente")
            results[control] = result
    _, final_manifest, _ = store.verify_suite(manifest.suite_id)
    require(hash_json(final_manifest) == original, "All runs must reuse the frozen suite")
    audit = work / "network-denials.log"
    require(not audit.exists(), "Forbidden network attempt detected; see network-denials.log")
    summary = {name: {"run_id": run.run_id,
                      "result_path": f"store/runs/{run.run_id}/resultados.json",
                      "close_path": f"store/runs/{run.run_id}/cierre.md",
                      "states": {c.case_id: c.state.value for c in run.cases}}
               for name, run in results.items()}
    (work / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    from qa_agent.store import hash_file
    verification = {
        "suite_id": manifest.suite_id, "plan_hash": manifest.plan_hash,
        "manifest_hash": original, "manual_not_ai_designed": True,
        "complete_mcp_pytest_equivalence": True, "standalone_cp03_equivalence": True,
        "offline_close_regeneration": True, "network_denials": 0,
        "files": {path.relative_to(work).as_posix(): hash_file(path)
                  for path in sorted(work.rglob("*")) if path.is_file()},
    }
    (work / "verification.json").write_text(json.dumps(verification, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


@contextmanager
def isolated_environment(work):
    original = dict(os.environ)
    home = work / "home"
    home.mkdir()
    # No DeepSeek/OpenAI key, proxy, dotenv home, pytest plugin/config injection
    # or credentials are inherited. Only new local targets can be contacted.
    env = {"PATH": os.defpath, "HOME": str(home), "TMPDIR": str(work),
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "QA_AGENT_HOME": str(work / "store"),
           "PYTHONPATH": os.pathsep.join([str(FIXTURE / "network_guard"), str(ROOT)]),
           "QA_ACCEPTANCE_NETWORK_LOG": str(work / "network-denials.log")}
    os.environ.clear()
    os.environ.update(env)
    try:
        runpy.run_path(str(FIXTURE / "network_guard" / "sitecustomize.py"))
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true", help="Validate manual fixture offline; runtime not needed")
    parser.add_argument("--artifacts", type=Path, help="Copy temporary artifacts here, also on failure (must not exist)")
    args = parser.parse_args(argv)
    if args.artifacts:
        require(not args.artifacts.exists(), "--artifacts destination must not exist")
        require(not args.artifacts.resolve().is_relative_to(DEMO_API),
                "Never write artifacts inside todo-api")
    with tempfile.TemporaryDirectory(prefix="qa-todo-acceptance-") as temporary:
        work = Path(temporary)
        try:
            with isolated_environment(work):
                if args.plan_only:
                    plan = manual_plan()
                    print(f"Manual fixture valid (NOT AI-designed): {len(plan.cases)} cases, order {', '.join(ORDER)}; zero attempts")
                else:
                    acceptance(work, preflight())
        finally:
            if args.artifacts:
                import shutil
                shutil.copytree(work, args.artifacts)
                print(f"Artifacts: {args.artifacts.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, OSError, ValueError) as error:
        print(f"ACCEPTANCE FAILED: {error}", file=sys.stderr)
        raise SystemExit(1)
