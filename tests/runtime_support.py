"""Disposable loopback fixtures shared only by runtime-owned tests."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

from qa_agent.generate import generate_suite
from qa_agent.models import Case, EnvironmentProfile, Operation, Parameter, Plan, Requirement, Step
from qa_agent.store import Store, save_plan


@contextmanager
def local_http(*, response_value=True):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            self.do_GET()
        def do_GET(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append({"method": self.command, "path": self.path, "body": raw, "auth": self.headers.get("Authorization")})
            status, body, headers = 200, json.dumps({"id": 7, "value": response_value, "items": [{"a/b": None}]}).encode(), {}
            if self.path.startswith("/slow"):
                time.sleep(1)
            if self.path.startswith("/invalid"):
                body = b'not json'
            if self.path.startswith("/empty"):
                status, body = 204, b""
            if self.path.startswith("/null"):
                body = b"null"
            if self.path.startswith("/large"):
                body = b"x" * 4096
            if self.path.startswith("/redirect"):
                status, headers = 302, {"Location": "/target"}
            if self.path.startswith("/reflect"):
                body = json.dumps({"reflection": self.headers.get("Authorization"), "nested": {"cookie": "hidden"}}).encode()
                if self.path.startswith("/reflect_unicode"):
                    # Escape every ASCII character too, not only quotes/backslashes.
                    reflected = '"' + "".join(f"\\u{ord(char):04x}" for char in self.headers.get("Authorization", "")) + '"'
                    body = ('{"reflection":' + reflected + ',"nested":{"cookie":"hidden"}}').encode()
            if self.path.startswith("/reject"):
                status, body = 422, b'{"error":"invalid"}'
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def prepare(tmp_path: Path, port: int, *, cases=None, operations=None, approved=True, profile_changes=None):
    store = Store(tmp_path / "state")
    operations = operations or [Operation(operation_id="read", method="GET", path="/read"),
                                Operation(operation_id="create", method="POST", path="/create", request_body={"content": {"application/json": {"schema": {}}}}),
                                Operation(operation_id="get", method="GET", path="/items/{id}",
                                          parameters=[Parameter(name="id", location="path", required=True)])]
    if cases is None:
        cases = [Case(id="CP-01", requirement_ids=["R"], objective="capture and read", expected="strict true",
                      steps=[Step(operation_id="create", body={"value": True}, assertions=[{"status": 200}, {"presente": "/id"}], captures={"id": "/id", "original": ""}),
                             Step(operation_id="get", path={"id": {"var": "id"}}, assertions=[{"status": 200}, {"igual": "/value", "valor": True}, {"igual": "", "valor": {"var": "original"}}])])]
    plan = Plan(requirements=[Requirement(id="R", text="read resource")], contract_hash="a" * 64,
                operations=operations, cases=cases)
    source = save_plan(tmp_path / "source.json", plan)
    manifest = generate_suite(source, tmp_path / "suite", store=store)
    profile = EnvironmentProfile(id="local", base_url=f"http://127.0.0.1:{port}",
                                 allowed_targets=[{"host": "127.0.0.1", "port": port, "ips": ["127.0.0.1"]}],
                                 allowed_operations=[operation.operation_id for operation in operations],
                                 product_version="fixture", **(profile_changes or {}))
    store.save_profile(profile)
    if approved:
        store.approve_suite(manifest.suite_id, approved_by="fixture operator")
    return store, manifest, plan, source


def one_case(steps=None, **kwargs):
    return Case(id="C", requirement_ids=["R"], objective="fixture", expected="fixture response",
                steps=steps or [Step(operation_id="read", assertions=[{"status": 200}])], **kwargs)
