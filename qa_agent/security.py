"""Local-only destination policy and explicit, non-persistent credential handling."""
from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Iterable, Mapping
from urllib.parse import quote, quote_plus, urlsplit

from pydantic import BaseModel

from .models import (AssertionResult, Case, CaseResult, EnvironmentProfile,
                     JsonValue, Operation, RunResult, StepEvidence)

REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(r"authorization|proxy.authorization|cookie|password|passwd|secret|token|api.?key|credential", re.I)


class SecurityError(ValueError):
    pass


def validate_base_url(profile: EnvironmentProfile) -> str:
    """Only explicit numeric loopback origins: no DNS/proxy/rebinding ambiguity."""
    parsed = urlsplit(profile.base_url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError:
        raise SecurityError("Base URL requires an explicit numeric loopback IP and valid port") from None
    if parsed.scheme not in {"http", "https"} or not address.is_loopback:
        raise SecurityError("Only HTTP(S) loopback targets are supported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or "\\" in profile.base_url:
        raise SecurityError("Base URL cannot contain credentials, query, fragment or backslash")
    if any(c.isspace() or ord(c) < 32 for c in profile.base_url):
        raise SecurityError("Invalid URL characters")
    if "%" in parsed.path or any(s in {".", ".."} for s in parsed.path.split("/")):
        raise SecurityError("Unsafe base path")
    matches = [t for t in profile.allowed_targets if t.host == parsed.hostname and t.port == port]
    if not matches:
        raise SecurityError("Target host/port is not allowlisted")
    try:
        allowed = {ipaddress.ip_address(ip) for target in matches for ip in target.ips}
    except ValueError:
        raise SecurityError("Allowlist contains an invalid IP") from None
    if address not in allowed or any(not ip.is_loopback for ip in allowed):
        raise SecurityError("Target IP is not explicitly allowlisted loopback")
    return profile.base_url.rstrip("/")


def authorize_operation(profile: EnvironmentProfile, operation: Operation | str) -> None:
    operation_id = operation.operation_id if isinstance(operation, Operation) else operation
    validate_base_url(profile)
    if operation_id not in profile.allowed_operations:
        raise SecurityError("Operation is not authorized by the environment")


def build_url(profile: EnvironmentProfile, operation: Operation, path_values: Mapping[str, JsonValue]) -> str:
    """Resolve only the contract route. Query must be passed as HTTPX params."""
    authorize_operation(profile, operation)
    # Revalidate nested mutable models immediately before building the destination.
    operation = Operation.model_validate(operation.model_dump())
    path = operation.path
    expected = {p.name for p in operation.parameters if p.location == "path"}
    if set(path_values) != expected:
        raise SecurityError("Path values do not match the operation")
    for name, value in path_values.items():
        if value is None or isinstance(value, (dict, list)):
            raise SecurityError("Path values must be non-null scalars")
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if text in {".", "..", ""} or any(c in text for c in "/\\%?#") or any(ord(c) < 32 for c in text):
            raise SecurityError("Unsafe path value")
        path = path.replace("{" + name + "}", quote(text, safe=""))
    if "{" in path or "}" in path:
        raise SecurityError("Unresolved path placeholder")
    return validate_base_url(profile) + path


def resolve_credentials(profile: EnvironmentProfile, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read only the named environment variables. Never include values in errors."""
    environ = os.environ if environ is None else environ
    headers = {}
    for header, variable in profile.credentials.items():
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header) or header.lower() in {"host", "content-length", "transfer-encoding"}:
            raise SecurityError("Unsupported credential header")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise SecurityError("Invalid credential environment variable name")
        value = environ.get(variable)
        if not value:
            raise SecurityError("Required credential is unavailable")
        if "\r" in value or "\n" in value:
            raise SecurityError("Invalid credential value")
        headers[header] = value
    return headers


def redact(value, secrets: Iterable[str] = ()):
    """Return a detached JSON-safe structure; compare raw values before redaction.

    Pass credential VALUES explicitly to redact reflected values, exception text and
    stdout/stderr. Typed result/evidence models preserve authoritative metadata;
    only their free payloads and non-null diagnostic texts are redacted. Generic
    JSON remains a free payload. Never scans the process environment for secrets.
    """
    model_fields = {
        AssertionResult: (("expected", "observed"), ("message",), ()),
        StepEvidence: (("request", "response", "expected", "observed", "captures"), ("error",), ("assertions",)),
        CaseResult: ((), ("reason",), ("steps",)),
        RunResult: ((), ("error",), ("cases",)),
    }
    variants = set()
    scalar_secrets = []
    for secret in secrets:
        if secret:
            variants.update((secret, quote(secret, safe=""), quote_plus(secret)))
            try:
                scalar = json.loads(secret)
            except (ValueError, TypeError):
                continue
            if scalar is None or type(scalar) in {bool, int, float}:
                scalar_secrets.append(scalar)
    ordered = sorted(variants, key=len, reverse=True)

    def clean(item):
        if any(type(item) is type(secret) and item == secret for secret in scalar_secrets):
            return REDACTED
        if isinstance(item, BaseModel):
            for model_type, (payloads, texts, children) in model_fields.items():
                if isinstance(item, model_type):
                    result = item.model_dump(mode="json", by_alias=True)
                    for name in payloads:
                        result[name] = clean(getattr(item, name))
                    for name in texts:
                        text = getattr(item, name)
                        result[name] = clean(text) if text is not None else None
                    for name in children:
                        result[name] = [clean(child) for child in getattr(item, name)]
                    return result
            item = item.model_dump(mode="json", by_alias=True)
        if isinstance(item, Mapping):
            return {clean(str(k)): REDACTED if _SENSITIVE.search(str(k)) else clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item]
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        if isinstance(item, str):
            for secret in ordered:
                item = item.replace(secret, REDACTED)
            # Credentials embedded in URL userinfo or common query/log key-value forms.
            item = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", item)
            item = re.sub(r"(?i)((?:authorization|cookie|password|passwd|secret|token|api[_-]?key)\s*[:=]\s*)[^\s&,;]+", r"\1[REDACTED]", item)
            return item
        return item

    return clean(value)


def missing_preconditions(case: Case, profile: EnvironmentProfile) -> list[str]:
    """Declarative flags only. No shell/eval/network checks are performed."""
    return [name for name in case.preconditions if profile.preconditions.get(name) is not True]


def httpx_client_options(profile: EnvironmentProfile) -> dict:
    """Shared mandatory policy; callers must not override from plan data."""
    validate_base_url(profile)
    return {"timeout": profile.request_timeout_seconds, "follow_redirects": False, "trust_env": False}
