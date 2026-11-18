import json

import pytest
from pydantic import ValidationError

from qa_agent.models import (AssertionResult, Case, CaseState, EnvironmentProfile, Operation, Plan, Requirement,
                             Step, StepEvidence, SuiteCase, SuiteManifest)
from qa_agent.store import (ApprovalError, IntegrityError, Store, StoreError, atomic_write_json,
                            canonical_json, hash_json, load_plan, plan_hash, read_json,
                            safe_suite_file, save_plan, suite_files)


def registered(tmp_path):
    store = Store(tmp_path / "state")
    plan = Plan(requirements=[Requirement(id="R", text="read")], contract_hash="a" * 64,
                operations=[Operation(operation_id="read", method="GET", path="/resources")],
                cases=[Case(id="C", requirement_ids=["R"], objective="Read", expected="200", steps=[Step(operation_id="read", assertions=[{"status": 200}])])])
    source = save_plan(tmp_path / "source.json", plan)
    output = tmp_path / "suite"
    output.mkdir()
    save_plan(output / "plan.json", plan)
    (output / "test_c.py").write_text("def test_case():\n    pass\n")
    (output / "common.py").write_text("# generated common runtime\n")
    (output / "pytest.ini").write_text("[pytest]\n")
    (output / "requirements.lock").write_text("pytest==9.2.0\n")
    files = suite_files(output)
    manifest = SuiteManifest(suite_id="suite-test", plan_hash=plan_hash(plan), source_plan_path=str(source), files=files,
                             cases=[SuiteCase(case_id="C", requirement_ids=["R"], script="test_c.py", script_hash=files["test_c.py"])])
    store.register_suite(output, manifest)
    profile = EnvironmentProfile(id="local", base_url="http://127.0.0.1:8765", allowed_targets=[{"host": "127.0.0.1", "port": 8765, "ips": ["127.0.0.1"]}], allowed_operations=["read"], product_version="reference")
    store.save_profile(profile)
    return store, output, source, manifest, plan


def test_canonical_hash_and_no_coercion(tmp_path):
    assert canonical_json({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert hash_json({"b": 2, "a": 1}) == hash_json({"a": 1, "b": 2})
    assert hash_json([1, 2]) != hash_json([2, 1])
    assert hash_json(True) != hash_json(1)
    assert hash_json(1) != hash_json(1.0)
    store, _, source, _, plan = registered(tmp_path)
    source.write_text(json.dumps(plan.model_dump(mode="json", by_alias=True), indent=4))
    assert plan_hash(load_plan(source)) == plan_hash(plan)
    path = store.save_plan(plan)
    with pytest.raises(FileExistsError):
        store.save_plan(plan, path.parent.name)


def test_atomic_failure_preserves_previous_file(tmp_path, monkeypatch):
    import qa_agent.store as module
    target = atomic_write_json(tmp_path / "record.json", {"old": True})
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write_json(target, {"new": True})
    assert read_json(target) == {"old": True}
    assert list(tmp_path.glob(".qa-tmp-*")) == []


def test_registration_approval_and_separate_trusted_registry(tmp_path):
    store, output, _, manifest, _ = registered(tmp_path)
    with pytest.raises(ApprovalError):
        store.verify_suite(manifest.suite_id)
    assert store.verify_suite(manifest.suite_id, require_approval=False)[0] == output
    approval = store.approve_suite(manifest.suite_id, approved_by="human reviewer")
    assert approval.plan_hash == manifest.plan_hash
    assert approval.manifest_hash == hash_json(manifest)
    assert store.verify_suite(manifest.suite_id)[1] == manifest
    assert store.suite_id_for_path(output) == manifest.suite_id
    assert not (output / "approval.json").exists()
    with pytest.raises(FileExistsError):
        store.register_suite(output, manifest)


@pytest.mark.parametrize("relative", ["plan.json", "test_c.py", "common.py", "pytest.ini", "requirements.lock", "manifest.json"])
def test_every_payload_and_manifest_tamper_detected(tmp_path, relative):
    store, output, _, manifest, _ = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    with (output / relative).open("a") as handle:
        handle.write("\n# malicious change\n")
    with pytest.raises((IntegrityError, ValueError)):
        store.verify_suite(manifest.suite_id)


def test_rehashing_manifest_does_not_reauthorize(tmp_path):
    store, output, _, manifest, _ = registered(tmp_path)
    (output / "test_c.py").write_text("# replaced\n")
    manifest.files = suite_files(output)
    manifest.cases[0].script_hash = manifest.files["test_c.py"]
    atomic_write_json(output / "manifest.json", manifest)
    with pytest.raises(IntegrityError, match="trusted registration"):
        store.verify_suite(manifest.suite_id, require_approval=False)


@pytest.mark.parametrize("field,value", [("objective", "new objective"), ("mandatory", False), ("priority", "low"), ("data", {"new": 1})])
def test_source_plan_change_invalidates_approval_even_copy_unchanged(tmp_path, field, value):
    store, _, source, manifest, plan = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    changed = plan.model_dump(mode="json")
    changed["cases"][0][field] = value
    atomic_write_json(source, changed)
    with pytest.raises(IntegrityError, match="regenerate"):
        store.verify_suite(manifest.suite_id)


def test_missing_original_and_extra_file_block_execution(tmp_path):
    store, output, source, manifest, _ = registered(tmp_path)
    (output / "conftest.py").write_text("# unexpected config\n")
    with pytest.raises(IntegrityError):
        store.verify_suite(manifest.suite_id, require_approval=False)
    (output / "conftest.py").unlink()
    source.unlink()
    with pytest.raises(IntegrityError):
        store.verify_suite(manifest.suite_id, require_approval=False)


@pytest.mark.parametrize("relative", ["../escape.py", "/tmp/escape.py", "a/../../escape", "a\\evil", "a//b", "./a"])
def test_traversal_blocked(tmp_path, relative):
    with pytest.raises(IntegrityError):
        safe_suite_file(tmp_path, relative)


def test_symlink_and_copied_suite_rejected(tmp_path):
    store, output, _, manifest, _ = registered(tmp_path)
    (output / "linked.py").symlink_to(tmp_path / "source.json")
    with pytest.raises(IntegrityError):
        store.verify_suite(manifest.suite_id, require_approval=False)
    (output / "linked.py").unlink()
    import shutil
    copied = tmp_path / "copy"
    shutil.copytree(output, copied)
    with pytest.raises(IntegrityError):
        store.suite_id_for_path(copied)


@pytest.mark.parametrize("identifier", ["../evil", "/tmp/a", "https://example.test", "..", "", "a/b"])
def test_untrusted_ids_rejected(tmp_path, identifier):
    with pytest.raises(StoreError):
        Store(tmp_path).verify_suite(identifier)


def test_history_snapshot_evidence_and_redaction(tmp_path):
    store, output, source, manifest, _ = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    first = store.create_run(manifest.suite_id, "local")
    second = store.create_run(manifest.suite_id, "local")
    assert first.run_id != second.run_id
    assert first.cases[0].state == CaseState.GENERATED
    assert not first.cases[0].attempted
    evidence = StepEvidence(run_id=first.run_id, case_id="C", requirement_ids=["R"], script="test_c.py", script_hash=manifest.cases[0].script_hash,
                            plan_hash=manifest.plan_hash, product_version="reference", step_index=0, operation_id="read", state="passed", http_status=200,
                            request={"headers": {"Authorization": "synthetic-secret"}}, observed={"echo": "synthetic-secret"})
    path = store.append_evidence(evidence, secrets=["synthetic-secret"])
    assert "synthetic-secret" not in path.read_text()
    another = store.append_evidence(evidence, secrets=["synthetic-secret"])
    assert path != another
    first.cases[0] = first.cases[0].model_copy(update={"state": CaseState.PASSED, "attempted": True, "evidence_paths": [str(path)]})
    store.save_run(first)
    assert store.load_run(first.run_id).cases[0].attempted
    assert sorted(store.list_runs()) == sorted([first.run_id, second.run_id])
    (output / "test_c.py").write_text("# new script\n")
    source.write_text("{}")
    assert store.load_run_plan(first.run_id).cases[0].id == "C"
    assert (store.run_dir(first.run_id) / "suite" / "test_c.py").read_text().startswith("def test_case")
    assert store.load_run(second.run_id).cases[0].state == CaseState.GENERATED


def test_profile_roundtrip_and_default_home(tmp_path, monkeypatch):
    store, _, _, _, _ = registered(tmp_path)
    assert store.load_profile("local").product_version == "reference"
    monkeypatch.setenv("QA_AGENT_HOME", str(tmp_path / "other"))
    assert Store().root == tmp_path / "other"


@pytest.mark.parametrize("text", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_invalid_json_is_not_silently_accepted(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        read_json(path)


@pytest.mark.parametrize("relative", ["plan.json", "environment.json", "resultados.json", "suite/test_c.py"])
def test_historical_corruption_is_explicit(tmp_path, relative):
    store, _, _, manifest, _ = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    run = store.create_run(manifest.suite_id, "local")
    target = store.run_dir(run.run_id) / relative
    target.write_text(target.read_text() + "\n ")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.load_run(run.run_id)


def test_evidence_reader_and_corruption_detection(tmp_path):
    store, _, _, manifest, _ = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    run = store.create_run(manifest.suite_id, "local")
    evidence = StepEvidence(run_id=run.run_id, case_id="C", requirement_ids=["R"], script="test_c.py", script_hash=manifest.cases[0].script_hash,
                            plan_hash=manifest.plan_hash, product_version="reference", step_index=0, operation_id="read", state="technical_error", error="timeout")
    path = store.append_evidence(evidence)
    assert store.load_evidence(run.run_id, path).http_status is None
    assert store.load_evidence(run.run_id, path.relative_to(store.run_dir(run.run_id))) == evidence
    with pytest.raises(IntegrityError):
        store.load_evidence(run.run_id, "../source.json")
    path.unlink()
    with pytest.raises(IntegrityError):
        store.load_run(run.run_id)


def test_evidence_trace_cannot_invent_script_or_operation(tmp_path):
    store, _, _, manifest, _ = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    run = store.create_run(manifest.suite_id, "local")
    evidence = StepEvidence(run_id=run.run_id, case_id="C", requirement_ids=["R"], script="unknown.py", script_hash=manifest.cases[0].script_hash,
                            plan_hash=manifest.plan_hash, product_version="reference", step_index=0, operation_id="read", state="passed")
    with pytest.raises(IntegrityError, match="traceability"):
        store.append_evidence(evidence)


def test_published_json_examples_are_valid():
    import re
    from pathlib import Path
    from qa_agent.models import EnvironmentProfile
    document = (Path(__file__).resolve().parents[1] / "docs" / "API_CONTRACT.md").read_text()
    examples = re.findall(r"```json\n(.*?)\n```", document, flags=re.S)
    assert len(examples) == 2
    plan = Plan.model_validate_json(examples[0])
    profile = EnvironmentProfile.model_validate_json(examples[1])
    assert plan.cases[0].executable and profile.id == "local"


def run_with_evidence(tmp_path):
    store, _, _, manifest, _ = registered(tmp_path)
    store.approve_suite(manifest.suite_id, approved_by="human")
    run = store.create_run(manifest.suite_id, "local")
    evidence = StepEvidence(run_id=run.run_id, case_id="C", requirement_ids=["R"], script="test_c.py", script_hash=manifest.cases[0].script_hash,
                            plan_hash=manifest.plan_hash, product_version="reference", step_index=0, operation_id="read", state="inconclusive")
    return store, run, evidence


def test_numeric_secret_is_redacted_before_evidence_persistence_and_load(tmp_path):
    from qa_agent.security import REDACTED
    store, run, evidence = run_with_evidence(tmp_path)
    evidence.observed = {"echo": 123456, "flag": True}
    evidence.expected = 123456
    evidence.response = {"body": {"reflected": 123456}}
    evidence.captures = {"echo": 123456}
    path = store.append_evidence(evidence, secrets=["123456"])
    assert "123456" not in path.read_text()
    loaded = store.load_evidence(run.run_id, path)
    assert loaded.observed == {"echo": REDACTED, "flag": True}
    assert loaded.expected == loaded.captures["echo"] == REDACTED
    # Aggregate must match the persisted, redacted copy, not leak raw scalar values.
    run.cases[0] = run.cases[0].model_copy(update={"state": CaseState.INCONCLUSIVE, "attempted": True, "steps": [evidence], "evidence_paths": [str(path)]})
    result_path = store.save_run(run, secrets=iter(["123456"]))
    assert "123456" not in result_path.read_text()
    assert store.load_run(run.run_id).cases[0].steps[0].observed["echo"] == REDACTED


@pytest.mark.parametrize("change", [{"step_index": -1}, {"http_status": True}, {"step_index": -1, "http_status": True}])
def test_append_revalidates_mutable_evidence_before_any_write(tmp_path, change):
    store, run, evidence = run_with_evidence(tmp_path)
    directory = store.run_dir(run.run_id)
    before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    with pytest.raises(ValidationError):
        store.append_evidence(evidence.model_copy(update=change))
    assert {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before
    store.verify_run_integrity(run.run_id)


@pytest.mark.parametrize("change", [{"script": "invented.py"}, {"script_hash": "b" * 64}, {"script": None}])
def test_save_run_rejects_invented_case_script_and_hash(tmp_path, change):
    store, run, _ = run_with_evidence(tmp_path)
    before = (store.run_dir(run.run_id) / "resultados.json").read_bytes()
    run.cases[0] = run.cases[0].model_copy(update=change)
    with pytest.raises(IntegrityError, match="script traceability"):
        store.save_run(run)
    assert (store.run_dir(run.run_id) / "resultados.json").read_bytes() == before


@pytest.mark.parametrize("change", [
    {"run_id": "another-run"}, {"case_id": "another-case"}, {"requirement_ids": ["other"]},
    {"plan_hash": "b" * 64}, {"script": "invented.py"}, {"script_hash": "b" * 64},
    {"product_version": "other"}, {"operation_id": "invented"}, {"step_index": 1},
])
def test_save_run_rejects_embedded_evidence_with_invented_trace(tmp_path, change):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    run.cases[0] = run.cases[0].model_copy(update={"state": CaseState.INCONCLUSIVE, "attempted": True, "steps": [evidence.model_copy(update=change)], "evidence_paths": [str(path)]})
    with pytest.raises(IntegrityError, match="traceability"):
        store.save_run(run)
    assert not store.load_run(run.run_id).cases[0].attempted


def test_embedded_evidence_must_match_persisted_content(tmp_path):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    run.cases[0] = run.cases[0].model_copy(update={"state": CaseState.INCONCLUSIVE, "attempted": True, "steps": [evidence.model_copy(update={"observed": "invented observation"})], "evidence_paths": [str(path)]})
    with pytest.raises(IntegrityError, match="matching persisted"):
        store.save_run(run)
    run.cases[0] = run.cases[0].model_copy(update={"steps": [evidence], "evidence_paths": []})
    with pytest.raises(IntegrityError, match="matching persisted"):
        store.save_run(run)


@pytest.mark.parametrize("case_level", [True, False])
def test_save_run_rejects_references_to_another_run_or_missing_evidence(tmp_path, case_level):
    store, run, evidence = run_with_evidence(tmp_path)
    other = store.create_run(run.suite_id, run.environment_id)
    foreign = store.append_evidence(evidence.model_copy(update={"run_id": other.run_id}))
    if case_level:
        run.cases[0].evidence_paths = [str(foreign)]
    else:
        run.evidence_paths = [str(foreign)]
    with pytest.raises(IntegrityError):
        store.save_run(run)
    if case_level:
        run.cases[0].evidence_paths = ["evidence/missing.json"]
    else:
        run.evidence_paths = ["evidence/missing.json"]
    with pytest.raises(IntegrityError):
        store.save_run(run)


def test_provisional_attempt_before_http_needs_no_response_evidence(tmp_path):
    store, run, evidence = run_with_evidence(tmp_path)
    run.cases[0] = run.cases[0].model_copy(update={"state": CaseState.INCONCLUSIVE, "attempted": True})
    store.save_run(run)
    assert store.load_run(run.run_id).cases[0].attempted
    assert store.load_run(run.run_id).cases[0].steps == []
    # A start event with no response can also be embedded once persisted.
    path = store.append_evidence(evidence)
    run.cases[0] = run.cases[0].model_copy(update={"steps": [evidence], "evidence_paths": [str(path)]})
    run.evidence_paths = [str(path)]
    store.save_run(run)
    assert store.load_run(run.run_id).cases[0].steps[0].http_status is None


@pytest.mark.parametrize("secret,echo", [("200", 200), ("true", True), ("null", None), ("123456", 123456), ("passed", "passed"), ("read", "read"), ("C", "C")])
def test_field_aware_redaction_preserves_metadata_through_append_save_load(tmp_path, secret, echo):
    from qa_agent.security import REDACTED
    store, run, evidence = run_with_evidence(tmp_path)
    evidence = evidence.model_copy(update={
        "state": CaseState.PASSED, "http_status": 200, "duration_ms": 200.0,
        "request": {"echo": echo}, "response": {"echo": echo},
        "expected": echo, "observed": echo, "captures": {"echo": echo},
        "assertions": [AssertionResult(kind="equal", passed=True, expected=echo, observed=echo)],
    })
    path = store.append_evidence(evidence, secrets=[secret])
    loaded_evidence = store.load_evidence(run.run_id, path)
    for name in ("run_id", "case_id", "requirement_ids", "plan_hash", "script", "script_hash", "product_version", "step_index", "operation_id", "started_at", "duration_ms", "state", "http_status", "error"):
        assert getattr(loaded_evidence, name) == getattr(evidence, name)
    assert type(loaded_evidence.http_status) is int
    for payload in (loaded_evidence.request, loaded_evidence.response, loaded_evidence.captures):
        assert payload == {"echo": REDACTED}
    assert loaded_evidence.expected == loaded_evidence.observed == REDACTED
    assert loaded_evidence.assertions[0].passed is True
    assert loaded_evidence.assertions[0].kind == "equal"
    assert loaded_evidence.assertions[0].message is None
    assert loaded_evidence.assertions[0].expected == loaded_evidence.assertions[0].observed == REDACTED
    assert evidence.observed is echo  # No mutation before comparisons.
    run.cases[0] = run.cases[0].model_copy(update={"state": CaseState.PASSED, "attempted": True, "critical_defect": True, "steps": [evidence], "evidence_paths": [str(path)]})
    run.evidence_paths = [str(path)]
    store.save_run(run, secrets=[secret])
    loaded = store.load_run(run.run_id)
    for name in ("run_id", "suite_id", "plan_hash", "environment_id", "product_version", "status", "started_at", "ended_at", "result_path", "evidence_paths", "error"):
        assert getattr(loaded, name) == getattr(run, name)
    case = loaded.cases[0]
    assert case.case_id == "C" and case.requirement_ids == ["R"]
    assert case.script == "test_c.py" and case.script_hash == evidence.script_hash
    assert case.attempted is True and case.critical_defect is True
    assert case.reason is None and loaded.ended_at is None and loaded.error is None
    assert case.steps[0] == loaded_evidence
    assert case.steps[0].error is None
    store.verify_run_integrity(run.run_id)


@pytest.mark.parametrize("target", ["append", "save"])
def test_sanitized_models_are_revalidated_before_persistence(tmp_path, monkeypatch, target):
    import qa_agent.store as module
    store, run, evidence = run_with_evidence(tmp_path)
    directory = store.run_dir(run.run_id)
    before = {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()}
    def invalid_redaction(value, secrets):
        result = value.model_dump(mode="json")
        if target == "append":
            result["http_status"] = True
        else:
            result["cases"][0]["attempted"] = "invalid boolean"
        return result
    monkeypatch.setattr(module, "redact", invalid_redaction)
    with pytest.raises(ValidationError):
        if target == "append":
            store.append_evidence(evidence)
        else:
            store.save_run(run)
    assert {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("evidence_count", [0, 1, 12, 24, 48])
def test_save_verifies_once_and_hashes_linearly_per_operation(tmp_path, monkeypatch, evidence_count):
    import qa_agent.store as module
    store, run, evidence = run_with_evidence(tmp_path)
    paths = [str(store.append_evidence(evidence)) for _ in range(evidence_count)]
    run.cases[0].evidence_paths = paths
    run.evidence_paths = paths
    tracked_count = len(read_json(store.run_dir(run.run_id) / "integrity.json")["files"])
    original_verify, original_hash = store.verify_run_integrity, module.hash_file
    verifications, hashes = [], []
    def verify(run_id):
        verifications.append(run_id)
        return original_verify(run_id)
    def hash_file(path):
        hashes.append(path)
        return original_hash(path)
    monkeypatch.setattr(store, "verify_run_integrity", verify)
    monkeypatch.setattr(module, "hash_file", hash_file)
    # Both saves must independently verify, including repeated identical input.
    for _ in range(2):
        verifications.clear()
        hashes.clear()
        store.save_run(run)
        assert verifications == [run.run_id]
        # Full index + case/global evidence reads + recording the new aggregate.
        assert len(hashes) == tracked_count + 2 * evidence_count + 1


@pytest.mark.parametrize("target", ["evidence", "unreferenced_evidence", "plan.json", "environment.json",
                                  "suite/test_c.py", "resultados.json", "index_hash", "index_missing", "index_extra"])
def test_each_public_read_and_save_rechecks_all_historical_artifacts(tmp_path, target):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    unreferenced = store.append_evidence(evidence)
    assert store.load_evidence(run.run_id, path) == evidence
    store.save_run(run)
    directory = store.run_dir(run.run_id)
    if target.startswith("index_"):
        index = read_json(directory / "integrity.json")
        relative = path.relative_to(directory).as_posix()
        if target == "index_hash":
            index["files"][relative] = "b" * 64
        elif target == "index_missing":
            del index["files"][relative]
        else:
            index["files"]["evidence/missing.json"] = "b" * 64
        atomic_write_json(directory / "integrity.json", index)
    else:
        changed = path if target == "evidence" else unreferenced if target == "unreferenced_evidence" else directory / target
        changed.write_bytes(changed.read_bytes() + b"\n ")
    before = (directory / "resultados.json").read_bytes()
    with pytest.raises(IntegrityError):
        store.load_evidence(run.run_id, path)
    with pytest.raises(IntegrityError):
        store.save_run(run)
    assert (directory / "resultados.json").read_bytes() == before


def test_public_evidence_reads_never_share_verification(tmp_path, monkeypatch):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    original = store.verify_run_integrity
    calls = []
    def verify(run_id):
        calls.append(run_id)
        return original(run_id)
    monkeypatch.setattr(store, "verify_run_integrity", verify)
    assert store.load_evidence(run.run_id, path) == evidence
    assert store.load_evidence(run.run_id, path) == evidence
    assert calls == [run.run_id, run.run_id]


@pytest.mark.parametrize("target", ["evidence", "suite"])
def test_symlink_replacement_after_successful_operation_is_rejected(tmp_path, target):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    store.load_evidence(run.run_id, path)
    store.save_run(run)
    changed = path if target == "evidence" else store.run_dir(run.run_id) / "suite" / "test_c.py"
    destination = tmp_path / "same-content"
    destination.write_bytes(changed.read_bytes())
    changed.unlink()
    changed.symlink_to(destination)
    with pytest.raises(IntegrityError, match="Symlinks"):
        store.load_evidence(run.run_id, path)
    with pytest.raises(IntegrityError, match="Symlinks"):
        store.save_run(run)


def test_same_run_id_in_different_store_cannot_share_context(tmp_path):
    import shutil
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    other = Store(tmp_path / "other-state")
    other_directory = other.run_dir(run.run_id)
    shutil.copytree(store.run_dir(run.run_id), other_directory)
    relative = path.relative_to(store.run_dir(run.run_id))
    assert other.load_evidence(run.run_id, relative) == evidence
    with pytest.raises(IntegrityError, match="escaped"):
        other.load_evidence(run.run_id, path)
    (other_directory / relative).write_bytes(b"{}")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        other.load_evidence(run.run_id, relative)
    assert store.load_evidence(run.run_id, path) == evidence


def test_evidence_is_rehashed_on_each_read_even_within_one_save(tmp_path, monkeypatch):
    import qa_agent.store as module
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    run.cases[0].evidence_paths = [str(path)]
    run.evidence_paths = [str(path)]
    before = (store.run_dir(run.run_id) / "resultados.json").read_bytes()
    original = module._validate_evidence_trace
    calls = []
    def validate(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(True)
        if len(calls) == 1:
            path.write_bytes(path.read_bytes() + b"\n ")
    monkeypatch.setattr(module, "_validate_evidence_trace", validate)
    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.save_run(run)
    assert len(calls) == 1
    assert (store.run_dir(run.run_id) / "resultados.json").read_bytes() == before


@pytest.mark.parametrize("operation", ["foreign-case", "wrong-operation"])
def test_persisted_trace_revalidated_after_a_successful_save(tmp_path, operation):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    store.save_run(run)
    value = read_json(path)
    value["case_id" if operation == "foreign-case" else "operation_id"] = "invented"
    atomic_write_json(path, value)
    # Even if a trusted local owner reindexes modified bytes, semantic trace
    # validation must still reject the evidence on the next save.
    store._record_run_file(run.run_id, path.relative_to(store.run_dir(run.run_id)).as_posix())
    run.cases[0].evidence_paths = [str(path)]
    with pytest.raises(IntegrityError, match="traceability"):
        store.save_run(run)


@pytest.mark.parametrize("cross_reference", ["case", "operation"])
def test_legitimate_other_case_and_operation_cannot_be_substituted(tmp_path, cross_reference):
    from qa_agent.generate import generate_suite
    store, _, _, _, plan = registered(tmp_path)
    plan.operations.append(Operation(operation_id="other", method="GET", path="/other"))
    plan.cases.append(Case(id="OTHER", requirement_ids=["R"], objective="Other case", expected="200",
                           steps=[Step(operation_id="other", assertions=[{"status": 200}])]))
    source = save_plan(tmp_path / "two-cases.json", plan)
    manifest = generate_suite(source, tmp_path / "two-cases-suite", store=store)
    store.approve_suite(manifest.suite_id, approved_by="synthetic diagnostic test")
    run = store.create_run(manifest.suite_id, "local")
    mapping = manifest.cases[1 if cross_reference == "case" else 0]
    evidence = StepEvidence(run_id=run.run_id, case_id=mapping.case_id, requirement_ids=["R"],
                            script=mapping.script, script_hash=mapping.script_hash, plan_hash=manifest.plan_hash,
                            product_version="reference", step_index=0, operation_id="other", state="inconclusive")
    if cross_reference == "operation":
        with pytest.raises(IntegrityError, match="traceability"):
            store.append_evidence(evidence)
    else:
        path = store.append_evidence(evidence)
        run.cases[0].evidence_paths = [str(path)]
        with pytest.raises(IntegrityError, match="traceability"):
            store.save_run(run)


def test_save_revalidates_nested_model_mutation_without_mutating_input(tmp_path):
    store, run, evidence = run_with_evidence(tmp_path)
    path = store.append_evidence(evidence)
    run.cases[0] = run.cases[0].model_copy(update={"state": CaseState.INCONCLUSIVE, "attempted": True,
                                               "steps": [evidence.model_copy(update={"http_status": True})],
                                               "evidence_paths": [str(path)]})
    before = (store.run_dir(run.run_id) / "resultados.json").read_bytes()
    with pytest.raises(ValidationError):
        store.save_run(run)
    assert run.cases[0].steps[0].http_status is True
    assert (store.run_dir(run.run_id) / "resultados.json").read_bytes() == before
