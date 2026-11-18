"""Export self-contained, reviewable pytest suites without network or installation."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from jinja2 import Environment, StrictUndefined

from .models import Plan, SuiteCase, SuiteManifest
from .store import (IntegrityError, Store, atomic_write_bytes, load_plan, plan_hash,
                    save_plan, suite_files)

# This closed inventory is also the runtime's code/config allowlist. No payload
# filename, import, pytest argument or dependency is obtained from plan data.
_BUNDLED = ("__init__.py", "models.py", "security.py", "store.py", "runtime.py", "generate.py")
# Complete installed dependency closure, verified in the shared Python 3.14.7
# environment. Export needs no qa-agent installation, MCP package or source tree.
_LOCK = """annotated-types==0.8.0
anyio==4.16.0
certifi==2026.10.23
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.20
iniconfig==2.3.1
Jinja2==3.1.6
MarkupSafe==3.0.4
packaging==26.3
pluggy==1.7.0
pydantic==2.14.3
pydantic_core==2.50.4
Pygments==2.21.0
pytest==9.2.0
PyYAML==6.0.3
typing-inspection==0.4.5
typing_extensions==4.16.0
"""


def _payloads(plan: Plan) -> dict[str, bytes]:
    package = Path(__file__).resolve().parent
    template = (package / "templates" / "case.py.j2").read_text(encoding="utf-8")
    render = Environment(undefined=StrictUndefined, keep_trailing_newline=True).from_string(template)
    payload = {
        "pytest.ini": b"[pytest]\naddopts = -p no:cacheprovider\npython_files = test_*.py\n",
        "requirements.lock": _LOCK.encode(),
        "conftest.py": (package / "templates" / "conftest.py").read_bytes(),
    }
    for name in _BUNDLED:
        payload["_qa_runtime/" + name] = (package / name).read_bytes()
    for name in ("case.py.j2", "conftest.py"):
        payload["_qa_runtime/templates/" + name] = (package / "templates" / name).read_bytes()
    for index, case in enumerate(plan.cases):
        if case.executable:
            # Index avoids case-sensitive filesystem collisions and hostile IDs.
            payload[f"test_{index:04d}.py"] = render.render(case_id=repr(case.id)).encode()
    return payload


def verify_generated(root: Path, plan: Plan) -> None:
    """Registration alone cannot authorize arbitrary Python/config as a suite."""
    expected = _payloads(plan)
    if set(suite_files(root)) != {"plan.json", *expected}:
        raise IntegrityError("Not a generator-produced payload inventory")
    if any((root / name).read_bytes() != value for name, value in expected.items()):
        raise IntegrityError("Suite code/config differs from trusted generator")


def generate_suite(plan_path: Path, output: Path, *, store: Store | None = None) -> SuiteManifest:
    store = store or Store()
    source = Path(plan_path).absolute()
    if source.is_symlink():
        raise IntegrityError("Source plan cannot be a symlink")
    source = source.resolve()
    plan = load_plan(source)
    output = Path(output).absolute()
    if output.is_symlink():
        raise IntegrityError("Output cannot be a symlink")
    output.mkdir(parents=True, exist_ok=False)
    save_plan(output / "plan.json", plan)
    for relative, content in _payloads(plan).items():
        atomic_write_bytes(output / relative, content)
    files = suite_files(output)
    manifest = SuiteManifest(
        suite_id="suite-" + uuid4().hex, plan_hash=plan_hash(plan),
        source_plan_path=str(source), files=files,
        cases=[SuiteCase(case_id=case.id, requirement_ids=case.requirement_ids,
                         script=f"test_{index:04d}.py", script_hash=files[f"test_{index:04d}.py"])
               for index, case in enumerate(plan.cases) if case.executable],
    )
    return store.register_suite(output.resolve(), manifest)
