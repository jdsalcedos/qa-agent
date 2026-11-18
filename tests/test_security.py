import json
from urllib.parse import quote

import pytest

from qa_agent.models import AssertionResult, Case, EnvironmentProfile, Operation, Parameter
from qa_agent.security import (
    REDACTED, SecurityError, authorize_operation, build_url, httpx_client_options,
    missing_preconditions, redact, resolve_credentials, validate_base_url,
)


def profile():
    return EnvironmentProfile(id="local", base_url="http://127.0.0.1:8765", allowed_targets=[{"host": "127.0.0.1", "port": 8765, "ips": ["127.0.0.1"]}], allowed_operations=["read"], product_version="reference")


def operation():
    return Operation(operation_id="read", method="GET", path="/resources/{id}", parameters=[Parameter(name="id", location="path", required=True)])


def test_explicit_loopback_and_httpx_policy():
    p = profile()
    assert validate_base_url(p) == p.base_url
    assert build_url(p, operation(), {"id": 3}) == "http://127.0.0.1:8765/resources/3"
    assert httpx_client_options(p) == {"timeout": 10.0, "follow_redirects": False, "trust_env": False}
    p.base_url += "/api/"
    assert build_url(p, operation(), {"id": "hello world"}).endswith("/api/resources/hello%20world")


@pytest.mark.parametrize("url", ["http://localhost:8765", "http://example.com:8765", "http://169.254.169.254:8765", "http://10.0.0.1:8765", "http://127.1:8765", "http://2130706433:8765", "http://0x7f000001:8765", "file:///tmp/a", "http://127.0.0.1:8766", "http://user:pass@127.0.0.1:8765", "http://127.0.0.1:8765/?x=1", "http://127.0.0.1:8765/#x", "http://127.0.0.1:8765/../x", "http://127.0.0.1:8765/%2f", "http://127.0.0.1:8765\\x"])
def test_unauthorized_destinations_never_resolve_dns(url, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("DNS is forbidden"))
    p = profile()
    p.base_url = url
    with pytest.raises(SecurityError):
        validate_base_url(p)


@pytest.mark.parametrize("value", ["..", ".", "", "../x", "//evil", "x?y", "x#y", "%2f", "x\\y", "x\ny", None, [], {}])
def test_path_escape_rejection(value):
    with pytest.raises(SecurityError):
        build_url(profile(), operation(), {"id": value})


def test_allowlist_ip_and_operation_gate():
    p = profile()
    with pytest.raises(SecurityError):
        authorize_operation(p, "delete")
    p.allowed_targets[0].ips = ["127.0.0.2"]
    with pytest.raises(SecurityError):
        validate_base_url(p)
    with pytest.raises(SecurityError):
        build_url(profile(), operation(), {"extra": 1})


def test_ipv6_numeric_loopback():
    p = EnvironmentProfile(id="v6", base_url="http://[::1]:8765", allowed_targets=[{"host": "::1", "port": 8765, "ips": ["::1"]}], allowed_operations=["read"], product_version="reference")
    assert build_url(p, operation(), {"id": 1}) == "http://[::1]:8765/resources/1"


def test_credentials_only_named_env_values_and_safe_errors():
    class NamedOnly(dict):
        def __iter__(self):
            pytest.fail("Must not enumerate environment")
        def get(self, key):
            assert key == "SYNTHETIC_TEST_TOKEN"
            return super().get(key)
    p = profile()
    p.credentials = {"Authorization": "SYNTHETIC_TEST_TOKEN"}
    assert resolve_credentials(p, NamedOnly(SYNTHETIC_TEST_TOKEN="synthetic-canary")) == {"Authorization": "synthetic-canary"}
    with pytest.raises(SecurityError) as error:
        resolve_credentials(p, {})
    assert "synthetic-canary" not in str(error.value)
    p.credentials = {"Host": "SYNTHETIC_TEST_TOKEN"}
    with pytest.raises(SecurityError):
        resolve_credentials(p, {"SYNTHETIC_TEST_TOKEN": "synthetic-canary"})


def test_redaction_reflected_values_urls_nested_and_logs():
    secret = "canary/secret+test"
    payload = {"headers": {"Authorization": "Bearer abc", "Cookie": "foo"}, "body": {"password": "abc", "safe": [secret, None]}, "url": "http://u:p@127.0.0.1/path?token=abc", "stdout": f"failed: {secret} {quote(secret, safe='')}"}
    cleaned = redact(payload, [secret])
    encoded = json.dumps(cleaned)
    assert secret not in encoded
    assert quote(secret, safe="") not in encoded
    assert "Bearer abc" not in encoded
    assert "u:p@" not in encoded
    assert cleaned["body"]["safe"][1] is None
    assert cleaned["headers"]["Authorization"] == REDACTED
    assert payload["body"]["safe"][0] == secret  # no mutation before comparisons
    assert redact(b"token=abc") == "token=[REDACTED]"


def test_preconditions_are_declarative_and_pre_http():
    case = Case(id="C", requirement_ids=["R"], objective="o", expected="e", preconditions=["seed", "data"], automation_pending_reason="pending")
    p = profile()
    p.preconditions = {"seed": True, "data": False}
    assert missing_preconditions(case, p) == ["data"]


@pytest.mark.parametrize("secret,value", [("123456", 123456), ("123456.5", 123456.5), ("true", True), ("null", None)])
def test_redaction_matches_known_json_scalars(secret, value):
    payload = {"nested": [{"echo": value}], "other": 2}
    cleaned = redact(payload, [secret])
    assert cleaned["nested"][0]["echo"] == REDACTED
    assert payload["nested"][0]["echo"] is value
    assert cleaned["other"] == 2


@pytest.mark.parametrize("secret,masked,untouched", [("1", 1, True), ("0", 0, False), ("true", True, 1), ("false", False, 0)])
def test_scalar_redaction_does_not_equate_booleans_and_integers(secret, masked, untouched):
    cleaned = redact([masked, untouched], [secret])
    assert cleaned[0] == REDACTED
    assert cleaned[1] is untouched


def test_typed_assertion_redaction_preserves_authoritative_flags_and_null_text():
    assertion = AssertionResult(kind="equal", passed=True, expected=True, observed=None)
    cleaned = redact(assertion, ["true", "null", "equal"])
    assert cleaned == {"kind": "equal", "passed": True, "expected": REDACTED, "observed": REDACTED, "message": None}
    assert AssertionResult.model_validate(cleaned).passed is True
    assertion.message = "echo true null equal"
    assert redact(assertion, ["true", "null", "equal"])["message"] == "echo [REDACTED] [REDACTED] [REDACTED]"
