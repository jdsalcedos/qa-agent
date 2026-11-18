import asyncio
import os
from pathlib import Path
from secrets import token_hex
import sys
import subprocess

from mcp import Client, StdioServerParameters, stdio_client
import pytest

from qa_agent.mcp_client import execute_via_mcp
from qa_agent.runtime import executor_environment
from runtime_support import local_http, one_case, prepare


def test_real_stdio_discovery_call_and_equivalent_results(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port)
        data = execute_via_mcp(manifest.suite_id, "local", store=store)
        assert set(data) == {"run_id", "status", "result_path", "evidence_paths"}
        assert data["status"] == "completed"
        run = store.load_run(data["run_id"])
        assert run.cases[0].state.value == "passed"
        assert Path(data["result_path"]).is_file()
        assert all(Path(path).is_file() for path in data["evidence_paths"])
        assert len(requests) == 2
        direct = subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", str(tmp_path / "suite" / "test_0000.py")],
                                cwd=tmp_path / "suite", env={"QA_AGENT_HOME": str(store.root), "QA_ENTORNO": "local", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                                capture_output=True, text=True, timeout=20)
        assert direct.returncode == 0, direct.stdout + direct.stderr
        other = store.load_run(next(identifier for identifier in store.list_runs() if identifier != run.run_id))
        assert other.cases[0].state == run.cases[0].state
        assert [step.assertions for step in other.cases[0].steps] == [step.assertions for step in run.cases[0].steps]
        assert [step.captures for step in other.cases[0].steps] == [step.captures for step in run.cases[0].steps]
        assert len(requests) == 4


def test_stdio_rejects_extra_invalid_unknown_and_unapproved(tmp_path):
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, approved=False)
        async def check():
            root = str(Path(__file__).resolve().parents[1])
            bootstrap = "import sys,runpy;sys.path.insert(0,sys.argv[1]);runpy.run_module('qa_agent.mcp_server',run_name='__main__')"
            params = StdioServerParameters(command=sys.executable, args=["-I", "-B", "-c", bootstrap, root],
                                           env=executor_environment(store.load_profile("local"), store), cwd=store.root)
            store.root.mkdir(exist_ok=True)
            with open(os.devnull, "w") as stderr:
                async with Client(stdio_client(params, errlog=stderr), read_timeout_seconds=15) as client:
                    tools = await client.list_tools()
                    assert [tool.name for tool in tools.tools] == ["ejecutar_plan"]
                    for args in [
                        {"suite_id": manifest.suite_id, "entorno_id": "local", "code": "print('no')"},
                        {"suite_id": "../escape", "entorno_id": "local"},
                        {"suite_id": 123, "entorno_id": "local"},
                        {"suite_id": "unknown", "entorno_id": "local"},
                        {"suite_id": manifest.suite_id, "entorno_id": "local"},
                    ]:
                        result = await client.call_tool("ejecutar_plan", args)
                        assert result.is_error or "error" in (result.structured_content or {})
        asyncio.run(check())
        assert requests == []
        assert store.list_runs() == []


def test_mcp_redacts_secret_and_environment_is_allowlisted(tmp_path, monkeypatch):
    secret = token_hex(16)
    monkeypatch.setenv("FIXTURE_AUTH", secret)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-forward")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--invalid-pytest-option")
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()],
                                      profile_changes={"credentials": {"Authorization": "FIXTURE_AUTH"}})
        env = executor_environment(store.load_profile("local"), store)
        assert "UNRELATED_SECRET" not in env
        assert "PYTEST_ADDOPTS" not in env
        data = execute_via_mcp(manifest.suite_id, "local", store=store)
        assert data["status"] == "completed"
        assert requests[0]["auth"] == secret
        for path in store.root.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes()


def test_mcp_missing_credential_records_block_not_attempt(tmp_path, monkeypatch):
    monkeypatch.delenv("FIXTURE_ABSENT", raising=False)
    with local_http() as (port, requests):
        store, manifest, _, _ = prepare(tmp_path, port, cases=[one_case()],
                                      profile_changes={"credentials": {"Authorization": "FIXTURE_ABSENT"}})
        data = execute_via_mcp(manifest.suite_id, "local", store=store)
        run = store.load_run(data["run_id"])
        assert run.cases[0].state.value == "blocked"
        assert not run.cases[0].attempted
        assert requests == []
