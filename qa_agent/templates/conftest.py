"""Trusted generated bridge; controlled workers execute only verified snapshots."""
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from _qa_runtime.runtime import direct_results, execute_case


def pytest_collection_modifyitems(items):
    import json
    manifest = json.loads((ROOT / "manifest.json").read_text())
    order = {entry["script"]: index for index, entry in enumerate(manifest["cases"])}
    items.sort(key=lambda item: order.get(item.path.name, len(order)))


@pytest.fixture(scope="session")
def qa_runtime(request):
    if os.environ.get("QA_RUNTIME_RUN"):
        def invoke(case_id):
            result = execute_case(os.environ["QA_RUNTIME_RUN"], case_id, ROOT)
            if result.state.value == "blocked":
                pytest.skip("Execution precondition unavailable")
            if result.state.value != "passed":
                pytest.fail("Recorded case state: " + result.state.value, pytrace=False)
        return invoke
    scripts = [item.path.name for item in request.session.items]
    results = direct_results(ROOT, scripts)
    def invoke(case_id):
        case = next(case for case in results.cases if case.case_id == case_id)
        if case.state.value == "blocked":
            pytest.skip("Execution precondition unavailable")
        if case.state.value != "passed":
            pytest.fail("Recorded case state: " + case.state.value, pytrace=False)
    return invoke
