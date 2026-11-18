"""Atomic local persistence, immutable suite registration and approval gates.

The registry and approvals are trusted local state, not a sandbox against the
owner of the account. Never store them inside an untrusted/exported suite.
"""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
import yaml

from .models import (Approval, CaseResult, CaseState, EnvironmentProfile, Plan,
                     RunResult, RunStatus, StepEvidence, SuiteManifest)
from .security import redact, validate_base_url


class StoreError(ValueError):
    pass


class IntegrityError(StoreError):
    pass


class ApprovalError(StoreError):
    pass


def canonical_json(value: Any) -> bytes:
    """UTF-8, sorted object keys, compact separators; arrays retain their order."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def hash_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("Expected a regular, non-symlink file")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def plan_hash(plan: Plan) -> str:
    # Revalidation detects mutation of nested lists/dicts after construction.
    return hash_json(Plan.model_validate_json(plan.model_dump_json(by_alias=True)))


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise StoreError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def read_json(path: Path) -> Any:
    def invalid_constant(_):
        raise StoreError("Non-finite JSON number")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object, parse_constant=invalid_constant)


def atomic_write_json(path: Path, value: Any) -> Path:
    return atomic_write_bytes(path, canonical_json(value) + b"\n")


def atomic_write_bytes(path: Path, value: bytes) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise IntegrityError("Refusing to overwrite a symlink")
    descriptor, temporary = tempfile.mkstemp(prefix=".qa-tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def load_plan(path: Path) -> Plan:
    return Plan.model_validate(read_json(path))


def save_plan(path: Path, plan: Plan) -> Path:
    plan = Plan.model_validate_json(plan.model_dump_json(by_alias=True))
    return atomic_write_json(path, plan)


def load_profile(path: Path) -> EnvironmentProfile:
    path = Path(path)
    value = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix.lower() in {".yaml", ".yml"} else read_json(path)
    profile = EnvironmentProfile.model_validate(value)
    validate_base_url(profile)
    return profile


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise StoreError("Invalid local identifier")
    return value


def safe_suite_file(root: Path, relative: str) -> Path:
    root = Path(root)
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or "\\" in relative or any(p in {"", ".", ".."} for p in relative.split("/")):
        raise IntegrityError("Unsafe suite-relative path")
    current = root
    if root.is_symlink():
        raise IntegrityError("Suite root cannot be a symlink")
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise IntegrityError("Symlinks are forbidden in suites")
    if not current.resolve().is_relative_to(root.resolve()):
        raise IntegrityError("Suite path escaped root")
    return current


def suite_files(root: Path) -> dict[str, str]:
    """Inventory all payload files (manifest excluded); ignore only Python caches."""
    root = Path(root)
    if root.is_symlink():
        raise IntegrityError("Suite root cannot be a symlink")
    files = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        safe_suite_file(root, relative)
        if any(p in {"__pycache__", ".pytest_cache"} for p in path.relative_to(root).parts):
            continue
        if path.is_file() and relative != "manifest.json":
            files[relative] = hash_file(path)
        elif not path.is_dir() and not path.is_file():
            raise IntegrityError("Suite contains a special file")
    return files


def _validate_suite(root: Path, manifest: SuiteManifest) -> Plan:
    if suite_files(root) != manifest.files:
        raise IntegrityError("Suite payload differs from registered hashes")
    if "plan.json" not in manifest.files:
        raise IntegrityError("Suite must contain plan.json")
    plan = load_plan(safe_suite_file(root, "plan.json"))
    if plan_hash(plan) != manifest.plan_hash:
        raise IntegrityError("Suite plan hash mismatch")
    executable = [case for case in plan.cases if case.executable]
    if [case.id for case in executable] != [case.case_id for case in manifest.cases]:
        raise IntegrityError("Manifest case order/membership differs from plan")
    scripts = []
    for case, mapping in zip(executable, manifest.cases):
        if mapping.requirement_ids != case.requirement_ids or manifest.files.get(mapping.script) != mapping.script_hash:
            raise IntegrityError("Manifest traceability/hash mismatch")
        if not mapping.script.endswith(".py"):
            raise IntegrityError("Case script must be Python")
        scripts.append(mapping.script)
    if len(scripts) != len(set(scripts)):
        raise IntegrityError("Case scripts must be unique")
    return plan


def _validate_evidence_trace(evidence: StepEvidence, run_id: str, plan: Plan,
                             manifest: SuiteManifest, product_version: str,
                             case_id: str | None = None) -> None:
    case = next((case for case in plan.cases if case.id == evidence.case_id), None)
    mapping = next((mapping for mapping in manifest.cases if mapping.case_id == evidence.case_id), None)
    if (evidence.run_id != run_id or evidence.plan_hash != manifest.plan_hash
            or case is None or mapping is None
            or (case_id is not None and evidence.case_id != case_id)
            or evidence.requirement_ids != case.requirement_ids
            or evidence.script != mapping.script or evidence.script_hash != mapping.script_hash
            or evidence.product_version != product_version
            or not 0 <= evidence.step_index < len(case.steps)
            or evidence.operation_id != case.steps[evidence.step_index].operation_id):
        raise IntegrityError("Evidence traceability mismatch")


class _VerifiedRunEvidence:
    """Operation-local reader; never retained on a Store or reused after writes.

    Verify every historical artifact once, then keep the run/directory/index
    bound together. Each evidence read still checks its path, hash and model;
    only the redundant full-run scans are avoided.
    """
    def __init__(self, store: Store, run_id: str):
        store.verify_run_integrity(run_id)
        self._run_id = run_id
        self._directory = store.run_dir(run_id)
        self._tracked = read_json(self._directory / "integrity.json")["files"]

    def load(self, evidence_path: Path | str) -> StepEvidence:
        path = Path(evidence_path)
        if path.is_absolute():
            try:
                relative = path.relative_to(self._directory).as_posix()
            except ValueError:
                raise IntegrityError("Evidence path escaped run") from None
        else:
            relative = path.as_posix()
        if not relative.startswith("evidence/") or relative not in self._tracked:
            raise IntegrityError("Evidence must be registered in the run evidence directory")
        path = safe_suite_file(self._directory, relative)
        if hash_file(path) != self._tracked[relative]:
            raise IntegrityError("Historical artifact hash mismatch")
        evidence = StepEvidence.model_validate(read_json(path))
        if evidence.run_id != self._run_id:
            raise IntegrityError("Evidence run identity mismatch")
        return evidence


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("QA_AGENT_HOME") or Path(__file__).resolve().parents[1] / "data").resolve()

    def _path(self, kind: str, identifier: str, filename: str) -> Path:
        return self.root / kind / _identifier(identifier) / filename

    def save_plan(self, plan: Plan, plan_id: str | None = None) -> Path:
        identifier = _identifier(plan_id or "plan-" + uuid4().hex)
        directory = self.root / "plans" / identifier
        directory.mkdir(parents=True, exist_ok=False)
        return save_plan(directory / "plan.json", plan)

    def save_profile(self, profile: EnvironmentProfile) -> Path:
        validate_base_url(profile)
        return atomic_write_json(self._path("environments", profile.id, "profile.json"), profile)

    def load_profile(self, environment_id: str) -> EnvironmentProfile:
        profile = load_profile(self._path("environments", environment_id, "profile.json"))
        if profile.id != environment_id:
            raise IntegrityError("Environment ID mismatch")
        return profile

    def register_suite(self, suite_path: Path, manifest: SuiteManifest) -> SuiteManifest:
        suite_path = Path(suite_path)
        if suite_path.is_symlink():
            raise IntegrityError("Suite root cannot be a symlink")
        suite_path = suite_path.resolve()
        source = Path(manifest.source_plan_path)
        if not source.is_absolute() or source.is_symlink():
            raise IntegrityError("source_plan_path must be an absolute regular path")
        if plan_hash(load_plan(source)) != manifest.plan_hash:
            raise IntegrityError("Original plan differs from generated plan")
        _validate_suite(suite_path, manifest)
        registry = self._path("suites", manifest.suite_id, "registration.json")
        registry.parent.mkdir(parents=True, exist_ok=False)
        atomic_write_json(safe_suite_file(suite_path, "manifest.json"), manifest)
        atomic_write_json(registry, {"suite_path": str(suite_path), "manifest_hash": hash_json(manifest)})
        return manifest

    def verify_suite(self, suite_id: str, *, require_approval: bool = True) -> tuple[Path, SuiteManifest, Plan]:
        registration = read_json(self._path("suites", suite_id, "registration.json"))
        root = Path(registration["suite_path"])
        manifest = SuiteManifest.model_validate(read_json(safe_suite_file(root, "manifest.json")))
        if manifest.suite_id != suite_id or hash_json(manifest) != registration["manifest_hash"]:
            raise IntegrityError("Manifest differs from trusted registration")
        plan = _validate_suite(root, manifest)
        try:
            source = Path(manifest.source_plan_path)
            if source.is_symlink() or plan_hash(load_plan(source)) != manifest.plan_hash:
                raise IntegrityError("Original plan changed; regenerate and approve again")
        except (OSError, ValueError) as exc:
            raise IntegrityError("Original plan unavailable or changed; regenerate the suite") from exc
        if require_approval:
            try:
                approval = Approval.model_validate(read_json(self._path("suites", suite_id, "approval.json")))
            except (OSError, ValueError) as exc:
                raise ApprovalError("Explicit human approval is required") from exc
            if approval.suite_id != suite_id or approval.plan_hash != manifest.plan_hash or approval.manifest_hash != hash_json(manifest):
                raise ApprovalError("Approval is stale or belongs to a different suite")
        return root, manifest, plan

    def approve_suite(self, suite_id: str, *, approved_by: str) -> Approval:
        _, manifest, _ = self.verify_suite(suite_id, require_approval=False)
        approval = Approval(suite_id=suite_id, plan_hash=manifest.plan_hash,
                            manifest_hash=hash_json(manifest), approved_by=approved_by)
        atomic_write_json(self._path("suites", suite_id, "approval.json"), approval)
        return approval

    def suite_id_for_path(self, suite_path: Path) -> str:
        suite_path = Path(suite_path).resolve()
        manifest = SuiteManifest.model_validate(read_json(safe_suite_file(suite_path, "manifest.json")))
        registered, _, _ = self.verify_suite(manifest.suite_id, require_approval=False)
        if registered != suite_path:
            raise IntegrityError("Suite path is not the registered directory")
        return manifest.suite_id

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / _identifier(run_id)

    def create_run(self, suite_id: str, environment_id: str) -> RunResult:
        suite_path, manifest, plan = self.verify_suite(suite_id)
        profile = self.load_profile(environment_id)
        run_id = "run-" + uuid4().hex
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "evidence").mkdir()
        snapshot = directory / "suite"
        snapshot.mkdir()
        # Copy only verified payload, then verify copied bytes before any execution.
        for relative in [*manifest.files, "manifest.json"]:
            source = safe_suite_file(suite_path, relative)
            target = safe_suite_file(snapshot, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target, follow_symlinks=False)
        _validate_suite(snapshot, manifest)
        if hash_json(SuiteManifest.model_validate(read_json(snapshot / "manifest.json"))) != hash_json(manifest):
            raise IntegrityError("Manifest changed during snapshot")
        save_plan(directory / "plan.json", plan)
        atomic_write_json(directory / "environment.json", profile)
        tracked = {"plan.json": hash_file(directory / "plan.json"),
                   "environment.json": hash_file(directory / "environment.json")}
        tracked.update({"suite/" + relative: hash_file(snapshot / relative)
                        for relative in [*manifest.files, "manifest.json"]})
        atomic_write_json(directory / "integrity.json", {"files": tracked})
        mappings = {c.case_id: c for c in manifest.cases}
        result = RunResult(run_id=run_id, suite_id=suite_id, plan_hash=manifest.plan_hash,
                           environment_id=environment_id, product_version=profile.product_version,
                           status=RunStatus.PENDING, result_path=str(directory / "resultados.json"),
                           cases=[CaseResult(case_id=c.id, requirement_ids=c.requirement_ids,
                                             script=mappings[c.id].script if c.id in mappings else None,
                                             script_hash=mappings[c.id].script_hash if c.id in mappings else None,
                                             state=CaseState.GENERATED if c.executable else CaseState.DESIGNED,
                                             reason=c.automation_pending_reason) for c in plan.cases])
        self.save_run(result)
        return result

    def save_run(self, result: RunResult, *, secrets: Iterable[str] = ()) -> Path:
        secrets = tuple(secrets)
        result = RunResult.model_validate_json(result.model_dump_json())
        directory = self.run_dir(result.run_id)
        if not (directory / "plan.json").is_file():
            raise StoreError("Create a run before updating results")
        evidence_reader = _VerifiedRunEvidence(self, result.run_id)
        plan = load_plan(directory / "plan.json")
        manifest = SuiteManifest.model_validate(read_json(directory / "suite" / "manifest.json"))
        profile = EnvironmentProfile.model_validate(read_json(directory / "environment.json"))
        if plan_hash(plan) != result.plan_hash or result.suite_id != manifest.suite_id:
            raise IntegrityError("Run plan/suite identity mismatch")
        if result.environment_id != profile.id or result.product_version != profile.product_version:
            raise IntegrityError("Run environment identity mismatch")
        if [(c.case_id, c.requirement_ids) for c in result.cases] != [(c.id, c.requirement_ids) for c in plan.cases]:
            raise IntegrityError("Run case traceability mismatch")
        mappings = {mapping.case_id: mapping for mapping in manifest.cases}
        for case_result in result.cases:
            mapping = mappings.get(case_result.case_id)
            expected_script = (mapping.script, mapping.script_hash) if mapping else (None, None)
            if (case_result.script, case_result.script_hash) != expected_script:
                raise IntegrityError("Run script traceability mismatch")
            persisted = set()
            for evidence_path in case_result.evidence_paths:
                evidence = evidence_reader.load(evidence_path)
                _validate_evidence_trace(evidence, result.run_id, plan, manifest,
                                         result.product_version, case_result.case_id)
                persisted.add(hash_json(evidence))
            for evidence in case_result.steps:
                _validate_evidence_trace(evidence, result.run_id, plan, manifest,
                                         result.product_version, case_result.case_id)
                if hash_json(redact(evidence, secrets)) not in persisted:
                    raise IntegrityError("Embedded evidence has no matching persisted reference")
        for evidence_path in result.evidence_paths:
            evidence = evidence_reader.load(evidence_path)
            _validate_evidence_trace(evidence, result.run_id, plan, manifest, result.product_version)
        sanitized = RunResult.model_validate(redact(result, secrets))
        path = atomic_write_json(directory / "resultados.json", sanitized)
        self._record_run_file(result.run_id, "resultados.json")
        return path

    def load_run(self, run_id: str) -> RunResult:
        self.verify_run_integrity(run_id)
        result = RunResult.model_validate(read_json(self.run_dir(run_id) / "resultados.json"))
        if result.run_id != run_id or plan_hash(self.load_run_plan(run_id)) != result.plan_hash:
            raise IntegrityError("Historical result/plan mismatch")
        return result

    def load_run_plan(self, run_id: str) -> Plan:
        self.verify_run_integrity(run_id)
        return load_plan(self.run_dir(run_id) / "plan.json")

    def _record_run_file(self, run_id: str, relative: str) -> None:
        directory = self.run_dir(run_id)
        index = read_json(directory / "integrity.json")
        index["files"][relative] = hash_file(safe_suite_file(directory, relative))
        atomic_write_json(directory / "integrity.json", index)

    def verify_run_integrity(self, run_id: str) -> None:
        """Detect damaged/missing historical files; report generation needs no HTTP."""
        directory = self.run_dir(run_id)
        try:
            tracked = read_json(directory / "integrity.json")["files"]
            if not isinstance(tracked, dict) or "plan.json" not in tracked:
                raise IntegrityError("Missing historical integrity metadata")
            for relative, expected in tracked.items():
                if hash_file(safe_suite_file(directory, relative)) != expected:
                    raise IntegrityError("Historical artifact hash mismatch")
            observed = {p.relative_to(directory).as_posix() for p in (directory / "evidence").glob("*.json")}
            if observed != {relative for relative in tracked if relative.startswith("evidence/")}:
                raise IntegrityError("Unindexed or missing historical evidence")
        except (OSError, KeyError, TypeError) as exc:
            raise IntegrityError("Missing or corrupt historical artifacts") from exc

    def load_evidence(self, run_id: str, evidence_path: Path | str) -> StepEvidence:
        # A fresh context on every public call: no cross-operation trust/cache.
        return _VerifiedRunEvidence(self, run_id).load(evidence_path)

    def append_evidence(self, evidence: StepEvidence, *, secrets: Iterable[str] = ()) -> Path:
        # model_copy and mutations of nested containers bypass assignment validation.
        # Revalidate before indexing the plan or creating any evidence artifact.
        evidence = StepEvidence.model_validate_json(evidence.model_dump_json())
        directory = self.run_dir(evidence.run_id)
        if not directory.is_dir():
            raise StoreError("Unknown run")
        plan = self.load_run_plan(evidence.run_id)
        result = self.load_run(evidence.run_id)
        manifest = SuiteManifest.model_validate(read_json(directory / "suite" / "manifest.json"))
        _validate_evidence_trace(evidence, evidence.run_id, plan, manifest, result.product_version)
        sanitized = StepEvidence.model_validate(redact(evidence, secrets))
        name = f"{evidence.case_id}-{evidence.step_index:04d}-{uuid4().hex}.json"
        path = atomic_write_json(directory / "evidence" / name, sanitized)
        self._record_run_file(evidence.run_id, "evidence/" + name)
        return path

    def list_runs(self) -> list[str]:
        directory = self.root / "runs"
        return sorted(p.name for p in directory.iterdir() if p.is_dir() and (p / "resultados.json").is_file()) if directory.exists() else []
