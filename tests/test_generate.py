import ast
from pathlib import Path

import pytest

from qa_agent.generate import generate_suite, verify_generated
from qa_agent.models import Case
from qa_agent.store import IntegrityError, Store, suite_files
from runtime_support import prepare, one_case


def test_export_complete_offline_and_pending(tmp_path, monkeypatch):
    import httpx
    def forbidden(*args, **kwargs):
        raise AssertionError("Generation must not contact HTTP")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    pending = Case(id="browser", requirement_ids=["R"], objective="browser", expected="manual", automation_pending_reason="browser unsupported")
    store, manifest, plan, _ = prepare(tmp_path, 8765, cases=[one_case(), pending])
    root = tmp_path / "suite"
    assert [entry.case_id for entry in manifest.cases] == ["C"]
    assert {"plan.json", "pytest.ini", "requirements.lock", "conftest.py", "_qa_runtime/runtime.py"} <= set(manifest.files)
    assert "manifest.json" not in manifest.files
    assert len(list(root.glob("test_*.py"))) == 1
    assert suite_files(root) == manifest.files
    verify_generated(root, plan)
    assert not store.list_runs()
    for path in root.rglob("*.py"):
        ast.parse(path.read_text())


def test_generation_deterministic_payload_and_no_overwrite(tmp_path):
    store, first, _, source = prepare(tmp_path, 8765)
    second = generate_suite(source, tmp_path / "second", store=store)
    assert first.suite_id != second.suite_id
    assert first.files == second.files
    with pytest.raises(FileExistsError):
        generate_suite(source, tmp_path / "suite", store=store)


def test_plan_strings_are_not_python(tmp_path):
    case = one_case(data={"payload": "'); __import__('os').system('false') #\n{}"})
    _, _, plan, _ = prepare(tmp_path, 8765, cases=[case])
    text = (tmp_path / "suite" / "test_0000.py").read_text()
    assert "__import__" not in text
    assert "qa_runtime('C')" in text
    verify_generated(tmp_path / "suite", plan)


def test_trusted_generator_rejects_extra_code_even_registered(tmp_path):
    _, _, plan, _ = prepare(tmp_path, 8765)
    (tmp_path / "suite" / "conftest.py").write_text("# changed")
    with pytest.raises(IntegrityError, match="trusted generator"):
        verify_generated(tmp_path / "suite", plan)


def test_same_punctuation_case_ids_cannot_collide(tmp_path):
    first = one_case()
    second = first.model_copy(update={"id": "c"})
    _, manifest, _, _ = prepare(tmp_path, 8765, cases=[first, second])
    assert len({entry.script for entry in manifest.cases}) == 2
