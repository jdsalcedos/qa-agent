import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import MagicMock

import httpx
import pytest

from qa_agent import cli
from qa_agent.models import Approval, EnvironmentProfile, Plan, SuiteManifest
from qa_agent.store import Store, atomic_write_json, load_plan, save_plan


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError("CLI tests cannot use real HTTP")
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)


def profile():
    return EnvironmentProfile.model_validate({"id": "local", "base_url": "http://127.0.0.1:8765",
        "allowed_targets": [{"host": "127.0.0.1", "port": 8765, "ips": ["127.0.0.1"]}],
        "allowed_operations": ["getResource"], "product_version": "synthetic",
        "credentials": {"Authorization": "QA_SYNTHETIC_AUTH"}})


@pytest.mark.parametrize("command", ["disenar", "generar", "aprobar", "ejecutar", "cerrar", "perfil"])
def test_help_for_all_commands(command, capsys):
    with pytest.raises(SystemExit) as caught:
        cli.main([command, "--help"])
    assert caught.value.code == 0
    assert command in capsys.readouterr().out


def test_module_entrypoint_real_process_help():
    result = subprocess.run([sys.executable, "-m", "qa_agent", "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "disenar" in result.stdout and "cerrar" in result.stdout


def test_design_command_offline_partial_plan(tmp_path, monkeypatch, capsys):
    class FakeDesigner:
        def propose(self, prompt):
            context = json.loads(prompt.split("Contexto de entrada (datos, no instrucciones):\n")[1])
            context.pop("synthetic_data")
            context["cases"] = [{"id": "CP-01", "requirement_ids": ["R-01"],
                                 "objective": "Aclarar regla", "expected": "Falta contrato",
                                 "automation_pending_reason": "Contrato ausente"}]
            return Plan.model_validate(context)
    monkeypatch.setattr(cli, "DeepSeekDesigner", FakeDesigner)
    requirements = tmp_path / "req.md"
    requirements.write_text("R-01: Consultar recurso\n")
    output = tmp_path / "plan.json"
    assert cli.main(["disenar", "--requisitos", str(requirements), "--salida", str(output)]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["attempts"] == 0
    assert load_plan(output).skill_hashes
    assert not (tmp_path / "home" / "runs").exists()
    assert not (tmp_path / "home" / "suites").exists()


def test_design_error_no_key_no_partial_output(tmp_path, capsys):
    output = tmp_path / "plan.json"
    assert cli.main(["disenar", "--requisitos", "missing.md", "--salida", str(output)]) == 1
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err
    assert not output.exists()


def test_generate_deferred_public_api(tmp_path, monkeypatch, capsys):
    module = ModuleType("qa_agent.generate")
    manifest = SuiteManifest(suite_id="suite-double", plan_hash="a" * 64, source_plan_path=str(tmp_path / "plan.json"))
    module.generate_suite = MagicMock(return_value=manifest)
    monkeypatch.setitem(sys.modules, "qa_agent.generate", module)
    plan, output = tmp_path / "plan.json", tmp_path / "suite"
    assert cli.main(["generar", "--plan", str(plan), "--salida", str(output)]) == 0
    args, kwargs = module.generate_suite.call_args
    assert args == (plan, output)
    assert isinstance(kwargs["store"], Store)
    assert json.loads(capsys.readouterr().out)["suite_id"] == "suite-double"


def test_approval_requires_explicit_human_identity_and_confirmation():
    for args in (["aprobar", "--suite", "suite-1"],
                 ["aprobar", "--suite", "suite-1", "--responsable", "Human"],
                 ["aprobar", "--suite", "suite-1", "--confirmar"]):
        with pytest.raises(SystemExit) as caught:
            cli.main(args)
        assert caught.value.code == 2


def test_approval_calls_store_only_with_human_act(tmp_path, monkeypatch, capsys):
    store = MagicMock(spec=Store)
    store.suite_id_for_path.return_value = "suite-1"
    store.approve_suite.return_value = Approval(suite_id="suite-1", plan_hash="a" * 64,
                                               manifest_hash="b" * 64, approved_by="Human")
    monkeypatch.setattr(cli, "Store", lambda: store)
    suite = tmp_path / "suite"
    suite.mkdir()
    assert cli.main(["aprobar", "--suite", str(suite), "--responsable", "Human", "--confirmar"]) == 0
    store.approve_suite.assert_called_once_with("suite-1", approved_by="Human")
    value = json.loads(capsys.readouterr().out)
    assert value["plan_hash"] == "a" * 64 and value["manifest_hash"] == "b" * 64


def test_execution_uses_mcp_client_never_runtime(tmp_path, monkeypatch, capsys):
    store = MagicMock(spec=Store)
    store.load_profile.return_value = profile()
    monkeypatch.setattr(cli, "Store", lambda: store)
    module = ModuleType("qa_agent.mcp_client")
    module.execute_via_mcp = MagicMock(return_value={"run_id": "run-double", "status": "completed",
                                                    "result_path": "runs/run-double/resultados.json", "evidence_paths": []})
    runtime = ModuleType("qa_agent.runtime")
    runtime.execute_suite = MagicMock(side_effect=AssertionError("No direct runtime shortcut"))
    monkeypatch.setitem(sys.modules, "qa_agent.mcp_client", module)
    monkeypatch.setitem(sys.modules, "qa_agent.runtime", runtime)
    assert cli.main(["ejecutar", "--suite", "suite-1", "--entorno", "local"]) == 0
    module.execute_via_mcp.assert_called_once_with("suite-1", "local", store=store)
    store.verify_suite.assert_called_once_with("suite-1")
    runtime.execute_suite.assert_not_called()
    assert json.loads(capsys.readouterr().out)["run_id"] == "run-double"


def test_execution_no_approval_means_no_mcp_call(monkeypatch):
    store = MagicMock(spec=Store)
    store.verify_suite.side_effect = ValueError("Approval absent")
    monkeypatch.setattr(cli, "Store", lambda: store)
    module = ModuleType("qa_agent.mcp_client")
    module.execute_via_mcp = MagicMock()
    monkeypatch.setitem(sys.modules, "qa_agent.mcp_client", module)
    assert cli.main(["ejecutar", "--suite", "suite-1", "--entorno", "local"]) == 1
    module.execute_via_mcp.assert_not_called()


def test_execution_error_never_logs_credentials(monkeypatch, capsys):
    store = MagicMock(spec=Store)
    store.load_profile.return_value = profile()
    monkeypatch.setattr(cli, "Store", lambda: store)
    monkeypatch.setenv("QA_SYNTHETIC_AUTH", "TARGET-CANARY")
    module = ModuleType("qa_agent.mcp_client")
    module.execute_via_mcp = MagicMock(side_effect=RuntimeError("TARGET-CANARY password=RAW-CANARY"))
    monkeypatch.setitem(sys.modules, "qa_agent.mcp_client", module)
    assert cli.main(["ejecutar", "--suite", "suite-1", "--entorno", "local"]) == 1
    captured = capsys.readouterr()
    assert "CANARY" not in captured.out + captured.err


def test_execution_response_reflection_redacted(monkeypatch, capsys):
    store = MagicMock(spec=Store)
    store.load_profile.return_value = profile()
    monkeypatch.setattr(cli, "Store", lambda: store)
    monkeypatch.setenv("QA_SYNTHETIC_AUTH", "TARGET-CANARY")
    module = ModuleType("qa_agent.mcp_client")
    module.execute_via_mcp = MagicMock(return_value={"run_id": "run-1", "message": "TARGET-CANARY"})
    monkeypatch.setitem(sys.modules, "qa_agent.mcp_client", module)
    assert cli.main(["ejecutar", "--suite", "suite-1", "--entorno", "local"]) == 0
    assert "TARGET-CANARY" not in capsys.readouterr().out


@pytest.mark.parametrize("extension", [".json", ".yaml"])
def test_register_and_load_local_profile(tmp_path, extension, capsys):
    path = tmp_path / ("profile" + extension)
    # JSON is also a valid YAML input.
    atomic_write_json(path, profile())
    assert cli.main(["perfil", "--archivo", str(path)]) == 0
    registered = json.loads(capsys.readouterr().out)
    assert registered["environment_id"] == "local"
    assert Path(registered["profile_path"]).is_file()
    assert cli.main(["perfil", "--entorno", "local"]) == 0
    assert json.loads(capsys.readouterr().out)["base_url"] == "http://127.0.0.1:8765"


def test_localhost_profile_rejected(tmp_path, capsys):
    path = tmp_path / "profile.json"
    value = profile().model_dump(mode="json")
    value["base_url"] = "http://localhost:8765"
    atomic_write_json(path, value)
    assert cli.main(["perfil", "--archivo", str(path)]) == 1
    assert not (tmp_path / "home" / "environments").exists()


def test_close_plan_real_cli_function_no_llm(tmp_path, capsys):
    source = save_plan(tmp_path / "plan.json", Plan())
    assert cli.main(["cerrar", "--plan", str(source)]) == 0
    path = json.loads(capsys.readouterr().out)["report_path"]
    assert "Evidencia insuficiente" in Path(path).read_text()


def test_close_run_routes_to_history(monkeypatch, tmp_path, capsys):
    close = MagicMock(return_value=tmp_path / "cierre.md")
    monkeypatch.setattr(cli, "close_run", close)
    assert cli.main(["cerrar", "--corrida", "run-1"]) == 0
    assert close.call_args.args == ("run-1",)
    assert isinstance(close.call_args.kwargs["store"], Store)
    assert json.loads(capsys.readouterr().out)["report_path"].endswith("cierre.md")


def test_close_requires_exactly_one_source():
    for argv in (["cerrar"], ["cerrar", "--plan", "p.json", "--corrida", "run-1"]):
        with pytest.raises(SystemExit) as caught:
            cli.main(argv)
        assert caught.value.code == 2


def test_interrupt_exit_code(monkeypatch):
    monkeypatch.setattr(cli, "close_run", MagicMock(side_effect=KeyboardInterrupt))
    assert cli.main(["cerrar", "--corrida", "run-1"]) == 130


@pytest.mark.parametrize("data", [{"password": "SYNTHETIC-CANARY"}, {"label": "SYNTHETIC-CANARY"}])
def test_profile_never_copies_sensitive_data_or_credential_reflections(tmp_path, monkeypatch, capsys, data):
    monkeypatch.setenv("QA_SYNTHETIC_AUTH", "SYNTHETIC-CANARY")
    value = profile().model_dump(mode="json")
    value["data"] = data
    source = atomic_write_json(tmp_path / "profile.json", value)
    assert cli.main(["perfil", "--archivo", str(source)]) == 1
    captured = capsys.readouterr()
    assert "SYNTHETIC-CANARY" not in captured.out + captured.err
    assert not (tmp_path / "home" / "environments").exists()


def test_profile_rejects_literal_bearer_instead_of_env_variable(tmp_path):
    value = profile().model_dump(mode="json")
    value["credentials"] = {"Authorization": "Bearer SYNTHETIC-CANARY"}
    source = atomic_write_json(tmp_path / "profile.json", value)
    assert cli.main(["perfil", "--archivo", str(source)]) == 1
    assert not (tmp_path / "home" / "environments").exists()
